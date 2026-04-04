"""
Dashboard API: CDR aggregates for charts.
User module data is intentionally excluded.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import get_organization_id, sdk_token_scheme
from app.services.call_manager import WebRTCCallManager
from app.services.recording_manager import LiveKitS3RecordingManager
from app.services.token_service import TokenService
from app.utils.mongodb_org import org_filter
from app.utils.performance_monitor import monitor

logger = logging.getLogger(__name__)
router = APIRouter()

recording_manager = LiveKitS3RecordingManager()
token_service = TokenService()
call_manager = WebRTCCallManager(recording_manager, token_service)


def get_call_manager() -> WebRTCCallManager:
    return call_manager


def _parse_dashboard_dates(date_from: Optional[str], date_to: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    date_from_dt = None
    date_to_dt = None
    if date_from:
        try:
            try:
                date_from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                if date_from_dt.tzinfo is None:
                    date_from_dt = date_from_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                date_from_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from.")
    if date_to:
        try:
            is_date_only = len(date_to) == 10 and date_to.count("-") == 2
            if is_date_only:
                date_to_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
                )
            else:
                date_to_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                if date_to_dt.tzinfo is None:
                    date_to_dt = date_to_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to.")
    return date_from_dt, date_to_dt


def _as_label_count(items):
    return [{"label": item["_id"] or "unknown", "count": item["count"]} for item in items]


@router.get("/dashboard/summary")
@monitor(name="api.dashboard.summary")
async def get_dashboard_summary(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    date_field: str = Query("created_at"),
    manager: WebRTCCallManager = Depends(get_call_manager),
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
) -> Dict[str, Any]:
    if date_field not in ("created_at", "started_at", "ended_at"):
        raise HTTPException(status_code=400, detail="date_field must be one of: created_at, started_at, ended_at")
    date_from_dt, date_to_dt = _parse_dashboard_dates(date_from, date_to)
    match = dict(org_filter(organization_id))
    if date_from_dt or date_to_dt:
        date_query: Dict[str, Any] = {}
        if date_from_dt:
            date_query["$gte"] = date_from_dt
        if date_to_dt:
            date_query["$lte"] = date_to_dt
        match[date_field] = date_query
    try:
        by_status = list(
            manager.calls_collection.aggregate([{"$match": match}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}])
        )
        by_kind = list(
            manager.calls_collection.aggregate([{"$match": match}, {"$group": {"_id": "$kind", "count": {"$sum": 1}}}])
        )
        by_recording_status = list(
            manager.calls_collection.aggregate(
                [{"$match": match}, {"$group": {"_id": "$recording_status", "count": {"$sum": 1}}}]
            )
        )
        total_calls = manager.calls_collection.count_documents(match)
    except Exception as error:
        logger.error({"event": "dashboard_summary_failed", "error": str(error)})
        raise HTTPException(status_code=500, detail=str(error))

    return {
        "organization_id": organization_id,
        "users": [],
        "calls": {
            "total": total_calls,
            "by_status": _as_label_count(by_status),
            "by_kind": _as_label_count(by_kind),
            "by_recording_status": _as_label_count(by_recording_status),
        },
        "period": {"date_from": date_from, "date_to": date_to, "date_field": date_field},
    }
