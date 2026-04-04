# services/recording_manager.py - LiveKit S3 Recording Manager
import traceback
import boto3
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from livekit import api
import botocore

logger = logging.getLogger(__name__)

class LiveKitS3RecordingManager:
    """Manages LiveKit recordings with S3 storage"""
    
    def __init__(self):
        # AWS Configuration
        self.aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        self.aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        self.aws_region = os.getenv("AWS_REGION", "us-east-1")
        self.s3_bucket = os.getenv("AWS_S3_BUCKET_NAME")
        
        # LiveKit Configuration
        self.livekit_api_key = os.getenv("LIVEKIT_SDK_API_KEY")
        self.livekit_api_secret = os.getenv("LIVEKIT_SDK_API_SECRET")
        self.livekit_url = os.getenv("LIVEKIT_SDK_URL")
        
        # Validate configuration
        self._validate_configuration()
        
        # Initialize clients
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.aws_region
        )
        
        # Initialize LiveKit client lazily to avoid event loop issues
        self.livekit_client = None
        
        logger.info("LiveKitS3RecordingManager initialized successfully")
    
    async def _ensure_livekit_client(self):
        """Ensure LiveKit client is initialized"""
        if self.livekit_client is None:
            self.livekit_client = api.LiveKitAPI(
                url=self.livekit_url,
                api_key=self.livekit_api_key,
                api_secret=self.livekit_api_secret
            )
    
    def _validate_configuration(self):
        """Validate required environment variables"""
        required_vars = [
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET_NAME",
            "LIVEKIT_SDK_API_KEY", "LIVEKIT_SDK_API_SECRET", "LIVEKIT_SDK_URL"
        ]
        
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
    
    async def start_recording_to_s3(
        self, 
        room_name: str, 
        s3_path: str = None,
        recording_options: Dict[str, Any] = None
    ) -> str:
        """Start room composite recording directly to S3"""
        await self._ensure_livekit_client()
        try:
            # Generate S3 path if not provided
            if not s3_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
                s3_path = f"recordings/{room_name}/{timestamp}.mp4"
                #s3_path = f"recordings/{room_name}/{timestamp}"
            
            # Configure S3 output
            s3_output = api.S3Upload(
                access_key=self.aws_access_key_id,
                secret=self.aws_secret_access_key,
                region=self.aws_region,
                bucket=self.s3_bucket,
                force_path_style=True,
                #filename_prefix=s3_path
            )

            # recording to a mp4 file
            file_output = api.EncodedFileOutput(
                filepath=s3_path,
                s3=s3_output,
            )
            
            # Default recording options
            default_options = {
                "audio_only": True,
                "video_only": False,
                "width": 1920,
                "height": 1080,
                "framerate": 30,
                "layout": "grid"
            }

            
            if recording_options:
                default_options.update(recording_options)
            
            # Create room composite egress request
            request = api.RoomCompositeEgressRequest(
                room_name=room_name,
                layout="speaker",#default_options.get("layout", "grid"),
                audio_only=default_options["audio_only"],
                video_only=default_options["video_only"],
                file_outputs=[file_output]               
            )

            """
            output=api.EncodedFileOutput(
                file_type=api.EncodedFileType.MP4,
                s3=s3_output
            )                
            options=api.RoomCompositeOptions(
                width=default_options["width"],
                height=default_options["height"],
                framerate=default_options["framerate"]
            )"""
            
            # Start the egress
            response = await self.livekit_client.egress.start_room_composite_egress(request)
            
            logger.info(
                f"Started recording {response.egress_id} for room {room_name} "
                f"to S3: s3://{self.s3_bucket}/{s3_path}"
            )
            return response.egress_id
            
        except Exception as e:
            logger.error(f"Failed to start S3 recording for room {room_name}: {e}")
            raise
    
    async def start_track_recording_to_s3(
        self, 
        room_name: str, 
        track_id: str,
        s3_path: str = None
    ) -> str:
        """Record a specific track directly to S3"""
        await self._ensure_livekit_client()
        try:
            if not s3_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
                s3_path = f"track-recordings/{room_name}/{track_id}/{timestamp}"
            
            s3_output = api.S3Upload(
                access_key=self.aws_access_key_id,
                secret=self.aws_secret_access_key,
                region=self.aws_region,
                bucket=self.s3_bucket,
                filename_prefix=s3_path
            )
            
            request = api.TrackEgressRequest(
                room_name=room_name,
                track_id=track_id,
                output=api.DirectFileOutput(
                    s3=s3_output
                )
            )
            
            response = await self.livekit_client.egress.start_track_egress(request)
            
            logger.info(f"Started track recording {response.egress_id} for track {track_id} to S3")
            return response.egress_id
            
        except Exception as e:
            logger.error(f"Failed to start track recording: {e}")
            raise
    
    async def start_web_recording_to_s3(
        self, 
        url: str,
        room_name: str,
        s3_path: str = None,
        recording_options: Dict[str, Any] = None
    ) -> str:
        """Record web content to S3"""
        await self._ensure_livekit_client()
        try:
            if not s3_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
                s3_path = f"web-recordings/{room_name}/{timestamp}"
            
            default_options = {
                "width": 1920,
                "height": 1080,
                "framerate": 30,
                "audio_only": False,
                "video_only": False
            }
            
            if recording_options:
                default_options.update(recording_options)
            
            s3_output = api.S3Upload(
                access_key=self.aws_access_key_id,
                secret=self.aws_secret_access_key,
                region=self.aws_region,
                bucket=self.s3_bucket,
                filename_prefix=s3_path
            )
            
            request = api.WebEgressRequest(
                url=url,
                audio_only=default_options["audio_only"],
                video_only=default_options["video_only"],
                output=api.EncodedFileOutput(
                    file_type=api.EncodedFileType.MP4,
                    s3=s3_output
                ),
                options=api.WebEgressOptions(
                    width=default_options["width"],
                    height=default_options["height"],
                    framerate=default_options["framerate"]
                )
            )
            
            response = await self.livekit_client.egress.start_web_egress(request)
            
            logger.info(f"Started web recording {response.egress_id} to S3")
            return response.egress_id
            
        except Exception as e:
            logger.error(f"Failed to start web recording: {e}")
            raise
    
    async def stop_recording(self, egress_id: str) -> bool:
        """Stop any type of recording"""
        await self._ensure_livekit_client()
        try:
            req = api.StopEgressRequest(egress_id=egress_id)
            response = await self.livekit_client.egress.stop_egress(req)
            logger.info(f"Stopped recording {egress_id}")
            return True
        except Exception as e:
            logger.error(traceback.format_exc())
            e.with_traceback(e.__traceback__)
            logger.error(f"Failed to stop recording {egress_id}: {e}")
            return False
    
    async def get_recording_status(self, egress_id: str, room_name: str = "") -> Optional[Dict]:
        """Get the status and information of a recording"""
        await self._ensure_livekit_client()
        try:
            # if you want to filter by room name:
            req = api.ListEgressRequest(room_name=room_name, egress_id=egress_id)
            response = await self.livekit_client.egress.list_egress(req)            
            if response.items:
                egress_info = response.items[0]
                return {
                    "egress_id": egress_info.egress_id,
                    "status": egress_info.status.name if hasattr(egress_info.status, 'name') else str(egress_info.status),
                    "started_at": egress_info.started_at,
                    "ended_at": egress_info.ended_at,
                    "file_results": [
                        {
                            "filename": result.filename,
                            "size": result.size,
                            "location": result.location
                        } for result in egress_info.file_results
                    ]
                }
        except Exception as e:
            logger.error(f"Failed to get recording status for {egress_id}: {e}")
        return None
    
    async def list_recordings(self, room_name: str = None, active_only: bool = False) -> List[Dict]:
        """List recordings, optionally filtered by room or active status"""
        await self._ensure_livekit_client()
        try:
            response = await self.livekit_client.egress.list_egress(
                room_name=room_name or "",
                active=active_only
            )
            
            recordings = []
            for egress_info in response.items:
                recording = {
                    "egress_id": egress_info.egress_id,
                    "room_name": egress_info.room_name,
                    "status": egress_info.status.name if hasattr(egress_info.status, 'name') else str(egress_info.status),
                    "started_at": egress_info.started_at,
                    "ended_at": egress_info.ended_at,
                    "file_results": [
                        {
                            "filename": result.filename,
                            "size": result.size,
                            "location": result.location
                        } for result in egress_info.file_results
                    ]
                }
                recordings.append(recording)
            
            return recordings
            
        except Exception as e:
            logger.error(f"Failed to list recordings: {e}")
            return []
    
    async def upload_file_to_s3(
        self, 
        local_file_path: str, 
        s3_key: str,
        metadata: Dict[str, str] = None
    ) -> bool:
        """Upload a local file to S3 (for post-processing scenarios)"""
        try:
            extra_args = {}
            if metadata:
                extra_args['Metadata'] = metadata
            
            self.s3_client.upload_file(
                local_file_path, 
                self.s3_bucket, 
                s3_key,
                ExtraArgs=extra_args
            )
            
            logger.info(f"Uploaded {local_file_path} to s3://{self.s3_bucket}/{s3_key}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to upload file to S3: {e}")
            return False
    
    async def generate_presigned_url(self, s3_key: str, expiration: int = 3600) -> Optional[str]:
        """Generate a presigned URL for accessing a recorded file"""
        try:
            response = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.s3_bucket, 'Key': s3_key},
                ExpiresIn=expiration
            )
            return response
        except Exception as e:
            logger.error(f"Failed to generate presigned URL for {s3_key}: {e}")
            return None
    
    async def delete_recording(self, s3_key: str) -> bool:
        """Delete a recording from S3"""
        try:
            self.s3_client.delete_object(Bucket=self.s3_bucket, Key=s3_key)
            logger.info(f"Deleted recording from s3://{self.s3_bucket}/{s3_key}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete recording {s3_key}: {e}")
            return False
    
    def get_recording_metadata(self, s3_key: str) -> Optional[Dict]:
        """Get metadata for a recording stored in S3"""
        try:
            response = self.s3_client.head_object(Bucket=self.s3_bucket, Key=s3_key)
            return {
                "size": response['ContentLength'],
                "last_modified": response['LastModified'].isoformat(),
                "content_type": response.get('ContentType'),
                "metadata": response.get('Metadata', {})
            }
        except Exception as e:
            logger.error(f"Failed to get metadata for {s3_key}: {e}")
            return None
    
    async def health_check(self) -> Dict[str, Any]:
        """Check the health of recording services"""
        await self._ensure_livekit_client()
        health_status = {
            "livekit_api": False,
            "s3_connection": False,
            "bucket_accessible": False,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        try:
            # Test LiveKit API connection
            await self.livekit_client.egress.list_egress(room_name="", active=False)
            health_status["livekit_api"] = True
        except Exception as e:
            logger.error(f"LiveKit API health check failed: {e}")
        
        try:
            # Test S3 connection
            self.s3_client.head_bucket(Bucket=self.s3_bucket)
            health_status["s3_connection"] = True
            health_status["bucket_accessible"] = True
        except Exception as e:
            logger.error(f"S3 health check failed: {e}")
        
        return health_status
    
    async def download_call_recording(
        self,
        s3_key: str,
        local_path: str = None
    ) -> str:
        """
        Download a call recording from S3 to a local file.
        Example s3_key: 'call-recordings/room_1756923002621/2025-09-03_18-10-14.mp4'
        """
        try:
            # Check if file exists
            self.s3_client.head_object(Bucket=self.s3_bucket, Key=s3_key)
            if not local_path:
                local_path = os.path.basename(s3_key)
            self.s3_client.download_file(self.s3_bucket, s3_key, local_path)
            logger.info(f"Downloaded s3://{self.s3_bucket}/{s3_key} to {local_path}")
            return local_path
        except botocore.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == '404' or error_code == 'NoSuchKey':
                logger.error(f"Recording not found in S3: {s3_key}")
                raise FileNotFoundError(f"Recording not found: {s3_key}")
            else:
                logger.error(f"Failed to download recording {s3_key}: {e}")
                raise