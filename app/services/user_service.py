import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo import MongoClient
from redis import Redis

from app.config import (
    DB_NAME,
    MONGO_URI,
    REDIS_URL,
    REDIS_USER_CACHE_PREFIX,
    REDIS_USER_CACHE_TTL_SECONDS,
)
from app.utils.mongodb_org import org_filter

client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client[DB_NAME]
collection = db["users"]
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
    logger.warning({"event": "user_cache_redis_init_failed", "error": str(e)})


def _cache_org_scope(organization_id: Optional[str]) -> str:
    return str(organization_id).strip() if organization_id and str(organization_id).strip() else "global"


def _cache_user_id_key(user_id: str, organization_id: Optional[str] = None) -> str:
    return f"{REDIS_USER_CACHE_PREFIX}:id:{_cache_org_scope(organization_id)}:{str(user_id).strip()}"


def _cache_user_email_key(email: str, organization_id: Optional[str] = None) -> str:
    return f"{REDIS_USER_CACHE_PREFIX}:email:{_cache_org_scope(organization_id)}:{str(email).strip().lower()}"


def _to_cache_value(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_to_cache_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_cache_value(v) for k, v in value.items()}
    return value


def _from_cache_value(value: Any, key: Optional[str] = None) -> Any:
    if isinstance(value, list):
        return [_from_cache_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_cache_value(v, key=k) for k, v in value.items()}
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


def _cache_get_json(key: str) -> Optional[Any]:
    if not redis_client:
        return None
    try:
        raw = redis_client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning({"event": "user_cache_get_failed", "key": key, "error": str(e)})
        return None


def _cache_set_json(key: str, value: Any, ttl: int = REDIS_USER_CACHE_TTL_SECONDS) -> None:
    if not redis_client:
        return
    try:
        redis_client.setex(key, max(1, int(ttl)), json.dumps(_to_cache_value(value)))
    except Exception as e:
        logger.warning({"event": "user_cache_set_failed", "key": key, "error": str(e)})


def invalidate_user_cache(
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> None:
    if not redis_client:
        return
    keys = []
    if user_id:
        uid = str(user_id).strip()
        if organization_id and str(organization_id).strip():
            keys.append(_cache_user_id_key(uid, organization_id))
        else:
            keys.append(_cache_user_id_key(uid, None))
            keys.append(f"{REDIS_USER_CACHE_PREFIX}:id:*:{uid}")
    if email:
        normalized_email = str(email).strip().lower()
        if organization_id and str(organization_id).strip():
            keys.append(_cache_user_email_key(normalized_email, organization_id))
        else:
            keys.append(_cache_user_email_key(normalized_email, None))
            keys.append(f"{REDIS_USER_CACHE_PREFIX}:email:*:{normalized_email}")
    try:
        for key in keys:
            if "*" in key:
                for match in redis_client.scan_iter(match=key, count=100):
                    redis_client.delete(match)
            else:
                redis_client.delete(key)
    except Exception as e:
        logger.warning({"event": "user_cache_invalidate_failed", "error": str(e)})


def get_user_by_id(user_id: str, organization_id: Optional[str] = None) -> Optional[Dict]:
    cache_key = _cache_user_id_key(user_id, organization_id)
    cached = _cache_get_json(cache_key)
    if cached:
        return _from_cache_value(cached)
    try:
        query: Dict[str, object] = {"_id": ObjectId(user_id)}
    except Exception:
        return None
    if organization_id:
        query.update(org_filter(organization_id))
    user = collection.find_one(query)
    if user:
        _cache_set_json(cache_key, user)
        email = user.get("email")
        if email:
            _cache_set_json(_cache_user_email_key(email, organization_id), user)
    return user


def get_user_by_email(email: str, organization_id: Optional[str] = None) -> Optional[Dict]:
    cache_key = _cache_user_email_key(email, organization_id)
    cached = _cache_get_json(cache_key)
    if cached:
        return _from_cache_value(cached)
    query: Dict[str, object] = {"email": email}
    if organization_id:
        query.update(org_filter(organization_id))
    user = collection.find_one(query)
    if user:
        _cache_set_json(cache_key, user)
        _cache_set_json(_cache_user_id_key(str(user.get("_id")), organization_id), user)
    return user


def get_users_by_ids(user_ids: List[str], organization_id: Optional[str] = None) -> Dict[str, Dict]:
    if not user_ids:
        return {}

    out: Dict[str, Dict] = {}
    missing_ids: List[str] = []
    for uid in user_ids:
        cached = _cache_get_json(_cache_user_id_key(uid, organization_id))
        if cached:
            out[uid] = _from_cache_value(cached)
        else:
            missing_ids.append(uid)

    if not missing_ids:
        return out

    object_ids = []
    id_map: Dict[ObjectId, str] = {}
    for uid in missing_ids:
        try:
            oid = ObjectId(uid)
            object_ids.append(oid)
            id_map[oid] = uid
        except Exception:
            continue
    if not object_ids:
        return out
    query: Dict[str, object] = {"_id": {"$in": object_ids}}
    if organization_id:
        query.update(org_filter(organization_id))
    for user in collection.find(query):
        uid = id_map.get(user["_id"])
        if uid is not None:
            out[uid] = user
            _cache_set_json(_cache_user_id_key(uid, organization_id), user)
            email = user.get("email")
            if email:
                _cache_set_json(_cache_user_email_key(email, organization_id), user)
    return out


class _UserServiceProxy:
    get_user_by_id = staticmethod(get_user_by_id)
    get_user_by_email = staticmethod(get_user_by_email)
    get_users_by_ids = staticmethod(get_users_by_ids)


user_service = _UserServiceProxy()
