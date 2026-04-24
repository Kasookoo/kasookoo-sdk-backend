import os
import time
from typing import Any, Dict, Optional
from app.config import LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
from datetime import datetime, timedelta
from pydantic import BaseModel
from livekit import api
import logging
from app.models.models import CallerTokenRequest, RejectCallTokenRequest
from app.services import notification__service

from app.api.auth import authenticate_token, sdk_token_scheme
from app.utils.metrics import monitor_api

from app.config import LIVEKIT_URL
from app.services.notification_service import (
    BroadcastNotificationRequest, 
    BulkNotificationRequest, 
    NotificationResponse, 
    RegisterTokenRequest, 
    SendNotificationRequest, 
    SubscribeTopicRequest, 
    create_indexes
)
from app.services.user_service import user_service
#from livekit import RoomServiceClient



# --- Initialize Services ---
router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Collections
notification_tokens_collection = notification__service.notification_tokens_collection
notification_logs_collection = notification__service.notification_logs_collection


# API Endpoints
@router.on_event("startup")
async def startup():
    try:
        await create_indexes()
        logger.info("Notification service started successfully")
    except Exception as e:
        logger.warning(f"Notification service started with warnings: {e}")
        logger.info("Notification service started (indexes may not be created)")

@router.post("/notifications/register-token", response_model=None)
@monitor_api("notifications.register-token")
async def register_device_token_proxy(request: RegisterTokenRequest, token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    """Register device token for push notifications"""
    user = user_service.get_user_by_id(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_type = user.get("role", "agent") or request.user_type
    success = await notification__service.register_device_token(
        user_id=request.user_id,
        user_type=user_type,
        device_token=request.device_token,
        device_type=request.device_type.value,
        new_device_token=request.new_device_token if hasattr(request, 'new_device_token') else None,
        device_info=request.device_info if hasattr(request, 'device_info') else None
    )
    
    if success:
        return {"success": True, "message": "Device token registered successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to register device token")
    
@router.post("/notifications/update-token", response_model=None)
@monitor_api("notifications.update-token")
async def update_device_token_proxy(request: RegisterTokenRequest, token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    """Update device token for push notifications"""
    try:
        success = await notification__service.register_device_token(
            user_id=request.user_id,
            user_type=request.user_type,
            device_token=request.device_token,        
            device_type=request.device_type.value,
            new_device_token=request.new_device_token if hasattr(request, 'new_device_token') else None,
            device_info=request.device_info if hasattr(request, 'device_info') else None
        )
        
        if success:
            return {"success": True, "message": f"Device token {request.device_token} registered successfully"}
        else:
            raise HTTPException(status_code=500, detail="Failed to register device token")
    except Exception as e:
        logger.error(f"Error updating device token: {e}")
        raise e
    
@router.post("/notifications/unregister-token", response_model=None)
@monitor_api("notifications.unregister-token")
async def unregister_device_token_proxy(request: RegisterTokenRequest, token: str = Depends(sdk_token_scheme)):
    await authenticate_token(token)
    """Register device token for push notifications"""
    success = await notification__service.unregister_device_token(
        user_id=request.user_id,
        user_type=request.user_type,
        device_token=request.device_token,
        device_type=request.device_type.value
    )
    
    if success:
        return {"success": True, "message": "Device token registered successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to register device token")

