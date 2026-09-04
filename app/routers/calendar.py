"""
calendar.py — Live ICS calendar subscription feeds for Tripdex.

Endpoints:
  GET /api/trips/{trip_id}/calendar.ics?token=<calendar_token>
      → per-trip feed (all segments in that trip)
  GET /api/calendar/{user_token}.ics
      → per-user feed (all segments across all trips)

Both are public URLs (no session cookie needed) — secured by a long random token
so calendar apps (Google, Apple, Outlook) can poll them without auth headers.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.models.models import Trip, Segment
from app.routers.deps import get_current_user
import secrets, os
from datetime import datetime, timezone

router = APIRouter(tags=["calendar"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://waypoint.emdm.ch")
PRODID = "-//Tripdex//Travel Itinerary//EN"


# ── ICS helpers ───────────────────────────────────────────────────────────────

def _ics_escape(s: str) -> str:
    """Escape ICS text field special characters."""
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_dt(dt_str: str, tz_str: str = None) -> str:
    """
    Format a datetime string for ICS.
    If timezone is given, emit TZID= form. Otherwise UTC Z form.
    """
    if not dt_str:
        return None
    try:
        dt_str = dt_str[:16].replace(" ", "T")  # normalise
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return None

    if tz_str and tz_str != "UTC":
        # TZID format — calendar apps handle DST
        return f"TZID={tz_str}:{dt.strftime('%Y%m%dT%H%M%S')}"
    else:
        return f"{dt.strftime('%Y%m%dT%H%M%S')}Z"


def _seg_summary(seg: Segment) -> str:
    """Human-readable one-line summary for the VEVENT SUMMARY field."""
    icons = {"flight": "✈", "train": "🚄", "hotel": "🏨", "car": "🚗",
             "taxi": "🚕", "activity": "🎟", "other": "📌"}
    icon = icons.get(seg.type, "📌")

    if seg.type == "hotel":
        carrier = seg.carrier or "Hotel"
        origin  = seg.origin or ""
        return f"{icon} {carrier}" + (f" — {origin}" if origin else "")

    orig = seg.origin or ""
    dest = seg.destination or ""
    route = f"{orig} → {dest}" if orig and dest else (orig or dest or seg.type.capitalize())
    carrier = seg.carrier or ""
    return f"{icon} {carrier} {route}".strip() if carrier else f"{icon} {route}"


def _seg_description(seg: Segment) -> str:
    """Multi-line VEVENT DESCRIPTION."""
    lines = []
    if seg.carrier:         lines.append(f"Carrier: {seg.carrier}")
    if seg.confirmation_ref:lines.append(f"Ref: {seg.confirmation_ref}")
    meta = seg.meta or {}
    if meta.get("seat"):    lines.append(f"Seat: {meta['seat']}")
    if meta.get("terminal_departure"): lines.append(f"Terminal: {meta['terminal_departure']}")
    if meta.get("gate"):    lines.append(f"Gate: {meta['gate']}")
    if meta.get("notes"):   lines.append(f"Notes: {meta['notes']}")
    if meta.get("address"): lines.append(f"Address: {meta['address']}")
    if meta.get("nights"):  lines.append(f"Nights: {meta['nights']}")
    return "\\n".join(_ics_escape(l) for l in lines)


def _seg_location(seg: Segment) -> str:
    meta = seg.meta or {}
    if meta.get("address"):
        return _ics_escape(meta["address"])
    parts = [p for p in [seg.origin, seg.destination] if p]
    return _ics_escape(", ".join(parts))


def _vevent(seg: Segment, trip_name: str) -> str:
    """Build a VEVENT block for one segment."""
    uid = f"{seg.id}@waypoint.emdm.ch"
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = _ics_escape(_seg_summary(seg))
    desc    = _seg_description(seg)
    loc     = _seg_location(seg)

    dtstart = _ics_dt(seg.departs_at, seg.departs_tz)
    dtend   = _ics_dt(seg.arrives_at, seg.arrives_tz) if seg.arrives_at else dtstart

    if not dtstart:
        return ""  # skip segments with no date

    # Format DTSTART / DTEND with optional TZID prefix
    def dt_prop(name, val):
        if val and val.startswith("TZID="):
            return f"{name};{val}"
        return f"{name}:{val}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc}",
        dt_prop("DTSTART", dtstart),
        dt_prop("DTEND", dtend),
        f"SUMMARY:{summary}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{desc}")
    if loc:
        lines.append(f"LOCATION:{loc}")
    lines.append(f"CATEGORIES:{seg.type.upper()}")
    if seg.confirmation_ref:
        lines.append(f"COMMENT:Ref: {_ics_escape(seg.confirmation_ref)}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _build_ics(trip_name: str, segments: list, feed_url: str) -> str:
    """Assemble a complete ICS calendar string."""
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    vevents = [_vevent(s, trip_name) for s in segments]
    vevents = [v for v in vevents if v]

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Tripdex — {_ics_escape(trip_name)}",
        f"X-WR-CALDESC:Live itinerary from Tripdex",
        f"REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        f"X-PUBLISHED-TTL:PT1H",
        f"SOURCE:{feed_url}",
        *vevents,
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


# ── Token management ──────────────────────────────────────────────────────────

def _ensure_trip_token(db: Session, trip_id: str) -> str:
    """Return existing calendar_token for a trip, or generate and save one."""
    row = db.execute(
        text("SELECT calendar_token FROM trips WHERE id=:id"), {"id": trip_id}
    ).mappings().fetchone()
    if row and row["calendar_token"]:
        return row["calendar_token"]
    token = secrets.token_urlsafe(32)
    db.execute(
        text("UPDATE trips SET calendar_token=:tok WHERE id=:id"),
        {"tok": token, "id": trip_id},
    )
    db.commit()
    return token


def _ensure_user_token(db: Session, user_id: str) -> str:
    """Return existing calendar_token for a user, or generate and save one."""
    row = db.execute(
        text("SELECT calendar_token FROM users WHERE id=:id"), {"id": user_id}
    ).mappings().fetchone()
    if row and row["calendar_token"]:
        return row["calendar_token"]
    token = secrets.token_urlsafe(32)
    db.execute(
        text("UPDATE users SET calendar_token=:tok WHERE id=:id"),
        {"tok": token, "id": user_id},
    )
    db.commit()
    return token


# ── API: get tokens (authenticated) ──────────────────────────────────────────

@router.get("/api/trips/{trip_id}/calendar-token")
def get_trip_calendar_token(
    trip_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return (and lazily create) the calendar subscription URLs for a trip."""
    trip = db.query(Trip).filter(Trip.id == trip_id, Trip.user_id == user["id"]).first()
    if not trip:
        raise HTTPException(404, "Trip not found")

    trip_token  = _ensure_trip_token(db, trip_id)
    user_token  = _ensure_user_token(db, user["id"])

    base = FRONTEND_URL.replace("https://", "").replace("http://", "")
    trip_url = f"https://{base}/api/trips/{trip_id}/calendar.ics?token={trip_token}"
    user_url = f"https://{base}/api/calendar/{user_token}.ics"

    return {
        "trip_ics_url":  trip_url,
        "trip_webcal":   trip_url.replace("https://", "webcal://"),
        "user_ics_url":  user_url,
        "user_webcal":   user_url.replace("https://", "webcal://"),
    }


# ── ICS feeds (public, token-secured) ────────────────────────────────────────

@router.get("/api/trips/{trip_id}/calendar.ics", response_class=PlainTextResponse)
def trip_calendar_feed(trip_id: str, token: str = None, db: Session = Depends(get_db)):
    """Per-trip live ICS feed. Token in query string."""
    if not token:
        raise HTTPException(401, "Missing token")

    row = db.execute(
        text("SELECT id, name, calendar_token FROM trips WHERE id=:id"),
        {"id": trip_id},
    ).mappings().fetchone()

    if not row or row["calendar_token"] != token:
        raise HTTPException(403, "Invalid token")

    segments = (
        db.query(Segment)
        .filter(Segment.trip_id == trip_id)
        .order_by(Segment.departs_at)
        .all()
    )

    base = FRONTEND_URL.replace("https://", "").replace("http://", "")
    feed_url = f"https://{base}/api/trips/{trip_id}/calendar.ics?token={token}"
    ics = _build_ics(row["name"], segments, feed_url)

    return PlainTextResponse(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{row["name"]}.ics"'},
    )


@router.get("/api/calendar/{user_token}.ics", response_class=PlainTextResponse)
def user_calendar_feed(user_token: str, db: Session = Depends(get_db)):
    """Per-user all-trips ICS feed."""
    row = db.execute(
        text("SELECT id, username FROM users WHERE calendar_token=:tok"),
        {"tok": user_token},
    ).mappings().fetchone()

    if not row:
        raise HTTPException(403, "Invalid token")

    user_id = row["id"]
    trips   = db.query(Trip).filter(Trip.user_id == user_id).order_by(Trip.start_date).all()

    all_segments = []
    for trip in trips:
        segs = (
            db.query(Segment)
            .filter(Segment.trip_id == trip.id)
            .order_by(Segment.departs_at)
            .all()
        )
        # Tag each segment with its trip name for the summary
        for s in segs:
            s._trip_name = trip.name  # transient attr
        all_segments.extend(segs)

    base = FRONTEND_URL.replace("https://", "").replace("http://", "")
    feed_url = f"https://{base}/api/calendar/{user_token}.ics"
    ics = _build_ics(f"All trips — {row['username']}", all_segments, feed_url)

    return PlainTextResponse(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tripdex-all-trips.ics"'},
    )
