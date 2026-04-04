"""
Messaging API endpoints for persistent message storage and conversation management
"""
import asyncio
import logging
from typing import Optional

from bson.errors import InvalidId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.api.auth import get_organization_id, normal_authenticate_token, require_scopes, sdk_token_scheme
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


@router.on_event("startup")
async def messaging_startup() -> None:
    await messaging_service.ensure_indexes()


@router.post("/messaging/get-token")
@monitor(name="api.messaging.get_messaging_tokens")
async def get_messaging_token(
    request: MessagingTokenRequest,
    background_tasks: BackgroundTasks,
    token: str = Depends(sdk_token_scheme),
    _principal: dict = Depends(require_scopes(["messaging:token:create"])),
) -> TokenResponse:
    await normal_authenticate_token(token)
    try:
        return await messaging_service.prepare_push_notification(request=request, background_tasks=background_tasks)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/messaging/send")
@monitor(name="api.messaging.send_message")
async def send_message(
    request: SendMessageRequest,
    background_tasks: BackgroundTasks,
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
    _principal: dict = Depends(require_scopes(["messaging:send"])),
) -> MessageResponse:
    await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> ConversationListResponse:
    _, email, _ = await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> MessageListResponse:
    _, email, _ = await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> dict:
    _, email, _ = await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> dict:
    _, email, _ = await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> dict:
    _, email, _ = await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> dict:
    _, email, _ = await normal_authenticate_token(token)
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
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> dict:
    _, email, _ = await normal_authenticate_token(token)
    user = user_service.get_user_by_email(email, organization_id=organization_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = str(user.get("_id"))
    count = await messaging_service.get_message_count(user_id=user_id, conversation_id=conversation_id, organization_id=organization_id)
    return {"user_id": user_id, "conversation_id": conversation_id, "message_count": count}
