from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal
from app.models.models import Segment, Trip
from app.schemas.schemas import SegmentCreate, SegmentUpdate, SegmentOut
from app.routers.enrich import _enrich_segment
from app.routers.deps import get_current_user
import asyncio
import logging

log = logging.getLogger("tripdex.segments")

_last_enrich_time = 0.0

def _sync_trip_dates(trip, db):
    """
    Expand trip start_date/end_date to cover all its segments.
    Called after any segment create/update so the trip dates stay in sync.
    """
    from sqlalchemy import text as _text
    rows = db.execute(_text(
        "SELECT departs_at, arrives_at FROM segments WHERE trip_id=:tid AND departs_at IS NOT NULL"
    ), {"tid": trip.id}).fetchall()
    if not rows:
        return
    dates = []
    for departs_at, arrives_at in rows:
        if departs_at:
            dates.append(departs_at[:10])
        if arrives_at:
            dates.append(arrives_at[:10])
    if not dates:
        return
    new_start = min(dates)
    new_end   = max(dates)
    changed = False
    if not trip.start_date or new_start < trip.start_date:
        trip.start_date = new_start; changed = True
    if not trip.end_date or new_end > trip.end_date:
        trip.end_date = new_end; changed = True
    if changed:
        db.commit()


async def _bg_enrich(segment_id: str):
    """Run enrichment in background after segment create/update."""
    global _last_enrich_time
    import time as _time
    # Respect AeroDataBox 1 req/sec rate limit
    elapsed = _time.monotonic() - _last_enrich_time
    if elapsed < 1.1:
        await __import__('asyncio').sleep(1.1 - elapsed)
    _last_enrich_time = _time.monotonic()

    db = SessionLocal()
    try:
        seg = db.query(Segment).filter(Segment.id == segment_id).first()
        if not seg:
            return
        enrichment = await _enrich_segment(seg)
        if enrichment.get("enrich_status") not in ("skipped", None):
            # Write back segment-column fields (prefixed with _) if segment lacks them
            if enrichment.get("_arrives_at") and not seg.arrives_at:
                # AviationStack free tier returns today's flight, not the actual future date.
                # Use only the TIME portion, anchored to the segment's own departure date.
                try:
                    from datetime import datetime as _dt2, timedelta as _td2
                    arr_time = enrichment["_arrives_at"][11:16]  # "HH:MM"
                    dep_date = (seg.departs_at or "")[:10]       # "YYYY-MM-DD"
                    if dep_date and arr_time:
                        dep_time = (seg.departs_at or "")[ 11:16]
                        # If arrival time < departure time, flight lands next day
                        next_day = arr_time < dep_time
                        arr_date = dep_date
                        if next_day:
                            d = _dt2.fromisoformat(dep_date) + _td2(days=1)
                            arr_date = d.strftime("%Y-%m-%d")
                        seg.arrives_at = f"{arr_date}T{arr_time}:00"
                except Exception:
                    pass
            if enrichment.get("_arrives_tz") and not seg.arrives_tz:
                seg.arrives_tz = enrichment["_arrives_tz"]
            # Strip private keys before storing in meta
            meta_enrichment = {k: v for k, v in enrichment.items() if not k.startswith("_")}
            meta = dict(seg.meta or {})
            meta.update(meta_enrichment)
            seg.meta = meta
            db.commit()
            log.info("auto-enriched segment %s (%s) → %s", segment_id[:8], seg.type, enrichment.get("enrich_status"))
    except Exception as e:
        log.warning("auto-enrich failed for %s: %s", segment_id[:8], e)
    finally:
        db.close()

def schedule_enrich(bg: BackgroundTasks, segment_id: str):
    # Use create_task so we stay within the running event loop
    bg.add_task(_run_enrich, segment_id)

async def _run_enrich(segment_id: str):
    await _bg_enrich(segment_id)

router = APIRouter(prefix="/api/segments", tags=["segments"])

def _owned_segment(segment_id: str, user: dict, db: Session) -> Segment:
    from sqlalchemy import text as _text
    seg = db.query(Segment).filter(Segment.id == segment_id).first()
    if not seg:
        raise HTTPException(404, "Segment not found")
    if seg.trip_id:
        # Normal segment: verify via trip ownership
        trip = db.query(Trip).filter(Trip.id == seg.trip_id).first()
        if not trip or trip.user_id != user["id"]:
            raise HTTPException(403, "Not your segment")
    else:
        # Orphan segment: verify via raw_email sender
        if seg.raw_email_id:
            row = db.execute(_text(
                "SELECT from_address FROM raw_emails WHERE id=:id"
            ), {"id": seg.raw_email_id}).fetchone()
            if not row or user["email"].lower() not in row[0].lower():
                raise HTTPException(403, "Not your segment")
        else:
            raise HTTPException(403, "Not your segment")
    return seg

@router.get("/", response_model=list[SegmentOut])
def list_segments(trip_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip or trip.user_id != user["id"]:
        raise HTTPException(403, "Not your trip")
    return (
        db.query(Segment)
        .filter(Segment.trip_id == trip_id)
        .order_by(Segment.departs_at.asc())
        .all()
    )

@router.post("/", response_model=SegmentOut, status_code=201)
def create_segment(body: SegmentCreate, bg: BackgroundTasks, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    trip = db.query(Trip).filter(Trip.id == body.trip_id).first()
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.user_id != user["id"]:
        raise HTTPException(403, "Not your trip")
    seg = Segment(**body.model_dump())
    db.add(seg); db.commit(); db.refresh(seg)
    _sync_trip_dates(trip, db)
    schedule_enrich(bg, seg.id)
    return seg

@router.get("/{segment_id}", response_model=SegmentOut)
def get_segment(segment_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return _owned_segment(segment_id, user, db)

@router.patch("/{segment_id}", response_model=SegmentOut)
def update_segment(segment_id: str, body: SegmentUpdate, bg: BackgroundTasks, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    seg = _owned_segment(segment_id, user, db)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(seg, k, v)
    db.commit(); db.refresh(seg)
    if seg.trip_id:
        trip = db.query(Trip).filter(Trip.id == seg.trip_id).first()
        if trip: _sync_trip_dates(trip, db)
    schedule_enrich(bg, seg.id)
    return seg

@router.delete("/{segment_id}", status_code=204)
def delete_segment(segment_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    seg = _owned_segment(segment_id, user, db)
    db.delete(seg); db.commit()
