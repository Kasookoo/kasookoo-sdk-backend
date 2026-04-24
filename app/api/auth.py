from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import uuid

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Header, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwk, jwt
import logging
from pymongo import MongoClient
from pydantic import BaseModel, Field
from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    DB_NAME,
    JWT_PRIVATE_KEY,
    JWT_PUBLIC_KEY,
    JWT_KID,
    MONGO_URI,
    SDK_PUBLIC_ALLOWED_SCOPES,
    SDK_SESSION_DURATION_SECONDS,
    SDK_SIGNING_SECRET,
    SDK_TOKEN_AUDIENCE,
    SDK_TOKEN_ISSUER,
    SDK_TOKEN_LEEWAY_SECONDS,
)

load_dotenv(dotenv_path=".env.local")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = APIRouter()
security = HTTPBearer(auto_error=False)
SESSION_STORE: Dict[str, Dict[str, Any]] = {}
SESSION_COLLECTION = None

if MONGO_URI and DB_NAME:
    try:
        _mongo_client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
        SESSION_COLLECTION = _mongo_client[DB_NAME]["sdk_auth_sessions"]
        SESSION_COLLECTION.create_index("sid", unique=True)
        SESSION_COLLECTION.create_index("sub")
        SESSION_COLLECTION.create_index("active")
    except Exception as ex:
        logger.warning("Failed to initialize MongoDB session storage, falling back to in-memory: %s", ex)
        SESSION_COLLECTION = None


def _get_bearer_token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing token")
    return credentials.credentials


async def sdk_token_scheme(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """
    Compatibility dependency used by existing routes.
    Returns the bearer token string without using password-login OAuth flow.
    """
    return _get_bearer_token(credentials)


def _get_signing_key() -> str:
    if ALGORITHM == "RS256":
        if not JWT_PRIVATE_KEY:
            raise HTTPException(status_code=500, detail="JWT_PRIVATE_KEY is not configured")
        return JWT_PRIVATE_KEY
    if not SDK_SIGNING_SECRET:
        raise HTTPException(status_code=500, detail="SDK_SIGNING_SECRET is not configured")
    return SDK_SIGNING_SECRET


def _get_verify_key() -> str:
    if ALGORITHM == "RS256":
        if not JWT_PUBLIC_KEY:
            raise HTTPException(status_code=500, detail="JWT_PUBLIC_KEY is not configured")
        return JWT_PUBLIC_KEY
    if not SDK_SIGNING_SECRET:
        raise HTTPException(status_code=500, detail="SDK_SIGNING_SECRET is not configured")
    return SDK_SIGNING_SECRET


def _ensure_session_active(payload: Dict[str, Any]) -> None:
    sid = payload.get("sid")
    if not sid:
        return
    session = _get_session(str(sid))
    if not session or not session.get("active"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked or not found")
    if str(session.get("sub")) != str(payload.get("sub")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session subject mismatch")


def _get_session(sid: str) -> Optional[Dict[str, Any]]:
    if SESSION_COLLECTION is not None:
        return SESSION_COLLECTION.find_one({"sid": sid})
    return SESSION_STORE.get(sid)


def _decode_sdk_token(token: str) -> Dict[str, Any]:
    try:
        decode_kwargs: Dict[str, Any] = {
            "algorithms": [ALGORITHM],
            "audience": SDK_TOKEN_AUDIENCE if SDK_TOKEN_AUDIENCE else None,
            "issuer": SDK_TOKEN_ISSUER if SDK_TOKEN_ISSUER else None,
            "options": {"require_exp": True, "require_iat": True},
            "leeway": SDK_TOKEN_LEEWAY_SECONDS,
        }
        try:
            payload = jwt.decode(token, _get_verify_key(), **decode_kwargs)
        except TypeError:
            # Backward-compat for jose versions that don't support `leeway` kwarg.
            decode_kwargs.pop("leeway", None)
            payload = jwt.decode(token, _get_verify_key(), **decode_kwargs)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid SDK token")

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid SDK token subject")
    _ensure_session_active(payload)
    return payload


class CreateClientSessionRequest(BaseModel):
    sub: str = Field(..., description="Subject for SDK session token (user/device/guest id)")
    organization_id: Optional[str] = Field(default=None, description="Organization/tenant id")
    email: Optional[str] = None
    scopes: List[str] = Field(default_factory=list, description="Requested scopes from allowed public scope set")
    ttl_seconds: int = Field(default=SDK_SESSION_DURATION_SECONDS, ge=30, le=600, description="Token lifetime in seconds")
    extra_claims: Dict[str, Any] = Field(default_factory=dict, description="Optional extra claims (non-security)")


class UserResponse(BaseModel):
    id: str
    email: str
    phone_number: Optional[str] = None
    clerk_id: Optional[str] = None
    first_name: str
    last_name: str
    role: str
    organization_id: Optional[str] = None


def _create_sdk_token(payload: Dict[str, Any], ttl_seconds: int) -> str:
    now = datetime.now(timezone.utc)
    to_encode = dict(payload)
    to_encode["iat"] = int(now.timestamp())
    to_encode["exp"] = int((now + timedelta(seconds=ttl_seconds)).timestamp())
    if SDK_TOKEN_AUDIENCE:
        to_encode["aud"] = SDK_TOKEN_AUDIENCE
    if SDK_TOKEN_ISSUER:
        to_encode["iss"] = SDK_TOKEN_ISSUER
    headers = {"kid": JWT_KID} if ALGORITHM == "RS256" else None
    return jwt.encode(to_encode, _get_signing_key(), algorithm=ALGORITHM, headers=headers)


def _create_or_update_session(sub: str, organization_id: Optional[str], session_id: Optional[str] = None) -> str:
    sid = session_id or f"sess_{uuid.uuid4().hex}"
    session_doc = {
        "sid": sid,
        "sub": str(sub),
        "organization_id": str(organization_id) if organization_id else None,
        "active": True,
        "updated_at": int(datetime.now(timezone.utc).timestamp()),
    }
    if SESSION_COLLECTION is not None:
        SESSION_COLLECTION.update_one({"sid": sid}, {"$set": session_doc}, upsert=True)
    else:
        SESSION_STORE[sid] = session_doc
    return sid


def _validate_public_scopes(requested_scopes: List[str]) -> List[str]:
    requested = [str(scope).strip() for scope in requested_scopes if str(scope).strip()]
    allowed = set(SDK_PUBLIC_ALLOWED_SCOPES)
    disallowed = [scope for scope in requested if scope not in allowed]
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requested scopes are not allowed: {', '.join(disallowed)}",
        )
    return requested


def create_access_token(data: Dict[str, Any]) -> str:
    """Short-lived SDK-signed JWT (same mechanism as client-sessions)."""
    return _create_sdk_token(data, ttl_seconds=ACCESS_TOKEN_EXPIRE_MINUTES * 60)


oauth2_scheme = sdk_token_scheme


async def normal_authenticate_token(token: str) -> Tuple[str, str, Dict[str, Any]]:
    """Decode SDK access token; returns (sub, email, payload)."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    payload = _decode_sdk_token(token)
    subject = str(payload.get("sub"))
    email = str(payload.get("email") or payload.get("sub") or subject)
    return subject, email, payload


async def authenticate_token(token: str) -> str:
    """Return authenticated principal id (`sub` from SDK token)."""
    subject, _, _ = await normal_authenticate_token(token)
    return subject


def _extract_scopes(payload: Dict[str, Any]) -> List[str]:
    scopes = payload.get("scopes")
    if isinstance(scopes, list):
        return [str(item).strip() for item in scopes if str(item).strip()]
    if isinstance(scopes, str):
        return [item for item in scopes.split(" ") if item]
    scope = payload.get("scope")
    if isinstance(scope, str):
        return [item for item in scope.split(" ") if item]
    return []


async def get_sdk_principal(token: str = Depends(sdk_token_scheme)) -> Dict[str, Any]:
    payload = _decode_sdk_token(token)
    payload["resolved_scopes"] = _extract_scopes(payload)
    return payload


def require_scopes(required_scopes: List[str]):
    async def _enforce_scopes(principal: Dict[str, Any] = Depends(get_sdk_principal)) -> Dict[str, Any]:
        available = set(principal.get("resolved_scopes", []))
        missing = [scope for scope in required_scopes if scope not in available]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scopes: {', '.join(missing)}",
            )
        return principal

    return _enforce_scopes


@router.get("/api/v1/sdk/auth/introspect")
async def sdk_auth_introspect(principal: Dict[str, Any] = Depends(get_sdk_principal)) -> Dict[str, Any]:
    """
    Debug endpoint to inspect validated SDK token claims.
    """
    return {
        "active": True,
        "sub": principal.get("sub"),
        "sid": principal.get("sid"),
        "iss": principal.get("iss"),
        "aud": principal.get("aud"),
        "organization_id": principal.get("organization_id") or principal.get("org_id"),
        "scopes": principal.get("resolved_scopes", []),
        "exp": principal.get("exp"),
        "iat": principal.get("iat"),
        "jti": principal.get("jti"),
    }


@router.get("/.well-known/jwks.json")
async def sdk_jwks() -> Dict[str, Any]:
    if ALGORITHM != "RS256":
        return {"keys": []}
    if not JWT_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="JWT public key is not configured")

    key = jwk.construct(JWT_PUBLIC_KEY, algorithm=ALGORITHM).to_dict()
    key.update({"use": "sig", "alg": "RS256", "kid": JWT_KID})
    return {"keys": [key]}


def get_organization_id(
    x_organization_id: str = Header(default=None, alias="X-Organization-Id"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    if x_organization_id and str(x_organization_id).strip():
        return str(x_organization_id).strip()
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing token")
    token = _get_bearer_token(credentials)
    payload = _decode_sdk_token(token)
    org_id = payload.get("organization_id") or payload.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organization_id")
    return str(org_id)


@router.post("/api/v1/sdk/auth/client-sessions")
async def create_client_session(
    request: CreateClientSessionRequest,
) -> Dict[str, Any]:
    """
    First-party endpoint for frontend SDK usage.
    Issues a short-lived token and creates a Mongo-backed session directly in Kasookoo backend.
    """
    scopes = _validate_public_scopes(request.scopes)

    claims: Dict[str, Any] = {
        "sub": request.sub,
        "scopes": scopes,
    }
    if request.email:
        claims["email"] = request.email
    if request.organization_id:
        claims["organization_id"] = request.organization_id

    session_id = _create_or_update_session(request.sub, request.organization_id, None)
    claims["sid"] = session_id
    claims["jti"] = str(uuid.uuid4())
    claims.update(request.extra_claims or {})

    token = _create_sdk_token(claims, ttl_seconds=request.ttl_seconds)
    return {
        "token": token,
        "token_type": "Bearer",
        "session_id": session_id,
        "expires_in": request.ttl_seconds,
        "audience": SDK_TOKEN_AUDIENCE,
        "issuer": SDK_TOKEN_ISSUER,
        "allowed_scopes": SDK_PUBLIC_ALLOWED_SCOPES,
    }


@router.post("/api/v1/sdk/auth/sessions/{session_id}/tokens")
async def refresh_sdk_session_token(
    session_id: str,
    principal: Dict[str, Any] = Depends(get_sdk_principal),
) -> Dict[str, Any]:
    sid = str(principal.get("sid") or "")
    if not sid or sid != session_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Session mismatch")

    session = SESSION_STORE.get(session_id)
    if SESSION_COLLECTION is not None:
        session = SESSION_COLLECTION.find_one({"sid": session_id})
    if not session or not session.get("active"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked or not found")

    claims: Dict[str, Any] = {
        "sub": principal.get("sub"),
        "sid": session_id,
        "scopes": principal.get("resolved_scopes", []),
        "jti": str(uuid.uuid4()),
    }
    org_id = principal.get("organization_id") or principal.get("org_id")
    if org_id:
        claims["organization_id"] = org_id
    if principal.get("email"):
        claims["email"] = principal.get("email")

    token = _create_sdk_token(claims, ttl_seconds=SDK_SESSION_DURATION_SECONDS)
    return {
        "token": token,
        "token_type": "Bearer",
        "session_id": session_id,
        "expires_in": SDK_SESSION_DURATION_SECONDS,
    }


@router.delete("/api/v1/sdk/auth/sessions/{session_id}")
async def revoke_sdk_session(
    session_id: str,
    principal: Dict[str, Any] = Depends(get_sdk_principal),
) -> Dict[str, Any]:
    sid = str(principal.get("sid") or "")
    if not sid or sid != session_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot revoke another session")

    session = SESSION_STORE.get(session_id)
    if SESSION_COLLECTION is not None:
        SESSION_COLLECTION.update_one(
            {"sid": session_id},
            {"$set": {"active": False, "updated_at": int(datetime.now(timezone.utc).timestamp())}},
        )
    elif session:
        session["active"] = False
        session["updated_at"] = int(datetime.now(timezone.utc).timestamp())

    return {"message": "Session revoked", "session_id": session_id}
