import asyncio
import os
import time
from typing import List, Optional, Tuple
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId
from fastapi.params import Depends
from fsspec.registry import s3_msg
from pydantic.fields import Deprecated

from app.config import LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET, ANONYMOUS_GUEST_CALL_ADMIN_EMAIL
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Body, Request, Header
import jwt
from pydantic import BaseModel
from livekit import api
from livekit.api import DeleteRoomRequest, webhook, TokenVerifier
from google.protobuf.json_format import MessageToDict
import logging
from app.models.models import CallRequest, CalledTokenRequest, CallerTokenRequest, AnonymousCallerTokenRequest, RecordingRequest, CallStatusResponse, RejectCallTokenRequest, TokenRequest, TokenResponse, CallerTokenResponse, ParticipantType, MessagingTokenRequest
from app.services import user_service, notification__service, associated_number_service, organization_service
from app.services.notification_service import DataMessageRequest, NotificationPriority, SendNotificationRequest
from app.config import LIVEKIT_URL
from app.services.call_manager import WebRTCCallManager
from app.services.recording_manager import LiveKitS3RecordingManager
from app.services.token_service import TokenService
from fastapi.responses import FileResponse, JSONResponse

from app.api.auth import authenticate_static_token
from app.security.interceptor import authenticate_sdk_user, intercept_sdk_access

from app.utils.websocket_manager import WebSocketManager
from app.utils.performance_monitor import monitor

#from livekit import RoomServiceClient

if not all([LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET]):
    raise EnvironmentError("LiveKit server environment variables are not set!")

# --- Initialize Services ---
router = APIRouter()
#room_service = RoomServiceClient(LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# TTL cache for anonymous guest call admin user resolution (from ANONYMOUS_GUEST_CALL_ADMIN_EMAIL config)
_anonymous_admin_cache: Optional[Tuple[str, float]] = None  # (admin_user_id, expiry_ts)
_ANONYMOUS_ADMIN_CACHE_TTL = 300  # 5 minutes


def _get_cached_anonymous_admin_user_id() -> Optional[str]:
    """Return cached admin user ID if valid, else None."""
    global _anonymous_admin_cache
    if _anonymous_admin_cache:
        admin_id, expiry_ts = _anonymous_admin_cache
        if time.monotonic() < expiry_ts:
            return admin_id
        _anonymous_admin_cache = None
    return None


def _set_cached_anonymous_admin_user_id(admin_id: str) -> None:
    """Cache admin user ID with TTL."""
    global _anonymous_admin_cache
    _anonymous_admin_cache = (admin_id, time.monotonic() + _ANONYMOUS_ADMIN_CACHE_TTL)


# LiveKitAPI client creation is deferred to request handlers/services.
# Creating it at import time fails on Python 3.13 because aiohttp requires a running loop.

# Initialize services
recording_manager = LiveKitS3RecordingManager()
token_service = TokenService()
call_manager = WebRTCCallManager(recording_manager, token_service)
websocket_manager = WebSocketManager()


def _format_user_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or user.get("email", "User")


def _build_room_token(room_name: str, participant_identity: str, participant_name: str) -> str:
    access_token = api.AccessToken(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)
    video_grant = api.VideoGrants(
        room=room_name,
        room_join=True,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    access_token.with_identity(participant_identity)
    access_token.with_name(participant_name or participant_identity)
    access_token.with_grants(video_grant)
    return access_token.to_jwt()


async def _handle_sip_inbound_mapping_from_webhook(webhook_data: dict) -> None:
    """
    Handle SIP inbound participant_joined event:
    - resolve organization by sip.trunkID
    - resolve mapped user by associated number (sip.trunkPhoneNumber)
    - push receive_call notification with room token
    """
    try:
        if (webhook_data or {}).get("event") != "participant_joined":
            return

        room_name = ((webhook_data or {}).get("room") or {}).get("name")
        participant_data = (webhook_data or {}).get("participant") or {}
        if not room_name or not isinstance(participant_data, dict):
            return

        participant_kind = str(participant_data.get("kind") or "").lower()
        participant_identity = str(participant_data.get("identity") or "")
        is_sip = participant_kind == "sip" or participant_identity.startswith("sip-")
        if not is_sip:
            return

        attributes = participant_data.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}
        called_number = attributes.get("sip.trunkPhoneNumber") or ""
        caller_number = attributes.get("sip.phoneNumber") or ""
        sip_trunk_id = attributes.get("sip.trunkID") or ""

        organization_id: Optional[str] = None
        if sip_trunk_id:
            org = organization_service.get_organization_by_inbound_trunk_id(str(sip_trunk_id))
            if org and org.get("id"):
                organization_id = str(org.get("id"))

        mapping = associated_number_service.get_active_mapping_by_number(
            phone_number=str(called_number), organization_id=organization_id
        )
        if not mapping or not mapping.get("user_id"):
            logger.info(
                {
                    "event": "sip_inbound_no_associated_number_mapping",
                    "room_name": room_name,
                    "organization_id": organization_id,
                    "called_number": called_number,
                    "sip_trunk_id": sip_trunk_id,
                }
            )
            return

        called_user_id = str(mapping["user_id"])
        called_user = user_service.get_user_by_id(called_user_id, organization_id=organization_id)
        if not called_user:
            logger.warning(
                {
                    "event": "sip_inbound_mapped_user_not_found",
                    "room_name": room_name,
                    "organization_id": organization_id,
                    "mapped_user_id": called_user_id,
                }
            )
            return

        called_user_name = _format_user_name(called_user)
        called_user_role = called_user.get("role", "customer")
        room_token = _build_room_token(room_name, called_user_id, called_user_name)
        title = f"Incoming SIP Call from {caller_number or 'Unknown'}"
        body = f"Please answer the call to {called_number or 'your number'}"
        data = {
            "title": title,
            "body": body,
            "type": "sip_incoming_call",
            "action": "receive_call",
            "room_name": room_name,
            "participant_identity": called_user_id,
            "participant_identity_name": called_user_name,
            "participant_identity_type": "callee",
            "called_user_id": called_user_id,
            "caller_user_id": caller_number or "sip-caller",
            "caller_phone_number": caller_number or "",
            "called_phone_number": called_number or "",
            "accessToken": room_token,
            "wsUrl": LIVEKIT_SDK_URL,
        }
        fcm_tokens = await notification__service.get_user_tokens(
            called_user_id, called_user_role, organization_id=organization_id
        )
        if not fcm_tokens:
            logger.warning(
                {
                    "event": "sip_inbound_no_fcm_tokens",
                    "room_name": room_name,
                    "organization_id": organization_id,
                    "called_user_id": called_user_id,
                }
            )
            return

        await notification__service.prepare_notification(
            fcm_tokens=fcm_tokens,
            title=title,
            body=body,
            data=data,
            is_push_notification=True,
            caller_user_id=caller_number or "sip-caller",
            background_tasks=None,
        )
        logger.info(
            {
                "event": "sip_inbound_notification_sent",
                "room_name": room_name,
                "organization_id": organization_id,
                "called_user_id": called_user_id,
                "called_number": called_number,
                "caller_number": caller_number,
                "token_count": len(fcm_tokens),
            }
        )
    except Exception as e:
        logger.warning({"event": "sip_inbound_mapping_failed", "error": str(e)})


async def _delete_livekit_room(room_name: str) -> bool:
    """
    Delete a LiveKit room, disconnecting all participants.
    Returns True if successful, False otherwise.
    """
    # Validate room_name before attempting deletion
    if not room_name or not room_name.strip():
        logger.warning({"event": "skip_delete_empty_room", "room_name": room_name})
        return False
    
    try:
        logger.info({"event": "deleting_livekit_room", "room_name": room_name})
        await room_service.delete_room(DeleteRoomRequest(room=room_name))
        logger.info({"event": "livekit_room_deleted", "room_name": room_name})
        return True
    except Exception as e:
        error_str = str(e)
        # Handle "room does not exist" gracefully - it's not an error if room is already gone
        if "not_found" in error_str.lower() or "does not exist" in error_str.lower() or "404" in error_str:
            logger.info({
                "event": "room_already_deleted", 
                "room_name": room_name, 
                "message": "Room does not exist (may have been already deleted)"
            })
            return True  # Consider it successful if room doesn't exist
        else:
            # Log actual errors
            logger.error({
                "event": "failed_to_delete_livekit_room", 
                "room_name": room_name, 
                "error": error_str
            })
        # Don't raise exception - room deletion failure shouldn't break the call end flow
        return False

# Dependency injection
def get_call_manager() -> WebRTCCallManager:
    return call_manager


# --- API Endpoint to Generate a Token ---
@router.post("/webrtc/get-token")
@monitor(name="api.sdk.get_token")
async def get_token_endpoint(request: TokenRequest):
    """
    Generates a LiveKit access token with specific permissions.
    For a 'monitor-client', we disable publishing rights.
    """
    logger.info({"event": "get_token_endpoint", "LIVEKIT_SDK_URL": LIVEKIT_SDK_URL})
    # Create an AccessToken object with the client's identity
    access_token = api.AccessToken(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)
    
    # Define the grant (permissions) for the token
    # A monitor should not publish its own audio/video
    video_grant = api.VideoGrants(
        room=request.room_name,
        room_join=True,
        can_publish=True, # <-- This makes the client a silent observer
        can_subscribe=True,
        can_publish_data=True
    )
    
    logger.info({
        "event": "token_issued",
        "participant_identity": request.participant_identity,
        "room_name": request.room_name
    })

    # Set token properties and grants
    access_token.with_identity(request.participant_identity)
    access_token.with_name(request.participant_identity_name or request.participant_identity)
    access_token.with_kind(request.participant_identity_type)  # Set the kind of participant
    access_token.with_grants(video_grant)  # Add the grant to the token

    # Return the token as a JWT string
    jwt = access_token.to_jwt()
    logger.info({"event": "jwt_generated", "jwt_length": len(jwt)})  # Don't log full JWT for security
    return TokenResponse(accessToken=jwt, wsUrl=LIVEKIT_SDK_URL)

# --- API Endpoint to Generate a Token ---
@router.post("/webrtc/get-caller-livekit-token")
@monitor(name="api.sdk.get_caller_livekit_token")
async def get_caller_livekit_token(
    request: CallerTokenRequest,
    background_tasks: BackgroundTasks,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(authenticate_sdk_user),
):
    """
    Generates a LiveKit access token with specific permissions.
    For a 'monitor-client', we disable publishing rights.
    """
    logger.info({"event": "get_caller_livekit_token_request", "request": request.dict() if hasattr(request, 'dict') else str(request)})
    logger.info({"event": "device_type_received", "device_type": request.device_type})
    logger.info({"event": "caller_user_id_found", "caller_user_id": request.caller_user_id})
    participant_identity_type = request.participant_identity_type
    device_type = request.device_type
    if participant_identity_type == "driver":
        user = user_service.get_user_by_id(request.caller_user_id)
        if user:
            request.participant_identity = request.caller_user_id
            user_name = _format_user_name(user)
            if user_name:
                request.participant_identity_name = user_name
        else:
            raise HTTPException(status_code=404, detail="No driver record found") 
        customer_user_id = request.called_user_id
        if not customer_user_id:
            raise HTTPException(status_code=400, detail="called_user_id is required for driver calls")
        customer_user = user_service.get_user_by_id(customer_user_id)
        if not customer_user:
            raise HTTPException(status_code=404, detail="Customer user record not found")
        customer_name = _format_user_name(customer_user)
        title=f"Incoming Call from {user_name}"
        body=f"Please answer the call from {user_name}"
        data={  # Optional additional data
            "title": title,
            "body": body,
            "type": "driver_incoming_call",
            "action": "receive_call",
            "room_name": request.room_name,
            "participant_identity": customer_user_id,
            "participant_identity_name": customer_name,
            "participant_identity_type": "customer",
            "called_user_id": customer_user_id,
            "caller_user_id": request.caller_user_id,
            "is_call_recording": str(request.is_call_recording).lower()
        }
        fcm_tokens = await notification__service.get_user_tokens(customer_user_id, "customer")

    elif participant_identity_type == "customer":
        customer_user = user_service.get_user_by_id(request.caller_user_id)
        if customer_user:
            request.participant_identity = request.caller_user_id
            customer_name = _format_user_name(customer_user)
            if customer_name:
                request.participant_identity_name = customer_name
        else:
            raise HTTPException(status_code=404, detail="No customer record found")
        user_id = request.called_user_id
        if not user_id:
            raise HTTPException(status_code=400, detail="called_user_id is required for customer calls")
        user = user_service.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="No driver record found for customer")
        user_id = str(user.get("_id"))
        user_name = _format_user_name(user)
        
        title=f"Incoming Call from {customer_name}"
        body=f"Please answer the call from {customer_name}"
        data={  # Optional additional data
            "title": title,
            "body": body,
            "type": "customer_incoming_call",
            "action": "receive_call",
            "room_name": request.room_name,
            "participant_identity": user_id,
            "participant_identity_name": user_name,
            "participant_identity_type": "driver",
            "called_user_id": user_id,
            "caller_user_id": request.caller_user_id,
            "is_call_recording": str(request.is_call_recording).lower()
        }
        fcm_tokens = await notification__service.get_user_tokens(user_id, "driver")

    if not fcm_tokens:
            error_message = f"No FCM tokens found for user ID: {request.caller_user_id}"
            logger.warning(error_message)
            raise HTTPException(status_code=404, detail=error_message)    
    prepare_notification = await notification__service.prepare_notification(
        fcm_tokens=fcm_tokens,       
        title=title,
        body=body,
        data=data,
        is_push_notification=request.is_push_notification,
        caller_user_id=request.caller_user_id,
        background_tasks=background_tasks
    )

    logger.info({"event": "prepare_notification_result", "result": prepare_notification})

    """
    logger.info(f"FCM tokens found: {fcm_tokens}")
    logger.info(f"is_push_notification: {request.is_push_notification}")
    if request.is_push_notification:
        logger.info(f"Title: {title} Body: {body} Data: {data}")
        # Create notification request
        notification_request = SendNotificationRequest(
            user_ids=[request.caller_user_id],   # List[str]
            device_tokens=fcm_tokens,            # List[str]
            title=title,                         # str
            body=body,                           # str
            data=data,                           # Optional[dict]
            priority=NotificationPriority.HIGH   # Enum
        )
        logger.info(f"Notification request: {notification_request}")
        # Send notification
        result = await notification__service.send__notification(
            request=notification_request,
            background_tasks=background_tasks
        )
    else:
        logger.info(f"Data notifcation : {data}")

        data_message_request = DataMessageRequest(
            user_ids=[request.caller_user_id],   # List[str]
            device_tokens=fcm_tokens,            # List[str]
            data=data,                           # Optional[dict]
            priority=NotificationPriority.HIGH   # Enum
        )
        result = await notification__service.send_data_message(
            request=data_message_request,
            background_tasks=background_tasks
        )

    logger.info(f"Notification result: {result}")
    """
    #Create livekit token for caller
    request.participant_identity_type = ParticipantType.CALLER
    return await get_token_endpoint(request)


async def _prepare_caller_call_flow(
    request: CallerTokenRequest,
    background_tasks: BackgroundTasks,
    manager: WebRTCCallManager
) -> Tuple[str, str]:
    """
    Shared logic for caller token endpoints to avoid duplication.
    Prepares notification payload, validates users, and schedules call session.
    Returns (called_user_name, called_user_role).
    """
    logger.info({"event": "get_caller_livekit_token_request", "request": request.dict() if hasattr(request, 'dict') else str(request)})
    logger.info({"event": "device_type_received", "device_type": request.device_type})
    logger.info({"event": "caller_user_id_found", "caller_user_id": request.caller_user_id})

    caller_user = user_service.get_user_by_id(request.caller_user_id)
    if caller_user:
        request.participant_identity = request.caller_user_id
        caller_user_role = caller_user.get("role", "customer")        
        caller_user_name = caller_user.get("first_name", "User") + " " + caller_user.get("last_name", "")
        if caller_user_name:
            request.participant_identity_name = caller_user_name
        request.participant_identity_type = ParticipantType.CALLER
    else:
        raise HTTPException(status_code=404, detail="No driver record found")

    called_user = user_service.get_user_by_id(request.called_user_id)
    if not called_user:
        raise HTTPException(status_code=404, detail="No active customer record found for driver")

    called_user_name = called_user.get("first_name", "User") + " " + called_user.get("last_name", "")
    called_user_role = called_user.get("role", "customer")
    logger.info({"event": "called_user_found", "called_user_name": called_user_name, "called_user_role": called_user_role})

    if((caller_user_role == "admin") and (called_user_role == "customer")):
        caller_user_role = "driver"
    elif((caller_user_role == "admin") and (called_user_role == "driver")):
        caller_user_role = "customer"

    # Use caller's organization_id (or callee's as fallback) for call session
    organization_id = None
    if caller_user and caller_user.get("organization_id"):
        organization_id = str(caller_user.get("organization_id"))
    elif called_user and called_user.get("organization_id"):
        organization_id = str(called_user.get("organization_id"))

    call_request = CallRequest(
        caller_id=request.caller_user_id,
        callee_id=request.called_user_id,
        room_name=request.room_name,
        auto_record=request.is_call_recording or False,
        recording_options={"width": 1920, "height": 1080},
        caller_participant={
            "id": request.caller_user_id,
            "name": caller_user_name,
            "phone_number": caller_user.get("phone_number", ""),
            "email": caller_user.get("email", ""),
            "role": caller_user_role
        },
        callee_participant={
            "id": request.called_user_id,
            "name": called_user_name,
            "phone_number": called_user.get("phone_number", ""),
            "email": called_user.get("email", ""),
            "role": called_user_role
        }
    )
    
    asyncio.create_task(
        manager.initiate_call_session(call_request, organization_id=organization_id)
    )

    title = f"Incoming Call from {request.participant_identity_name}"
    body = f"Please answer the call from {request.participant_identity_name}"
    called_token_request = TokenRequest(
        room_name=request.room_name,
        participant_identity=request.called_user_id,
        participant_identity_name=called_user_name,
        participant_identity_type=ParticipantType.CALLEE
    )    

    # Run notification preparation asynchronously in background
    async def _send_notification_async():
        """Helper function to prepare notification data and send notification asynchronously"""
        try:
            # Generate token synchronously (needed for return value)
            called_token_response = await get_token_endpoint(called_token_request)
            
            # Prepare notification data asynchronously (FCM tokens + data preparation)
            async def _prepare_notification_data_async():
                """Async block to prepare FCM tokens and notification data"""
                # Fetch FCM tokens
                fcm_tokens = await notification__service.get_user_tokens(request.called_user_id, called_user_role)
                logger.info({"event": "fcm_tokens_found", "token_count": len(fcm_tokens), "tokens": fcm_tokens})
                
                # Validate FCM tokens
                if not fcm_tokens:
                    error_message = "The called user is offline you cannot call them right now"
                    logger.warning(error_message)
                    # Note: We log the error but don't fail the request since this runs in background
                    return None, None
                
                # Prepare notification data
                data = {
                    "title": title,
                    "body": body,
                    "type": f"{caller_user_role}_incoming_call",
                    "action": "receive_call",
                    "room_name": request.room_name,
                    "participant_identity": request.called_user_id,
                    "participant_identity_name": caller_user_name,
                    "participant_identity_type": caller_user_role,
                    "called_user_id": request.called_user_id,
                    "caller_user_id": request.caller_user_id,
                    "is_call_recording": str(request.is_call_recording).lower(),
                    "accessToken": called_token_response.accessToken,
                    "wsUrl": called_token_response.wsUrl
                }
                
                return fcm_tokens, data
            
            # Prepare notification data
            fcm_tokens, data = await _prepare_notification_data_async()
            
            # Only send notification if FCM tokens are available
            if fcm_tokens and data:
                prepare_notification = await notification__service.prepare_notification(
                    fcm_tokens=fcm_tokens,
                    title=title,
                    body=body,
                    data=data,
                    is_push_notification=request.is_push_notification,
                    caller_user_id=request.caller_user_id,
                    background_tasks=background_tasks
                )
                logger.info({"event": "prepare_notification_result", "result": prepare_notification})
            else:
                logger.warning({"event": "notification_skipped", "reason": "no_fcm_tokens", "room_name": request.room_name})
        except Exception as e:
            logger.error({"event": "notification_preparation_failed", "error": str(e), "room_name": request.room_name})

    # Schedule notification preparation as background task (non-blocking)
    asyncio.create_task(_send_notification_async())

    return called_user_name, called_user_role


async def _prepare_anonymous_caller_call_flow(
    request: AnonymousCallerTokenRequest,
    background_tasks: BackgroundTasks,
    manager: WebRTCCallManager
) -> Tuple[str, str]:
    """
    Prepares call flow when caller is anonymous (not in user DB); callee must exist in user table.
    Validates callee, initiates call session, and schedules notification to callee.
    Returns (called_user_name, called_user_role).
    """
    logger.info({
        "event": "prepare_anonymous_caller_call_flow",
        "anonymous_identity": request.participant_identity,
        "called_user_id": request.called_user_id,
        "room_name": request.room_name
    })

    caller_identity = request.participant_identity
    caller_display_name = (request.participant_identity_name or "Anonymous").strip() or "Anonymous"

    # Validate callee: must exist in user table
    try:
        called_user = user_service.get_user_by_id(request.called_user_id)
    except InvalidId:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid called user ID format: {request.called_user_id}"
        )
    if not called_user:
        raise HTTPException(status_code=404, detail="Callee user not found")

    called_user_name = _format_user_name(called_user)
    called_user_role = called_user.get("role", "customer")
    logger.info({"event": "called_user_found", "called_user_name": called_user_name, "called_user_role": called_user_role})

    # Use callee's organization_id (caller is anonymous, callee receives the call)
    organization_id = None
    if called_user and called_user.get("organization_id"):
        organization_id = str(called_user.get("organization_id"))

    call_request = CallRequest(
        caller_id=caller_identity,
        callee_id=request.called_user_id,
        room_name=request.room_name,
        auto_record=request.is_call_recording or False,
        recording_options={"width": 1920, "height": 1080},
        caller_participant={
            "id": caller_identity,
            "name": caller_display_name,
            "phone_number": "",
            "email": "",
            "role": "caller"
        },
        callee_participant={
            "id": request.called_user_id,
            "name": called_user_name,
            "phone_number": called_user.get("phone_number", ""),
            "email": called_user.get("email", ""),
            "role": called_user_role
        }
    )

    asyncio.create_task(manager.initiate_call_session(call_request, organization_id=organization_id))

    title = f"Incoming Call from {caller_display_name}"
    body = f"Please answer the call from {caller_display_name}"
    called_token_request = TokenRequest(
        room_name=request.room_name,
        participant_identity=request.called_user_id,
        participant_identity_name=called_user_name,
        participant_identity_type=ParticipantType.CALLEE
    )

    async def _send_notification_async():
        try:
            called_token_response = await get_token_endpoint(called_token_request)

            async def _prepare_notification_data_async():
                fcm_tokens = await notification__service.get_user_tokens(request.called_user_id, called_user_role)
                logger.info({"event": "fcm_tokens_found", "token_count": len(fcm_tokens), "tokens": fcm_tokens})
                if not fcm_tokens:
                    return None, None
                data = {
                    "title": title,
                    "body": body,
                    "type": "anonymous_incoming_call",
                    "action": "receive_call",
                    "room_name": request.room_name,
                    "participant_identity": request.called_user_id,
                    "participant_identity_name": caller_display_name,
                    "participant_identity_type": "caller",
                    "called_user_id": request.called_user_id,
                    "caller_user_id": caller_identity,
                    "is_call_recording": str(request.is_call_recording).lower(),
                    "accessToken": called_token_response.accessToken,
                    "wsUrl": called_token_response.wsUrl
                }
                return fcm_tokens, data

            fcm_tokens, data = await _prepare_notification_data_async()
            if fcm_tokens and data:
                await notification__service.prepare_notification(
                    fcm_tokens=fcm_tokens,
                    title=title,
                    body=body,
                    data=data,
                    is_push_notification=request.is_push_notification,
                    caller_user_id=caller_identity,
                    background_tasks=background_tasks
                )
                logger.info({"event": "prepare_notification_result", "anonymous_caller": True})
            else:
                logger.warning({"event": "notification_skipped", "reason": "no_fcm_tokens", "room_name": request.room_name})
        except Exception as e:
            logger.error({"event": "notification_preparation_failed", "error": str(e), "room_name": request.room_name})

    asyncio.create_task(_send_notification_async())
    return called_user_name, called_user_role


@router.post("/webrtc/get-caller-token")
@monitor(name="api.webrtc.get_caller_token")
async def get_caller_token(
    request: CallerTokenRequest,
    background_tasks: BackgroundTasks,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(intercept_sdk_access(["webrtc:token:create"])),
):
    """
    Generates a LiveKit access token with specific permissions.
    For a 'monitor-client', we disable publishing rights.
    """
    called_user_name, called_user_role = await _prepare_caller_call_flow(request, background_tasks, manager)
    return await get_token_endpoint(request)


@router.post("/webrtc/get-call-tokens")
@monitor(name="api.webrtc.get_call_tokens")
async def get_call_tokens(
    request: CallerTokenRequest,
    background_tasks: BackgroundTasks,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(intercept_sdk_access(["webrtc:token:create"])),
) -> TokenResponse:
    """
    Extended version of caller token API that also returns the called participant's token.
    """
    called_user_name, called_user_role = await _prepare_caller_call_flow(
        request, background_tasks, manager
    )

    caller_token_response = await get_token_endpoint(request)

    logger.info({
        "event": "get-call-tokens",
        "caller_identity": request.participant_identity,
        "called_identity": request.called_user_id,
        "room_name": request.room_name
    })    
    return caller_token_response


@router.post("/webrtc/get-call-tokens-anonymous")
@monitor(name="api.webrtc.get_call_tokens_anonymous")
async def get_call_tokens_anonymous(
    request: AnonymousCallerTokenRequest,
    background_tasks: BackgroundTasks,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _: str = Depends(authenticate_static_token),
) -> TokenResponse:
    """
    Same as get-call-tokens but the caller is anonymous (not in user DB).
    Requires API key: send Authorization: Bearer <STATIC_API_KEY>.
    For use outside of login (e.g. public guest call from website).
    Caller is identified by participant_identity and participant_identity_name only.
    Callee: provide called_user_id, or use ANONYMOUS_GUEST_CALL_ADMIN_EMAIL from config/.env.
    """
    # Resolve called_user_id: use request value or fetch admin by ANONYMOUS_GUEST_CALL_ADMIN_EMAIL (cached)
    called_user_id = request.called_user_id
    if not called_user_id:
        called_user_id = _get_cached_anonymous_admin_user_id()
        if not called_user_id:
            admin_email = ANONYMOUS_GUEST_CALL_ADMIN_EMAIL
            if not admin_email:
                raise HTTPException(
                    status_code=400,
                    detail="called_user_id is required when ANONYMOUS_GUEST_CALL_ADMIN_EMAIL is not configured in .env",
                )
            admin_user = user_service.get_user_by_email(admin_email)
            if not admin_user:
                raise HTTPException(
                    status_code=404,
                    detail=f"Admin user not found for email: {admin_email}",
                )
            called_user_id = str(admin_user["_id"])
            _set_cached_anonymous_admin_user_id(called_user_id)
        request = request.model_copy(update={"called_user_id": called_user_id})

    await _prepare_anonymous_caller_call_flow(request, background_tasks, manager)

    # Build token request for anonymous caller (same shape as get_token_endpoint expects)
    token_request = TokenRequest(
        room_name=request.room_name,
        participant_identity=request.participant_identity,
        participant_identity_name=request.participant_identity_name or "Anonymous",
        participant_identity_type=ParticipantType.CALLER
    )
    caller_token_response = await get_token_endpoint(token_request)

    logger.info({
        "event": "get-call-tokens-anonymous",
        "caller_identity": request.participant_identity,
        "called_identity": request.called_user_id,
        "room_name": request.room_name
    })
    return caller_token_response


async def _validate_messaging_users(
    sender_user_id: str,
    receiver_user_id: str
) -> Tuple[str, str, str, str]:
    """
    Validate and retrieve sender and receiver user information.
    
    Returns:
        Tuple containing (sender_name, sender_role, receiver_name, receiver_role)
    """
    # Validate and get sender user
    try:
        sender_user = user_service.get_user_by_id(sender_user_id)
    except InvalidId:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sender user ID format: {sender_user_id}"
        )
    
    if not sender_user:
        raise HTTPException(status_code=404, detail="Sender user not found")
    
    sender_name = _format_user_name(sender_user)
    sender_role = sender_user.get("role", "customer")
    
    # Validate and get receiver user
    try:
        receiver_user = user_service.get_user_by_id(receiver_user_id)
    except InvalidId:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid receiver user ID format: {receiver_user_id}"
        )
    
    if not receiver_user:
        raise HTTPException(status_code=404, detail="Receiver user not found")
    
    receiver_name = _format_user_name(receiver_user)
    receiver_role = receiver_user.get("role", "customer")
    
    return sender_name, sender_role, receiver_name, receiver_role


async def _generate_messaging_token(
    request: MessagingTokenRequest,
    sender_name: str,
    sender_role: str
) -> TokenResponse:
    """
    Generate LiveKit tokens for both sender and receiver participants.
    
    Returns:
        Tuple containing (sender_token_response, receiver_token_response)
    """
    # Prepare sender token request
    sender_token_request = TokenRequest(
        room_name=request.room_name,
        participant_identity=request.sender_user_id,
        participant_identity_name=sender_name,
        participant_identity_type=sender_role
    )
    
    # Generate sender token (this will be returned in response)
    sender_token_response = await get_token_endpoint(sender_token_request)
        
    return sender_token_response


async def _prepare_receiver_notification_data(
    request: MessagingTokenRequest,
    sender_name: str,
    sender_role: str,
    receiver_name: str,
    receiver_role: str
) -> Tuple[Optional[List[str]], Optional[dict], Optional[str], Optional[str]]:
    """
    Prepare notification data for receiver including FCM tokens and payload.
    
    Returns:
        Tuple containing (fcm_tokens, data, title, body) or (None, None, None, None) if no FCM tokens
    """
    # Fetch FCM tokens for receiver
    fcm_tokens = await notification__service.get_user_tokens(request.receiver_user_id, receiver_role)
    logger.info({
        "event": "fcm_tokens_found",
        "token_count": len(fcm_tokens),
        "tokens": fcm_tokens
    })
    
    # Validate FCM tokens
    if not fcm_tokens:
        error_message = "The receiver user is offline and cannot receive messages right now"
        logger.warning(error_message)
        return None, None, None, None
    
    # Prepare notification data
    title = f"New Message from {sender_name}"
    body = f"You have a new message from {sender_name}"
    data = {
        "title": title,
        "body": body,
        "type": "incoming_message",
        "action": "receive_message",
        "category": "receive_message",
        "room_name": request.room_name,
        "participant_identity": request.receiver_user_id,
        "participant_identity_name": receiver_name,
        "participant_identity_type": receiver_role,
        "sender_user_id": request.sender_user_id,
        "receiver_user_id": request.receiver_user_id,
        "sender_name": sender_name
    }
    
    return fcm_tokens, data, title, body


async def _send_receiver_notification_async(
    request: MessagingTokenRequest,
    sender_name: str,
    sender_role: str,
    receiver_name: str,
    receiver_role: str,
    background_tasks: BackgroundTasks
):
    """
    Send receiver token and messaging data via Firebase notification asynchronously.
    """
    try:
        # Prepare notification data
        fcm_tokens, data, title, body = await _prepare_receiver_notification_data(
            request=request,
            sender_name=sender_name,
            sender_role=sender_role,
            receiver_name=receiver_name,
            receiver_role=receiver_role
        )
        
        # Only send notification if FCM tokens are available
        if fcm_tokens and data:
            prepare_notification = await notification__service.prepare_notification(
                fcm_tokens=fcm_tokens,
                title=title,
                body=body,
                data=data,
                is_push_notification=request.is_push_notification,
                caller_user_id=request.sender_user_id,
                background_tasks=background_tasks
            )
            
            logger.info({
                "event": "prepare_notification_result",
                "result": prepare_notification
            })
        else:
            logger.warning({
                "event": "notification_skipped",
                "reason": "no_fcm_tokens",
                "room_name": request.room_name
            })
    except Exception as e:
        logger.error({
            "event": "notification_preparation_failed",
            "error": str(e),
            "room_name": request.room_name
        })


async def _prepare_messaging_flow(
    request: MessagingTokenRequest,
    background_tasks: BackgroundTasks
) -> TokenResponse:
    """
    Shared logic for messaging token endpoints to avoid duplication.
    Prepares notification payload, validates users, and generates tokens.
    Returns (sender_token_response, receiver_token_response).
    """
    logger.info({
        "event": "prepare_messaging_flow",
        "sender_user_id": request.sender_user_id,
        "receiver_user_id": request.receiver_user_id,
        "room_name": request.room_name
    })
    
    # Validate users
    sender_name, sender_role, receiver_name, receiver_role = await _validate_messaging_users(
        sender_user_id=request.sender_user_id,
        receiver_user_id=request.receiver_user_id
    )
    
    # Generate tokens for both participants
    sender_token_response = await _generate_messaging_token(
        request=request,
        sender_name=sender_name,
        sender_role=sender_role
    )
    
    if request.is_push_notification:
        # Send receiver notification asynchronously in background
        async def _send_notification_async():
            """Helper function to send notification asynchronously"""
            await _send_receiver_notification_async(
                request=request,
                sender_name=sender_name,
                sender_role=sender_role,
                receiver_name=receiver_name,
                receiver_role=receiver_role,
                background_tasks=background_tasks
            )
        
        # Schedule notification preparation as background task (non-blocking)
        asyncio.create_task(_send_notification_async())
    
    return sender_token_response



@router.post("/webrtc/get-called-token")
@monitor(name="api.webrtc.get_called_token")
async def get_called_token(
    request: CalledTokenRequest,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(intercept_sdk_access(["webrtc:token:create"])),
):
    """
    Generates a LiveKit access token with specific permissions.
    For a 'monitor-client', we disable publishing rights.
    """
    try:
        user = user_service.get_user_by_id(request.called_user_id)
    except InvalidId:
        user = None

    if user:
        request.participant_identity = request.called_user_id
        user_name = _format_user_name(user)
        if user_name.strip():
            request.participant_identity_name = user_name.strip()
        request.participant_identity_type = ParticipantType.CALLEE
    else:
        raise HTTPException(status_code=404, detail="Called user not found")
    logger.info(f"User: {user_name} with ID: {request.called_user_id}")
    request_data = CallRequest(
        caller_id="caller",
        callee_id=request.participant_identity,
        room_name=request.room_name,
        auto_record=request.is_call_recording,
        recording_options={"width": 1920, "height": 1080}
    )
    asyncio.create_task(
        manager.update_call_session(request_data)
    )
    logger.info({"event": "call_request_data", "request_data": request_data.dict() if hasattr(request_data, 'dict') else str(request_data)})
    if request_data.auto_record:
        recording_options = request_data.recording_options
                
        # Start recording after a delay to ensure participants join
        asyncio.create_task(
            manager._delayed_recording_start(request.room_name, recording_options, delay=5)
        )
    return await get_token_endpoint(request)


@router.post("/webrtc/calls/{room_name}/end")
@monitor(name="api.webrtc.end_call")
async def end_call(
    room_name: str,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(intercept_sdk_access(["webrtc:call:end"])),
):
    """End an active call and destroy the LiveKit room"""
    try:
        # Broadcast call ended event
        await websocket_manager.broadcast_to_room(room_name, {
            "type": "call_ended",
            "room_name": room_name
        })
        # Update call session (status, duration) immediately - don't rely solely on webhook
        try:
            await manager.end_call(room_name)
        except Exception as end_err:
            logger.warning({"event": "end_call_update_failed", "room_name": room_name, "error": str(end_err)})
        # Destroy the LiveKit room (room_finished webhook will also fire, end_call is idempotent)
        await _delete_livekit_room(room_name)
        return {"success": True, "message": "Call ending initiated", "room_name": room_name}
    except Exception as e:
        logger.error(f"Failed to end call {room_name}: {e}")
        # Still try to delete the room even if call end failed
        await _delete_livekit_room(room_name)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/webrtc/calls/{room_name}/status", response_model=CallStatusResponse)
@monitor(name="api.webrtc.get_call_status")
async def get_call_status(
    room_name: str,
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(intercept_sdk_access(["webrtc:call:read"])),
):
    """Get current call status and recording information"""
    try:
        return await manager.get_call_status(room_name)
    except Exception as e:
        logger.error(f"Failed to get status for {room_name}: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    
@router.get("/webrtc/calls")
@monitor(name="api.webrtc.list_active_calls")
async def list_active_calls(
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(intercept_sdk_access(["webrtc:call:read"])),
):
    """List all active calls"""
    return await manager.list_active_calls()

@router.get("/webrtc/calls/debug")
@monitor(name="api.webrtc.debug_active_calls")
async def debug_active_calls(
    manager: WebRTCCallManager = Depends(get_call_manager),
    _principal: dict = Depends(authenticate_sdk_user),
):
    """Debug endpoint to check active calls state"""
    return {
        "active_calls": list(manager.active_calls.keys()),
        "active_calls_count": len(manager.active_calls),
        "participant_to_room": dict(manager.participant_to_room)
    }

# Recording Management Endpoints
@router.post("/webrtc/recordings/start")
@monitor(name="api.webrtc.start_standalone_recording")
async def start_standalone_recording(
    request: RecordingRequest,
    _principal: dict = Depends(intercept_sdk_access(["recording:start"])),
):
    """Start recording without call context (standalone recording)"""
    try:
        options = request.options or {}
        egress_id = await recording_manager.start_recording_to_s3(
            room_name=request.room_name,
            recording_options=options
        )
        return {
            "success": True,
            "egress_id": egress_id,
            "room_name": request.room_name,
            "type": request.recording_type
        }
    except Exception as e:
        logger.error(f"Failed to start standalone recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/webrtc/recordings/{egress_id}/status")
@monitor(name="api.webrtc.get_recording_status")
async def get_recording_status(
    egress_id: str,
    _principal: dict = Depends(intercept_sdk_access(["recording:read"])),
):
    """Get recording status and information"""
    try:
        status = await recording_manager.get_recording_status(egress_id)
        if not status:
            raise HTTPException(status_code=404, detail="Recording not found")
        return status
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get recording status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/webrtc/recordings/{egress_id}/stop")
@monitor(name="api.webrtc.stop_standalone_recording")
async def stop_standalone_recording(
    egress_id: str,
    _principal: dict = Depends(intercept_sdk_access(["recording:stop"])),
):
    """Stop a standalone recording"""
    try:
        success = await recording_manager.stop_recording(egress_id)
        return {
            "success": success,
            "egress_id": egress_id
        }
    except Exception as e:
        logger.error(f"Failed to stop recording {egress_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/webrtc/download-recording/{room_name}/{file_name}")
@monitor(name="api.webrtc.download_recording")
async def download_recording(room_name: str, file_name: str):
    try:
        s3_key = f"call-recordings/{room_name}/{file_name}.mp4"
        local_file = await recording_manager.download_call_recording(s3_key)
        return FileResponse(local_file, media_type="video/mp4", filename=os.path.basename(local_file))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Recording not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# WebSocket endpoint for real-time updates
@router.websocket("/webrtc/ws/calls/{room_name}")
@monitor(name="api.webrtc.websocket_endpoint")
async def websocket_endpoint(websocket: WebSocket, room_name: str):
    await websocket_manager.connect(websocket, room_name)
    try:
        while True:
            # Keep connection alive and listen for client messages
            data = await websocket.receive_text()
            # Handle client messages if needed
            logger.info(f"Received WebSocket message for {room_name}: {data}")
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket, room_name)


# Webhook endpoint for LiveKit events
@router.post("/webrtc/webhooks/livekit")
@monitor(name="api.webrtc.livekit_webhook")
async def livekit_webhook(
    request: Request,
    authorization: str = Header(None),
    manager: WebRTCCallManager = Depends(get_call_manager),
):
    """Handle LiveKit webhook events"""
    webhook_data = None
    try:
        body = await request.body()
        verifier = TokenVerifier(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)
        receiver = webhook.WebhookReceiver(token_verifier=verifier)
        event = receiver.receive(body.decode("utf-8"), authorization)
        # WebhookReceiver returns a protobuf WebhookEvent, not a Pydantic model.
        webhook_data = MessageToDict(event)

        event_type = webhook_data.get("event")
        room_data = webhook_data.get("room", {})
        room_name = room_data.get("name")
        participant_data = webhook_data.get("participant", {})
        participant_identity = participant_data.get("identity")
        
        # Log room and participant events
        if event_type == "room_started":
            logger.info({
                "event": "livekit_room_created",
                "room_name": room_name,
                "room_sid": room_data.get("sid"),
                "num_participants": room_data.get("num_participants", 0),
                "webhook_data": webhook_data
            })
        elif event_type == "participant_joined":
            logger.info({
                "event": "livekit_participant_joined",
                "room_name": room_name,
                "participant_identity": participant_identity,
                "participant_sid": participant_data.get("sid"),
                "participant_name": participant_data.get("name"),
                "participant_kind": participant_data.get("kind"),
                "num_participants": room_data.get("num_participants", 0),
                "webhook_data": webhook_data
            })
        elif event_type == "track_published":
            track_data = webhook_data.get("track", {})
            logger.info({
                "event": "livekit_track_published",
                "room_name": room_name,
                "participant_identity": participant_identity,
                "track_sid": track_data.get("sid"),
                "track_type": track_data.get("type"),  # "audio" or "video"
                "track_name": track_data.get("name"),
                "track_mime": track_data.get("mimeType"),
                "track_source": track_data.get("source"),
                "note": "Verify both participants publish audio tracks for voice communication"
            })
        elif event_type == "track_subscribed":
            track_data = webhook_data.get("track", {})
            publisher_identity = webhook_data.get("participant", {}).get("identity")
            logger.info({
                "event": "livekit_track_subscribed",
                "room_name": room_name,
                "subscriber_identity": participant_identity,
                "publisher_identity": publisher_identity,
                "track_sid": track_data.get("sid"),
                "track_type": track_data.get("type"),
                "track_mime": track_data.get("mimeType"),
                "note": "Subscriber can now hear/see publisher's track"
            })
        elif event_type == "participant_left":
            logger.info({
                "event": "livekit_participant_left",
                "room_name": room_name,
                "participant_identity": participant_identity,
                "participant_sid": participant_data.get("sid"),
                "webhook_data": webhook_data
            })
        elif event_type == "room_finished":
            logger.info({
                "event": "livekit_room_finished",
                "room_name": room_name,
                "room_sid": room_data.get("sid"),
                "webhook_data": webhook_data
            })
        # Handle SIP inbound number->user mapping in the main LiveKit webhook too.
        await _handle_sip_inbound_mapping_from_webhook(webhook_data)

        await manager.handle_livekit_webhook(webhook_data)

        # Broadcast webhook events to connected clients
        if room_name:
            await websocket_manager.broadcast_to_room(room_name, {
                "type": "livekit_event",
                "event": event_type,
                "data": webhook_data
            })

        return {"status": "success"}
    except Exception as e:
        logger.error({
            "event": "webhook_processing_error",
            "error": str(e),
            "webhook_data": webhook_data
        })
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

@router.post("/webrtc/calls/reject")
@monitor(name="api.webrtc.reject_call")
async def reject_call(
    request: RejectCallTokenRequest,
    background_tasks: BackgroundTasks
):
    """Reject a call and destroy the LiveKit room"""
    try:
        logger.info(f"Received request: {request}")
        logger.info(f"caller_user_id found: {request.caller_user_id}")
        logger.info(f"called_user_id found: {request.called_user_id}")
        caller_user = user_service.get_user_by_id(request.caller_user_id)
        if caller_user:
            participant_identity_type = caller_user.get("role", "customer")
        else:
            participant_identity_type = "driver" if request.participant_identity_type == "customer" else "customer"
        title="Reject Incoming Call"
        body="Rejecting incoiming the call"
        data={  
            "title": title,
            "body": body,
            "type": "reject_incoming_call",
            "action": "reject_call",
            "room_name": request.room_name,
            "participant_identity": request.called_user_id,
            "participant_identity_type": request.participant_identity_type,
            "called_user_id": request.called_user_id,
            "caller_user_id": request.caller_user_id
        }
        fcm_tokens = await notification__service.get_user_tokens(request.caller_user_id, participant_identity_type)
        logger.info(f"FCM tokens found: {fcm_tokens}")
        if not fcm_tokens:
                error_message = f"No FCM tokens found for user ID: {request.caller_user_id}"
                logger.warning(error_message)
                raise HTTPException(status_code=404, detail=error_message)    
        prepare_notification = await notification__service.prepare_notification(
            fcm_tokens=fcm_tokens,       
            title=title,
            body=body,
            data=data,
            is_push_notification=False,
            caller_user_id=request.caller_user_id,
            background_tasks=background_tasks
        )
        logger.info({"event": "prepare_notification_result", "result": prepare_notification})
        
        # Destroy the LiveKit room when call is rejected
        await _delete_livekit_room(request.room_name)

    except Exception as e:
        logger.error(f"Failed to reject call {request.room_name}: {e}")
        # Still try to delete the room even if reject failed
        if hasattr(request, 'room_name') and request.room_name:
            await _delete_livekit_room(request.room_name)
        raise HTTPException(status_code=500, detail=str(e))



@router.post("/webrtc/calls/caller-end")
@monitor(name="api.webrtc.caller_end_call")
async def caller_end_call(
    request: RejectCallTokenRequest,
    background_tasks: BackgroundTasks
):
    """End call by caller and destroy the LiveKit room"""
    try:
        logger.info(f"Received request: {request}")
        logger.info(f"caller_user_id found: {request.caller_user_id}")
        logger.info(f"called_user_id found: {request.called_user_id}")
        
        # Validate called_user_id before using it
        if not request.called_user_id or request.called_user_id.strip() == "":
            raise HTTPException(status_code=400, detail="called_user_id is required and cannot be empty")
        
        called_user = user_service.get_user_by_id(request.called_user_id)
        if called_user:
            participant_identity_type = called_user.get("role", "customer")
        else:
            participant_identity_type = "driver" if request.participant_identity_type == "customer" else "customer"        
        title="End Call by Caller"
        body="End the call by the caller before connecting"
        
        data={  
            "title": title,
            "body": body,
            "type": "caller_end_call",
            "action": "caller_end_call",
            "room_name": request.room_name,
            "participant_identity": request.called_user_id,
            "participant_identity_type": participant_identity_type,
            "called_user_id": request.called_user_id,
            "caller_user_id": request.caller_user_id
        }    
        fcm_tokens = await notification__service.get_user_tokens(request.called_user_id, participant_identity_type)
        logger.info(f"FCM tokens found: {fcm_tokens}")
        if not fcm_tokens:
                error_message = f"No FCM tokens found for user ID: {request.caller_user_id}"
                logger.warning(error_message)
                raise HTTPException(status_code=404, detail=error_message)    
        prepare_notification = await notification__service.prepare_notification(
            fcm_tokens=fcm_tokens,       
            title=title,
            body=body,
            data=data,
            is_push_notification=False,
            caller_user_id=request.caller_user_id,
            background_tasks=background_tasks
        )
        logger.info({"event": "prepare_notification_result", "result": prepare_notification})
        
        # Destroy the LiveKit room when caller ends the call
        await _delete_livekit_room(request.room_name)

    except Exception as e:
        logger.error(f"Failed to end call {request.room_name}: {e}")
        # Still try to delete the room even if caller end failed
        if hasattr(request, 'room_name') and request.room_name:
            await _delete_livekit_room(request.room_name)
        raise HTTPException(status_code=500, detail=str(e))

# To run this server, save it as `main.py` and run: uvicorn main:app --reload