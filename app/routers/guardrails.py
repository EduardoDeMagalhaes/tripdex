"""
guardrails.py — Pre-GPT checks for the Tripdex dialog endpoint.

Each guardrail is a function with signature:
    check_*(message, draft, trip, all_trips) -> GuardrailHit | None

run_guardrails() calls them in order and returns the first hit, or None.
New guardrails can be added here without touching parse.py.
"""

from __future__ import annotations
import re
from datetime import date, datetime
from dataclasses import dataclass, field
from typing import Optional, Any


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class GuardrailHit:
    """
    Returned when a guardrail fires.
    `code`    — machine-readable identifier (used by frontend to pick UI)
    `message` — human-readable warning shown to the user
    `options` — list of action labels the frontend can offer as buttons
    `meta`    — arbitrary extra data (e.g. suggested_trip_id)
    """
    code: str
    message: str
    options: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# ── Date extraction helpers ──────────────────────────────────────────────────

# Patterns we try to extract a date from free text, in priority order.
# We intentionally keep this simple: exact ISO dates and "Month Day" phrases.
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

def _parse_date_from_text(text: str, reference_year: int) -> Optional[date]:
    """
    Try to extract the first recognisable date from free text.
    Returns a date object or None.
    """
    t = text.lower()

    # ISO: 2026-08-09 or 2026/08/09
    m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # "August 9th", "9 August", "aug 9", "9th aug", etc.
    m = re.search(
        r'(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})|([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?',
        t,
    )
    if m:
        if m.group(1):   # day first
            day_s, mon_s = m.group(1), m.group(2)[:3]
        else:            # month first
            day_s, mon_s = m.group(4), m.group(3)[:3]
        month = _MONTH_MAP.get(mon_s)
        if month:
            try:
                return date(reference_year, month, int(day_s))
            except ValueError:
                pass

    return None


def _date_from_draft(draft: Optional[dict]) -> Optional[date]:
    """Pull departs_at from the current draft if present."""
    if not draft:
        return None
    dt_str = draft.get("departs_at") or ""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str[:10]).date()
    except ValueError:
        return None


def _trip_date(trip, attr: str) -> Optional[date]:
    val = getattr(trip, attr, None)
    if not val:
        return None
    if isinstance(val, date):
        return val
    try:
        return datetime.fromisoformat(str(val)[:10]).date()
    except ValueError:
        return None


# ── Guardrail 1: segment date outside trip boundaries ───────────────────────

def check_date_out_of_range(
    message: str,
    draft: Optional[dict],
    trip,
    all_trips: list[dict],
) -> Optional[GuardrailHit]:
    """
    Fires when the user asks to add a segment whose date falls outside the
    current trip's start/end date range.
    """
    trip_start = _trip_date(trip, "start_date")
    trip_end   = _trip_date(trip, "end_date")
    if not trip_start or not trip_end:
        return None   # trip has no dates — can't enforce

    ref_year = trip_start.year
    seg_date = _date_from_draft(draft) or _parse_date_from_text(message, ref_year)
    if not seg_date:
        return None   # no date found — GPT will ask

    if trip_start <= seg_date <= trip_end:
        return None   # within range — all good

    # ── date is outside range — build a helpful message ──────────────────
    # Look for another trip that covers this date
    suggested: Optional[dict] = None
    for t in all_trips:
        if t.get("isCurrent"):
            continue
        try:
            t_start = datetime.fromisoformat(str(t.get("start_date", ""))[:10]).date()
            t_end   = datetime.fromisoformat(str(t.get("end_date",   ""))[:10]).date()
            if t_start <= seg_date <= t_end:
                suggested = t
                break
        except ValueError:
            continue

    date_str  = seg_date.strftime("%-d %B %Y")
    range_str = f"{trip_start.strftime('%-d %b')} – {trip_end.strftime('%-d %b %Y')}"

    if suggested:
        msg = (
            f"{date_str} is outside this trip ({range_str}). "
            f"It looks like it belongs to \"{suggested['name']}\" instead."
        )
        options = [
            f"Move to \"{suggested['name']}\"",
            "Extend this trip's dates",
            "Add anyway",
            "Cancel",
        ]
        meta = {"suggested_trip_id": suggested.get("id"), "suggested_trip_name": suggested["name"]}
    else:
        msg = (
            f"{date_str} is outside this trip ({range_str}). "
            f"Would you like to extend the trip dates or create a new trip for this segment?"
        )
        options = ["Extend this trip's dates", "Create new trip", "Add anyway", "Cancel"]
        meta = {}

    return GuardrailHit(
        code="DATE_OUT_OF_RANGE",
        message=msg,
        options=options,
        meta={"segment_date": seg_date.isoformat(), **meta},
    )


# ── Registry — add new guardrails here ──────────────────────────────────────

_GUARDRAILS = [
    check_date_out_of_range,
    # check_duplicate_flight,   ← future
    # check_hotel_overlap,      ← future
    # check_route_relevance,    ← future (GPT-assisted, more complex)
]


def run_guardrails(
    message: str,
    draft: Optional[dict],
    trip,
    all_trips: list[dict],
) -> Optional[GuardrailHit]:
    """
    Run all registered guardrails in order.
    Returns the first hit, or None if everything is fine.
    """
    for check in _GUARDRAILS:
        hit = check(message, draft, trip, all_trips)
        if hit:
            return hit
    return None
