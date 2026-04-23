#!/usr/bin/env python3
import asyncio
import os
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import firebase_admin
import httpx
import motor.motor_asyncio
from fastapi import BackgroundTasks, HTTPException
from firebase_admin import credentials, messaging
from pydantic import BaseModel, Field
from pymongo import IndexModel

from app.config import DB_NAME, MONGO_URI, SERVER_API_HOST, STATIC_API_KEY
from app.models.models import CallerTokenRequest
from app.utils.mongodb_org import org_filter

import logging

logger = logging.getLogger(__name__)


def format_device_info_string(device_info: dict) -> str:
    if not device_info or not isinstance(device_info, dict):
        return "Unknown Device"
    parts: List[str] = []
    device_type = (
        device_info.get("device_type")
        or device_info.get("type")
        or device_info.get("platform", "")
    )
    if isinstance(device_type, str) and device_type:
        parts.append(device_type.title())
    if device_info.get("browser"):
        parts.append(f"({device_info['browser']})")
    os_info = device_info.get("os") or device_info.get("operating_system", "")
    if os_info:
        parts.append(f"on {os_info}")
    model = device_info.get("model", "")
    if model:
        parts.append(f"({model})")
    return " ".join(parts) if parts else "Unknown Device"


client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
database = client[DB_NAME]
notification_tokens_collection = database.notification_tokens
notification_logs_collection = database.notification_logs


def initialize_firebase() -> bool:
    try:
        if firebase_admin._apps:
            return True
        credentials_path = os.getenv(
            "FIREBASE_CREDENTIALS_PATH",
            os.path.join("app", "credentials", "firebase-service-account.json"),
        )
        if not os.path.exists(credentials_path):
            logger.warning("Firebase credentials not found; notification sending disabled")
            return False
        cred = credentials.Certificate(credentials_path)
        firebase_admin.initialize_app(cred)
        return True
    except Exception as exc:
        logger.warning(f"Firebase init failed: {exc}")
        return False


class NotificationPriority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"


class DeviceType(str, Enum):
    WEB = "web"
    ANDROID = "android"
    IOS = "ios"


class RegisterTokenRequest(BaseModel):
    user_id: str
    user_type: str = "driver"
    device_token: str
    device_type: DeviceType
    new_device_token: Optional[str] = None
    device_info: Optional[Dict[str, Any]] = None


class SendNotificationRequest(BaseModel):
    user_ids: Optional[List[str]] = None
    device_tokens: Optional[List[str]] = None
    title: str
    body: str
    data: Optional[Dict[str, Any]] = None
    priority: NotificationPriority = NotificationPriority.NORMAL
    click_action: Optional[str] = None
    icon: Optional[str] = None
    image: Optional[str] = None


class BroadcastNotificationRequest(BaseModel):
    title: str
    body: str
    data: Optional[Dict[str, Any]] = None
    topic: str = "all_users"
    priority: NotificationPriority = NotificationPriority.NORMAL


class NotificationResponse(BaseModel):
    success: bool
    message: str
    sent_count: int = 0
    failed_count: int = 0
    errors: Optional[List[str]] = None


class BulkNotificationRequest(BaseModel):
    notifications: List[Dict[str, Any]]
    batch_size: int = 500


class SubscribeTopicRequest(BaseModel):
    device_tokens: List[str]
    topic: str


class DataMessageRequest(BaseModel):
    user_ids: Optional[List[str]] = None
    device_tokens: Optional[List[str]] = None
    data: Dict[str, str]
    priority: NotificationPriority = NotificationPriority.NORMAL


async def create_indexes():
    try:
        await notification_tokens_collection.create_indexes(
            [
                IndexModel([("device_token", 1)], unique=True),
                IndexModel([("user_id", 1)]),
                IndexModel([("user_type", 1)]),
                IndexModel([("is_active", 1)]),
            ]
        )
        await notification_logs_collection.create_indexes(
            [
                IndexModel([("user_id", 1)]),
                IndexModel([("sent_at", -1)]),
                IndexModel([("status", 1)]),
            ]
        )
    except Exception as exc:
        logger.warning(f"create_indexes warning: {exc}")


class NotificationService:
    notification_tokens_collection = notification_tokens_collection
    notification_logs_collection = notification_logs_collection

    def __init__(self) -> None:
        self.firebase_initialized = initialize_firebase()

    async def register_device_token(
        self,
        user_id: str,
        user_type: str,
        device_token: str,
        device_type: str,
        new_device_token: Optional[str] = None,
        device_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        now = datetime.utcnow()
        target_token = new_device_token or device_token
        await notification_tokens_collection.update_one(
            {"device_token": device_token},
            {
                "$set": {
                    "user_id": user_id,
                    "user_type": user_type,
                    "device_type": device_type,
                    "device_info": device_info or {},
                    "is_active": True,
                    "updated_at": now,
                    "device_token": target_token,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return True

    async def unregister_device_token(
        self, user_id: str, user_type: str, device_token: str, device_type: str
    ) -> bool:
        await notification_tokens_collection.update_one(
            {"device_token": device_token, "user_id": user_id},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
        )
        return True

    async def get_user_tokens(
        self, user_id: str, user_type: Optional[str] = None, organization_id: Optional[str] = None
    ) -> List[str]:
        tokens: List[str] = []
        query: Dict[str, Any] = {"user_id": user_id, "is_active": True}
        if user_type:
            query["user_type"] = user_type
        if organization_id:
            query["organization_id"] = organization_id
        cursor = notification_tokens_collection.find(query)
        async for token_doc in cursor:
            tokens.append(token_doc["device_token"])
        return tokens

    async def get_all_active_tokens(
        self, device_type: Optional[str] = None, limit: int = 1000
    ) -> List[str]:
        query: Dict[str, Any] = {"is_active": True}
        if device_type:
            query["device_type"] = device_type
        tokens: List[str] = []
        cursor = notification_tokens_collection.find(query).limit(limit)
        async for token_doc in cursor:
            tokens.append(token_doc["device_token"])
        return tokens

    async def validate_single_token(self, token: str) -> bool:
        if not self.firebase_initialized:
            return False
        try:
            message = messaging.Message(data={"validate": "1"}, token=token)
            messaging.send(message, dry_run=True)
            return True
        except Exception:
            return False

    async def _deactivate_token(self, token: str):
        await notification_tokens_collection.update_one(
            {"device_token": token},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
        )

    async def log_notification(
        self,
        user_id: Optional[str],
        title: str,
        body: str,
        data: Optional[Dict] = None,
        status: str = "sent",
        message_id: Optional[str] = None,
    ):
        await notification_logs_collection.insert_one(
            {
                "user_id": user_id,
                "title": title,
                "body": body,
                "data": data,
                "status": status,
                "message_id": message_id,
                "sent_at": datetime.utcnow(),
            }
        )

    async def send_notification(
        self,
        tokens: Optional[List[str]] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        priority: str = "normal",
        click_action: Optional[str] = None,
        icon: Optional[str] = None,
        image: Optional[str] = None,
        request: Optional[SendNotificationRequest] = None,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> Dict[str, Any] | NotificationResponse:
        if request is not None:
            return await self.send__notification(request, background_tasks)
        tokens = tokens or []
        title = title or ""
        body = body or ""
        if not self.firebase_initialized:
            return {"success": False, "sent_count": 0, "failed_count": len(tokens)}
        if not tokens:
            return {"success": False, "sent_count": 0, "failed_count": 0}

        messages = [
            messaging.Message(
                token=t,
                notification=messaging.Notification(title=title, body=body, image=image),
                data={k: str(v) for k, v in (data or {}).items()},
            )
            for t in tokens
        ]
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, messaging.send_each, messages)
        return {
            "success": response.success_count > 0,
            "sent_count": response.success_count,
            "failed_count": response.failure_count,
        }

    async def send__notification(
        self, request: SendNotificationRequest, background_tasks: Optional[BackgroundTasks]
    ) -> NotificationResponse:
        all_tokens = list(set(request.device_tokens or []))
        result = await self.send_notification(
            tokens=all_tokens,
            title=request.title,
            body=request.body,
            data=request.data,
            priority=request.priority.value,
            click_action=request.click_action,
            icon=request.icon,
            image=request.image,
        )
        if background_tasks and request.user_ids:
            for user_id in request.user_ids:
                background_tasks.add_task(
                    self.log_notification,
                    user_id=user_id,
                    title=request.title,
                    body=request.body,
                    data=request.data,
                    status="sent" if result["success"] else "failed",
                )
        return NotificationResponse(
            success=result["success"],
            message="Notification sent successfully" if result["success"] else "Failed to send notification",
            sent_count=result["sent_count"],
            failed_count=result["failed_count"],
        )

    async def send_data_message(
        self, request: SendNotificationRequest, background_tasks: Optional[BackgroundTasks]
    ) -> NotificationResponse:
        return await self.send__notification(request, background_tasks)

    async def prepare_notification(
        self,
        fcm_tokens: List[str],
        title: str,
        body: str,
        data: Optional[dict] = None,
        is_push_notification: bool = True,
        caller_user_id: Optional[str] = None,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> NotificationResponse:
        req = SendNotificationRequest(
            user_ids=[caller_user_id] if caller_user_id else None,
            device_tokens=fcm_tokens,
            title=title,
            body=body,
            data=data,
            priority=NotificationPriority.HIGH,
        )
        if is_push_notification:
            return await self.send__notification(req, background_tasks)
        return await self.send_data_message(req, background_tasks)

    async def send_to_topic(
        self, topic: str, title: str, body: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if not self.firebase_initialized:
            return {"success": False, "message": "Firebase is not initialized"}
        message = messaging.Message(
            topic=topic,
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
        )
        loop = asyncio.get_running_loop()
        message_id = await loop.run_in_executor(None, messaging.send, message)
        return {"success": True, "message_id": message_id}

    async def subscribe_to_topic(self, tokens: List[str], topic: str) -> Dict[str, Any]:
        if not self.firebase_initialized:
            return {"success": False, "message": "Firebase is not initialized"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, messaging.subscribe_to_topic, tokens, topic)
        return {
            "success": True,
            "success_count": len(tokens) - response.failure_count,
            "failure_count": response.failure_count,
            "topic": topic,
        }

    async def unsubscribe_from_topic(self, tokens: List[str], topic: str) -> Dict[str, Any]:
        if not self.firebase_initialized:
            return {"success": False, "message": "Firebase is not initialized"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, messaging.unsubscribe_from_topic, tokens, topic)
        return {
            "success": True,
            "success_count": len(tokens) - response.failure_count,
            "failure_count": response.failure_count,
            "topic": topic,
        }

    async def get_notification_stats(
        self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {}
        if start_date or end_date:
            query["sent_at"] = {}
            if start_date:
                query["sent_at"]["$gte"] = start_date
            if end_date:
                query["sent_at"]["$lte"] = end_date
        total_notifications = await notification_logs_collection.count_documents(query)
        active_tokens = await notification_tokens_collection.count_documents({"is_active": True})
        return {
            "statistics": {"total": total_notifications},
            "active_tokens": active_tokens,
            "total_notifications": total_notifications,
        }

    async def get_user_device_info(
        self, user_id: str, user_type: str, organization_id: Optional[str] = None
    ) -> List[dict]:
        try:
            query: Dict[str, Any] = {"user_id": user_id, "is_active": True, "user_type": user_type}
            if organization_id:
                query.update(org_filter(organization_id))
            cursor = notification_tokens_collection.find(query)
            device_info_list: List[dict] = []
            async for token_doc in cursor:
                device_info = token_doc.get("device_info") or {}
                device_token = token_doc.get("device_token", "")
                if not device_info:
                    device_info = {
                        "device_type": token_doc.get("device_type", "unknown"),
                        "device_token": device_token,
                    }
                if "device_token" not in device_info:
                    device_info = {**device_info, "device_token": device_token}
                device_info_list.append(
                    {
                        "device_token": device_token,
                        "device_info": device_info,
                        "formatted_string": format_device_info_string(device_info),
                    }
                )
            return device_info_list
        except Exception as exc:
            logger.error("Failed to get user device info: %s", exc)
            return []

    async def exist_user(self, user_id: str) -> bool:
        try:
            n = await notification_tokens_collection.count_documents(
                {"user_id": user_id, "is_active": True}
            )
            return n > 0
        except Exception as exc:
            logger.error("Failed to check user notification tokens: %s", exc)
            return False

    async def send_notification_to_callee(self, request: CallerTokenRequest) -> dict:
        url = f"{SERVER_API_HOST}/notifications/send-notification-to-callee"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {STATIC_API_KEY}"}
        try:
            async with httpx.AsyncClient() as async_client:
                response = await async_client.post(url, headers=headers, json=request.dict())
                if response.status_code == 200:
                    return response.json()
                return {
                    "success": False,
                    "error": f"Notification service returned status {response.status_code}",
                    "status_code": response.status_code,
                }
        except Exception as exc:
            logger.error(f"send_notification_to_callee failed: {exc}")
            return {"success": False, "error": str(exc)}
