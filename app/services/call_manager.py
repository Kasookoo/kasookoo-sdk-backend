# services/call_manager.py - WebRTC Call Management Service
import asyncio
import json
import logging
from datetime import datetime, timezone
import os
import traceback
from typing import AsyncIterator, Dict, List, Set, Optional, Any
import uuid
from app.config import MONGO_URI, DB_NAME

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

from app.models.models import (
    CallRequest, CallStatus, RecordingStatus, CallStatusResponse,
    RecordingStartResponse, RecordingStopResponse, CallEndResponse,
    ActiveCallsResponse, ActiveCallInfo, TokenRequest
)
from livekit.api import DeleteRoomRequest
from livekit.protocol.room import ListParticipantsRequest

from app.services.recording_manager import LiveKitS3RecordingManager
from app.services.token_service import TokenService

from app.config import API_HOST
from app.utils.mongodb_org import org_filter
from app.utils.logging_utils import make_json_serializable

logger = logging.getLogger(__name__)


class CallSession:
    """Represents an active call session"""
    
    def __init__(self, room_name: str, caller_participant: Optional[Dict] = None, callee_participant: Optional[Dict] = None, auto_record: bool = False, recording_options: Optional[Dict] = None, organization_id: Optional[str] = None):
        self.room_name = room_name
        self.organization_id: Optional[str] = organization_id
        self.status = CallStatus.WAITING
        self.recording_status = RecordingStatus.NOT_STARTED
        self.egress_id: Optional[str] = None
        self.participants: Set[str] = set()
        self.created_at = datetime.now(timezone.utc)
        self.started_at = datetime.now(timezone.utc)
        self.ended_at = datetime.now(timezone.utc)
        self.duration_seconds = 0.0
        self.recording_file_name: Optional[str] = None
        self.recording_s3_location: Optional[str] = None
        self.recording_started_at: Optional[datetime] = None
        self.recording_ended_at: Optional[datetime] = None
        self.call_id = str(uuid.uuid4())
        self.auto_record: bool = auto_record
        self.recording_options: Optional[Dict] = recording_options
        self.caller_participant: Optional[Dict] = caller_participant
        self.callee_participant: Optional[Dict] = callee_participant
        self.kind: str = "webrtc_to_webrtc"

    def to_dict(self) -> Dict:
        """Convert call session to dictionary"""
        # Ensure participants list includes caller and callee even if set is empty
        participants_list = list(self.participants)
        if not participants_list:
            # If participants set is empty, populate from caller_id and callee_id
            if self.caller_participant:
                caller_id = self.caller_participant.get("id") if isinstance(self.caller_participant, dict) else getattr(self.caller_participant, "identity", None) or getattr(self.caller_participant, "id", None)
                if caller_id:
                    participants_list.append(caller_id)
            if self.callee_participant:
                callee_id = self.callee_participant.get("id") if isinstance(self.callee_participant, dict) else getattr(self.callee_participant, "identity", None) or getattr(self.callee_participant, "id", None)
                if callee_id and callee_id not in participants_list:
                    participants_list.append(callee_id)
        
        # Calculate duration: use stored value if call ended, otherwise calculate current duration
        duration_seconds = self.duration_seconds
        if duration_seconds is None:
            if self.started_at:
                # For active calls, calculate current duration
                if self.ended_at:
                    # Call has ended, calculate from started_at to ended_at
                    duration_seconds = (self.ended_at - self.started_at).total_seconds()
                else:
                    # Call is still active, calculate from started_at to now
                    duration_seconds = (datetime.now(timezone.utc) - self.started_at).total_seconds()
            else:
                # Call hasn't started yet
                duration_seconds = 0.0
        
        out = {
            "room_name": self.room_name,
            "status": self.status.value,
            "participants": participants_list,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": duration_seconds,
            "call_id": self.call_id,
            "caller_participant": self.caller_participant,
            "callee_participant": self.callee_participant,
            "kind": self.kind,
        }
        # Only include recording fields when there is a recording (egress_id set)
        if self.egress_id:
            out["egress_id"] = self.egress_id
            out["recording_status"] = self.recording_status.value
            if self.recording_file_name:
                out["recording_file_name"] = self.recording_file_name
            if self.recording_s3_location:
                out["recording_s3_location"] = self.recording_s3_location
            if self.recording_started_at:
                out["recording_started_at"] = self.recording_started_at.isoformat()
            if self.recording_ended_at:
                out["recording_ended_at"] = self.recording_ended_at.isoformat()
        if self.organization_id:
            out["organization_id"] = str(self.organization_id)
        # Remove all null values
        return {k: v for k, v in out.items() if v is not None and v != "null"}

class WebRTCCallManager:
    """Manages WebRTC call lifecycle and recording"""
    
    def __init__(self, recording_manager: LiveKitS3RecordingManager, token_service: TokenService):
        self.recording_manager = recording_manager
        self.token_service = token_service
        self.active_calls: Dict[str, CallSession] = {}
        self.participant_to_room: Dict[str, str] = {}

        # Add MongoDB client and collection
        # Initialize MongoDB client
        self.mongo_client = client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.mongo_client[DB_NAME]
        self.calls_collection = self.db["call_sessions"]

    async def _delete_livekit_room(self, room_name: str) -> bool:
        """
        Delete a LiveKit room, disconnecting all participants.
        Returns True if successful, False otherwise.
        """
        if not room_name or not room_name.strip():
            logger.warning({"event": "skip_delete_empty_room", "room_name": room_name})
            return False
        try:
            logger.info({"event": "deleting_livekit_room", "room_name": room_name})
            await self.recording_manager.livekit_client.room.delete_room(DeleteRoomRequest(room=room_name))
            logger.info({"event": "livekit_room_deleted", "room_name": room_name})
            return True
        except Exception as e:
            error_str = str(e)
            if "not_found" in error_str.lower() or "does not exist" in error_str.lower() or "404" in error_str:
                logger.info({
                    "event": "room_already_deleted",
                    "room_name": room_name,
                    "message": "Room does not exist (may have been already deleted)"
                })
                return True
            logger.error({
                "event": "failed_to_delete_livekit_room",
                "room_name": room_name,
                "error": error_str
            })
            return False

    def _clean_participant_data(self, participant: Dict) -> Dict:
        """
        Clean participant data by removing sensitive/unnecessary fields and extracting SIP data.
        
        Removes: permission, tracks, version
        For SIP participants: extracts sip.phoneNumber and sip.trunkPhoneNumber from attributes
        For non-SIP participants (no kind field): fetches user data by identity and adds email, phone_number, role
        """
        if not isinstance(participant, dict):
            return participant
        
        # Create a copy to avoid mutating the original
        cleaned = participant.copy()
        
        # Remove unwanted fields
        cleaned.pop("permission", None)
        cleaned.pop("tracks", None)
        cleaned.pop("version", None)
        
        # Handle SIP participants
        if cleaned.get("kind") == "SIP":
            attributes = cleaned.get("attributes", {})
            if isinstance(attributes, dict):
                # Extract SIP phone number and trunk phone number
                sip_phone_number = attributes.get("sip.phoneNumber")
                sip_trunk_phone_number = attributes.get("sip.trunkPhoneNumber")
                
                if sip_phone_number:
                    cleaned["phone_number"] = sip_phone_number
                if sip_trunk_phone_number:
                    cleaned["caller_id"] = sip_trunk_phone_number
            
            # Remove attributes after extraction
            cleaned.pop("attributes", None)       
            
        else:
            # For non-SIP participants (WebRTC participants), fetch user data
            # Use identity field as user_id
            participant_identity = cleaned.get("identity")
            if participant_identity:
                try:
                    # Lazy import to avoid circular import issues
                    from app.services import user_service as _user_service
                    user = _user_service.get_user_by_id(participant_identity)
                    if user:
                        # Add user data to participant
                        if user.get("email"):
                            cleaned["email"] = user.get("email")
                        if user.get("phone_number"):
                            cleaned["phone_number"] = user.get("phone_number")
                        if user.get("role"):
                            cleaned["role"] = user.get("role")
                except Exception as e:
                    # Log error but don't fail - participant data will be stored without user info
                    logger.warning({
                        "event": "failed_to_fetch_user_data_for_participant",
                        "participant_identity": participant_identity,
                        "error": str(e)
                    })
        
        return cleaned

    async def initiate_call_session(self, request: CallRequest, organization_id: Optional[str] = None):
        """Initiate a new WebRTC call between two participants."""
        if not organization_id:
            from app.services import organization_service
            default_org = organization_service.get_or_create_default_organization()
            organization_id = str(default_org["_id"])
        try:
            # Generate unique room name if not provided
            if not request.room_name:
                timestamp = int(datetime.now().timestamp())
                room_name = f"call_{request.caller_id}_{request.callee_id}_{timestamp}"
            else:
                room_name = request.room_name

            logger.info({"event": "initiating_call_session", "room_name": room_name, "caller_id": request.caller_id, "callee_id": request.callee_id})

            # Check if room already exists
            if room_name in self.active_calls:
                logger.warning({"event": "call_room_already_exists", "room_name": room_name, "msg": "Call room already exists in active_calls"})
                await self.update_call_session(request, organization_id=organization_id)
                logger.info({"event": "call_session_updated_via_initiate", "room_name": room_name})
                return

            # Create call session
            call_session = CallSession(
                room_name=room_name,
                caller_participant=request.caller_participant,
                callee_participant=request.callee_participant,
                auto_record=request.auto_record,
                recording_options=request.recording_options,
                organization_id=organization_id,
            )
            self.active_calls[room_name] = call_session
            
            # Store participant mappings
            self.participant_to_room[request.caller_id] = room_name
            if request.callee_id:
                self.participant_to_room[request.callee_id] = room_name
            
            # Persist initial call record to MongoDB (call_sessions collection)
            try:
                call_data = call_session.to_dict()
                update_op = {"$set": call_data}
                if not call_data.get("egress_id"):
                    update_op["$unset"] = {k: "" for k in ("egress_id", "recording_file_name", "recording_s3_location", "recording_started_at", "recording_ended_at", "recording_status")}
                await self.calls_collection.update_one(
                    {"room_name": room_name},
                    update_op,
                    upsert=True
                )
                logger.info({"event": "call_session_saved_to_mongodb", "room_name": room_name})
            except Exception as db_error:
                logger.error({"event": "save_call_session_to_mongodb_failed", "room_name": room_name, "error": str(db_error)})
            
            logger.info({"event": "call_session_initiated", "room_name": room_name, "active_calls_count": len(self.active_calls)})
            
        except Exception as e:
            # Cleanup on error
            if 'room_name' in locals() and room_name in self.active_calls:
                del self.active_calls[room_name]
            if request.caller_id in self.participant_to_room:
                del self.participant_to_room[request.caller_id]
            if request.callee_id in self.participant_to_room:
                del self.participant_to_room[request.callee_id]
            logger.error({"event": "initiate_call_session_failed", "room_name": room_name if 'room_name' in locals() else None, "error": str(e)})           
            raise

    async def update_call_session(self, request: CallRequest, organization_id: Optional[str] = None):
        """Update an existing WebRTC call session."""
        try:
            room_name = request.room_name
            if room_name in self.active_calls and organization_id:
                self.active_calls[room_name].organization_id = organization_id
            logger.info({"event": "updating_call_session", "room_name": room_name, "new_callee": request.callee_id})
            
            logger.info({"event": "active_calls_list", "active_calls": list(self.active_calls.keys())})
            # Check if room already exists
            if room_name in self.active_calls:              
                # Update call session
                call_session = self.active_calls[room_name]
                call_session.started_at = datetime.now(timezone.utc)
                # Update auto_record and recording_options if provided
                if hasattr(request, 'auto_record'):
                    call_session.auto_record = request.auto_record
                if hasattr(request, 'recording_options') and request.recording_options:
                    call_session.recording_options = request.recording_options
                self.active_calls[room_name] = call_session
                
                # Update participant mapping
                self.participant_to_room[request.callee_id] = room_name
                
                logger.info({"event": "call_session_updated", "room_name": room_name})
            else:
                logger.warning({"event": "room_not_found_during_update", "room_name": room_name, "msg": "Room not found in active_calls during update"})
                await self.initiate_call_session(request, organization_id=organization_id)
                
        except Exception as e:
            logger.error({"event": "update_call_session_failed", "room_name": room_name, "error": str(e)})
            raise



    async def _delayed_recording_start(self, room_name: str, options: Dict, delay: int = 5):
        """Start recording after a delay to ensure participants have joined"""
        await asyncio.sleep(delay)
        try:
            # Check if call session still exists and recording hasn't started
            if room_name not in self.active_calls:
                logger.info({
                    "event": "recording_skipped_no_active_call",
                    "room_name": room_name,
                    "note": "Call ended or never tracked before recording delay elapsed (normal on failed/short calls)",
                })
                return
            
            call_session = self.active_calls[room_name]
            
            # Only start recording if it hasn't started yet
            if call_session.recording_status == RecordingStatus.NOT_STARTED:
                await self.start_call_recording(room_name, options)
                logger.info({"event": "auto_recording_started", "room_name": room_name})
            else:
                logger.info({
                    "event": "recording_already_started_or_in_progress",
                    "room_name": room_name,
                    "recording_status": call_session.recording_status.value
                })
        except Exception as e:
            logger.error({"event": "auto_recording_start_failed", "room_name": room_name, "error": str(e)})

    async def start_call_recording(self, room_name: str, options: Dict = None) -> RecordingStartResponse:
        """Start recording an active call"""
        if room_name not in self.active_calls:
            raise ValueError("Call not found")

        call_session = self.active_calls[room_name]

        if call_session.recording_status != RecordingStatus.NOT_STARTED:
            raise ValueError(f"Recording already {call_session.recording_status.value}")

        call_session.recording_status = RecordingStatus.STARTING

        try:
            # Default recording options for calls
            recording_options = {
                "width": 1280,
                "height": 720,
                "framerate": 30,
                "audio_only": False,
                "video_only": False
            }
            if options:
                recording_options.update(options)

            # Generate S3 path for the recording
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
            s3_path = f"call-recordings/{room_name}/{timestamp}.mp4"
            #s3_path = f"call-recordings/{room_name}/{timestamp}"
            call_session.recording_s3_location = s3_path
            call_session.recording_file_name = timestamp

            # Start recording
            egress_id = await self.recording_manager.start_recording_to_s3(
                room_name=room_name,
                s3_path=s3_path,
                recording_options=recording_options
            )            
            call_session.egress_id = egress_id
            call_session.recording_status = RecordingStatus.ACTIVE

            logger.info({"event": "recording_started", "room_name": room_name, "egress_id": egress_id, "recording_s3_location": s3_path, "recording_file_name": timestamp})
            
            # Save initial recording info to MongoDB
            try:
                call_data = call_session.to_dict()
                await self.calls_collection.update_one(
                    {"room_name": room_name},
                    {"$set": call_data},
                    upsert=True
                )
                logger.info({"event": "recording_start_info_saved_to_mongodb", "room_name": room_name, "egress_id": egress_id})
            except Exception as db_error:
                logger.error({"event": "save_recording_start_info_to_mongodb_failed", "room_name": room_name, "error": str(db_error)})

            return RecordingStartResponse(
                success=True,
                egress_id=egress_id,
                room_name=room_name,
                recording_status=call_session.recording_status.value,
                s3_path=s3_path
            )

        except Exception as e:
            call_session.recording_status = RecordingStatus.FAILED
            logger.error({"event": "start_recording_failed", "room_name": room_name, "error": str(e)})
            raise

    async def stop_call_recording(self, room_name: str) -> RecordingStopResponse:
        """Stop recording an active call"""
        if room_name not in self.active_calls:
            raise ValueError("Call not found")

        call_session = self.active_calls[room_name]

        if not call_session.egress_id:
            raise ValueError("No active recording found")

        call_session.recording_status = RecordingStatus.STOPPING

        try:
            success = await self.recording_manager.stop_recording(call_session.egress_id)

            if success:
                call_session.recording_status = RecordingStatus.COMPLETED

                # Get recording info for S3 location
                #recording_info = await self.recording_manager.get_recording_status(call_session.egress_id, room_name)
                #if recording_info and recording_info.get("file_results"):
                #    call_session.recording_s3_location = recording_info["file_results"][0]["location"]
            else:
                call_session.recording_status = RecordingStatus.FAILED

            return RecordingStopResponse(
                success=success,
                egress_id=call_session.egress_id,
                recording_status=call_session.recording_status.value,
                s3_location=call_session.recording_s3_location
            )

        except Exception as e:
            call_session.recording_status = RecordingStatus.FAILED
            logger.error({"event": "stop_recording_error", "room_name": room_name, "error": str(e), "traceback": traceback.format_exc()})
            raise

    def _build_call_sessions_query(
        self,
        search: str = None,
        caller_id: str = None,
        callee_id: str = None,
        status: str = None,
        kind: str = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        date_field: str = "created_at",
        organization_id: Optional[str] = None,
    ) -> dict:
        query = org_filter(organization_id) if organization_id else {}
        conditions = [query] if query else []
        if caller_id:
            conditions.append({"caller_participant.identity": caller_id})
        if callee_id:
            conditions.append({"callee_participant.identity": callee_id})
        if kind:
            conditions.append({"kind": kind})
        if status:
            conditions.append({"status": status})
        if date_from or date_to:
            date_query = {}
            if date_from:
                if isinstance(date_from, datetime):
                    date_query["$gte"] = date_from.isoformat()
                else:
                    date_query["$gte"] = date_from
            if date_to:
                if isinstance(date_to, datetime):
                    date_query["$lte"] = date_to.isoformat()
                else:
                    date_query["$lte"] = date_to
            if date_query:
                conditions.append({date_field: date_query})
        if search:
            search_conditions = {
                "$or": [
                    {"room_name": {"$regex": search, "$options": "i"}},
                    {"call_id": {"$regex": search, "$options": "i"}},
                    {"caller_participant.identity": {"$regex": search, "$options": "i"}},
                    {"caller_participant.name": {"$regex": search, "$options": "i"}},
                    {"caller_participant.email": {"$regex": search, "$options": "i"}},
                    {"caller_participant.phone_number": {"$regex": search, "$options": "i"}},
                    {"callee_participant.identity": {"$regex": search, "$options": "i"}},
                    {"callee_participant.name": {"$regex": search, "$options": "i"}},
                    {"callee_participant.email": {"$regex": search, "$options": "i"}},
                    {"callee_participant.phone_number": {"$regex": search, "$options": "i"}},
                ]
            }
            conditions.append(search_conditions)
        if len(conditions) == 1:
            return conditions[0]
        if len(conditions) > 1:
            return {"$and": conditions}
        return {}

    async def list_call_session(
        self,
        search: str = None,
        caller_id: str = None,
        callee_id: str = None,
        status: str = None,
        kind: str = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        date_field: str = "created_at",
        skip: int = 0,
        limit: int = 10,
        organization_id: Optional[str] = None,
    ) -> tuple:
        """
        List call sessions with pagination, search, and filtering. Scoped by organization_id when provided.
        Returns (sessions, total) where total is the count of all matching records for pagination.
        """
        try:
            query = self._build_call_sessions_query(
                search=search,
                caller_id=caller_id,
                callee_id=callee_id,
                status=status,
                kind=kind,
                date_from=date_from,
                date_to=date_to,
                date_field=date_field,
                organization_id=organization_id,
            )
            logger.info({"event": "list_call_session_query", "has_org_filter": bool(organization_id)})
            
            # Get total count for pagination (same query, no skip/limit)
            total = await self.calls_collection.count_documents(query)
            
            call_sessions = []
            # Sort by created_at in descending order (newest first)
            cursor = self.calls_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
            for call in await cursor.to_list(length=limit):
                if call.get("recording_file_name") and call.get("room_name"):
                    call["recording_download_url"] = f"{API_HOST}/sdk/download-recording/{call['room_name']}/{call['recording_file_name']}"
                # Convert ObjectId and other non-JSON types for API serialization
                call_sessions.append(make_json_serializable(call))
            return call_sessions, total
        except Exception as e:
            logger.error({"event": "list_call_sessions_failed", "error": str(e)})
            raise

    async def iter_call_session_documents(
        self,
        search: str = None,
        caller_id: str = None,
        callee_id: str = None,
        status: str = None,
        kind: str = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        date_field: str = "created_at",
        organization_id: Optional[str] = None,
        max_rows: int = 50000,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream call session documents matching the same filters as list_call_session (newest first).
        Stops after max_rows rows to bound memory and response size.
        """
        query = self._build_call_sessions_query(
            search=search,
            caller_id=caller_id,
            callee_id=callee_id,
            status=status,
            kind=kind,
            date_from=date_from,
            date_to=date_to,
            date_field=date_field,
            organization_id=organization_id,
        )
        cursor = self.calls_collection.find(query).sort("created_at", -1)
        n = 0
        async for doc in cursor:
            if n >= max_rows:
                break
            yield make_json_serializable(doc)
            n += 1

    async def dashboard_cdr_metrics(
        self,
        organization_id: Optional[str],
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        date_field: str = "created_at",
    ) -> Dict[str, Any]:
        """
        Aggregated CDR counts for dashboards (e.g. pie charts): totals and breakdowns by
        status, call kind, and recording_status. Same org and date scoping as list_call_session.
        """
        query = self._build_call_sessions_query(
            date_from=date_from,
            date_to=date_to,
            date_field=date_field,
            organization_id=organization_id,
        )
        pipeline = [
            {"$match": query},
            {
                "$facet": {
                    "total_count": [{"$count": "total"}],
                    "by_status": [
                        {"$group": {"_id": {"$ifNull": ["$status", "unknown"]}, "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                    ],
                    "by_kind": [
                        {"$group": {"_id": {"$ifNull": ["$kind", "unknown"]}, "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                    ],
                    "by_recording_status": [
                        {"$group": {"_id": {"$ifNull": ["$recording_status", "none"]}, "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                    ],
                }
            },
        ]
        rows = await self.calls_collection.aggregate(pipeline).to_list(length=1)
        facet = rows[0] if rows else {}
        tc = facet.get("total_count") or []
        total = int(tc[0]["total"]) if tc else 0

        def _slices(key: str) -> List[Dict[str, Any]]:
            slices: List[Dict[str, Any]] = []
            for x in facet.get(key) or []:
                label = x.get("_id")
                if label is None:
                    label = "unknown"
                slices.append({"label": str(label), "count": int(x["count"])})
            return slices

        return {
            "total": total,
            "by_status": _slices("by_status"),
            "by_kind": _slices("by_kind"),
            "by_recording_status": _slices("by_recording_status"),
        }

    async def end_call(self, room_name: str) -> CallEndResponse:
        """End a call and cleanup resources"""
        logger.info({"event": "attempting_to_end_call", "room_name": room_name})
        logger.info({"event": "active_calls_list", "active_calls": list(self.active_calls.keys())})
        
        if room_name not in self.active_calls:
            # Check if call exists in database as fallback
            try:
                existing_call = await self.calls_collection.find_one({"room_name": room_name})
                if existing_call:
                    logger.warning({"event": "call_not_in_active_but_in_db", "room_name": room_name, "status": existing_call.get('status', 'unknown'), "msg": "Call not found in active_calls but exists in database"})
                    # If call is already ended in database, return success
                    if existing_call.get('status') == 'ended':
                        logger.info({"event": "call_already_ended_in_db", "room_name": room_name, "msg": "Call already ended in database, returning success"})
                        return CallEndResponse(
                            success=True,
                            room_name=room_name,
                            status="ended",
                            duration_seconds=existing_call.get('duration_seconds', 0),
                            recording_s3_location=existing_call.get('recording_s3_location')
                        )
                    # Call exists but not in active_calls (e.g. multi-worker, webhook hit different worker)
                    await self._update_ended_call_from_doc(room_name, existing_call)
                    updated = await self.calls_collection.find_one({"room_name": room_name})
                    final_duration = updated.get('duration_seconds', 0) if updated else existing_call.get('duration_seconds', 0)
                    return CallEndResponse(
                        success=True,
                        room_name=room_name,
                        status="ended",
                        duration_seconds=final_duration,
                        recording_s3_location=existing_call.get('recording_s3_location')
                    )
                else:
                    # Call not found in active_calls or database
                    # This can happen when room_finished webhook is received for a call that was never properly tracked
                    # or was already cleaned up. Return success to avoid errors in background tasks.
                    logger.warning({"event": "call_not_found_anywhere", "room_name": room_name, "msg": "Call not found in active_calls or database, treating as already cleaned up"})
                    return CallEndResponse(
                        success=True,
                        room_name=room_name,
                        status="ended",
                        duration_seconds=0,
                        recording_s3_location=None
                    )
            except Exception as e:
                logger.error({"event": "database_check_error", "room_name": room_name, "error": str(e), "msg": "Error checking database for call"})
                # If database check fails, assume call is already cleaned up to avoid errors in background tasks
                logger.warning({"event": "database_check_failed", "room_name": room_name, "msg": "Database check failed, treating as already cleaned up"})
                return CallEndResponse(
                    success=True,
                    room_name=room_name,
                    status="ended",
                    duration_seconds=0,
                    recording_s3_location=None
                )

        call_session = self.active_calls[room_name]

        try:
            # Stop recording if active
            if call_session.egress_id and call_session.recording_status == RecordingStatus.ACTIVE:
                await self.stop_call_recording(room_name)

            call_session.status = CallStatus.ENDED
            
            # Single participant: call was never answered (e.g. callee never joined) - duration 0, ended_at = started_at
            if len(call_session.participants) == 1:
                call_session.duration_seconds = 0.0
                call_session.ended_at = call_session.started_at or call_session.created_at
                logger.info({
                    "event": "call_ended_single_participant",
                    "room_name": room_name,
                    "duration_seconds": 0,
                    "note": "Call ended with only one participant - duration set to 0, ended_at = started_at"
                })
            else:
                # Duration calculation priority (works even when recording is disabled):
                # Priority 1: Use recording file duration from LiveKit API/webhook (from MP4 file metadata)
                #            LiveKit reads the MP4 file metadata (MOOV atom) and provides duration in fileResults
                #            This is the actual playable duration of the recorded file
                if call_session.egress_id and call_session.duration_seconds is None:
                    try:
                        # Get recording status - LiveKit API returns duration from file metadata
                        recording_info = await self.recording_manager.get_recording_status(
                            call_session.egress_id, 
                            room_name
                        )
                        if recording_info:
                            recording_file_duration = recording_info.get("duration_seconds")
                            # Also check file_results in recording_info
                            if recording_file_duration is None and recording_info.get("file_results"):
                                first_file_info = recording_info["file_results"][0]
                                if first_file_info.get("duration"):
                                    recording_file_duration = first_file_info["duration"]
                            
                            if recording_file_duration is not None:
                                call_session.duration_seconds = round(float(recording_file_duration), 2)
                                logger.info({
                                    "event": "duration_from_recording_api",
                                    "room_name": room_name,
                                    "duration_seconds": call_session.duration_seconds,
                                    "note": "Duration from MP4 file metadata (MOOV atom) via LiveKit API"
                                })
                    except Exception as e:
                        logger.warning({
                            "event": "failed_to_fetch_recording_duration",
                            "room_name": room_name,
                            "error": str(e)
                        })
                
                # Priority 2: Use recording timestamps if available (when recording is enabled)
                #            Only available when recording is enabled
                if call_session.duration_seconds is None and call_session.recording_started_at and call_session.recording_ended_at:
                    call_session.started_at = call_session.recording_started_at
                    call_session.ended_at = call_session.recording_ended_at
                    call_session.duration_seconds = round((call_session.recording_ended_at - call_session.recording_started_at).total_seconds(), 2)
                    
                    logger.info({
                        "event": "duration_calculated_from_recording_timestamps",
                        "room_name": room_name,
                        "recording_started_at": call_session.recording_started_at.isoformat(),
                        "recording_ended_at": call_session.recording_ended_at.isoformat(),
                        "started_at": call_session.started_at.isoformat(),
                        "ended_at": call_session.ended_at.isoformat(),
                        "duration_seconds": call_session.duration_seconds,
                        "duration_minutes": round(call_session.duration_seconds / 60, 2),
                        "note": "Calculated from recording timestamps"
                    })
                else:
                    # Priority 3: When recording is disabled or not available, calculate duration from call timestamps
                    # Set ended_at to exact datetime when call ends
                    call_session.ended_at = datetime.now(timezone.utc)
                    
                    logger.info({
                        "event": "call_ending",
                        "room_name": room_name,
                        "ended_at": call_session.ended_at.isoformat(),
                        "has_caller_participant": call_session.caller_participant is not None,
                        "has_callee_participant": call_session.callee_participant is not None,
                        "has_recording_timestamps": call_session.recording_started_at is not None and call_session.recording_ended_at is not None,
                        "recording_enabled": call_session.auto_record
                    })

                    # Priority 3a: Calculate duration from joinedAt fields in participant data
                    # This gives accurate timestamps based on when both participants were actually in the call
                    # Works even when recording is disabled
                    caller_participant = call_session.caller_participant
                    callee_participant = call_session.callee_participant
                    
                    if caller_participant and callee_participant and isinstance(caller_participant, dict) and isinstance(callee_participant, dict):
                        try:
                            # Get join times - prefer joinedAtMs (milliseconds) for precision, fallback to joinedAt
                            caller_joined_ms = caller_participant.get("joinedAtMs")
                            caller_joined = caller_participant.get("joinedAt")
                            callee_joined_ms = callee_participant.get("joinedAtMs")
                            callee_joined = callee_participant.get("joinedAt")
                            
                            caller_joined_ts = None
                            callee_joined_ts = None
                            
                            # Process caller join time
                            if caller_joined_ms:
                                # joinedAtMs is in milliseconds, convert to seconds
                                caller_joined_ts = float(caller_joined_ms) / 1000.0
                            elif caller_joined:
                                # joinedAt is typically in seconds (Unix timestamp)
                                # But check if it's actually milliseconds (value > 1e10 indicates milliseconds)
                                caller_joined_val = float(caller_joined)
                                # Current Unix timestamp in seconds is ~1.7e9, in milliseconds is ~1.7e12
                                # If value > 1e10 (10 billion), it's likely milliseconds
                                if caller_joined_val > 1e10:
                                    caller_joined_ts = caller_joined_val / 1000.0
                                else:
                                    caller_joined_ts = caller_joined_val
                            
                            # Process callee join time
                            if callee_joined_ms:
                                # joinedAtMs is in milliseconds, convert to seconds
                                callee_joined_ts = float(callee_joined_ms) / 1000.0
                            elif callee_joined:
                                # joinedAt is typically in seconds (Unix timestamp)
                                # But check if it's actually milliseconds (value > 1e10 indicates milliseconds)
                                callee_joined_val = float(callee_joined)
                                # Current Unix timestamp in seconds is ~1.7e9, in milliseconds is ~1.7e12
                                # If value > 1e10 (10 billion), it's likely milliseconds
                                if callee_joined_val > 1e10:
                                    callee_joined_ts = callee_joined_val / 1000.0
                                else:
                                    callee_joined_ts = callee_joined_val
                            
                            if caller_joined_ts and callee_joined_ts:
                                # Get the later join time (when both participants were in the call)
                                call_start_timestamp = max(caller_joined_ts, callee_joined_ts)
                                
                                # Update started_at to exact datetime from participant join time
                                call_session.started_at = datetime.fromtimestamp(call_start_timestamp, tz=timezone.utc)
                                
                                # Calculate duration from call start to end time
                                end_time = call_session.ended_at.timestamp()
                                duration_seconds = end_time - call_start_timestamp
                                
                                logger.info({
                                    "event": "started_at_and_duration_calculated_from_joinedAt",
                                    "room_name": room_name,
                                    "caller_joined_original": str(caller_joined_ms or caller_joined),
                                    "callee_joined_original": str(callee_joined_ms or callee_joined),
                                    "caller_joined_ts": caller_joined_ts,
                                    "callee_joined_ts": callee_joined_ts,
                                    "call_start_timestamp": call_start_timestamp,
                                    "started_at": call_session.started_at.isoformat(),
                                    "ended_at": call_session.ended_at.isoformat(),
                                    "end_time_timestamp": end_time,
                                    "duration_seconds": round(duration_seconds, 2),
                                    "duration_minutes": round(duration_seconds / 60, 2)
                                })
                                
                                call_session.duration_seconds = round(duration_seconds, 2)
                        except (ValueError, TypeError, AttributeError) as e:
                            logger.warning({
                                "event": "failed_to_calculate_from_joinedAt",
                                "room_name": room_name,
                                "error": str(e)
                            })
                            # Fallback to started_at if participant data calculation fails
                            if call_session.started_at:
                                duration_seconds = (call_session.ended_at - call_session.started_at).total_seconds()
                                call_session.duration_seconds = round(duration_seconds, 2)
                            elif len(call_session.participants) == 1:
                                call_session.duration_seconds = 0.0
                                logger.info({
                                    "event": "call_ended_without_answer_in_end_call",
                                    "room_name": room_name,
                                    "note": "Call ended without being answered - duration set to 0"
                                })
                            else:
                                # Connected call but started_at missing - use created_at as fallback
                                started_at = call_session.created_at
                                call_session.started_at = started_at
                                call_session.duration_seconds = round(
                                    (call_session.ended_at - started_at).total_seconds(), 2
                                )
                                logger.info({
                                    "event": "duration_from_created_at_fallback",
                                    "room_name": room_name,
                                    "note": "started_at missing, used created_at for connected call"
                                })
                            logger.info({
                                "event": "duration_calculated_from_started_at_fallback",
                                "room_name": room_name,
                                "duration_seconds": call_session.duration_seconds
                            })
                    else:
                        # Priority 3b: Fallback to started_at if participant data is not available
                        # This is the final fallback when recording is disabled and participant data is missing
                        if call_session.started_at:
                            duration_seconds = (call_session.ended_at - call_session.started_at).total_seconds()
                            call_session.duration_seconds = round(duration_seconds, 2)
                            logger.info({
                                "event": "duration_calculated_from_started_at_fallback",
                                "room_name": room_name,
                                "duration_seconds": call_session.duration_seconds,
                                "note": "Fallback calculation (recording may be disabled or participant data unavailable)"
                            })
                        elif len(call_session.participants) == 1:
                            # Single participant = missed call
                            call_session.duration_seconds = 0.0
                            logger.info({
                                "event": "call_ended_without_answer_final_fallback",
                                "room_name": room_name,
                                "duration_seconds": 0.0,
                                "note": "Call ended without being answered - duration set to 0"
                            })
                        else:
                            # Connected call but started_at missing - use created_at as fallback
                            started_at = call_session.created_at
                            call_session.started_at = started_at
                            call_session.duration_seconds = round(
                                (call_session.ended_at - started_at).total_seconds(), 2
                            )
                            logger.info({
                                "event": "duration_from_created_at_fallback",
                                "room_name": room_name,
                                "note": "started_at missing, used created_at for connected call duration"
                            })

            # Save to MongoDB (use update_one with upsert to handle duplicates)
            # to_dict() already omits nulls and recording fields when no recording
            call_data = call_session.to_dict()
            recording_fields = ("egress_id", "recording_file_name", "recording_s3_location", "recording_started_at", "recording_ended_at", "recording_status")
            no_recording = not call_data.get("egress_id")
            try:
                update_op = {"$set": call_data}
                if no_recording:
                    update_op["$unset"] = {k: "" for k in recording_fields}
                await self.calls_collection.update_one(
                    {"room_name": room_name},
                    update_op,
                    upsert=True
                )
                logger.info({
                    "event": "call_saved_to_mongodb",
                    "room_name": room_name,
                    "started_at": call_data.get("started_at"),
                    "ended_at": call_data.get("ended_at"),
                    "duration_seconds": call_data.get("duration_seconds"),
                    "status": call_data.get("status")
                })
            except Exception as db_error:
                logger.error({"event": "save_call_to_mongodb_failed", "room_name": room_name, "error": str(db_error)})
                # Continue even if database save fails

            # Clean up participant mappings (handles single-participant calls where one of caller/callee may be None)
            for identity in call_session.participants:
                if identity in self.participant_to_room:
                    del self.participant_to_room[identity]

            # Ensure duration_seconds is set (should be set by now, but use 0 as fallback)
            final_duration = call_session.duration_seconds if call_session.duration_seconds is not None else 0.0
            
            logger.info({"event": "call_ended", "room_name": room_name, "duration_seconds": round(final_duration, 1)})

            return CallEndResponse(
                success=True,
                room_name=room_name,
                status=call_session.status.value,
                duration_seconds=final_duration,
                recording_s3_location=call_session.recording_s3_location
            )

        except Exception as e:            
            #logger.error(f"Failed to end call {room_name}: { str(e) }")
            logger.error(traceback.format_exc())  # Print full traceback for debugging
            raise
        finally:
            # Always cleanup the call session
            if room_name in self.active_calls:
                del self.active_calls[room_name]

    async def _update_ended_call_from_doc(self, room_name: str, existing_call: dict) -> None:
        """Update call record in DB with ended status and duration (used when room not in active_calls)."""
        try:
            ended_at = datetime.now(timezone.utc)
            started_at = None
            if existing_call.get("started_at"):
                try:
                    sa = existing_call["started_at"]
                    started_at = datetime.fromisoformat(sa.replace("Z", "+00:00")) if isinstance(sa, str) else sa
                except (ValueError, TypeError):
                    pass
            if started_at is None:
                for part in (existing_call.get("caller_participant"), existing_call.get("callee_participant")):
                    if not part or not isinstance(part, dict):
                        continue
                    for key in ("joinedAtMs", "joinedAt"):
                        val = part.get(key)
                        if val is not None:
                            try:
                                v = float(val)
                                ts = v / 1000.0 if (key == "joinedAtMs" or v > 1e10) else v
                                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                                if started_at is None or dt < started_at:
                                    started_at = dt
                            except (ValueError, TypeError):
                                pass
                            break
            if started_at is None:
                ca = existing_call.get("created_at")
                if ca:
                    try:
                        started_at = datetime.fromisoformat(ca.replace("Z", "+00:00")) if isinstance(ca, str) else ca
                    except (ValueError, TypeError):
                        pass
            duration_seconds = round((ended_at - started_at).total_seconds(), 2) if started_at else 0.0
            update_data = {"status": "ended", "ended_at": ended_at.isoformat(), "duration_seconds": duration_seconds}
            if started_at and not existing_call.get("started_at"):
                update_data["started_at"] = started_at.isoformat()
            await self.calls_collection.update_one(
                {"room_name": room_name},
                {"$set": update_data},
                upsert=False
            )
            logger.info({"event": "call_ended_db_update", "room_name": room_name, "duration_seconds": duration_seconds})
        except Exception as e:
            logger.warning({"event": "failed_to_update_ended_call", "room_name": room_name, "error": str(e)})

    def _build_call_status_response(self, call_session: CallSession, room_name: str) -> dict:
        """Build response dict from in-memory CallSession."""
        caller_id = ""
        callee_id = ""
        if call_session.caller_participant and isinstance(call_session.caller_participant, dict):
            caller_id = call_session.caller_participant.get("identity") or call_session.caller_participant.get("id") or ""
        if call_session.callee_participant and isinstance(call_session.callee_participant, dict):
            callee_id = call_session.callee_participant.get("identity") or call_session.callee_participant.get("id") or ""
        return {
            "room_name": room_name,
            "caller_id": caller_id,
            "callee_id": callee_id,
            "status": call_session.status,
            "recording_status": call_session.recording_status,
            "participants": list(call_session.participants),
            "created_at": call_session.created_at.isoformat(),
            "started_at": call_session.started_at.isoformat() if call_session.started_at else None,
            "ended_at": call_session.ended_at.isoformat() if call_session.ended_at else None,
            "_egress_id": call_session.egress_id,
        }

    def _build_call_status_from_doc(self, doc: dict, room_name: str) -> dict:
        """Build response dict from MongoDB document (fallback when room not in active_calls)."""
        caller_id = ""
        callee_id = ""
        for part, key in ((doc.get("caller_participant"), "caller"), (doc.get("callee_participant"), "callee")):
            if part and isinstance(part, dict):
                val = part.get("identity") or part.get("id") or ""
                if key == "caller":
                    caller_id = val
                else:
                    callee_id = val
        participants = doc.get("participants") or []
        if not participants and (caller_id or callee_id):
            if caller_id:
                participants.append(caller_id)
            if callee_id and callee_id not in participants:
                participants.append(callee_id)
        status_val = doc.get("status", "waiting")
        try:
            status = CallStatus(status_val) if isinstance(status_val, str) else status_val
        except ValueError:
            status = CallStatus.WAITING
        rec_status_val = doc.get("recording_status", "not_started")
        try:
            recording_status = RecordingStatus(rec_status_val) if isinstance(rec_status_val, str) else rec_status_val
        except ValueError:
            recording_status = RecordingStatus.NOT_STARTED
        created_at = doc.get("created_at")
        if created_at and hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        created_at = created_at or ""
        started_at = doc.get("started_at")
        if started_at and hasattr(started_at, "isoformat"):
            started_at = started_at.isoformat()
        ended_at = doc.get("ended_at")
        if ended_at and hasattr(ended_at, "isoformat"):
            ended_at = ended_at.isoformat()
        return {
            "room_name": room_name,
            "caller_id": caller_id,
            "callee_id": callee_id,
            "status": status,
            "recording_status": recording_status,
            "participants": participants,
            "created_at": created_at or "",
            "started_at": started_at,
            "ended_at": ended_at,
            "_egress_id": doc.get("egress_id"),
        }

    async def get_call_status(self, room_name: str) -> CallStatusResponse:
        """Get current call status and recording information.
        Checks active_calls first; falls back to MongoDB when room not in memory (e.g. multi-worker).
        """
        logger.info({"event": "get_call_status", "room_name": room_name})
        logger.info({"event": "active_calls_list", "active_calls": list(self.active_calls.keys())})

        if room_name in self.active_calls:
            call_session = self.active_calls[room_name]
            response_data = self._build_call_status_response(call_session, room_name)
        else:
            # Fallback: fetch from MongoDB (handles multi-worker - room may be on different worker)
            existing = await self.calls_collection.find_one({"room_name": room_name})
            if not existing:
                raise ValueError("Call not found")
            # Only return from DB if call is still active (waiting or active); ended calls are not "current"
            doc_status = existing.get("status", "waiting")
            if doc_status == "ended":
                raise ValueError("Call not found")
            # When polling: if record says active but room no longer exists in LiveKit, update the record
            try:
                await self.recording_manager.livekit_client.room.list_participants(ListParticipantsRequest(room=room_name))
            except Exception as lk_err:
                err_str = str(lk_err).lower()
                if "not_found" in err_str or "does not exist" in err_str or "404" in err_str or "not found" in err_str:
                    logger.info({"event": "get_call_status_room_gone_updating", "room_name": room_name, "note": "Room gone in LiveKit, updating DB"})
                    await self._update_ended_call_from_doc(room_name, existing)
                    raise ValueError("Call not found")
            logger.info({"event": "get_call_status_from_db_fallback", "room_name": room_name, "status": doc_status, "note": "Room not in active_calls, using MongoDB"})
            response_data = self._build_call_status_from_doc(existing, room_name)

        # Add recording information if available
        egress_id = response_data.pop("_egress_id", None)
        if egress_id:
            try:
                recording_info = await self.recording_manager.get_recording_status(egress_id, room_name)
                if recording_info:
                    # Generate presigned URLs for file downloads
                    if recording_info.get("file_results"):
                        for file_result in recording_info["file_results"]:
                            if file_result.get("location", "").startswith("s3://"):
                                s3_location = file_result["location"]
                                # Extract S3 key from location
                                s3_key = s3_location.replace(f"s3://{self.recording_manager.s3_bucket}/", "")
                                file_result["download_url"] = await self.recording_manager.generate_presigned_url(
                                    s3_key, expiration=3600
                                )
                    response_data["recording_info"] = recording_info
            except Exception as e:
                logger.error({"event": "get_recording_info_failed", "room_name": room_name, "error": str(e)})

        return CallStatusResponse(**response_data)

    async def list_active_calls(self) -> ActiveCallsResponse:
        """List all active calls"""
        active_calls = []
        for room_name, call_session in self.active_calls.items():
            call_info = ActiveCallInfo(
                room_name=room_name,
                caller_participant=call_session.caller_participant,
                callee_participant=call_session.callee_participant,
                status=call_session.status.value,
                recording_status=call_session.recording_status.value,
                created_at=call_session.created_at.isoformat()
            )
            active_calls.append(call_info)
        
        return ActiveCallsResponse(active_calls=active_calls)

    async def handle_livekit_webhook(self, webhook_data: Dict):
        """Handle LiveKit webhook events"""        
        try:
            event_type = webhook_data.get("event")
            room_data = webhook_data.get("room", {})
            room_name = room_data.get("name")
            # For egress events, room is not in payload - extract room_name from egressInfo first
            if not room_name and event_type in ["egress_started", "egress_ended", "egress_updated"]:
                egress_info = webhook_data.get("egressInfo", {})
                room_name = egress_info.get("roomName") or egress_info.get("room_name")
            # Skip processing for rooms containing "chat" keyword (messaging rooms)
            if room_name and "chat" in room_name.lower():
                logger.info({"event": "skipping_chat_room_webhook", "room_name": room_name, "event_type": event_type})
                return
                
            logger.info({"event": "handle_livekit_webhook_data", "webhook_data": webhook_data})

            logger.info({
                "event": "handle_livekit_webhook_called",
                "event_type": event_type,
                "room_name": room_name,
                "active_calls_count": len(self.active_calls),
                "active_calls": list(self.active_calls.keys())
            })

            if not room_name:
                logger.warning({"event": "webhook_missing_room_name", "event_type": event_type})
                return
           

            # Handle room_started event - create call session when room is created
            if event_type == "room_started":
                if room_name not in self.active_calls:
                    from app.services import organization_service
                    # For sip_bridge rooms, organization_id is in room metadata (set at create_room)
                    organization_id = None
                    room_metadata = room_data.get("metadata")
                    if room_metadata:
                        try:
                            meta = json.loads(room_metadata) if isinstance(room_metadata, str) else room_metadata
                            if isinstance(meta, dict) and meta.get("type") == "sip_bridge":
                                organization_id = meta.get("organization_id")
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if not organization_id:
                        default_org = organization_service.get_or_create_default_organization()
                        organization_id = str(default_org["_id"])
                    call_session = CallSession(
                        room_name=room_name,
                        caller_participant={},
                        callee_participant={},
                        auto_record=True,
                        recording_options=None,
                        organization_id=organization_id,
                    )
                    # Note: caller_participant and callee_participant are already initialized to None in __init__
                    self.active_calls[room_name] = call_session
                    # Persist so call record has correct organization_id from the start
                    try:
                        call_data = call_session.to_dict()
                        update_op = {"$set": call_data}
                        if not call_data.get("egress_id"):
                            update_op["$unset"] = {k: "" for k in ("egress_id", "recording_file_name", "recording_s3_location", "recording_started_at", "recording_ended_at", "recording_status")}
                        await self.calls_collection.update_one(
                            {"room_name": room_name},
                            update_op,
                            upsert=True
                        )
                        logger.info({"event": "call_session_from_room_started_saved_to_mongodb", "room_name": room_name, "organization_id": organization_id})
                    except Exception as db_error:
                        logger.error({"event": "save_room_started_session_to_mongodb_failed", "room_name": room_name, "error": str(db_error)})
                    logger.info({"event": "call_session_created_from_room_started", "room_name": room_name, "organization_id": organization_id})
                else:
                    logger.warning({"event": "call_session_already_exists_from_room_started", "session": self.active_calls[room_name]})
                return

            # For other events, ensure call session exists
            if room_name not in self.active_calls:
                # LiveKit often delivers webhooks out of order: e.g. SIP leg fails and we tear down the
                # room while the WebRTC client's participant_joined / track_* events arrive later.
                try:
                    existing = await self.calls_collection.find_one(
                        {"room_name": room_name},
                        {"status": 1, "ended_at": 1},
                    )
                    if existing and (
                        existing.get("status") == CallStatus.ENDED.value
                        or existing.get("ended_at") is not None
                    ):
                        logger.info({
                            "event": "webhook_after_call_finalized",
                            "room_name": room_name,
                            "event_type": event_type,
                            "note": "Ignoring late webhook; call already ended (out-of-order delivery)",
                        })
                        return
                except Exception as db_error:
                    logger.warning({
                        "event": "webhook_unknown_room_db_check_failed",
                        "room_name": room_name,
                        "event_type": event_type,
                        "error": str(db_error),
                    })
                logger.warning({"event": "webhook_event_for_unknown_room", "room_name": room_name, "event_type": event_type})
                return

            call_session = self.active_calls[room_name]

            if event_type == "participant_joined":
                participant = webhook_data.get("participant", {})
                participant_identity = participant.get("identity") or participant.get("id")
                
                
                if participant_identity:
                    if participant.get("kind") != "EGRESS":
                        call_session.participants.add(participant_identity)
                    else:
                        logger.info({
                            "event": "egress_participant_joined",
                            "room_name": room_name,
                            "participant_identity": participant_identity,
                            "egress_id": participant.get("egressId")
                        })
                        return
                    
                    # Enhanced logging for SIP participants
                    is_sip = participant_identity.startswith("sip-")
                    participant_kind = participant.get("kind", "unknown")
                    
                    logger.info({
                        "event": "participant_joined_call",
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "is_sip_participant": is_sip,
                        "participant_kind": participant_kind,
                        "current_participants_count": len(call_session.participants),
                        "all_participants": list(call_session.participants),
                        "note": "SIP participant joined successfully" if is_sip else "WebRTC participant joined"
                    })

                    # Get current number of participants
                    num_participants = len(call_session.participants)
                    
                    logger.warning({"event": "call_session", "callee_participant": call_session.callee_participant})
                    # First participant (caller) - set caller_id if not already set
                    if num_participants == 1:
                        call_session.started_at = datetime.now(timezone.utc)                        
                        
                        if participant.get("kind") != "EGRESS":
                            # Store cleaned participant data
                            if participant.get("kind") == "SIP":                                
                                call_session.kind = "webrtc_to_sip"
                                call_session.callee_participant = self._clean_participant_data(participant)
                            else:
                                call_session.caller_participant = self._clean_participant_data(participant)

                        # If auto_record is enabled, schedule recording to start
                        # It will start when second participant joins, or after a delay if only one participant
                        if call_session.auto_record and call_session.recording_status == RecordingStatus.NOT_STARTED:
                            recording_options = call_session.recording_options or {
                                "width": 1280,
                                "height": 720,
                                "framerate": 30
                            }
                            # Schedule recording with a longer delay (10 seconds) to wait for second participant
                            # If second participant joins before delay, recording will start then instead
                            asyncio.create_task(
                                self._delayed_recording_start(room_name, recording_options, delay=10)
                            )
                            logger.info({
                                "event": "recording_scheduled_on_first_participant_join",
                                "room_name": room_name,
                                "caller_id": participant_identity,
                                "auto_record": call_session.auto_record,
                                "delay_seconds": 10
                            })
                        
                        # Update participant mapping
                        self.participant_to_room[participant_identity] = room_name
                    
                    # Second participant (callee) - set callee_id and update started_at
                    elif num_participants == 2:
                       
                                                
                        if participant.get("kind") != "EGRESS":
                            # Store cleaned participant data
                            # When kind is webrtc_to_sip, SIP joined first (callee). Second participant is WebRTC (caller).
                            if call_session.kind == "webrtc_to_sip":
                                call_session.caller_participant = self._clean_participant_data(participant)
                            elif call_session.kind is not None and call_session.kind == "SIP":
                                call_session.kind = "webrtc_to_sip"
                                call_session.caller_participant = self._clean_participant_data(participant)
                            else:
                                call_session.callee_participant = self._clean_participant_data(participant)
                        
                        # Calculate started_at from participant join times
                        caller_participant = call_session.caller_participant
                        callee_participant = call_session.callee_participant
                        logger.info({
                            "event": "caller_participant_and_callee_participant",
                            "caller_participant": caller_participant,
                            "callee_participant": callee_participant
                        })
                        
                        if caller_participant and callee_participant and isinstance(caller_participant, dict) and isinstance(callee_participant, dict):
                            try:
                                # Get join times - prefer joinedAtMs (milliseconds) for precision
                                caller_joined_ms = caller_participant.get("joinedAtMs")
                                caller_joined = caller_participant.get("joinedAt")
                                callee_joined_ms = callee_participant.get("joinedAtMs")
                                callee_joined = callee_participant.get("joinedAt")
                                
                                caller_joined_ts = None
                                callee_joined_ts = None
                                
                                # Process caller join time
                                if caller_joined_ms:
                                    caller_joined_ts = float(caller_joined_ms) / 1000.0
                                elif caller_joined:
                                    caller_joined_val = float(caller_joined)
                                    # Current Unix timestamp in seconds is ~1.7e9, in milliseconds is ~1.7e12
                                    # If value > 1e10 (10 billion), it's likely milliseconds
                                    if caller_joined_val > 1e10:
                                        caller_joined_ts = caller_joined_val / 1000.0
                                    else:
                                        caller_joined_ts = caller_joined_val
                                
                                # Process callee join time
                                if callee_joined_ms:
                                    callee_joined_ts = float(callee_joined_ms) / 1000.0
                                elif callee_joined:
                                    callee_joined_val = float(callee_joined)
                                    # Current Unix timestamp in seconds is ~1.7e9, in milliseconds is ~1.7e12
                                    # If value > 1e10 (10 billion), it's likely milliseconds
                                    if callee_joined_val > 1e10:
                                        callee_joined_ts = callee_joined_val / 1000.0
                                    else:
                                        callee_joined_ts = callee_joined_val
                                
                                if caller_joined_ts and callee_joined_ts:
                                    # Get the later join time (when both participants were in the call)
                                    call_start_timestamp = max(caller_joined_ts, callee_joined_ts)
                                    
                                    # Convert timestamp to datetime
                                    call_session.started_at = datetime.fromtimestamp(call_start_timestamp, tz=timezone.utc)
                                    
                                    logger.info({
                                        "event": "started_at_set_from_joinedAt",
                                        "room_name": room_name,
                                        "caller_joined_original": caller_joined_ms or caller_joined,
                                        "callee_joined_original": callee_joined_ms or callee_joined,
                                        "caller_joined_ts": caller_joined_ts,
                                        "callee_joined_ts": callee_joined_ts,
                                        "started_at": call_session.started_at.isoformat()
                                    })
                            except (ValueError, TypeError, AttributeError) as e:
                                logger.warning({
                                    "event": "failed_to_set_started_at_from_joinedAt",
                                    "room_name": room_name,
                                    "error": str(e)
                                })
                                # Fallback to current time
                                call_session.started_at = datetime.now(timezone.utc)
                        else:
                            # Fallback to current time if participant data not available
                            call_session.started_at = datetime.now(timezone.utc)
                        
                        call_session.status = CallStatus.ACTIVE
                        logger.info({
                            "event": "call_started_with_callee",
                            "room_name": room_name,
                            "callee_id": participant_identity,
                            "started_at": call_session.started_at.isoformat() if call_session.started_at else None
                        })
                        
                        # Start recording if auto_record is enabled when call becomes active
                        if call_session.auto_record and call_session.recording_status == RecordingStatus.NOT_STARTED:
                            recording_options = call_session.recording_options or {
                                "width": 1280,
                                "height": 720,
                                "framerate": 30
                            }
                            # Start recording immediately when both participants have joined
                            # Use a short delay (2 seconds) to ensure both participants are fully connected
                            asyncio.create_task(
                                self._delayed_recording_start(room_name, recording_options, delay=2)
                            )
                            logger.info({
                                "event": "auto_recording_initiated_on_participant_join",
                                "room_name": room_name,
                                "num_participants": num_participants,
                                "auto_record": call_session.auto_record
                                })
                            
                            # Save to MongoDB when call becomes active
                            try:
                                call_data = call_session.to_dict()
                                update_op = {"$set": call_data}
                                if not call_data.get("egress_id"):
                                    update_op["$unset"] = {k: "" for k in ("egress_id", "recording_file_name", "recording_s3_location", "recording_started_at", "recording_ended_at", "recording_status")}
                                await self.calls_collection.update_one(
                                    {"room_name": room_name},
                                    update_op,
                                    upsert=True
                                )
                                logger.info({"event": "call_saved_to_mongodb_on_start", "room_name": room_name})
                            except Exception as db_error:
                                logger.error({"event": "save_call_to_mongodb_failed", "room_name": room_name, "error": str(db_error)})
                        
                        # Update participant mapping
                        self.participant_to_room[participant_identity] = room_name

            elif event_type == "participant_left":
                participant = webhook_data.get("participant", {})
                participant_identity = participant.get("identity")
                participant_kind = participant.get("kind")
                
                # Skip EGRESS participants (recording bots) - only destroy room when SIP or WebRTC participant leaves
                if participant_identity and participant_kind != "EGRESS":
                    num_active_participants = room_data.get("numParticipants", 0)
                    logger.info({
                        "event": "participant_left_call",
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "participant_kind": participant_kind,
                        "remaining_participants": num_active_participants
                    })

                    # When any SIP or WebRTC participant leaves, destroy the room and end the call
                    # This disconnects all remaining participants in the LiveKit room
                    call_session.ended_at = datetime.now(timezone.utc)
                    
                    # Calculate duration from started_at to ended_at
                    # Use created_at as fallback when started_at is None (e.g. participant_joined hit different worker)
                    # Single participant = missed call, duration 0; 2+ participants = connected call
                    if call_session.started_at:
                        call_session.duration_seconds = round(
                            (call_session.ended_at - call_session.started_at).total_seconds(), 
                            2
                        )
                    elif len(call_session.participants) == 1:
                        call_session.duration_seconds = 0.0
                        logger.info({
                            "event": "call_ended_without_answer",
                            "room_name": room_name,
                            "note": "Call ended without being answered - duration set to 0"
                        })
                    else:
                        # Connected call but started_at missing - use created_at as fallback
                        started_at = call_session.created_at
                        call_session.started_at = started_at
                        call_session.duration_seconds = round(
                            (call_session.ended_at - started_at).total_seconds(), 2
                        )
                        logger.info({
                            "event": "duration_from_created_at_fallback",
                            "room_name": room_name,
                            "note": "started_at was missing, used created_at for connected call duration"
                        })
                    
                    logger.info({
                        "event": "call_ended_on_participant_left_destroying_room",
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "participant_kind": participant_kind,
                        "started_at": call_session.started_at.isoformat() if call_session.started_at else None,
                        "ended_at": call_session.ended_at.isoformat(),
                        "duration_seconds": call_session.duration_seconds
                    })
                    
                    # Save to MongoDB
                    try:
                        call_data = call_session.to_dict()
                        await self.calls_collection.update_one(
                            {"room_name": room_name},
                            {"$set": {
                                "ended_at": call_data.get("ended_at"),
                                "duration_seconds": call_data.get("duration_seconds"),
                                "started_at": call_data.get("started_at")
                            }},
                            upsert=False
                        )
                        logger.info({
                            "event": "call_data_saved_to_mongodb_on_participant_left",
                            "room_name": room_name,
                            "ended_at": call_session.ended_at.isoformat(),
                            "duration_seconds": call_session.duration_seconds
                        })
                    except Exception as db_error:
                        logger.error({
                            "event": "save_call_data_to_mongodb_failed",
                            "room_name": room_name,
                            "error": str(db_error)
                        })
                    
                    # End the call session (stops recording, cleans up)
                    await self.end_call(room_name)
                    
                    # Destroy the LiveKit room - disconnects all remaining participants
                    await self._delete_livekit_room(room_name)

            elif event_type == "egress_started":
                egress_info = webhook_data.get("egressInfo", {})
                egress_id = egress_info.get("egressId")
                
                if call_session.egress_id == egress_id:
                    call_session.recording_status = RecordingStatus.ACTIVE
                    
                    # Store recording start time from webhook or current time
                    # Try to get startedAt from egress_info, otherwise use current time
                    started_at = egress_info.get("startedAt") or egress_info.get("started_at")
                    if started_at:
                        # Handle different timestamp formats
                        if isinstance(started_at, (int, float)):
                            # Check if it's milliseconds (> 1e10) or seconds
                            if started_at > 1e10:
                                call_session.recording_started_at = datetime.fromtimestamp(started_at / 1000.0, tz=timezone.utc)
                            else:
                                call_session.recording_started_at = datetime.fromtimestamp(started_at, tz=timezone.utc)
                        else:
                            call_session.recording_started_at = datetime.now(timezone.utc)
                    else:
                        call_session.recording_started_at = datetime.now(timezone.utc)
                    
                    logger.info({
                        "event": "recording_started_for_call",
                        "room_name": room_name,
                        "egress_id": egress_id,
                        "recording_started_at": call_session.recording_started_at.isoformat()
                    })

            elif event_type == "egress_ended":
                egress_info = webhook_data.get("egressInfo", {})
                egress_id = egress_info.get("egressId")
                
                if call_session.egress_id == egress_id:
                    call_session.recording_status = RecordingStatus.COMPLETED
                    
                    # Always try to fetch recording status from API first to get accurate timestamps and duration
                    # The API provides the most accurate recording start/end times and duration that match the audio file
                    # Retry with delays since recording might not be fully processed when webhook arrives
                    # File duration may not be accurate immediately - need to wait for file to be fully written
                    max_retries = 5
                    retry_delay = 2.0  # seconds - increased to allow file to be fully written
                    recording_info = None  # Store recording info for duration extraction
                    
                    for attempt in range(max_retries):
                        try:
                            # Add delay for retries (not on first attempt)
                            if attempt > 0:
                                await asyncio.sleep(retry_delay * attempt)
                            
                            recording_info = await self.recording_manager.get_recording_status(egress_id, room_name)
                            if recording_info:
                                # Store recording_info for later use in duration calculation
                                # Extract timestamps from recording info
                                rec_started_at = recording_info.get("started_at")
                                rec_ended_at = recording_info.get("ended_at")
                                
                                # Log what we received for debugging
                                logger.info({
                                    "event": "recording_info_received_from_api",
                                    "room_name": room_name,
                                    "egress_id": egress_id,
                                    "attempt": attempt + 1,
                                    "has_started_at": rec_started_at is not None,
                                    "has_ended_at": rec_ended_at is not None,
                                    "started_at_type": type(rec_started_at).__name__ if rec_started_at else None,
                                    "ended_at_type": type(rec_ended_at).__name__ if rec_ended_at else None,
                                    "started_at_value": str(rec_started_at) if rec_started_at else None,
                                    "ended_at_value": str(rec_ended_at) if rec_ended_at else None
                                })
                                
                                # Handle started_at
                                if rec_started_at:
                                    if isinstance(rec_started_at, datetime):
                                        call_session.recording_started_at = rec_started_at
                                    elif isinstance(rec_started_at, (int, float)):
                                        if rec_started_at > 1e10:
                                            call_session.recording_started_at = datetime.fromtimestamp(rec_started_at / 1000.0, tz=timezone.utc)
                                        else:
                                            call_session.recording_started_at = datetime.fromtimestamp(rec_started_at, tz=timezone.utc)
                                    elif hasattr(rec_started_at, 'seconds'):
                                        # Handle protobuf Timestamp (has .seconds and .nanos attributes)
                                        # Convert seconds to datetime, nanos are sub-second precision
                                        timestamp_seconds = rec_started_at.seconds
                                        if timestamp_seconds:
                                            call_session.recording_started_at = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
                                    elif hasattr(rec_started_at, 'timestamp'):
                                        # Fallback: try timestamp() method if available
                                        try:
                                            call_session.recording_started_at = datetime.fromtimestamp(rec_started_at.timestamp(), tz=timezone.utc)
                                        except Exception:
                                            logger.warning({
                                                "event": "failed_to_convert_recording_started_at",
                                                "room_name": room_name,
                                                "egress_id": egress_id,
                                                "type": type(rec_started_at).__name__
                                            })
                                
                                # Handle ended_at
                                if rec_ended_at:
                                    if isinstance(rec_ended_at, datetime):
                                        call_session.recording_ended_at = rec_ended_at
                                    elif isinstance(rec_ended_at, (int, float)):
                                        if rec_ended_at > 1e10:
                                            call_session.recording_ended_at = datetime.fromtimestamp(rec_ended_at / 1000.0, tz=timezone.utc)
                                        else:
                                            call_session.recording_ended_at = datetime.fromtimestamp(rec_ended_at, tz=timezone.utc)
                                    elif hasattr(rec_ended_at, 'seconds'):
                                        # Handle protobuf Timestamp (has .seconds and .nanos attributes)
                                        # Convert seconds to datetime, nanos are sub-second precision
                                        timestamp_seconds = rec_ended_at.seconds
                                        if timestamp_seconds:
                                            call_session.recording_ended_at = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
                                    elif hasattr(rec_ended_at, 'timestamp'):
                                        # Fallback: try timestamp() method if available
                                        try:
                                            call_session.recording_ended_at = datetime.fromtimestamp(rec_ended_at.timestamp(), tz=timezone.utc)
                                        except Exception:
                                            logger.warning({
                                                "event": "failed_to_convert_recording_ended_at",
                                                "room_name": room_name,
                                                "egress_id": egress_id,
                                                "type": type(rec_ended_at).__name__
                                            })
                                
                                # If we got both timestamps, we're done
                                if call_session.recording_started_at and call_session.recording_ended_at:
                                    logger.info({
                                        "event": "recording_timestamps_fetched_from_api",
                                        "room_name": room_name,
                                        "egress_id": egress_id,
                                        "attempt": attempt + 1,
                                        "recording_started_at": call_session.recording_started_at.isoformat(),
                                        "recording_ended_at": call_session.recording_ended_at.isoformat()
                                    })
                                    break
                                elif attempt < max_retries - 1:
                                    # If we didn't get both timestamps, retry
                                    logger.info({
                                        "event": "recording_timestamps_incomplete_retrying",
                                        "room_name": room_name,
                                        "egress_id": egress_id,
                                        "attempt": attempt + 1,
                                        "has_started_at": call_session.recording_started_at is not None,
                                        "has_ended_at": call_session.recording_ended_at is not None
                                    })
                                    continue
                        except Exception as api_error:
                            if attempt < max_retries - 1:
                                logger.warning({
                                    "event": "failed_to_fetch_recording_timestamps_from_api_retrying",
                                    "room_name": room_name,
                                    "egress_id": egress_id,
                                    "attempt": attempt + 1,
                                    "error": str(api_error)
                                })
                            else:
                                logger.warning({
                                    "event": "failed_to_fetch_recording_timestamps_from_api",
                                    "room_name": room_name,
                                    "egress_id": egress_id,
                                    "max_retries": max_retries,
                                    "error": str(api_error)
                                })
                    
                    # Fallback: Try to get endedAt from webhook if API fetch failed
                    if not call_session.recording_ended_at:
                        ended_at = egress_info.get("endedAt") or egress_info.get("ended_at")
                        if ended_at:
                            # Handle different timestamp formats
                            if isinstance(ended_at, (int, float)):
                                # Check if it's milliseconds (> 1e10) or seconds
                                if ended_at > 1e10:
                                    call_session.recording_ended_at = datetime.fromtimestamp(ended_at / 1000.0, tz=timezone.utc)
                                else:
                                    call_session.recording_ended_at = datetime.fromtimestamp(ended_at, tz=timezone.utc)
                            else:
                                call_session.recording_ended_at = datetime.now(timezone.utc)
                        else:
                            # Final fallback: use current time
                            call_session.recording_ended_at = datetime.now(timezone.utc)
                    
                    # Fallback: Try to get startedAt from webhook if API fetch failed and not already set
                    if not call_session.recording_started_at:
                        started_at = egress_info.get("startedAt") or egress_info.get("started_at")
                        if started_at:
                            if isinstance(started_at, (int, float)):
                                if started_at > 1e10:
                                    call_session.recording_started_at = datetime.fromtimestamp(started_at / 1000.0, tz=timezone.utc)
                                else:
                                    call_session.recording_started_at = datetime.fromtimestamp(started_at, tz=timezone.utc)
                        else:
                            # Use recording_started_at from egress_started if available, otherwise current time
                            if not call_session.recording_started_at:
                                call_session.recording_started_at = datetime.now(timezone.utc)
                    
                    # Store S3 location and filename from egress info
                    file_results = egress_info.get("fileResults", [])
                    if file_results:
                        first_file = file_results[0]
                        # Extract location (S3 path)
                        if first_file.get("location"):
                            call_session.recording_s3_location = first_file.get("location")
                        # Extract filename
                        if first_file.get("filename"):
                            call_session.recording_file_name = first_file.get("filename")
                        # If filename not in fileResults, extract from location
                        elif call_session.recording_s3_location:
                            # Extract filename from S3 location path
                            call_session.recording_file_name = os.path.basename(call_session.recording_s3_location)
                    
                    # Priority: Use recording file duration from API (most accurate)
                    # IMPORTANT: Webhook duration may be inaccurate if file is not fully written yet
                    # Always prefer API response over webhook duration for accuracy
                    recording_file_duration = None
                    
                    # Priority 1: Get duration from recording_info API response (most accurate)
                    # The API returns duration from file metadata (MOOV atom) in file_results
                    # This is the actual playable duration of the recorded file
                    if recording_info:
                        recording_file_duration = recording_info.get("duration_seconds")
                        # Also check file_results in recording_info
                        if recording_file_duration is None and recording_info.get("file_results"):
                            first_file_info = recording_info["file_results"][0]
                            if first_file_info.get("duration"):
                                recording_file_duration = first_file_info["duration"]
                                logger.info({
                                    "event": "duration_from_api_file_results",
                                    "room_name": room_name,
                                    "duration_seconds": recording_file_duration,
                                    "note": "Duration from MP4 file metadata (MOOV atom) via LiveKit API - most accurate"
                                })
                    
                    # Priority 2: If API didn't return duration yet, try additional retries with longer delay
                    # File might still be writing when webhook fires, so we need to wait longer
                    if recording_file_duration is None:
                        logger.info({
                            "event": "duration_not_available_yet_retrying",
                            "room_name": room_name,
                            "egress_id": egress_id,
                            "note": "File may still be writing, retrying with longer delay to get accurate duration"
                        })
                        # Wait longer for file to be fully written (5 seconds)
                        await asyncio.sleep(5.0)
                        try:
                            retry_recording_info = await self.recording_manager.get_recording_status(egress_id, room_name)
                            if retry_recording_info:
                                recording_file_duration = retry_recording_info.get("duration_seconds")
                                if recording_file_duration is None and retry_recording_info.get("file_results"):
                                    first_file_info = retry_recording_info["file_results"][0]
                                    if first_file_info.get("duration"):
                                        recording_file_duration = first_file_info["duration"]
                                        logger.info({
                                            "event": "duration_from_retry_api_call",
                                            "room_name": room_name,
                                            "duration_seconds": recording_file_duration,
                                            "note": "Duration retrieved after additional delay - file should be fully written"
                                        })
                        except Exception as retry_error:
                            logger.warning({
                                "event": "failed_to_get_duration_on_retry",
                                "room_name": room_name,
                                "egress_id": egress_id,
                                "error": str(retry_error)
                            })
                    
                    # Priority 3: Check egress_info fileResults (from webhook) - only if API didn't work
                    # WARNING: Webhook duration may be inaccurate if file is not fully written
                    if recording_file_duration is None and file_results:
                        first_file = file_results[0]
                        file_duration_ns = first_file.get("duration")
                        if file_duration_ns:
                            # Convert nanoseconds to seconds
                            webhook_duration = float(file_duration_ns) / 1_000_000_000.0
                            # Only use webhook duration if it seems reasonable (not suspiciously short)
                            # Compare with timestamp-based duration to validate
                            if call_session.recording_started_at and call_session.recording_ended_at:
                                timestamp_duration = (call_session.recording_ended_at - call_session.recording_started_at).total_seconds()
                                # If webhook duration is much shorter than timestamp duration, it's likely inaccurate
                                if webhook_duration < timestamp_duration * 0.5:  # Less than 50% of timestamp duration
                                    logger.warning({
                                        "event": "webhook_duration_seems_inaccurate",
                                        "room_name": room_name,
                                        "webhook_duration_seconds": webhook_duration,
                                        "timestamp_duration_seconds": timestamp_duration,
                                        "note": "Webhook duration seems too short, will use timestamp duration instead"
                                    })
                                    recording_file_duration = None  # Don't use inaccurate webhook duration
                                else:
                                    recording_file_duration = webhook_duration
                                    logger.info({
                                        "event": "duration_from_webhook_fileResults",
                                        "room_name": room_name,
                                        "duration_seconds": recording_file_duration,
                                        "note": "Duration from webhook (validated against timestamps)"
                                    })
                            else:
                                recording_file_duration = webhook_duration
                                logger.info({
                                    "event": "duration_from_webhook_fileResults",
                                    "room_name": room_name,
                                    "duration_seconds": recording_file_duration,
                                    "note": "Duration from webhook (no timestamp validation available)"
                                })
                    
                    # Priority 4: Check egress_info root level for duration
                    if recording_file_duration is None:
                        egress_duration = egress_info.get("duration_seconds") or egress_info.get("duration")
                        if egress_duration:
                            # If it's in nanoseconds (> 1e10), convert to seconds
                            if isinstance(egress_duration, (int, float)) and egress_duration > 1e10:
                                recording_file_duration = float(egress_duration) / 1_000_000_000.0
                            else:
                                recording_file_duration = float(egress_duration)
                    
                    # Use recording file duration if available, otherwise calculate from timestamps
                    if recording_file_duration is not None:
                        call_session.duration_seconds = round(float(recording_file_duration), 2)
                        logger.info({
                            "event": "call_duration_from_recording_file_metadata",
                            "room_name": room_name,
                            "duration_seconds": call_session.duration_seconds,
                            "duration_minutes": round(call_session.duration_seconds / 60, 2),
                            "note": "Using recording file duration from LiveKit metadata (actual playable duration from MP4 file)"
                        })
                    elif call_session.recording_started_at and call_session.recording_ended_at:
                        # Fallback: Calculate duration from recording timestamps
                        recording_duration = (call_session.recording_ended_at - call_session.recording_started_at).total_seconds()
                        call_session.duration_seconds = round(recording_duration, 2)
                        logger.info({
                            "event": "call_duration_calculated_from_timestamps",
                            "room_name": room_name,
                            "recording_started_at": call_session.recording_started_at.isoformat(),
                            "recording_ended_at": call_session.recording_ended_at.isoformat(),
                            "duration_seconds": call_session.duration_seconds,
                            "duration_minutes": round(call_session.duration_seconds / 60, 2),
                            "note": "Calculated from timestamps (fallback - file duration not available)"
                        })
                    
                    # Set started_at and ended_at from recording timestamps if available
                    if call_session.recording_started_at:
                        call_session.started_at = call_session.recording_started_at
                    if call_session.recording_ended_at:
                        call_session.ended_at = call_session.recording_ended_at
                    
                    logger.info({
                        "event": "recording_completed_for_call",
                        "room_name": room_name,
                        "egress_id": egress_id,
                        "recording_s3_location": call_session.recording_s3_location,
                        "recording_file_name": call_session.recording_file_name,
                        "recording_started_at": call_session.recording_started_at.isoformat() if call_session.recording_started_at else None,
                        "recording_ended_at": call_session.recording_ended_at.isoformat() if call_session.recording_ended_at else None
                    })
                    
                    # Save updated recording info to MongoDB
                    try:
                        call_data = call_session.to_dict()
                        update_data = {
                            **call_data,
                            "recording_started_at": call_data.get("recording_started_at"),
                            "recording_ended_at": call_data.get("recording_ended_at"),
                            "duration_seconds": call_data.get("duration_seconds"),
                            "started_at": call_data.get("started_at"),
                            "ended_at": call_data.get("ended_at")
                        }
                        update_data = {k: v for k, v in update_data.items() if v is not None and v != "null"}
                        await self.calls_collection.update_one(
                            {"room_name": room_name},
                            {"$set": update_data},
                            upsert=True
                        )
                        logger.info({
                            "event": "recording_info_saved_to_mongodb",
                            "room_name": room_name,
                            "egress_id": egress_id,
                            "recording_started_at": call_data.get("recording_started_at"),
                            "recording_ended_at": call_data.get("recording_ended_at"),
                            "duration_seconds": call_data.get("duration_seconds"),
                            "has_recording_timestamps": call_data.get("recording_started_at") is not None and call_data.get("recording_ended_at") is not None
                        })
                    except Exception as db_error:
                        logger.error({
                            "event": "save_recording_info_to_mongodb_failed",
                            "room_name": room_name,
                            "egress_id": egress_id,
                            "error": str(db_error),
                            "traceback": traceback.format_exc()
                        })

            elif event_type == "room_finished":
                logger.info({"event": "room_finished_ending_call", "room_name": room_name, "msg": "Room finished, ending call"})
                await self.end_call(room_name)

        except Exception as e:
            logger.error({"event": "livekit_webhook_error", "room_name": room_name if 'room_name' in locals() else None, "error": str(e)})
            raise
