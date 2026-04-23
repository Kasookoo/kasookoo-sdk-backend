# models.py - Pydantic Models for Request/Response
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum


# --- Organization (multi-tenant) ---
class SipOutboundTrunkSettings(BaseModel):
    """Outbound SIP trunk configuration for an organization. Stored in organization.settings.sip_outbound_trunk."""
    name: Optional[str] = Field(None, description="Trunk display name (e.g. My trunk)")
    address: Optional[str] = Field(None, description="SIP provider address (e.g. sip.telnyx.com)")
    auth_username: Optional[str] = Field(None, description="SIP auth username")
    auth_password: Optional[str] = Field(None, description="SIP auth password")
    numbers: Optional[List[str]] = Field(None, description="E.164 numbers the trunk can use (e.g. ['+12135550100'])")
    trunk_id: Optional[str] = Field(None, description="LiveKit SIP trunk ID after creation; can be set manually if trunk created in dashboard")


class SipInboundTrunkSettings(BaseModel):
    """Inbound SIP trunk configuration for an organization. Stored in organization.settings.sip_inbound_trunk."""
    name: Optional[str] = Field(None, description="Trunk display name (e.g. My inbound trunk)")
    numbers: Optional[List[str]] = Field(
        None,
        description="E.164 numbers accepted by this trunk (e.g. ['+15105550100']). Empty list means accept any number (requires allowed_addresses or auth).",
    )
    allowed_addresses: Optional[List[str]] = Field(
        None,
        description="Allowed SIP provider IPs/addresses for inbound calls (provider-dependent).",
    )
    allowed_numbers: Optional[List[str]] = Field(
        None,
        description="Allowed caller phone numbers (E.164). If set, only calls from these numbers are accepted.",
    )
    auth_username: Optional[str] = Field(None, description="Inbound trunk auth username (if your SIP provider supports it)")
    auth_password: Optional[str] = Field(None, description="Inbound trunk auth password (if your SIP provider supports it)")
    krisp_enabled: Optional[bool] = Field(None, description="Enable Krisp noise cancellation for inbound calls")
    metadata: Optional[str] = Field(None, description="Optional trunk metadata")
    trunk_id: Optional[str] = Field(None, description="LiveKit SIP trunk ID after creation; can be set manually if trunk created in dashboard")


class OrganizationSettings(BaseModel):
    """Organization settings. Stored as sub-document under organization.settings in DB."""
    show_user_list: str = Field(
        "agent_customer_list",
        description="When 'agent_customer_list', filter_users API returns the user list; when 'all_user_list', returns all user list.",
    )
    sip_outbound_trunk: Optional[SipOutboundTrunkSettings] = Field(
        None,
        description="Outbound SIP trunk configuration for this organization; used by make_outbound_call when organization_id is set.",
    )
    sip_inbound_trunk: Optional[SipInboundTrunkSettings] = Field(
        None,
        description="Inbound SIP trunk configuration for this organization; used for accepting inbound calls via a SIP provider.",
    )
    contact_center_number: Optional[str] = Field(
        None,
        description="Default/contact center phone number for this organization. Used when no phone_number is provided (e.g. dial). Takes priority over DEFAULT_PHONE_NUMBER from .env.",
    )


class OrganizationBase(BaseModel):
    name: str = Field(..., description="Organization display name")
    slug: str = Field(..., description="Unique slug/code for the organization")
    email: Optional[str] = Field(None, description="Organization contact email")
    phone_number: Optional[str] = Field(None, description="Organization contact phone number")


class OrganizationCreate(OrganizationBase):
    """Create organization request; can include settings (e.g. sip_outbound_trunk) to store in DB."""
    settings: Optional[OrganizationSettings] = Field(None, description="Organization settings including optional sip_outbound_trunk")

    class Config:
        schema_extra = {
            "example": {
                "name": "Acme Corp",
                "slug": "acme-corp",
                "email": "contact@acme.com",
                "phone_number": "+12135551234",
                "settings": {
                    "show_user_list": "same_user_list",
                    "contact_center_number": "+12135559999",
                    "sip_outbound_trunk": {
                        "name": "My trunk",
                        "address": "sip.telnyx.com",
                        "auth_username": "<username>",
                        "auth_password": "<password>",
                        "numbers": ["+12135550100"]
                    }
                }
            }
        }


class OrganizationUpdate(BaseModel):
    """Partial update; only provided fields are updated."""
    name: Optional[str] = Field(None, description="Organization display name")
    slug: Optional[str] = Field(None, description="Unique slug/code for the organization")
    email: Optional[str] = Field(None, description="Organization contact email")
    phone_number: Optional[str] = Field(None, description="Organization contact phone number")
    settings: Optional[OrganizationSettings] = Field(None, description="Organization settings")

    class Config:
        schema_extra = {
            "example": {
                "name": "Updated Org Name",
                "slug": "updated-slug",
                "email": "updated@example.com",
                "phone_number": None,
                "settings": {
                    "show_user_list": "same_user_list",
                    "contact_center_number": "+12135559999",
                    "sip_outbound_trunk": {
                        "name": "My trunk",
                        "address": "sip.telnyx.com",
                        "auth_username": "<username>",
                        "auth_password": "<password>",
                        "numbers": ["+12135550100"],
                        "trunk_id": "ST_xxxx"
                    }
                }
            }
        }


class OrganizationResponse(OrganizationBase):
    id: str = Field(..., description="Organization ID")
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    settings: Optional[OrganizationSettings] = None

    class Config:
        from_attributes = True


class OrganizationSignupRequest(BaseModel):
    """Request body for organization signup: creates a new org and its first admin user."""
    organization_name: str = Field(..., description="Organization display name")
    organization_slug: str = Field(..., description="Unique slug for the organization (e.g. acme-corp)")
    organization_email: Optional[str] = Field(None, description="Organization contact email")
    organization_phone_number: Optional[str] = Field(None, description="Organization contact phone")
    organization_settings: Optional[OrganizationSettings] = Field(None, description="Organization settings (e.g. sip_outbound_trunk) to store")
    admin_email: str = Field(..., description="Admin user email (used for login)")
    admin_password: str = Field(..., description="Admin user password (plain or SHA-256 hex digest)")
    admin_first_name: str = Field(..., description="Admin user first name")
    admin_last_name: str = Field(..., description="Admin user last name")
    admin_phone_number: Optional[str] = Field(None, description="Admin user phone number")


class AssociatedNumberBase(BaseModel):
    phone_number: str = Field(..., description="Inbound trunk phone number in E.164 format")
    user_id: str = Field(..., description="Mapped user ID who should receive incoming WebRTC call")
    label: Optional[str] = Field(None, description="Optional display label for this number mapping")
    is_active: bool = Field(True, description="Whether this mapping is active")


class AssociatedNumberCreate(AssociatedNumberBase):
    pass


class AssociatedNumberUpdate(BaseModel):
    phone_number: Optional[str] = None
    user_id: Optional[str] = None
    label: Optional[str] = None
    is_active: Optional[bool] = None


class AssociatedNumberResponse(AssociatedNumberBase):
    id: str
    organization_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class CallStatus(str, Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    ENDED = "ended"
    RECORDING = "recording"

class RecordingStatus(str, Enum):
    NOT_STARTED = "not_started"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"

class ParticipantType(str, Enum):
    CALLER = "caller"
    CALLEE = "callee"
    MONITOR = "monitor"
    RECORDER = "recorder"
    ADMIN = "admin"
    CUSTOMER = "customer"
    DRIVER = "driver"

# --- Define the request data model for validation ---
class ConnectCallersRequest(BaseModel):
    sip_uri1: str
    sip_uri2: str

# --- Define the request data model ---
class TokenRequest(BaseModel):
    room_name: str
    participant_identity: str
    participant_identity_name: Optional[str] = None
    participant_identity_type: Optional[str] = None

class CallerTokenRequest(TokenRequest):
    caller_user_id: Optional[str] = None
    called_user_id: Optional[str] = None
    device_type: Optional[str] = None
    is_push_notification: bool = True
    is_call_recording: Optional[bool] = True


class AnonymousCallerTokenRequest(BaseModel):
    """Request for call tokens when the caller is anonymous (not in user DB); callee must exist in user table."""
    room_name: str = Field(..., description="LiveKit room name")
    participant_identity: str = Field(..., description="Anonymous caller identity (e.g. session or device id)")
    participant_identity_name: Optional[str] = Field("Anonymous", description="Display name for the anonymous caller")
    participant_identity_type: Optional[str] = Field("caller", description="Participant type for the caller")
    called_user_id: Optional[str] = Field(None, description="User ID of the callee. If not provided, uses organization's anonymous_guest_call_admin_email to resolve admin user.")
    device_type: Optional[str] = None
    is_push_notification: bool = True
    is_call_recording: Optional[bool] = True

class CalledTokenRequest(TokenRequest):
    called_user_id: Optional[str] = None
    is_call_recording: Optional[bool] = True


class RejectCallTokenRequest(TokenRequest):
    caller_user_id: str
    called_user_id: str

class MessagingTokenRequest(TokenRequest):
    sender_user_id: str = Field(..., description="User ID of the message sender")
    receiver_user_id: str = Field(..., description="User ID of the message receiver")
    device_type: Optional[str] = None
    is_push_notification: bool = True

class TokenResponse(BaseModel):
    accessToken: str
    wsUrl: str

class CallerTokenResponse(BaseModel):
    """Response model for get-caller-token endpoint with both caller and called tokens"""
    caller: TokenResponse = Field(..., description="Caller's LiveKit access token")
    called: TokenResponse = Field(..., description="Called participant's LiveKit access token")

# Request Models
class CallRequest(BaseModel):
    caller_id: str = Field(..., description="Unique identifier for the caller")
    callee_id: str = Field(..., description="Unique identifier for the callee")
    room_name: Optional[str] = Field(None, description="Optional custom room name")
    auto_record: bool = Field(False, description="Whether to start recording automatically")
    recording_options: Optional[Dict[str, Any]] = Field(None, description="Recording configuration options")
    caller_participant: Optional[Dict[str, Any]] = Field(None, description="Caller participant data")
    callee_participant: Optional[Dict[str, Any]] = Field(None, description="Callee participant data")
    
    class Config:
        schema_extra = {
            "example": {
                "caller_id": "user123",
                "callee_id": "user456",
                "auto_record": True,
                "recording_options": {
                    "width": 1280,
                    "height": 720,
                    "framerate": 30
                }
            }
        }

class TokenRequest(BaseModel):
    participant_identity: str = Field(..., description="Unique participant identifier")
    participant_identity_name: Optional[str] = Field(None, description="Display name for participant")
    participant_identity_type: Optional[ParticipantType] = Field(ParticipantType.CALLER, description="Type of participant")
    room_name: str = Field(..., description="Room name to join")
    
    class Config:
        schema_extra = {
            "example": {
                "participant_identity": "user123",
                "participant_identity_name": "John Doe",
                "participant_identity_type": "caller",
                "room_name": "call_room_123"
            }
        }

class RecordingRequest(BaseModel):
    room_name: str = Field(..., description="Name of the room to record")
    recording_type: str = Field("composite", description="Type of recording: composite, track, web")
    options: Optional[Dict[str, Any]] = Field(None, description="Recording options")
    
    class Config:
        schema_extra = {
            "example": {
                "room_name": "call_room_123",
                "recording_type": "composite",
                "options": {
                    "width": 1920,
                    "height": 1080,
                    "framerate": 30,
                    "layout": "grid"
                }
            }
        }

# Response Models
class TokenResponse(BaseModel):
    accessToken: str = Field(..., description="JWT access token for LiveKit")
    wsUrl: str = Field(..., description="WebSocket URL for LiveKit connection")
    
class CallResponse(BaseModel):
    success: bool = Field(..., description="Whether the call initiation was successful")
    room_name: str = Field(..., description="Generated or provided room name")
    call_id: str = Field(..., description="Unique call identifier")
    caller_token: str = Field(..., description="Access token for the caller")
    callee_token: str = Field(..., description="Access token for the callee")
    ws_url: str = Field(..., description="WebSocket URL for LiveKit connection")
    status: str = Field(..., description="Current call status")
    auto_record: bool = Field(..., description="Whether auto-recording is enabled")
    recording_will_start: Optional[bool] = Field(None, description="Indicates if recording will start automatically")

class FileResult(BaseModel):
    filename: str = Field(..., description="Name of the recorded file")
    size: int = Field(..., description="File size in bytes")
    location: str = Field(..., description="S3 location of the file")
    download_url: Optional[str] = Field(None, description="Presigned download URL")

class RecordingInfo(BaseModel):
    egress_id: str = Field(..., description="Unique egress/recording identifier")
    status: str = Field(..., description="Current recording status")
    started_at: Optional[int] = Field(None, description="Recording start timestamp")
    ended_at: Optional[int] = Field(None, description="Recording end timestamp")
    file_results: List[FileResult] = Field(default_factory=list, description="List of recorded files")

class CallStatusResponse(BaseModel):
    room_name: str = Field(..., description="Room name")
    caller_id: str = Field(..., description="Caller identifier")
    callee_id: str = Field(..., description="Callee identifier")
    status: CallStatus = Field(..., description="Current call status")
    recording_status: RecordingStatus = Field(..., description="Current recording status")
    participants: List[str] = Field(default_factory=list, description="List of active participants")
    created_at: str = Field(..., description="Call creation timestamp")
    started_at: Optional[str] = Field(None, description="Call start timestamp")
    ended_at: Optional[str] = Field(None, description="Call end timestamp")
    recording_info: Optional[RecordingInfo] = Field(None, description="Recording information if available")

class RecordingStartResponse(BaseModel):
    success: bool = Field(..., description="Whether recording started successfully")
    egress_id: str = Field(..., description="Unique recording identifier")
    room_name: str = Field(..., description="Room being recorded")
    recording_status: str = Field(..., description="Current recording status")
    s3_path: str = Field(..., description="S3 path where recording will be saved")

class RecordingStopResponse(BaseModel):
    success: bool = Field(..., description="Whether recording stopped successfully")
    egress_id: str = Field(..., description="Recording identifier")
    recording_status: str = Field(..., description="Final recording status")
    s3_location: Optional[str] = Field(None, description="Final S3 location of recording")

class CallEndResponse(BaseModel):
    success: bool = Field(..., description="Whether call ended successfully")
    room_name: str = Field(..., description="Room that was ended")
    status: str = Field(..., description="Final call status")
    duration_seconds: float = Field(..., description="Total call duration in seconds")
    recording_s3_location: Optional[str] = Field(None, description="S3 location of recording if available")

class ActiveCallInfo(BaseModel):
    room_name: str
    caller_participant: Dict[str, Any]
    callee_participant: Dict[str, Any]
    status: str
    recording_status: str
    created_at: str

class ActiveCallsResponse(BaseModel):
    active_calls: List[ActiveCallInfo] = Field(default_factory=list)

# WebSocket Message Models
class WebSocketMessage(BaseModel):
    type: str = Field(..., description="Message type")
    room_name: Optional[str] = Field(None, description="Associated room name")
    data: Optional[Dict[str, Any]] = Field(None, description="Message payload")

class StatusUpdateMessage(WebSocketMessage):
    type: str = Field(default="status_update")
    status: str = Field(..., description="Current call status")
    recording_status: str = Field(..., description="Current recording status")
    participants_count: int = Field(..., description="Number of active participants")

# Error Response Models
class ErrorResponse(BaseModel):
    success: bool = Field(default=False)
    error: str = Field(..., description="Error message")
    code: Optional[str] = Field(None, description="Error code")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional error details")

# Messaging Models
class SendMessageRequest(BaseModel):
    sender_user_id: str = Field(..., description="User ID of the message sender")
    receiver_user_id: str = Field(..., description="User ID of the message receiver")
    room_name: str = Field(..., description="LiveKit room name for the conversation")
    message: str = Field(..., description="Message content")
    message_type: Optional[str] = Field("text", description="Type of message (text, image, file, etc.)")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional message metadata")
    is_push_notification: bool = Field(True, description="Whether to send push notification to receiver")
    
    class Config:
        schema_extra = {
            "example": {
                "sender_user_id": "user123",
                "receiver_user_id": "user456",
                "room_name": "chat_room_123",
                "message": "Hello, how are you?",
                "message_type": "text",
                "metadata": {},
                "is_push_notification": True
            }
        }

class MessageResponse(BaseModel):
    id: str = Field(..., description="Message ID")
    conversation_id: str = Field(..., description="Conversation ID")
    sender_user_id: str = Field(..., description="User ID of the sender")
    receiver_user_id: str = Field(..., description="User ID of the receiver")
    room_name: str = Field(..., description="LiveKit room name")
    message: str = Field(..., description="Message content")
    message_type: str = Field(..., description="Type of message")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
    created_at: str = Field(..., description="Message creation timestamp")
    read_at: Optional[str] = Field(None, description="Message read timestamp")

class ConversationInfo(BaseModel):
    conversation_id: str = Field(..., description="Conversation ID")
    room_name: str = Field(..., description="LiveKit room name")
    participant_user_id: str = Field(..., description="Other participant's user ID")
    participant_name: Optional[str] = Field(None, description="Other participant's name")
    participant_email: Optional[str] = Field(None, description="Other participant's email")
    last_message: Optional[str] = Field(None, description="Last message content")
    last_message_at: Optional[str] = Field(None, description="Last message timestamp")
    unread_count: int = Field(0, description="Number of unread messages")
    created_at: str = Field(..., description="Conversation creation timestamp")
    updated_at: str = Field(..., description="Conversation last update timestamp")

class ConversationListResponse(BaseModel):
    conversations: List[ConversationInfo] = Field(default_factory=list, description="List of conversations")
    total: int = Field(..., description="Total number of conversations")

class MessageListResponse(BaseModel):
    messages: List[MessageResponse] = Field(default_factory=list, description="List of messages")
    total: int = Field(..., description="Total number of messages")
    conversation_id: str = Field(..., description="Conversation ID")
    messaging_tokens: Optional[TokenResponse] = Field(None, description="LiveKit messaging token and wsUrl when requested via include_messaging_tokens")

class MarkMessagesReadRequest(BaseModel):
    conversation_id: str = Field(..., description="Conversation ID")
    message_ids: Optional[List[str]] = Field(None, description="Specific message IDs to mark as read. If None, marks all unread messages in conversation")