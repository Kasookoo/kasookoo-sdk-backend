from app.config import (
    DB_NAME,
    LIVEKIT_SDK_API_KEY,
    LIVEKIT_SDK_API_SECRET,
    LIVEKIT_SDK_URL,
    MONGO_URI,
)

from .associated_number_service import AssociatedNumberService
from .livekit_sip_bridge import LiveKitSIPBridge, SIPBridgeAPI
from .notification_service import NotificationService
from .organization_service import OrganizationService
from .token_storage_service import token_storage_service
from .user_service import UserService, user_service

organization_service = OrganizationService(MONGO_URI, DB_NAME)
associated_number_service = AssociatedNumberService(MONGO_URI, DB_NAME)
livekit_sip_bridge = LiveKitSIPBridge(
    livekit_url=LIVEKIT_SDK_URL,
    api_key=LIVEKIT_SDK_API_KEY,
    api_secret=LIVEKIT_SDK_API_SECRET,
)
sip_bridge_api = SIPBridgeAPI(livekit_sip_bridge)


def get_sip_bridge():
    """Shared LiveKit SIP bridge and handler for `app.api.sip` (lazy init of router globals)."""
    return livekit_sip_bridge, sip_bridge_api


# Initialize notification service
notification__service = NotificationService()
