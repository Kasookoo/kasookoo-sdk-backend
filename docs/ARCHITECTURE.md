# System Architecture

This document describes the architecture of the Kasookoo SDK Backend, including runtime components, deployment topology, and authentication/session flow.

## 1) High-Level System Architecture

```mermaid
flowchart TB
    FE[Browser and Mobile Apps]
    SDK[Kasookoo Frontend SDK]
    Caddy[Caddy or API Gateway]

    subgraph App[Kasookoo SDK Backend]
      API[FastAPI API Layer]
      Auth[SDK Auth and Session APIs]
      Security[Depends Interceptor and Scope Guards]
      CallMgr[Call Manager]
      SIPBridge[LiveKit SIP Bridge]
      TokenSvc[LiveKit Token Service]
      MsgSvc[Messaging Service]
      NotifSvc[Notification Service]
      AssocSvc[Associated Number Service]
      UserSvc[User Service]
      OrgSvc[Organization Service]
    end

    Mongo[(MongoDB)]
    Redis[(Redis Cache)]

    subgraph LiveKit[LiveKit Stack]
      LK[LiveKit Server]
      SIP[LiveKit SIP]
      Egress[LiveKit Egress]
    end

    S3[AWS S3]
    FCM[Firebase FCM]
    SIPNET[SIP Trunk Provider]

    FE --> Caddy
    SDK --> Caddy
    Caddy --> API

    API --> Security
    API --> Auth
    API --> CallMgr
    API --> SIPBridge
    API --> TokenSvc
    API --> MsgSvc
    API --> NotifSvc
    API --> AssocSvc
    API --> UserSvc
    API --> OrgSvc

    Auth --> Mongo
    Auth <--> Redis
    UserSvc --> Mongo
    UserSvc <--> Redis
    OrgSvc --> Mongo
    OrgSvc <--> Redis
    MsgSvc --> Mongo
    CallMgr --> Mongo

    CallMgr --> LK
    SIPBridge --> SIP
    SIP --> SIPNET
    LK --> Egress
    Egress --> S3
    NotifSvc --> FCM
```

### Explanation

- Clients access the backend through API gateway/Caddy.
- FastAPI routers use dependency-based auth/interceptor checks before business handlers.
- MongoDB is the source of truth for sessions, users, organizations, calls, and messaging.
- Redis is a cache layer for session, user, and organization hot-read paths.
- LiveKit handles media sessions; SIP flows route via LiveKit SIP; recordings are exported to S3.

## 2) Component Diagram (Code Structure)

```mermaid
flowchart TB
    MAIN[app/main.py]

    subgraph API[app/api]
      A_AUTH[auth.py]
      A_WRTC[webrtc.py]
      A_SIP[sip.py]
      A_MSG[messaging.py]
      A_CDR[cdr.py]
      A_DASH[dashboard.py]
      A_NOTIF[notification.py]
      A_ASSOC[associated_numbers.py]
      A_MON[monitoring.py]
    end

    subgraph SEC[Security]
      INTERCEPT[app/security/interceptor.py]
    end

    subgraph SVC[app/services]
      CALLMGR[call_manager.py]
      TOKENSVC[token_service.py]
      RECSVC[recording_manager.py]
      MSGSVC[messaging_service.py]
      NOTIFSVC[notification_service.py]
      ASSOCSVC[associated_number_service.py]
      USERSVC[user_service.py]
      ORGSVC[organization_service.py]
      SIPSVC[livekit_sip_bridge.py]
    end

    DB[(MongoDB)]
    CACHE[(Redis)]
    LIVEKIT[LiveKit]

    MAIN --> API
    A_WRTC --> INTERCEPT
    A_MSG --> INTERCEPT
    A_AUTH --> TOKENSVC

    A_WRTC --> CALLMGR
    A_WRTC --> RECSVC
    A_SIP --> SIPSVC
    A_MSG --> MSGSVC
    A_NOTIF --> NOTIFSVC
    A_ASSOC --> ASSOCSVC
    A_CDR --> CALLMGR
    A_DASH --> USERSVC
    A_DASH --> ORGSVC

    USERSVC --> DB
    ORGSVC --> DB
    MSGSVC --> DB
    CALLMGR --> DB
    A_AUTH --> DB

    USERSVC <--> CACHE
    ORGSVC <--> CACHE
    A_AUTH <--> CACHE

    CALLMGR --> LIVEKIT
    RECSVC --> LIVEKIT
    SIPSVC --> LIVEKIT
```

## 3) Deployment Architecture

```mermaid
flowchart TB
    Internet[(Internet)]
    Clients[Frontend Clients]

    subgraph Host[Host VM or Kubernetes Node]
      Proxy[Caddy or Ingress]
      API[Kasookoo SDK Backend :7000]
      Redis[(Redis :6379)]
      Prom[Prometheus]
      Grafana[Grafana]
    end

    Mongo[(MongoDB / Atlas)]
    LiveKit[LiveKit Cluster]
    S3[(AWS S3)]
    FCM[Firebase]
    SIP[SIP Trunk Provider]

    Clients --> Internet --> Proxy --> API
    API --> Redis
    API --> Mongo
    API --> LiveKit
    API --> FCM
    API --> S3
    LiveKit --> SIP
    API --> Prom
    Grafana --> Prom
```

## 4) SDK Auth + Session Flow

```mermaid
sequenceDiagram
    participant FE as Frontend App
    participant API as SDK Backend API
    participant Auth as Auth Depends
    participant Redis as Redis
    participant Mongo as MongoDB

    FE->>API: POST /api/v1/sdk/auth/client-sessions
    API->>Mongo: Upsert sdk_auth_sessions(sid, sub, org, active=true)
    API->>Redis: Cache session metadata (optional)
    API-->>FE: token + session_id + expires_in

    FE->>API: Request protected endpoint with Bearer token
    API->>Auth: Validate token + scopes + sid
    Auth->>Redis: Lookup cached session
    alt Cache hit
      Redis-->>Auth: session active
    else Cache miss
      Auth->>Mongo: Validate sdk_auth_sessions sid/sub/active
      Mongo-->>Auth: session record
      Auth->>Redis: Set session cache
    end
    Auth-->>API: principal accepted
    API-->>FE: 200 OK

    FE->>API: DELETE /api/v1/sdk/auth/sessions/{sid}
    API->>Mongo: set active=false
    API->>Redis: delete session cache
    API-->>FE: revoked
```

## 5) Scalable Architecture (Target)

```mermaid
flowchart TB
    U[Clients] --> DNS[DNS and WAF] --> LB[Regional Load Balancer]

    subgraph Edge[Edge Layer]
      E1[Ingress or Caddy A]
      E2[Ingress or Caddy B]
    end
    LB --> E1
    LB --> E2

    subgraph App[Stateless API Layer]
      A1[SDK Backend Pod 1]
      A2[SDK Backend Pod 2]
      AN[SDK Backend Pod N]
    end
    E1 --> A1
    E1 --> A2
    E2 --> A2
    E2 --> AN

    subgraph Data[Data Layer]
      RC[(Redis HA Cluster)]
      MP[(Mongo Primary)]
      MR[(Mongo Replicas or Shards)]
    end
    A1 <--> RC
    A2 <--> RC
    AN <--> RC
    A1 --> MP
    A2 --> MP
    AN --> MP
    MP --- MR

    subgraph Realtime[Realtime Layer]
      LKLB[LiveKit Front Door]
      LK1[LiveKit Node 1]
      LK2[LiveKit Node 2]
      SIP1[SIP Service 1]
      SIP2[SIP Service 2]
      Egress[Egress Worker Pool]
    end
    A1 --> LKLB
    A2 --> LKLB
    AN --> LKLB
    LKLB --> LK1
    LKLB --> LK2
    LK1 --> SIP1
    LK2 --> SIP2
    LK1 --> Egress
    LK2 --> Egress
```

