#!/usr/bin/env python3
"""
WebRTC to SIP calling through LiveKit - Python Implementation
"""
from app.config import LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET, SDK_SIP_OUTBOUND_TRUNK_ID
import asyncio
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from livekit import api
from livekit.protocol.sip import CreateSIPOutboundTrunkRequest, CreateSIPParticipantRequest, SIPOutboundTrunkInfo
from livekit.api.room_service import RoomParticipantIdentity

#from livekit.api import RoomService
#from livekit.protocol import sip_pb2
import jwt

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LiveKitSIPBridge:
    """
    Main class for handling WebRTC to SIP bridge through LiveKit
    """
    
    def __init__(self, livekit_url: str, api_key: str, api_secret: str):
        self.livekit_url = livekit_url
        self.api_key = api_key
        self.api_secret = api_secret
        
        # Initialize LiveKit services
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
                            outbound_numbers_regex: str = r'\+1\d{10}') -> str:
        """
        Create and configure SIP trunk
        """
        try:          

            trunk = api.SIPOutboundTrunkInfo(
                name=trunk_name,
                address=outbound_address,
                numbers=outbound_numbers_regex,
                auth_username=outbound_username,
                auth_password=outbound_password
            )
            trunk_request = api.CreateSIPOutboundTrunkRequest(
                trunk = trunk
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

    async def setup_sip_inbound_trunk(self, 
                            trunk_name: str,
                            inbound_addresses: list,
                            inbound_numbers_regex: str = r'\+1\d{10}') -> str:
        """
        Create and configure SIP trunk
        """
        try:
            trunk =  api.SIPInboundTrunkInfo(
                name=trunk_name,
                address=inbound_addresses,
                numbers=inbound_numbers_regex,
                krisp_enabled = True,
            )
            trunk_request = api.CreateSIPInboundTrunkRequest(
                trunk = trunk
            )
            trunk = await self.sip.create_sip_inbound_trunk(trunk_request)
            #self.sip_trunk_id = trunk.sip_trunk_id
            self.sip_trunk_id = SDK_SIP_OUTBOUND_TRUNK_ID
            #trunk = await self.sip_service.create_sip_trunk(trunk_request)
            #self.sip_trunk_id = trunk.sip_trunk_id
            
            logger.info(f"SIP trunk created successfully: {trunk.sip_trunk_id}")
            return trunk.sip_trunk_id
            
        except Exception as e:
            logger.error(f"Failed to create SIP trunk: {e}")
            raise
    
    async def create_dispatch_rule(self, 
                                 rule_name: str,
                                 room_name_pattern: str = "sip-call-{call_id}",
                                 pin: Optional[str] = None) -> str:
        """
        Create dispatch rule for incoming SIP calls
        """
        if not self.sip_trunk_id:
            raise ValueError("SIP trunk must be created first")
            
        try:
            # Create dispatch rule
            rule = api.SipDispatchRule()
            rule.dispatch_rule_direct.room_name = room_name_pattern
            if pin:
                rule.dispatch_rule_direct.pin = pin
            
            dispatch_request = api.CreateSipDispatchRuleRequest(
                rule=rule,
                trunk_ids=[self.sip_trunk_id],
                name=rule_name
            )
            
            dispatch_rule = await self.sip.create_sip_dispatch_rule(dispatch_request)
            #dispatch_rule = await self.sip_service.create_sip_dispatch_rule(dispatch_request)
            
            logger.info(f"Dispatch rule created: {dispatch_rule.sip_dispatch_rule_id}")
            return dispatch_rule.sip_dispatch_rule_id
            
        except Exception as e:
            logger.error(f"Failed to create dispatch rule: {e}")
            raise
    
    async def make_outbound_call(self, 
                               phone_number: str,
                               room_name: str,
                               participant_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Initiate outbound SIP call to phone number
        """
        if not self.sip_trunk_id:
            raise ValueError("SIP trunk must be created first")
            
        try:
            if not participant_name:
                participant_name = f"sip-{phone_number}"
            
            participant_request = CreateSIPParticipantRequest(
                sip_trunk_id=self.sip_trunk_id,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=participant_name,
                participant_name=f"Phone {phone_number}",
                krisp_enabled = True,
                wait_until_answered = True
            )
            logger.info(f"Making outbound call to {phone_number} in room {room_name}")
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
            logger.error(f"Failed to make outbound call: {e}")
            raise
    
    def generate_access_token(self, 
                            room_name: str,
                            participant_identity: str,
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
        access_token.with_attributes
        access_token.with_identity(participant_identity)
        access_token.with_grants(video_grant)

        # Return the token as a JWT string
        token = access_token.to_jwt()
        logger.info(f"Generated JWT: {token}")
        return token
    
    async def create_room(self, room_name: str, max_participants: int = 10) -> Dict[str, Any]:
        """
        Create a LiveKit room for SIP bridge
        """
        try:
            room_request = api.CreateRoomRequest(
                name=room_name,
                max_participants=max_participants,
                metadata=json.dumps({
                    "type": "sip_bridge",
                    "created_at": datetime.utcnow().isoformat()
                })
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
        List all SIP trunks
        """
        try:
            request = api.ListSipTrunkRequest()
            response = await self.sip.list_sip_trunks(request)
            #response = await self.sip_service.list_sip_trunk(request)
            return [
                {
                    "trunk_id": trunk.sip_trunk_id,
                    "name": trunk.name,
                    "outbound_address": trunk.outbound_address
                }
                for trunk in response.items
            ]
        except Exception as e:
            logger.error(f"Failed to list SIP trunks: {e}")
            raise
    
    async def end_sip_call(self, participant_identity: str, room_name: str) -> bool:
        """
        End SIP call by removing participant from room
        """
        try:
            remove_request = RoomParticipantIdentity(
                room=room_name,
                identity=participant_identity
            )
            
            await self.room_service.remove_participant(remove_request)
            logger.info(f"SIP participant {participant_identity} removed from room {room_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to end SIP call: {e}")
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
    
    async def handle_make_call(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle outbound call request
        """
        phone_number = request_data.get("phone_number")
        room_name = request_data.get("room_name", f"call-{phone_number}-{int(datetime.utcnow().timestamp())}")
        
        if not phone_number:
            raise ValueError("Phone number is required")
        
        try:
            # Create room first
            room_result = await self.sip_bridge.create_room(room_name)
            
            # Make outbound call
            call_result = await self.sip_bridge.make_outbound_call(
                phone_number=phone_number,
                room_name=room_name
            )
            
            # Generate token for WebRTC client
            token = self.sip_bridge.generate_access_token(
                room_name=room_name,
                participant_identity=f"webrtc-user-{int(datetime.utcnow().timestamp())}"
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
    sip_bridge = LiveKitSIPBridge(
        livekit_url=LIVEKIT_SDK_URL,
        api_key=LIVEKIT_SDK_API_KEY,
        api_secret=LIVEKIT_SDK_API_SECRET
    )
    
    try:
        # Setup SIP trunk
        trunk_id = SDK_SIP_OUTBOUND_TRUNK_ID
        
        """
        trunk_id = await sip_bridge.setup_sip_trunk(
            trunk_name="main-trunk",
            inbound_addresses=["192.168.1.100"],
            outbound_address="sip.yourprovider.com:5060",
            outbound_username="your_username",
            outbound_password="your_password"
        )"""
        
        # Create dispatch rule for incoming calls
        dispatch_rule_id = await sip_bridge.create_dispatch_rule(
            rule_name="incoming-calls",
            room_name_pattern="sip-call-{call_id}"
        )
        
        # Create room for test call
        room_info = await sip_bridge.create_room("test-sip-room")
        
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
        webrtc_client = WebRTCClient(LIVEKIT_SDK_URL)
        connection_info = webrtc_client.connect_to_room("test-sip-room", token)
        
        print(f"WebRTC connection info: {connection_info}")
        
    except Exception as e:
        logger.error(f"Error in main: {e}")


if __name__ == "__main__":
    asyncio.run(main())