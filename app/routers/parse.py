from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any
from app.database import get_db
from app.models.models import Segment, Trip
from app.routers.deps import get_current_user
from app.routers.segments import schedule_enrich
from app.routers.guardrails import run_guardrails, GuardrailHit
from app.schemas.schemas import SegmentOut
import os, json, re, httpx
import logging
import logging.handlers as _lh
from openai import OpenAI

router = APIRouter(prefix="/api/parse", tags=["parse"])

def _verify_trip_ownership(trip_id: str, user: dict, db):
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.user_id != user["id"]:
        raise HTTPException(403, "Not your trip")
    return trip
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

AVIATIONSTACK_BASE = "http://api.aviationstack.com/v1/flights"

# ── one-shot system prompt (kept for /parse/text backward compat) ────────────

SYSTEM_ONESHOT = """Extract travel data from natural language. Return ONLY JSON:
{"type":"flight|hotel|train|car|activity|other",
"origin":"city or airport code","destination":"city or airport code",
"carrier":"airline name and flight number e.g. Swiss LX392",
"flight_iata":"IATA flight code only e.g. LX392 or null if not a flight",
"departs_at":"YYYY-MM-DDTHH:MM:00",
"departs_tz":"IANA tz e.g. Europe/Zurich","arrives_at":"YYYY-MM-DDTHH:MM:00 or null",
"arrives_tz":"IANA tz or null","confirmation_ref":"ref or null",
"confirmed":true,"meta":{"notes":"extra details or empty string"}}
Use year 2026 if no year is given. Infer timezone from city/airport."""

# ── dialog system prompt ─────────────────────────────────────────────────────

SYSTEM_DIALOG = """You are a smart travel assistant helping fill in a trip itinerary entry.

You receive the trip details and a list of ALREADY LOGGED segments. Use them to:
- Avoid asking for information already in the itinerary (flights, hotels, dates).
- Infer missing details from context (e.g. if the user just landed in Sao Paulo, timezone is America/Sao_Paulo).
- Never ask about flights, hotels or transfers that are already logged.

SEGMENT TYPES: flight, hotel, train, car, taxi, activity, other
- taxi: pickup address, dropoff address, carrier=driver/company name, departs_at, meta.phone, meta.driver, meta.notes
- car: rental pickup/dropoff, carrier=rental company
- Use "taxi" for pre-booked rides, airport transfers, private drivers.

REASON BEFORE ASKING:
- NEVER guess a flight number silently. If the user hasn't given one, offer concrete options.
- Use your knowledge of typical schedules: Swiss/SWISS operates ZRH-VIE as LX1390/LX1392/LX1394 (morning/midday/evening). Use route+airline+time to propose the most likely option and ask for confirmation: "Is that LX1390 at 06:55 or LX1392 at 10:25?"
- Ask ONCE, combining all missing info into one question. Never ask for flight number twice.
- Bundle date+time if both missing. Never ask for timezone or year.
- Never ask for arrival time — compute from typical route duration.
- ZRH-HRG ~4h, ZRH-LHR ~2h, ZRH-JFK ~9h, ZRH-DXB ~6h, ZRH-VIE ~1h15m, ZRH-AMS ~1h30m, ZRH-CDG ~1h20m.
- For carrier: infer airline from flight number prefix (LX=Swiss, BA=British Airways, SK=SAS, OS=Austrian, LH=Lufthansa, U2=easyJet, FR=Ryanair).
- If user confirms one of your proposed options, use that flight number immediately — do not ask again.

RETURN FLIGHT: When status=ready for a flight, always populate return_draft:
- Reverse the route, use trip end_date as departure.
- Propose the likely return flight (e.g. outbound WK591 ZRH-HRG -> return WK592 HRG-ZRH).
- Set return_draft=null for non-flight segments or one-way flights.

Set status=ready as soon as entry is complete. Do not ask for optional details.

WHEN YOU ARE STUCK: If after several exchanges you still cannot understand what the user wants,
or the request is outside your capabilities (not a travel segment, ambiguous beyond resolution,
or the user seems frustrated), return status="stuck" with a short, honest explanation in "question".
Do not keep asking the same things in circles. Better to admit you're stuck and let the user report it.

Fields needed — do NOT set status=ready until ALL are known:
- flight: origin, destination, departs_at, carrier, flight_iata (REQUIRED — always ask if missing)
  carrier MUST be "Airline FlightNumber" e.g. "Swiss LX1392" — never just the airline name alone.
  flight_iata MUST be just the code e.g. "LX1392".
- hotel: origin, carrier (name), departs_at, meta.nights
- taxi: origin (pickup address), destination (dropoff address), departs_at, carrier (driver/company)
- train/car/activity: origin, departs_at

Return ONLY JSON:
{
  "status": "question" or "ready",
  "question": "...",
  "draft": {"type":"...","origin":null,"destination":null,"carrier":null,"flight_iata":null,"departs_at":null,"departs_tz":null,"arrives_at":null,"arrives_tz":null,"confirmation_ref":null,"confirmed":false,"meta":{"notes":"","nights":null,"phone":null,"driver":null}},
  "return_draft": {"type":"flight","origin":null,"destination":null,"carrier":null,"flight_iata":null,"departs_at":null,"departs_tz":null,"arrives_at":null,"arrives_tz":null,"confirmed":false,"meta":{"notes":""}},
  "missing": []
}
Use 2026 if no year. Infer IANA timezone from city/airport."""

# ── AviationStack ────────────────────────────────────────────────────────────

async def aviationstack_lookup(flight_iata: str, flight_date: str):
    """
    Returns (flight_data_or_None, limitation_note_or_None).
    limitation_note is a user-friendly explanation when the free tier can't help.
    """
    key = os.getenv("AVIATIONSTACK_KEY")
    if not key:
        return None, "AviationStack key not configured on this server."

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                AVIATIONSTACK_BASE,
                params={"access_key": key, "flight_iata": flight_iata.upper(), "flight_date": flight_date},
            )
    except httpx.TimeoutException:
        return None, "Flight lookup timed out — AviationStack didn't respond in time."
    except httpx.HTTPError as e:
        return None, f"Could not reach AviationStack: {e}"

    if resp.status_code == 403:
        return None, (
            "Tripdex uses the AviationStack free tier, which only supports live flight lookups "
            "— date filtering is a paid feature. Flight times are estimated from what you typed."
        )
    if resp.status_code != 200:
        return None, f"AviationStack returned an unexpected error (HTTP {resp.status_code})."

    data = resp.json().get("data", [])
    if not data:
        return None, (
            "No live data found for this flight right now. Tripdex uses the AviationStack free tier, "
            "which only covers currently active flights — future or past flights don't appear. "
            "Flight times are estimated from what you typed."
        )

    return data[0], None


async def enrich_with_aviationstack(data: dict):
    """
    Try to verify and enrich a flight draft via AviationStack.
    Returns (enriched_data, parse_status, aviationstack_note).
    """
    flight_iata = data.get("flight_iata")
    departs_at = data.get("departs_at", "")
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", departs_at or "")

    if not flight_iata or not date_match:
        return data, "ok", None

    flight_date = date_match.group(1)
    f, limitation = await aviationstack_lookup(flight_iata, flight_date)

    if limitation:
        return data, "needs_review", limitation

    dep = f.get("departure", {})
    arr = f.get("arrival", {})

    data["origin"] = dep.get("iata") or data.get("origin")
    data["destination"] = arr.get("iata") or data.get("destination")
    data["departs_at"] = dep.get("scheduled") or data.get("departs_at")
    data["departs_tz"] = dep.get("timezone") or data.get("departs_tz")
    data["arrives_at"] = arr.get("scheduled") or data.get("arrives_at")
    data["arrives_tz"] = arr.get("timezone") or data.get("arrives_tz")

    meta = data.get("meta", {})
    if dep.get("terminal"):
        meta["terminal_departure"] = dep["terminal"]
    if dep.get("gate"):
        meta["gate_departure"] = dep["gate"]
    if arr.get("terminal"):
        meta["terminal_arrival"] = arr["terminal"]
    meta["aviationstack_verified"] = True
    data["meta"] = meta

    return data, "ok", None


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
        data, parse_status, av_note = await enrich_with_aviationstack(data)

    cols = Segment.__table__.columns.keys()
    seg = Segment(trip_id=body.trip_id, parse_status=parse_status,
                  **{k: v for k, v in data.items() if k in cols})
    seg.meta = data.get("meta", {})
    if av_note:
        seg.meta["aviationstack_note"] = av_note
    db.add(seg); db.commit(); db.refresh(seg)
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
    messages = [{"role": "system", "content": SYSTEM_DIALOG + trip_context}]
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
    if status=='ready' and draft.get('type')=='train':
        draft,timetable_note=await verify_train_time(draft)
    return DialogResponse(status=status,question=gpt.get('question'),draft=draft,return_draft=return_draft,missing=gpt.get('missing',[]),aviationstack_note=av_note,return_aviationstack_note=return_av_note,timetable_note=timetable_note,history=new_history)


@router.post("/dialog/confirm", response_model=SegmentOut, status_code=201)
async def dialog_confirm(body: DialogConfirmRequest, bg: BackgroundTasks, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
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


async def verify_train_time(draft: dict) -> tuple[dict, str | None]:
    """
    Cross-check a train draft against the real timetable (DB Vendo or SBB).
    Corrects departs_at/arrives_at if the user-supplied time doesn't match any
    real service, and returns a human-readable timetable_note.
    Returns (corrected_draft, note_or_None).
    """
    import asyncio
    from app.routers.enrich import search_connections, _is_german_station

    origin      = draft.get("origin", "")
    destination = draft.get("destination", "")
    departs_at  = draft.get("departs_at", "")  # "YYYY-MM-DDTHH:MM:00"

    if not origin or not destination or not departs_at:
        return draft, None

    # Format for search_connections: "YYYY-MM-DD HH:MM"
    datetime_str = departs_at[:16].replace("T", " ")

    try:
        result = await search_connections(
            from_station=origin,
            to_station=destination,
            datetime_str=datetime_str,
            limit=4,
        )
    except Exception:
        return draft, None

    connections = result.get("connections", [])
    if not connections:
        # No results at all — can't verify, don't block
        return draft, None

    source = result.get("source", "timetable")

    # Parse user-supplied departure time
    try:
        from datetime import datetime as _dt
        user_dep = _dt.strptime(datetime_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return draft, None

    # Find the closest real departure
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
        return draft, None

    TOLERANCE_SECONDS = 3 * 60  # 3 minutes — same train, rounding

    if best_diff <= TOLERANCE_SECONDS:
        # Times match — silently correct to exact timetable time and add carrier
        corrected = dict(draft)
        corrected["departs_at"] = best["departs"].replace(" ", "T") + ":00"
        if best.get("arrives"):
            corrected["arrives_at"] = best["arrives"].replace(" ", "T") + ":00"
        if not corrected.get("carrier") and best.get("carrier"):
            corrected["carrier"] = best["carrier"]
        note = (
            f"✓ Verified against {source}: {best['carrier'] or ''} "
            f"departs {best['departs'][11:16]}, arrives {best['arrives'][11:16]}"
            + (f", platform {best['platform_dep']}" if best.get("platform_dep") else "")
            + "."
        )
        return corrected, note

    # Times don't match — the user gave a fictional time
    # Correct to the nearest real train
    corrected = dict(draft)
    corrected["departs_at"] = best["departs"].replace(" ", "T") + ":00"
    if best.get("arrives"):
        corrected["arrives_at"] = best["arrives"].replace(" ", "T") + ":00"
    if best.get("carrier"):
        corrected["carrier"] = best["carrier"]

    note = (
        f"⚠ No train at {datetime_str[11:16]} — corrected to nearest real service: "
        f"{best['carrier'] or 'train'} at {best['departs'][11:16]}"
        + (f" (platform {best['platform_dep']})" if best.get("platform_dep") else "")
        + f". Source: {source}."
    )
    return corrected, note


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


# ── Edit segment assistant endpoint ──────────────────────────────────────────

class EditAssistRequest(BaseModel):
    segment: dict
    history: list
    message: str
    trip_segments: list = []

class EditAssistResponse(BaseModel):
    message: str
    updates: dict

@router.post("/assist/edit", response_model=EditAssistResponse)
async def edit_assist(body: EditAssistRequest):
    # Build trip context from other segments
    trip_ctx = ""
    if body.trip_segments:
        other = [s for s in body.trip_segments if s.get("type")]
        if other:
            lines_ctx = []
            for s in sorted(other, key=lambda x: x.get("departs_at") or ""):
                dep = (s.get("departs_at") or "")[:16].replace("T", " ")
                arr = (s.get("arrives_at") or "")[:16].replace("T", " ")
                lines_ctx.append(
                    f"  - {s['type']}: {s.get('origin','?')}→{s.get('destination','?')} "
                    f"{dep}{(' → '+arr) if arr else ''} [{s.get('carrier','')}]"
                )
            trip_ctx = "\n\nOTHER SEGMENTS IN THIS TRIP (use for context):\n" + "\n".join(lines_ctx)

    system = f"""You are helping edit a travel segment. Current state: {json.dumps(body.segment)}.{trip_ctx}

The user wants to update one or more fields. Respond with a JSON object:
{{
  "message": "short confirmation e.g. Done! Set seat to 12A.",
  "updates": {{
    "origin": null, "destination": null, "carrier": null,
    "airline": null, "flight_number": null, "flight_iata": null,
    "departs_date": null, "departs_time": null,
    "arrives_date": null, "arrives_time": null,
    "confirmation_ref": null, "notes": null, "confirmed": null
  }}
}}
Only include fields in updates that should change. Omit null fields.
Use DD.MM.YYYY for dates, HH:MM for times.
If user gives a flight number like LX966 → set flight_number=LX966, airline=Swiss (infer from IATA prefix).
Seat/terminal/gate → append to existing notes, don't replace.
Vague time references like "after my train ride" or "after arriving" → look at OTHER SEGMENTS to find the relevant train/flight arriving at the same city, use its arrives_at time + 30min as the new departs_time.
For "mark as confirmed" → confirmed=true. For "unconfirm" → confirmed=false.
If user asks to SEARCH FOR A CONNECTION or train between two places → set action="search_connection" and include from_station, to_station, and either datetime or arrive_before in the updates object.
Respond ONLY with the JSON object, no markdown fences."""

    messages = list(body.history) + [{"role": "user", "content": body.message}]

    try:
        r = client.chat.completions.create(
            model="gpt-4o", temperature=0,
            messages=[{"role": "system", "content": system}] + messages,
            response_format={"type": "json_object"},
        )
        raw = r.choices[0].message.content
        parsed = json.loads(raw)
        return EditAssistResponse(
            message=parsed.get("message", "Done!"),
            updates={k: v for k, v in parsed.get("updates", {}).items() if v is not None}
        )
    except Exception as e:
        raise HTTPException(500, f"Assistant error: {e}")

class TripAssistRequest(BaseModel):
    message: str

class TripAssistResponse(BaseModel):
    message: str
    name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

@router.post("/assist/trip", response_model=TripAssistResponse)
async def trip_assist(body: TripAssistRequest):
    from datetime import date
    today = str(date.today())
    system = f"""Extract trip details from a natural language description. Today is {today}.
Return ONLY a JSON object:
{{
  "message": "short confirmation e.g. Got it! Berlin trip 3–7 June 2026.",
  "name": "trip name e.g. Berlin Trip",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD"
}}
Infer reasonable dates if partial info given (e.g. "June" → use current year).
If duration not specified, default to 4 days.
Name should be concise: city + optional label (e.g. "Berlin", "Tokyo Conference", "Egypt Holiday").
Respond ONLY with the JSON object."""

    try:
        r = client.chat.completions.create(
            model="gpt-4o", temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": body.message}
            ],
            response_format={"type": "json_object"},
        )
        parsed = json.loads(r.choices[0].message.content)
        return TripAssistResponse(
            message=parsed.get("message", "Done!"),
            name=parsed.get("name"),
            start_date=parsed.get("start_date"),
            end_date=parsed.get("end_date"),
        )
    except Exception as e:
        raise HTTPException(500, f"Trip assistant error: {e}")


# ── Trip planner (top-level assistant) ───────────────────────────────────────

SYSTEM_PLANNER = """You are a travel planning assistant. Help the user plan a complete trip.

Your job in each turn:
1. Extract any trip/segment details mentioned
2. Ask ONE focused follow-up question to fill in the most important missing piece
3. When you have enough for a segment, include it in "save_segments"
4. When you have the trip name/dates, include them in "trip"

Return ONLY JSON:
{
  "question": "your next question to the user (null if trip is complete)",
  "status": "planning" or "complete",
  "trip": {"name": null, "start_date": null, "end_date": null},
  "save_segments": [
    {
      "type": "flight|hotel|train|taxi|car|activity|other",
      "origin": null, "destination": null, "carrier": null,
      "flight_iata": null,
      "departs_at": "YYYY-MM-DDTHH:MM",
      "arrives_at": "YYYY-MM-DDTHH:MM",
      "departs_tz": null, "arrives_tz": null,
      "confirmed": false,
      "meta": {}
    }
  ],
  "summary": "one-line summary of what you just saved (null if nothing saved)"
}

Rules:
- save_segments only when you have type + origin + departs_at at minimum
- For flights: require flight_iata before saving — if user says "Swiss" ask which flight number
- Infer IANA timezone from city/airport
- Use current year if not specified
- carrier format: "Airline FlightNumber" e.g. "Swiss LX966"
- Never invent flight numbers — ask the user
- For return flights: ask explicitly, don't assume
- status=complete only when user says they're done or trip is fully planned
- Keep questions short and specific — one thing at a time
"""

class PlanRequest(BaseModel):
    message: str
    history: list = []
    trip_id: Optional[str] = None  # null = new trip, set after first save

class PlanSegmentOut(BaseModel):
    id: str
    type: str
    origin: Optional[str] = None
    destination: Optional[str] = None
    carrier: Optional[str] = None
    departs_at: Optional[str] = None

class PlanResponse(BaseModel):
    question: Optional[str] = None
    status: str
    trip: Optional[dict] = None
    saved_segments: list = []
    summary: Optional[str] = None
    trip_id: Optional[str] = None
    history: list = []

@router.post("/plan", response_model=PlanResponse)
async def plan_trip(body: PlanRequest, bg: BackgroundTasks, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from app.models.models import Trip as TripModel

    messages = list(body.history) + [{"role": "user", "content": body.message}]

    r = client.chat.completions.create(
        model="gpt-4o", temperature=0,
        messages=[{"role": "system", "content": SYSTEM_PLANNER}] + messages,
        response_format={"type": "json_object"},
    )
    raw = r.choices[0].message.content
    try:
        gpt = json.loads(raw)
    except Exception:
        raise HTTPException(500, "Planner parse error")

    new_history = messages + [{"role": "assistant", "content": raw}]
    trip_id = body.trip_id
    saved_segments = []

    # Create or update trip
    trip_data = gpt.get("trip") or {}
    if trip_data.get("name") and not trip_id:
        trip = TripModel(
            name=trip_data["name"],
            start_date=trip_data.get("start_date"),
            end_date=trip_data.get("end_date"),
        )
        db.add(trip); db.commit(); db.refresh(trip)
        trip_id = trip.id
    elif trip_data.get("name") and trip_id:
        trip = db.query(TripModel).filter(TripModel.id == trip_id).first()
        if trip:
            if trip_data.get("name"):       trip.name       = trip_data["name"]
            if trip_data.get("start_date"): trip.start_date = trip_data["start_date"]
            if trip_data.get("end_date"):   trip.end_date   = trip_data["end_date"]
            db.commit()

    # Save any ready segments
    if trip_id:
        cols = Segment.__table__.columns.keys()
        for seg_data in (gpt.get("save_segments") or []):
            if not seg_data.get("type") or not seg_data.get("departs_at"):
                continue
            # Merge flight_iata into carrier
            flight_iata = seg_data.get("flight_iata")
            carrier = seg_data.get("carrier") or ""
            if flight_iata and flight_iata not in carrier:
                airline = re.sub(r'[A-Z]{2}\d{2,4}', '', carrier).strip()
                seg_data["carrier"] = f"{airline} {flight_iata}".strip() if airline else flight_iata

            seg = Segment(
                trip_id=trip_id,
                parse_status="ok",
                **{k: v for k, v in seg_data.items() if k in cols}
            )
            seg.meta = seg_data.get("meta") or {}
            if flight_iata:
                seg.meta["flight_iata"] = flight_iata
            db.add(seg); db.commit(); db.refresh(seg)
            schedule_enrich(bg, seg.id)
            saved_segments.append({"id": seg.id, "type": seg.type,
                                   "origin": seg.origin, "destination": seg.destination,
                                   "carrier": seg.carrier, "departs_at": seg.departs_at})

    return PlanResponse(
        question=gpt.get("question"),
        status=gpt.get("status", "planning"),
        trip=trip_data if trip_data.get("name") else None,
        saved_segments=saved_segments,
        summary=gpt.get("summary"),
        trip_id=trip_id,
        history=new_history,
    )


# ── Connection search endpoint ────────────────────────────────────────────────

class ConnectionSearchRequest(BaseModel):
    from_station: str
    to_station: str
    datetime: Optional[str] = None      # "YYYY-MM-DD HH:MM" depart after
    arrive_before: Optional[str] = None  # "YYYY-MM-DD HH:MM" arrive before
    context: Optional[str] = None        # natural language request for AI interpretation

class ConnectionSearchResponse(BaseModel):
    connections: list = []
    source: Optional[str] = None
    fallback: Optional[dict] = None
    ai_summary: Optional[str] = None

@router.post("/connections/search", response_model=ConnectionSearchResponse)
async def search_connections_endpoint(body: ConnectionSearchRequest):
    from app.routers.enrich import search_connections as _search

    result = await _search(
        from_station=body.from_station,
        to_station=body.to_station,
        datetime_str=body.datetime or "",
        arrive_before=body.arrive_before,
    )

    # Generate AI summary
    ai_summary = None
    if result["connections"]:
        conns = result["connections"][:3]
        lines = []
        for i, c in enumerate(conns, 1):
            plat = f" (platform {c['platform_dep']})" if c.get('platform_dep') else ""
            xfer = f", {c['transfers']} transfer{'s' if c['transfers']!=1 else ''}" if c.get('transfers') else ""
            lines.append(f"{i}. {c['carrier'] or 'Train'}{plat}: departs {c['departs']}, arrives {c['arrives']} ({c['duration'].replace('00d','').strip()}{ xfer})")
        ai_summary = "\n".join(lines)
    elif result.get("fallback"):
        fb = result["fallback"]
        suggestions = fb.get("suggestions", [])
        parts = []
        for s in suggestions:
            if s["type"] == "taxi":
                parts.append(s["label"])
            else:
                parts.append(f"{s['label']}: {s['url']}")
        ai_summary = "No live connections found. Options:\n" + "\n".join(f"• {p}" for p in parts)

    return ConnectionSearchResponse(
        connections=result["connections"],
        source=result["source"],
        fallback=result["fallback"],
        ai_summary=ai_summary,
    )
