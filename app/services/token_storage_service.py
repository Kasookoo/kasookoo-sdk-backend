#!/usr/bin/env python3
"""
Token Storage Service - Manages storage and retrieval of user tokens
"""

import os
import logging
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from bson import ObjectId
import motor.motor_asyncio
from redis.asyncio import Redis

from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    DB_NAME,
    MONGO_URI,
    REDIS_URL,
    REDIS_SESSION_PREFIX,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB configuration — use same DB as the rest of the app (user_service, auth, etc.).
# SDK_DATABASE_NAME overrides DB_NAME only if you intentionally use a separate DB.
MONGODB_URL = MONGO_URI
DATABASE_NAME = os.getenv("SDK_DATABASE_NAME") or DB_NAME

# Clean connection string - remove TLS parameters from URL for mongodb+srv://
clean_url = MONGODB_URL
if "mongodb+srv://" in clean_url:
    # Parse URL and remove TLS parameters from query string
    try:
        parsed = urlparse(clean_url)
        query_params = parse_qs(parsed.query)
        # Remove TLS-related parameters
        query_params.pop('tls', None)
        query_params.pop('tlsAllowInvalidCertificates', None)
        # Rebuild query string
        new_query = urlencode(query_params, doseq=True)
        # Rebuild URL
        clean_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))
    except Exception as e:
        logger.warning(f"Failed to parse MongoDB URL, using original: {e}")
        # Fallback: simple regex removal
        clean_url = re.sub(r'[&?]tls=[^&]*', '', clean_url)
        clean_url = re.sub(r'[&?]tlsAllowInvalidCertificates=[^&]*', '', clean_url)
        clean_url = clean_url.rstrip('&?')

# Initialize MongoDB client with proper TLS configuration
if "mongodb+srv://" in clean_url:
    # mongodb+srv:// automatically uses TLS
    client = motor.motor_asyncio.AsyncIOMotorClient(
        clean_url,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000
    )
elif "tls=true" in MONGODB_URL.lower() or "ssl=true" in MONGODB_URL.lower():
    # Explicit TLS for mongodb:// connections
    client = motor.motor_asyncio.AsyncIOMotorClient(
        clean_url,
        tls=True,
        tlsAllowInvalidCertificates=True,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000
    )
else:
    # Regular connection without TLS
    client = motor.motor_asyncio.AsyncIOMotorClient(
        clean_url,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000
    )
database = client[DATABASE_NAME]

# Collection for storing user tokens
user_tokens_collection = database.user_tokens

# Redis cache client for fast token/session lookups
redis_client: Optional[Redis] = Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


class TokenStorageService:
    """Service for managing user token storage in MongoDB"""
    
    def __init__(self):
        self.collection = user_tokens_collection
        self.redis = redis_client

    def _token_hash(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _cache_key(self, token_type: str, token: str) -> str:
        return f"{REDIS_SESSION_PREFIX}:{token_type}:{self._token_hash(token)}"

    def _to_iso(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    def _as_utc_naive(self, value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def _serialize_token_doc(self, token_doc: Dict[str, Any]) -> Dict[str, Any]:
        serialized = {}
        for key, value in token_doc.items():
            if key == "_id":
                continue
            serialized[key] = self._to_iso(value)
        return serialized

    def _deserialize_token_doc(self, token_doc: Dict[str, Any]) -> Dict[str, Any]:
        parsed = dict(token_doc)
        for field in (
            "created_at",
            "expires_at",
            "access_token_expires_at",
            "refresh_token_expires_at",
            "last_used_at",
            "deactivated_at",
        ):
            value = parsed.get(field)
            if isinstance(value, str):
                try:
                    parsed[field] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if isinstance(parsed[field], datetime):
                        parsed[field] = self._as_utc_naive(parsed[field])
                except ValueError:
                    # Keep original value if parse fails
                    pass
        return parsed

    async def _cache_token_doc(self, token_doc: Dict[str, Any]) -> None:
        if not self.redis:
            return
        try:
            payload = json.dumps(self._serialize_token_doc(token_doc))
            now = datetime.utcnow()
            access_exp = token_doc.get("access_token_expires_at") or token_doc.get("expires_at")
            refresh_exp = token_doc.get("refresh_token_expires_at") or token_doc.get("expires_at")
            if isinstance(access_exp, datetime):
                access_exp = self._as_utc_naive(access_exp)
            if isinstance(refresh_exp, datetime):
                refresh_exp = self._as_utc_naive(refresh_exp)

            if token_doc.get("access_token") and access_exp:
                access_ttl = max(1, int((access_exp - now).total_seconds()))
                await self.redis.setex(
                    self._cache_key("access", token_doc["access_token"]),
                    access_ttl,
                    payload,
                )

            if token_doc.get("refresh_token") and refresh_exp:
                refresh_ttl = max(1, int((refresh_exp - now).total_seconds()))
                await self.redis.setex(
                    self._cache_key("refresh", token_doc["refresh_token"]),
                    refresh_ttl,
                    payload,
                )
        except Exception as e:
            logger.warning(f"Redis cache set failed: {e}")

    async def _get_cached_token_doc(self, token_type: str, token: str) -> Optional[Dict[str, Any]]:
        if not self.redis:
            return None
        try:
            value = await self.redis.get(self._cache_key(token_type, token))
            if not value:
                return None
            return self._deserialize_token_doc(json.loads(value))
        except Exception as e:
            logger.warning(f"Redis cache get failed: {e}")
            return None

    async def _delete_cached_token(self, token_type: str, token: str) -> None:
        if not self.redis:
            return
        try:
            await self.redis.delete(self._cache_key(token_type, token))
        except Exception as e:
            logger.warning(f"Redis cache delete failed: {e}")
    
    async def save_user_tokens(
        self,
        user_id: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        user_type: Optional[str] = None,
        device_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist a short-lived access token record (refresh_token is ignored; kept for call compatibility)."""
        _ = refresh_token
        try:
            current_time = datetime.utcnow()
            access_token_expires = current_time + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            expires_at = access_token_expires

            token_doc: Dict[str, Any] = {
                "user_id": user_id,
                "access_token": access_token,
                "user_type": user_type,
                "device_info": device_info or {},
                "created_at": current_time,
                "expires_at": expires_at,
                "access_token_expires_at": access_token_expires,
                "is_active": True,
                "last_used_at": current_time,
            }
            
            result = await self.collection.insert_one(token_doc)
            await self._cache_token_doc(token_doc)
            
            logger.info(f"Saved tokens for user {user_id}, token_id: {result.inserted_id}")
            
            return {
                "id": str(result.inserted_id),
                "user_id": user_id,
                "created_at": current_time.isoformat(),
                "expires_at": expires_at.isoformat()
            }
        except Exception as e:
            logger.error(f"Failed to save user tokens: {e}")
            raise
    
    async def get_user_tokens(
        self,
        user_id: str,
        active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get all tokens for a user
        
        Args:
            user_id: User ID
            active_only: Only return active tokens
        
        Returns:
            List of token documents
        """
        try:
            query: Dict[str, Any] = {"user_id": user_id}
            if active_only:
                query["is_active"] = True
                # Also filter out expired tokens
                query["expires_at"] = {"$gt": datetime.utcnow()}
            
            cursor = self.collection.find(query).sort("created_at", -1)
            tokens = []
            async for token_doc in cursor:
                token_doc["id"] = str(token_doc["_id"])
                del token_doc["_id"]
                tokens.append(token_doc)
            
            return tokens
        except Exception as e:
            logger.error(f"Failed to get user tokens: {e}")
            return []
    
    async def get_token_by_access_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """
        Get token document by access token
        
        Args:
            access_token: JWT access token
        
        Returns:
            Token document or None
        """
        try:
            cached = await self._get_cached_token_doc("access", access_token)
            if cached:
                return cached

            token_doc = await self.collection.find_one({
                "access_token": access_token,
                "is_active": True
            })
            
            if token_doc:
                token_doc["id"] = str(token_doc["_id"])
                await self._cache_token_doc(token_doc)
                del token_doc["_id"]
            
            return token_doc
        except Exception as e:
            logger.error(f"Failed to get token by access token: {e}")
            return None
    
    async def deactivate_user_tokens(
        self,
        user_id: str,
        exclude_token_id: Optional[str] = None
    ) -> int:
        """
        Deactivate all tokens for a user (except optionally one token)
        
        Args:
            user_id: User ID
            exclude_token_id: Optional token ID to exclude from deactivation
        
        Returns:
            Number of tokens deactivated
        """
        try:
            tokens_to_deactivate = []
            cursor = self.collection.find(
                {"user_id": user_id, "is_active": True},
                {"access_token": 1, "refresh_token": 1}
            )
            async for token_doc in cursor:
                tokens_to_deactivate.append(token_doc)

            query = {
                "user_id": user_id,
                "is_active": True
            }
            
            if exclude_token_id:
                query["_id"] = {"$ne": ObjectId(exclude_token_id)}
            
            result = await self.collection.update_many(
                query,
                {
                    "$set": {
                        "is_active": False,
                        "deactivated_at": datetime.utcnow()
                    }
                }
            )
            
            logger.info(f"Deactivated {result.modified_count} tokens for user {user_id}")

            for token_doc in tokens_to_deactivate:
                access_token = token_doc.get("access_token")
                refresh_token = token_doc.get("refresh_token")
                if access_token:
                    await self._delete_cached_token("access", access_token)
                if refresh_token:
                    await self._delete_cached_token("refresh", refresh_token)

            return result.modified_count
        except Exception as e:
            logger.error(f"Failed to deactivate user tokens: {e}")
            return 0
    
    async def deactivate_token(self, access_token: str) -> bool:
        """
        Deactivate a specific token
        
        Args:
            access_token: JWT access token
        
        Returns:
            True if token was deactivated, False otherwise
        """
        try:
            token_doc = await self.collection.find_one(
                {"access_token": access_token},
                {"refresh_token": 1}
            )
            result = await self.collection.update_one(
                {"access_token": access_token},
                {
                    "$set": {
                        "is_active": False,
                        "deactivated_at": datetime.utcnow()
                    }
                }
            )
            
            if result.modified_count > 0:
                logger.info(f"Deactivated token: {access_token[:20]}...")
                await self._delete_cached_token("access", access_token)
                if token_doc and token_doc.get("refresh_token"):
                    await self._delete_cached_token("refresh", token_doc["refresh_token"])
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to deactivate token: {e}")
            return False
    
    async def update_token_last_used(self, access_token: str) -> bool:
        """
        Update the last_used_at timestamp for a token
        
        Args:
            access_token: JWT access token
        
        Returns:
            True if updated, False otherwise
        """
        try:
            result = await self.collection.update_one(
                {"access_token": access_token},
                {
                    "$set": {
                        "last_used_at": datetime.utcnow()
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update token last used: {e}")
            return False

    async def deactivate_expired_active_access_tokens(self) -> int:
        """
        Deactivate active token records where access token validity window has passed.

        Returns:
            Number of token records deactivated
        """
        try:
            now = datetime.utcnow()
            result = await self.collection.update_many(
                {
                    "is_active": True,
                    "$or": [
                        # Preferred field for access-token lifetime
                        {"access_token_expires_at": {"$lte": now}}                        
                    ],
                },
                {
                    "$set": {
                        "is_active": False,
                        "deactivated_at": now,
                    }
                },
            )
            deactivated_count = result.modified_count
            if deactivated_count > 0:
                logger.info(
                    f"Deactivated {deactivated_count} expired active access token(s)"
                )
            else:
                logger.info("No expired active access tokens found")
            return deactivated_count
        except Exception as e:
            logger.error(f"Failed to deactivate expired active access tokens: {e}")
            return 0
    
    async def delete_expired_tokens(self) -> int:
        """
        Delete expired tokens from the database
        
        Returns:
            Number of tokens deleted
        """
        try:
            result = await self.collection.delete_many({
                "expires_at": {"$lt": datetime.utcnow()}
            })
            
            logger.info(f"Deleted {result.deleted_count} expired tokens")
            return result.deleted_count
        except Exception as e:
            logger.error(f"Failed to delete expired tokens: {e}")
            return 0
    
    async def get_active_token_count(self, user_id: str) -> int:
        """
        Get count of active tokens for a user
        
        Args:
            user_id: User ID
        
        Returns:
            Number of active tokens
        """
        try:
            count = await self.collection.count_documents({
                "user_id": user_id,
                "is_active": True,
                "expires_at": {"$gt": datetime.utcnow()}
            })
            return count
        except Exception as e:
            logger.error(f"Failed to get active token count: {e}")
            return 0


# Create a singleton instance
token_storage_service = TokenStorageService()

