from typing import Dict, Optional

from bson import ObjectId
from pymongo import MongoClient

from app.config import MONGO_URI, DB_NAME


client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client[DB_NAME]
collection = db["organizations"]


def _normalize(doc: Optional[Dict]) -> Optional[Dict]:
    if not doc:
        return None
    doc["id"] = str(doc.get("_id"))
    return doc


def get_organization_by_id(org_id: str) -> Optional[Dict]:
    return _normalize(collection.find_one({"_id": ObjectId(org_id)}))


def get_organization_by_inbound_trunk_id(trunk_id: str) -> Optional[Dict]:
    return _normalize(collection.find_one({"settings.sip_inbound_trunk.trunk_id": trunk_id}))
