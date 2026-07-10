"""
parse_planner.py — /api/parse/plan (AI trip planner)
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any
from app.database import get_db
from app.models.models import Segment, Trip
from app.routers.deps import get_current_user
from app.routers.segments import schedule_enrich
from app.schemas.schemas import SegmentOut
from app.routers.parse_core import (
    _verify_trip_ownership, enrich_with_aviationstack,
    client
)
from app.routers.parse_dialog import DialogMessage
from sqlalchemy import text
import os, json, re

router = APIRouter(prefix="/api/parse", tags=["parse"])


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
      "type": "flight|hotel|train|taxi|car|activity|event|meeting|appointment|other",
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


