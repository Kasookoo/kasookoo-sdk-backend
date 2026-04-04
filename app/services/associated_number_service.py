"""
Associated Number Service - organization-scoped inbound numbers with separate user associations.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from app.config import DB_NAME, MONGO_URI
from app.utils.mongodb_org import org_filter, org_value

from .mongodb import BaseMongoClient


def _normalize_phone_number(phone_number: str) -> str:
    if not phone_number:
        return ""
    cleaned = "".join(c for c in str(phone_number).strip() if c.isdigit() or c == "+")
    return cleaned


def _serialize(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    out = dict(doc)
    if out.get("_id") is not None:
        out["id"] = str(out["_id"])
        del out["_id"]
    if out.get("organization_id") is not None:
        out["organization_id"] = str(out["organization_id"])
    for key in ("created_at", "updated_at"):
        value = out.get(key)
        if value is not None and hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


class AssociatedNumberService(BaseMongoClient):
    def __init__(self, mongo_uri: str, db_name: str):
        super().__init__(mongo_uri, db_name)
        self.collection = self.get_collection("associated_numbers")
        self.association_collection = self.get_collection("associated_number_users")

    def _set_user_association(self, associated_number_id: ObjectId, organization_id: str, user_id: str) -> None:
        now = datetime.now(timezone.utc)
        orgv = org_value(organization_id)
        self.association_collection.update_many(
            {
                "associated_number_id": associated_number_id,
                "organization_id": orgv,
                "is_active": True,
            },
            {"$set": {"is_active": False, "updated_at": now}},
        )
        self.association_collection.update_one(
            {
                "associated_number_id": associated_number_id,
                "organization_id": orgv,
                "user_id": user_id,
            },
            {
                "$set": {"is_active": True, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    def _get_active_user_id(self, associated_number_id: ObjectId, organization_id: str) -> Optional[str]:
        rel = self.association_collection.find_one(
            {
                "associated_number_id": associated_number_id,
                "is_active": True,
                **org_filter(organization_id),
            }
        )
        if not rel:
            return None
        uid = rel.get("user_id")
        return str(uid) if uid is not None else None

    def list(self, organization_id: str, skip: int = 0, limit: int = 100) -> List[dict]:
        query = org_filter(organization_id)
        cursor = self.collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
        items: List[dict] = []
        for doc in cursor:
            serialized = _serialize(doc)
            if not serialized:
                continue
            try:
                aid = ObjectId(serialized["id"])
                serialized["user_id"] = self._get_active_user_id(aid, organization_id)
            except Exception:
                serialized["user_id"] = None
            items.append(serialized)
        return items

    def create(
        self,
        organization_id: str,
        phone_number: str,
        user_id: str,
        label: Optional[str] = None,
        is_active: bool = True,
    ) -> dict:
        normalized = _normalize_phone_number(phone_number)
        now = datetime.now(timezone.utc)
        existing = self.collection.find_one({"phone_number": normalized, **org_filter(organization_id)})
        if existing:
            raise ValueError(f"Number '{normalized}' is already mapped in this organization")
        doc = {
            "organization_id": org_value(organization_id),
            "phone_number": normalized,
            "label": label,
            "is_active": bool(is_active),
            "created_at": now,
            "updated_at": now,
        }
        result = self.collection.insert_one(doc)
        associated_number_id = result.inserted_id
        self._set_user_association(associated_number_id, organization_id, user_id)
        doc["_id"] = associated_number_id
        out = _serialize(doc) or {}
        out["user_id"] = user_id
        return out

    def get_by_id(self, associated_number_id: str, organization_id: str) -> Optional[dict]:
        try:
            oid = ObjectId(associated_number_id)
        except Exception:
            return None
        doc = _serialize(self.collection.find_one({"_id": oid, **org_filter(organization_id)}))
        if not doc:
            return None
        doc["user_id"] = self._get_active_user_id(oid, organization_id)
        return doc

    def update(self, associated_number_id: str, organization_id: str, updates: Dict) -> Optional[dict]:
        try:
            oid = ObjectId(associated_number_id)
        except Exception:
            return None
        update_doc: Dict = {"updated_at": datetime.now(timezone.utc)}
        if "phone_number" in updates and updates["phone_number"] is not None:
            normalized = _normalize_phone_number(updates["phone_number"])
            dup = self.collection.find_one(
                {"_id": {"$ne": oid}, "phone_number": normalized, **org_filter(organization_id)}
            )
            if dup:
                raise ValueError(f"Number '{normalized}' is already mapped in this organization")
            update_doc["phone_number"] = normalized
        for field in ("label", "is_active"):
            if field in updates and updates[field] is not None:
                update_doc[field] = updates[field]
        doc = self.collection.find_one_and_update(
            {"_id": oid, **org_filter(organization_id)},
            {"$set": update_doc, "$unset": {"user_id": ""}},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return None
        if "user_id" in updates and updates["user_id"] is not None:
            self._set_user_association(oid, organization_id, str(updates["user_id"]))
        if "is_active" in updates and updates["is_active"] is False:
            self.association_collection.update_many(
                {"associated_number_id": oid, **org_filter(organization_id)},
                {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}},
            )
        out = _serialize(doc) or {}
        out["user_id"] = self._get_active_user_id(oid, organization_id)
        return out

    def delete(self, associated_number_id: str, organization_id: str) -> bool:
        try:
            oid = ObjectId(associated_number_id)
        except Exception:
            return False
        self.association_collection.delete_many({"associated_number_id": oid, **org_filter(organization_id)})
        result = self.collection.delete_one({"_id": oid, **org_filter(organization_id)})
        return result.deleted_count > 0

    def get_active_mapping_by_number(self, phone_number: str, organization_id: Optional[str] = None) -> Optional[dict]:
        normalized = _normalize_phone_number(phone_number)
        query = {"phone_number": normalized, "is_active": True}
        if organization_id:
            query.update(org_filter(organization_id))
        doc = self.collection.find_one(query)
        serialized = _serialize(doc)
        if not serialized:
            return None
        try:
            aid = ObjectId(serialized["id"])
        except Exception:
            return None
        if organization_id:
            user_id = self._get_active_user_id(aid, organization_id)
        else:
            rel = self.association_collection.find_one({"associated_number_id": aid, "is_active": True})
            user_id = str(rel.get("user_id")) if rel and rel.get("user_id") is not None else None
        if not user_id:
            return None
        serialized["user_id"] = user_id
        return serialized


associated_number_service = AssociatedNumberService(MONGO_URI, DB_NAME)
