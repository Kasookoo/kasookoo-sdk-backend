#!/usr/bin/env python3
"""
FastAPI server for WebRTC-SIP bridge operations
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from app.config import (
    LIVEKIT_SDK_URL,
    LIVEKIT_SDK_API_KEY,
    LIVEKIT_SDK_API_SECRET,
    SIP_TRUNK_NAME,
    SIP_INBOUND_ADDRESSES,
    SIP_OUTBOUND_ADDRESS,
    SIP_OUTBOUND_USERNAME,
    SIP_OUTBOUND_PASSWORD,
)

from app.services.livekit_sip_bridge import LiveKitSIPBridge, SIPBridgeAPI

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from livekit.api import webhook, TokenVerifier
from app.services import get_sip_bridge

from app.api.auth import authenticate_static_token, sdk_token_scheme, authenticate_token
from app.utils.performance_monitor import monitor


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Pydantic models for API requests
class MakeCallRequest(BaseModel):
    phone_number: str
    room_name: Optional[str] = None
    participant_name: Optional[str] = None


class EndCallRequest(BaseModel):
    participant_identity: str
    room_name: str


class JoinRoomRequest(BaseModel):
    room_name: str
    participant_identity: str


class CallStatusResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    wsUrl: Optional[str] = None
    error: Optional[str] = None


router = APIRouter()

sip_bridge: Optional[LiveKitSIPBridge] = None
api_handler: Optional[SIPBridgeAPI] = None


# @router.on_event("startup")
async def startup_event():
    global sip_bridge, api_handler

    print("Initializing LiveKit SIP Bridge...")

    sip_bridge, api_handler = get_sip_bridge()

    try:
        await sip_bridge.setup_sip_outbound_trunk(
            trunk_name=SIP_TRUNK_NAME,
            outbound_address=SIP_OUTBOUND_ADDRESS,
            outbound_username=SIP_OUTBOUND_USERNAME,
            outbound_password=SIP_OUTBOUND_PASSWORD,
        )

        trunk_id = await sip_bridge.setup_sip_inbound_trunk(
            trunk_name=SIP_TRUNK_NAME,
            inbound_addresses=SIP_INBOUND_ADDRESSES,
        )

        await sip_bridge.create_dispatch_rule(
            rule_name="incoming-calls-rule",
            room_name_pattern="sip-call-{call_id}",
        )

        print(f"SIP Bridge initialized successfully with trunk: {trunk_id}")

    except Exception as e:
        print(f"Failed to initialize SIP bridge: {e}")

@router.post("/sip/livekit-events")
@monitor_api("api.v1.sip.livekit-events")
async def receive_livekit_webhook(request: Request, authorization: str = Header(None)):
    body = await request.body()

    verifier = TokenVerifier(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)
    receiver = webhook.WebhookReceiver(token_verifier=verifier)

    event = receiver.receive(body.decode("utf-8"), authorization)
    logger.info(
        {
            "event": "livekit_webhook_received",
            "event_type": event.event,
            "room_name": event.room.name,
        }
    )

    if event.event == "participant_joined":
        caller = event.participant
        room_name = event.room.name
        send_push_notification("device_token", room_name, caller.name or caller.identity)
    return {"status": "success"}


def send_push_notification(device_token, room_name, caller_name):
    logger.info(
        {
            "event": "sending_push_notification",
            "device_token": device_token,
            "room_name": room_name,
            "caller_name": caller_name,
        }
    )


@router.post("/sip/calls/make", response_model=CallStatusResponse)
@monitor("api.sip.make_outbound_call")
async def make_outbound_call(request: MakeCallRequest, token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    return await sip_outbound_call(request)


@router.post("/sip/calls/dial", response_model=CallStatusResponse)
@monitor("api.sip.dial_outbound_call")
async def dial_outbound_call(request: MakeCallRequest, _token: str = Depends(authenticate_static_token)):
    return await sip_outbound_call(request)


@monitor("api.sip.sip_outbound_call")
async def sip_outbound_call(request: MakeCallRequest):
    global sip_bridge, api_handler
    if not api_handler:
        sip_bridge, api_handler = get_sip_bridge()
    if not api_handler:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")

    try:
        result = await api_handler.handle_make_call(
            {
                "phone_number": request.phone_number,
                "room_name": request.room_name,
                "participant_name": request.participant_name,
            }
        )

        logger.info({"event": "call_result", "result": result})
        if result["success"]:
            return CallStatusResponse(
                success=True,
                message="Call initiated successfully",
                data=result,
                wsUrl=LIVEKIT_SDK_URL,
            )
        return CallStatusResponse(
            success=False,
            message="Failed to initiate call",
            error=result.get("error"),
        )

    except Exception as e:
        e.with_traceback()
        print(f"Error making call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sip/calls/end", response_model=CallStatusResponse)
@monitor("api.v1.sip.end_call")
async def end_call(request: EndCallRequest, token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    return await end_sip_call(request)


@router.post("/sip/calls/hangup", response_model=CallStatusResponse)
@monitor("api.sdksip.hangup_call")
async def hangup_call(request: EndCallRequest, _token: str = Depends(authenticate_static_token)):
    return await end_sip_call(request)


@monitor("api.sip.end_sip_call")
async def end_sip_call(request: EndCallRequest):
    global sip_bridge, api_handler
    if not api_handler:
        sip_bridge, api_handler = get_sip_bridge()
    if not api_handler:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")

    try:
        result = await api_handler.handle_end_call(
            {
                "participant_identity": request.participant_identity,
                "room_name": request.room_name,
            }
        )

        return CallStatusResponse(
            success=result["success"],
            message=result["message"],
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sip/rooms/token")
@monitor("api.v1.sip.generate_room_token")
async def generate_room_token(request: JoinRoomRequest, token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    global sip_bridge, api_handler
    if not sip_bridge:
        sip_bridge, api_handler = get_sip_bridge()
    if not sip_bridge:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")

    try:
        token_jwt = sip_bridge.generate_access_token(
            room_name=request.room_name,
            participant_identity=request.participant_identity,
        )

        return {
            "success": True,
            "token": token_jwt,
            "room_name": request.room_name,
            "livekit_url": LIVEKIT_SDK_URL,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sip/sip/trunks")
@monitor("api.v1.sip.list_sip_trunks")
async def list_sip_trunks(token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    global sip_bridge, api_handler
    if not sip_bridge:
        sip_bridge, api_handler = get_sip_bridge()
    if not sip_bridge:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")

    try:
        trunks = await sip_bridge.list_sip_trunks()
        return {
            "success": True,
            "trunks": trunks,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sip/health")
@monitor("api.v1.sip.health_check")
async def health_check():
    sb, _ = get_sip_bridge()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "sip_bridge_initialized": sb is not None,
    }


@router.get("/sip")
@monitor("api.v1.sip.root")
async def root():
    return {
        "message": "LiveKit SIP Bridge API",
        "version": "1.0.0",
        "endpoints": {
            "make_call": "POST /sip/calls/make",
            "end_call": "POST /sip/calls/end",
            "generate_token": "POST /sip/rooms/token",
            "list_trunks": "GET /sip/sip/trunks",
            "health": "GET /sip/health",
        },
    }


async def cleanup_ended_calls():
    pass
