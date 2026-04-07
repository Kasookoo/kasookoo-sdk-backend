#!/usr/bin/env python3
"""
WebRTC to SIP calling through LiveKit - Python Implementation
"""
from app.config import LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET, SDK_SIP_OUTBOUND_TRUNK_ID, CALLER_ID, DEFAULT_PHONE_NUMBER
import asyncio
import json
import logging
from typing import Optional, Dict, Any, Union, List
from datetime import datetime

from livekit import api
from livekit.api import DeleteRoomRequest, RoomParticipantIdentity
from livekit.protocol.sip import (
    CreateSIPParticipantRequest,
    SIPOutboundTrunkInfo,
    CreateSIPOutboundTrunkRequest,
    SIPInboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
    DeleteSIPTrunkRequest,
)
from livekit.protocol.room import (
    ListParticipantsRequest,
    CreateRoomRequest
)

from app.models.models import CallRequest
from app.services.recording_manager import LiveKitS3RecordingManager
from app.services.token_service import TokenService
from app.services.call_manager import WebRTCCallManager
from fastapi import HTTPException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize call manager instance for use in SIP bridge
_recording_manager = LiveKitS3RecordingManager()
_token_service = TokenService()
_call_manager_instance = WebRTCCallManager(_recording_manager, _token_service)


def normalize_phone_number(phone_number: str) -> str:
    """
    Normalize phone number to E.164 format (e.g., +1234567890)
    - Removes spaces, dashes, parentheses
    - Adds + prefix if missing
    - Ensures it starts with country code
    """
    if not phone_number:
        return phone_number
    
    # Remove all non-digit characters except +
    cleaned = ''.join(c for c in phone_number if c.isdigit() or c == '+')
    
    # If it doesn't start with +, add it
    #if not cleaned.startswith('+'):
    #    cleaned = '+' + cleaned
    
    return cleaned


class LiveKitSIPBridge:
    """
    Main class for handling WebRTC to SIP bridge through LiveKit
    """
    
    def __init__(self, livekit_url: str, api_key: str, api_secret: str):
        self.livekit_url = livekit_url
        self.api_key = api_key
        self.api_secret = api_secret
        
        # Initialize LiveKit services
        if not LIVEKIT_SDK_URL or not LIVEKIT_SDK_API_KEY or not LIVEKIT_SDK_API_SECRET:
            raise ValueError("LiveKit configuration is missing. Please set LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, and LIVEKIT_SDK_API_SECRET")
        
        lkapi = api.LiveKitAPI(url=LIVEKIT_SDK_URL, api_key=LIVEKIT_SDK_API_KEY, api_secret=LIVEKIT_SDK_API_SECRET)
        self.sip = lkapi.sip
        self.room_service = lkapi.room
        #self.sip_service = SipService(livekit_url, api_key, api_secret)
        #self.room_service = RoomService(livekit_url, api_key, api_secret)
        
        self.sip_trunk_id: Optional[str] = SDK_SIP_OUTBOUND_TRUNK_ID
        
    async def setup_sip_outbound_trunk(self, 
                            trunk_name: str,
                            outbound_address: str,
                            outbound_username: str,
                            outbound_password: str,
                            numbers: Optional[Union[List[str], str]] = None) -> str:
        """
        Create and configure SIP outbound trunk (e.g. for Telnyx: address="sip.telnyx.com", numbers=["+12135550100"]).
        After creation, this trunk is used by make_outbound_call (self.sip_trunk_id is set).
        numbers: list of E.164 numbers the trunk can use (e.g. ["+12135550100"]) or a single regex string (e.g. r"\\+1\\d{10}").
        """
        if numbers is None:
            numbers = ['+12135550100']  # default single number style like reference
        # Normalize to list for LiveKit API (repeated field)
        numbers_list = numbers if isinstance(numbers, list) else [numbers]
        try:
            trunk = SIPOutboundTrunkInfo(
                name=trunk_name,
                address=outbound_address,
                numbers=numbers_list,
                auth_username=outbound_username,
                auth_password=outbound_password
            )
            trunk_request = CreateSIPOutboundTrunkRequest(
                trunk=trunk
            )
            trunk = await self.sip.create_sip_outbound_trunk(trunk_request)
            self.sip_trunk_id = trunk.sip_trunk_id
            #trunk = await self.sip_service.create_sip_trunk(trunk_request)
            #self.sip_trunk_id = trunk.sip_trunk_id
            
            logger.info(f"SIP trunk created successfully: {trunk.sip_trunk_id}")
            return trunk.sip_trunk_id
            
        except Exception as e:
            logger.error(f"Failed to create SIP trunk: {e}")
            raise

    async def delete_sip_outbound_trunk(self, sip_trunk_id: str) -> bool:
        """
        Delete an outbound SIP trunk from LiveKit by ID (DeleteSIPTrunk applies to SIP trunks in LiveKit).
        Returns True if delete succeeded. On failure (including not found), logs and returns False so callers can still clear DB state.
        """
        if not sip_trunk_id or not str(sip_trunk_id).strip():
            return False
        tid = str(sip_trunk_id).strip()
        try:
            await self.sip.delete_sip_trunk(DeleteSIPTrunkRequest(sip_trunk_id=tid))
            logger.info({"event": "livekit_sip_outbound_trunk_deleted", "sip_trunk_id": tid})
            return True
        except Exception as e:
            err = str(e).lower()
            if "not found" in err or "404" in err or "does not exist" in err:
                logger.warning(
                    {"event": "livekit_sip_trunk_delete_not_found", "sip_trunk_id": tid, "error": str(e)}
                )
            else:
                logger.warning(
                    {"event": "livekit_sip_trunk_delete_failed", "sip_trunk_id": tid, "error": str(e)}
                )
            return False

    async def setup_sip_inbound_trunk(self, 
                            trunk_name: str,
                            allowed_addresses: Optional[List[str]] = None,
                            numbers: Optional[List[str]] = None,
                            allowed_numbers: Optional[List[str]] = None,
                            auth_username: Optional[str] = None,
                            auth_password: Optional[str] = None,
                            krisp_enabled: Optional[bool] = None,
                            metadata: Optional[str] = None) -> str:
        """
        Create and configure SIP inbound trunk.
        Mirrors LiveKit CreateSIPInboundTrunk fields (numbers, allowed_addresses, allowed_numbers, auth, krisp, metadata).
        """
        try:
            trunk = SIPInboundTrunkInfo(
                name=trunk_name,
                allowed_addresses=allowed_addresses or [],
                numbers=numbers or [],
                allowed_numbers=allowed_numbers or [],
                auth_username=(auth_username or ""),
                auth_password=(auth_password or ""),
                krisp_enabled=(True if krisp_enabled is None else bool(krisp_enabled)),
                metadata=(metadata or ""),
            )
            trunk_request = CreateSIPInboundTrunkRequest(
                trunk=trunk
            )
            trunk = await self.sip.create_sip_inbound_trunk(trunk_request)
            # Keep outbound trunk id unchanged; inbound trunk is returned to caller for storage.
            #trunk = await self.sip_service.create_sip_trunk(trunk_request)
            #self.sip_trunk_id = trunk.sip_trunk_id
            
            logger.info(f"SIP trunk created successfully: {trunk.sip_trunk_id}")
            return trunk.sip_trunk_id
            
        except Exception as e:
            logger.error(f"Failed to create SIP trunk: {e}")
            raise

    async def delete_sip_inbound_trunk(self, sip_trunk_id: str) -> bool:
        """
        Delete an inbound SIP trunk from LiveKit by ID.
        LiveKit uses a single DeleteSIPTrunk API for both inbound/outbound trunks.
        """
        return await self.delete_sip_outbound_trunk(sip_trunk_id)
    
    async def create_dispatch_rule(self, 
                                 rule_name: str,
                                 room_name_pattern: str = "sip-call-{call_id}",
                                 pin: Optional[str] = None,
                                 trunk_id: Optional[str] = None) -> str:
        """
        Create dispatch rule for incoming SIP calls
        """
        # Use provided trunk_id or fall back to self.sip_trunk_id
        trunk_id_to_use = trunk_id or self.sip_trunk_id
        
        if not trunk_id_to_use:
            raise ValueError("SIP trunk ID must be provided or trunk must be created first")
            
        try:
            # Create dispatch rule
            from livekit.protocol.sip import SIPDispatchRule, SIPDispatchRuleDirect, CreateSIPDispatchRuleRequest
            
            dispatch_rule_direct = SIPDispatchRuleDirect(room_name=room_name_pattern)
            if pin:
                dispatch_rule_direct.pin = pin
            
            rule = SIPDispatchRule(dispatch_rule_direct=dispatch_rule_direct)
            
            dispatch_request = CreateSIPDispatchRuleRequest(
                rule=rule,
                trunk_ids=[trunk_id_to_use],
                name=rule_name
            )
            
            dispatch_rule = await self.sip.create_sip_dispatch_rule(dispatch_request)
            #dispatch_rule = await self.sip_service.create_sip_dispatch_rule(dispatch_request)
            
            logger.info(f"Dispatch rule created: {dispatch_rule.sip_dispatch_rule_id}")
            return dispatch_rule.sip_dispatch_rule_id
            
        except Exception as e:
            logger.error(f"Failed to create dispatch rule: {e}")
            raise
    
    async def _resolve_trunk_from_organization(
        self, organization_id: str, org: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Resolve SIP outbound trunk ID from organization settings.
        If org dict is provided, uses it (avoids duplicate query). Otherwise fetches by organization_id.
        If organization, settings, or sip_outbound_trunk are not in the database, returns configured trunk ID (self.sip_trunk_id).
        Otherwise: use settings.sip_outbound_trunk.trunk_id if set; else create trunk from config and return it.
        """
        from app.services import organization_service
        if org is None:
            org = organization_service.get_organization_by_id(organization_id)
        if not org:
            logger.info({"event": "org_not_found_use_config_trunk", "organization_id": organization_id})
            return self.sip_trunk_id
        settings = org.get("settings") or {}
        sip_cfg = settings.get("sip_outbound_trunk")
        if not sip_cfg:
            logger.info({"event": "org_sip_trunk_not_configured_use_config_trunk", "organization_id": organization_id})
            return self.sip_trunk_id
        if isinstance(sip_cfg, dict):
            trunk_id = sip_cfg.get("trunk_id")
            name = sip_cfg.get("name") or "org-trunk"
            address = sip_cfg.get("address")
            auth_username = sip_cfg.get("auth_username")
            auth_password = sip_cfg.get("auth_password")
            numbers = sip_cfg.get("numbers") or []
        else:
            trunk_id = getattr(sip_cfg, "trunk_id", None)
            name = getattr(sip_cfg, "name", None) or "org-trunk"
            address = getattr(sip_cfg, "address", None)
            auth_username = getattr(sip_cfg, "auth_username", None)
            auth_password = getattr(sip_cfg, "auth_password", None)
            numbers = getattr(sip_cfg, "numbers", None) or []
        if trunk_id:
            return trunk_id
        if address and auth_username and auth_password:
            try:
                created_trunk_id = await self.setup_sip_outbound_trunk(
                    trunk_name=name,
                    outbound_address=address,
                    outbound_username=auth_username,
                    outbound_password=auth_password,
                    numbers=numbers if numbers else None,
                )
                organization_service.update_organization_sip_trunk_id(organization_id, created_trunk_id)
                return created_trunk_id
            except Exception as e:
                logger.warning({"event": "create_trunk_from_org_failed", "organization_id": organization_id, "error": str(e)})
        logger.info({"event": "org_trunk_unavailable_use_config_trunk", "organization_id": organization_id})
        return self.sip_trunk_id

    async def make_outbound_call(self, 
                               phone_number: str,
                               room_name: str,
                               participant_name: Optional[str] = None,
                               user_id: Optional[str] = None,
                               sip_trunk_id: Optional[str] = None,
                               organization_id: Optional[str] = None,
                               org: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Initiate outbound SIP call to phone number.
        Trunk resolution order (first priority to last):
          1. Organization trunk (from DB when organization_id is set; org dict avoids duplicate query)
          2. Explicit sip_trunk_id argument
          3. .env configuration (SDK_SIP_OUTBOUND_TRUNK_ID / self.sip_trunk_id)
        """
        trunk_id = None
        if organization_id:
            trunk_id = await self._resolve_trunk_from_organization(organization_id, org=org)
        trunk_id = trunk_id or sip_trunk_id or self.sip_trunk_id
        logger.info({"sip_trunk_id": trunk_id, "organization_id": organization_id})
        if not trunk_id:
            error_msg = "SIP trunk ID is not configured. Please set SDK_SIP_OUTBOUND_TRUNK_ID environment variable or configure trunk in LiveKit dashboard."
            logger.error({
                "event": "sip_trunk_not_configured",
                "error": error_msg,
                "sip_trunk_id": trunk_id
            })
            raise ValueError(error_msg)
        
        # Normalize phone number to E.164 format
        original_phone_number = phone_number
        normalized_phone_number = normalize_phone_number(phone_number)
        
        if original_phone_number != normalized_phone_number:
            logger.info({
                "event": "phone_number_normalized",
                "original": original_phone_number,
                "normalized": normalized_phone_number
            })
        
        phone_number = normalized_phone_number
        
        # Store original for error reporting
        _original_phone = original_phone_number
            
        try:
            if not participant_name:
                participant_name = f"sip-{phone_number}"
            
            logger.info({
                "event": "making_outbound_sip_call",
                "phone_number": phone_number,
                "original_phone_number": original_phone_number,
                "room_name": room_name,
                "participant_identity": participant_name,
                "sip_trunk_id": trunk_id,
                "note": "Using trunk configured in LiveKit dashboard. Phone number normalized to E.164 format."
            })
            
            
            logger.info({"event": "user_id", "user_id": user_id})            
            
            # Get caller_id from user if user_id is provided (fetch once, reuse for sip_number and call_session)
            sip_number = None
            user = None
            if user_id:
                try:
                    from app.services import user_service as _user_service
                    user = _user_service.get_user_by_id(user_id)
                    if user and user.get("caller_id"):
                        sip_number = user.get("caller_id")
                        logger.info({
                            "event": "using_user_caller_id",
                            "user_id": user_id,
                            "caller_id": sip_number
                        })
                except Exception as e:
                    logger.warning({
                        "event": "failed_to_get_user_caller_id",
                        "user_id": user_id,
                        "error": str(e)
                    })
            
            # Fallback to CALLER_ID from config if user caller_id is not available
            if not sip_number:
                sip_number = CALLER_ID
                if sip_number:
                    logger.info({
                        "event": "using_config_caller_id",
                        "caller_id": sip_number
                    })
            
            # Determine display_name: prioritize CALLER_ID from config, then participant_name, then phone number
            display_name = sip_number if sip_number else (participant_name if participant_name else f"Phone {phone_number}")
            logger.info({"event": "display_name", "display_name": display_name})

            # Create call session in call_manager (persists to call_sessions when call ends via room_finished webhook)
            try:
                if user_id and user and user.get("caller_id"):
                    call_request = CallRequest(
                        caller_id=user_id,
                        callee_id=phone_number,
                        room_name=room_name,
                        auto_record=False,
                        recording_options=None,
                        caller_participant={
                            "id": user_id,
                            "name": user.get("first_name", "") + " " + user.get("last_name", ""),
                            "phone_number": user.get("phone_number", ""),
                            "email": user.get("email", ""),
                            "role": user.get("role", "")
                        },
                        callee_participant=None
                    )
                elif user_id:
                    call_request = CallRequest(
                        caller_id=user_id,
                        callee_id=phone_number,
                        room_name=room_name,
                        auto_record=False,
                        recording_options=None,
                        caller_participant={
                            "id": "Anonymous",
                            "name": "Anonymous",
                            "phone_number": "Anonymous",
                            "email": "Anonymous",
                            "role": "Anonymous"
                        },
                        callee_participant=None
                    )
                else:
                    call_request = CallRequest(
                        caller_id="Anonymous",
                        callee_id=phone_number,
                        room_name=room_name,
                        auto_record=False,
                        recording_options=None,
                        caller_participant={
                            "id": "Anonymous",
                            "name": "Anonymous",
                            "phone_number": "Anonymous",
                            "email": "Anonymous",
                            "role": "Anonymous"
                        },
                        callee_participant=None
                    )
                asyncio.create_task(
                    _call_manager_instance.initiate_call_session(call_request, organization_id=organization_id)
                )
            except Exception as e:
                logger.warning({
                    "event": "failed_to_initiate_call_session_for_sip",
                    "user_id": user_id,
                    "room_name": room_name,
                    "error": str(e)
                })

            participant_request = CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_number=sip_number,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=phone_number,
                participant_name="Call Center Number",
                krisp_enabled=True,
                wait_until_answered=True
            )
            
            participant = await self.sip.create_sip_participant(participant_request)
            
            # Extract all available participant information
            sip_call_id = getattr(participant, 'sip_call_id', None)
            sip_trunk_id = getattr(participant, 'sip_trunk_id', None)
            sip_dial_status = getattr(participant, 'dial_status', None)
            sip_dial_error = getattr(participant, 'dial_error', None)
            
            logger.info({
                "event": "sip_participant_created",
                "participant_id": participant.participant_id,
                "participant_identity": participant.participant_identity,
                "room_name": participant.room_name,
                "sip_call_id": sip_call_id,
                "sip_trunk_id": sip_trunk_id,
                "sip_dial_status": str(sip_dial_status) if sip_dial_status else None,
                "sip_dial_error": str(sip_dial_error) if sip_dial_error else None,
                "phone_number": phone_number,
                "display_name": display_name,
                "note": "SIP participant created. If dial_status is not ANSWERED or dial_error exists, check SIP trunk configuration."
            })
            
            # Immediately check if participant is in the room
            # Note: Even with wait_until_answered=True, participant might not join immediately
            # Give it a moment for the participant to establish connection
            await asyncio.sleep(1.0)  # Increased wait time to 1 second
            participants_after_creation = await self.list_participants(room_name)
            participant_identities_after = [p.get("identity") for p in participants_after_creation]
            
            sip_in_room_immediately = participant.participant_identity in participant_identities_after
            
            logger.info({
                "event": "participants_after_sip_creation",
                "room_name": room_name,
                "expected_sip_identity": participant.participant_identity,
                "current_participants": participant_identities_after,
                "sip_participant_in_room": sip_in_room_immediately,
                "sip_participant_id": participant.participant_id,
                "sip_call_id": getattr(participant, 'sip_call_id', None),
                "note": "If false, participant may join shortly. Background task will continue waiting. Check SIP trunk configuration if participant never joins."
            })
            
            # If participant is already in room, log success
            if sip_in_room_immediately:
                logger.info({
                    "event": "sip_participant_joined_immediately",
                    "room_name": room_name,
                    "sip_participant_identity": participant.participant_identity,
                    "all_participants": participant_identities_after,
                    "note": "SIP participant successfully joined room. Verify audio tracks are published/subscribed."
                })
            else:
                logger.warning({
                    "event": "sip_participant_not_in_room_yet",
                    "room_name": room_name,
                    "sip_participant_identity": participant.participant_identity,
                    "sip_participant_id": participant.participant_id,
                    "current_participants": participant_identities_after,
                    "troubleshooting": [
                        "SIP participant was created but not in room yet",
                        "This may indicate SIP call was answered but media connection not established",
                        "Check SIP trunk configuration and codec compatibility",
                        "Verify SIP provider allows media connection",
                        "Check LiveKit server logs for SIP connection errors"
                    ]
                })
            
            return {
                "participant_id": participant.participant_id,
                "participant_identity": participant.participant_identity,
                "room_name": room_name,
                "phone_number": phone_number,
                "sip_call_id": getattr(participant, 'sip_call_id', None),
                "sip_dial_status": str(getattr(participant, 'dial_status', None)) if hasattr(participant, 'dial_status') else None,
                "sip_dial_error": str(getattr(participant, 'dial_error', None)) if hasattr(participant, 'dial_error') else None
            }
            
        except Exception as e:
            error_str = str(e)
            error_type = type(e).__name__
            
            # Check for 503 SERVICE_UNAVAILABLE error
            is_503_error = "503" in error_str or "SERVICE_UNAVAILABLE" in error_str or "SIP_STATUS_SERVICE_UNAVAILABLE" in error_str
            
            error_details = {
                "event": "outbound_call_failed",
                "error_type": error_type,
                "error": error_str,
                "phone_number": phone_number,
                "room_name": room_name,
                "sip_trunk_id": trunk_id,
                "is_503_error": is_503_error
            }
            
            if is_503_error:
                error_details.update({
                    "phone_number_used": phone_number,
                    "original_phone_number": _original_phone if '_original_phone' in locals() else phone_number,
                    "troubleshooting": {
                        "description": "SIP provider returned 503 SERVICE_UNAVAILABLE. This typically means:",
                        "possible_causes": [
                            "SIP trunk is not properly configured in LiveKit dashboard",
                            "SIP provider is rejecting the call (authentication, rate limiting, or service unavailable)",
                            "Trunk ID does not exist or is incorrect",
                            "SIP provider's server is temporarily unavailable",
                            "Phone number format is incorrect or not allowed by provider",
                            "SIP trunk credentials (username/password) are incorrect",
                            "SIP trunk is not authorized for the destination country/region"
                        ],
                        "actions": [
                            f"Verify trunk ID '{trunk_id}' exists in LiveKit dashboard",
                            f"Check if phone number '{phone_number}' is in correct E.164 format (e.g., +447783021617)",
                            "Verify SIP trunk configuration in LiveKit dashboard:",
                            "  - Check trunk address/endpoint is correct",
                            "  - Verify username and password are correct",
                            "  - Ensure trunk is enabled and active",
                            "Check SIP provider logs for rejection reasons",
                            "Test trunk connectivity from LiveKit dashboard",
                            "Verify SIP provider account has sufficient credits/permissions",
                            "Contact SIP provider support to check if number is blocked or restricted"
                        ]
                    }
                })
            
            logger.error(error_details)
            raise
        
        
    async def make_outbound_call_2(self, 
        phone_number: str,
        room_name: str,
        participant_name: Optional[str] = None,
        user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Initiate outbound SIP call to phone number
        """
        if not self.sip_trunk_id:
            raise ValueError("SIP trunk must be created first")
            
        try:
            if not participant_name:
                participant_name = f"sip-{phone_number}"
            
            # Determine display_name: prioritize CALLER_ID from config, then participant_name, then phone number
            display_name = CALLER_ID if CALLER_ID else (participant_name if participant_name else f"Phone {phone_number}")
            
            # Get caller_id from user if user_id is provided
            sip_number = None
            
            try:
                # Lazy import to avoid circular import issues
                from app.services import user_service as _user_service
                logger.info(f"User ID: {user_id}")
                if user_id:
                    user = _user_service.get_user_by_id(user_id)
                    logger.info(f"User: {user}")
                    if user and user.get("caller_id"):
                        sip_number = user.get("caller_id")
                        logger.info({
                            "event": "using_user_caller_id",
                            "user_id": user_id,
                            "caller_id": sip_number
                        })
                        call_request = CallRequest(
                            caller_id=user_id,
                            callee_id="",
                            room_name=room_name,
                            auto_record=False,
                            recording_options=None,
                            caller_participant={
                                "id": user_id,
                                "name": user.get("first_name", "") + " " + user.get("last_name", ""),
                                "phone_number": user.get("phone_number", ""),
                                "email": user.get("email", ""),
                                "role": user.get("role", "")
                            },
                            callee_participant=None
                        )
                    else:
                        call_request = CallRequest(
                            caller_id=user_id if user_id else "Anonymous",
                            callee_id="",
                            room_name=room_name,
                            auto_record=False,
                            recording_options=None,
                            caller_participant={
                                "id": "Anonymous",
                                "name": "Anonymous",
                                "phone_number": "Anonymous",
                                "email": "Anonymous",
                                "role": "Anonymous"
                            },
                            callee_participant=None
                        )
                else:
                    call_request = CallRequest(
                        caller_id="Anonymous",
                        callee_id="",
                        room_name=room_name,
                        auto_record=False,
                        recording_options=None,
                        caller_participant={
                            "id": "Anonymous",
                            "name": "Anonymous",
                            "phone_number": "Anonymous",
                            "email": "Anonymous",
                            "role": "Anonymous"
                        },
                        callee_participant=None
                    )
                asyncio.create_task(
                    _call_manager_instance.initiate_call_session(call_request)
                )

            except Exception as e:
                logger.warning({
                    "event": "failed_to_get_user_caller_id",
                    "user_id": user_id,
                    "error": str(e)
                })
            
            logger.info(f"Sip number: {sip_number}")
            # Fallback to CALLER_ID from config if user caller_id is not available
            if not sip_number:
                sip_number = CALLER_ID
                if sip_number:
                    logger.info({
                        "event": "using_config_caller_id",
                        "caller_id": sip_number
                    })
            
            participant_request = CreateSIPParticipantRequest(
                sip_trunk_id=self.sip_trunk_id,
                sip_number=sip_number,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=participant_name,
                participant_name=participant_name if participant_name else f"Phone {phone_number}",
                krisp_enabled=True,
                wait_until_answered=True
            )
            logger.info(f"Making outbound call to {phone_number} in room {room_name} from caller {display_name}")
            logger.info(f"Participant request: {participant_request}")
            participant = await self.sip.create_sip_participant(participant_request)
            #participant = await self.sip_service.create_sip_participant(participant_request)
            logger.info(f"Outbound call initiated: {participant}")
            return {
                "participant_id": participant.participant_id,
                "participant_identity": participant.participant_identity,
                "room_name": room_name,
                "phone_number": phone_number
            }
            
        except Exception as e:
            error_str = str(e)
            error_type = type(e).__name__
            
            # Check for 402 PAYMENT_REQUIRED error
            is_402_error = "402" in error_str or "PAYMENT_REQUIRED" in error_str or "SIP_STATUS_PAYMENT_REQUIRED" in error_str
            
            error_details = {
                "event": "outbound_call_failed",
                "error_type": error_type,
                "error": error_str,
                "phone_number": phone_number,
                "room_name": room_name,
                "sip_trunk_id": self.sip_trunk_id,
                "is_402_error": is_402_error
            }
            
            if is_402_error:
                error_details.update({
                    "troubleshooting": {
                        "description": "SIP call failed with 402 PAYMENT_REQUIRED - account payment issue",
                        "sip_trunk_id": self.sip_trunk_id,
                        "check_account": "Verify SIP provider account has sufficient balance/credits",
                        "check_billing": "Check if SIP provider account billing is up to date",
                        "contact_provider": "Contact SIP provider to resolve payment/account issues"
                    }
                })
                logger.error(error_details)
                raise HTTPException(
                    status_code=402,
                    detail="SIP call failed: Payment required. Please check your SIP provider account balance and billing status."
                )
            
            logger.error(error_details)
            raise
    
    def generate_access_token(self, 
                            room_name: str,
                            participant_identity: str,
                            participant_identity_name: Optional[str] = None,
                            participant_identity_type: Optional[str] = None,
                            ttl_hours: int = 1) -> str:
        """
        Generate JWT access token for LiveKit room
        """
        """now = datetime.utcnow()
        exp = now + timedelta(hours=ttl_hours)
        
        payload = {
            "iss": self.api_key,
            "sub": participant_identity,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "video": {
                "roomJoin": True,
                "room": room_name,
                "canPublish": True,
                "canSubscribe": True
            }
        }
        
        token = jwt.encode(payload, self.api_secret, algorithm="HS256")"""

        access_token = api.AccessToken(LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET)
        #api.AudioCodec = api.AudioCodec.OPUS
        # Define the grant (permissions) for the token
        # A monitor should not publish its own audio/video
        video_grant = api.VideoGrants(
            room=room_name,
            room_join=True,
            can_publish=True, # <-- This makes the client a silent observer
            can_subscribe=True,
            can_publish_data=True
        )

        # Add the grant to the token
        #access_token.add_grant(video_grant)
        
        logging.info(f"Issued token for '{participant_identity}' in room '{room_name}'")
        access_token.with_identity(participant_identity)
        access_token.with_name(participant_identity_name or participant_identity)
        # livekit-server-sdk expects ParticipantKind; our code often passes strings.
        # Keep behavior but coerce safely for typing + runtime.
        if participant_identity_type:
            access_token.with_kind(str(participant_identity_type))
        access_token.with_grants(video_grant)

        # Return the token as a JWT string
        token = access_token.to_jwt()
        logger.info(f"Generated JWT: {token}")
        return token
    
    async def create_room(self, room_name: str, max_participants: int = 10, organization_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a LiveKit room for SIP bridge.
        organization_id is stored in room metadata so room_started webhook can use it for call session.
        """
        try:
            metadata_dict = {
                "type": "sip_bridge",
                "created_at": datetime.utcnow().isoformat()
            }
            if organization_id:
                metadata_dict["organization_id"] = organization_id
            room_request = CreateRoomRequest(
                name=room_name,
                max_participants=max_participants,
                metadata=json.dumps(metadata_dict)
            )
            
            room = await self.room_service.create_room(room_request)
            
            logger.info(f"Room created: {room.name}")
            return {
                "room_session_id": room.sid,
                "room_name": room.name,
                "creation_time": room.creation_time,
                "max_participants": room.max_participants
            }
            
        except Exception as e:
            logger.error(f"Failed to create room: {e}")
            raise
    
    async def list_sip_trunks(self) -> list:
        """
        List all SIP outbound trunks
        """
        try:
            from livekit.protocol.sip import ListSIPOutboundTrunkRequest
            request = ListSIPOutboundTrunkRequest()
            response = await self.sip.list_sip_outbound_trunk(request)
            return [
                {
                    "trunk_id": trunk.sip_trunk_id,
                    "name": trunk.name,
                    "outbound_address": getattr(trunk, 'address', None)
                }
                for trunk in response.items
            ]
        except Exception as e:
            logger.error(f"Failed to list SIP trunks: {e}")
            raise
    
    async def list_participants(self, room_name: str) -> list:
        """
        List all participants in a room
        """
        try:
            request = ListParticipantsRequest(room=room_name)
            response = await self.room_service.list_participants(request)
            participants = response.participants if hasattr(response, 'participants') else []
            return [
                {
                    "identity": p.identity,
                    "name": getattr(p, 'name', None),
                    "state": str(getattr(p, 'state', None))
                }
                for p in participants
            ]
        except Exception as e:
            logger.error(f"Failed to list participants: {e}")
            return []
    
    async def end_sip_call(self, participant_identity: str, room_name: str) -> bool:
        """
        End SIP call by removing participant from room, then destroying the room.
        """
        if not room_name or not room_name.strip():
            logger.warning(f"Skipping delete for empty room name")
            return False
        try:
            delay = 1 # 1 seconds to wait for the room to be destroyed
            await asyncio.sleep(delay)
            # First remove the participant (clean SIP hangup)
            if participant_identity:
                try:
                    remove_request = RoomParticipantIdentity(
                        room=room_name,
                        identity=participant_identity
                    )
                    await self.room_service.remove_participant(remove_request)
                    logger.info(f"SIP participant {participant_identity} removed from room {room_name}")
                except Exception as e:
                    error_str = str(e)
                    if "not_found" not in error_str.lower() and "does not exist" not in error_str.lower():
                        logger.warning(f"Failed to remove participant (may have already left): {e}")

            # Then destroy the room (disconnects any remaining participants)
            await self.room_service.delete_room(DeleteRoomRequest(room=room_name))
            logger.info(f"LiveKit room {room_name} destroyed (SIP call ended)")
            return True
        except Exception as e:
            error_str = str(e)
            if "not_found" in error_str.lower() or "does not exist" in error_str.lower() or "404" in error_str:
                logger.info(f"Room {room_name} already deleted or does not exist")
                return True
            logger.error(f"Failed to end SIP call (destroy room): {e}")
            return False


class WebRTCClient:
    """
    WebRTC client for connecting to LiveKit rooms with SIP bridge
    """
    
    def __init__(self, livekit_url: str):
        self.livekit_url = livekit_url
        self.room_name: Optional[str] = None
        self.token: Optional[str] = None
    
    def connect_to_room(self, room_name: str, token: str) -> Dict[str, Any]:
        """
        Connect to LiveKit room (WebRTC connection details)
        """
        self.room_name = room_name
        self.token = token
        
        # Return connection details for frontend WebRTC client
        return {
            "url": self.livekit_url,
            "token": token,
            "room_name": room_name,
            "connection_details": {
                "audio_enabled": True,
                "video_enabled": False,  # SIP calls are typically audio-only
                "data_enabled": True
            }
        }


# Flask/FastAPI example for web API
class SIPBridgeAPI:
    """
    Web API for SIP bridge operations
    """
    
    def __init__(self, sip_bridge: LiveKitSIPBridge):
        self.sip_bridge = sip_bridge

    async def _wait_and_dial_async(
        self,
        room_name: str,
        phone_number: str,
        participant_name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        org: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Background task to wait for WebRTC participant and then dial. Uses organization SIP trunk if organization_id is set.
        When org provided, avoids duplicate organization query for trunk resolution.
        """
        resolved_trunk_id: Optional[str] = None
        try:
            # Step 1: Wait for WebRTC participant to join
            max_attempts = 30  # 30 seconds
            webrtc_joined = False
            webrtc_identity = None
            for attempt in range(max_attempts):
                await asyncio.sleep(1)
                participants = await self.sip_bridge.list_participants(room_name)
                
                if participants and len(participants) > 0:
                    # Find WebRTC participant (not SIP participant)
                    for p in participants:
                        identity = p.get("identity", "")
                        # WebRTC participants typically start with "webrtc-" or don't start with "sip-"
                        if not identity.startswith("sip-") and not identity.startswith("EG_"):
                            webrtc_identity = identity
                            break
                    
                    if webrtc_identity:
                        logger.info({
                            "event": "webrtc_participant_detected",
                            "room_name": room_name,
                            "webrtc_identity": webrtc_identity,
                            "participants_count": len(participants),
                            "all_participants": [p.get("identity") for p in participants],
                            "wait_time_seconds": attempt + 1
                        })
                        webrtc_joined = True
                        break
                elif attempt % 5 == 0:  # Log every 5 seconds
                    logger.info({
                        "event": "waiting_for_webrtc_participant",
                        "room_name": room_name,
                        "wait_time_seconds": attempt + 1,
                        "max_attempts": max_attempts
                    })
            
            if not webrtc_joined:
                logger.warning({
                    "event": "webrtc_participant_timeout",
                    "room_name": room_name,
                    "phone_number": phone_number,
                    "max_attempts": max_attempts
                })
                return
            
            # Step 2: Make outbound SIP call
            logger.info({
                "event": "initiating_sip_call",
                "room_name": room_name,
                "phone_number": phone_number,
                "participant_name": participant_name,
                "webrtc_identity": webrtc_identity
            })

            # Resolve trunk early so background error logs reflect the actual trunk used.
            if organization_id:
                resolved_trunk_id = await self.sip_bridge._resolve_trunk_from_organization(organization_id, org=org)
            resolved_trunk_id = resolved_trunk_id or self.sip_bridge.sip_trunk_id
            
            call_result = await self.sip_bridge.make_outbound_call(
                phone_number=phone_number,
                room_name=room_name,
                participant_name=participant_name,
                user_id=user_id,
                sip_trunk_id=resolved_trunk_id,
                organization_id=organization_id,
                org=org,
            )
            
            sip_participant_identity = call_result.get("participant_identity")
            logger.info({
                "event": "outbound_call_initiated",
                "room_name": room_name,
                "phone_number": phone_number,
                "sip_participant_identity": sip_participant_identity,
                "call_result": call_result
            })
            
            # Step 3: Wait for SIP participant to actually join the room
            # The call is answered, but participant might take a moment to join
            max_wait_attempts = 30  # 30 seconds to wait for SIP participant to join (increased from 15)
            sip_joined = False
            for attempt in range(max_wait_attempts):
                await asyncio.sleep(1)
                participants = await self.sip_bridge.list_participants(room_name)
                
                # Check if SIP participant is in the room
                participant_identities = [p.get("identity") for p in participants]
                participant_details = {p.get("identity"): p for p in participants}
                
                # Verify both participants are present
                webrtc_present = webrtc_identity in participant_identities if webrtc_identity else False
                
                # Check for exact match first
                if sip_participant_identity in participant_identities:
                    logger.info({
                        "event": "sip_participant_joined_room",
                        "room_name": room_name,
                        "sip_participant_identity": sip_participant_identity,
                        "webrtc_participant_identity": webrtc_identity,
                        "webrtc_present": webrtc_present,
                        "all_participants": participant_identities,
                        "total_participants": len(participants),
                        "wait_time_seconds": attempt + 1,
                        "status": "both_participants_present" if webrtc_present else "sip_only"
                    })
                    sip_joined = True
                    break
                # Also check for any participant starting with "sip-" (in case identity differs slightly)
                elif any(pid.startswith("sip-") for pid in participant_identities):
                    sip_participant = next((p for p in participants if p.get("identity", "").startswith("sip-")), None)
                    if sip_participant:
                        logger.info({
                            "event": "sip_participant_joined_room",
                            "room_name": room_name,
                            "expected_sip_identity": sip_participant_identity,
                            "actual_sip_identity": sip_participant.get("identity"),
                            "webrtc_participant_identity": webrtc_identity,
                            "webrtc_present": webrtc_present,
                            "all_participants": participant_identities,
                            "total_participants": len(participants),
                            "wait_time_seconds": attempt + 1,
                            "status": "both_participants_present" if webrtc_present else "sip_only"
                        })
                        sip_joined = True
                        break
                else:
                    # Log every 5 seconds to track progress
                    if (attempt + 1) % 5 == 0 or attempt == 0:
                        logger.info({
                            "event": "waiting_for_sip_participant",
                            "room_name": room_name,
                            "expected_sip_identity": sip_participant_identity,
                            "webrtc_participant_identity": webrtc_identity,
                            "webrtc_present": webrtc_present,
                            "current_participants": participant_identities,
                            "participant_details": participant_details,
                            "wait_time_seconds": attempt + 1,
                            "max_wait_seconds": max_wait_attempts
                        })
            
            if not sip_joined:
                # Get final participant list for diagnostics
                final_participants = await self.sip_bridge.list_participants(room_name)
                final_participant_identities = [p.get("identity") for p in final_participants]
                
                logger.error({
                    "event": "sip_participant_join_timeout",
                    "room_name": room_name,
                    "phone_number": phone_number,
                    "sip_participant_identity": sip_participant_identity,
                    "max_wait_attempts": max_wait_attempts,
                    "final_participants": final_participant_identities,
                    "total_participants": len(final_participants),
                    "diagnostic": "SIP participant was created successfully (call answered) but never joined the room. This indicates a SIP trunk configuration issue.",
                    "troubleshooting": {
                        "check_sip_trunk": "Verify SIP trunk configuration in LiveKit dashboard",
                        "check_codecs": "Ensure codec compatibility (PCMU, PCMA, Opus)",
                        "check_media_encryption": "Verify media encryption settings match SIP provider",
                        "check_network": "Check firewall/network rules for RTP/RTCP ports",
                        "check_sip_provider": "Verify SIP provider allows media connection",
                        "livekit_logs": "Check LiveKit server logs for SIP connection errors"
                    }
                })
            else:
                # Final verification: both participants should be in the room
                final_participants = await self.sip_bridge.list_participants(room_name)
                final_identities = [p.get("identity") for p in final_participants]
                webrtc_final = webrtc_identity in final_identities if webrtc_identity else False
                sip_final = sip_participant_identity in final_identities or any(pid.startswith("sip-") for pid in final_identities)
                
                logger.info({
                    "event": "call_fully_connected",
                    "room_name": room_name,
                    "phone_number": phone_number,
                    "webrtc_joined": webrtc_final,
                    "webrtc_identity": webrtc_identity,
                    "sip_joined": sip_final,
                    "sip_participant_identity": sip_participant_identity,
                    "all_participants": final_identities,
                    "total_participants": len(final_participants),
                    "note": "Both participants are in the room. Verify they are publishing/subscribing to audio tracks for communication."
                })
                
        except Exception as e:
            error_str = str(e)
            is_503_error = "503" in error_str or "SERVICE_UNAVAILABLE" in error_str or "SIP_STATUS_SERVICE_UNAVAILABLE" in error_str
            
            error_details = {
                "event": "wait_and_dial_error",
                "room_name": room_name,
                "phone_number": phone_number,
                "error": error_str,
                "error_type": type(e).__name__,
                "is_503_error": is_503_error
            }
            
            if is_503_error:
                error_details.update({
                    "troubleshooting": {
                        "description": "SIP call failed with 503 SERVICE_UNAVAILABLE during background dialing",
                        "sip_trunk_id": resolved_trunk_id or self.sip_bridge.sip_trunk_id,
                        "check_trunk": f"Verify trunk '{resolved_trunk_id or self.sip_bridge.sip_trunk_id}' is properly configured in LiveKit dashboard",
                        "check_provider": "Check if SIP provider is accepting calls and trunk credentials are correct"
                    }
                })
            
            logger.error(error_details)

    async def handle_make_call(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle outbound call request - waits for WebRTC to join before dialing.
        Fetches organization once for both contact_center_number and trunk (single query).
        """
        phone_number = request_data.get("phone_number")
        room_name = request_data.get("room_name", f"room_{int(datetime.utcnow().timestamp())}")
        participant_name = request_data.get("participant_name")
        wait_for_webrtc = request_data.get("wait_for_webrtc", True)
        user_id = request_data.get("user_id")
        organization_id = request_data.get("organization_id")
        org_settings: Optional[Dict[str, Any]] = None

        if not organization_id and user_id:
            try:
                from app.services import user_service as _user_service
                user = _user_service.get_user_by_id(user_id)
                if user and user.get("organization_id") is not None:
                    organization_id = str(user.get("organization_id"))
            except Exception:
                pass

        if organization_id:
            from app.services import organization_service
            org_settings = organization_service.get_organization_call_settings(organization_id)
        if not phone_number or phone_number == "" or phone_number == "+443333054030":
            contact_num = org_settings.get("contact_center_number") if org_settings else None
            phone_number = contact_num if contact_num else DEFAULT_PHONE_NUMBER

        if not phone_number:
            raise ValueError("Phone number is required")

        try:
            # Create room first (pass organization_id so room_started webhook can use it for call session)
            room_result = await self.sip_bridge.create_room(room_name, organization_id=organization_id)

            # Generate a unique participant identity for WebRTC client
            # IMPORTANT: Client MUST use this exact identity when connecting
            webrtc_participant_identity = (user_id if user_id else f"webrtc-user-{int(datetime.utcnow().timestamp())}")
            
            # Generate token for WebRTC client with the specific identity
            token = self.sip_bridge.generate_access_token(
                room_name=room_name,
                participant_identity=webrtc_participant_identity,
                participant_identity_name=participant_name,
                participant_identity_type="user"
            )
            
            logger.info({
                "event": "call_setup_initiated",
                "room_name": room_name,
                "phone_number": phone_number,
                "webrtc_participant_identity": webrtc_participant_identity,
                "participant_name": participant_name,
                "note": "WebRTC client MUST use the exact participant_identity when connecting"
            })
            
            # Return token immediately in response with actual participant identity
            response = {
                "success": True,
                "call_details": {
                    "participant_identity": webrtc_participant_identity,  # Actual identity to use
                    "room_name": room_name,
                    "phone_number": phone_number
                },
                "room_token": token,
                "room_name": room_name,
                "room_session_id": room_result.get("room_session_id", ""),
                "phone_number": phone_number,
                "participant_identity": webrtc_participant_identity,  # Make it explicit
                "status": "waiting_for_webrtc",
                "wsUrl": self.sip_bridge.livekit_url
            }
            
            # If client wants us to wait, poll for WebRTC participant in background
            if wait_for_webrtc:
                org_for_trunk = org_settings.get("org") if org_settings else None
                asyncio.create_task(
                    self._wait_and_dial_async(
                        room_name=room_name,
                        phone_number=phone_number,
                        participant_name=participant_name,
                        user_id=user_id,
                        organization_id=organization_id,
                        org=org_for_trunk,
                    )
                )
            
            return response
            
        except Exception as e:
            logger.error({
                "event": "call_setup_failed",
                "error": str(e),
                "room_name": room_name,
                "phone_number": phone_number
            })
            return {
                "success": False,
                "error": str(e)
            }
        
    async def handle_make_call_2(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle outbound call request
        """
        phone_number = request_data.get("phone_number")
        room_name = request_data.get("room_name", f"call-{phone_number}-{int(datetime.utcnow().timestamp())}")
        user_id = request_data.get("user_id")
        
        if not phone_number:
            raise ValueError("Phone number is required")
        
        try:
            # Create room first
            room_result = await self.sip_bridge.create_room(room_name)
            
            # Make outbound call
            call_result = await self.sip_bridge.make_outbound_call_2(
                phone_number=phone_number,
                room_name=room_name,
                user_id=user_id
            )
            
            # Generate token for WebRTC client
            token = self.sip_bridge.generate_access_token(
                room_name=room_name,
                participant_identity=(user_id if user_id else f"webrtc-user-{int(datetime.utcnow().timestamp())}")
            )
            
            return {
                "success": True,
                "call_details": call_result,
                "room_token": token,
                "room_name": room_name,
                "room_session_id": room_result.get("room_session_id", ""),
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def handle_end_call(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle end call request
        """
        participant_identity = request_data.get("participant_identity")
        room_name = request_data.get("room_name")
        
        if not participant_identity or not room_name:
            raise ValueError("Participant identity and room name are required")
        
        success = await self.sip_bridge.end_sip_call(participant_identity, room_name)
        
        return {
            "success": success,
            "message": "Call ended" if success else "Failed to end call"
        }


# Example usage
async def main():
    """
    Example usage of the SIP bridge
    """
    # Initialize SIP bridge
    if not LIVEKIT_SDK_URL or not LIVEKIT_SDK_API_KEY or not LIVEKIT_SDK_API_SECRET:
        logger.error("LiveKit configuration is missing")
        return
    
    sip_bridge = LiveKitSIPBridge(
        livekit_url=LIVEKIT_SDK_URL,
        api_key=LIVEKIT_SDK_API_KEY,
        api_secret=LIVEKIT_SDK_API_SECRET
    )
    
    try:
        # Setup SIP trunk
        _trunk_id = SDK_SIP_OUTBOUND_TRUNK_ID
        
        """
        _trunk_id = await sip_bridge.setup_sip_trunk(
            trunk_name="main-trunk",
            inbound_addresses=["192.168.1.100"],
            outbound_address="sip.yourprovider.com:5060",
            outbound_username="your_username",
            outbound_password="your_password"
        )"""
        
        # Create dispatch rule for incoming calls
        _dispatch_rule_id = await sip_bridge.create_dispatch_rule(
            rule_name="incoming-calls",
            room_name_pattern="sip-call-{call_id}"
        )
        
        # Create room for test call
        _room_info = await sip_bridge.create_room("test-sip-room")
        
        # Make outbound call
        call_result = await sip_bridge.make_outbound_call(
            phone_number="+1234567890",
            room_name="test-sip-room"
        )
        
        # Generate token for WebRTC client
        token = sip_bridge.generate_access_token(
            room_name="test-sip-room",
            participant_identity="webrtc-user"
        )
        
        print(f"Call initiated: {call_result}")
        print(f"WebRTC token: {token}")
        
        # WebRTC client connection info
        if LIVEKIT_SDK_URL:
            webrtc_client = WebRTCClient(LIVEKIT_SDK_URL)
            connection_info = webrtc_client.connect_to_room("test-sip-room", token)
        
        print(f"WebRTC connection info: {connection_info}")
        
    except Exception as e:
        logger.error(f"Error in main: {e}")


if __name__ == "__main__":
    asyncio.run(main())