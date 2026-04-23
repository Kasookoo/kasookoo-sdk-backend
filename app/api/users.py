import logging
import random
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from app.api.auth import (
    oauth2_scheme,
    authenticate_token as normal_authenticate_token,
    get_organization_id,
)
from app.services import user_service, notification__service, organization_service
from app.services.token_storage_service import token_storage_service
from app.utils.performance_monitor import monitor

ALLOWED_USER_ROLES = ("customer", "driver", "admin")


def normalize_role(role: Optional[str]) -> Literal["customer", "driver", "admin"]:
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
        "drver": "driver",
        "diver": "driver",
        "admn": "admin",
        "administrator": "admin",
    }
    
    if role_lower in typo_map:
        return typo_map[role_lower]  # type: ignore
    
    # Default to customer if unrecognized
    return "customer"


def format_device_info(device_info: dict) -> str:
    """Format device information into a readable string"""
    if not device_info or not isinstance(device_info, dict):
        return "Unknown Device"
    
    device_parts = []
    
    # Device type - try multiple possible keys
    device_type = (device_info.get("device_type") or 
                  device_info.get("type") or 
                  device_info.get("platform", "")).lower()
    
    if device_type == "web":
        device_parts.append("Web Browser")
    elif device_type == "android":
        device_parts.append("Android Device")
    elif device_type == "ios":
        device_parts.append("iOS Device")
    elif device_type:
        device_parts.append(device_type.title())
    else:
        # Try to infer from other fields
        if device_info.get("browser"):
            device_parts.append("Web Browser")
        elif device_info.get("model"):
            device_parts.append("Mobile Device")
        else:
            device_parts.append("Unknown Device")
    
    # Browser info (for web)
    if device_type == "web" or device_info.get("browser"):
        browser = device_info.get("browser", "")
        if browser and browser.lower() != "unknown":
            device_parts.append(f"({browser})")
    
    # OS info
    os_info = device_info.get("os") or device_info.get("operating_system", "")
    if os_info and os_info.lower() != "unknown":
        device_parts.append(f"on {os_info}")
    
    # Device model (for mobile)
    if device_type in ["android", "ios"] or device_info.get("model"):
        model = device_info.get("model", "")
        if model and model.lower() != "unknown":
            device_parts.append(f"({model})")
    
    # Location (if available)
    location = device_info.get("location") or device_info.get("city", "")
    if location and location.lower() != "unknown":
        device_parts.append(f"from {location}")
    
    # IP address (if available)
    ip_address = device_info.get("ip_address") or device_info.get("ip", "")
    if ip_address and ip_address.lower() != "unknown":
        device_parts.append(f"IP: {ip_address}")
    
    # Last seen (if available)
    last_seen = device_info.get("last_seen") or device_info.get("last_active", "")
    if last_seen and last_seen.lower() != "unknown":
        device_parts.append(f"last seen: {last_seen}")
    
    # If we still don't have meaningful info, try to get any available info
    if len(device_parts) <= 1:  # Only device type
        # Try to get any non-empty string values
        for key, value in device_info.items():
            if isinstance(value, str) and value.strip() and value.lower() not in ["unknown", "null", "none", ""]:
                device_parts.append(f"{key}: {value}")
                break
    
    result = " ".join(device_parts) if device_parts else "Unknown Device"
    
    # If result is still just "Unknown Device", try to provide more context
    if result == "Unknown Device":
        # Check if we have any device token info
        device_token = device_info.get("device_token", "")
        if device_token:
            result = f"Device (Token: {device_token[:8]}...)"
        else:
            result = "Active Device"
    
    return result


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

class UserCreate(BaseModel):
    email: EmailStr
    phone_number: Optional[str] = None
    first_name: str
    last_name: str    
    password: Optional[str] = None
    role: Literal["customer", "driver", "admin"] = "customer"
    caller_id: Optional[str] = None

class UserResponse(BaseModel):
    id: str
    email: EmailStr
    phone_number: Optional[str] = None
    clerk_id: Optional[str] = None
    first_name: str
    last_name: str
    role: Literal["customer", "driver", "admin"] = "customer"
    caller_id: Optional[str] = None
    organization_id: Optional[str] = None

    #customer_id: Optional[str] = None
    #customer_name: Optional[str] = None




# Login/logout endpoints moved to app.api.auth module; use POST /api/v1/sdk/auth/client-sessions to mint JWTs.

async def logout_all_devices(user_id: str, user_role: str) -> dict:
    """
    Logout user from all devices by:
    1. Deactivating all FCM tokens (bulk operation)
    2. Note: JWT tokens are stateless and cannot be invalidated without server-side tracking.
       Tokens already issued will remain valid until they expire. To fully invalidate tokens,
       you would need to implement a token tracking system in the database.
    """
    try:
        logger.info(f"Logging out user {user_id} from all devices")
        
        # Deactivate all FCM tokens in bulk
        from datetime import datetime
        
        # Access the collection through the service
        collection = notification__service.notification_tokens_collection
        
        result = await collection.update_many(
            {
                "user_id": user_id,
                "user_type": user_role,
                "is_active": True
            },
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        deactivated_count = result.modified_count
        
        logger.info(f"Successfully deactivated {deactivated_count} FCM token(s) for user {user_id}")
        
        # Deactivate all stored JWT tokens for the user
        try:
            tokens_deactivated = await token_storage_service.deactivate_user_tokens(user_id)
            logger.info(f"Deactivated {tokens_deactivated} stored JWT token(s) for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to deactivate stored tokens: {e}")
            tokens_deactivated = 0
        
        return {
            "success": True,
            "message": f"Logged out from {deactivated_count} device(s)",
            "devices_logged_out": deactivated_count,
            "fcm_tokens_deactivated": deactivated_count,
            "jwt_tokens_deactivated": tokens_deactivated
        }
    except Exception as e:
        logger.error(f"Failed to logout all devices for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to logout from all devices: {str(e)}")

@router.post("/users/logout")
@monitor(name="api.users.user_logout")
async def user_logout(token: str = Depends(oauth2_scheme)):
    """
    Logout endpoint. Deactivates the token in the database.
    """
    # Deactivate token in database
    try:
        await token_storage_service.deactivate_token(token)
        logger.info(f"Deactivated token in database: {token[:20]}...")
    except Exception as e:
        logger.error(f"Failed to deactivate token in database: {e}")
        # Continue with logout even if database update fails
    
    return {"message": "Logout successful. Token deactivated."}

@router.post("/users/logout-all")
@monitor(name="api.users.user_logout_all")
async def user_logout_all(
    token: str = Depends(oauth2_scheme)
):
    """
    Logout user from all devices. Deactivates all FCM tokens and stored JWT tokens.
    """
    username = await normal_authenticate_token(token)
    try:
        # username from token is the user_id
        user_id = username
        
        # Get user to find role
        user = user_service.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_role = normalize_role(user.get("role"))
        
        # Logout from all devices
        result = await logout_all_devices(user_id, user_role)
        
        # Also deactivate current token
        try:
            await token_storage_service.deactivate_token(token)
            logger.info(f"Deactivated current token: {token[:20]}...")
        except Exception as e:
            logger.error(f"Failed to deactivate current token: {e}")
        
        return {
            "message": "Logged out from all devices successfully",
            **result
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to logout all devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))



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
    current_user_role = "driver"
    if current_user_id:
        current_user = user_service.get_user_by_id(current_user_id)
        if current_user:
            current_user_role = normalize_role(current_user.get("role", "driver"))
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
    # When filtering by driver, exclude the current user from the result
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

@router.get("/random-user", response_model=UserResponse)
@monitor(name="api.users.random_user")
async def random_user(
    search: str = Query(None, description="Search by email, first name, or last name"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(10, ge=1, le=100, description="Max records to return"),
    token: str = Depends(oauth2_scheme),
    organization_id: str = Depends(get_organization_id),
):
    username = await normal_authenticate_token(token)
    logger.info(f"Authenticated user: {username}")
    filters = {}
    if search:
        filters = {
            "$or": [
                {"email": {"$regex": search, "$options": "i"}},
                {"first_name": {"$regex": search, "$options": "i"}},
                {"last_name": {"$regex": search, "$options": "i"}},
            ]
        }
    list_users_result = user_service.list_users(
        filters, skip=skip, limit=limit, organization_id=organization_id
    )
    users = list_users_result[0] if isinstance(list_users_result, tuple) else list_users_result
    if not users:
        raise HTTPException(status_code=404, detail="No users found")
    user = random.choice(users)
    user_id = str(user["_id"])
    if await notification__service.exist_user(str(user["_id"])):
        return {
            "id": user_id,
            "email": user["email"],
            "phone_number": user.get("phone_number"),
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "role": normalize_role(user.get("role")),
            "clerk_id": str(user.get("clerk_id", "")),
            "caller_id": user.get("caller_id"),
            "organization_id": str(user.get("organization_id", "")),
        }
    else:
        # User does not exist in notification service
        raise HTTPException(status_code=404, detail="User not found in notification service")


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

class SignInAttempt(BaseModel):
    data: dict

@router.post("/users/clerk-signin-hook")
@monitor(name="api.users.clerk_signin_hook")
async def clerk_signin_hook(payload: SignInAttempt):
    email = payload.data.get("email_address")
    clerk_id = payload.data.get("user_id")
    user = user_service.get_user_by_email(email)
    if not user:
        return JSONResponse(
            status_code=403,
            content={"message": "User not allowed to sign in"},
        )
    return {"message": "Sign in approved"}

@router.get("/debug/device-info/{user_id}")
@monitor(name="api.users.debug_device_info")
async def debug_device_info(
    user_id: str,
    user_type: str = "driver",
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


