# Kasookoo WebRTC SDK Backend

A comprehensive WebRTC and SIP calling backend service built with FastAPI, LiveKit, and MongoDB. This service provides real-time communication capabilities including WebRTC calls, SIP integration, call recording, and push notifications.

## 🚀 Features

### Core Functionality
- **WebRTC Call Management** - Initiate, manage, and end WebRTC calls
- **SIP Integration** - Make outbound SIP calls to phone numbers
- **Call Recording** - Record calls with LiveKit integration
- **Real-time Updates** - WebSocket support for live call status
- **Push Notifications** - Firebase Cloud Messaging integration
- **Token Management** - Backend-signed SDK JWT (`client-sessions`) for all API authentication

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

## Architecture

The diagrams below describe the **current** deployment shape: a single **FastAPI** process exposing REST (and WebSocket) APIs, with auth enforced via **`Depends`** (see `app/security/interceptor.py` and `app/api/auth.py`) before route handlers run.

### System context

Who talks to what at a high level: clients, this backend, and external systems.

```mermaid
flowchart TB
    subgraph clients["Clients"]
        FE["Browser / mobile apps"]
        SDK["Frontend SDK"]
    end

    subgraph backend["Kasookoo SDK Backend (this service)"]
        API["FastAPI app\n(main.py)"]
    end

    subgraph data["Data and realtime"]
        MONGO[("MongoDB")]
        LK["LiveKit\n(WebRTC / rooms)"]
    end

    subgraph integrations["Integrations"]
        S3["AWS S3\n(recordings)"]
        FCM["Firebase\n(push)"]
        SIPNET["SIP / PSTN\n(via LiveKit SIP)"]
    end

    FE --> API
    SDK --> API
    API --> MONGO
    API --> LK
    API --> S3
    API --> FCM
    API --> SIPNET
```

**Notes:** SDK JWT issuance and session storage use MongoDB (`sdk_auth_sessions`, users, calls, messaging, etc.). LiveKit issues participant tokens computed server-side. Optional recording egress targets S3. Notifications use Firebase when configured.

### Component diagram (inside this repository)

Major **packages** and how they relate. Arrows show the main dependency direction (calls / uses).

```mermaid
flowchart TB
    subgraph http["HTTP entry"]
        MAIN["app/main.py\nFastAPI + CORS + Prometheus"]
    end

    subgraph routers["API routers app/api/"]
        R_AUTH["auth"]
        R_WRTC["webrtc"]
        R_SIP["sip"]
        R_MSG["messaging"]
        R_NOTIF["notification"]
        R_ASSOC["associated_numbers"]
        R_CDR["cdr + dashboard"]
        R_MON["monitoring"]
    end

    subgraph security["Security"]
        SEC["app/security/\ninterceptor.py"]
    end

    subgraph coreauth["Auth and tokens"]
        AUTH["auth.py\nJWT, sessions, JWKS,\nclient-sessions"]
    end

    subgraph services["app/services/"]
        CM["call_manager"]
        RM["recording_manager"]
        TS["token_service"]
        MS["messaging_service"]
        NS["notification services"]
        US["user_service"]
        OS["organization_service"]
        ANS["associated_number_service"]
        SIPB["livekit_sip_bridge"]
    end

    subgraph utils["app/utils/"]
        WSM["websocket_manager"]
        MET["metrics / performance_monitor"]
    end

    MAIN --> routers
    R_WRTC --> SEC
    R_MSG --> SEC
    R_ASSOC --> AUTH
    R_CDR --> AUTH
    R_AUTH --> AUTH
    SEC --> AUTH
    R_MON --> MET
    R_WRTC --> CM
    R_WRTC --> RM
    R_WRTC --> TS
    R_WRTC --> WSM
    R_MSG --> MS
    R_MSG --> US
    R_SIP --> SIPB
    R_NOTIF --> NS
    R_ASSOC --> ANS
    R_CDR --> CM
    CM --> MONGO[("MongoDB")]
    RM --> MONGO
    MS --> MONGO
    AUTH --> MONGO
    CM --> LK["LiveKit APIs"]
    RM --> LK
    RM --> S3["S3"]
    MAIN --> MET
```

**How to read it:** Routers are thin HTTP adapters. **`webrtc`** and **`messaging`** use **`intercept_sdk_access` / `authenticate_sdk_user`** (`interceptor.py`) before handlers; they delegate to **`auth.py`** for JWT verification, session checks, and scopes. **`associated_numbers`**, **`cdr`**, and **`dashboard`** rely on **`auth.py`** (e.g. `get_organization_id`) for JWT-backed org context. **`monitoring`** ties into **`metrics`** (Prometheus); it is not part of the SDK JWT interceptor chain. **`WebRTCCallManager`** coordinates call state and persistence; **`LiveKitS3RecordingManager`** handles egress/recording; **`MessagingService`** owns chat persistence; **`associated_number_service`** backs number mapping. **`notification`** routes proxy into the notification stack for FCM.

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

# Optional: outbound HTTP only (e.g. notification proxy to another service). Not used to authenticate callers to this API.
STATIC_API_KEY=replace-if-needed

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
- `POST /api/v1/sdk/auth/sessions/{session_id}/tokens` - Mint a new short-lived SDK JWT for the same `session_id` (`sid` in token)
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
- `POST /api/v1/sip/calls/dial` - Make SIP call (SDK JWT auth)
- `POST /api/v1/sip/calls/end` - End SIP call (JWT auth)
- `POST /api/v1/sip/calls/hangup` - End SIP call (SDK JWT auth)

### WebSocket & Webhooks
- `WS /api/v1/webrtc/ws/calls/{room_name}` - WebSocket connection
- `POST /api/v1/webrtc/webhooks/livekit` - LiveKit webhooks
- `POST /api/v1/sip/livekit-events` - SIP LiveKit events

## 🔐 Authentication

**Callers authenticate only with a backend-signed SDK JWT** in `Authorization: Bearer <token>`. Tokens are minted by this service via **`POST /api/v1/sdk/auth/client-sessions`** (short TTL, e.g. 60 seconds). There is **no** separate refresh-token or static API key for accessing these APIs.

### Request pipeline (Spring Boot–style “interceptor”)

FastAPI resolves `Depends(...)` **before** your route handler runs—similar to a Spring `HandlerInterceptor.preHandle` or a servlet filter that runs first, then the controller.

For SDK JWT routes, use helpers in `app/security/interceptor.py`:

- **`authenticate_sdk_user`** — validates `Authorization: Bearer` SDK JWT and session only (authentication).
- **`intercept_sdk_access(["scope:a", ...])`** — authentication **then** required scopes (authorization); same chain as `require_scopes` in `app/api/auth.py`.

Handlers should rely on the injected principal from these dependencies instead of decoding the JWT again. WebRTC routes use this pattern.

### Backend-signed SDK JWT (only supported client auth)
The frontend **must not** sign API JWTs itself. This backend issues short-lived JWTs via **`POST /api/v1/sdk/auth/client-sessions`**. Re-mint on expiry (same endpoint) or optionally rotate in place with **`POST /api/v1/sdk/auth/sessions/{sid}/tokens`** (see next diagram).

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

### Frontend SDK token fetch (no separate login service)

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

### Session token: mint and use (sequence diagram)

**First session JWT** (`client-sessions`) and **using it** on protected APIs. When `exp` is near, call **`client-sessions` again** (new `sid`) or use **optional session rotation** in the next diagram—there is **no OAuth-style refresh token**.

```mermaid
sequenceDiagram
    autonumber
    participant FE as Frontend App
    participant SDK as Kasookoo Frontend SDK
    participant KB as Kasookoo SDK Backend API
    participant AUTH as Auth Depends intercept_sdk_access
    participant DB as MongoDB sdk_auth_sessions
    participant JWKS as JWKS well-known

    Note over FE,KB: No end-user login required in SDK flow

    FE->>SDK: init publishableKey
    SDK->>KB: POST client-sessions
    Note right of SDK: Body with sub org scopes ttl
    KB->>KB: Validate scopes SDK_PUBLIC_ALLOWED_SCOPES
    KB->>DB: Upsert session sid sub org active
    KB-->>SDK: token session_id expires_in

    Note over SDK,AUTH: Depends runs before route handler
    SDK->>KB: HTTP Bearer JWT with sid
    KB->>AUTH: FastAPI Depends intercept_sdk_access first
    AUTH->>AUTH: Verify JWT RS256 HS256 claims
    AUTH->>DB: Verify sid active for sub
    AUTH->>AUTH: Enforce required scopes
    opt External JWKS optional
      SDK->>JWKS: GET well-known jwks
      JWKS-->>SDK: RS256 public keys
    end

    alt Auth dependency succeeds
        AUTH-->>KB: Principal OK handler runs
        KB->>KB: Business logic LiveKit org checks
        KB-->>SDK: 200 API response
        SDK-->>FE: Result
    else Auth dependency fails
        AUTH-->>KB: 401 or 403 handler skipped
        KB-->>SDK: Error response
        SDK->>KB: New client-sessions or POST sessions sid tokens
    end
```

**What this diagram shows (step by step):**

- The host app initializes the Kasookoo frontend SDK (for example with a publishable key or other init options).
- The SDK calls **`POST /api/v1/sdk/auth/client-sessions`** with `sub`, optional `organization_id`, requested `scopes`, and `ttl_seconds`.
- The backend checks that every requested scope is allowed (**`SDK_PUBLIC_ALLOWED_SCOPES`**).
- The backend creates or updates a row in **`sdk_auth_sessions`**: a session id (**`sid`**) tied to `sub` and org, marked **active**.
- The backend returns a short-lived **JWT** (`token`), **`session_id`** (same as `sid`), and **`expires_in`**.
- For later calls (WebRTC, messaging, etc.), the SDK sends **`Authorization: Bearer <token>`**. **FastAPI runs the auth dependency chain first** (`intercept_sdk_access` / `authenticate_sdk_user` in `app/security/interceptor.py`)—the same idea as a Spring interceptor **before** the controller method. Only if that succeeds does the **route handler** run (issue LiveKit token, etc.).
- Optionally, external clients may fetch **`GET /.well-known/jwks.json`** to verify RS256 signatures offline; this server verifies using configured keys inside the **Auth Depends** step.
- The interceptor step verifies the JWT (signature, time claims, audience/issuer if configured, **`sub`**, **`sid`**), confirms the session is **active** in the DB and matches **`sub`**, and enforces **required scopes** (and org headers where applicable).
- If the dependency chain succeeds, the handler returns **200** and the SDK surfaces the result to the app.
- If it fails, the client gets **401/403** and the business handler does **not** run; the SDK should call **`client-sessions` again** (new JWT + new `sid`) or **`POST .../sessions/{sid}/tokens`** if it still holds a valid session (next diagram).

### Optional: same-session JWT rotation and revoke (sequence diagram)

```mermaid
sequenceDiagram
    autonumber
    participant SDK as Kasookoo Frontend SDK
    participant KB as Kasookoo Backend Auth API
    participant AUTH as Auth Depends get_sdk_principal
    participant DB as MongoDB sdk_auth_sessions

    SDK->>KB: POST sessions sid tokens Bearer
    KB->>AUTH: Depends validate JWT and session
    AUTH->>AUTH: Decode JWT path sid matches token sid
    AUTH->>DB: Check sid active owned by sub
    alt session active
        AUTH-->>KB: Principal OK handler runs
        KB->>KB: Mint new token same sid new jti
        KB-->>SDK: token session_id expires_in
    else session revoked or missing
        AUTH-->>KB: Fail
        KB-->>SDK: 401 Session revoked or not found
    end

    SDK->>KB: DELETE sessions sid Bearer
    KB->>AUTH: Depends validate JWT ownership
    AUTH->>DB: Confirm sid matches principal
    AUTH-->>KB: OK revoke handler runs
    KB->>DB: Set active false for sid
    KB-->>SDK: Session revoked message
```

**What this diagram shows (step by step):**

- **Rotate JWT (same `sid`):** The SDK calls **`POST /api/v1/sdk/auth/sessions/{sid}/tokens`** with the **current** JWT in `Authorization: Bearer ...`. The path **`{sid}`** must match the **`sid`** inside the token. This mints a **new** short-lived JWT; it is **not** a refresh-token grant.
- **Auth Depends (`get_sdk_principal`)** runs **before** the refresh logic: decode/verify JWT, ensure **`sid`** matches the path, and check **`sdk_auth_sessions`** for an **active** session owned by **`sub`**.
- If that dependency succeeds, the route handler **mints** a **new** JWT with the **same** `sid`, a new **`jti`**, and returns `token`, `session_id`, and `expires_in`.
- If the session was revoked or missing, the dependency fails and the API returns **401** (“Session revoked or not found”) **without** minting.
- **Revoke (logout):** The SDK calls **`DELETE /api/v1/sdk/auth/sessions/{sid}`** with the current Bearer token. The same **Auth Depends** step validates the token first; then the handler sets **`active=false`** for that `sid` so existing JWTs stop working on the next request.
- The API confirms revocation with a short JSON message. After revoke, the client must use **`client-sessions`** again (first diagram) to obtain a **new** session if the user continues using the app.

**How the two diagrams connect:** Diagram 1 **creates** `sid` and the first `token`. Diagram 2 **reuses** that `sid` to **rotate** the JWT in place or **end** the session (revoke). Business API calls in Diagram 1 always use the latest valid JWT from **`client-sessions`** or, if you use sessions, from **`POST .../sessions/{sid}/tokens`**.

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