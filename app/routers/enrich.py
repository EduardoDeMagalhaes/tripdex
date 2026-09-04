"""
Segment enrichment router.
POST /api/segments/{id}/enrich   — enrich a single segment
POST /api/trips/{trip_id}/enrich — enrich all segments in a trip
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.routers.deps import get_current_user
from app.models.models import Segment
from app.schemas.schemas import SegmentOut
import httpx, logging
import urllib.parse
from datetime import datetime, timezone

router = APIRouter(tags=["enrich"])
log = logging.getLogger("tripdex.enrich")

SBB_API = "https://transport.opendata.ch/v1"


# ── helpers ────────────────────────────────────────────────────────────────────

def _station_name(raw: str | None) -> str | None:
    """Normalise a stored origin/destination to something the SBB API likes."""
    if not raw:
        return None
    # The API understands common spellings; strip trailing country hints like "(CH)"
    return raw.replace("(CH)", "").replace("(IT)", "").strip()


def _parse_dt(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%00", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(dt_str[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    return None


async def _enrich_train(seg: Segment) -> dict:
    """
    Query transport.opendata.ch for the stationboard at the departure station
    around the departure time, find the matching train, and return enrichment
    data to merge into seg.meta.
    """
    station = _station_name(seg.origin)
    dt = _parse_dt(seg.departs_at)
    if not station or not dt:
        return {"enrich_status": "skipped", "enrich_reason": "missing origin or departs_at"}

    # transport.opendata.ch stationboard
    # We ask for departures within a ±10-minute window
    params = {
        "station": station,
        "datetime": dt.strftime("%Y-%m-%d %H:%M"),
        "type": "departure",
        "limit": 20,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{SBB_API}/stationboard", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("SBB API error for %s: %s", station, e)
        return {"enrich_status": "api_error", "enrich_reason": str(e)}

    stationboard = data.get("stationboard", [])
    if not stationboard:
        return {"enrich_status": "no_data", "enrich_reason": "empty stationboard response"}

    # Try to match by train number stored in meta
    train_number = (seg.meta or {}).get("train_number")
    matched = None

    for entry in stationboard:
        cat  = entry.get("category", "")
        num  = str(entry.get("number", ""))
        name = entry.get("name", "")          # e.g. "EC 37"

        # Match on train number or full name (carrier field may contain "SBB EC 37")
        if train_number and num == str(train_number):
            matched = entry
            break
        if train_number and train_number in name:
            matched = entry
            break
        # Fallback: match by scheduled departure minute
        sched = entry.get("stop", {}).get("departure")
        if sched:
            sched_dt = _parse_dt(sched)
            if sched_dt and abs((sched_dt - dt).total_seconds()) <= 90:
                matched = entry
                break

    if not matched:
        return {
            "enrich_status": "no_match",
            "enrich_reason": f"train {train_number} not found in stationboard for {station} at {dt}",
        }

    stop = matched.get("stop", {})
    platform_dep = stop.get("platform") or stop.get("track")

    # Try to get arrival platform via the /connections endpoint
    platform_arr = None
    dest = _station_name(seg.destination)
    if dest:
        try:
            conn_params = {
                "from": station,
                "to": dest,
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M"),
                "limit": 3,
            }
            async with httpx.AsyncClient(timeout=10) as client:
                rc = await client.get(f"{SBB_API}/connections", params=conn_params)
                rc.raise_for_status()
                conns = rc.json().get("connections", [])
            for conn in conns:
                sections = conn.get("sections", [])
                for sec in sections:
                    journey = sec.get("journey", {})
                    j_num = str(journey.get("number", ""))
                    j_name = journey.get("name", "")
                    if (train_number and j_num == str(train_number)) or \
                       (train_number and train_number in j_name):
                        arr_stop = sec.get("arrival", {})
                        platform_arr = arr_stop.get("platform") or arr_stop.get("track")
                        break
                if platform_arr:
                    break
        except Exception as e:
            log.warning("SBB connections API error: %s", e)

    # Real-time delay
    dep_realtime = stop.get("departureTimestamp")
    delay_min = None
    if dep_realtime:
        rt_dt = datetime.fromtimestamp(dep_realtime, tz=timezone.utc).replace(tzinfo=None)
        delay_min = int((rt_dt - dt).total_seconds() // 60)

    return {
        "enrich_status": "ok",
        "platform_departure": str(platform_dep) if platform_dep else None,
        "platform_arrival":   str(platform_arr) if platform_arr else None,
        "delay_minutes":      delay_min,
        "enrich_source":      "transport.opendata.ch",
        "enrich_at":          datetime.utcnow().isoformat(),
    }




OSM_NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSM_OVERPASS  = "https://overpass-api.de/api/interpreter"
OSM_HEADERS   = {"User-Agent": "tripdex-travel-app/1.0 (ed@emdm.ch)"}


async def _nominatim_by_address(address: str, hotel_name: str) -> dict | None:
    """Geocode a street address and find the hotel via Overpass nearby."""
    # Step 1: geocode the address
    params = {"q": address, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    try:
        async with httpx.AsyncClient(timeout=10, headers=OSM_HEADERS) as client:
            r = await client.get(OSM_NOMINATIM, params=params)
            r.raise_for_status()
            results = r.json()
    except Exception:
        return None

    if not results:
        return None

    lat = float(results[0]["lat"])
    lon = float(results[0]["lon"])

    # Step 2: Overpass — find tourism=hotel within 100m of that point
    overpass_q = f"""
[out:json][timeout:15];
(
  node["tourism"="hotel"](around:250,{lat},{lon});
  way["tourism"="hotel"](around:250,{lat},{lon});
  node["tourism"="apartment"](around:250,{lat},{lon});
  node["building"="hotel"](around:250,{lat},{lon});
);
out tags center;
"""
    try:
        async with httpx.AsyncClient(timeout=15, headers=OSM_HEADERS) as client:
            rq = await client.post(OSM_OVERPASS, data={"data": overpass_q})
            rq.raise_for_status()
            elements = rq.json().get("elements", [])
    except Exception:
        return None

    if not elements:
        return None

    # Pick closest / best name match
    best = elements[0]
    for el in elements:
        el_name = el.get("tags", {}).get("name", "").lower()
        if any(w in el_name for w in hotel_name.lower().split()):
            best = el
            break

    tags = best.get("tags", {})
    center = best.get("center", {})
    return {
        "phone":   tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "stars":   tags.get("stars"),
        "lat":     str(center.get("lat") or lat),
        "lon":     str(center.get("lon") or lon),
        "osm_id":  best.get("id"),
        "osm_type": best.get("type"),
    }

async def _enrich_hotel(seg: Segment) -> dict:
    """
    Query Nominatim to find the hotel, then Overpass for phone/website details.
    Falls back gracefully at each step.
    """
    hotel_name = seg.carrier  # e.g. "Staybridge Suites Sao Paulo"
    city       = seg.origin   # e.g. "Sao Paulo"

    if not hotel_name:
        return {"enrich_status": "skipped", "enrich_reason": "no carrier/hotel name"}

    # ── Step 1: Nominatim search ────────────────────────────────────────────
    query = hotel_name if not city or city.lower() in hotel_name.lower() \
            else f"{hotel_name}, {city}"

    nom_params = {
        "q": query,
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 1,
        "extratags": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=10, headers=OSM_HEADERS) as client:
            r = await client.get(OSM_NOMINATIM, params=nom_params)
            r.raise_for_status()
            results = r.json()
    except Exception as e:
        log.warning("Nominatim error for %s: %s", hotel_name, e)
        return {"enrich_status": "api_error", "enrich_reason": str(e)}

    # Filter to hotel/accommodation results; fall back to first result
    hotel_results = [x for x in results
                     if x.get("category") in ("tourism", "building")
                     or x.get("type") in ("hotel", "hostel", "motel", "guest_house",
                                           "apartment", "inn", "resort")]
    best = hotel_results[0] if hotel_results else None  # only accept hotel-category results

    if not best:
        # Fallback: try geocoding by address + Overpass proximity search
        existing_addr = (seg.meta or {}).get("address")
        fallback = None
        if existing_addr:
            fallback = await _nominatim_by_address(existing_addr, hotel_name)
        if fallback:
            return {
                "enrich_status":  "ok",
                "enrich_source":  "openstreetmap/overpass-fallback",
                "enrich_at":      datetime.utcnow().isoformat(),
                "address":        existing_addr,
                **{k: v for k, v in fallback.items() if v is not None},
            }
        # Last resort: try Nominatim with city alone to get coords,
        # then Overpass to find any hotel near city centre
        city_fallback = None
        if city:
            city_fallback = await _nominatim_by_address(city, hotel_name)
        if city_fallback:
            return {
                "enrich_status":  "ok",
                "enrich_source":  "openstreetmap/city-fallback",
                "enrich_at":      datetime.utcnow().isoformat(),
                **{k: v for k, v in city_fallback.items() if v is not None},
            }
        return {"enrich_status": "no_match",
                "enrich_reason": f"Nominatim found nothing for: {query}"}

    osm_id   = best.get("osm_id")
    osm_type = best.get("osm_type")   # node / way / relation
    lat      = best.get("lat")
    lon      = best.get("lon")

    # Nominatim extratags sometimes already has phone/website
    extratags = best.get("extratags", {})
    phone   = extratags.get("phone") or extratags.get("contact:phone")
    website = extratags.get("website") or extratags.get("url") or extratags.get("contact:website")
    stars   = extratags.get("stars") or extratags.get("tourism:stars")

    address_obj = best.get("address", {})
    road    = address_obj.get("road", "")
    number  = address_obj.get("house_number", "")
    city_nm = (address_obj.get("city") or address_obj.get("town")
               or address_obj.get("village") or "")
    postcode = address_obj.get("postcode", "")
    country  = address_obj.get("country", "")
    osm_address = ", ".join(filter(None, [
        f"{road} {number}".strip(), city_nm, postcode, country
    ])) or None

    # ── Step 2: Overpass for richer tags if phone still missing ────────────
    if not phone and osm_id and osm_type:
        type_map = {"node": "node", "way": "way", "relation": "rel"}
        osm_el = type_map.get(osm_type)
        if osm_el:
            overpass_q = f"""
[out:json][timeout:10];
{osm_el}({osm_id});
out tags;
"""
            try:
                async with httpx.AsyncClient(timeout=15, headers=OSM_HEADERS) as client:
                    rq = await client.post(OSM_OVERPASS,
                                           data={"data": overpass_q})
                    rq.raise_for_status()
                    elements = rq.json().get("elements", [])
                if elements:
                    tags = elements[0].get("tags", {})
                    phone   = phone   or tags.get("phone") or tags.get("contact:phone")
                    website = website or tags.get("website") or tags.get("contact:website")
                    stars   = stars   or tags.get("stars")
            except Exception as e:
                log.warning("Overpass error for osm_id %s: %s", osm_id, e)

    return {
        "enrich_status":  "ok",
        "phone":          phone,
        "website":        website,
        "stars":          stars,
        "address":        osm_address or seg.meta.get("address"),
        "lat":            lat,
        "lon":            lon,
        "osm_id":         osm_id,
        "osm_type":       osm_type,
        "enrich_source":  "openstreetmap",
        "enrich_at":      datetime.utcnow().isoformat(),
    }



# Gate prefix → terminal name for airports where this is deterministic
_GATE_TO_TERMINAL = {
    "ZRH": lambda g: (
        "A" if g and g[0] in ("A",) and int("".join(filter(str.isdigit,g)) or 0) <= 59
        else "B" if g and g[0] == "B"
        else "D" if g and g[0] == "D"
        else "E" if g and g[0] == "E"
        else None
    ),
    "LHR": lambda g: (
        "2" if g and g[:2] in ("B1","B2","B3","B4","B5","C1","C2","C3","C4","C5")
        else "5" if g and g[0] == "A"
        else "3" if g and g[:2] in ("G1","G2","H1","H2")
        else None
    ),
    "FRA": lambda g: "1" if g and g[0] in ("A","B","C","Z") else "2" if g and g[0] in ("D","E") else None,
}

async def _fetch_recent_gate(flight_iata: str, airport_iata: str, is_departure: bool, adb_key: str) -> dict:
    """
    Query the last few days of the same flight to get a recent gate/terminal hint.
    Returns {"gate": ..., "terminal": ..., "hint_date": ..., "hint_status": ...} or {}.
    """
    import httpx as _hx
    from datetime import date as _d, timedelta as _td
    for delta in range(0, 5):
        day = (_d.today() - _td(days=delta)).isoformat()
        try:
            async with _hx.AsyncClient(timeout=6) as c:
                r = await c.get(
                    f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_iata}/{day}",
                    headers={"X-RapidAPI-Key": adb_key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
                )
            if r.status_code == 429:
                import asyncio as _aio; await _aio.sleep(1.2); continue
            if r.status_code not in (200,): continue
            data = r.json()
            f = (data[0] if isinstance(data, list) and data else data) or {}
            side = f.get("departure" if is_departure else "arrival", {})
            gate = side.get("gate")
            term = side.get("terminal")
            # Derive terminal from gate if not returned
            if not term and gate and airport_iata in _GATE_TO_TERMINAL:
                term = _GATE_TO_TERMINAL[airport_iata](gate)
            if gate or term:
                return {"gate": gate, "terminal": term,
                        "hint_date": day, "hint_status": f.get("status")}
        except Exception:
            continue
    return {}

def _log_api_call(service: str, endpoint: str, flight: str = None, status: str = "ok"):
    """Record an API call for usage tracking."""
    try:
        from app.database import SessionLocal as _SL
        from datetime import datetime as _dt2, timezone as _tz2
        db = _SL()
        db.execute(__import__("sqlalchemy").text(
            "INSERT INTO api_usage (service, endpoint, flight, status, called_at) "
            "VALUES (:svc, :ep, :fl, :st, :at)"
        ), {"svc": service, "ep": endpoint, "fl": flight,
            "st": status, "at": _dt2.now(_tz2.utc).isoformat()})
        db.commit(); db.close()
    except Exception:
        pass  # never let logging break enrichment


async def _enrich_flight(seg: Segment) -> dict:
    """Enrich a flight segment using AeroDataBox (primary) + AviationStack (live delays fallback)."""
    import re as _re, os as _os

    base = {k: None for k in [
        "terminal_departure","terminal_arrival","gate","gate_arrival",
        "boarding_time","baggage_claim","delay_minutes","aircraft",
        "cabin_class","fare_type","seat","baggage_allowance","ticket_number",
        "payment_card","checkin_time","checkout_time","address","phone",
        "platform_arrival","platform_departure","room_type","rate_plan",
        "nights","loyalty_points","cancellation_policy","class","coach",
        "train_number","price",
    ]}

    carrier = seg.carrier or ""
    fm = _re.search(r'\b([A-Z]{2}\d{2,4})\b', carrier)
    flight_iata = (fm.group(1) if fm else None) or (seg.meta or {}).get("flight_iata")
    if not flight_iata:
        return {**base, "enrich_status": "needs_flight_number",
                "enrich_reason": "Add a flight number (e.g. LX1742) to enable enrichment"}

    dep_date = (seg.departs_at or "")[:10]
    if not dep_date:
        return {**base, "enrich_status": "skipped", "flight_iata": flight_iata}

    # ── AeroDataBox (scheduled data for any date) ─────────────────────────────
    adb_key = _os.getenv("AERODATABOX_KEY")
    if adb_key:
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=8) as c:
                r = await c.get(
                    f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_iata}/{dep_date}",
                    headers={
                        "X-RapidAPI-Key":  adb_key,
                        "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com",
                    }
                )
            _log_api_call("aerodatabox", f"flights/number/{flight_iata}/{dep_date}",
                          flight_iata, "ok" if r.status_code == 200 else str(r.status_code))
            if r.status_code == 200:
                data = r.json()
                flights = data if isinstance(data, list) else [data]
                if flights and flights[0]:
                    f   = flights[0]
                    dep = f.get("departure", {})
                    arr = f.get("arrival",   {})
                    ac  = f.get("aircraft",  {}) or {}

                    arr_sched = arr.get("scheduledTime", {}).get("local", "")
                    arr_pred  = arr.get("predictedTime", {}).get("local", "")
                    arr_local = arr_pred or arr_sched

                    _arrives_at = None
                    _arrives_tz = (arr.get("airport") or {}).get("timeZone") or None
                    if arr_local:
                        try:
                            _arrives_at = arr_local[:16].replace(" ", "T") + ":00"
                        except Exception:
                            pass

                    delay_min = None
                    if arr_sched and arr_pred and arr_sched[:16] != arr_pred[:16]:
                        try:
                            from datetime import datetime as _dt
                            s1 = _dt.fromisoformat(arr_sched[:16].replace(" ", "T"))
                            s2 = _dt.fromisoformat(arr_pred[:16].replace(" ", "T"))
                            delay_min = int((s2 - s1).total_seconds() / 60)
                        except Exception:
                            pass

                    dep_apt  = (dep.get("airport") or {}).get("iata", "")
                    arr_apt  = (arr.get("airport") or {}).get("iata", "")
                    dep_term = dep.get("terminal")
                    dep_gate = dep.get("gate")
                    arr_term = arr.get("terminal")
                    arr_gate = arr.get("gate")

                    # If terminal missing, query recent occurrences for a hint
                    dep_hint = {}
                    arr_hint = {}
                    if not dep_term and not dep_gate and dep_apt:
                        dep_hint = await _fetch_recent_gate(flight_iata, dep_apt, True, adb_key)
                    if not arr_term and not arr_gate and arr_apt:
                        import asyncio as _aio; await _aio.sleep(1.1)
                        arr_hint = await _fetch_recent_gate(flight_iata, arr_apt, False, adb_key)
                    if dep_hint:
                        dep_term = dep_term or dep_hint.get("terminal")
                        dep_gate = dep_gate or dep_hint.get("gate")
                    if arr_hint:
                        arr_term = arr_term or arr_hint.get("terminal")
                        arr_gate = arr_gate or arr_hint.get("gate")

                    return {
                        **base,
                        "flight_iata":        flight_iata,
                        "flight_number":      flight_iata,
                        "aircraft":           ac.get("model") or ac.get("iata") or None,
                        "airline":            (f.get("airline") or {}).get("name"),
                        "terminal_departure": dep_term,
                        "terminal_arrival":   arr_term,
                        "terminal_hint":      bool(dep_hint or arr_hint),
                        "gate":               dep_gate,
                        "gate_arrival":       arr_gate,
                        "baggage_claim":      arr.get("baggageBelt"),
                        "delay_minutes":      delay_min,
                        "flight_status":      f.get("status"),
                        "distance_km":        round((f.get("greatCircleDistance") or {}).get("km", 0)) or None,
                        "last_updated":       f.get("lastUpdatedUtc"),
                        "enrich_status":      "ok",
                        "enrich_source":      "aerodatabox",
                        "enrich_at":          datetime.utcnow().isoformat(),
                        "_arrives_at":        _arrives_at,
                        "_arrives_tz":        _arrives_tz,
                    }
        except Exception as _e:
            import logging as _log
            _log.getLogger("tripdex").warning(f"AeroDataBox error for {flight_iata}/{dep_date}: {_e}")

    # ── AviationStack fallback (live/today only) ──────────────────────────────
    as_key = _os.getenv("AVIATIONSTACK_KEY")
    if not as_key:
        return {**base, "enrich_status": "schema-normalised",
                "flight_iata": flight_iata, "flight_number": flight_iata,
                "enrich_source": "schema-normalised",
                "enrich_at": datetime.utcnow().isoformat()}
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=8) as c:
            r = await c.get("http://api.aviationstack.com/v1/flights",
                params={"access_key": as_key, "flight_iata": flight_iata, "limit": 1})
        flights = r.json().get("data", [])
        if not flights:
            return {**base, "enrich_status": "schema-normalised",
                    "flight_iata": flight_iata, "flight_number": flight_iata,
                    "enrich_source": "schema-normalised",
                    "enrich_at": datetime.utcnow().isoformat()}
        f   = flights[0]
        dep = f.get("departure", {})
        arr = f.get("arrival",   {})
        return {
            **base,
            "flight_iata":        flight_iata,
            "flight_number":      flight_iata,
            "enrich_status":      "ok",
            "enrich_source":      "aviationstack-live",
            "enrich_at":          datetime.utcnow().isoformat(),
            "terminal_departure": dep.get("terminal"),
            "terminal_arrival":   arr.get("terminal"),
            "gate":               dep.get("gate"),
            "gate_arrival":       arr.get("gate"),
            "baggage_claim":      arr.get("baggage"),
            "delay_minutes":      arr.get("delay") or dep.get("delay"),
            "aircraft":           (f.get("aircraft") or {}).get("iata"),
        }
    except Exception as _e:
        import logging as _log
        _log.getLogger("tripdex").warning(f"AviationStack error for {flight_iata}: {_e}")
        return {**base, "enrich_status": "error", "flight_iata": flight_iata,
                "enrich_error": str(_e)}


async def _enrich_generic_location(seg: Segment) -> dict:
    """
    For non-transport, non-hotel segments (event/meeting/appointment/activity/other
    and any user-defined custom type): resolve the free-text location (origin) into
    a proper address via OpenStreetMap/Nominatim, so the segment can show more than
    just the name the user typed.
    """
    location = (seg.origin or "").strip()
    if not location:
        return {"enrich_status": "skipped", "enrich_reason": "no location given"}

    params = {"q": location, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    try:
        async with httpx.AsyncClient(timeout=10, headers=OSM_HEADERS) as client:
            r = await client.get(OSM_NOMINATIM, params=params)
            r.raise_for_status()
            results = r.json()
    except Exception as e:
        log.warning("Nominatim error for %s: %s", location, e)
        return {"enrich_status": "api_error", "enrich_reason": str(e)}

    if not results:
        return {"enrich_status": "no_match", "enrich_reason": f"Nominatim found nothing for: {location}"}

    best = results[0]
    return {
        "enrich_status": "ok",
        "enrich_source": "openstreetmap",
        "enrich_at":     datetime.utcnow().isoformat(),
        "address":       best.get("display_name"),
        "lat":           best.get("lat"),
        "lon":           best.get("lon"),
    }

async def _enrich_segment(seg: Segment) -> dict:
    if seg.type == "train":
        return await _enrich_train(seg)
    if seg.type == "hotel":
        return await _enrich_hotel(seg)
    if seg.type == "flight":
        return await _enrich_flight(seg)
    if seg.type in ("taxi", "car"):
        return {"enrich_status": "skipped", "enrich_reason": f"type '{seg.type}' not supported yet"}
    # event, meeting, appointment, activity, other, and any user-defined custom type
    return await _enrich_generic_location(seg)


# ── routes ─────────────────────────────────────────────────────────────────────


@router.get("/api/usage")
def get_api_usage(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Return API usage stats for the current month."""
    from sqlalchemy import text as _text
    from datetime import datetime as _dt, timezone as _tz
    month = _dt.now(_tz.utc).strftime("%Y-%m")
    rows = db.execute(_text("""
        SELECT service,
               COUNT(*) as calls,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) as ok,
               SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) as errors
        FROM api_usage
        WHERE called_at LIKE :month
        GROUP BY service
    """), {"month": f"{month}%"}).mappings().fetchall()

    limits = {"aerodatabox": 2400, "aviationstack": 500}
    result = {}
    for r in rows:
        svc = r["service"]
        calls = r["calls"]
        limit = limits.get(svc)
        result[svc] = {
            "calls_this_month": calls,
            "ok": r["ok"],
            "errors": r["errors"],
            "limit": limit,
            "pct": round(calls / limit * 100, 1) if limit else None,
            "warning": (calls / limit) >= 0.75 if limit else False,
            "critical": (calls / limit) >= 0.90 if limit else False,
        }
    return {"month": month, "services": result}

@router.post("/api/segments/{segment_id}/enrich", response_model=SegmentOut)
async def enrich_segment(segment_id: str, db: Session = Depends(get_db)):
    seg = db.query(Segment).filter(Segment.id == segment_id).first()
    if not seg:
        raise HTTPException(404, "Segment not found")

    enrichment = await _enrich_segment(seg)

    # Write back segment-column fields if segment lacks them
    if enrichment.get("_arrives_at") and not seg.arrives_at:
        try:
            from datetime import datetime as _dt2, timedelta as _td2
            arr_time = enrichment["_arrives_at"][11:16]
            dep_date = (seg.departs_at or "")[:10]
            if dep_date and arr_time:
                dep_time = (seg.departs_at or "")[11:16]
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
    # Merge into meta (strip private keys, never wipe existing keys)
    meta_enrichment = {k: v for k, v in enrichment.items() if not k.startswith("_")}
    meta = dict(seg.meta or {})
    meta.update(meta_enrichment)
    seg.meta = meta
    seg.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(seg)
    return seg


@router.post("/api/trips/{trip_id}/enrich", response_model=list[SegmentOut])
async def enrich_trip(trip_id: str, db: Session = Depends(get_db)):
    segments = (
        db.query(Segment)
        .filter(Segment.trip_id == trip_id)
        .order_by(Segment.departs_at.asc())
        .all()
    )
    if not segments:
        raise HTTPException(404, "No segments found for trip")

    for seg in segments:
        enrichment = await _enrich_segment(seg)
        meta = dict(seg.meta or {})
        meta.update(enrichment)
        seg.meta = meta
        seg.updated_at = datetime.utcnow()

    db.commit()
    for seg in segments:
        db.refresh(seg)
    return segments



# ── Connection search ─────────────────────────────────────────────────────────

TRANSPORT_API = "https://transport.opendata.ch/v1"

# Fallback booking sites by country/region
BOOKING_SITES = {
    "DE": ("DB (German Rail)", "https://www.bahn.de"),
    "IT": ("Trenitalia", "https://www.trenitalia.com"),
    "FR": ("SNCF Connect", "https://www.sncf-connect.com"),
    "ES": ("Renfe", "https://www.renfe.com"),
    "NL": ("NS", "https://www.ns.nl"),
    "AT": ("ÖBB", "https://www.oebb.at"),
    "BE": ("SNCB/NMBS", "https://www.belgiantrain.be"),
    "UK": ("National Rail", "https://www.nationalrail.co.uk"),
    "default": ("Google Maps", "https://maps.google.com"),
}

# Approx distance threshold for taxi suggestion (km)
TAXI_MAX_KM = 40


async def search_connections(
    from_station: str,
    to_station: str,
    datetime_str: str,   # "YYYY-MM-DD HH:MM"
    arrive_before: str | None = None,  # constrain by arrival time
    limit: int = 4,
) -> dict:
    """
    Search train connections via transport.opendata.ch.
    Returns {"connections": [...], "source": "...", "fallback": {...}}
    """
    # First resolve station names to IDs (handles German stations too)
    async def resolve(name):
        try:
            async with httpx.AsyncClient(timeout=6, headers={"User-Agent": "tripdex/1.0"}) as c:
                r = await c.get(f"{TRANSPORT_API}/locations",
                                params={"query": name, "type": "station"})
                stations = r.json().get("stations", [])
                return stations[0] if stations else None
        except Exception:
            return None

    # ── Try DB Vendo first for German domestic routes ──────────────────────────
    if _is_german_station(from_station) and _is_german_station(to_station):
        log.info("DB Vendo: trying German route %s → %s", from_station, to_station)
        db_results = await _search_db_vendo(
            from_station, to_station, datetime_str, arrive_before, limit
        )
        if db_results:
            return {"connections": db_results, "source": "db-vendo", "fallback": None}
        log.info("DB Vendo: no results for %s → %s, falling through to SBB", from_station, to_station)

    # ── Try ÖBB for Austrian/Italian routes ───────────────────────────────────
    if _is_austrian_or_italian_station(from_station) or _is_austrian_or_italian_station(to_station):
        log.info("ÖBB: trying Austrian/Italian route %s → %s", from_station, to_station)
        oebb_results = await _search_oebb(
            from_station, to_station, datetime_str, arrive_before, limit
        )
        if oebb_results:
            return {"connections": oebb_results, "source": "oebb", "fallback": None}
        log.info("ÖBB: no results for %s → %s, falling through to SBB", from_station, to_station)

    from_st = await resolve(from_station)
    to_st   = await resolve(to_station)

    if not from_st or not to_st:
        return {
            "connections": [],
            "source": None,
            "fallback": _build_fallback(from_station, to_station, "station_not_found")
        }

    # Parse datetime; if arrive_before given, search backwards
    params = {
        "from":     from_st["id"],
        "to":       to_st["id"],
        "limit":    limit,
    }
    if arrive_before:
        params["datetime"]    = arrive_before
        params["isArrivalTime"] = 1
    else:
        params["datetime"] = datetime_str

    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "tripdex/1.0"}) as c:
            r = await c.get(f"{TRANSPORT_API}/connections", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {
            "connections": [],
            "source": None,
            "fallback": _build_fallback(from_station, to_station, "api_error", str(e))
        }

    conns = data.get("connections", [])
    if not conns:
        return {
            "connections": [],
            "source": "transport.opendata.ch",
            "fallback": _build_fallback(from_station, to_station, "no_results",
                                        from_coord=from_st.get("coordinate"),
                                        to_coord=to_st.get("coordinate"))
        }

    results = []
    for c in conns:
        dep  = c.get("from", {})
        arr  = c.get("to", {})
        legs = c.get("sections", [])
        results.append({
            "departs":          dep.get("departure", "")[:16].replace("T", " "),
            "arrives":          arr.get("arrival",   "")[:16].replace("T", " "),
            "duration":         c.get("duration", ""),
            "platform_dep":     dep.get("platform"),
            "platform_arr":     arr.get("platform"),
            "transfers":        c.get("transfers", 0),
            "carrier":          legs[0].get("journey", {}).get("name") if legs else None,
            "from_name":        dep.get("station", {}).get("name", from_station),
            "to_name":          arr.get("station", {}).get("name", to_station),
        })

    return {"connections": results, "source": "transport.opendata.ch", "fallback": None}


def _build_fallback(from_st, to_st, reason, detail=None,
                    from_coord=None, to_coord=None):
    """Build a fallback suggestion: taxi (short distance) or booking websites."""
    # Estimate distance if coords available
    dist_km = None
    if from_coord and to_coord:
        import math
        lat1, lon1 = from_coord.get("x", 0), from_coord.get("y", 0)
        lat2, lon2 = to_coord.get("x", 0), to_coord.get("y", 0)
        # Haversine approx
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
        dist_km = round(6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

    suggestions = []

    # Taxi suggestion for short distances
    if dist_km and dist_km <= TAXI_MAX_KM:
        est_time = max(10, int(dist_km * 1.8))  # rough minutes
        est_cost = max(15, int(dist_km * 2.2))  # rough CHF/EUR
        suggestions.append({
            "type": "taxi",
            "label": f"Taxi (~{est_time} min, ~€{est_cost})",
            "note": f"About {dist_km}km — a taxi is a practical option",
            "search_url": f"https://maps.google.com/maps?saddr={urllib.parse.quote(from_st)}&daddr={urllib.parse.quote(to_st)}&dirflg=d"
        })

    # Booking website suggestions
    # Try to guess country from station name (rough heuristic)
    def guess_country(name):
        name_l = name.lower()
        if any(x in name_l for x in ["münchen","berlin","frankfurt","hamburg","köln","mannheim","frankenthal","heidelberg"]): return "DE"
        if any(x in name_l for x in ["milano","roma","venezia","firenze","napoli","torino"]): return "IT"
        if any(x in name_l for x in ["paris","lyon","marseille","bordeaux","toulouse","nice"]): return "FR"
        if any(x in name_l for x in ["amsterdam","rotterdam","utrecht","eindhoven"]): return "NL"
        if any(x in name_l for x in ["wien","graz","salzburg","innsbruck","linz"]): return "AT"
        if any(x in name_l for x in ["london","manchester","birmingham","edinburgh"]): return "UK"
        return "default"

    country = guess_country(from_st) or guess_country(to_st)
    site_name, site_url = BOOKING_SITES.get(country, BOOKING_SITES["default"])

    # Build a direct search URL where possible
    if country == "DE":
        search_url = f"https://www.bahn.de/buchung/fahrplan/suche#sts=true&so={urllib.parse.quote(from_st)}&zo={urllib.parse.quote(to_st)}"
    elif country == "IT":
        search_url = f"https://www.trenitalia.com/en.html"
    elif country == "FR":
        search_url = f"https://www.sncf-connect.com/en-en/train-tickets"
    else:
        search_url = f"https://maps.google.com/maps?saddr={urllib.parse.quote(from_st)}&daddr={urllib.parse.quote(to_st)}&dirflg=r"

    suggestions.append({
        "type": "website",
        "label": site_name,
        "url": search_url,
        "note": f"Search on {site_name} for this route"
    })

    # Always add Google Maps transit as backup
    if country != "default":
        suggestions.append({
            "type": "website",
            "label": "Google Maps (transit)",
            "url": f"https://maps.google.com/maps?saddr={urllib.parse.quote(from_st)}&daddr={urllib.parse.quote(to_st)}&dirflg=r",
            "note": "View transit options on Google Maps"
        })

    return {
        "reason": reason,
        "detail": detail,
        "suggestions": suggestions,
        "dist_km": dist_km,
    }




# ── ÖBB (Austrian Federal Railways) connection search ────────────────────────

async def _search_oebb(
    from_station: str,
    to_station: str,
    datetime_str: str,
    arrive_before: str | None = None,
    limit: int = 4,
) -> list[dict]:
    """
    Query v6.oebb.transport.rest for journeys.
    Covers Austria + cross-border routes into Germany and Italy.
    Returns same shape as _search_db_vendo / transport.opendata.ch results.
    """
    headers = {"User-Agent": "tripdex/1.0 trip.helper@emdm.ch", "Accept": "application/json"}

    async def resolve_oebb(name: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=6, headers=headers) as c:
                r = await c.get(f"{OEBB_API}/locations",
                                params={"query": name, "results": 1})
                r.raise_for_status()
                stations = r.json()
                return stations[0] if stations else None
        except Exception as e:
            log.warning("oebb location lookup failed for %s: %s", name, e)
            return None

    from_st = await resolve_oebb(from_station)
    to_st   = await resolve_oebb(to_station)
    if not from_st or not to_st:
        return []

    # Parse datetime into ISO with Vienna timezone
    try:
        import zoneinfo as _zi
        dt_naive = datetime.strptime(datetime_str[:16], "%Y-%m-%d %H:%M")
        tz_vienna = _zi.ZoneInfo("Europe/Vienna")
        dt_local  = dt_naive.replace(tzinfo=tz_vienna)
        offset    = dt_local.strftime("%z")
        offset_fmt = offset[:3] + ":" + offset[3:]
        iso_dt    = dt_naive.strftime("%Y-%m-%dT%H:%M:00") + offset_fmt
    except Exception:
        iso_dt = None

    params: dict = {
        "from":    from_st["id"],
        "to":      to_st["id"],
        "results": limit,
    }
    if arrive_before:
        try:
            arr_dt = datetime.strptime(arrive_before[:16], "%Y-%m-%d %H:%M")
            params["arrival"] = arr_dt.strftime("%Y-%m-%dT%H:%M:00.000Z")
        except ValueError:
            pass
    elif iso_dt:
        params["departure"] = iso_dt

    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as c:
            r = await c.get(f"{OEBB_API}/journeys", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("oebb journeys error (%s → %s): %s", from_station, to_station, e)
        return []

    results = []
    for journey in data.get("journeys", []):
        legs = journey.get("legs", [])
        if not legs: continue
        first = legs[0]
        last  = legs[-1]
        # Skip walk-only journeys
        if all(leg.get("walking") for leg in legs): continue

        dep_str = (first.get("departure") or first.get("plannedDeparture") or "")[:16].replace("T", " ")
        arr_str = (last.get("arrival")    or last.get("plannedArrival")    or "")[:16].replace("T", " ")
        dep_platform = first.get("departurePlatform") or first.get("plannedDeparturePlatform")
        arr_platform = last.get("arrivalPlatform")    or last.get("plannedArrivalPlatform")

        # Build leg summary
        train_legs = [l for l in legs if not l.get("walking")]
        line_names = []
        for l in train_legs:
            ln = (l.get("line") or {}).get("name")
            if ln and ln not in line_names:
                line_names.append(ln)

        transfers = max(0, len(train_legs) - 1)

        # Compute duration string (same format as DB Vendo)
        duration = ""
        if dep_str and arr_str:
            try:
                d = datetime.strptime(dep_str, "%Y-%m-%d %H:%M")
                a = datetime.strptime(arr_str, "%Y-%m-%d %H:%M")
                mins = int((a - d).total_seconds() // 60)
                duration = f"{mins // 60:02d}d{mins % 60:02d}:{00:02d}"
            except Exception:
                pass

        results.append({
            "departs":      dep_str,
            "arrives":      arr_str,
            "duration":     duration,
            "platform_dep": str(dep_platform) if dep_platform else None,
            "platform_arr": str(arr_platform) if arr_platform else None,
            "transfers":    transfers,
            "carrier":      " / ".join(line_names) if line_names else None,
            "from_name":    from_st.get("name", from_station),
            "to_name":      to_st.get("name", to_station),
            "legs":         [
                {
                    "from":      l["origin"]["name"],
                    "to":        l["destination"]["name"],
                    "departure": (l.get("departure") or l.get("plannedDeparture") or "")[:16].replace("T", " "),
                    "arrival":   (l.get("arrival")   or l.get("plannedArrival")   or "")[:16].replace("T", " "),
                    "line":      (l.get("line") or {}).get("name"),
                    "platform":  l.get("departurePlatform") or l.get("plannedDeparturePlatform"),
                }
                for l in train_legs
            ],
        })

    return results

# ── DB Vendo (Deutsche Bahn) connection search ─────────────────────────────────

DB_VENDO_API = "http://localhost:3000"

OEBB_API = "https://v6.oebb.transport.rest/api"

_AUSTRIAN_ITALIAN_KEYWORDS = [
    # Austrian cities
    "wien", "vienna", "graz", "linz", "salzburg", "innsbruck", "klagenfurt",
    "villach", "wels", "st. pölten", "bregenz", "eisenstadt", "feldkirch",
    "bruck", "leoben", "steyr", "dornbirn", "wiener neustadt", "kufstein",
    "brenner", "brennero", "schwarzach",
    # Italian cities covered by ÖBB
    "venezia", "venice", "verona", "milano", "milan", "bologna", "firenze",
    "florence", "roma", "rome", "napoli", "naples", "torino", "turin",
    "genova", "genoa", "trieste", "udine", "padova", "padua", "vicenza",
    "trento", "trient", "bolzano", "bozen", "merano", "meran",
]

def _is_austrian_or_italian_station(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in _AUSTRIAN_ITALIAN_KEYWORDS)

_GERMAN_KEYWORDS = [
    "hbf", "bahnhof", "münchen", "berlin", "frankfurt", "hamburg", "köln",
    "mannheim", "frankenthal", "heidelberg", "dortmund", "düsseldorf",
    "stuttgart", "nürnberg", "leipzig", "dresden", "hannover", "bremen",
    "karlsruhe", "augsburg", "wiesbaden", "mainz", "freiburg", "aachen",
    "koblenz", "saarbrücken", "magdeburg", "erfurt", "rostock", "kassel",
    "ludwigshafen", "würzburg", "ulm", "bonn", "münster", "bielefeld",
]

def _is_german_station(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in _GERMAN_KEYWORDS)


async def _search_db_vendo(
    from_station: str,
    to_station: str,
    datetime_str: str,
    arrive_before: str | None = None,
    limit: int = 4,
) -> list[dict]:
    """
    Query the self-hosted db-vendo-client container for journeys.
    Returns a list of connection dicts (same shape as transport.opendata.ch results)
    or an empty list on any error.
    """
    headers = {"User-Agent": "tripdex/1.0", "Accept": "application/json"}

    async def resolve_db(name: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=6, headers=headers) as c:
                r = await c.get(f"{DB_VENDO_API}/locations",
                                params={"query": name, "results": 1})
                r.raise_for_status()
                stations = r.json()
                return stations[0] if stations else None
        except Exception as e:
            log.warning("db-vendo location lookup failed for %s: %s", name, e)
            return None

    from_st = await resolve_db(from_station)
    to_st   = await resolve_db(to_station)
    if not from_st or not to_st:
        return []

    # Parse datetime
    try:
        import zoneinfo as _zi
        dt_naive = datetime.strptime(datetime_str[:16], "%Y-%m-%d %H:%M")
        # Attach Europe/Berlin tz so db-vendo receives a proper local-time ISO string
        tz_berlin = _zi.ZoneInfo("Europe/Berlin")
        dt_local  = dt_naive.replace(tzinfo=tz_berlin)
        offset    = dt_local.strftime("%z")          # e.g. "+0200"
        offset_fmt = offset[:3] + ":" + offset[3:]   # "+02:00"
        iso_dt    = dt_naive.strftime("%Y-%m-%dT%H:%M:00") + offset_fmt
    except Exception:
        iso_dt = None

    params: dict = {
        "from":    from_st["id"],
        "to":      to_st["id"],
        "results": limit,
    }
    if arrive_before:
        try:
            arr_dt = datetime.strptime(arrive_before[:16], "%Y-%m-%d %H:%M")
            params["arrival"]         = arr_dt.strftime("%Y-%m-%dT%H:%M:00.000Z")
        except ValueError:
            pass
    elif iso_dt:
        params["departure"] = iso_dt

    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as c:
            r = await c.get(f"{DB_VENDO_API}/journeys", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("db-vendo journeys error (%s → %s): %s", from_station, to_station, e)
        return []

    journeys = data.get("journeys", [])
    if not journeys:
        return []

    results = []
    for j in journeys:
        legs = j.get("legs", [])
        if not legs:
            continue
        first_leg = legs[0]
        last_leg  = legs[-1]

        dep_place = first_leg.get("origin", {})
        arr_place = last_leg.get("destination", {})
        dep_time  = first_leg.get("plannedDeparture") or first_leg.get("departure", "")
        arr_time  = last_leg.get("plannedArrival")    or last_leg.get("arrival", "")
        dep_plat  = first_leg.get("departurePlatform")
        arr_plat  = last_leg.get("arrivalPlatform")

        # Duration
        duration = ""
        if dep_time and arr_time:
            try:
                d = datetime.fromisoformat(dep_time.replace("Z", "+00:00"))
                a = datetime.fromisoformat(arr_time.replace("Z", "+00:00"))
                mins = int((a - d).total_seconds() // 60)
                duration = f"{mins // 60:02d}d{mins % 60:02d}:{00:02d}"
            except Exception:
                pass

        # First train/product name
        carrier = None
        for leg in legs:
            line = leg.get("line", {})
            if line:
                carrier = (line.get("name") or
                           f"{line.get('product','').upper()} {line.get('fahrtNr','')}".strip())
                break

        transfers = max(0, len([l for l in legs
                                 if l.get("line") or l.get("walking") is False]) - 1)

        results.append({
            "departs":      dep_time[:16].replace("T", " ") if dep_time else "",
            "arrives":      arr_time[:16].replace("T", " ") if arr_time else "",
            "duration":     duration,
            "platform_dep": str(dep_plat) if dep_plat else None,
            "platform_arr": str(arr_plat) if arr_plat else None,
            "transfers":    transfers,
            "carrier":      carrier,
            "from_name":    dep_place.get("name", from_station),
            "to_name":      arr_place.get("name", to_station),
        })

    return results
