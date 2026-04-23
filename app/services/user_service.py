import asyncio
import secrets
import json
from typing import Dict, List, Optional, Any
from unittest import result
from datetime import datetime, timedelta
from fastapi import HTTPException
from pymongo.collection import Collection
from bson import ObjectId
import hashlib
import requests
import httpx
import logging
from redis import Redis
from passlib.context import CryptContext
from .mongodb import BaseMongoClient
from app.config import (
    CLERK_SECRET_KEY,
    REDIS_URL,
    REDIS_USER_CACHE_PREFIX,
    REDIS_USER_CACHE_TTL_SECONDS,
)
from app.utils.performance_monitor import monitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

from app.utils.mongodb_org import org_filter as _org_filter


class UserService(BaseMongoClient):
    def __init__(self, mongo_uri: str, db_name: str):
        super().__init__(mongo_uri, db_name)
        self.collection = self.get_collection("users")
        self.redis: Optional[Redis] = None
        try:
            self.redis = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as e:
            logger.warning({"event": "user_cache_redis_init_failed", "error": str(e)})

    def _cache_org_scope(self, organization_id: Optional[str]) -> str:
        return str(organization_id).strip() if organization_id and str(organization_id).strip() else "global"

    def _cache_user_id_key(self, user_id: str, organization_id: Optional[str] = None) -> str:
        return f"{REDIS_USER_CACHE_PREFIX}:id:{self._cache_org_scope(organization_id)}:{str(user_id).strip()}"

    def _cache_user_email_key(self, email: str, organization_id: Optional[str] = None) -> str:
        return f"{REDIS_USER_CACHE_PREFIX}:email:{self._cache_org_scope(organization_id)}:{str(email).strip().lower()}"

    def _to_cache_value(self, value: Any) -> Any:
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, list):
            return [self._to_cache_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._to_cache_value(v) for k, v in value.items()}
        return value

    def _from_cache_value(self, value: Any, key: Optional[str] = None) -> Any:
        if isinstance(value, list):
            return [self._from_cache_value(v) for v in value]
        if isinstance(value, dict):
            restored = {}
            for k, v in value.items():
                restored[k] = self._from_cache_value(v, key=k)
            return restored
        if isinstance(value, str):
            if key and key.endswith("_id"):
                try:
                    return ObjectId(value)
                except Exception:
                    return value
            if key in ("_id", "organization_id"):
                try:
                    return ObjectId(value)
                except Exception:
                    return value
            if key and (key.endswith("_at") or key in ("created_at", "updated_at", "expires_at")):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except Exception:
                    return value
        return value

    def _cache_get_json(self, key: str) -> Optional[Any]:
        if not self.redis:
            return None
        try:
            raw = self.redis.get(key)
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            logger.warning({"event": "user_cache_get_failed", "key": key, "error": str(e)})
            return None

    def _cache_set_json(self, key: str, value: Any, ttl: int = REDIS_USER_CACHE_TTL_SECONDS) -> None:
        if not self.redis:
            return
        try:
            self.redis.setex(key, max(1, int(ttl)), json.dumps(self._to_cache_value(value)))
        except Exception as e:
            logger.warning({"event": "user_cache_set_failed", "key": key, "error": str(e)})

    def _cache_delete(self, key: str) -> None:
        if not self.redis:
            return
        try:
            self.redis.delete(key)
        except Exception as e:
            logger.warning({"event": "user_cache_delete_failed", "key": key, "error": str(e)})

    def _invalidate_user_cache(self, user_id: Optional[str] = None, email: Optional[str] = None, organization_id: Optional[str] = None) -> None:
        if not self.redis:
            return

        keys = []
        if user_id:
            uid = str(user_id).strip()
            if organization_id and str(organization_id).strip():
                keys.append(self._cache_user_id_key(uid, organization_id))
            else:
                keys.append(self._cache_user_id_key(uid, None))
                keys.append(f"{REDIS_USER_CACHE_PREFIX}:id:*:{uid}")

        if email:
            normalized_email = str(email).strip().lower()
            if organization_id and str(organization_id).strip():
                keys.append(self._cache_user_email_key(normalized_email, organization_id))
            else:
                keys.append(self._cache_user_email_key(normalized_email, None))
                keys.append(f"{REDIS_USER_CACHE_PREFIX}:email:*:{normalized_email}")

        keys.extend(
            [
                f"{REDIS_USER_CACHE_PREFIX}:list:*",
                f"{REDIS_USER_CACHE_PREFIX}:count_by_role:*",
            ]
        )

        try:
            for key in keys:
                if "*" in key:
                    for match in self.redis.scan_iter(match=key, count=100):
                        self.redis.delete(match)
                else:
                    self.redis.delete(key)
        except Exception as e:
            logger.warning({"event": "user_cache_invalidate_failed", "error": str(e)})

    def list_users(self, filters: Optional[dict] = None, skip: int = 0, limit: int = 10, organization_id: Optional[str] = None):
        if filters is None:
            filters = {}
        query_filters = dict(filters)
        if organization_id:
            query_filters.update(_org_filter(organization_id))

        cache_key = f"{REDIS_USER_CACHE_PREFIX}:list:{self._cache_org_scope(organization_id)}:{hashlib.sha256(json.dumps(self._to_cache_value(query_filters), sort_keys=True).encode('utf-8')).hexdigest()}:{skip}:{limit}"
        cached = self._cache_get_json(cache_key)
        if cached:
            users = [self._from_cache_value(u) for u in cached.get("users", [])]
            total = int(cached.get("total", 0))
            return users, total

        total = self.collection.count_documents(query_filters)
        users = list(self.collection.find(query_filters).skip(skip).limit(limit))
        self._cache_set_json(cache_key, {"users": users, "total": total}, ttl=60)
        return users, total

    def count_users_by_role(self, organization_id: Optional[str] = None) -> Dict[str, Any]:
        """
        User counts for dashboard: total and breakdown by role (scoped by organization when provided).
        Roles are normalized to lowercase; missing role is counted as customer.
        """
        cache_key = f"{REDIS_USER_CACHE_PREFIX}:count_by_role:{self._cache_org_scope(organization_id)}"
        cached = self._cache_get_json(cache_key)
        if cached:
            return cached

        match: Dict[str, Any] = {}
        if organization_id:
            match.update(_org_filter(organization_id))
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": {"$toLower": {"$ifNull": ["$role", "customer"]}},
                    "count": {"$sum": 1},
                }
            },
        ]
        by_role_raw = list(self.collection.aggregate(pipeline))
        by_role_map = {row["_id"]: row["count"] for row in by_role_raw if row.get("_id")}
        total = sum(by_role_map.values())
        order = ("customer", "driver", "admin")
        by_role = [{"role": r, "count": int(by_role_map.get(r, 0))} for r in order]
        for role, cnt in sorted(by_role_map.items()):
            if role not in order:
                by_role.append({"role": role, "count": int(cnt)})
        payload = {"total": int(total), "by_role": by_role}
        self._cache_set_json(cache_key, payload, ttl=60)
        return payload
    # ...existing code...

    @monitor(name="user_service.create_user")
    async def create_user(self, email: str, phone_number: str, first_name: str, last_name: str,  role: str, password: str, caller_id: Optional[str] = None, organization_id: Optional[str] = None) -> dict:
        if not organization_id:
            from app.services import organization_service
            default_org = organization_service.get_or_create_default_organization()
            organization_id = str(default_org["_id"])
        user = {
            "email": email,
            "phone_number": phone_number,
            "first_name": first_name,
            "last_name": last_name,
            "role": role,
            "password": password,
            "hashed_password": self.get_password_hash(password=password, algorithm="sha256"),
            "organization_id": ObjectId(organization_id) if isinstance(organization_id, str) and len(organization_id) == 24 else organization_id,
        }
        if caller_id:
            user["caller_id"] = caller_id
        new_user = self.collection.insert_one(user)
        if not new_user:
            raise HTTPException(status_code=400, detail="User creation failed")
        user["_id"] = new_user.inserted_id
        #asyncio.run(self.create_clerk_user(user))
        await self.create_clerk_user(user)
        self._invalidate_user_cache(user_id=str(user["_id"]), email=email, organization_id=organization_id)
        logging.info(f"User created with ID: {user['_id']}")
        logging.info(f"Clerk user created with email: {email} and username: {first_name}")
        # Optionally, you can return the user object or just the ID
        return user

    def get_user_by_clerk_id(self, clerk_id: str) -> Optional[dict]:
        user = self.collection.find_one({"clerk_id": clerk_id})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    def get_user_by_id(self, user_id: str, organization_id: Optional[str] = None) -> Optional[dict]:
        cache_key = self._cache_user_id_key(user_id, organization_id)
        cached = self._cache_get_json(cache_key)
        if cached:
            return self._from_cache_value(cached)

        query = {"_id": ObjectId(user_id)}
        if organization_id:
            query.update(_org_filter(organization_id))
        user = self.collection.find_one(query)
        if user:
            self._cache_set_json(cache_key, user)
            email = user.get("email")
            if email:
                self._cache_set_json(self._cache_user_email_key(email, organization_id), user)
        return user

    def get_user_by_email(self, email: str, organization_id: Optional[str] = None) -> Optional[dict]:
        cache_key = self._cache_user_email_key(email, organization_id)
        cached = self._cache_get_json(cache_key)
        if cached:
            return self._from_cache_value(cached)

        query = {"email": email}
        if organization_id:
            query.update(_org_filter(organization_id))
        user = self.collection.find_one(query)
        if user:
            self._cache_set_json(cache_key, user)
            self._cache_set_json(self._cache_user_id_key(str(user.get("_id")), organization_id), user)
        return user

    def get_users_by_ids(self, user_ids: List[str], organization_id: Optional[str] = None) -> Dict[str, dict]:
        """
        Fetch multiple users by ID in a single query. Returns a dict mapping user_id (str) to user document.
        Invalid IDs are skipped; missing users are omitted from the result.
        When organization_id is provided, results are filtered to that organization.
        """
        if not user_ids:
            return {}
        object_ids = []
        id_to_str = {}
        for uid in user_ids:
            try:
                oid = ObjectId(uid)
                object_ids.append(oid)
                id_to_str[oid] = uid
            except (TypeError, ValueError):
                continue
        if not object_ids:
            return {}
        query = {"_id": {"$in": object_ids}}
        if organization_id:
            query.update(_org_filter(organization_id))
        cursor = self.collection.find(query)
        result = {}
        for user in cursor:
            uid = id_to_str.get(user["_id"])
            if uid is not None:
                result[uid] = user
        return result

    @monitor(name="user_service.update_user")
    def update_user(self, user_id: str, update_data: dict) -> Optional[dict]:
        existing_user = self.get_user_by_id(user_id)
        self.collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})
        updated_user = self.get_user_by_id(user_id)
        self._invalidate_user_cache(
            user_id=user_id,
            email=(existing_user or {}).get("email") or (updated_user or {}).get("email"),
            organization_id=str((existing_user or {}).get("organization_id") or (updated_user or {}).get("organization_id") or ""),
        )
        return updated_user

    @monitor(name="user_service.delete_user")
    def delete_user(self, user_id: str) -> bool:
        existing_user = self.get_user_by_id(user_id)
        result = self.collection.delete_one({"_id": ObjectId(user_id)})
        is_deleted = result.deleted_count == 1
        if is_deleted:
            if existing_user and "clerk_id" in existing_user:
                asyncio.run(self.delete_clerk_user(existing_user["clerk_id"]))
            self._invalidate_user_cache(
                user_id=user_id,
                email=(existing_user or {}).get("email"),
                organization_id=str((existing_user or {}).get("organization_id") or ""),
            )
        else:
            raise HTTPException(status_code=404, detail="User not found")
        return is_deleted
    

    def get_password_hash(self, password):
        """Hash a password."""
        return pwd_context.hash(password)
   

    def get_password_hash(self, password: str, algorithm: str = "sha256") -> str:
        """
        Hash a password using SHA-2 algorithms.
        
        :param password: The plain-text password to hash.
        :param algorithm: SHA-2 variant ('sha224', 'sha256', 'sha384', 'sha512').
        :return: Hexadecimal string of the hashed password.
        """
        # Get the hashing function
        if algorithm not in hashlib.algorithms_guaranteed:
            raise ValueError(f"Unsupported algorithm: {algorithm}")
        
        hasher = hashlib.new(algorithm)
        hasher.update(password.encode("utf-8"))
        return hasher.hexdigest()


    async def create_clerk_user(self, user: dict) -> dict:
        async with httpx.AsyncClient() as client:
            username = user["email"].split("@")[0]
            username = username.replace(".", "_")
            logger.info(f"Creating Clerk user with username: {username} and email: {user['email']}")
            response = await client.post(
                "https://api.clerk.dev/v1/users",
                headers={
                    "Authorization": f"Bearer {CLERK_SECRET_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "first_name": user["first_name"],
                    "last_name": user["last_name"],
                    "email_address": [user["email"]],
                    #"phone_number": [user["phone_number"]],
                    "username": username,  # Using email prefix as username
                    "external_id": str(user["_id"]),
                    "password": user["password"]  # Assuming password is used as username
                    #"password_hasher": user["hashed_password"]
                }
            )
            response_data = response.json()
            logger.info(f"Clerk user creation response: {response_data}")
            if response.status_code == 200:
                clerk_id = response_data.get("id")
                logger.info(f"Clerk user created with ID: {clerk_id}")
                user["clerk_id"] = clerk_id
                self.collection.update_one({"_id": ObjectId(user["_id"])}, {"$set": {"clerk_id": clerk_id}})
            return response_data
    
    def delete_clerk_user(self, clerk_id: str) -> bool:
        response = requests.delete(
            f"https://api.clerk.dev/v1/users/{clerk_id}",
            headers={
                "Authorization": f"Bearer {CLERK_SECRET_KEY}",
                "Content-Type": "application/json"
            }
        )
        return response.status_code == 200
    
    def generate_reset_token(self) -> str:
        """Generate a secure random token for password reset."""
        return secrets.token_urlsafe(32)
    
    @monitor(name="user_service.create_password_reset_token")
    async def create_password_reset_token(self, user_id: str, email: str, expires_hours: int = 1) -> str:
        """
        Create a password reset token for a user.
        
        Args:
            user_id: User ID
            email: User email
            expires_hours: Token expiration time in hours (default: 1)
        
        Returns:
            Reset token string
        """
        reset_token = self.generate_reset_token()
        expires_at = datetime.utcnow() + timedelta(hours=expires_hours)
        
        # Get password_reset_tokens collection
        reset_tokens_collection = self.get_collection("password_reset_tokens")
        
        # Deactivate any existing reset tokens for this user
        reset_tokens_collection.update_many(
            {"user_id": user_id, "is_active": True},
            {"$set": {"is_active": False, "deactivated_at": datetime.utcnow()}}
        )
        
        # Create new reset token
        token_doc = {
            "user_id": user_id,
            "email": email,
            "reset_token": reset_token,
            "created_at": datetime.utcnow(),
            "expires_at": expires_at,
            "is_active": True,
            "used_at": None
        }
        
        reset_tokens_collection.insert_one(token_doc)
        logger.info({"event": "password_reset_token_created", "user_id": user_id, "email": email})
        
        return reset_token
    
    @monitor(name="user_service.verify_password_reset_token")
    async def verify_password_reset_token(self, reset_token: str) -> Optional[dict]:
        """
        Verify a password reset token and return user info if valid.
        
        Args:
            reset_token: Reset token to verify
        
        Returns:
            Dictionary with user_id and email if token is valid, None otherwise
        """
        reset_tokens_collection = self.get_collection("password_reset_tokens")
        
        token_doc = reset_tokens_collection.find_one({
            "reset_token": reset_token,
            "is_active": True
        })
        
        if not token_doc:
            logger.warning({"event": "password_reset_token_not_found", "token": reset_token[:20] + "..."})
            return None
        
        # Check if token has expired
        expires_at = token_doc.get("expires_at")
        if expires_at:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            if expires_at < datetime.utcnow():
                logger.warning({"event": "password_reset_token_expired", "user_id": token_doc.get("user_id")})
                # Deactivate expired token
                reset_tokens_collection.update_one(
                    {"_id": token_doc["_id"]},
                    {"$set": {"is_active": False, "deactivated_at": datetime.utcnow()}}
                )
                return None
        
        # Check if token has already been used
        if token_doc.get("used_at"):
            logger.warning({"event": "password_reset_token_already_used", "user_id": token_doc.get("user_id")})
            return None
        
        return {
            "user_id": token_doc.get("user_id"),
            "email": token_doc.get("email")
        }
    
    @monitor(name="user_service.create_password_reset_otp")
    async def create_password_reset_otp(self, user_id: str, email: str, expires_minutes: int = 15) -> str:
        """
        Create a 6-digit OTP for password reset (mobile flow).

        Args:
            user_id: User ID
            email: User email
            expires_minutes: OTP expiration in minutes (default: 15)

        Returns:
            6-digit OTP string
        """
        otp = "".join(str(secrets.randbelow(10)) for _ in range(6))
        expires_at = datetime.utcnow() + timedelta(minutes=expires_minutes)

        otp_collection = self.get_collection("password_reset_otps")

        # Deactivate any existing OTPs for this user
        otp_collection.update_many(
            {"user_id": user_id, "is_active": True},
            {"$set": {"is_active": False, "deactivated_at": datetime.utcnow()}}
        )

        otp_doc = {
            "user_id": user_id,
            "email": email,
            "otp": otp,
            "created_at": datetime.utcnow(),
            "expires_at": expires_at,
            "is_active": True,
            "used_at": None
        }
        otp_collection.insert_one(otp_doc)
        logger.info({"event": "password_reset_otp_created", "user_id": user_id, "email": email})
        return otp

    @monitor(name="user_service.verify_password_reset_otp")
    async def verify_password_reset_otp(self, email: str, otp: str) -> Optional[dict]:
        """
        Verify OTP for password reset. Returns user info if valid.

        Args:
            email: User email
            otp: 6-digit OTP

        Returns:
            Dictionary with user_id and email if valid, None otherwise
        """
        otp_collection = self.get_collection("password_reset_otps")

        token_doc = otp_collection.find_one({
            "email": email,
            "otp": otp,
            "is_active": True
        })

        if not token_doc:
            return None

        expires_at = token_doc.get("expires_at")
        if expires_at:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires_at < datetime.utcnow():
                otp_collection.update_one(
                    {"_id": token_doc["_id"]},
                    {"$set": {"is_active": False, "deactivated_at": datetime.utcnow()}}
                )
                return None

        if token_doc.get("used_at"):
            return None

        return {
            "user_id": token_doc.get("user_id"),
            "email": token_doc.get("email")
        }

    @monitor(name="user_service.mark_otp_as_used")
    async def mark_otp_as_used(self, email: str, otp: str) -> bool:
        """Mark OTP as used after successful password reset."""
        otp_collection = self.get_collection("password_reset_otps")
        result = otp_collection.update_one(
            {"email": email, "otp": otp},
            {"$set": {"is_active": False, "used_at": datetime.utcnow()}}
        )
        return result.modified_count > 0

    @monitor(name="user_service.mark_reset_token_as_used")
    async def mark_reset_token_as_used(self, reset_token: str) -> bool:
        """
        Mark a password reset token as used.
        
        Args:
            reset_token: Reset token to mark as used
        
        Returns:
            True if token was marked as used, False otherwise
        """
        reset_tokens_collection = self.get_collection("password_reset_tokens")
        
        result = reset_tokens_collection.update_one(
            {"reset_token": reset_token},
            {
                "$set": {
                    "is_active": False,
                    "used_at": datetime.utcnow()
                }
            }
        )
        
        return result.modified_count > 0
    
    @monitor(name="user_service.update_user_password")
    async def update_user_password(self, user_id: str, new_password: str) -> bool:
        """
        Update a user's password.
        
        Args:
            user_id: User ID
            new_password: New password - either plain text or SHA-256 hex digest (64 chars)
        
        Returns:
            True if password was updated successfully
        """
        try:
            # If new_password is already SHA-256 hex digest (64 chars), use it directly
            # Otherwise hash the plain password
            from string import hexdigits
            HEX_CHARS = set(hexdigits.lower())
            stripped = (new_password or "").strip()
            is_sha256_hex = len(stripped) == 64 and all(c in HEX_CHARS for c in stripped.lower())
            if is_sha256_hex:
                hashed_password = stripped.lower()
            else:
                hashed_password = self.get_password_hash(password=new_password, algorithm="sha256")
            
            self.collection.update_one(
                {"_id": ObjectId(user_id)},
                {
                    "$set": {
                        "hashed_password": hashed_password,
                        "password": hashed_password,  # Login uses same format
                        "password_updated_at": datetime.utcnow()
                    }
                }
            )
            user_after_update = self.get_user_by_id(user_id)
            self._invalidate_user_cache(
                user_id=user_id,
                email=(user_after_update or {}).get("email"),
                organization_id=str((user_after_update or {}).get("organization_id") or ""),
            )
            
            logger.info({"event": "user_password_updated", "user_id": user_id})
            return True
        except Exception as e:
            logger.error({"event": "password_update_failed", "user_id": user_id, "error": str(e)})
            raise HTTPException(status_code=500, detail=f"Failed to update password: {str(e)}")