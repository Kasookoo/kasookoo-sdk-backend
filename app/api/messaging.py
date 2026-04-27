"""
Messaging API endpoints for persistent message storage and conversation management
"""
import asyncio
import logging
import time
from typing import Optional, Tuple

from bson.errors import InvalidId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.api.auth import get_organization_id
from app.security.interceptor import authenticate_sdk_user, intercept_sdk_access
from app.models.models import (
    ConversationInfo,
    ConversationListResponse,
    MarkMessagesReadRequest,
    MessageListResponse,
    MessageResponse,
    MessagingTokenRequest,
    SendMessageRequest,
    TokenResponse,
)
from app.services.user_service import user_service
from app.services.messaging_service import MessagingService
from app.utils.performance_monitor import monitor

logger = logging.getLogger(__name__)
router = APIRouter()
messaging_service = MessagingService()


def _format_user_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or user.get("email", "User")


def _split_name(full_name: Optional[str]) -> Tuple[str, str]:
    normalized = (full_name or "").strip()
    if not normalized:
        return "User", ""
    parts = normalized.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


async def _upsert_messaging_participant(
    participant,
    label: str,
    organization_id: Optional[str],
) -> Tuple[str, str, str]:
    participant_name = ((participant.name if participant else None) or "").strip() or "User"
    participant_email = ((participant.email if participant else None) or "").strip().lower()
    participant_phone = ((participant.phone_number if participant else None) or "").strip()
    participant_role = ((participant.type if participant else None) or "customer").strip().lower() or "customer"
    if participant_role == "driver":
        participant_role = "agent"

    if not participant_email:
        raise ValueError(f"{label}.email is required")

    # Resolve by organization + email
    if organization_id and participant_email:
        existing_by_email = user_service.get_user_by_email(participant_email, organization_id=organization_id)
        if existing_by_email:
            existing_user_id = str(existing_by_email["_id"])
            first_name, last_name = _split_name(participant_name)
            updated_user = user_service.update_user(
                existing_user_id,
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": participant_email,
                    "phone_number": participant_phone,
                    "role": participant_role,
                },
            )
            if updated_user:
                return existing_user_id, _format_user_name(updated_user), (updated_user.get("role") or participant_role)

    # Create new user when not found
    first_name, last_name = _split_name(participant_name)
    created_user = await user_service.create_user(
        email=participant_email,
        phone_number=participant_phone,
        first_name=first_name,
        last_name=last_name,
        role=participant_role,
        password=f"msg-{label}-{int(time.time() * 1000)}",
        organization_id=organization_id,
    )
    created_user_id = str(created_user["_id"])
    return created_user_id, _format_user_name(created_user), (created_user.get("role") or participant_role)


@router.on_event("startup")
async def messaging_startup() -> None:
    await messaging_service.ensure_indexes()


@router.post("/messaging/get-token")
@monitor(name="api.messaging.get_messaging_tokens")
async def get_messaging_token(
    request: MessagingTokenRequest,
    background_tasks: BackgroundTasks,
    _principal: dict = Depends(intercept_sdk_access(["messaging:token:create"])),
) -> TokenResponse:
    try:
        organization_id = (_principal or {}).get("organization_id") or (_principal or {}).get("org_id")
        sender_id, _, _ = await _upsert_messaging_participant(
            participant=request.sender,
            label="sender",
            organization_id=organization_id,
        )
        receiver_id, _, _ = await _upsert_messaging_participant(
            participant=request.receiver,
            label="receiver",
            organization_id=organization_id,
        )

        resolved_request = request.model_copy(
            update={
                "sender_user_id": sender_id,
                "receiver_user_id": receiver_id,
                "participant_identity": ((request.sender.email if request.sender else "") or "").strip().lower(),
                "participant_identity_name": (request.sender.name if request.sender else None),
                "participant_identity_type": (request.sender.type if request.sender else None) or "customer",
            }
        )
        return await messaging_service.prepare_push_notification(
            request=resolved_request,
            background_tasks=background_tasks,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/messaging/send")
@monitor(name="api.messaging.send_message")
async def send_message(
    request: SendMessageRequest,
    background_tasks: BackgroundTasks,
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(intercept_sdk_access(["messaging:send"])),
) -> MessageResponse:
    try:
        sender, receiver = await asyncio.gather(
            asyncio.to_thread(user_service.get_user_by_id, request.sender_user_id, organization_id),
            asyncio.to_thread(user_service.get_user_by_id, request.receiver_user_id, organization_id),
        )
        if not sender:
            raise HTTPException(status_code=404, detail="Sender user not found")
        if not receiver:
            raise HTTPException(status_code=404, detail="Receiver user not found")

        message_doc = await messaging_service.save_message(
            sender_user_id=request.sender_user_id,
            receiver_user_id=request.receiver_user_id,
            room_name=request.room_name,
            message=request.message,
            message_type=request.message_type or "text",
            metadata=request.metadata,
            organization_id=organization_id,
        )
        conversation_id = message_doc["conversation_id"]
        if request.is_push_notification:
            def _format_name(u: dict) -> str:
                name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                return name or u.get("email", "User")
            background_tasks.add_task(
                messaging_service.send_receiver_notification,
                sender_user_id=request.sender_user_id,
                sender_name=_format_name(sender),
                sender_role=sender.get("role", "customer"),
                receiver_user_id=request.receiver_user_id,
                receiver_name=_format_name(receiver),
                receiver_role=receiver.get("role", "customer"),
                room_name=request.room_name,
                is_push_notification=request.is_push_notification,
                background_tasks=background_tasks,
                message=request.message,
                conversation_id=conversation_id,
            )
        return MessageResponse(**message_doc)
    except InvalidId as e:
        raise HTTPException(status_code=400, detail=f"Invalid user ID: {str(e)}")


@router.get("/messaging/conversations")
@monitor(name="api.messaging.get_conversations")
async def get_conversations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> ConversationListResponse:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    conversations = await messaging_service.get_conversations(user_id=user_id, skip=skip, limit=limit, organization_id=organization_id)
    enriched = []
    for conv in conversations:
        participant = user_service.get_user_by_id(conv["participant_user_id"], organization_id=organization_id)
        if participant:
            conv["participant_name"] = f"{participant.get('first_name', '')} {participant.get('last_name', '')}".strip()
            conv["participant_email"] = participant.get("email")
        if isinstance(conv.get("unread_count"), int) and conv["unread_count"] < 0:
            conv["unread_count"] = 0
        enriched.append(ConversationInfo(**conv))
    return ConversationListResponse(conversations=enriched, total=len(enriched))


@router.get("/messaging/conversations/{conversation_id}/messages")
@monitor(name="api.messaging.get_messages")
async def get_messages(
    conversation_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    include_messaging_tokens: bool = Query(False),
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> MessageListResponse:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    messages = await messaging_service.get_messages(
        conversation_id=conversation_id, user_id=user_id, skip=skip, limit=limit, organization_id=organization_id
    )
    message_responses = [MessageResponse(**msg) for msg in messages]
    messaging_tokens = None
    if include_messaging_tokens:
        conv = await messaging_service.get_conversation(conversation_id=conversation_id, user_id=user_id)
        if conv:
            room_name = conv.get("room_name")
            participant_user_id = conv.get("participant_user_id")
            if room_name and participant_user_id:
                token_request = MessagingTokenRequest(
                    room_name=room_name,
                    participant_identity=user_id,
                    sender_user_id=user_id,
                    receiver_user_id=participant_user_id,
                    is_push_notification=False,
                )
                try:
                    messaging_tokens = await messaging_service.prepare_push_notification(
                        request=token_request, background_tasks=BackgroundTasks()
                    )
                except (ValueError, KeyError):
                    pass
    return MessageListResponse(
        messages=message_responses,
        total=len(message_responses),
        conversation_id=conversation_id,
        messaging_tokens=messaging_tokens,
    )


@router.post("/messaging/mark-read")
@monitor(name="api.messaging.mark_read")
async def mark_messages_read(
    request: MarkMessagesReadRequest,
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> dict:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    count = await messaging_service.mark_messages_read(
        conversation_id=request.conversation_id,
        user_id=user_id,
        message_ids=request.message_ids,
        organization_id=organization_id,
    )
    return {"success": True, "conversation_id": request.conversation_id, "messages_marked_read": count}


@router.delete("/messaging/messages/{message_id}")
@monitor(name="api.messaging.delete_message")
async def delete_message(
    message_id: str,
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> dict:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    deleted = await messaging_service.delete_message(message_id=message_id, user_id=user_id, organization_id=organization_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found or you do not have permission to delete it")
    return {"success": True, "message_id": message_id}


@router.delete("/messaging/conversations/{conversation_id}")
@monitor(name="api.messaging.delete_conversation")
async def delete_conversation(
    conversation_id: str,
    delete_messages: bool = Query(False),
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> dict:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    deleted = await messaging_service.delete_conversation(
        conversation_id=conversation_id, user_id=user_id, delete_messages=delete_messages, organization_id=organization_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "conversation_id": conversation_id, "delete_messages": delete_messages}


@router.get("/messaging/unread-count")
@monitor(name="api.messaging.get_unread_count")
async def get_unread_count(
    conversation_id: Optional[str] = Query(None),
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> dict:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    count = await messaging_service.get_unread_count(user_id=user_id, conversation_id=conversation_id, organization_id=organization_id)
    return {"user_id": user_id, "conversation_id": conversation_id, "unread_count": count}


@router.get("/messaging/message-count")
@monitor(name="api.messaging.get_message_count")
async def get_message_count(
    conversation_id: Optional[str] = Query(None),
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(authenticate_sdk_user),
) -> dict:
    email = str(_principal.get("email") or _principal.get("sub") or "")
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    count = await messaging_service.get_message_count(user_id=user_id, conversation_id=conversation_id, organization_id=organization_id)
    return {"user_id": user_id, "conversation_id": conversation_id, "message_count": count}
