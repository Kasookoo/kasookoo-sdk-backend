from app.config import MONGO_URI, DB_NAME, LIVEKIT_SDK_URL, LIVEKIT_SDK_API_KEY, LIVEKIT_SDK_API_SECRET

from .notification_service import NotificationService
from .livekit_sip_bridge import LiveKitSIPBridge, SIPBridgeAPI
from .associated_number_service import associated_number_service
from .user_service import user_service
from . import organization_service

# Initialize notification service
notification__service = NotificationService()

# Lazy initialization of SIP bridge to avoid aiohttp session creation at import time
livekit_sip_bridge = None
sip_bridge_api = None

def get_sip_bridge():
    global livekit_sip_bridge, sip_bridge_api
    if livekit_sip_bridge is None:
        livekit_sip_bridge = LiveKitSIPBridge(
            livekit_url=LIVEKIT_SDK_URL,
            api_key=LIVEKIT_SDK_API_KEY,
            api_secret=LIVEKIT_SDK_API_SECRET
        )
        sip_bridge_api = SIPBridgeAPI(livekit_sip_bridge)
    return livekit_sip_bridge, sip_bridge_api