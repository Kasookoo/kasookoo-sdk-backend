# models.py - Pydantic Models for Request/Response
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum

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
    DRIVER="driver"

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
    caller_id: str
    callee_id: str
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

# --- Define the request data model for validation ---
class ConnectCallersRequest(BaseModel):
    sip_uri1: str
    sip_uri2: str


class CallerTokenRequest(TokenRequest):
    caller_user_id: Optional[str] = None
    called_user_id: Optional[str] = None
    device_type: Optional[str] = None
    is_push_notification: bool = True
    is_call_recording: Optional[bool] = True

class CalledTokenRequest(TokenRequest):
    called_user_id: Optional[str] = None
    is_call_recording: Optional[bool] = True


class RejectCallTokenRequest(TokenRequest):
    caller_user_id: str
    called_user_id: str

class AnonymousCallerTokenRequest(BaseModel):
    room_name: str
    participant_identity: str
    participant_identity_name: Optional[str] = "Anonymous"
    participant_identity_type: Optional[str] = "caller"
    called_user_id: Optional[str] = None
    device_type: Optional[str] = None
    is_push_notification: bool = True
    is_call_recording: Optional[bool] = True

class MessagingTokenRequest(TokenRequest):
    sender_user_id: str
    receiver_user_id: str
    device_type: Optional[str] = None
    is_push_notification: bool = True

class CallerTokenResponse(BaseModel):
    caller: TokenResponse
    called: TokenResponse


class AssociatedNumberBase(BaseModel):
    phone_number: str
    user_id: str
    label: Optional[str] = None
    is_active: bool = True


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


class SendMessageRequest(BaseModel):
    sender_user_id: str
    receiver_user_id: str
    room_name: str
    message: str
    message_type: Optional[str] = "text"
    metadata: Optional[Dict[str, Any]] = None
    is_push_notification: bool = True


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_user_id: str
    receiver_user_id: str
    room_name: str
    message: str
    message_type: str
    metadata: Optional[Dict[str, Any]] = None
    created_at: str
    read_at: Optional[str] = None


class ConversationInfo(BaseModel):
    conversation_id: str
    room_name: str
    participant_user_id: str
    participant_name: Optional[str] = None
    participant_email: Optional[str] = None
    last_message: Optional[str] = None
    last_message_at: Optional[str] = None
    unread_count: int = 0
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    conversations: List[ConversationInfo] = Field(default_factory=list)
    total: int


class MessageListResponse(BaseModel):
    messages: List[MessageResponse] = Field(default_factory=list)
    total: int
    conversation_id: str
    messaging_tokens: Optional[TokenResponse] = None


class MarkMessagesReadRequest(BaseModel):
    conversation_id: str
    message_ids: Optional[List[str]] = None

