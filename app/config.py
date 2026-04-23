import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

# SDK token auth settings
JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "").replace("\\n", "\n")
JWT_PUBLIC_KEY = os.getenv("JWT_PUBLIC_KEY", "").replace("\\n", "\n")
JWT_KID = os.getenv("JWT_KID", "default")
ALGORITHM = os.getenv("SDK_TOKEN_ALGORITHM", "RS256" if (JWT_PRIVATE_KEY and JWT_PUBLIC_KEY) else "HS256")
SDK_SIGNING_SECRET = os.getenv("SDK_SIGNING_SECRET", "")
SDK_TOKEN_AUDIENCE = os.getenv("SDK_TOKEN_AUDIENCE", "kasookoo-sdk-backend")
SDK_TOKEN_ISSUER = os.getenv("SDK_TOKEN_ISSUER", "")
SDK_TOKEN_LEEWAY_SECONDS = int(os.getenv("SDK_TOKEN_LEEWAY_SECONDS", "15"))
# Short-lived SDK JWTs: frontend mints a new token on each interval (no refresh-token flow).
SDK_SESSION_DURATION_SECONDS = int(os.getenv("SDK_SESSION_DURATION_SECONDS", "60"))
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1"))
# Used by optional Clerk helper token helpers (HS256 with caller-provided secret)
BOT_JWT_ALGORITHM = "HS256"
SDK_PUBLIC_ALLOWED_SCOPES = [
    scope.strip()
    for scope in os.getenv(
        "SDK_PUBLIC_ALLOWED_SCOPES",
        "webrtc:token:create,webrtc:call:read,webrtc:call:end,messaging:token:create,messaging:send,recording:start,recording:read,recording:stop",
    ).split(",")
    if scope.strip()
]

STATIC_API_KEY = os.getenv("STATIC_API_KEY", "17537c5618b70cefe382dc33a39178010e7e24873f3897609d346a85") # @@KasookooTest123456 sha2 encoding

# LiveKit Settings
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
SIP_OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")

LIVEKIT_AFRICA_API_KEY = os.getenv("LIVEKIT_AFRICA_API_KEY")
LIVEKIT_AFRICA_API_SECRET = os.getenv("LIVEKIT_AFRICA_API_SECRET")
LIVEKIT_AFRICA_URL = os.getenv("LIVEKIT_AFRICA_URL")
AFRICA_SIP_OUTBOUND_TRUNK_ID = os.getenv("AFRICA_SIP_OUTBOUND_TRUNK_ID")

# Configuration from environment variables
LIVEKIT_SDK_URL = os.getenv("LIVEKIT_SDK_URL")
LIVEKIT_SDK_API_KEY = os.getenv("LIVEKIT_SDK_API_KEY")
LIVEKIT_SDK_API_SECRET = os.getenv("LIVEKIT_SDK_API_SECRET")
SDK_SIP_OUTBOUND_TRUNK_ID = os.getenv("SDK_SIP_OUTBOUND_TRUNK_ID", "ST_mCzRRndksJkk")
DEFAULT_PHONE_NUMBER = os.getenv("DEFAULT_PHONE_NUMBER", "966966")
CALLER_ID = os.getenv("CALLER_ID")
ANONYMOUS_GUEST_CALL_ADMIN_EMAIL = os.getenv("ANONYMOUS_GUEST_CALL_ADMIN_EMAIL")

# SIP configuration
SIP_TRUNK_NAME = os.getenv("SIP_TRUNK_NAME", "default-trunk")
SIP_INBOUND_ADDRESSES = os.getenv("SIP_INBOUND_ADDRESSES", "192.168.1.100").split(",")
SIP_OUTBOUND_ADDRESS = os.getenv("SIP_OUTBOUND_ADDRESS", "sip.provider.com:5060")
SIP_OUTBOUND_USERNAME = os.getenv("SIP_OUTBOUND_USERNAME", "username")
SIP_OUTBOUND_PASSWORD = os.getenv("SIP_OUTBOUND_PASSWORD", "password")

# MongoDB Settings
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

# Redis Settings (cache layer for user/org lookups)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_USER_CACHE_PREFIX = os.getenv("REDIS_USER_CACHE_PREFIX", "user")
REDIS_USER_CACHE_TTL_SECONDS = int(os.getenv("REDIS_USER_CACHE_TTL_SECONDS", "120"))
REDIS_ORG_CACHE_PREFIX = os.getenv("REDIS_ORG_CACHE_PREFIX", "org")
REDIS_ORG_CACHE_TTL_SECONDS = int(os.getenv("REDIS_ORG_CACHE_TTL_SECONDS", "300"))
REDIS_SESSION_PREFIX = os.getenv("REDIS_SESSION_PREFIX", "session")

# Clerk Settings
CLERK_ISSUER = os.getenv("CLERK_ISSUER", "https://superb-snake-55.clerk.accounts.dev")
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "sk_test_Hs4ttrG4qcRZFaWDFGNxlo089NiEeNsG7Irvxf864k")  # Clerk backend API key
CLERK_JWKS_URL = f"{CLERK_ISSUER}/.well-known/jwks.json"
CLERK_AUDIENCE = os.getenv("CLERK_AUDIENCE", "https://superb-snake-55.clerk.accounts.dev")  # Replace with your Clerk frontend API

API_HOST = os.getenv("API_HOST", "https://webrtc.kasookoo.ai/api/v1/sdk")

SERVER_API_HOST = os.getenv("API_HOST", "https://sdk.kasookoo.ai/api/v1/bot")