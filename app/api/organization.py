"""
Organization CRUD API - Manage organizations (multi-tenant).
Includes signup journey: create new organization + first admin user and return tokens.
"""
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import (
    oauth2_scheme,
    create_access_token,
    UserResponse as AuthUserResponse,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)
from app.config import SDK_SIP_OUTBOUND_TRUNK_ID
from app.services import livekit_sip_bridge, organization_service
from app.services import user_service
from app.services.token_storage_service import token_storage_service
from app.models.models import (
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
    OrganizationSignupRequest,
    OrganizationSettings,
)
from app.utils.performance_monitor import monitor

logger = logging.getLogger(__name__)


async def _ensure_livekit_trunk_created(settings: Optional[OrganizationSettings]) -> Optional[OrganizationSettings]:
    """
    If settings contain sip_outbound_trunk with address/auth but no trunk_id,
    create the LiveKit trunk and return settings with trunk_id set.
    """
    if not settings or not settings.sip_outbound_trunk:
        return settings
    sip = settings.sip_outbound_trunk
    if sip.trunk_id:
        return settings
    if not (sip.address and sip.auth_username and sip.auth_password):
        return settings
    try:
        from app.services import livekit_sip_bridge
        trunk_id = await livekit_sip_bridge.setup_sip_outbound_trunk(
            trunk_name=sip.name or "org-trunk",
            outbound_address=sip.address,
            outbound_username=sip.auth_username,
            outbound_password=sip.auth_password,
            numbers=sip.numbers,
        )
        new_sip = sip.model_copy(update={"trunk_id": trunk_id})
        logger.info({"event": "livekit_trunk_created_for_org", "trunk_id": trunk_id})
        return settings.model_copy(update={"sip_outbound_trunk": new_sip})
    except Exception as e:
        logger.warning("Failed to create LiveKit trunk for organization: %s", e)
        return settings


async def _ensure_livekit_inbound_trunk_created(settings: Optional[OrganizationSettings]) -> Optional[OrganizationSettings]:
    """
    If settings contain sip_inbound_trunk with config but no trunk_id,
    create the LiveKit inbound trunk and return settings with trunk_id set.
    """
    if not settings or not getattr(settings, "sip_inbound_trunk", None):
        return settings
    sip = settings.sip_inbound_trunk
    if sip.trunk_id:
        return settings
    has_any_allow = bool(sip.allowed_addresses) or bool(sip.auth_username and sip.auth_password)
    if not has_any_allow:
        # LiveKit requires additional fields when accepting calls to any number (numbers=[]),
        # and practically you want some allowlist/auth anyway. If not provided, skip creation.
        return settings
    try:
        trunk_id = await livekit_sip_bridge.setup_sip_inbound_trunk(
            trunk_name=sip.name or "org-inbound-trunk",
            allowed_addresses=sip.allowed_addresses,
            numbers=sip.numbers,
            allowed_numbers=sip.allowed_numbers,
            auth_username=sip.auth_username,
            auth_password=sip.auth_password,
            krisp_enabled=sip.krisp_enabled,
            metadata=sip.metadata,
        )
        new_sip = sip.model_copy(update={"trunk_id": trunk_id})
        logger.info({"event": "livekit_inbound_trunk_created_for_org", "trunk_id": trunk_id})
        return settings.model_copy(update={"sip_inbound_trunk": new_sip})
    except Exception as e:
        logger.warning("Failed to create LiveKit inbound trunk for organization: %s", e)
        return settings

router = APIRouter()


class OrganizationSignupResponse(BaseModel):
    """Response for organization signup: access token + user + organization."""
    access_token: str
    access_token_type: str = "bearer"
    access_token_expires_minutes: int
    created_at: str
    user: AuthUserResponse
    organization: OrganizationResponse


async def _authenticate(token: str = Depends(oauth2_scheme)) -> str:
    """Ensure request is authenticated (token valid)."""
    from app.api.auth import normal_authenticate_token
    await normal_authenticate_token(token)
    return token


@router.post("/organizations/signup", response_model=OrganizationSignupResponse, status_code=201)
@monitor(name="api.organization.signup")
async def organization_signup(body: OrganizationSignupRequest):
    """
    Signup journey: create a new organization and its first admin user.
    No authentication required. Returns a short-lived access_token plus user and organization.
    The frontend SDK should mint a new JWT on each interval (no refresh token).
    """
    settings_to_store = body.organization_settings
    if body.organization_settings:
        settings_to_store = await _ensure_livekit_trunk_created(body.organization_settings)
        settings_to_store = await _ensure_livekit_inbound_trunk_created(settings_to_store)
    try:
        org = organization_service.create_organization(
            name=body.organization_name,
            slug=body.organization_slug,
            email=body.organization_email,
            phone_number=body.organization_phone_number,
            settings=settings_to_store,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    org_id = org.get("id") or str(org.get("_id", ""))
    try:
        created_user = await user_service.create_user(
            email=body.admin_email,
            phone_number=body.admin_phone_number or "",
            first_name=body.admin_first_name,
            last_name=body.admin_last_name,
            role="admin",
            password=body.admin_password,
            organization_id=org_id,
        )
    except Exception as e:
        logger.exception("Failed to create admin user during signup")
        raise HTTPException(status_code=400, detail=f"Failed to create admin user: {str(e)}")
    user_id = str(created_user["_id"])
    token_data = {"sub": user_id, "email": created_user["email"]}
    access_token = create_access_token(token_data)
    try:
        await token_storage_service.save_user_tokens(
            user_id=user_id,
            access_token=access_token,
            user_type="admin",
        )
    except Exception as e:
        logger.warning("Failed to save tokens for signup user: %s", e)
    org_response = OrganizationResponse(**org)
    user_response = AuthUserResponse(
        id=user_id,
        email=created_user["email"],
        phone_number=created_user.get("phone_number"),
        clerk_id=created_user.get("clerk_id"),
        first_name=created_user["first_name"],
        last_name=created_user["last_name"],
        role="admin",
        organization_id=org_id,
    )
    current_date_time = time.strftime("%Y-%m-%d %H:%M:%S")
    return OrganizationSignupResponse(
        access_token=access_token,
        access_token_type="bearer",
        access_token_expires_minutes=ACCESS_TOKEN_EXPIRE_MINUTES,
        created_at=current_date_time,
        user=user_response,
        organization=org_response,
    )


@router.get("/organizations", response_model=List[OrganizationResponse])
@monitor(name="api.organization.list_organizations")
async def list_organizations(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=500, description="Max records to return"),
    token: str = Depends(_authenticate),
):
    """List all organizations with pagination."""
    items = organization_service.list_organizations(skip=skip, limit=limit)
    return [OrganizationResponse(**item) for item in items]


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
@monitor(name="api.organization.get_organization")
async def get_organization(
    organization_id: str,
    token: str = Depends(_authenticate),
):
    """Get a single organization by ID."""
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    # Ensure serialized for response (id, datetime strings)
    if "_id" in org:
        org = dict(org)
        org["id"] = str(org["_id"])
        del org["_id"]
    for key in ("created_at", "updated_at"):
        if key in org and org.get(key) is not None and hasattr(org[key], "isoformat"):
            org[key] = org[key].isoformat()
    return OrganizationResponse(**org)


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
@monitor(name="api.organization.create_organization")
async def create_organization(
    body: OrganizationCreate,
    token: str = Depends(_authenticate),
):
    """Create a new organization. Slug must be unique. Can include settings (e.g. sip_outbound_trunk). LiveKit trunk is created if SIP config is provided."""
    settings_to_store = body.settings
    if body.settings:
        settings_to_store = await _ensure_livekit_trunk_created(body.settings)
        settings_to_store = await _ensure_livekit_inbound_trunk_created(settings_to_store)
    try:
        org = organization_service.create_organization(
            name=body.name,
            slug=body.slug,
            email=body.email,
            phone_number=body.phone_number,
            settings=settings_to_store,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return OrganizationResponse(**org)


@router.put("/organizations/{organization_id}", response_model=OrganizationResponse)
@monitor(name="api.organization.update_organization")
async def update_organization(
    organization_id: str,
    body: OrganizationUpdate,
    token: str = Depends(_authenticate),
):
    """Update an organization by ID. Only provided fields are updated. LiveKit trunk is created if settings.sip_outbound_trunk has config but no trunk_id."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        org = organization_service.get_organization_by_id(organization_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if "_id" in org:
            org = dict(org)
            org["id"] = str(org["_id"])
            del org["_id"]
        for key in ("created_at", "updated_at"):
            if key in org and org.get(key) is not None and hasattr(org[key], "isoformat"):
                org[key] = org[key].isoformat()
        return OrganizationResponse(**org)
    if "settings" in updates and updates["settings"]:
        settings_obj = OrganizationSettings(**updates["settings"])
        settings_obj = await _ensure_livekit_trunk_created(settings_obj)
        settings_obj = await _ensure_livekit_inbound_trunk_created(settings_obj)
        updates["settings"] = settings_obj
    try:
        org = organization_service.update_organization(
            organization_id=organization_id,
            **updates,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return OrganizationResponse(**org)


@router.delete("/organizations/{organization_id}", status_code=204)
@monitor(name="api.organization.delete_organization")
async def delete_organization(
    organization_id: str,
    token: str = Depends(_authenticate),
):
    """Delete an organization by ID."""
    deleted = organization_service.delete_organization(organization_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Organization not found")
    return None


@router.delete("/organizations/{organization_id}/settings/sip-outbound-trunk", response_model=OrganizationResponse)
@monitor(name="api.organization.remove_sip_outbound_trunk")
async def remove_sip_outbound_trunk(
    organization_id: str,
    token: str = Depends(_authenticate),
):
    """Remove SIP outbound trunk from LiveKit (when org has a dedicated trunk_id) and clear org settings."""
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    settings = org.get("settings") or {}
    sip_cfg = settings.get("sip_outbound_trunk") or {}
    trunk_id = sip_cfg.get("trunk_id") if isinstance(sip_cfg, dict) else None
    if trunk_id and str(trunk_id).strip():
        tid = str(trunk_id).strip()
        env_default = (SDK_SIP_OUTBOUND_TRUNK_ID or "").strip()
        if env_default and tid == env_default:
            logger.info(
                {
                    "event": "skip_livekit_trunk_delete_shared_env_trunk",
                    "organization_id": organization_id,
                    "sip_trunk_id": tid,
                }
            )
        else:
            await livekit_sip_bridge.delete_sip_outbound_trunk(tid)
    organization_service.remove_sip_outbound_trunk(organization_id)
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    # Serialize for response (id, datetime strings)
    org = dict(org)
    if "_id" in org:
        org["id"] = str(org["_id"])
        del org["_id"]
    for key in ("created_at", "updated_at"):
        if key in org and org.get(key) is not None and hasattr(org[key], "isoformat"):
            org[key] = org[key].isoformat()
    return OrganizationResponse(**org)


@router.delete("/organizations/{organization_id}/settings/sip-inbound-trunk", response_model=OrganizationResponse)
@monitor(name="api.organization.remove_sip_inbound_trunk")
async def remove_sip_inbound_trunk(
    organization_id: str,
    token: str = Depends(_authenticate),
):
    """Remove SIP inbound trunk from LiveKit (when org has a dedicated trunk_id) and clear org settings."""
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    settings = org.get("settings") or {}
    sip_cfg = settings.get("sip_inbound_trunk") or {}
    trunk_id = sip_cfg.get("trunk_id") if isinstance(sip_cfg, dict) else None
    if trunk_id and str(trunk_id).strip():
        tid = str(trunk_id).strip()
        await livekit_sip_bridge.delete_sip_inbound_trunk(tid)
    organization_service.remove_sip_inbound_trunk(organization_id)
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    # Serialize for response (id, datetime strings)
    org = dict(org)
    if "_id" in org:
        org["id"] = str(org["_id"])
        del org["_id"]
    for key in ("created_at", "updated_at"):
        if key in org and org.get(key) is not None and hasattr(org[key], "isoformat"):
            org[key] = org[key].isoformat()
    return OrganizationResponse(**org)


@router.post("/organizations/{organization_id}/settings/sip-inbound-trunk/reset", response_model=OrganizationResponse)
@monitor(name="api.organization.reset_sip_inbound_trunk")
async def reset_sip_inbound_trunk(
    organization_id: str,
    token: str = Depends(_authenticate),
):
    """
    Reset SIP inbound trunk: delete existing trunk (if any) and recreate from stored settings config.
    Updates settings.sip_inbound_trunk.trunk_id with the new value.
    """
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    settings = org.get("settings") or {}
    sip_cfg = settings.get("sip_inbound_trunk") or {}
    if not isinstance(sip_cfg, dict) or not sip_cfg:
        raise HTTPException(status_code=400, detail="sip_inbound_trunk settings are not configured for this organization")

    existing_tid = (sip_cfg.get("trunk_id") or "").strip()
    if existing_tid:
        await livekit_sip_bridge.delete_sip_inbound_trunk(existing_tid)

    # Recreate trunk from config (must include allowed_addresses or auth)
    has_any_allow = bool(sip_cfg.get("allowed_addresses")) or bool(sip_cfg.get("auth_username") and sip_cfg.get("auth_password"))
    if not has_any_allow:
        raise HTTPException(status_code=400, detail="sip_inbound_trunk must include allowed_addresses or auth_username/auth_password to reset")

    try:
        trunk_id = await livekit_sip_bridge.setup_sip_inbound_trunk(
            trunk_name=sip_cfg.get("name") or "org-inbound-trunk",
            allowed_addresses=sip_cfg.get("allowed_addresses"),
            numbers=sip_cfg.get("numbers"),
            allowed_numbers=sip_cfg.get("allowed_numbers"),
            auth_username=sip_cfg.get("auth_username"),
            auth_password=sip_cfg.get("auth_password"),
            krisp_enabled=sip_cfg.get("krisp_enabled"),
            metadata=sip_cfg.get("metadata"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to reset inbound trunk: {str(e)}")

    organization_service.update_organization_sip_inbound_trunk_id(organization_id, trunk_id)
    org = organization_service.get_organization_by_id(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    # Serialize for response (id, datetime strings)
    org = dict(org)
    if "_id" in org:
        org["id"] = str(org["_id"])
        del org["_id"]
    for key in ("created_at", "updated_at"):
        if key in org and org.get(key) is not None and hasattr(org[key], "isoformat"):
            org[key] = org[key].isoformat()
    return OrganizationResponse(**org)
