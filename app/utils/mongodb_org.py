"""
Shared MongoDB organization_id filter for type-flexible queries.
"""
from typing import Any, Dict, Optional

from bson import ObjectId


def org_filter(organization_id: Optional[str]) -> Dict[str, Any]:
    if not organization_id or not str(organization_id).strip():
        return {}
    organization_id = str(organization_id).strip()
    if len(organization_id) != 24:
        return {"organization_id": organization_id}
    try:
        oid = ObjectId(organization_id)
        return {"organization_id": {"$in": [oid, organization_id]}}
    except Exception:
        return {"organization_id": organization_id}


def org_value(organization_id: Optional[str]):
    if not organization_id or not str(organization_id).strip():
        return None
    organization_id = str(organization_id).strip()
    if len(organization_id) == 24:
        try:
            return ObjectId(organization_id)
        except Exception:
            pass
    return organization_id
