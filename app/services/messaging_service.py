"""
Messaging Service - Handles message persistence and conversation management
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import motor.motor_asyncio
from bson import ObjectId
from bson.errors import InvalidId
from livekit import api

from app.config import DB_NAME, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET, LIVEKIT_SDK_URL, MONGO_URI
from app.models.models import MessagingTokenRequest, TokenResponse
from app.services import notification__service, user_service
from app.utils.mongodb_org import org_filter as _org_filter, org_value as _org_value

logger = logging.getLogger(__name__)


class MessagingService:
    def __init__(self):
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        self.db = self.mongo_client[DB_NAME]
        self.messages_collection = self.db["messages"]
        self.conversations_collection = self.db["conversations"]

    async def ensure_indexes(self) -> None:
        try:
            await self.conversations_collection.create_index([("user_id", 1), ("last_message_at", -1)])
            await self.conversations_collection.create_index([("user_id", 1), ("conversation_id", 1)])
            await self.messages_collection.create_index([("conversation_id", 1), ("created_at", -1)])
        except Exception as e:
            logger.warning({"event": "messaging_indexes_failed", "error": str(e)})

    def _generate_conversation_id(self, user_id_1: str, user_id_2: str, room_name: str) -> str:
        sorted_ids = sorted([user_id_1, user_id_2])
        return f"conv_{sorted_ids[0]}_{sorted_ids[1]}_{room_name}"

    async def save_message(
        self,
        sender_user_id: str,
        receiver_user_id: str,
        room_name: str,
        message: str,
        message_type: str = "text",
        metadata: Optional[Dict] = None,
        organization_id: Optional[str] = None,
    ) -> Dict:
        if not organization_id:
            raise ValueError("organization_id is required for save_message")
        conversation_id = self._generate_conversation_id(sender_user_id, receiver_user_id, room_name)
        now = datetime.now(timezone.utc)
        message_doc = {
            "conversation_id": conversation_id,
            "sender_user_id": sender_user_id,
            "receiver_user_id": receiver_user_id,
            "room_name": room_name,
            "message": message,
            "message_type": message_type,
            "metadata": metadata or {},
            "created_at": now.isoformat(),
            "read_at": None,
            "organization_id": _org_value(organization_id) or organization_id,
        }
        result = await self.messages_collection.insert_one(message_doc)
        await asyncio.gather(
            self._update_conversation(sender_user_id, conversation_id, room_name, receiver_user_id, message, now, False, organization_id),
            self._update_conversation(receiver_user_id, conversation_id, room_name, sender_user_id, message, now, True, organization_id),
        )
        return {"id": str(result.inserted_id), "conversation_id": conversation_id, **message_doc}

    async def _update_conversation(
        self,
        user_id: str,
        conversation_id: str,
        room_name: str,
        other_user_id: str,
        last_message: str,
        last_message_at: datetime,
        increment_unread: bool = False,
        organization_id: Optional[str] = None,
    ):
        if not organization_id:
            raise ValueError("organization_id is required for _update_conversation")
        now = datetime.now(timezone.utc).isoformat()
        base = {"user_id": user_id, "conversation_id": conversation_id, **_org_filter(organization_id)}
        existing = await self.conversations_collection.find_one(base)
        if existing:
            update: Dict[str, Any] = {"$set": {"last_message": last_message, "last_message_at": last_message_at.isoformat(), "updated_at": now}}
            if increment_unread:
                update["$inc"] = {"unread_count": 1}
            await self.conversations_collection.update_one(base, update)
        else:
            await self.conversations_collection.insert_one(
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "room_name": room_name,
                    "participant_user_id": other_user_id,
                    "last_message": last_message,
                    "last_message_at": last_message_at.isoformat(),
                    "unread_count": 1 if increment_unread else 0,
                    "created_at": now,
                    "updated_at": now,
                    "organization_id": _org_value(organization_id) or organization_id,
                }
            )

    async def get_conversations(self, user_id: str, skip: int = 0, limit: int = 50, organization_id: Optional[str] = None) -> List[Dict]:
        if not organization_id:
            raise ValueError("organization_id is required for get_conversations")
        cursor = self.conversations_collection.find({"user_id": user_id, **_org_filter(organization_id)}).sort("last_message_at", -1).skip(skip).limit(limit)
        items: List[Dict] = []
        async for conv in cursor:
            conv["id"] = str(conv["_id"])
            del conv["_id"]
            items.append(conv)
        return items

    async def get_conversation(self, conversation_id: str, user_id: str) -> Optional[Dict]:
        conv = await self.conversations_collection.find_one({"user_id": user_id, "conversation_id": conversation_id})
        if conv:
            conv["id"] = str(conv["_id"])
            del conv["_id"]
        return conv

    async def get_messages(self, conversation_id: str, user_id: str, skip: int = 0, limit: int = 100, organization_id: Optional[str] = None) -> List[Dict]:
        if not organization_id:
            raise ValueError("organization_id is required for get_messages")
        query = {
            "conversation_id": conversation_id,
            **_org_filter(organization_id),
            "$or": [{"sender_user_id": user_id}, {"receiver_user_id": user_id}],
        }
        cursor = self.messages_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
        items: List[Dict] = []
        async for msg in cursor:
            msg["id"] = str(msg["_id"])
            del msg["_id"]
            items.append(msg)
        items.reverse()
        return items

    async def mark_messages_read(
        self,
        conversation_id: str,
        user_id: str,
        message_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> int:
        if not organization_id:
            raise ValueError("organization_id is required for mark_messages_read")
        query: Dict[str, Any] = {"conversation_id": conversation_id, "receiver_user_id": user_id, **_org_filter(organization_id)}
        if message_ids:
            query["_id"] = {"$in": [ObjectId(mid) for mid in message_ids]}
        else:
            query["read_at"] = None
        result = await self.messages_collection.update_many(query, {"$set": {"read_at": datetime.now(timezone.utc).isoformat()}})
        if result.modified_count > 0:
            await self.conversations_collection.update_one(
                {"user_id": user_id, "conversation_id": conversation_id, **_org_filter(organization_id)},
                {"$inc": {"unread_count": -result.modified_count}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
            )
        return result.modified_count

    async def get_unread_count(self, user_id: str, conversation_id: Optional[str] = None, organization_id: Optional[str] = None) -> int:
        if not organization_id:
            raise ValueError("organization_id is required for get_unread_count")
        if conversation_id:
            conv = await self.conversations_collection.find_one({"user_id": user_id, "conversation_id": conversation_id, **_org_filter(organization_id)})
            return conv.get("unread_count", 0) if conv else 0
        pipeline = [{"$match": {"user_id": user_id, **_org_filter(organization_id)}}, {"$group": {"_id": None, "total": {"$sum": "$unread_count"}}}]
        result = await self.conversations_collection.aggregate(pipeline).to_list(1)
        return result[0]["total"] if result else 0

    async def get_message_count(self, user_id: str, conversation_id: Optional[str] = None, organization_id: Optional[str] = None) -> int:
        if not organization_id:
            raise ValueError("organization_id is required for get_message_count")
        query: Dict[str, Any] = {"$or": [{"sender_user_id": user_id}, {"receiver_user_id": user_id}], **_org_filter(organization_id)}
        if conversation_id:
            query["conversation_id"] = conversation_id
        return await self.messages_collection.count_documents(query)

    async def delete_message(self, message_id: str, user_id: str, organization_id: Optional[str] = None) -> bool:
        if not organization_id:
            raise ValueError("organization_id is required for delete_message")
        try:
            obj_id = ObjectId(message_id)
        except (InvalidId, ValueError):
            return False
        msg = await self.messages_collection.find_one({"_id": obj_id, **_org_filter(organization_id)})
        if not msg:
            return False
        if msg.get("sender_user_id") != user_id and msg.get("receiver_user_id") != user_id:
            return False
        result = await self.messages_collection.delete_one({"_id": obj_id, **_org_filter(organization_id)})
        return result.deleted_count > 0

    async def delete_conversation(self, conversation_id: str, user_id: str, delete_messages: bool = False, organization_id: Optional[str] = None) -> bool:
        if not organization_id:
            raise ValueError("organization_id is required for delete_conversation")
        result = await self.conversations_collection.delete_one({"user_id": user_id, "conversation_id": conversation_id, **_org_filter(organization_id)})
        if result.deleted_count <= 0:
            return False
        if delete_messages:
            await self.messages_collection.delete_many({"conversation_id": conversation_id, **_org_filter(organization_id)})
            await self.conversations_collection.delete_many({"conversation_id": conversation_id, **_org_filter(organization_id)})
        return True

    def _format_user_name(self, user: dict) -> str:
        first = (user.get("first_name") or "").strip()
        last = (user.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
        return name or user.get("email", "User")

    def validate_messaging_users(self, sender_user_id: str, receiver_user_id: str) -> Tuple[str, str, str, str]:
        sender_user = user_service.get_user_by_id(sender_user_id)
        if not sender_user:
            raise KeyError(f"Sender user not found: {sender_user_id}")
        receiver_user = user_service.get_user_by_id(receiver_user_id)
        if not receiver_user:
            raise KeyError(f"Receiver user not found: {receiver_user_id}")
        return (
            self._format_user_name(sender_user),
            sender_user.get("role", "customer"),
            self._format_user_name(receiver_user),
            receiver_user.get("role", "customer"),
        )

    async def generate_messaging_token(self, sender_user_id: str, sender_name: str, sender_role: str, room_name: str) -> TokenResponse:
        access_token = api.AccessToken(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)
        video_grant = api.VideoGrants(room=room_name, room_join=True, can_publish=True, can_subscribe=True, can_publish_data=True)
        access_token.with_identity(sender_user_id)
        access_token.with_name(sender_name or sender_user_id)
        try:
            access_token.with_kind(sender_role)  # type: ignore[arg-type]
        except Exception:
            pass
        access_token.with_grants(video_grant)
        return TokenResponse(accessToken=access_token.to_jwt(), wsUrl=LIVEKIT_SDK_URL or "")

    async def prepare_receiver_notification_data(
        self,
        sender_user_id: str,
        sender_name: str,
        sender_role: str,
        receiver_user_id: str,
        receiver_name: str,
        receiver_role: str,
        room_name: str,
        message: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Tuple[Optional[List[str]], Optional[dict], Optional[str], Optional[str]]:
        if conversation_id is None:
            conversation_id = self._generate_conversation_id(sender_user_id, receiver_user_id, room_name)
        fcm_tokens = await notification__service.get_user_tokens(receiver_user_id, receiver_role)
        if not fcm_tokens:
            return None, None, None, None
        title = f"New Message from {sender_name}"
        body = f"{sender_name}: {message[:100] + '...' if message and len(message) > 100 else message}" if message else f"You have a new message from {sender_name}"
        data = {
            "title": title,
            "body": body,
            "type": "incoming_message",
            "action": "receive_message",
            "room_name": room_name,
            "conversation_id": conversation_id,
            "participant_identity": receiver_user_id,
            "participant_identity_name": receiver_name,
            "participant_identity_type": receiver_role,
            "sender_user_id": sender_user_id,
            "receiver_user_id": receiver_user_id,
            "sender_name": sender_name,
        }
        if message:
            data["message"] = message
        return fcm_tokens, data, title, body

    async def send_receiver_notification(
        self,
        sender_user_id: str,
        sender_name: str,
        sender_role: str,
        receiver_user_id: str,
        receiver_name: str,
        receiver_role: str,
        room_name: str,
        is_push_notification: bool,
        background_tasks,
        message: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ):
        fcm_tokens, data, title, body = await self.prepare_receiver_notification_data(
            sender_user_id, sender_name, sender_role, receiver_user_id, receiver_name, receiver_role, room_name, message, conversation_id
        )
        if fcm_tokens and data and title and body:
            await notification__service.prepare_notification(
                fcm_tokens=fcm_tokens,
                title=title,
                body=body,
                data=data,
                is_push_notification=is_push_notification,
                caller_user_id=sender_user_id,
                background_tasks=background_tasks,
            )

    async def prepare_push_notification(self, request: MessagingTokenRequest, background_tasks) -> TokenResponse:
        sender_name, sender_role, receiver_name, receiver_role = await asyncio.to_thread(
            self.validate_messaging_users, request.sender_user_id, request.receiver_user_id
        )
        sender_token_response = await self.generate_messaging_token(
            sender_user_id=request.sender_user_id,
            sender_name=sender_name,
            sender_role=sender_role,
            room_name=request.room_name,
        )
        if request.is_push_notification:
            asyncio.create_task(
                self.send_receiver_notification(
                    sender_user_id=request.sender_user_id,
                    sender_name=sender_name,
                    sender_role=sender_role,
                    receiver_user_id=request.receiver_user_id,
                    receiver_name=receiver_name,
                    receiver_role=receiver_role,
                    room_name=request.room_name,
                    is_push_notification=request.is_push_notification,
                    background_tasks=background_tasks,
                )
            )
        return sender_token_response
