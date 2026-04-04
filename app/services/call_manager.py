# services/call_manager.py - WebRTC Call Management Service
import asyncio
import logging
from datetime import datetime, timezone
import os
import traceback
from typing import Dict, Set, Optional
import uuid

import motor
from pymongo import MongoClient
from app.config import MONGO_URI, DB_NAME

from app.models.models import (
    CallRequest, CallStatus, RecordingStatus, CallStatusResponse,
    RecordingStartResponse, RecordingStopResponse, CallEndResponse,
    ActiveCallsResponse, ActiveCallInfo, TokenRequest
)
from app.services.recording_manager import LiveKitS3RecordingManager
from app.services.token_service import TokenService

from app.config import API_HOST

logger = logging.getLogger(__name__)



class CallSession:
    """Represents an active call session"""
    
    def __init__(
        self,
        room_name: str,
        caller_id: str,
        callee_id: str,
        caller_participant: Optional[Dict] = None,
        callee_participant: Optional[Dict] = None,
    ):
        self.room_name = room_name
        self.caller_id = caller_id
        self.callee_id = callee_id
        self.status = CallStatus.WAITING
        self.recording_status = RecordingStatus.NOT_STARTED
        self.egress_id: Optional[str] = None
        self.participants: Set[str] = set()
        self.created_at = datetime.now(timezone.utc)
        self.started_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        self.duration_seconds: Optional[float] = None
        self.recording_file_name: Optional[str] = None
        self.recording_s3_location: Optional[str] = None
        self.call_id = str(uuid.uuid4())
        self.caller_participant = caller_participant or {"id": caller_id}
        self.callee_participant = callee_participant or {"id": callee_id}

    def to_dict(self) -> Dict:
        """Convert call session to dictionary"""
        return {
            "room_name": self.room_name,
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "status": self.status.value,
            "recording_status": self.recording_status.value,
            "participants": list(self.participants),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "egress_id": self.egress_id,
            "duration_seconds": self.duration_seconds,
            "recording_file_name": self.recording_file_name,
            "recording_s3_location": self.recording_s3_location,
            "call_id": self.call_id
            ,"caller_participant": self.caller_participant,
            "callee_participant": self.callee_participant,
        }

class WebRTCCallManager:
    """Manages WebRTC call lifecycle and recording"""
    
    def __init__(self, recording_manager: LiveKitS3RecordingManager, token_service: TokenService):
        self.recording_manager = recording_manager
        self.token_service = token_service
        self.active_calls: Dict[str, CallSession] = {}
        self.participant_to_room: Dict[str, str] = {}

        # Add MongoDB client and collection
        # Initialize MongoDB client
        self.mongo_client = client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        self.db = self.mongo_client[DB_NAME]
        self.calls_collection = self.db["call_sessions"]

    async def initiate_call_session(self, request: CallRequest, organization_id: Optional[str] = None):
        """Initiate a new WebRTC call between two participants"""
        try:
            # Generate unique room name if not provided
            if not request.room_name:
                timestamp = int(datetime.now().timestamp())
                room_name = f"call_{request.caller_id}_{request.callee_id}_{timestamp}"
            else:
                room_name = request.room_name

            # Check if room already exists
            if room_name in self.active_calls:
                raise ValueError(f"Call room {room_name} already exists")

            # Create call session
            call_session = CallSession(
                room_name,
                request.caller_id,
                request.callee_id,
                caller_participant=request.caller_participant,
                callee_participant=request.callee_participant,
            )
            self.active_calls[room_name] = call_session
        except Exception as e:
            # Cleanup on error
            """
            if room_name in self.active_calls:
                del self.active_calls[room_name]
            if request.caller_id in self.participant_to_room:
                del self.participant_to_room[request.caller_id]
            if request.callee_id in self.participant_to_room:
                del self.participant_to_room[request.callee_id]
            """
            logger.error(f"Failed to initiate call session: {e}")

    async def update_call_session(self, request: CallRequest):
        """Initiate a new WebRTC call between two participants"""
        try:
            room_name = request.room_name
            # Check if room already exists
            if room_name in self.active_calls:              
                # Update call session
                call_session = self.active_calls[room_name]
                call_session.callee_id = request.callee_id
                self.active_calls[room_name] = call_session
        except Exception as e:
            # Cleanup on error
            """
            if room_name in self.active_calls:
                del self.active_calls[room_name]
            if request.caller_id in self.participant_to_room:
                del self.participant_to_room[request.caller_id]
            if request.callee_id in self.participant_to_room:
                del self.participant_to_room[request.callee_id]
            """
            logger.error(f"Failed to update call session: {e}")



    async def initiate_call(self, request: CallRequest) -> Dict:
        """Initiate a new WebRTC call between two participants"""
        try:
            # Generate unique room name if not provided
            if not request.room_name:
                timestamp = int(datetime.now().timestamp())
                room_name = f"call_{request.caller_id}_{request.callee_id}_{timestamp}"
            else:
                room_name = request.room_name

            # Check if room already exists
            if room_name in self.active_calls:
                raise ValueError(f"Call room {room_name} already exists")

            # Create call session
            call_session = CallSession(
                room_name,
                request.caller_id,
                request.callee_id,
                caller_participant=request.caller_participant,
                callee_participant=request.callee_participant,
            )
            self.active_calls[room_name] = call_session

            # Generate tokens for both participants
            caller_token_request = TokenRequest(
                participant_identity=request.caller_id,
                participant_identity_name=f"Caller {request.caller_id}",
                participant_identity_type="caller",
                room_name=room_name
            )

            callee_token_request = TokenRequest(
                participant_identity=request.callee_id,
                participant_identity_name=f"Callee {request.callee_id}",
                participant_identity_type="callee",
                room_name=room_name
            )

            caller_token_response = await self.token_service.generate_token(caller_token_request)
            callee_token_response = await self.token_service.generate_token(callee_token_request)

            # Store participant mappings
            self.participant_to_room[request.caller_id] = room_name
            self.participant_to_room[request.callee_id] = room_name

            response = {
                "success": True,
                "room_name": room_name,
                "call_id": call_session.call_id,
                "caller_token": caller_token_response.accessToken,
                "callee_token": callee_token_response.accessToken,
                "ws_url": caller_token_response.wsUrl,
                "status": call_session.status.value,
                "auto_record": request.auto_record
            }

            # Schedule auto-recording if requested
            if request.auto_record:
                recording_options = request.recording_options or {
                    "width": 1280,
                    "height": 720,
                    "framerate": 30,
                    "layout": "grid"
                }
                
                # Start recording after a delay to ensure participants join
                asyncio.create_task(
                    self._delayed_recording_start(room_name, recording_options, delay=5)
                )
                response["recording_will_start"] = True

            logger.info(f"Initiated call {room_name} between {request.caller_id} and {request.callee_id}")
            return response

        except Exception as e:
            # Cleanup on error
            if room_name in self.active_calls:
                del self.active_calls[room_name]
            if request.caller_id in self.participant_to_room:
                del self.participant_to_room[request.caller_id]
            if request.callee_id in self.participant_to_room:
                del self.participant_to_room[request.callee_id]
            
            logger.error(f"Failed to initiate call: {e}")
            raise

    async def _delayed_recording_start(self, room_name: str, options: Dict, delay: int = 5):
        """Start recording after a delay to ensure participants have joined"""
        await asyncio.sleep(delay)
        try:
            await self.start_call_recording(room_name, options)
            logger.info(f"Auto-started recording for call {room_name}")
        except Exception as e:
            logger.error(f"Failed to auto-start recording for {room_name}: {e}")

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

            logger.info(f"Started recording {egress_id} for call {room_name}")

            return RecordingStartResponse(
                success=True,
                egress_id=egress_id,
                room_name=room_name,
                recording_status=call_session.recording_status.value,
                s3_path=s3_path
            )

        except Exception as e:
            call_session.recording_status = RecordingStatus.FAILED
            logger.error(f"Failed to start recording for {room_name}: {e}")
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
            logger.error(traceback.format_exc())
            e.with_traceback(e.__traceback__)
            logger.error(f"Failed to stop recording for {room_name}")
            raise

    async def list_call_session(self, search: str = None, skip: int = 0, limit: int = 10) -> list:
        """
        List call sessions with pagination and search.
        :param search: Search by room_name, caller_id, or callee_id (case-insensitive)
        :param skip: Number of records to skip
        :param limit: Max number of records to return
        """
        try:
            query = {}
            if search:
                query = {
                    "$or": [
                        {"room_name": {"$regex": search, "$options": "i"}},
                        {"caller_id": {"$regex": search, "$options": "i"}},
                        {"callee_id": {"$regex": search, "$options": "i"}},
                    ]
                }
            call_sessions = []
            cursor = self.calls_collection.find(query).skip(skip).limit(limit)
            DOWNLOAD_API = f"{API_HOST}/sdk/download-recording/room_1758223142219/2025-09-18_19-19-14"
            async for call in cursor:
                if call.get("recording_file_name") and call.get("room_name"):
                    call["recording_download_url"] = f"{API_HOST}/sdk/download-recording/{call['room_name']}/{call['recording_file_name']}"                    
                call_sessions.append(call)
            return call_sessions
        except Exception as e:
            logger.error(f"Failed to list call sessions: {e}")
            raise

    async def end_call(self, room_name: str) -> CallEndResponse:
        """End a call and cleanup resources"""
        if room_name not in self.active_calls:
            raise ValueError("Call not found")

        call_session = self.active_calls[room_name]

        try:
            # Stop recording if active
            if call_session.egress_id and call_session.recording_status == RecordingStatus.ACTIVE:
                await self.stop_call_recording(room_name)

            call_session.status = CallStatus.ENDED
            call_session.ended_at = datetime.now(timezone.utc)

            # Calculate call duration
            duration_seconds = (call_session.ended_at - call_session.created_at).total_seconds()
            call_session.duration_seconds = duration_seconds

            # Save to MongoDB
            self.calls_collection.insert_one(call_session.to_dict())

            # Clean up participant mappings
            if call_session.caller_id in self.participant_to_room:
                del self.participant_to_room[call_session.caller_id]
            if call_session.callee_id in self.participant_to_room:
                del self.participant_to_room[call_session.callee_id]

            logger.info(f"Ended call {room_name} (duration: {duration_seconds:.1f}s)")

            return CallEndResponse(
                success=True,
                room_name=room_name,
                status=call_session.status.value,
                duration_seconds=duration_seconds,
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

    async def get_call_status(self, room_name: str) -> CallStatusResponse:
        """Get current call status and recording information"""
        if room_name not in self.active_calls:
            raise ValueError("Call not found")

        call_session = self.active_calls[room_name]
        
        response_data = {
            "room_name": room_name,
            "caller_id": call_session.caller_id,
            "callee_id": call_session.callee_id,
            "status": call_session.status,
            "recording_status": call_session.recording_status,
            "participants": list(call_session.participants),
            "created_at": call_session.created_at.isoformat(),
            "started_at": call_session.started_at.isoformat() if call_session.started_at else None,
            "ended_at": call_session.ended_at.isoformat() if call_session.ended_at else None
        }

        # Add recording information if available
        if call_session.egress_id:
            try:
                recording_info = await self.recording_manager.get_recording_status(call_session.egress_id)
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
                logger.error(f"Failed to get recording info for {room_name}: {e}")

        return CallStatusResponse(**response_data)

    async def list_active_calls(self) -> ActiveCallsResponse:
        """List all active calls"""
        active_calls = []
        for room_name, call_session in self.active_calls.items():
            call_info = ActiveCallInfo(
                room_name=room_name,
                caller_id=call_session.caller_id,
                callee_id=call_session.callee_id,
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

            if not room_name or room_name not in self.active_calls:
                return

            call_session = self.active_calls[room_name]

            if event_type == "participant_joined":
                participant = webhook_data.get("participant", {})
                participant_identity = participant.get("identity")
                
                if participant_identity:
                    call_session.participants.add(participant_identity)
                    logger.info(f"Participant {participant_identity} joined call {room_name}")

                    # Mark call as active when both participants join
                    if len(call_session.participants) >= 2 and call_session.status == CallStatus.WAITING:
                        call_session.status = CallStatus.ACTIVE
                        call_session.started_at = datetime.now(timezone.utc)
                        logger.info(f"Call {room_name} is now active")

            elif event_type == "participant_left":
                participant = webhook_data.get("participant", {})
                participant_identity = participant.get("identity")
                
                if participant_identity:
                    call_session.participants.discard(participant_identity)
                    logger.info(f"Participant {participant_identity} left call {room_name}")

                    # End call if all participants leave
                    if len(call_session.participants) == 0:
                        logger.info(f"All participants left, ending call {room_name}")
                        await self.end_call(room_name)

            elif event_type == "egress_started":
                egress_info = webhook_data.get("egressInfo", {})
                egress_id = egress_info.get("egressId")
                
                if call_session.egress_id == egress_id:
                    call_session.recording_status = RecordingStatus.ACTIVE
                    logger.info(f"Recording {egress_id} started for call {room_name}")

            elif event_type == "egress_ended":
                egress_info = webhook_data.get("egressInfo", {})
                egress_id = egress_info.get("egressId")
                
                if call_session.egress_id == egress_id:
                    call_session.recording_status = RecordingStatus.COMPLETED
                    
                    # Store S3 location from egress info
                    file_results = egress_info.get("fileResults", [])
                    if file_results:
                        call_session.recording_s3_location = file_results[0].get("location")
                    
                    logger.info(f"Recording {egress_id} completed for call {room_name}: {call_session.recording_s3_location}")

            elif event_type == "room_finished":
                logger.info(f"Room {room_name} finished, ending call")
                await self.end_call(room_name)

        except Exception as e:
            logger.error(f"Error handling LiveKit webhook: {e}")
            raise

    def get_call_by_participant(self, participant_id: str) -> Optional[CallSession]:
        """Get active call for a participant"""
        room_name = self.participant_to_room.get(participant_id)
        if room_name and room_name in self.active_calls:
            return self.active_calls[room_name]
        return None

    def cleanup_expired_calls(self, max_age_hours: int = 24):
        """Cleanup calls that have been running for too long (safety measure)"""
        current_time = datetime.now(timezone.utc)
        expired_rooms = []
        
        for room_name, call_session in self.active_calls.items():
            age_hours = (current_time - call_session.created_at).total_seconds() / 3600
            if age_hours > max_age_hours:
                expired_rooms.append(room_name)
        
        for room_name in expired_rooms:
            logger.warning(f"Cleaning up expired call {room_name}")
            try:
                asyncio.create_task(self.end_call(room_name))
            except Exception as e:
                logger.error(f"Failed to cleanup expired call {room_name}: {e}")