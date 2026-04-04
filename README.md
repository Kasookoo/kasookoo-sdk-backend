# Kasookoo WebRTC SDK Backend

A comprehensive WebRTC and SIP calling backend service built with FastAPI, LiveKit, and MongoDB. This service provides real-time communication capabilities including WebRTC calls, SIP integration, call recording, and push notifications.

## 🚀 Features

### Core Functionality
- **WebRTC Call Management** - Initiate, manage, and end WebRTC calls
- **SIP Integration** - Make outbound SIP calls to phone numbers
- **Call Recording** - Record calls with LiveKit integration
- **Real-time Updates** - WebSocket support for live call status
- **Push Notifications** - Firebase Cloud Messaging integration
- **Token Management** - JWT and static API key authentication

### API Capabilities
- Generate LiveKit access tokens for participants
- Manage call sessions with MongoDB persistence
- Handle LiveKit webhooks for call events
- Download recorded call files from S3
- List active calls and call history
- SIP trunk management and configuration

## 🛠️ Technologies

- **Python 3.13+**
- **FastAPI** - Modern, fast web framework
- **LiveKit** - Real-time communication platform
- **MongoDB** - Database for call sessions and user data
- **Firebase Admin SDK** - Push notifications
- **Uvicorn** - ASGI server
- **WebSockets** - Real-time communication
- **AWS S3** - Call recording storage

## 📋 Prerequisites

- Python 3.13 or higher
- MongoDB instance (local or cloud)
- LiveKit server (self-hosted or cloud)
- Firebase project for push notifications
- AWS S3 bucket for call recordings (optional)

## 🚀 Quick Start

### 1. Clone and Setup

```bash
# Clone the repository
git clone <your-repo-url>
cd kasookoo-webrtc-sdk-backend

# Create and activate virtual environment
python -m venv venv
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Configuration

Create a `.env.local` file in the project root:

```env
# SDK Token Auth Settings
SDK_TOKEN_ALGORITHM=RS256
JWT_KID=default
JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
# Optional HS256 fallback (legacy mode only)
SDK_SIGNING_SECRET=replace-with-strong-random-secret
SDK_TOKEN_AUDIENCE=kasookoo-sdk-backend
SDK_TOKEN_ISSUER=kasookoo-integrator
SDK_TOKEN_LEEWAY_SECONDS=15
SDK_SESSION_DURATION_SECONDS=60
SDK_PUBLIC_ALLOWED_SCOPES=webrtc:token:create,webrtc:call:read,webrtc:call:end,messaging:token:create,messaging:send,recording:start,recording:read,recording:stop

# Static API Key (default test key)
STATIC_API_KEY=17537c5618b70cefe382dc33a39178010e7e24873f3897609d346a85

# LiveKit Settings (replace with your actual values)
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
LIVEKIT_URL=wss://your-livekit-server.com
SIP_OUTBOUND_TRUNK_ID=your-sip-trunk-id

# SDK LiveKit Settings
LIVEKIT_SDK_URL=wss://your-livekit-server.com
LIVEKIT_SDK_API_KEY=your-livekit-api-key
LIVEKIT_SDK_API_SECRET=your-livekit-api-secret
SDK_SIP_OUTBOUND_TRUNK_ID=ST_mCzRRndksJkk

# SIP Configuration
SIP_TRUNK_NAME=default-trunk
SIP_INBOUND_ADDRESSES=192.168.1.100
SIP_OUTBOUND_ADDRESS=sip.provider.com:5060
SIP_OUTBOUND_USERNAME=username
SIP_OUTBOUND_PASSWORD=password

# MongoDB Settings
MONGO_URI=mongodb://localhost:27017
DB_NAME=kasookoo_webrtc

# Clerk Settings (if using Clerk authentication)
CLERK_ISSUER=https://your-clerk-issuer.com
CLERK_SECRET_KEY=your-clerk-secret-key
CLERK_AUDIENCE=https://your-clerk-audience.com

# API Host Settings
API_HOST=https://webrtc.kasookoo.ai/api/v1/sdk
SERVER_API_HOST=https://sdk.kasookoo.ai/api/v1/bot
```

### 3. Run the Service

#### Option 1: Using Server Manager (Recommended)
```bash
# Activate virtual environment
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# Start server in background
python server_manager_simple.py start

# Check server status
python server_manager_simple.py status

# View server logs
python server_manager_simple.py logs

# Stop server
python server_manager_simple.py stop

# Restart server
python server_manager_simple.py restart

# Check server health
python server_manager_simple.py health
```

#### Option 2: Using Convenience Scripts
```bash
# Windows users
server.bat start      # Start server
server.bat status     # Check status
server.bat stop       # Stop server
server.bat logs       # View logs
server.bat dev        # Development mode (foreground)

# Unix/Linux/macOS users
./server.sh start     # Start server
./server.sh status    # Check status
./server.sh stop      # Stop server
./server.sh logs      # View logs
./server.sh dev       # Development mode (foreground)
```

#### Option 3: Using Uvicorn directly
```bash
# Activate virtual environment
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# Run the server
uvicorn app.main:app --host 0.0.0.0 --port 7000 --reload
```

#### Option 4: Using the startup script
```bash
# Activate virtual environment
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# Run using the startup script (foreground mode)
python start_server.py
```

#### Option 5: Using Python directly
```bash
# Activate virtual environment
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# Run the main module
python -m app.main
```

### 4. Run with Docker

> ℹ️ Ensure you have a `.env.local` file populated with all required settings before building the image.

#### Build the image
```bash
docker build -t kasookoo-backend .
```

#### Run the container
```bash
docker run --env-file .env.local -p 7000:7000 kasookoo-backend
```

#### Using Docker Compose
Spin up the API together with a MongoDB instance:
```bash
docker compose up --build
```

This exposes the API on `http://localhost:7000` and a MongoDB instance on `mongodb://localhost:27017`.

### 5. Verify Installation

Once the server is running, you should see:
```
INFO:     Uvicorn running on http://0.0.0.0:7000 (Press CTRL+C to quit)
INFO:     Started reloader process [XXXX] using WatchFiles
INFO:     Started server process [XXXX]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

Test the health endpoint:
```bash
curl http://localhost:7000/api/v1/sip/health
```

## 🛠️ Server Management

The project includes comprehensive server management tools for easy deployment and monitoring.

### Server Manager Commands

| Command | Description | Example |
|---------|-------------|---------|
| `start` | Start server in background | `python server_manager_simple.py start` |
| `stop` | Stop running server | `python server_manager_simple.py stop` |
| `restart` | Restart the server | `python server_manager_simple.py restart` |
| `status` | Show server status and health | `python server_manager_simple.py status` |
| `logs` | View recent server logs | `python server_manager_simple.py logs` |
| `health` | Check server health via API | `python server_manager_simple.py health` |

### Server Manager Options

- `--no-reload`: Disable auto-reload (for production)
- `--foreground`: Run in foreground (don't daemonize)
- `--lines N`: Show N lines of logs (default: 50)

### Example Server Management Workflow

```bash
# Start the server
python server_manager_simple.py start

# Check if it's running
python server_manager_simple.py status

# View logs if needed
python server_manager_simple.py logs

# Check health
python server_manager_simple.py health

# Stop when done
python server_manager_simple.py stop
```

### Process Management

The server manager automatically:
- ✅ Creates PID files for process tracking
- ✅ Logs server output to `server.log`
- ✅ Handles graceful shutdowns
- ✅ Monitors server health via API
- ✅ Provides detailed status information

## 📚 API Documentation

### Swagger UI
Once the server is running, visit:
- **Swagger UI**: http://localhost:7000/swagger
- **ReDoc**: http://localhost:7000/redoc

### Postman Collection
Import the provided Postman collection for comprehensive API testing:
- `Kasookoo_WebRTC_SDK_Backend.postman_collection.json`
- `Kasookoo_WebRTC_SDK_Backend.postman_environment.json`

See `POSTMAN_COLLECTION_README.md` for detailed usage instructions.

## 🔧 API Endpoints Overview

### Authentication
- `GET /api/v1/sdk/auth/introspect` - Inspect validated SDK token (debug)
- `GET /.well-known/jwks.json` - Public key set for RS256 token verification
- `POST /api/v1/sdk/auth/client-sessions` - Create frontend SDK session directly from Kasookoo backend
- `POST /api/v1/sdk/auth/token` - Admin/trusted mint endpoint (STATIC_API_KEY protected, optional)
- `POST /api/v1/sdk/auth/sessions/{session_id}/tokens` - Refresh short-lived SDK token
- `DELETE /api/v1/sdk/auth/sessions/{session_id}` - Revoke current session

Session state is now persisted in MongoDB collection `sdk_auth_sessions` (falls back to in-memory only if Mongo is unavailable during startup).

### WebRTC - Call Management
- `POST /api/v1/webrtc/get-caller-token` - Generate caller token
- `POST /api/v1/webrtc/get-called-token` - Generate called user token
- `POST /api/v1/webrtc/calls/{room_name}/end` - End call
- `GET /api/v1/webrtc/calls/{room_name}/status` - Get call status
- `GET /api/v1/webrtc/call/sessions` - List call sessions
- `GET /api/v1/webrtc/calls` - List active calls
- `POST /api/v1/webrtc/calls/reject` - Reject call

### WebRTC - Recording Management
- `POST /api/v1/webrtc/recordings/start` - Start recording
- `GET /api/v1/webrtc/recordings/{egress_id}/status` - Get recording status
- `POST /api/v1/webrtc/recordings/{egress_id}/stop` - Stop recording
- `GET /api/v1/webrtc/download-recording/{room_name}/{file_name}` - Download recording

### SIP - SIP Call Management
- `POST /api/v1/sip/calls/make` - Make SIP call (JWT auth)
- `POST /api/v1/sip/calls/dial` - Make SIP call (API key auth)
- `POST /api/v1/sip/calls/end` - End SIP call (JWT auth)
- `POST /api/v1/sip/calls/hangup` - End SIP call (API key auth)

### WebSocket & Webhooks
- `WS /api/v1/webrtc/ws/calls/{room_name}` - WebSocket connection
- `POST /api/v1/webrtc/webhooks/livekit` - LiveKit webhooks
- `POST /api/v1/sip/livekit-events` - SIP LiveKit events

## 🔐 Authentication

The API supports direct first-party SDK authentication and optional admin API-key auth:

### 1. Kasookoo Backend-Signed SDK Token (recommended for frontend SDK)
Frontend must not sign tokens. The Kasookoo SDK backend now issues short-lived JWTs directly via `POST /api/v1/sdk/auth/client-sessions`.

```bash
Authorization: Bearer <sdk_signed_jwt>
```

Expected JWT claims:
- `sub` (required)
- `exp` (required)
- `iat` (required)
- `aud` (recommended, should match `SDK_TOKEN_AUDIENCE`)
- `iss` (recommended, should match `SDK_TOKEN_ISSUER`)
- `organization_id` or `org_id` (required for org-scoped endpoints)
- `scopes` or `scope` (recommended)

### 2. Static API Key Authentication
Used only for specific endpoints that are intentionally API-key protected.

```bash
Authorization: Bearer <STATIC_API_KEY>
```

### Frontend SDK Token Fetch Pattern (no integrator backend)

```javascript
// frontend
async function getSdkToken() {
  const res = await fetch("https://sdk.kasookoo.ai/api/v1/sdk/auth/client-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sub: "guest-user-123",
      organization_id: "org_123",
      scopes: ["webrtc:token:create", "webrtc:call:read"],
      ttl_seconds: 60
    })
  });
  if (!res.ok) throw new Error("Failed to fetch SDK token");
  const data = await res.json();
  return { token: data.token, sessionId: data.session_id };
}

async function callKasookooApi(payload) {
  const { token } = await getSdkToken();
  return fetch("https://sdk.kasookoo.ai/api/v1/webrtc/sdk/get-caller-token", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
    },
    body: JSON.stringify(payload),
  });
}
```

### Backend-Signed SDK Token Flow (Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    participant FE as Frontend App (SDK Consumer)
    participant SDK as Kasookoo Frontend SDK
    participant KB as Kasookoo SDK Backend API
    participant DB as MongoDB (sdk_auth_sessions)
    participant JWKS as /.well-known/jwks.json

    Note over FE,KB: No end-user login required in SDK flow

    FE->>SDK: init({ publishableKey, ... })
    SDK->>KB: POST /api/v1/sdk/auth/client-sessions
    Note right of SDK: Body: sub, organization_id, scopes, ttl_seconds
    KB->>KB: Validate requested scopes against SDK_PUBLIC_ALLOWED_SCOPES
    KB->>DB: Upsert session (sid, sub, org, active=true)
    KB-->>SDK: { token, session_id, expires_in }

    SDK->>KB: API call + Authorization: Bearer <backendSignedJwt with sid>
    opt External verifier bootstrap
      SDK->>JWKS: GET /.well-known/jwks.json
      JWKS-->>SDK: RS256 public keys (kid)
    end
    KB->>KB: Verify JWT signature (RS256 via JWT_PUBLIC_KEY or HS256 fallback)
    KB->>KB: Validate exp/iat/aud/iss/sub/sid
    KB->>DB: Verify sid is active and belongs to sub
    KB->>KB: Validate scopes (require_scopes)
    KB->>KB: Validate organization_id (if needed)

    alt Token valid + scopes allowed
        KB-->>SDK: 200 Success + API response
        SDK-->>FE: Result
    else Token invalid/expired/missing scope
        KB-->>SDK: 401/403 Error
        SDK->>KB: Refresh token / create new client session
    end
```

### Sample Token Mint Endpoint Flow (Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    participant ADMIN as Trusted Server/Admin Tool
    participant KB as Kasookoo Backend Auth API
    participant JWT as JWT Signer (JWT_PRIVATE_KEY)
    participant DB as MongoDB (sdk_auth_sessions)

    Note over ADMIN,KB: Optional trusted endpoint for server-side/admin minting

    ADMIN->>KB: POST /api/v1/sdk/auth/token
    Note right of ADMIN: Headers: Authorization: Bearer <STATIC_API_KEY><br/>Body: sub, scopes, organization_id, ttl_seconds, session_id?, extra_claims

    KB->>KB: Validate static API key
    KB->>KB: Create/reuse sid + build claims (sub, sid, scopes, org, email, jti)
    KB->>DB: Upsert sid as active
    KB->>JWT: Sign token with JWT_PRIVATE_KEY
    Note right of JWT: Add iat, exp, aud, iss + header kid
    JWT-->>KB: signed JWT
    KB-->>ADMIN: { token, token_type, session_id, expires_in, audience, issuer }
```

### Session Refresh & Revoke Flow (Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    participant SDK as Kasookoo Frontend SDK
    participant KB as Kasookoo Backend Auth API
    participant DB as MongoDB (sdk_auth_sessions)

    SDK->>KB: POST /api/v1/sdk/auth/sessions/{sid}/tokens (Bearer currentToken)
    KB->>KB: Decode + verify JWT (signature, exp/iat/aud/iss, sid)
    KB->>DB: Check sid is active and owned by sub
    alt session active
        KB->>KB: Mint new short-lived token (same sid, new jti)
        KB-->>SDK: { token, session_id, expires_in }
    else session revoked/not found
        KB-->>SDK: 401 Session revoked or not found
    end

    SDK->>KB: DELETE /api/v1/sdk/auth/sessions/{sid} (Bearer currentToken)
    KB->>DB: Set active=false for sid
    KB-->>SDK: { message: "Session revoked" }
```

### Minimum Scopes by Endpoint

Use these scopes in your backend-signed SDK token (`scope` string or `scopes` array):

| Endpoint | Method | Required Scope |
|---|---|---|
| `/api/v1/sdk/get-caller-token` | `POST` | `webrtc:token:create` |
| `/api/v1/sdk/get-call-tokens` | `POST` | `webrtc:token:create` |
| `/api/v1/sdk/get-called-token` | `POST` | `webrtc:token:create` |
| `/api/v1/sdk/get-messaging-tokens` | `POST` | `messaging:token:create` |
| `/api/v1/sdk/calls/{room_name}/end` | `POST` | `webrtc:call:end` |
| `/api/v1/sdk/calls/{room_name}/status` | `GET` | `webrtc:call:read` |
| `/api/v1/sdk/calls` | `GET` | `webrtc:call:read` |
| `/api/v1/sdk/recordings/start` | `POST` | `recording:start` |
| `/api/v1/sdk/recordings/{egress_id}/status` | `GET` | `recording:read` |
| `/api/v1/sdk/recordings/{egress_id}/stop` | `POST` | `recording:stop` |
| `/api/v1/messaging/get-token` | `POST` | `messaging:token:create` |
| `/api/v1/messaging/send` | `POST` | `messaging:send` |

Example compact scope set for full SDK usage:

```text
webrtc:token:create webrtc:call:read webrtc:call:end messaging:token:create messaging:send recording:start recording:read recording:stop
```

## 🌐 WebSocket Usage

Connect to the WebSocket endpoint for real-time updates:
```javascript
const ws = new WebSocket('ws://localhost:7000/api/v1/webrtc/ws/calls/room_name');
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('Received:', data);
};
```

## 📱 Example Usage

### Making a WebRTC Call

1. **Get Caller Token**:
```bash
curl -X POST "http://localhost:7000/api/v1/webrtc/get-caller-token" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "participant_identity": "caller_123",
    "participant_identity_name": "John Doe",
    "participant_identity_type": "caller",
    "room_name": "call_room_123",
    "caller_user_id": "caller_123",
    "called_user_id": "called_456",
    "device_type": "mobile",
    "is_push_notification": true,
    "is_call_recording": true
  }'
```

2. **Get Called Token**:
```bash
curl -X POST "http://localhost:7000/api/v1/webrtc/get-called-token" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "participant_identity": "called_456",
    "participant_identity_name": "Jane Smith",
    "participant_identity_type": "customer",
    "room_name": "call_room_123",
    "called_user_id": "called_456",
    "is_call_recording": true
  }'
```

### Making a SIP Call

```bash
curl -X POST "http://localhost:7000/api/v1/sip/calls/dial" \
  -H "Authorization: Bearer 17537c5618b70cefe382dc33a39178010e7e24873f3897609d346a85" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+1234567890",
    "room_name": "sip_call_123",
    "participant_name": "John Doe"
  }'
```

## 🐛 Troubleshooting

### Common Issues

1. **Import Errors**: Ensure all dependencies are installed:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables**: Check that `.env.local` file exists and contains all required variables.

3. **Port Already in Use**: If port 7000 is busy, use a different port:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

4. **MongoDB Connection**: Ensure MongoDB is running and accessible.

5. **LiveKit Configuration**: Verify LiveKit server credentials and URL.

### Logs and Debugging

- Server logs are displayed in the terminal
- Use `--log-level debug` for detailed logging
- Check MongoDB for call session data
- Monitor LiveKit server logs for WebRTC issues

## 🔄 Development

### Code Structure
```
app/
├── api/           # API route handlers
├── models/        # Pydantic models
├── services/      # Business logic
├── utils/         # Utility functions
└── main.py        # FastAPI application
```

### Adding New Endpoints
1. Create route in appropriate `app/api/` file
2. Add Pydantic models in `app/models/`
3. Implement business logic in `app/services/`
4. Update Postman collection

## 📄 License

MIT License - see LICENSE file for details.

## 🤝 Support

For issues and questions:
1. Check the troubleshooting section
2. Review server logs
3. Test with Swagger UI at `/swagger`
4. Use the provided Postman collection