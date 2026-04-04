from typing import Dict, List, Optional

from bson import ObjectId
from pymongo import MongoClient

from app.config import DB_NAME, MONGO_URI
from app.utils.mongodb_org import org_filter

client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client[DB_NAME]
collection = db["users"]


def get_user_by_id(user_id: str, organization_id: Optional[str] = None) -> Optional[Dict]:
    try:
        query: Dict[str, object] = {"_id": ObjectId(user_id)}
    except Exception:
        return None
    if organization_id:
        query.update(org_filter(organization_id))
    return collection.find_one(query)


def get_user_by_email(email: str, organization_id: Optional[str] = None) -> Optional[Dict]:
    query: Dict[str, object] = {"email": email}
    if organization_id:
        query.update(org_filter(organization_id))
    return collection.find_one(query)


def get_users_by_ids(user_ids: List[str], organization_id: Optional[str] = None) -> Dict[str, Dict]:
    object_ids = []
    id_map: Dict[ObjectId, str] = {}
    for uid in user_ids:
        try:
            oid = ObjectId(uid)
            object_ids.append(oid)
            id_map[oid] = uid
        except Exception:
            continue
    if not object_ids:
        return {}
    query: Dict[str, object] = {"_id": {"$in": object_ids}}
    if organization_id:
        query.update(org_filter(organization_id))
    out: Dict[str, Dict] = {}
    for user in collection.find(query):
        uid = id_map.get(user["_id"])
        if uid is not None:
            out[uid] = user
    return out


class _UserServiceProxy:
    get_user_by_id = staticmethod(get_user_by_id)
    get_user_by_email = staticmethod(get_user_by_email)
    get_users_by_ids = staticmethod(get_users_by_ids)


user_service = _UserServiceProxy()
