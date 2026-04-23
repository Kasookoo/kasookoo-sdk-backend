"""
Organization Service - Manages organizations for multi-tenant isolation.
"""
import logging
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo import ReturnDocument
from redis import Redis

from app.models.models import OrganizationSettings
from app.config import (
    REDIS_URL,
    REDIS_ORG_CACHE_PREFIX,
    REDIS_ORG_CACHE_TTL_SECONDS,
)

from .mongodb import BaseMongoClient

logger = logging.getLogger(__name__)

# Slug for the default organization (used for backfill and legacy users)
DEFAULT_ORG_SLUG = "default"


def _merge_settings_excluding_nulls(incoming: dict, existing: Optional[dict]) -> dict:
    """
    Merge incoming settings with existing, but skip null values from incoming.
    When auth_password (or any field) is null in incoming, keep the existing value.
    """
    if not existing:
        return {k: v for k, v in incoming.items() if v is not None}
    result = dict(existing)
    for k, v in incoming.items():
        if v is None:
            continue  # Don't overwrite with null
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge_settings_excluding_nulls(v, result[k])
        else:
            result[k] = v
    return result


def _serialize_org(org: Optional[dict]) -> Optional[dict]:
    """Add string id and serialize datetimes for API responses."""
    if not org or not org.get("_id"):
        return org
    out = dict(org)
    out["id"] = str(out["_id"])
    del out["_id"]
    for key in ("created_at", "updated_at"):
        if key in out and out[key] is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


class OrganizationService(BaseMongoClient):
    def __init__(self, mongo_uri: str, db_name: str):
        super().__init__(mongo_uri, db_name)
        self.collection = self.get_collection("organizations")
        self.redis: Optional[Redis] = None
        try:
            self.redis = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as e:
            logger.warning({"event": "org_cache_redis_init_failed", "error": str(e)})

    def _cache_key_by_id(self, organization_id: str) -> str:
        return f"{REDIS_ORG_CACHE_PREFIX}:id:{str(organization_id).strip()}"

    def _cache_key_by_slug(self, slug: str) -> str:
        return f"{REDIS_ORG_CACHE_PREFIX}:slug:{str(slug).strip().lower()}"

    def _cache_key_inbound_trunk(self, trunk_id: str) -> str:
        return f"{REDIS_ORG_CACHE_PREFIX}:inbound_trunk:{str(trunk_id).strip()}"

    def _cache_key_default_org(self) -> str:
        return f"{REDIS_ORG_CACHE_PREFIX}:default"

    def _cache_key_list(self, skip: int, limit: int) -> str:
        return f"{REDIS_ORG_CACHE_PREFIX}:list:{int(skip)}:{int(limit)}"

    def _to_cache_value(self, value: Any) -> Any:
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: self._to_cache_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._to_cache_value(v) for v in value]
        return value

    def _from_cache_value(self, value: Any, key: Optional[str] = None) -> Any:
        if isinstance(value, dict):
            return {k: self._from_cache_value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self._from_cache_value(v) for v in value]
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

    def _cache_get_json(self, key: str) -> Optional[Any]:
        if not self.redis:
            return None
        try:
            raw = self.redis.get(key)
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            logger.warning({"event": "org_cache_get_failed", "key": key, "error": str(e)})
            return None

    def _cache_set_json(self, key: str, value: Any, ttl: int = REDIS_ORG_CACHE_TTL_SECONDS) -> None:
        if not self.redis:
            return
        try:
            self.redis.setex(key, max(1, int(ttl)), json.dumps(self._to_cache_value(value)))
        except Exception as e:
            logger.warning({"event": "org_cache_set_failed", "key": key, "error": str(e)})

    def _cache_delete(self, key: str) -> None:
        if not self.redis:
            return
        try:
            self.redis.delete(key)
        except Exception as e:
            logger.warning({"event": "org_cache_delete_failed", "key": key, "error": str(e)})

    def _invalidate_org_cache(self, organization_id: Optional[str] = None, slug: Optional[str] = None, inbound_trunk_id: Optional[str] = None) -> None:
        if not self.redis:
            return
        keys = [f"{REDIS_ORG_CACHE_PREFIX}:list:*"]
        if organization_id:
            keys.append(self._cache_key_by_id(organization_id))
        if slug:
            keys.append(self._cache_key_by_slug(slug))
        if inbound_trunk_id:
            keys.append(self._cache_key_inbound_trunk(inbound_trunk_id))
        keys.append(self._cache_key_default_org())
        try:
            for key in keys:
                if "*" in key:
                    for match in self.redis.scan_iter(match=key, count=100):
                        self.redis.delete(match)
                else:
                    self.redis.delete(key)
        except Exception as e:
            logger.warning({"event": "org_cache_invalidate_failed", "error": str(e)})

    def get_default_organization(self) -> Optional[dict]:
        """
        Return the default organization document. Used for migration and for users without org.
        """
        cached = self._cache_get_json(self._cache_key_default_org())
        if cached:
            return self._from_cache_value(cached)

        org = self.collection.find_one({"slug": DEFAULT_ORG_SLUG})
        if org and org.get("_id"):
            org["id"] = str(org["_id"])
            self._cache_set_json(self._cache_key_default_org(), org)
            self._cache_set_json(self._cache_key_by_id(org["id"]), org)
            self._cache_set_json(self._cache_key_by_slug(DEFAULT_ORG_SLUG), org)
        return org

    def get_or_create_default_organization(self) -> dict:
        """
        Get the default organization, creating it if it does not exist.
        Used by migration and at runtime when resolving tenant.
        """
        org = self.get_default_organization()
        if org:
            return org
        now = datetime.now(timezone.utc)
        doc = {
            "name": "Default",
            "slug": DEFAULT_ORG_SLUG,
            "email": None,
            "phone_number": None,
            "created_at": now,
            "updated_at": now,
            "settings": {},
        }
        result = self.collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        doc["id"] = str(result.inserted_id)
        self._invalidate_org_cache(organization_id=doc["id"], slug=DEFAULT_ORG_SLUG)
        logger.info({"event": "default_organization_created", "organization_id": doc["id"]})
        return doc

    def get_organization_by_id(self, organization_id: str) -> Optional[dict]:
        """Get organization by ID. Returns None if not found or invalid id."""
        cached = self._cache_get_json(self._cache_key_by_id(organization_id))
        if cached:
            return self._from_cache_value(cached)
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return None
        org = self.collection.find_one({"_id": obj_id})
        if org and org.get("_id"):
            org["id"] = str(org["_id"])
            self._cache_set_json(self._cache_key_by_id(org["id"]), org)
            if org.get("slug"):
                self._cache_set_json(self._cache_key_by_slug(org["slug"]), org)
        return org

    def list_organizations(
        self,
        skip: int = 0,
        limit: int = 100,
    ) -> List[dict]:
        """List organizations with optional pagination."""
        cache_key = self._cache_key_list(skip, limit)
        cached = self._cache_get_json(cache_key)
        if cached:
            return cached
        cursor = self.collection.find({}).sort("created_at", -1).skip(skip).limit(limit)
        result: List[dict] = []
        for doc in cursor:
            if doc:
                serialized = _serialize_org(doc)
                if serialized is not None:
                    result.append(serialized)
        self._cache_set_json(cache_key, result, ttl=60)
        return result

    def create_organization(
        self,
        name: str,
        slug: str,
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        settings: Optional[OrganizationSettings] = None,
    ) -> dict:
        """Create a new organization. Raises if slug already exists. Optionally store settings (e.g. sip_outbound_trunk)."""
        existing = self.collection.find_one({"slug": slug})
        if existing:
            raise ValueError(f"Organization with slug '{slug}' already exists")
        now = datetime.now(timezone.utc)
        default_settings = OrganizationSettings(show_user_list="agent_customer_list", sip_outbound_trunk=None, sip_inbound_trunk=None)
        settings_to_store = (settings or default_settings).model_dump()
        doc = {
            "name": name,
            "slug": slug,
            "email": email,
            "phone_number": phone_number,
            "created_at": now,
            "updated_at": now,
            "settings": settings_to_store,
        }
        result = self.collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        doc["id"] = str(result.inserted_id)
        self._invalidate_org_cache(organization_id=doc["id"], slug=slug)
        logger.info({"event": "organization_created", "organization_id": doc["id"], "slug": slug})
        serialized = _serialize_org(doc)
        if serialized is None:
            return doc  # fallback if serialize fails
        return serialized

    def update_organization(
        self,
        organization_id: str,
        name: Optional[str] = None,
        slug: Optional[str] = None,
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        settings: Optional[OrganizationSettings] = None
    ) -> Optional[dict]:
        """Update organization by ID. Only provided fields are updated. Returns updated org or None."""
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return None
        existing_org = self.collection.find_one({"_id": obj_id})
        if not existing_org:
            return None
        old_slug = existing_org.get("slug")

        update: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if name is not None:
            update["name"] = name
        if slug is not None:
            existing = self.collection.find_one({"slug": slug, "_id": {"$ne": obj_id}})
            if existing:
                raise ValueError(f"Organization with slug '{slug}' already exists")
            update["slug"] = slug
        if email is not None:
            update["email"] = email
        if phone_number is not None:
            update["phone_number"] = phone_number
        if settings is not None:
            settings_dict = settings.model_dump() if hasattr(settings, "model_dump") else settings
            existing_settings = (existing_org or {}).get("settings") if existing_org else None
            update["settings"] = _merge_settings_excluding_nulls(settings_dict, existing_settings)
        result = self.collection.find_one_and_update(
            {"_id": obj_id},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            return None
        inbound_trunk_id = None
        inbound = ((result.get("settings") or {}).get("sip_inbound_trunk") or {})
        if isinstance(inbound, dict):
            inbound_trunk_id = inbound.get("trunk_id")
        self._invalidate_org_cache(
            organization_id=str(result.get("_id")),
            slug=slug or result.get("slug") or old_slug,
            inbound_trunk_id=inbound_trunk_id,
        )
        return _serialize_org(result)

    def delete_organization(self, organization_id: str) -> bool:
        """Delete organization by ID. Returns True if deleted, False if not found or invalid id."""
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return False
        existing = self.collection.find_one({"_id": obj_id})
        result = self.collection.delete_one({"_id": obj_id})
        if result.deleted_count:
            inbound_trunk_id = None
            inbound = (((existing or {}).get("settings") or {}).get("sip_inbound_trunk") or {})
            if isinstance(inbound, dict):
                inbound_trunk_id = inbound.get("trunk_id")
            self._invalidate_org_cache(
                organization_id=organization_id,
                slug=(existing or {}).get("slug"),
                inbound_trunk_id=inbound_trunk_id,
            )
            logger.info({"event": "organization_deleted", "organization_id": organization_id})
        return result.deleted_count > 0

    def get_contact_center_number(self, organization_id: str) -> Optional[str]:
        """
        Get contact center number from organization settings.
        Returns None if org not found or contact_center_number not set.
        Prefer get_organization_call_settings() when both contact_center_number and trunk are needed (single query).
        """
        settings = self.get_organization_call_settings(organization_id)
        return settings.get("contact_center_number") if settings else None

    def get_organization_call_settings(self, organization_id: str) -> Optional[Dict[str, Any]]:
        """
        Get organization call settings in a single query: contact_center_number and sip_outbound_trunk.
        Use this when both are needed (e.g. dial flow) to avoid duplicate organization fetches.
        Returns dict with keys: contact_center_number, sip_outbound_trunk, org; or None if org not found.
        """
        org = self.get_organization_by_id(organization_id)
        if not org:
            return None
        settings = org.get("settings") or {}
        number = settings.get("contact_center_number")
        contact_center_number = str(number).strip() if number else None
        return {
            "contact_center_number": contact_center_number,
            "sip_outbound_trunk": settings.get("sip_outbound_trunk"),
            "sip_inbound_trunk": settings.get("sip_inbound_trunk"),
            "org": org,
        }

    def remove_sip_outbound_trunk(self, organization_id: str) -> bool:
        """Remove sip_outbound_trunk from organization settings. Preserves other settings. Returns True if updated."""
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return False
        org = self.collection.find_one({"_id": obj_id})
        if not org:
            return False
        settings = dict(org.get("settings") or {})
        if "sip_outbound_trunk" not in settings:
            return True  # Already removed, consider success
        settings.pop("sip_outbound_trunk", None)
        result = self.collection.update_one(
            {"_id": obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc), "settings": settings}},
        )
        if result.modified_count:
            self._invalidate_org_cache(organization_id=organization_id, slug=org.get("slug"))
            logger.info({"event": "organization_sip_outbound_trunk_removed", "organization_id": organization_id})
        return result.modified_count > 0

    def update_organization_sip_trunk_id(self, organization_id: str, trunk_id: str) -> bool:
        """Set or update settings.sip_outbound_trunk.trunk_id for an organization. Preserves other settings. Returns True if updated."""
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return False
        org = self.collection.find_one({"_id": obj_id})
        if not org:
            return False
        settings = dict(org.get("settings") or {})
        sip_trunk = dict(settings.get("sip_outbound_trunk") or {})
        sip_trunk["trunk_id"] = trunk_id
        settings["sip_outbound_trunk"] = sip_trunk
        result = self.collection.update_one(
            {"_id": obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc), "settings": settings}},
        )
        if result.modified_count:
            self._invalidate_org_cache(organization_id=organization_id, slug=org.get("slug"))
            logger.info({"event": "organization_sip_trunk_id_updated", "organization_id": organization_id})
        return result.modified_count > 0

    def remove_sip_inbound_trunk(self, organization_id: str) -> bool:
        """Remove sip_inbound_trunk from organization settings. Preserves other settings. Returns True if updated."""
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return False
        org = self.collection.find_one({"_id": obj_id})
        if not org:
            return False
        settings = dict(org.get("settings") or {})
        if "sip_inbound_trunk" not in settings:
            return True  # Already removed, consider success
        settings.pop("sip_inbound_trunk", None)
        result = self.collection.update_one(
            {"_id": obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc), "settings": settings}},
        )
        if result.modified_count:
            self._invalidate_org_cache(organization_id=organization_id, slug=org.get("slug"))
            logger.info({"event": "organization_sip_inbound_trunk_removed", "organization_id": organization_id})
        return result.modified_count > 0

    def update_organization_sip_inbound_trunk_id(self, organization_id: str, trunk_id: str) -> bool:
        """Set or update settings.sip_inbound_trunk.trunk_id for an organization. Preserves other settings. Returns True if updated."""
        try:
            obj_id = ObjectId(organization_id)
        except (TypeError, ValueError):
            return False
        org = self.collection.find_one({"_id": obj_id})
        if not org:
            return False
        settings = dict(org.get("settings") or {})
        sip_trunk = dict(settings.get("sip_inbound_trunk") or {})
        sip_trunk["trunk_id"] = trunk_id
        settings["sip_inbound_trunk"] = sip_trunk
        result = self.collection.update_one(
            {"_id": obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc), "settings": settings}},
        )
        if result.modified_count:
            self._invalidate_org_cache(organization_id=organization_id, slug=org.get("slug"), inbound_trunk_id=trunk_id)
            logger.info({"event": "organization_sip_inbound_trunk_id_updated", "organization_id": organization_id})
        return result.modified_count > 0

    def get_organization_by_inbound_trunk_id(self, trunk_id: str) -> Optional[dict]:
        """Find organization that owns settings.sip_inbound_trunk.trunk_id."""
        if not trunk_id or not str(trunk_id).strip():
            return None
        normalized_trunk = str(trunk_id).strip()
        cached = self._cache_get_json(self._cache_key_inbound_trunk(normalized_trunk))
        if cached:
            return cached
        org = self.collection.find_one({"settings.sip_inbound_trunk.trunk_id": normalized_trunk})
        serialized = _serialize_org(org) if org else None
        if serialized:
            self._cache_set_json(self._cache_key_inbound_trunk(normalized_trunk), serialized)
        return serialized
