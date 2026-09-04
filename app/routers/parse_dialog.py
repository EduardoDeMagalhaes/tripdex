"""
parse_dialog.py — /api/parse/text, /dialog, /dialog/confirm
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any
from app.database import get_db
from app.models.models import Segment, Trip
from app.routers.deps import get_current_user
from app.routers.segments import schedule_enrich
from app.routers.guardrails import run_guardrails
from app.schemas.schemas import SegmentOut
from app.routers.parse_core import (
    SYSTEM_DIALOG, SYSTEM_ONESHOT, enrich_with_aviationstack,
    _verify_trip_ownership, client
)
from sqlalchemy import text
import os, json, re, asyncio

router = APIRouter(prefix="/api/parse", tags=["parse"])


# ── Pydantic models ──────────────────────────────────────────────────────────

class ParseRequest(BaseModel):
    text: str
    trip_id: str

class DialogMessage(BaseModel):
    role: str        # "user" or "assistant"
    content: str

class DialogRequest(BaseModel):
    trip_id: str
    message: str
    history: list[DialogMessage] = []
    draft: Optional[dict[str, Any]] = None
    all_trips: list[dict] = []
    bypass_guardrails: bool = False
    bypass_verification: bool = False

class DialogResponse(BaseModel):
    status: str
    question: Optional[str] = None
    draft: Optional[dict[str, Any]] = None
    return_draft: Optional[dict[str, Any]] = None
    missing: list[str] = []
    aviationstack_note: Optional[str] = None
    return_aviationstack_note: Optional[str] = None
    timetable_note: Optional[str] = None
    guardrail_hit: Optional[dict] = None
    train_options: Optional[list] = None   # structured connections for UI picker
    more_segments: list[str] = []           # other distinct segments detected in the same message, not yet logged
    history: list[DialogMessage] = []

class DialogConfirmRequest(BaseModel):
    trip_id: str
    draft: dict[str, Any]


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/text", response_model=SegmentOut, status_code=201)
async def parse_text(body: ParseRequest, bg: BackgroundTasks, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """One-shot NL parse — kept for backward compatibility."""
    _verify_trip_ownership(body.trip_id, user, db)

    r = client.chat.completions.create(
        model="gpt-4o", temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_ONESHOT},
            {"role": "user", "content": body.text},
        ],
    )
    data = json.loads(r.choices[0].message.content)
    parse_status, av_note = "ok", None
    if data.get("type") == "flight":
        data, parse_status, av_note = asyncio.run(enrich_with_aviationstack(data))

    cols = Segment.__table__.columns.keys()
    seg = Segment(trip_id=body.trip_id, parse_status=parse_status,
                  **{k: v for k, v in data.items() if k in cols})
    seg.meta = data.get("meta", {})
    if av_note:
        seg.meta["aviationstack_note"] = av_note
    db.add(seg); db.commit(); db.refresh(seg)
    # Sync trip dates to cover new segment
    from app.routers.segments import _sync_trip_dates
    trip_obj = db.query(Trip).filter(Trip.id == body.trip_id).first()
    if trip_obj: _sync_trip_dates(trip_obj, db)
    schedule_enrich(bg, seg.id)
    return seg


@router.post("/dialog", response_model=DialogResponse)
async def parse_dialog(body: DialogRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Multi-turn dialog. Frontend sends full history + draft each turn.
    GPT asks one question at a time until it has enough, then sets status=ready.
    No DB write happens here — that's done by /dialog/confirm.
    """
    _verify_trip_ownership(body.trip_id, user, db)

    trip = db.query(Trip).filter(Trip.id == body.trip_id).first()

    # ── Guardrails (pre-GPT) ─────────────────────────────────────────────────
    hit = None if body.bypass_guardrails else run_guardrails(body.message, body.draft, trip, body.all_trips)
    if hit:
        return DialogResponse(
            status="guardrail",
            guardrail_hit={"code": hit.code, "message": hit.message, "options": hit.options, "meta": hit.meta},
            history=list(body.history),
        )

    # Build existing segments summary
    from app.models.models import Segment as SegModel
    existing = db.query(SegModel).filter(SegModel.trip_id == trip.id).order_by(SegModel.departs_at).all()
    seg_lines = []
    for s in existing:
        line = f"  - {s.type}: {s.origin or ''} -> {s.destination or ''} | {s.carrier or ''} | {(s.departs_at or '')[:16]} | ref:{s.confirmation_ref or 'none'}"
        seg_lines.append(line)
    segs_ctx = "\n".join(seg_lines) if seg_lines else "  (none yet)"
    trip_ctx = f"[Trip: {trip.name}, start: {trip.start_date}, end: {trip.end_date}\nAlready logged segments:\n{segs_ctx}]"
    trip_context = ""
    if body.all_trips:
        lines = []
        for t in body.all_trips:
            marker = " ← CURRENT" if t.get("isCurrent") else ""
            lines.append(f"  - {t['name']} ({t.get('start_date','?')} to {t.get('end_date','?')}){marker}")
        trip_context = "\n\nAVAILABLE TRIPS (user is currently viewing the CURRENT one):\n" + "\n".join(lines)

    from app.models.models import CustomSegmentType
    custom_types = db.query(CustomSegmentType).filter(CustomSegmentType.user_id == user["id"]).all()
    custom_types_context = ""
    if custom_types:
        ct_lines = [f"  - key=\"{ct.key}\" label=\"{ct.label}\"" for ct in custom_types]
        custom_types_context = "\n\nCUSTOM SEGMENT TYPES (this user's own saved types — use the key if one matches):\n" + "\n".join(ct_lines)

    messages = [{"role": "system", "content": SYSTEM_DIALOG + trip_context + custom_types_context}]
    for m in body.history:
        messages.append({"role": m.role, "content": m.content})

    user_content = body.message
    if body.draft:
        user_content += f"\n\n[Draft so far: {json.dumps(body.draft)}]"
    user_content += f"\n\n{trip_ctx}"
    messages.append({"role": "user", "content": user_content})

    r = client.chat.completions.create(
        model="gpt-4o", temperature=0,
        response_format={"type": "json_object"},
        messages=messages,
    )
    gpt = json.loads(r.choices[0].message.content)

    new_history = list(body.history) + [
        DialogMessage(role="user", content=body.message),
        DialogMessage(role="assistant", content=r.choices[0].message.content),
    ]

    status = gpt.get("status", "question")
    draft = gpt.get("draft", body.draft or {})
    av_note=None
    return_draft=gpt.get('return_draft')
    return_av_note=None

    # ── Stuck detection: ≥5 question turns → nudge user to report ───────────
    question_turns = sum(1 for m in new_history if m.role == "assistant" and
                         '"status": "question"' in m.content)
    if question_turns >= 5 and status == "question":
        status = "stuck"
        gpt["question"] = (
            "I\'ve asked a few times and I\'m still not sure I understand what you need. "
            "This might be something I can\'t handle yet — or a bug. "
            "Would you like to report it so we can improve Tripdex?"
        )

    if status=='ready' and draft.get('type')=='flight' and draft.get('flight_iata'):
        draft,_,av_note=await enrich_with_aviationstack(draft)
    if status=='ready' and return_draft and return_draft.get('flight_iata'):
        return_draft,_,return_av_note=await enrich_with_aviationstack(return_draft)
    timetable_note=None
    if status=='ready' and draft.get('type')=='train' and not body.bypass_verification:
        draft, timetable_note, tt_connections = await verify_train_time(draft)
        if tt_connections:
            # Time didn't match — return structured options for the UI picker
            origin = draft.get("origin","")
            destination = draft.get("destination","")
            requested_time = (draft.get("departs_at","") or "")[:16][-5:]
            return DialogResponse(
                status="train_options",
                question=f"No train at {requested_time} from {origin} to {destination}. Pick a service:",
                draft=draft,
                return_draft=return_draft,
                train_options=tt_connections,
                missing=[],
                history=new_history,
            )
    return DialogResponse(status=status,question=gpt.get('question'),draft=draft,return_draft=return_draft,missing=gpt.get('missing',[]),aviationstack_note=av_note,return_aviationstack_note=return_av_note,timetable_note=timetable_note,more_segments=gpt.get('more_segments',[]),history=new_history)


@router.post("/dialog/confirm", response_model=SegmentOut, status_code=201)
def dialog_confirm(body: DialogConfirmRequest, bg: BackgroundTasks, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    User approved the summary card — write the segment to DB.
    """
    _verify_trip_ownership(body.trip_id, user, db)

    data = dict(body.draft)
    parse_status = data.pop("parse_status", "ok")
    av_note        = data.pop("aviationstack_note", None)
    timetable_note = data.pop("timetable_note", None)

    # Merge flight_iata into carrier if carrier doesn't already contain the flight number
    flight_iata = data.get("flight_iata") or (data.get("meta") or {}).get("flight_iata")
    if flight_iata:
        carrier = data.get("carrier") or ""
        import re as _re
        if not _re.search(r'[A-Z]{2}\d{2,4}', carrier):
            # carrier has no flight number yet — append it
            airline = carrier.strip() or ""
            data["carrier"] = f"{airline} {flight_iata}".strip() if airline else flight_iata

    cols = Segment.__table__.columns.keys()
    seg = Segment(trip_id=body.trip_id, parse_status=parse_status,
                  **{k: v for k, v in data.items() if k in cols})
    seg.meta = data.get("meta", {})
    if flight_iata:
        seg.meta["flight_iata"] = flight_iata  # keep for enrich
    if av_note:
        seg.meta["aviationstack_note"] = av_note
    _log_dialog(body.trip_id, getattr(body, 'history', []), data)
    db.add(seg); db.commit(); db.refresh(seg)
    schedule_enrich(bg, seg.id)
    return seg


# ── Dialog quality log ────────────────────────────────────────────────────────
import logging
import logging.handlers as _lh
import os as _os

_dialog_log_path = _os.path.join(_os.path.dirname(__file__), "../../logs/dialog.log")
_os.makedirs(_os.path.dirname(_dialog_log_path), exist_ok=True)
_dlog = logging.getLogger("tripdex.dialog")
if not _dlog.handlers:
    _h = _lh.RotatingFileHandler(
        _dialog_log_path, maxBytes=10*1024*1024, backupCount=2, encoding="utf-8"
    )
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _dlog.addHandler(_h)
    _dlog.setLevel(logging.INFO)


async def verify_train_time(draft: dict) -> tuple[dict, str | None, list | None]:
    """
    Cross-check a train draft against the real timetable.
    - If time matches within 3 min: silently correct + return confirmation note.
    - If time is wrong: return (original_draft, None, connections_list) so the
      caller can ask the user to pick instead of auto-correcting silently.
    Returns (draft, note_or_None, connections_for_question_or_None).
    """
    from app.routers.enrich import search_connections

    origin      = draft.get("origin", "")
    destination = draft.get("destination", "")
    departs_at  = draft.get("departs_at", "")

    if not origin or not destination or not departs_at:
        return draft, None, None

    datetime_str = departs_at[:16].replace("T", " ")

    try:
        result = await search_connections(
            from_station=origin,
            to_station=destination,
            datetime_str=datetime_str,
            limit=4,
        )
    except Exception:
        return draft, None, None

    connections = result.get("connections", [])
    if not connections:
        return draft, None, None

    source = result.get("source", "timetable")

    try:
        from datetime import datetime as _dt
        user_dep = _dt.strptime(datetime_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return draft, None, None

    best = None
    best_diff = None
    for conn in connections:
        try:
            conn_dep = _dt.strptime(conn["departs"][:16], "%Y-%m-%d %H:%M")
            diff = abs((conn_dep - user_dep).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = conn
        except Exception:
            continue

    if not best:
        return draft, None, None

    TOLERANCE_SECONDS = 3 * 60

    if best_diff <= TOLERANCE_SECONDS:
        # Match — silently correct and confirm
        corrected = dict(draft)
        corrected["departs_at"] = best["departs"].replace(" ", "T") + ":00"
        if best.get("arrives"):
            corrected["arrives_at"] = best["arrives"].replace(" ", "T") + ":00"
        if not corrected.get("carrier") and best.get("carrier"):
            corrected["carrier"] = best["carrier"]
        xfers = best.get("transfers", 0)
        xfer_str = f", {xfers} change{'s' if xfers != 1 else ''}" if xfers else ", direct"
        note = (
            f"✓ Verified against {source}: {best.get('carrier') or ''} "
            f"departs {best['departs'][11:16]}, arrives {best['arrives'][11:16]}"
            + (f", platform {best['platform_dep']}" if best.get("platform_dep") else "")
            + xfer_str + "."
        )
        return corrected, note, None

    # No match — return connections so caller can ask user to pick
    return draft, None, connections[:4]


def _log_dialog(trip_id: str, history: list, draft: dict):
    try:
        _dlog.info(json.dumps({
            "trip_id": trip_id,
            "turns":   len(history),
            "draft":   draft,
            "history": history,
        }, ensure_ascii=False))
    except Exception:
        pass


