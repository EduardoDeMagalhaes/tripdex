"""
custom_types.py — /api/user/segment-types
User-defined custom segment types, saved to the user's profile and reusable across trips.
"""
import re, uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import CustomSegmentType
from app.schemas.schemas import CustomSegmentTypeCreate, CustomSegmentTypeOut
from app.routers.deps import get_current_user

router = APIRouter(prefix="/api/user/segment-types", tags=["custom-types"])

def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or uuid.uuid4().hex[:8]

@router.get("", response_model=list[CustomSegmentTypeOut])
def list_custom_types(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return (db.query(CustomSegmentType)
              .filter(CustomSegmentType.user_id == user["id"])
              .order_by(CustomSegmentType.created_at)
              .all())

@router.post("", response_model=CustomSegmentTypeOut, status_code=201)
def create_custom_type(body: CustomSegmentTypeCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "Label is required")
    if len(label) > 40:
        raise HTTPException(400, "Label too long")
    base_key = _slugify(label)
    key = base_key
    # Ensure uniqueness per user (in case of duplicate labels)
    i = 2
    while db.query(CustomSegmentType).filter(
        CustomSegmentType.user_id == user["id"], CustomSegmentType.key == key
    ).first():
        existing = db.query(CustomSegmentType).filter(
            CustomSegmentType.user_id == user["id"], CustomSegmentType.key == base_key
        ).first()
        if existing and existing.label.lower() == label.lower():
            return existing
        key = f"{base_key}_{i}"
        i += 1
    icon = (body.icon or "\U0001F4CC").strip()[:8] or "\U0001F4CC"
    ct = CustomSegmentType(user_id=user["id"], key=key, label=label, icon=icon)
    db.add(ct); db.commit(); db.refresh(ct)
    return ct

@router.delete("/{type_id}", status_code=204)
def delete_custom_type(type_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    ct = db.query(CustomSegmentType).filter(
        CustomSegmentType.id == type_id, CustomSegmentType.user_id == user["id"]
    ).first()
    if not ct:
        raise HTTPException(404, "Not found")
    db.delete(ct); db.commit()
    return None
