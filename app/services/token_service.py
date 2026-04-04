# services/token_service.py - LiveKit Token Generation Service
import logging
import os
from livekit import api
from app.models.models import TokenRequest, TokenResponse, ParticipantType

logger = logging.getLogger(__name__)

class TokenService:
    """Handles LiveKit token generation with various permission levels"""
    
    def __init__(self):
        self.api_key = os.getenv("LIVEKIT_SDK_API_KEY")
        self.api_secret = os.getenv("LIVEKIT_SDK_API_SECRET")
        self.server_url = os.getenv("LIVEKIT_SDK_URL")
        
        if not all([self.api_key, self.api_secret, self.server_url]):
            raise ValueError("Missing LiveKit configuration. Check LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET, and LIVEKIT_SDK_URL")
        
        logger.info("TokenService initialized successfully")
    
    async def generate_token(self, request: TokenRequest) -> TokenResponse:
        """Generate a LiveKit access token with appropriate permissions"""
        try:
            # Create AccessToken object
            access_token = api.AccessToken(self.api_key, self.api_secret)
            
            # Determine permissions based on participant type
            video_grant = self._create_video_grant(request)
            
            # Configure token
            access_token.with_identity(request.participant_identity)
            access_token.with_name(request.participant_identity_name or request.participant_identity)
            access_token.with_kind(request.participant_identity_type.value if request.participant_identity_type else "user")
            access_token.with_grants(video_grant)
            
            # Add metadata for enhanced functionality
            metadata = {
                "participant_type": request.participant_identity_type.value if request.participant_identity_type else "user",
                "generated_at": str(api.time.time()),
                "room_name": request.room_name
            }
            access_token.with_metadata(str(metadata))
            
            # Generate JWT
            jwt_token = access_token.to_jwt()
            
            logger.info(
                f"Generated token for '{request.participant_identity}' "
                f"({request.participant_identity_type.value if request.participant_identity_type else 'user'}) "
                f"in room '{request.room_name}'"
            )
            
            return TokenResponse(
                accessToken=jwt_token,
                wsUrl=self.server_url
            )
            
        except Exception as e:
            logger.error(f"Failed to generate token: {e}")
            raise
    
    def _create_video_grant(self, request: TokenRequest) -> api.VideoGrants:
        """Create VideoGrants based on participant type"""
        participant_type = request.participant_identity_type or ParticipantType.CALLER
        
        if participant_type == ParticipantType.RECORDER:
            # Recording participant - can subscribe but typically doesn't publish
            return api.VideoGrants(
                room=request.room_name,
                room_join=True,
                can_publish=False,        # Recorders don't publish their own content
                can_subscribe=True,       # Must subscribe to record others
                can_publish_data=True,    # For recording metadata/status updates
                recorder=True,            # Core recording capability
                room_record=True,         # Explicit recording permission
                room_admin=False,         # Limited to recording functions only
            )
        
        elif participant_type == ParticipantType.ADMIN:
            # Admin participant - full permissions
            return api.VideoGrants(
                room=request.room_name,
                room_join=True,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                recorder=True,            # Can control recordings
                room_record=True,         # Can record
                room_admin=True,          # Full admin rights
                room_create=True,         # Can create rooms
                room_list=True            # Can list rooms
            )
        
        elif participant_type == ParticipantType.MONITOR:
            # Monitor participant - observe only
            return api.VideoGrants(
                room=request.room_name,
                room_join=True,
                can_publish=False,        # Monitor doesn't publish
                can_subscribe=True,       # But can observe
                can_publish_data=False,   # Limited data publishing
                recorder=False,           # No recording rights
                room_record=False
            )
        
        else:  # CALLER, CALLEE, or default
            # Regular participant - standard call permissions
            return api.VideoGrants(
                room=request.room_name,
                room_join=True,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                recorder=False,           # Regular users can't record by default
                room_record=False,
                can_update_own_metadata=True
            )
    
    async def generate_recording_token(self, room_name: str, recorder_identity: str) -> TokenResponse:
        """Generate a specialized token for recording purposes"""
        recording_request = TokenRequest(
            participant_identity=f"recorder-{recorder_identity}",
            participant_identity_name=f"Recording Service - {recorder_identity}",
            participant_identity_type=ParticipantType.RECORDER,
            room_name=room_name
        )
        
        return await self.generate_token(recording_request)
    
    async def generate_monitor_token(self, room_name: str, monitor_identity: str) -> TokenResponse:
        """Generate a token for monitoring/observing a call"""
        monitor_request = TokenRequest(
            participant_identity=f"monitor-{monitor_identity}",
            participant_identity_name=f"Monitor - {monitor_identity}",
            participant_identity_type=ParticipantType.MONITOR,
            room_name=room_name
        )
        
        return await self.generate_token(monitor_request)
    
    async def generate_admin_token(self, room_name: str, admin_identity: str) -> TokenResponse:
        """Generate a token with full admin permissions"""
        admin_request = TokenRequest(
            participant_identity=admin_identity,
            participant_identity_name=f"Admin - {admin_identity}",
            participant_identity_type=ParticipantType.ADMIN,
            room_name=room_name
        )
        
        return await self.generate_token(admin_request)
    
    def validate_token_request(self, request: TokenRequest) -> bool:
        """Validate token request parameters"""
        if not request.participant_identity:
            return False
        
        if not request.room_name:
            return False
        
        # Check identity format (basic validation)
        if len(request.participant_identity) < 1 or len(request.participant_identity) > 256:
            return False
        
        if len(request.room_name) < 1 or len(request.room_name) > 256:
            return False
        
        return True