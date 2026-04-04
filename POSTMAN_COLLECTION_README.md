# Kasookoo WebRTC SDK Backend - Postman Collection

This repository contains a comprehensive Postman collection for testing the Kasookoo WebRTC SDK Backend API.

## Files Included

1. **Kasookoo_WebRTC_SDK_Backend.postman_collection.json** - Main Postman collection
2. **Kasookoo_WebRTC_SDK_Backend.postman_environment.json** - Environment variables
3. **POSTMAN_COLLECTION_README.md** - This documentation file

## Import Instructions

### 1. Import Collection
1. Open Postman
2. Click "Import" button
3. Select `Kasookoo_WebRTC_SDK_Backend.postman_collection.json`
4. Click "Import"

### 2. Import Environment
1. In Postman, click the gear icon (⚙️) in the top right
2. Click "Import"
3. Select `Kasookoo_WebRTC_SDK_Backend.postman_environment.json`
4. Click "Import"
5. Select the imported environment from the dropdown

## API Overview

The API is organized into the following main sections:

### 1. Authentication
- **Get Access Token (Deprecated)** - OAuth2 token endpoint (deprecated, use static API key instead)

### 2. WebRTC - Call Management
- **Get Caller Token** - Generate LiveKit token for caller and initiate call
- **Get Called Token** - Generate LiveKit token for called user
- **End Call** - End an active call session
- **Get Call Status** - Get current call status and recording info
- **List Call Sessions** - List call sessions with search and pagination
- **List Active Calls** - List all active calls
- **Reject Call** - Reject an incoming call

### 3. WebRTC - Recording Management
- **Start Standalone Recording** - Start recording without call context
- **Get Recording Status** - Get recording status and information
- **Stop Standalone Recording** - Stop a standalone recording
- **Download Recording** - Download a recorded call file

### 4. WebRTC - WebSocket & Webhooks
- **WebSocket Connection** - Real-time call updates via WebSocket
- **LiveKit Webhook** - Handle LiveKit webhook events

### 5. SIP - Call Management
- **Make Outbound Call (JWT)** - Initiate SIP call using JWT auth
- **Make Outbound Call (Static API Key)** - Initiate SIP call using static API key
- **End SIP Call (JWT)** - End SIP call using JWT auth
- **Hangup SIP Call (Static API Key)** - End SIP call using static API key

### 6. SIP - Room & Token Management
- **Generate Room Token** - Generate access token for joining SIP room
- **List SIP Trunks** - List all configured SIP trunks

### 7. SIP - System & Health
- **Health Check** - Health check endpoint for SIP bridge
- **Root Endpoint** - API information endpoint
- **LiveKit Events Webhook** - Handle LiveKit events for SIP calls

## Environment Variables

The following environment variables are configured:

| Variable | Description | Example Value |
|----------|-------------|---------------|
| `base_url` | Base URL for the API | `http://localhost:7000` |
| `ws_url` | WebSocket URL | `ws://localhost:7000` |
| `access_token` | JWT access token | `your_jwt_access_token_here` |
| `static_api_key` | Static API key for authentication | `17537c5618b70cefe382dc33a39178010e7e24873f3897609d346a85` |
| `room_name` | Default room name for testing | `call_room_123` |
| `egress_id` | Recording egress ID | `EG_abc123` |
| `file_name` | Recording file name | `recording_20231201_120000` |
| `search_term` | Search term for call sessions | `call_room` |
| `livekit_webhook_auth` | LiveKit webhook authorization | `your_livekit_webhook_auth_here` |
| `livekit_url` | LiveKit server URL | `wss://your-livekit-server.com` |
| `livekit_api_key` | LiveKit API key | `your_livekit_api_key_here` |
| `livekit_api_secret` | LiveKit API secret | `your_livekit_api_secret_here` |

## Authentication Methods

The API supports two authentication methods:

### 1. JWT Token Authentication
- Use the `access_token` environment variable
- Token is obtained from the deprecated `/token` endpoint
- Used for most SDK endpoints

### 2. Static API Key Authentication
- Use the `static_api_key` environment variable
- Default value: `17537c5618b70cefe382dc33a39178010e7e24873f3897609d346a85`
- Used for SIP endpoints and some SDK endpoints

## Usage Examples

### 1. Making a WebRTC Call

1. **Get Caller Token**:
   - Set `room_name` in environment
   - Update request body with actual user IDs
   - Send request to get caller token

2. **Get Called Token**:
   - Use same `room_name`
   - Update request body with called user details
   - Send request to get called user token

3. **Monitor Call**:
   - Use WebSocket connection to monitor call status
   - Or use "Get Call Status" endpoint

### 2. Making a SIP Call

1. **Make Outbound Call**:
   - Use either JWT or Static API Key authentication
   - Set phone number and room name
   - Send request to initiate SIP call

2. **Generate Room Token**:
   - Get token for joining the SIP room
   - Use token to connect to LiveKit room

### 3. Recording Management

1. **Start Recording**:
   - Use "Start Standalone Recording" endpoint
   - Configure recording options (resolution, framerate, etc.)

2. **Monitor Recording**:
   - Use "Get Recording Status" with the returned `egress_id`

3. **Download Recording**:
   - Use "Download Recording" endpoint with room name and file name

## Request/Response Examples

### Get Caller Token Request
```json
{
  "participant_identity": "caller_user_123",
  "participant_identity_name": "John Doe",
  "participant_identity_type": "caller",
  "room_name": "call_room_123",
  "caller_user_id": "caller_user_123",
  "called_user_id": "called_user_456",
  "device_type": "mobile",
  "is_push_notification": true,
  "is_call_recording": true
}
```

### Get Caller Token Response
```json
{
  "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "wsUrl": "wss://your-livekit-server.com"
}
```

### Make SIP Call Request
```json
{
  "phone_number": "+1234567890",
  "room_name": "sip_call_123",
  "participant_name": "John Doe"
}
```

### Make SIP Call Response
```json
{
  "success": true,
  "message": "Call initiated successfully",
  "data": {
    "room_name": "sip_call_123",
    "call_id": "call_abc123"
  },
  "wsUrl": "wss://your-livekit-server.com"
}
```

## Error Handling

The API returns standard HTTP status codes:

- `200` - Success
- `400` - Bad Request
- `401` - Unauthorized
- `404` - Not Found
- `500` - Internal Server Error

Error responses include:
```json
{
  "success": false,
  "error": "Error message",
  "code": "ERROR_CODE",
  "details": {
    "additional": "error details"
  }
}
```

## WebSocket Usage

For real-time updates, connect to the WebSocket endpoint:
```
ws://localhost:7000/api/v1/webrtc/ws/calls/{room_name}
```

WebSocket messages include:
- `call_ended` - When a call ends
- `livekit_event` - LiveKit room events
- `status_update` - Call status changes

## Testing Tips

1. **Start with Health Check**: Use the health check endpoint to verify the API is running
2. **Use Environment Variables**: Update the environment variables with your actual values
3. **Test Authentication**: Verify your tokens and API keys are working
4. **Monitor Logs**: Check the server logs for detailed error information
5. **WebSocket Testing**: Use Postman's WebSocket feature or a separate WebSocket client

## Server Configuration

The API server runs on:
- **Host**: `0.0.0.0`
- **Port**: `7000`
- **Swagger UI**: `http://localhost:7000/swagger`

## Support

For issues or questions:
1. Check the server logs
2. Verify environment variables
3. Test with the Swagger UI at `/swagger`
4. Review the API documentation in the codebase

## Changelog

- **v1.0.0** - Initial Postman collection with all API endpoints
- Includes authentication, call management, recording, and SIP integration
- Comprehensive environment variables and examples
