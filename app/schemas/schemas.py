from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class SegmentBase(BaseModel):
    type: str = "other"
    departs_at: Optional[str] = None
    departs_tz: Optional[str] = None
    arrives_at: Optional[str] = None
    arrives_tz: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    carrier: Optional[str] = None
    confirmation_ref: Optional[str] = None
    confirmed: bool = False
    parse_status: str = "ok"
    meta: Optional[dict] = {}
    next_segment_id: Optional[str] = None

class SegmentCreate(SegmentBase):
    trip_id: str
    raw_email_id: Optional[str] = None

class SegmentUpdate(SegmentBase):
    trip_id: Optional[str] = None

class SegmentOut(SegmentBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    trip_id: Optional[str] = None
    raw_email_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class CustomSegmentTypeCreate(BaseModel):
    label: str
    icon: Optional[str] = "📌"

class CustomSegmentTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    key: str
    label: str
    icon: str

class TripBase(BaseModel):
    name: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    home_currency: str = "CHF"
    location: Optional[str] = None
    description: Optional[str] = None

class TripCreate(TripBase):
    pass

class TripUpdate(BaseModel):
    name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    home_currency: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None

class TripListOut(TripBase):
    """Lightweight trip summary for list endpoint — no segments embedded."""
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    calendar_token: Optional[str] = None

class TripOut(TripBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    calendar_token: Optional[str] = None
    segments: list[SegmentOut] = []

class RawEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    trip_id: Optional[str] = None
    message_id: Optional[str] = None
    from_address: Optional[str] = None
    subject: Optional[str] = None
    has_pdf: bool
    parse_status: str
    received_at: datetime
