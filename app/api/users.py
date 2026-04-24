import logging
import random
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from app.api.auth import (
    oauth2_scheme,
    authenticate_token as normal_authenticate_token,
    get_organization_id,
)
from app.services import user_service, notification__service, organization_service
from app.services.token_storage_service import token_storage_service
from app.utils.performance_monitor import monitor

ALLOWED_USER_ROLES = ("customer", "agent", "admin")


def normalize_role(role: Optional[str]) -> Literal["customer", "agent", "admin"]:
    """
    Normalize role value to ensure it's one of the valid roles.
    Handles typos and invalid values by defaulting to "customer".
    """
    if not role:
        return "customer"
    
    role_lower = role.lower().strip()
    
    # Direct match
   
    if role_lower in ALLOWED_USER_ROLES:
        return role_lower  # type: ignore
    
    # Handle common typos
    typo_map = {
        "costumer": "customer",
        "custumer": "customer",
        "customr": "customer",
        "drver": "agent",
        "diver": "agent",
        "admn": "admin",
        "administrator": "admin",
    }
    
    if role_lower in typo_map:
        return typo_map[role_lower]  # type: ignore
    
    # Default to customer if unrecognized
    return "customer"


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

class UserCreate(BaseModel):
    email: EmailStr
    phone_number: Optional[str] = None
    first_name: str
    last_name: str    
    password: Optional[str] = None
    role: Literal["customer", "agent", "admin"] = "customer"
    caller_id: Optional[str] = None

class UserResponse(BaseModel):
    id: str
    email: EmailStr
    phone_number: Optional[str] = None
    clerk_id: Optional[str] = None
    first_name: str
    last_name: str
    role: Literal["customer", "agent", "admin"] = "customer"
    caller_id: Optional[str] = None
    organization_id: Optional[str] = None

    #customer_id: Optional[str] = None
    #customer_name: Optional[str] = None




# Login/logout endpoints moved to app.api.auth module; use POST /api/v1/sdk/auth/client-sessions to mint JWTs.



@router.get("/users")
@monitor(name="api.users.list_users")
async def list_users(
    role: str = Query(None, description="Filter by user role"),
    search: str = Query(None, description="Search by email, first name, or last name"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(10, ge=1, le=100, description="Max records to return"),
    token: str = Depends(oauth2_scheme),
    organization_id: str = Depends(get_organization_id),
):
    username = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {username}")
    filters = {}
    if role:
        filters["role"] = role
    if search:
        filters["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"first_name": {"$regex": search, "$options": "i"}},
            {"last_name": {"$regex": search, "$options": "i"}},
        ]
    users, total = user_service.list_users(filters, skip=skip, limit=limit, organization_id=organization_id)
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "users": [
            {
                "id": str(user["_id"]),
                "clerk_id": str(user.get("clerk_id", "")),
                "email": user["email"],
                "phone_number": user.get("phone_number"),
                "first_name": user["first_name"],
                "last_name": user["last_name"],
                "role": normalize_role(user.get("role")),
                "customer_id": user.get("customer_id"),
                "customer_name": user.get("customer_name"),
                "caller_id": user.get("caller_id"),
                "organization_id": str(user.get("organization_id", "")),
            }
            for user in users
        ],
    }


@router.get("/users/filter", response_model=List[UserResponse])
@monitor(name="api.users.filter_users")
async def filter_users(
    role: str = Query(None, description="Filter by user role"),
    show_user_list: Optional[str] = Query(
        None,
        description="Override organization show_user_list setting for this request",
    ),
    search: str = Query(None, description="Search by email, first name, or last name"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Max records to return"),
    token: str = Depends(oauth2_scheme),
    organization_id: str = Depends(get_organization_id),
):
    current_user_id = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {current_user_id}")
    current_user_role = "agent"
    if current_user_id:
        current_user = user_service.get_user_by_id(current_user_id)
        if current_user:
            current_user_role = normalize_role(current_user.get("role", "agent"))
    organization = organization_service.get_organization_by_id(organization_id)
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    filters = {}
    settings = organization.get("settings") or {}
    # Request parameter has higher priority than organization configuration.
    effective_show_user_list = show_user_list or settings.get("show_user_list")
    if effective_show_user_list == "opposite_user_list":
        if role:
            filters["role"] = role
    else:
        filters["role"] = current_user_role
    # When filtering by same role, exclude the current user from the result
    if filters.get("role") == current_user_role:
        try:
            filters["_id"] = {"$ne": ObjectId(current_user_id)}
        except (TypeError, ValueError):
            pass  # invalid ObjectId, skip exclusion
    if search:
        filters["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"first_name": {"$regex": search, "$options": "i"}},
            {"last_name": {"$regex": search, "$options": "i"}},
        ]
    logger.info({"event": "filter_users", "filters": filters})
    list_users_result = user_service.list_users(
        filters, skip=skip, limit=limit, organization_id=organization_id
    )
    # list_users may return either:
    # - list[dict] (legacy shape)
    # - tuple[list[dict], int] (list + total count)add 
    users = list_users_result[0] if isinstance(list_users_result, tuple) else list_users_result
    return [
        {
            "id": str(user["_id"]),
            "clerk_id": str(user.get("clerk_id", "")),
            "email": user["email"],
            "phone_number": user.get("phone_number"),
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "role": normalize_role(user.get("role")),
            "customer_id": user.get("customer_id"),
            "customer_name": user.get("customer_name"),
            "caller_id": user.get("caller_id"),
            "organization_id": str(user.get("organization_id", "")),
        }
        for user in users
    ]


@router.post("/users/create", response_model=UserResponse)
@monitor(name="api.users.create_user")
async def create_user(user: UserCreate, token: str = Depends(oauth2_scheme)):
    username = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {username}")
    current_user = user_service.get_user_by_id(username)
    org_id = None
    if current_user and current_user.get("organization_id"):
        org_id = str(current_user["organization_id"])
    if not org_id:
        default_org = organization_service.get_or_create_default_organization()
        org_id = str(default_org["_id"])
    existing_user = user_service.get_user_by_email(user.email)
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered in another organization")
    created_user = await user_service.create_user(
        email=user.email,
        phone_number=user.phone_number or "",
        first_name=user.first_name,
        last_name=user.last_name,
        role=user.role or "customer",
        password=user.password or "",
        caller_id=user.caller_id,
        organization_id=org_id,
    )
    return {
        "id": str(created_user["_id"]),
        "email": str(created_user["email"]),
        "phone_number": created_user.get("phone_number"),
        "first_name": str(created_user["first_name"]),
        "last_name": str(created_user["last_name"]),
        "role": normalize_role(created_user.get("role")),
        "caller_id": created_user.get("caller_id")
    }

@router.get("/users/{user_id}", response_model=UserResponse)
@monitor(name="api.users.get_user")
async def get_user(user_id: str, token: str = Depends(oauth2_scheme)):
    username = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {username}")
    user = user_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": str(user["_id"]),
        "clerk_id": str(user.get("clerk_id", "")),
        "email": str(user["email"]),
        "phone_number": user.get("phone_number"),
        "first_name": str(user["first_name"]),
        "last_name": str(user["last_name"]),
        "role": normalize_role(user.get("role")),
        "caller_id": user.get("caller_id"),
        "organization_id": str(user.get("organization_id", "")),
    }

@router.put("/users/{user_id}", response_model=UserResponse)
@monitor(name="api.users.update_user")
async def update_user(
    user_id: str, 
    user_update: UserCreate, 
    token: str = Depends(oauth2_scheme)
):
    username = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {username}")
    # Exclude password fields from update to prevent password changes
    update_data = user_update.model_dump(exclude_none=True)
    update_data.pop("password", None)  # Remove password if present
    update_data.pop("hashed_password", None)  # Remove hashed_password if present
    updated_user = user_service.update_user(user_id, update_data)
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": str(updated_user["_id"]),
        "clerk_id": str(updated_user.get("clerk_id", "")),
        "email": str(updated_user["email"]),
        "phone_number": updated_user.get("phone_number"),
        "first_name": str(updated_user["first_name"]),
        "last_name": str(updated_user["last_name"]),
        "role": normalize_role(updated_user.get("role")),
        "caller_id": updated_user.get("caller_id"),
        "organization_id": str(updated_user.get("organization_id", "")),
    }

@router.delete("/users/{user_id}")
@monitor(name="api.users.delete_user")
async def delete_user(user_id: str, token: str = Depends(oauth2_scheme)):
    username = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {username}")
    deleted = user_service.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "User deleted successfully"}
@router.get("/debug/device-info/{user_id}")
@monitor(name="api.users.debug_device_info")
async def debug_device_info(
    user_id: str,
    user_type: str = "agent",
    token: str = Depends(oauth2_scheme)
):
    """Debug endpoint to check device information for a user"""
    username = await normal_authenticate_token(token)
    try:
        device_info_list = await notification__service.get_user_device_info(user_id, user_type)
        return {
            "user_id": user_id,
            "user_type": user_type,
            "device_count": len(device_info_list),
            "devices": device_info_list
        }
    except Exception as e:
        logger.error(f"Failed to debug device info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/debug/user-tokens/{user_id}")
@monitor(name="api.users.debug_user_tokens")
async def debug_user_tokens(
    user_id: str,
    active_only: bool = True,
    token: str = Depends(oauth2_scheme)
):
    """Debug endpoint to check stored tokens for a user"""
    username = await normal_authenticate_token(token)
    try:
        tokens = await token_storage_service.get_user_tokens(user_id, active_only=active_only)
        active_count = await token_storage_service.get_active_token_count(user_id)
        return {
            "user_id": user_id,
            "active_token_count": active_count,
            "total_tokens": len(tokens),
            "tokens": tokens
        }
    except Exception as e:
        logger.error(f"Failed to debug user tokens: {e}")
        raise HTTPException(status_code=500, detail=str(e))


