#!/usr/bin/env python3
"""
FastAPI server for WebRTC-SIP bridge operations
"""

import logging
import os
import asyncio
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
    SIP_OUTBOUND_PASSWORD
)

# Import the SIP bridge classes from the main module
from app.services.livekit_sip_bridge import LiveKitSIPBridge, SIPBridgeAPI

from fastapi import APIRouter, Depends, FastAPI, HTTPException, BackgroundTasks, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from livekit.api import webhook, TokenVerifier
from app.services import get_sip_bridge

from app.api.auth import authenticate_static_token
from app.utils.metrics import monitor_api


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Pydantic models for API requests
class MakeCallRequest(BaseModel):
    phone_number: str
    room_name: Optional[str] = None
    participant_name: Optional[str] = None
    participant_identity: Optional[str] = None
    organization_id: Optional[str] = None

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

# Initialize FastAPI app
router = APIRouter()


# Global SIP bridge instance - will be initialized lazily
sip_bridge: Optional[LiveKitSIPBridge] = None
api_handler: Optional[SIPBridgeAPI] = None

#@router.on_event("startup")
async def startup_event():
   
    global sip_bridge, api_handler
    
    print("Initializing LiveKit SIP Bridge...")
    
    sip_bridge, api_handler = get_sip_bridge()
    
    try:
        # Setup SIP trunk
        await sip_bridge.setup_sip_outbound_trunk(
            trunk_name=SIP_TRUNK_NAME,
            outbound_address=SIP_OUTBOUND_ADDRESS,
            outbound_username=SIP_OUTBOUND_USERNAME,
            outbound_password=SIP_OUTBOUND_PASSWORD
        )

        trunk_id = await sip_bridge.setup_sip_inbound_trunk(
            trunk_name=SIP_TRUNK_NAME,
            outbound_address=SIP_OUTBOUND_ADDRESS
        )
        
        # Create dispatch rule for incoming calls
        await sip_bridge.create_dispatch_rule(
            rule_name="incoming-calls-rule",
            room_name_pattern="sip-call-{call_id}"
        )
        
        print(f"SIP Bridge initialized successfully with trunk: {trunk_id}")
        
    except Exception as e:
        print(f"Failed to initialize SIP bridge: {e}")
        # Continue anyway - some operations might still work

@router.post("/sip/livekit-events")
@monitor_api("api.v1.sip.livekit-events")
async def receive_livekit_webhook(request: Request, authorization: str = Header(None)):
    # The webhook receiver validates the request to ensure it's from LiveKit
    body = await request.body()

    # Create the verifier
    verifier = TokenVerifier(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)

    # Create the webhook receiver with the verifier
    receiver = webhook.WebhookReceiver(token_verifier=verifier)

    # Then inside your route
    event = receiver.receive(body.decode("utf-8"), authorization)
    #event = webhook.WebhookReceiver().receive(body.decode("utf-8"), authorization)
    logger.info(f"Received LiveKit event: {event.event} for room: {event.room.name}")
    # We only care about the first person joining an empty room (the caller)
    
    if event.event == "participant_joined":
        caller = event.participant
        room_name = event.room.name
        
        # This is where you determine who to notify.
        # For example, the room name could be "call-from-user_a-to-user_b"
        # Or you could look up the call details in your database.
        
        # Simplified logic:      
        send_push_notification("device_token", room_name, caller.name or caller.identity)
    return {"status": "success"}

def send_push_notification(device_token, room_name, caller_name):
    logger.info(f"Sending notification to device: {device_token}")
    logger.info(f"Message: {caller_name} is calling you in room: {room_name}")
    #
    # ---> ADD YOUR FCM/APNs PUSH NOTIFICATION LOGIC HERE <---
    #

@router.post("/sip/calls/make", response_model=CallStatusResponse)
@monitor_api("api.v1.sip.calls.make")
async def make_outbound_call(request: MakeCallRequest, api_key: str = Depends(authenticate_static_token)):
    """
    Initiate an outbound SIP call
    """
    # Static API key authentication - no username needed
    return await sip_outbound_call(request)

@router.post("/sip/calls/dial", response_model=CallStatusResponse)
@monitor_api("api.v1.sip.calls.dial")
async def dial_outbound_call(request: MakeCallRequest, token: str = Depends(authenticate_static_token)):
    """
    Initiate an outbound SIP call
    """    
    return await sip_outbound_call(request)



async def sip_outbound_call(request: MakeCallRequest):
    """
    Initiate an outbound SIP call
    """
    global sip_bridge, api_handler
    if not api_handler:
        sip_bridge, api_handler = get_sip_bridge()
    if not api_handler:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")
    
    try:
        result = await api_handler.handle_make_call({
            "phone_number": request.phone_number,
            "room_name": request.room_name,
            "participant_name": request.participant_name,
            "user_id": request.participant_identity,
            "organization_id": request.organization_id,
        })
        
        logger.info(f"Call result: {result}")
        if result["success"]:
            return CallStatusResponse(
                success=True,
                message="Call initiated successfully",
                data=result,
                wsUrl=LIVEKIT_SDK_URL
            )
        else:
            return CallStatusResponse(
                success=False,
                message="Failed to initiate call",
                error=result.get("error")
            )
            
    except Exception as e:
        e.with_traceback()
        print(f"Error making call: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/sip/calls/end", response_model=CallStatusResponse)
@monitor_api("api.v1.sip.calls.end")
async def end_call(request: EndCallRequest, api_key: str = Depends(authenticate_static_token)):
    """
    End an active SIP call
    """
    # Static API key authentication - no username needed
    return await end_sip_call(request)

@router.post("/sip/calls/hangup", response_model=CallStatusResponse)
@monitor_api("api.v1.sip.calls.hangup")
async def hangup_call(request: EndCallRequest, token: str = Depends(authenticate_static_token)):
    """
    End an active SIP call
    """
    return await end_sip_call(request) 

async def end_sip_call(request: EndCallRequest):
    """
    End an active SIP call
    """
    global sip_bridge, api_handler
    if not api_handler:
        sip_bridge, api_handler = get_sip_bridge()
    if not api_handler:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")
    
    try:
        result = await api_handler.handle_end_call({
            "participant_identity": request.participant_identity,
            "room_name": request.room_name
        })
        
        return CallStatusResponse(
            success=result["success"],
            message=result["message"]
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sip/rooms/token")
@monitor_api("api.v1.sip.rooms.token")
async def generate_room_token(request: JoinRoomRequest, api_key: str = Depends(authenticate_static_token)):
    """
    Generate access token for joining a room
    """
    # Static API key authentication - no username needed
    global sip_bridge, api_handler
    if not sip_bridge:
        sip_bridge, api_handler = get_sip_bridge()
    if not sip_bridge:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")
    
    try:
        token = sip_bridge.generate_access_token(
            room_name=request.room_name,
            participant_identity=request.participant_identity
        )
        
        return {
            "success": True,
            "token": token,
            "room_name": request.room_name,
            "livekit_url": LIVEKIT_SDK_URL
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sip/sip/trunks")
@monitor_api("api.v1.sip.sip.trunks")
async def list_sip_trunks(api_key: str = Depends(authenticate_static_token)):
    """
    List all configured SIP trunks
    """
    # Static API key authentication - no username needed
    global sip_bridge, api_handler
    if not sip_bridge:
        sip_bridge, api_handler = get_sip_bridge()
    if not sip_bridge:
        raise HTTPException(status_code=500, detail="SIP bridge not initialized")
    
    try:
        trunks = await sip_bridge.list_sip_trunks()
        return {
            "success": True,
            "trunks": trunks
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sip/health")
@monitor_api("api.v1.sip.health")
async def health_check():
    """
    Health check endpoint
    """
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "sip_bridge_initialized": sip_bridge is not None
    }


@router.get("/sip")
@monitor_api("api.v1.sip")
async def root():
    """
    Root endpoint with API information
    """
    return {
        "message": "LiveKit SIP Bridge API",
        "version": "1.0.0",
        "endpoints": {
            "make_call": "POST /sip/calls/make",
            "end_call": "POST /sip/calls/end", 
            "generate_token": "POST /sip/rooms/token",
            "list_trunks": "GET /sip/sip/trunks",
            "health": "GET /sip/health"
        }
    }


# Background task for call cleanup
async def cleanup_ended_calls():
    """
    Background task to clean up ended calls
    """
    # Implementation for cleaning up ended calls
    # This could include removing empty rooms, logging call details, etc.
    pass


