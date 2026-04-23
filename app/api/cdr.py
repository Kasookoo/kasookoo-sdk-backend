"""
CDR (Call Detail Records) API endpoints for call session management and reporting
"""
import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.api.auth import get_organization_id, oauth2_scheme
from app.services import user_service
from app.services.call_manager import WebRTCCallManager
from app.utils.performance_monitor import monitor

logger = logging.getLogger(__name__)

router = APIRouter()

# Initialize call manager
from app.services.recording_manager import LiveKitS3RecordingManager
from app.services.token_service import TokenService
recording_manager = LiveKitS3RecordingManager()
token_service = TokenService()
call_manager = WebRTCCallManager(recording_manager, token_service)


def get_call_manager() -> WebRTCCallManager:
    return call_manager


def _parse_cdr_query_dates(
    date_from: Optional[str],
    date_to: Optional[str],
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Parse optional date_from / date_to query strings (same rules as list endpoint)."""
    date_from_dt = None
    date_to_dt = None

    if date_from:
        try:
            try:
                date_from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                if date_from_dt.tzinfo is None:
                    date_from_dt = date_from_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                date_from_dt = datetime.strptime(date_from, "%Y-%m-%d")
                date_from_dt = date_from_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date_from format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
            )

    if date_to:
        try:
            is_date_only = len(date_to) == 10 and date_to.count("-") == 2
            if is_date_only:
                date_to_dt = datetime.strptime(date_to, "%Y-%m-%d")
                date_to_dt = date_to_dt.replace(
                    hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
                )
            else:
                date_to_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                if date_to_dt.tzinfo is None:
                    date_to_dt = date_to_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date_to format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
            )

    return date_from_dt, date_to_dt


def _participant_csv_fields(p: Any) -> List[str]:
    if not p or not isinstance(p, dict):
        return ["", "", "", "", ""]
    ident = p.get("identity") or p.get("id")
    if ident is not None:
        ident = str(ident)
    name = (p.get("name") or "").replace("\r", " ").replace("\n", " ")
    email = (p.get("email") or "").replace("\r", " ").replace("\n", " ")
    phone = (p.get("phone_number") or p.get("caller_id") or "").replace("\r", " ").replace("\n", " ")
    role = (p.get("role") or "").replace("\r", " ").replace("\n", " ")
    return [ident or "", name, email, phone, role]


def _cdr_doc_to_csv_row(doc: Dict[str, Any]) -> List[Any]:
    """One flat CSV row from a serialized call_sessions document."""
    cp = doc.get("caller_participant")
    calp = doc.get("callee_participant")
    if isinstance(cp, dict) and cp.get("kind") == "SIP":
        cp, calp = calp, cp

    oid = doc.get("_id")
    id_str = str(oid) if oid is not None else ""
    org = doc.get("organization_id")
    org_str = str(org) if org is not None else ""

    participants = doc.get("participants")
    if isinstance(participants, list):
        participants_str = ";".join(str(x) for x in participants if x is not None)
    else:
        participants_str = ""

    dur = doc.get("duration_seconds")
    dur_out = "" if dur is None else dur

    return [
        id_str,
        org_str,
        doc.get("room_name") or "",
        doc.get("call_id") or "",
        doc.get("status") or "",
        doc.get("kind") or "",
        doc.get("created_at") or "",
        doc.get("started_at") or "",
        doc.get("ended_at") or "",
        dur_out,
        doc.get("recording_status") or "",
        *_participant_csv_fields(cp),
        *_participant_csv_fields(calp),
        participants_str,
    ]


def _content_disposition_attachment(filename: str) -> str:
    """RFC 5987 + quoted filename so browsers treat the response as a file download."""
    safe = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    # filename* helps non-ASCII names; ASCII-only names work with filename= too
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(safe)}"


_CSV_HEADERS = [
    "id",
    "organization_id",
    "room_name",
    "call_id",
    "status",
    "kind",
    "created_at",
    "started_at",
    "ended_at",
    "duration_seconds",
    "recording_status",
    "caller_identity",
    "caller_name",
    "caller_email",
    "caller_phone",
    "caller_role",
    "callee_identity",
    "callee_name",
    "callee_email",
    "callee_phone",
    "callee_role",
    "participants",
]


@router.get("/cdr/sessions/export")
@monitor(name="api.cdr.export_call_sessions_csv")
async def export_call_sessions_csv(
    search: str = Query(None, description="Same as GET /cdr/sessions"),
    caller_id: str = Query(None),
    callee_id: str = Query(None),
    kind: str = Query(None),
    status: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    date_field: str = Query("created_at"),
    max_rows: int = Query(50000, ge=1, le=100000, description="Maximum rows in CSV (cap 100k)"),
    manager: WebRTCCallManager = Depends(get_call_manager),
    token: str = Depends(oauth2_scheme),
    organization_id: str = Depends(get_organization_id),
):
    """
    Download call sessions (CDR) as a UTF-8 CSV file. Uses the same filters as GET /cdr/sessions.
    Excel-friendly: includes BOM. Rows are capped by max_rows (default 50k, max 100k).

    Returns a full response with Content-Length and Content-Disposition: attachment so browsers
    and fetch()+Blob flows can save the file. For fetch(), use response.blob() then an object URL
    with <a download> or FileSaver; navigate/window.open cannot send Authorization headers.
    """
    date_from_dt, date_to_dt = _parse_cdr_query_dates(date_from, date_to)
    if date_field not in ["created_at", "started_at", "ended_at"]:
        raise HTTPException(status_code=400, detail="date_field must be one of: 'created_at', 'started_at', 'ended_at'")

    filename = f"cdr_sessions_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADERS)
    async for doc in manager.iter_call_session_documents(
        search=search,
        caller_id=caller_id,
        callee_id=callee_id,
        status=status,
        kind=kind,
        date_from=date_from_dt,
        date_to=date_to_dt,
        date_field=date_field,
        organization_id=organization_id,
        max_rows=max_rows,
    ):
        writer.writerow(_cdr_doc_to_csv_row(doc))

    body = ("\ufeff" + buffer.getvalue()).encode("utf-8")

    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition_attachment(filename),
            "Content-Transfer-Encoding": "binary",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            # Let browser JS read filename/content metadata in cross-origin setups.
            "Access-Control-Expose-Headers": "Content-Disposition, Content-Length, Content-Type",
        },
    )


@router.get("/cdr/sessions")
@monitor(name="api.cdr.get_call_sessions")
async def get_call_sessions(
    search: str = Query(None, description="Search across room_name, call_id, participant names, emails, phone numbers (case-insensitive)"),
    caller_id: str = Query(None, description="Filter by exact caller_id match"),
    callee_id: str = Query(None, description="Filter by exact callee_id match"),
    kind: str = Query(None, description="Filter by call kind (e.g., 'webrtc_to_webrtc', 'webrtc_to_sip')"),
    status: str = Query(None, description="Filter by call status (e.g., 'active', 'ended')"),
    date_from: str = Query(None, description="Filter by start date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"),
    date_to: str = Query(None, description="Filter by end date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"),
    date_field: str = Query("created_at", description="Date field to filter by: 'created_at', 'started_at', or 'ended_at'"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(10, ge=1, le=100, description="Max records to return"),
    manager: WebRTCCallManager = Depends(get_call_manager),
    token: str = Depends(oauth2_scheme),
    organization_id: str = Depends(get_organization_id),
):
    """
    List call sessions (CDR) with enhanced search, filtering, date filtering, and pagination.
    Includes caller and callee user objects in each session.

    Parameters:
    - search: Optional text search across room_name, call_id, participant names, emails, phone numbers (case-insensitive regex)
    - caller_id: Optional exact match filter for caller_id
    - callee_id: Optional exact match filter for callee_id
    - kind: Optional filter by call kind (e.g., 'webrtc_to_webrtc', 'webrtc_to_sip')
    - status: Optional filter by call status (e.g., 'active', 'ended')
    - date_from: Optional start date filter (ISO format)
    - date_to: Optional end date filter (ISO format)
    - date_field: Date field to filter by ('created_at', 'started_at', or 'ended_at'), default: 'created_at'
    - skip: Number of records to skip (pagination)
    - limit: Maximum number of records to return
    """
    try:
        date_from_dt, date_to_dt = _parse_cdr_query_dates(date_from, date_to)

        if date_field not in ["created_at", "started_at", "ended_at"]:
            raise HTTPException(status_code=400, detail="date_field must be one of: 'created_at', 'started_at', 'ended_at'")

        sessions, total = await manager.list_call_session(
            search=search,
            caller_id=caller_id,
            callee_id=callee_id,
            status=status,
            kind=kind,
            date_from=date_from_dt,
            date_to=date_to_dt,
            date_field=date_field,
            skip=skip,
            limit=limit,
            organization_id=organization_id,
        )

        logger.info(f"get_call_sessions returned {len(sessions)} sessions, total={total}")

        def _participant_id(p) -> Optional[str]:
            if not p or not isinstance(p, dict):
                return None
            pid = p.get("id") or p.get("identity")
            if pid:
                return str(pid) if isinstance(pid, ObjectId) else pid
            return None

        user_ids = set()
        for session in sessions:
            for part in (session.get("caller_participant"), session.get("callee_participant")):
                pid = _participant_id(part)
                if pid:
                    user_ids.add(pid)

        users_dict = {}
        if user_ids:
            try:
                users_raw = await asyncio.to_thread(
                    user_service.get_users_by_ids, list(user_ids), organization_id
                )
                for user_id, user in users_raw.items():
                    if user:
                        user_record = {
                            "id": str(user.get("_id")),
                            "email": user.get("email"),
                            "phone_number": user.get("phone_number"),
                            "name": ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip() or "Unknown",
                            "role": user.get("role") or "customer"
                        }
                        users_dict[user_id] = user_record
            except (InvalidId, Exception) as e:
                logger.warning(f"Failed to batch fetch users: {e}")

        for session in sessions:
            if "_id" in session:
                session["id"] = str(session["_id"])
                del session["_id"]

            caller_participant = session.get("caller_participant")
            callee_participant = session.get("callee_participant")

            if isinstance(caller_participant, dict) and caller_participant.get("kind") == "SIP":
                session["callee"] = callee_participant
                session["caller"] = caller_participant
            else:
                if caller_participant:
                    session["caller"] = caller_participant
                else:
                    participants = session.get("participants") or []
                    caller_pid = _participant_id(caller_participant) or (participants[0] if participants else None)
                    if caller_pid:
                        session["caller"] = users_dict.get(caller_pid)
                if callee_participant:
                    session["callee"] = callee_participant
                else:
                    participants = session.get("participants") or []
                    callee_pid = _participant_id(callee_participant) or (participants[1] if len(participants) > 1 else None)
                    if callee_pid:
                        session["callee"] = users_dict.get(callee_pid)
            session.pop("caller_participant", None)
            session.pop("callee_participant", None)
            session.pop("caller_id", None)
            session.pop("callee_id", None)
            for key in ("recording_ended_at", "recording_file_name", "recording_s3_location", "recording_status", "egress_id"):
                session.pop(key, None)

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "sessions": sessions,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list call sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))
