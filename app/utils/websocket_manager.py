# utils/websocket_manager.py - WebSocket Connection Manager
import json
import logging
from typing import Dict, List, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)

class WebSocketManager:
    """Manages WebSocket connections for real-time updates"""
    
    def __init__(self):
        # room_name -> list of websockets
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # websocket -> room_name mapping for cleanup
        self.connection_rooms: Dict[WebSocket, str] = {}
    
    async def connect(self, websocket: WebSocket, room_name: str):
        """Accept a WebSocket connection and associate it with a room"""
        await websocket.accept()
        
        if room_name not in self.active_connections:
            self.active_connections[room_name] = []
        
        self.active_connections[room_name].append(websocket)
        self.connection_rooms[websocket] = room_name
        
        logger.info(f"WebSocket connected to room {room_name}. Total connections: {len(self.active_connections[room_name])}")
        
        # Send initial connection confirmation
        await self.send_personal_message(websocket, {
            "type": "connection_established",
            "room_name": room_name,
            "message": "WebSocket connection established"
        })
    
    def disconnect(self, websocket: WebSocket, room_name: str = None):
        """Disconnect a WebSocket and clean up"""
        # Get room name if not provided
        if not room_name:
            room_name = self.connection_rooms.get(websocket)
        
        if not room_name:
            return
        
        # Remove from active connections
        if room_name in self.active_connections:
            try:
                self.active_connections[room_name].remove(websocket)
                logger.info(f"WebSocket disconnected from room {room_name}")
                
                # Clean up empty room
                if not self.active_connections[room_name]:
                    del self.active_connections[room_name]
                    logger.info(f"Room {room_name} removed - no active connections")
                    
            except ValueError:
                logger.warning(f"WebSocket not found in room {room_name} connections")
        
        # Remove from connection mapping
        if websocket in self.connection_rooms:
            del self.connection_rooms[websocket]
    
    async def send_personal_message(self, websocket: WebSocket, message: dict):
        """Send a message to a specific WebSocket connection"""
        try:
            await websocket.send_text(json.dumps(message))
        except Exception as e:
            logger.error(f"Failed to send personal message: {e}")
            # Remove the connection if it's broken
            room_name = self.connection_rooms.get(websocket)
            if room_name:
                self.disconnect(websocket, room_name)
    
    async def broadcast_to_room(self, room_name: str, message: dict):
        """Broadcast a message to all connections in a room"""
        if room_name not in self.active_connections:
            logger.debug(f"No active connections for room {room_name}")
            return
        
        connections_to_remove = []
        message_json = json.dumps(message)
        
        for websocket in self.active_connections[room_name][:]:  # Create a copy to iterate safely
            try:
                await websocket.send_text(message_json)
            except Exception as e:
                logger.error(f"Failed to send message to WebSocket in room {room_name}: {e}")
                connections_to_remove.append(websocket)
        
        # Clean up broken connections
        for websocket in connections_to_remove:
            self.disconnect(websocket, room_name)
        
        if connections_to_remove:
            logger.info(f"Removed {len(connections_to_remove)} broken connections from room {room_name}")
    
    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all connected WebSockets"""
        for room_name in list(self.active_connections.keys()):
            await self.broadcast_to_room(room_name, message)
    
    def get_room_connection_count(self, room_name: str) -> int:
        """Get the number of active connections for a room"""
        return len(self.active_connections.get(room_name, []))
    
    def get_total_connections(self) -> int:
        """Get the total number of active WebSocket connections"""
        total = 0
        for connections in self.active_connections.values():
            total += len(connections)
        return total
    
    def get_active_rooms(self) -> List[str]:
        """Get a list of rooms with active connections"""
        return list(self.active_connections.keys())
    
    async def send_call_status_update(self, room_name: str, call_status: str, recording_status: str, participants_count: int):
        """Send a call status update to all connections in a room"""
        message = {
            "type": "call_status_update",
            "room_name": room_name,
            "call_status": call_status,
            "recording_status": recording_status,
            "participants_count": participants_count,
            "timestamp": json.dumps({"$date": {"$numberLong": str(int(__import__('time').time() * 1000))}})
        }
        await self.broadcast_to_room(room_name, message)
    
    async def send_recording_event(self, room_name: str, event_type: str, egress_id: str = None, additional_data: dict = None):
        """Send recording-related events to room connections"""
        message = {
            "type": "recording_event",
            "event": event_type,
            "room_name": room_name,
            "egress_id": egress_id,
            "timestamp": json.dumps({"$date": {"$numberLong": str(int(__import__('time').time() * 1000))}})
        }
        
        if additional_data:
            message.update(additional_data)
        
        await self.broadcast_to_room(room_name, message)
    
    async def send_participant_event(self, room_name: str, participant_id: str, event_type: str):
        """Send participant join/leave events"""
        message = {
            "type": "participant_event",
            "event": event_type,
            "room_name": room_name,
            "participant_id": participant_id,
            "timestamp": json.dumps({"$date": {"$numberLong": str(int(__import__('time').time() * 1000))}})
        }
        await self.broadcast_to_room(room_name, message)
    
    def cleanup_all_connections(self):
        """Clean up all WebSocket connections (for shutdown)"""
        logger.info("Cleaning up all WebSocket connections")
        self.active_connections.clear()
        self.connection_rooms.clear()