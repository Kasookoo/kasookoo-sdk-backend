"""
CDR (Call Detail Records) API endpoints for call session reporting.
User enrichment is intentionally excluded.
"""
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

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


def _parse_cdr_query_dates(date_from: Optional[str], date_to: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
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
            raise HTTPException(status_code=400, detail="Invalid date_from format.")
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
            raise HTTPException(status_code=400, detail="Invalid date_to format.")
    return date_from_dt, date_to_dt


def _participant_csv_fields(p: Any) -> List[str]:
    if not p or not isinstance(p, dict):
        return ["", "", "", "", ""]
    return [
        str(p.get("identity") or p.get("id") or ""),
        str(p.get("name") or "").replace("\r", " ").replace("\n", " "),
        str(p.get("email") or "").replace("\r", " ").replace("\n", " "),
        str(p.get("phone_number") or p.get("caller_id") or "").replace("\r", " ").replace("\n", " "),
        str(p.get("role") or "").replace("\r", " ").replace("\n", " "),
    ]


def _cdr_doc_to_csv_row(doc: Dict[str, Any]) -> List[Any]:
    cp = doc.get("caller_participant")
    calp = doc.get("callee_participant")
    if isinstance(cp, dict) and cp.get("kind") == "SIP":
        cp, calp = calp, cp
    return [
        str(doc.get("_id") or ""),
        str(doc.get("organization_id") or ""),
        doc.get("room_name") or "",
        doc.get("call_id") or "",
        doc.get("status") or "",
        doc.get("kind") or "",
        doc.get("created_at") or "",
        doc.get("started_at") or "",
        doc.get("ended_at") or "",
        doc.get("duration_seconds") or "",
        doc.get("recording_status") or "",
        *_participant_csv_fields(cp),
        *_participant_csv_fields(calp),
        ";".join(str(x) for x in (doc.get("participants") or []) if x is not None),
    ]


_CSV_HEADERS = [
    "id", "organization_id", "room_name", "call_id", "status", "kind", "created_at", "started_at",
    "ended_at", "duration_seconds", "recording_status", "caller_identity", "caller_name",
    "caller_email", "caller_phone", "caller_role", "callee_identity", "callee_name", "callee_email",
    "callee_phone", "callee_role", "participants",
]


def _build_query(
    organization_id: str,
    search: Optional[str],
    caller_id: Optional[str],
    callee_id: Optional[str],
    kind: Optional[str],
    status: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    date_field: str,
) -> Dict[str, Any]:
    query: Dict[str, Any] = dict(org_filter(organization_id))
    if kind:
        query["kind"] = kind
    if status:
        query["status"] = status
    if caller_id:
        query["caller_id"] = caller_id
    if callee_id:
        query["callee_id"] = callee_id
    if date_from or date_to:
        date_query: Dict[str, Any] = {}
        if date_from:
            date_query["$gte"] = date_from
        if date_to:
            date_query["$lte"] = date_to
        query[date_field] = date_query
    if search:
        query["$or"] = [
            {"room_name": {"$regex": search, "$options": "i"}},
            {"call_id": {"$regex": search, "$options": "i"}},
            {"caller_participant.name": {"$regex": search, "$options": "i"}},
            {"callee_participant.name": {"$regex": search, "$options": "i"}},
            {"caller_participant.phone_number": {"$regex": search, "$options": "i"}},
            {"callee_participant.phone_number": {"$regex": search, "$options": "i"}},
        ]
    return query


@router.get("/cdr/sessions/export")
@monitor(name="api.cdr.export_call_sessions_csv")
async def export_call_sessions_csv(
    search: str = Query(None),
    caller_id: str = Query(None),
    callee_id: str = Query(None),
    kind: str = Query(None),
    status: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    date_field: str = Query("created_at"),
    max_rows: int = Query(50000, ge=1, le=100000),
    manager: WebRTCCallManager = Depends(get_call_manager),
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
):
    date_from_dt, date_to_dt = _parse_cdr_query_dates(date_from, date_to)
    if date_field not in ["created_at", "started_at", "ended_at"]:
        raise HTTPException(status_code=400, detail="date_field must be one of: 'created_at', 'started_at', 'ended_at'")
    query = _build_query(
        organization_id, search, caller_id, callee_id, kind, status, date_from_dt, date_to_dt, date_field
    )

    filename = f"cdr_sessions_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"

    async def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        yield "\ufeff".encode("utf-8")
        writer.writerow(_CSV_HEADERS)
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)
        cursor = manager.calls_collection.find(query).sort("created_at", -1).limit(max_rows)
        for doc in cursor:
            writer.writerow(_cdr_doc_to_csv_row(doc))
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/cdr/sessions")
@monitor(name="api.cdr.get_call_sessions")
async def get_call_sessions(
    search: str = Query(None),
    caller_id: str = Query(None),
    callee_id: str = Query(None),
    kind: str = Query(None),
    status: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    date_field: str = Query("created_at"),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    manager: WebRTCCallManager = Depends(get_call_manager),
    token: str = Depends(sdk_token_scheme),
    organization_id: str = Depends(get_organization_id),
):
    try:
        date_from_dt, date_to_dt = _parse_cdr_query_dates(date_from, date_to)
        if date_field not in ["created_at", "started_at", "ended_at"]:
            raise HTTPException(status_code=400, detail="date_field must be one of: 'created_at', 'started_at', 'ended_at'")
        query = _build_query(
            organization_id, search, caller_id, callee_id, kind, status, date_from_dt, date_to_dt, date_field
        )
        total = manager.calls_collection.count_documents(query)
        cursor = manager.calls_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
        sessions = list(cursor)
        for session in sessions:
            if "_id" in session:
                session["id"] = str(session["_id"])
                del session["_id"]
            session.pop("caller_id", None)
            session.pop("callee_id", None)
        return {"total": total, "skip": skip, "limit": limit, "sessions": sessions}
    except HTTPException:
        raise
    except Exception as error:
        logger.error(f"Failed to list call sessions: {error}")
        raise HTTPException(status_code=500, detail=str(error))
