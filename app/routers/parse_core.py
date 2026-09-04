import httpx
"""
parse_core.py — Shared constants, clients, and utility functions for parse routers.
No FastAPI router here — imported by parse_dialog, parse_assist, parse_planner, parse_connect.
"""
from openai import OpenAI
import os, json, re, httpx
from sqlalchemy import text

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
AVIATIONSTACK_BASE = "http://api.aviationstack.com/v1/flights"


# ── Ownership helper ──────────────────────────────────────────────────────────

def _verify_trip_ownership(trip_id: str, user: dict, db):
    from app.models.models import Trip
    from fastapi import HTTPException
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.user_id != user["id"]:
        raise HTTPException(403, "Not your trip")
    return trip



SYSTEM_ONESHOT = """Extract travel data from natural language. Return ONLY JSON:
{"type":"flight|hotel|train|car|activity|event|meeting|appointment|other",
"origin":"city or airport code","destination":"city or airport code",
"carrier":"airline name and flight number e.g. Swiss LX392",
"flight_iata":"IATA flight code only e.g. LX392 or null if not a flight",
"departs_at":"YYYY-MM-DDTHH:MM:00",
"departs_tz":"IANA tz e.g. Europe/Zurich","arrives_at":"YYYY-MM-DDTHH:MM:00 or null",
"arrives_tz":"IANA tz or null","confirmation_ref":"ref or null",
"confirmed":false,"meta":{"notes":"extra details or empty string"}}  // set confirmed=true only if a real booking/confirmation_ref is present
Use year 2026 if no year is given. Infer timezone from city/airport."""

# ── dialog system prompt ─────────────────────────────────────────────────────

SYSTEM_DIALOG = """You are a smart travel assistant helping fill in a trip itinerary entry.

You receive the trip details and a list of ALREADY LOGGED segments. Use them to:
- Avoid asking for information already in the itinerary (flights, hotels, dates).
- Infer missing details from context (e.g. if the user just landed in Sao Paulo, timezone is America/Sao_Paulo).
- Never ask about flights, hotels or transfers that are already logged.

SEGMENT TYPES: flight, hotel, train, car, taxi, activity, event, meeting, appointment, other
- taxi: pickup address, dropoff address, carrier=driver/company name, departs_at, meta.phone, meta.driver, meta.notes
- car: rental pickup/dropoff, carrier=rental company
- Use "taxi" for pre-booked rides, airport transfers, private drivers.
- Use "event" for non-transport happenings with no fixed transport component (visits, celebrations, tributes, ceremonies, family gatherings).
- Use "meeting" for work/business/social meetings.
- Use "appointment" for medical, professional, or personal appointments (doctor, dentist, official errands).
- Only fall back to "other" if none of the above fit.
- If the user's account has custom segment types listed below (CUSTOM SEGMENT TYPES), prefer one of those keys over "other" when it clearly matches what the user described — use the exact key given, not the label.

MULTIPLE SEGMENTS IN ONE MESSAGE — CRITICAL, NEVER SILENTLY DROP:
- A single message often describes more than one distinct segment (e.g. two appointments at different times/places on the same day, an outbound + return flight, several stops).
- Never merge two distinct times/places into a single draft, and never silently discard any of them.
- Extract the FIRST chronological one as the normal "draft".
- List every OTHER distinct segment you noticed (that is not yet logged) as short plain-language descriptions in "more_segments" — one string per segment, written so it could itself be sent back to you as a follow-up message and understood on its own (include date, time, place, and any name/reference mentioned). Example: "20/07/26 15:00 at Hospital do Coração, same tribute for papai".
- If there is only one segment, "more_segments" must be an empty list.

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
  "more_segments": [],
  "missing": []
}
Use 2026 if no year. Infer IANA timezone from city/airport."""

# ── AviationStack ────────────────────────────────────────────────────────────

# ── AviationStack ─────────────────────────────────────────────────────────────



# ── Swiss train time verification ─────────────────────────────────────────────



# ── AviationStack ───────────────────────────────────────────

async def aviationstack_lookup(flight_iata: str, flight_date: str):
    """
    Returns (flight_data_or_None, limitation_note_or_None).
    limitation_note is a user-friendly explanation when the free tier can't help.
    """
    key = os.getenv("AVIATIONSTACK_KEY")
    if not key:
        return None, "AviationStack key not configured on this server."

    try:
        async with httpx.AsyncClient(timeout=8) as client_:
            resp = await client_.get(
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


