"""
emails.py — Email ingest pipeline: raw email → GPT parsing → segments.
Transactional email functions live in email_templates.py.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.database import get_db
from app.models.models import RawEmail, Segment, Trip
from app.schemas.schemas import SegmentOut
from app.routers.deps import get_current_user
from app.routers.segments import schedule_enrich
from app.routers.email_templates import send_unregistered_reply, send_ingest_reply, FROM_EMAIL, FRONTEND_URL
from sqlalchemy import text
from openai import OpenAI
import os, json, re as _re
import urllib.parse, urllib.request
import io as _io, uuid as _uuid
import math as _math
import uuid as _uuid
import json as _json
from collections import Counter as _Counter
from datetime import datetime as _dt, date as _date, timezone as _tz, timedelta as _td
from sqlalchemy import text as _text
from app.models.models import Trip as TripModel
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/api/emails", tags=["emails"])
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


import re as _re
def _extract_email(addr: str) -> str:
    """Extract plain email from 'Name <email>' format."""
    m = _re.search(r'<([^>]+)>', addr)
    return (m.group(1) if m else addr).strip().lower()

def _should_send_bounce(db, sender_email: str) -> bool:
    """Return True if we should still send an unregistered bounce to this sender.
    Suppresses after 3 bounces in 24h to prevent reply amplification."""
    clean = _extract_email(sender_email)
    cutoff = (_dt.now(_tz.utc) - _td(hours=24)).isoformat()
    count = db.execute(
        text("SELECT COUNT(*) FROM raw_emails WHERE LOWER(from_address) LIKE :pat AND parse_status='unregistered' AND received_at > :cutoff"),
        {"pat": f"%{clean}%", "cutoff": cutoff}
    ).scalar() or 0
    return count < 3


def lookup_user_by_email(db, sender_email: str):
    """Return user row for a verified sender (primary or source email), or None."""
    clean = _extract_email(sender_email)
    # Check primary account email
    row = db.execute(
        text("SELECT id, username, email, home_city, home_airports FROM users WHERE LOWER(email)=:email AND is_verified=1"),
        {"email": clean}
    ).mappings().fetchone()
    if row:
        return dict(row)
    # Check confirmed source emails
    src = db.execute(
        text("""SELECT u.id, u.username, u.email, u.home_city, u.home_airports
                FROM user_source_emails se
                JOIN users u ON se.user_id = u.id
                WHERE LOWER(se.email)=:email AND se.status='confirmed' AND u.is_verified=1"""),
        {"email": clean}
    ).mappings().fetchone()
    return dict(src) if src else None


SYSTEM = """Extract ALL travel segments from this booking confirmation email.
You MUST return a JSON object with a single key "segments" whose value is an array.
Each element represents one travel segment with this structure:
{
  "type": "flight|hotel|train|car|activity|other",
  "origin": "city or IATA code",
  "destination": "city or IATA code or null",
  "carrier": "airline/hotel/operator name and number",
  "flight_iata": "IATA flight code only e.g. BA0579 or null",
  "departs_at": "YYYY-MM-DDTHH:MM:00",
  "departs_tz": "IANA timezone",
  "arrives_at": "YYYY-MM-DDTHH:MM:00 or null",
  "arrives_tz": "IANA timezone or null",
  "confirmation_ref": "booking ref or null",
  "confirmed": true,
  "meta": {
    "notes": "any extra info not captured elsewhere",
    "cabin_class": "Economy/Premium Economy/Business/First or null",
    "seat": "seat number e.g. 14A or null",
    "boarding_time": "HH:MM or null",
    "terminal_departure": "terminal at departure airport or null",
    "terminal_arrival": "terminal at arrival airport or null",
    "gate": "gate number or null",
    "baggage_allowance": "e.g. 1 bag 23kg or null",
    "fare_type": "e.g. World Traveller Plus or null",
    "ticket_number": "e-ticket number or null",
    "platform_departure": "platform number at departure station or null",
    "platform_arrival": "platform number at arrival station or null",
    "train_number": "e.g. EC 37 or null",
    "coach": "coach/carriage number or null",
    "class": "1st or 2nd or null",
    "address": "full street address for hotels or null",
    "phone": "hotel phone or null",
    "checkin_time": "HH:MM local e.g. 15:00 or null",
    "checkout_time": "HH:MM local e.g. 12:00 or null",
    "room_type": "e.g. 1 Queen Studio Suite or null",
    "nights": "number of nights or null",
    "rate_plan": "e.g. Reward Nights or null",
    "loyalty_points": "e.g. 50000 Points or null",
    "cancellation_policy": "brief summary or null",
    "price": "total price with currency e.g. CHF 2306.00 or null",
    "payment_card": "last 4 digits only e.g. **** 3679 or null"
  }
}
Rules:
- Extract EVERY segment — all flights each leg separately, each train leg separately, hotel check-in, transfers
- A 4-flight itinerary MUST produce 4 segment objects. A 2-leg train MUST produce 2 segment objects.
- For hotels: departs_at = check-in datetime, arrives_at = check-out datetime
- Set confirmed=true for all
- Infer IANA timezone from city/airport
- Use the year from the email; if missing use 2026
- Only set meta fields where data is present in the email — use null otherwise
- Return {"segments": []} if no travel data found
"""

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = _math.radians(lat2 - lat1)
    dlon = _math.radians(lon2 - lon1)
    a = _math.sin(dlat/2)**2 + _math.cos(_math.radians(lat1))*_math.cos(_math.radians(lat2))*_math.sin(dlon/2)**2
    return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1-a))

def _geocode_iata_or_city(name: str) -> tuple[float,float] | None:
    """Best-effort lat/lon for an IATA code or city name via Nominatim."""
    if not name:
        return None
    q = name.strip()
    url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(q)}&format=json&limit=1&accept-language=en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Waypoint/1.0 trip.helper@emdm.ch"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None

def _trip_geo_points(trip) -> list[tuple[float,float]]:
    """Return list of (lat,lon) for a trip based on its location field."""
    coords = []
    if trip.location:
        c = _geocode_iata_or_city(trip.location)
        if c:
            coords.append(c)
    return coords

def should_ask_user(db, segments: list, trips: list, user: dict = None) -> bool:
    """
    Return True if the user should be asked where to assign these segments
    instead of auto-creating a new trip.

    Fires when ANY existing trip is within 3 days AND within 30km of a
    segment endpoint — excluding the user's home-base airports/city.
    """
    if not trips:
        return False

    # Build home-base exclusion set
    home_airports = set()
    home_city_coords = None
    if user:
        raw_airports = (user.get("home_airports") or "")
        home_airports = {c.strip().upper() for c in raw_airports.split(",") if c.strip()}
        home_city = (user.get("home_city") or "").strip()
        if home_city:
            home_city_coords = _geocode_iata_or_city(home_city)

    def _is_home_base(name: str) -> bool:
        if not name:
            return False
        if name.upper() in home_airports:
            return True
        if home_city_coords:
            c = _geocode_iata_or_city(name)
            if c and _haversine_km(c[0], c[1], home_city_coords[0], home_city_coords[1]) <= 30:
                return True
        return False

    # Collect all segment dates
    seg_dates = sorted([
        s["departs_at"][:10] for s in segments if (s.get("departs_at") or "")[:10]
    ])
    if not seg_dates:
        return False
    first_date = seg_dates[0]
    last_date  = seg_dates[-1]

    # Collect non-home-base segment endpoints
    seg_points = set()
    for seg in segments:
        t = seg.get("type", "")
        if t in ("flight", "train"):
            if seg.get("origin") and not _is_home_base(seg["origin"]):
                seg_points.add(seg["origin"])
            if seg.get("destination") and not _is_home_base(seg["destination"]):
                seg_points.add(seg["destination"])
        else:
            if seg.get("destination") and not _is_home_base(seg["destination"]):
                seg_points.add(seg["destination"])
            if seg.get("origin") and not _is_home_base(seg["origin"]):
                seg_points.add(seg["origin"])

    # If all endpoints are home-base, nothing is ambiguous
    if not seg_points:
        return False
    try:
        first_dt = _date.fromisoformat(first_date)
        last_dt  = _date.fromisoformat(last_date)
    except Exception:
        return False

    for trip in trips:
        if not trip.start_date:
            continue
        try:
            trip_start = _date.fromisoformat(trip.start_date)
            trip_end   = _date.fromisoformat(trip.end_date) if trip.end_date else trip_start
        except Exception:
            continue

        # Date proximity: within 3 days of trip start or end
        date_close = (
            abs((first_dt - trip_start).days) <= 3 or
            abs((first_dt - trip_end).days)   <= 3 or
            abs((last_dt  - trip_start).days) <= 3 or
            abs((last_dt  - trip_end).days)   <= 3
        )
        if not date_close:
            continue

        # Geo proximity: any segment endpoint within 30km of trip location
        trip_coords = _trip_geo_points(trip)
        if not trip_coords:
            # No trip coords — date proximity alone is enough to ask
            return True

        for pt_name in seg_points:
            pt_coords = _geocode_iata_or_city(pt_name)
            if not pt_coords:
                continue
            for (tlat, tlon) in trip_coords:
                dist = _haversine_km(pt_coords[0], pt_coords[1], tlat, tlon)
                if dist <= 30:
                    return True

    return False


def save_orphan_segments(db, raw_email, segments_data: list, user_id: str, trips_for_email: list) -> list:
    """
    Persist segments as orphans (trip_id=NULL, parse_status='pending_assignment').
    Create signed resolve tokens for each candidate trip + one for 'new'.
    Returns list of (token_str, trip_or_None) for email assembly.
    """

    cols = Segment.__table__.columns.keys()
    seg_ids = []
    for seg_data in segments_data:
        seg = Segment(
            trip_id      = None,
            raw_email_id = raw_email.id,
            parse_status = "pending_assignment",
            **{k: v for k, v in seg_data.items() if k in cols}
        )
        seg.meta = seg_data.get("meta", {})
        seg.meta["source"] = "email"
        db.add(seg)
        db.flush()
        seg_ids.append(seg.id)

    # One token per candidate trip + one for "create new"
    expires = (_dt.now(_tz.utc) + _td(days=9999)).isoformat()  # never expires
    tokens = []
    candidates = list(trips_for_email) + [None]  # None = create new trip
    for trip in candidates:
        tok = _uuid.uuid4().hex + _uuid.uuid4().hex  # 64-char token
        meta = _json.dumps({"segment_ids": seg_ids, "trip_id": trip.id if trip else "new"})
        db.execute(_text(
            "INSERT INTO email_tokens (id, user_id, token, type, expires_at, meta) "
            "VALUES (:id, :uid, :tok, 'assign', :exp, :meta)"
        ), {"id": str(_uuid.uuid4()), "uid": user_id, "tok": tok, "exp": expires, "meta": meta})
        tokens.append((tok, trip))

    db.commit()
    return tokens

def create_trip_from_segments(db, segments: list, user_id: str):
    """
    Auto-create a trip from parsed segments when none exists.
    Names the trip after the primary destination + month.
    Returns the new Trip object.
    """

    # Collect all departure dates to set the date range
    dates = sorted([
        s["departs_at"][:10]
        for s in segments
        if (s.get("departs_at") or "")[:10]
    ])
    start_date = dates[0]  if dates else None
    end_date   = dates[-1] if dates else None

    # Also consider arrives_at for end_date (e.g. hotel checkout)
    arrive_dates = sorted([
        s["arrives_at"][:10]
        for s in segments
        if (s.get("arrives_at") or "")[:10]
    ])
    if arrive_dates:
        end_date = max(end_date or "", arrive_dates[-1]) or end_date

    # Primary destination: first non-home destination across all segment endpoints.
    # For return trips (ZRH→PMO, PMO→ZRH) this picks PMO, not ZRH.
    def _is_home(name):
        if not name:
            return True
        n = name.strip().upper()
        # Check against home_airports if available (passed via user context at module level)
        # We use the module-level _is_home_base helper if user context is available,
        # but here we fall back to a simpler check: any IATA-looking token in the name
        # that appears in both origin and destination of different segments = likely home.
        return False  # resolved below

    # Collect all endpoints (origin + destination) across segments
    all_endpoints = []
    for seg in segments:
        for field in ("origin", "destination"):
            v = seg.get(field)
            if v:
                all_endpoints.append(v.strip().upper())

    # Count frequency — home base appears most often (start + end of trip)
    freq = _Counter(all_endpoints)
    # Most frequent endpoint is likely home — exclude it and pick first remaining destination
    most_common_count = freq.most_common(1)[0][1] if freq else 0
    home_candidates = {k for k, c in freq.items() if c == most_common_count} if most_common_count > 1 else set()

    # First destination not in home_candidates
    destinations = [
        s.get("destination", "").strip().upper()
        for s in segments
        if s.get("destination") and s["destination"].strip().upper() not in home_candidates
    ]
    primary_dest = destinations[0] if destinations else (
        # fallback: just take the most-mentioned non-origin destination
        freq.most_common()[-1][0] if freq else None
    )

    # Trip name: "Palermo · Jul 2026" or just "Trip · Jul 2026"
    if start_date:
        try:
            month_str = _dt.fromisoformat(start_date).strftime("%b %Y")
        except Exception:
            month_str = start_date[:7]
    else:
        month_str = _dt.now(_tz.utc).strftime("%b %Y")

    trip_name = f"{primary_dest} · {month_str}" if primary_dest else f"Trip · {month_str}"
    trip_id = str(_uuid_mod.uuid4())
    now = _dt.now(_tz.utc).isoformat()
    db.execute(_text("""
        INSERT INTO trips (id, name, start_date, end_date, location, user_id, home_currency, created_at)
        VALUES (:id, :name, :start_date, :end_date, :location, :user_id, 'CHF', :created_at)
    """), dict(id=trip_id, name=trip_name, start_date=start_date, end_date=end_date,
               location=primary_dest, user_id=user_id, created_at=now))
    db.flush()
    # Return as ORM object so the rest of the ingest flow can use trip.id normally
    return db.query(TripModel).filter(TripModel.id == trip_id).one()

def find_best_trip(db, segments, user_id: str = None):
    """
    Find the best trip for a set of segments based on date overlap.
    Scoped to user_id if provided.
    Priority: exact date range match > same month > closest upcoming > most recent.
    """
    q = db.query(Trip)
    if user_id:
        q = q.filter(Trip.user_id == user_id)
    trips = q.order_by(Trip.start_date).all()
    if not trips:
        return None

    seg_dates = sorted([(s.get("departs_at") or "")[:10]
                        for s in segments if (s.get("departs_at") or "")[:10]])
    if not seg_dates:
        from datetime import date
        today = str(date.today())
        upcoming = [t for t in trips if (t.start_date or "") >= today]
        return min(upcoming, key=lambda t: t.start_date) if upcoming else trips[-1]

    first_date = seg_dates[0]

    # 1. Exact overlap: first segment date falls within trip range
    for trip in trips:
        if trip.start_date and trip.end_date:
            if trip.start_date <= first_date <= trip.end_date:
                return trip

    # 2. Partial overlap: any segment date within any trip
    all_dates = set(seg_dates)
    for trip in trips:
        if trip.start_date and trip.end_date:
            if any(trip.start_date <= d <= trip.end_date for d in all_dates):
                return trip

    # 3. No reliable match — let caller auto-create a new trip
    return None


def call_gpt(subject, body_text):
    r = client.chat.completions.create(
        model="gpt-4o", temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Subject: {subject}\n\n{body_text}"},
        ],
    )
    parsed = json.loads(r.choices[0].message.content)
    if isinstance(parsed, list):
        segments_data = parsed
    elif "segments" in parsed:
        segments_data = parsed["segments"]
    elif "type" in parsed:
        segments_data = [parsed]
    else:
        segments_data = next((v for v in parsed.values() if isinstance(v, list)), [])
    return normalise_segments(segments_data)


class IngestRequest(BaseModel):
    message_id: str
    from_address: str
    subject: str
    body_text: str
    trip_id: Optional[str] = None

class IngestResponse(BaseModel):
    ok: bool
    message_id: str
    trip_id: Optional[str] = None
    segments_created: int = 0
    parse_status: str = "ok"
    error: Optional[str] = None

class ReparseRequest(BaseModel):
    raw_email_id: str
    trip_id: Optional[str] = None



# ── Ingest confirmation reply ─────────────────────────────────────────────────





@router.get("/orphans")
def get_orphans(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Return all pending_assignment segments for the current user."""
    rows = db.execute(_text("""
        SELECT s.id, s.type, s.origin, s.destination, s.departs_at, s.arrives_at,
               s.carrier, s.confirmation_ref, s.meta, s.raw_email_id,
               r.subject as email_subject, r.received_at as email_received
        FROM segments s
        LEFT JOIN raw_emails r ON s.raw_email_id = r.id
        LEFT JOIN raw_emails r2 ON r2.id = s.raw_email_id
        WHERE s.trip_id IS NULL
          AND s.parse_status = 'pending_assignment'
          AND r.from_address LIKE :email_pat
        ORDER BY s.departs_at ASC
    """), {"email_pat": f"%{user['email']}%"}).mappings().fetchall()
    return [dict(r) for r in rows]


@router.post("/resolve/{token}")
def resolve_assignment(token: str, db: Session = Depends(get_db)):
    """
    One-click resolve: assign orphan segments to a trip (or create one).
    No auth required — the token IS the auth.
    """

    row = db.execute(_text(
        "SELECT * FROM email_tokens WHERE token=:tok AND type='assign' AND used_at IS NULL"
    ), {"tok": token}).mappings().fetchone()
    if not row:
        return {"ok": False, "error": "Invalid or already used link"}

    meta = _json.loads(row["meta"] or "{}")
    seg_ids  = meta.get("segment_ids", [])
    trip_id  = meta.get("trip_id")

    if trip_id == "new":
        # Collect segment data to build a trip name
        segs = db.execute(_text(
            f"SELECT * FROM segments WHERE id IN ({','.join([repr(i) for i in seg_ids])})"
        )).mappings().fetchall()
        seg_list = [dict(s) for s in segs]
        trip_obj = create_trip_from_segments(db, seg_list, user_id=row["user_id"])
        trip_id  = trip_obj.id
    else:
        trip_obj = db.query(Trip).filter(Trip.id == trip_id).first()
        if not trip_obj:
            return {"ok": False, "error": "Trip not found"}

    # Assign segments
    now = _dt.now(_tz.utc).isoformat()
    for sid in seg_ids:
        db.execute(_text(
            "UPDATE segments SET trip_id=:tid, parse_status='ok', updated_at=:now WHERE id=:sid AND trip_id IS NULL"
        ), {"tid": trip_id, "now": now, "sid": sid})

    db.execute(_text("UPDATE email_tokens SET used_at=:now WHERE token=:tok"),
               {"now": now, "tok": token})
    db.commit()
    html = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:Georgia,serif;background:#f5f0e8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#fff;border-radius:12px;padding:40px 32px;max-width:380px;text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.08)}}
h2{{margin:0 0 12px;font-size:22px;color:#2c2825}} p{{color:#6b635a;line-height:1.6;margin:0 0 24px}}
a{{display:inline-block;background:#b5651d;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-size:15px}}</style>
</head><body><div class="card">
<div style="font-size:32px;margin-bottom:16px">✦</div>
<h2>Added to {trip_obj.name}</h2>
<p>Your segments have been assigned. Open Waypoint to see your itinerary.</p>
<a href="{FRONTEND_URL}">Open Waypoint</a>
</div></body></html>"""
    return HTMLResponse(html)


@router.post("/ingest", response_model=IngestResponse)
def ingest_email(body: IngestRequest, bg: BackgroundTasks, db: Session = Depends(get_db)):
    existing = db.query(RawEmail).filter(RawEmail.message_id == body.message_id).first()
    if existing:
        return IngestResponse(ok=True, message_id=body.message_id, parse_status="duplicate", error="Already processed")

    # ── Resolve sender to a user ───────────────────────────────────────────────
    sender_clean = _extract_email(body.from_address)
    user = lookup_user_by_email(db, sender_clean)
    if not user:
        # Store raw record so rate-limit counter works
        raw_unregd = RawEmail(
            message_id=body.message_id, from_address=body.from_address,
            subject=body.subject, body_text=body.body_text,
            parse_status="unregistered"
        )
        db.add(raw_unregd); db.commit()
        if _should_send_bounce(db, sender_clean):
            send_unregistered_reply(sender_clean)
        return IngestResponse(ok=False, message_id=body.message_id,
                              parse_status="unregistered",
                              error=f"Sender {sender_clean} not registered")

    raw = RawEmail(message_id=body.message_id, from_address=body.from_address,
                   subject=body.subject, body_text=body.body_text, parse_status="processing")
    db.add(raw); db.flush()
    try:
        segments_data = call_gpt(body.subject, body.body_text)
    except Exception as e:
        raw.parse_status = "failed"; db.commit()
        send_ingest_reply(sender_clean, "failed", body.subject)
        return IngestResponse(ok=False, message_id=body.message_id, parse_status="failed", error=str(e))
    if not segments_data:
        raw.parse_status = "no_segments"; db.commit()
        send_ingest_reply(sender_clean, "no_segments", body.subject)
        return IngestResponse(ok=True, message_id=body.message_id, parse_status="no_segments", segments_created=0)
    trip_id = body.trip_id
    if not trip_id:
        trip = find_best_trip(db, segments_data, user_id=user["id"])
        trip_id = trip.id if trip else None
    trip_created = False
    if not trip_id:
        # Check if any existing trip is close in date + location → ask user instead
        all_user_trips = db.query(Trip).filter(Trip.user_id == user["id"]).all()
        if all_user_trips and should_ask_user(db, segments_data, all_user_trips, user=user):
            # Save as orphans, send confirmation email
            tokens = save_orphan_segments(db, raw, segments_data, user["id"], all_user_trips)
            raw.parse_status = "pending_assignment"
            db.commit()
            from app.routers.email_templates import send_assignment_email
            send_assignment_email(
                to=sender_clean,
                subject=body.subject,
                segments_data=segments_data,
                trips_and_tokens=tokens,
            )
            return IngestResponse(ok=True, message_id=body.message_id,
                                  parse_status="pending_assignment", segments_created=0)
        # No close trips — auto-create
        new_trip = create_trip_from_segments(db, segments_data, user_id=user["id"])
        trip_id = new_trip.id
        trip_created = True
    raw.trip_id = trip_id
    cols = Segment.__table__.columns.keys()
    created = 0
    for seg_data in segments_data:
        seg = Segment(trip_id=trip_id, raw_email_id=raw.id, parse_status="ok",
                      **{k: v for k, v in seg_data.items() if k in cols})
        seg.meta = seg_data.get("meta", {}); seg.meta["source"] = "email"
        db.add(seg); db.flush(); schedule_enrich(bg, seg.id); created += 1
    raw.parse_status = "ok"; db.commit()
    trip_obj = db.query(Trip).filter(Trip.id == trip_id).first()
    send_ingest_reply(
        to=sender_clean,
        status="ok",
        subject=body.subject,
        segments_data=segments_data,
        trip_name=trip_obj.name if trip_obj else None,
        trip_created=trip_created,
    )
    return IngestResponse(ok=True, message_id=body.message_id, trip_id=trip_id, segments_created=created)


@router.post("/reparse", response_model=IngestResponse)
def reparse_email(body: ReparseRequest, bg: BackgroundTasks, db: Session = Depends(get_db)):
    raw = db.query(RawEmail).filter(RawEmail.id == body.raw_email_id).first()
    if not raw:
        raise HTTPException(404, "Raw email not found")
    db.query(Segment).filter(Segment.raw_email_id == raw.id).delete(); db.flush()
    try:
        segments_data = call_gpt(raw.subject or "", raw.body_text or "")
    except Exception as e:
        raw.parse_status = "failed"; db.commit()
        return IngestResponse(ok=False, message_id=raw.message_id, parse_status="failed", error=str(e))
    if not segments_data:
        raw.parse_status = "no_segments"; db.commit()
        return IngestResponse(ok=True, message_id=raw.message_id, parse_status="no_segments", segments_created=0)
    trip_id = body.trip_id or raw.trip_id
    if not trip_id:
        trip = find_best_trip(db, segments_data)
        trip_id = trip.id if trip else None
    if not trip_id:
        raw.parse_status = "failed"; db.commit()
        return IngestResponse(ok=False, message_id=raw.message_id, parse_status="failed", error="No trip found")
    raw.trip_id = trip_id
    cols = Segment.__table__.columns.keys()
    created = 0
    for seg_data in segments_data:
        seg = Segment(trip_id=trip_id, raw_email_id=raw.id, parse_status="ok",
                      **{k: v for k, v in seg_data.items() if k in cols})
        seg.meta = seg_data.get("meta", {}); seg.meta["source"] = "email"
        db.add(seg); db.flush(); schedule_enrich(bg, seg.id); created += 1
    raw.parse_status = "ok"; db.commit()
    return IngestResponse(ok=True, message_id=raw.message_id, trip_id=trip_id, segments_created=created)


@router.get("/review")
def get_review_emails(db: Session = Depends(get_db)):
    return db.query(RawEmail).filter(
        RawEmail.parse_status.in_(["failed", "needs_review", "no_segments"])
    ).order_by(RawEmail.received_at.desc()).limit(50).all()


def _last(s): return s.strip().split()[-1] if s and s.strip() else None
def _dt(s): return s if s and "T" in str(s) else (s if s else None)

def normalise_segments(raw):
    out = []
    for s in raw:
        n = {"type": s.get("type", "flight"), "confirmed": True, "meta": s.get("meta", {"notes": ""})}
        n["origin"] = s.get("origin") or _last(s.get("departure_airport", ""))
        n["destination"] = s.get("destination") or _last(s.get("arrival_airport", ""))
        n["carrier"] = s.get("carrier") or s.get("airline") or s.get("flight_number")
        n["flight_iata"] = s.get("flight_iata") or s.get("flight_number")
        n["departs_at"] = _dt(s.get("departs_at") or s.get("departure_time"))
        n["departs_tz"] = s.get("departs_tz") or s.get("departure_timezone")
        n["arrives_at"] = _dt(s.get("arrives_at") or s.get("arrival_time"))
        n["arrives_tz"] = s.get("arrives_tz") or s.get("arrival_timezone")
        n["confirmation_ref"] = s.get("confirmation_ref") or s.get("reference") or s.get("booking_ref")
        out.append(n)
    return out

# ── PDF upload endpoint ───────────────────────────────────────────────────────


@router.post("/upload-pdf")
async def upload_pdf(
    file: UploadFile = File(...),
    trip_id: str = None,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Accept a PDF upload, extract text with pdfplumber, run through the
    ingest pipeline and return the same IngestResponse shape.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(413, "PDF too large — maximum 10 MB")

    # ── Extract text ─────────────────────────────────────────────────────────
    try:
        import pdfplumber
        with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
            pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
        pdf_text = "\n\n".join(pages)
    except Exception as e:
        raise HTTPException(422, f"Could not read PDF: {e}")

    if not pdf_text.strip():
        return IngestResponse(
            ok=False, message_id=f"pdf-{file.filename}",
            parse_status="no_segments", segments_created=0,
            error="No readable text found in PDF — it may be scanned or image-only",
        )

    # Truncate to stay within GPT context
    if len(pdf_text) > 12000:
        pdf_text = pdf_text[:12000] + "\n[truncated]"

    # ── Resolve user and find/validate trip ──────────────────────────────────
    message_id = f"pdf-upload-{_uuid.uuid4().hex[:12]}"

    # Validate trip ownership if provided
    if trip_id:
        trip = db.query(Trip).filter(Trip.id == trip_id, Trip.user_id == user["id"]).first()
        if not trip:
            raise HTTPException(404, "Trip not found")
    else:
        trip = None

    # ── Parse via GPT ────────────────────────────────────────────────────────
    raw = RawEmail(
        message_id=message_id,
        from_address=user["email"],
        subject=f"PDF upload: {file.filename}",
        body_text=pdf_text,
        parse_status="processing",
    )
    db.add(raw); db.flush()

    try:
        segments_data = call_gpt(f"PDF: {file.filename}", pdf_text)
    except Exception as e:
        raw.parse_status = "failed"; db.commit()
        return IngestResponse(ok=False, message_id=message_id, parse_status="failed", error=str(e))

    if not segments_data:
        raw.parse_status = "no_segments"; db.commit()
        return IngestResponse(ok=True, message_id=message_id, parse_status="no_segments", segments_created=0)

    # ── Find or match trip ────────────────────────────────────────────────────
    if not trip_id:
        trip = find_best_trip(db, segments_data, user_id=user["id"])
        trip_id = trip.id if trip else None

    if not trip_id:
        raw.parse_status = "failed"; db.commit()
        return IngestResponse(ok=False, message_id=message_id, parse_status="failed",
                              error="Could not match PDF to a trip — create a trip first")

    raw.trip_id = trip_id
    cols = Segment.__table__.columns.keys()
    created = 0
    for seg_data in segments_data:
        seg = Segment(trip_id=trip_id, raw_email_id=raw.id, parse_status="ok",
                      **{k: v for k, v in seg_data.items() if k in cols})
        seg.meta = seg_data.get("meta", {}); seg.meta["source"] = "pdf_upload"
        db.add(seg); db.flush(); schedule_enrich(bg, seg.id); created += 1

    raw.parse_status = "ok"; db.commit()

    trip_obj = db.query(Trip).filter(Trip.id == trip_id).first()
    send_ingest_reply(
        to=user["email"],
        status="ok",
        subject=f"PDF upload: {file.filename}",
        segments_data=segments_data,
        trip_name=trip_obj.name if trip_obj else None,
    )

    return IngestResponse(ok=True, message_id=message_id, trip_id=trip_id, segments_created=created)
