import logging
import uvicorn

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    associated_numbers,
    auth,
    cdr,
    dashboard,
    messaging,
    monitoring,
    notification,
    organization,
    sip,
    users,
    webrtc,
)
from app.config import *
from app.utils.metrics import PrometheusMiddleware

logger = logging.getLogger("agent-dispatcher-api")
logger.setLevel(logging.INFO)

app = FastAPI(
    swagger_ui_parameters={"syntaxHighlight.theme": "obsidian"},
    title="The AI BOT Outbound Call API",
    description="The AI BOT Outbound Call API for the Swagger UI.",
    version="1.0",
    docs_url="/swagger",
)

app.add_middleware(
    CORSMiddleware,
    #allow_origins=["*"],
    #allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add Prometheus middleware for metrics collection
app.add_middleware(PrometheusMiddleware)

# Include routers
app.include_router(auth.router)
app.include_router(webrtc.router, prefix="/api/v1", tags=["webrtc"])
app.include_router(sip.router, prefix="/api/v1", tags=["sip"])
app.include_router(notification.router, prefix="/api/v1", tags=["notification"])
app.include_router(messaging.router, prefix="/api/v1", tags=["messaging"])
app.include_router(associated_numbers.router, prefix="/api/v1", tags=["associated_numbers"])
app.include_router(cdr.router, prefix="/api/v1", tags=["cdr"])
app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
app.include_router(organization.router, prefix="/api/v1", tags=["organization"])
app.include_router(users.router, prefix="/api/v1", tags=["users"])
app.include_router(monitoring.router, prefix="/api/v1/monitoring", tags=["monitoring"])

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=7000,
        reload=True,
        log_level="info"
    )