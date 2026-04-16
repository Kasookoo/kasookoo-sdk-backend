import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId
from pymongo import MongoClient
from redis import Redis

from app.config import (
    DB_NAME,
    MONGO_URI,
    REDIS_ORG_CACHE_PREFIX,
    REDIS_ORG_CACHE_TTL_SECONDS,
    REDIS_URL,
)


client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client[DB_NAME]
collection = db["organizations"]
logger = logging.getLogger(__name__)
redis_client: Optional[Redis] = None
try:
    redis_client = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
except Exception as e:
    logger.warning({"event": "org_cache_redis_init_failed", "error": str(e)})


def _cache_key_by_id(org_id: str) -> str:
    return f"{REDIS_ORG_CACHE_PREFIX}:id:{str(org_id).strip()}"


def _cache_key_inbound_trunk(trunk_id: str) -> str:
    return f"{REDIS_ORG_CACHE_PREFIX}:inbound_trunk:{str(trunk_id).strip()}"


def _to_cache_value(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_cache_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_cache_value(v) for v in value]
    return value


def _from_cache_value(value: Any, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {k: _from_cache_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_cache_value(v) for v in value]
    if isinstance(value, str):
        if key in ("_id", "organization_id"):
            try:
                return ObjectId(value)
            except Exception:
                return value
        if key and key.endswith("_at"):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
    return value


def _cache_get_json(key: str) -> Optional[Any]:
    if not redis_client:
        return None
    try:
        raw = redis_client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning({"event": "org_cache_get_failed", "key": key, "error": str(e)})
        return None


def _cache_set_json(key: str, value: Any, ttl: int = REDIS_ORG_CACHE_TTL_SECONDS) -> None:
    if not redis_client:
        return
    try:
        redis_client.setex(key, max(1, int(ttl)), json.dumps(_to_cache_value(value)))
    except Exception as e:
        logger.warning({"event": "org_cache_set_failed", "key": key, "error": str(e)})


def invalidate_organization_cache(org_id: Optional[str] = None, inbound_trunk_id: Optional[str] = None) -> None:
    if not redis_client:
        return
    keys = []
    if org_id:
        keys.append(_cache_key_by_id(org_id))
    if inbound_trunk_id:
        keys.append(_cache_key_inbound_trunk(inbound_trunk_id))
    try:
        for key in keys:
            redis_client.delete(key)
    except Exception as e:
        logger.warning({"event": "org_cache_invalidate_failed", "error": str(e)})


def _normalize(doc: Optional[Dict]) -> Optional[Dict]:
    if not doc:
        return None
    doc["id"] = str(doc.get("_id"))
    return doc


def get_organization_by_id(org_id: str) -> Optional[Dict]:
    cache_key = _cache_key_by_id(org_id)
    cached = _cache_get_json(cache_key)
    if cached:
        return _from_cache_value(cached)
    try:
        doc = collection.find_one({"_id": ObjectId(org_id)})
    except Exception:
        return None
    normalized = _normalize(doc)
    if normalized:
        _cache_set_json(cache_key, normalized)
    return normalized


def get_organization_by_inbound_trunk_id(trunk_id: str) -> Optional[Dict]:
    cache_key = _cache_key_inbound_trunk(trunk_id)
    cached = _cache_get_json(cache_key)
    if cached:
        return _from_cache_value(cached)
    normalized = _normalize(collection.find_one({"settings.sip_inbound_trunk.trunk_id": trunk_id}))
    if normalized:
        _cache_set_json(cache_key, normalized)
    return normalized
