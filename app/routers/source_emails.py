"""
source_emails.py — Manage per-user source email addresses.

Endpoints:
  GET    /api/source-emails          → list my source emails
  POST   /api/source-emails          → add a new source email (sends verification)
  DELETE /api/source-emails/{id}     → remove a source email
  POST   /api/source-emails/{id}/resend → resend verification email
  GET    /api/source-emails/verify   → verify token (no auth — token is auth)
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel, EmailStr
from datetime import datetime, timezone, timedelta
import uuid, os

from app.database import get_db
from app.routers.deps import get_current_user
from app.models.models import UserSourceEmail
from app.routers.email_templates import send_email, _email_template, FRONTEND_URL, FROM_EMAIL

router = APIRouter(prefix="/api/source-emails", tags=["source-emails"])

TOKEN_TTL_HOURS   = 48
RESEND_COOLDOWN_M = 60    # 1 hour between resends per address
RESEND_DAILY_MAX  = 20    # global cap across all source-email verifications


def _now():
    return datetime.now(timezone.utc)


def _expiry_cutoff_for_issued_within(delta: timedelta) -> str:
    """email_tokens has no created_at column — only expires_at, with a fixed
    TOKEN_TTL_HOURS lifetime. So 'issued within `delta`' == 'expires_at is still
    at least (TOKEN_TTL_HOURS - delta) away', i.e. expires_at > now + TTL - delta.
    Used instead of a schema migration for the rate-limit checks below."""
    return (_now() + timedelta(hours=TOKEN_TTL_HOURS) - delta).isoformat()


def _issue_token(db: Session, user_id: str, source_email_id: str) -> str:
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    expires = (_now() + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
    db.execute(text(
        "INSERT INTO email_tokens (id, user_id, token, type, expires_at, meta) "
        "VALUES (:id, :uid, :tok, 'verify_source', :exp, :meta)"
    ), {
        "id":   str(uuid.uuid4()),
        "uid":  user_id,
        "tok":  tok,
        "exp":  expires,
        "meta": source_email_id,
    })
    return tok


def _send_verification(to: str, token: str):
    link = f"{FRONTEND_URL}/api/source-emails/verify?token={token}"
    subject = "Confirm your Waypoint source email"
    text_body = (
        f"Hi,\n\n"
        f"Someone requested to add {to} as a source email on a Waypoint account.\n\n"
        f"Click the link below to confirm (expires in {TOKEN_TTL_HOURS} hours):\n{link}\n\n"
        f"If you didn't request this, ignore this email.\n\n— Waypoint"
    )
    body_html = (
        f"<p style=\"margin:0 0 12px;font-size:15px;color:#4a4540;line-height:1.7;\">"
        f"Someone requested to add <strong style=\"color:#1a1814\">{to}</strong> as a source "
        f"email address on a Waypoint account."
        f"</p>"
        f"<p style=\"margin:0;font-size:15px;color:#4a4540;line-height:1.7;\">"
        f"Click below to confirm. The link expires in {TOKEN_TTL_HOURS} hours."
        f"</p>"
    )
    html = _email_template(
        heading="Confirm source email",
        body_html=body_html,
        cta_url=link,
        cta_label="Confirm this email address",
        footnote="If you didn't request this, you can safely ignore it. Your inbox will not receive further emails.",
    )
    send_email(to, subject, html, text_body)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_resend_limits(db: Session, source_email_id: str):
    """Raise 429 if cooldown or daily cap exceeded."""
    cutoff_hour  = _expiry_cutoff_for_issued_within(timedelta(minutes=RESEND_COOLDOWN_M))
    cutoff_day   = _expiry_cutoff_for_issued_within(timedelta(hours=24))

    # Per-address cooldown
    recent = db.execute(text(
        "SELECT COUNT(*) FROM email_tokens "
        "WHERE meta=:sid AND type='verify_source' AND expires_at > :cutoff"
    ), {"sid": source_email_id, "cutoff": cutoff_hour}).scalar()
    if recent and recent > 0:
        raise HTTPException(429, f"Please wait {RESEND_COOLDOWN_M} minutes before resending.")

    # Global daily cap (all verify_source tokens)
    daily = db.execute(text(
        "SELECT COUNT(*) FROM email_tokens "
        "WHERE type='verify_source' AND expires_at > :cutoff"
    ), {"cutoff": cutoff_day}).scalar()
    if daily and daily >= RESEND_DAILY_MAX:
        raise HTTPException(429, "Daily verification limit reached. Try again tomorrow.")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
def list_source_emails(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = db.execute(text(
        "SELECT id, email, status, created_at, verified_at "
        "FROM user_source_emails WHERE user_id=:uid ORDER BY created_at ASC"
    ), {"uid": user["id"]}).mappings().fetchall()
    return [dict(r) for r in rows]


class AddSourceEmailBody(BaseModel):
    email: EmailStr


@router.post("")
def add_source_email(body: AddSourceEmailBody, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    addr = body.email.lower().strip()

    # It's the caller's own account email — safe to say so specifically (unlike the
    # generic cases below, this can't leak whether some OTHER email is registered).
    # It's also not a mistake to try: this address already works for ingest via the
    # primary-email check in lookup_user_by_email(), it just never needed a row here.
    if addr == (user.get("email") or "").lower():
        raise HTTPException(409, "That's your account's primary email — bookings from it are already routed here automatically, no need to add it separately.")

    # Block if already confirmed anywhere
    existing_confirmed = db.execute(text(
        "SELECT id FROM user_source_emails WHERE LOWER(email)=:email AND status='confirmed'"
    ), {"email": addr}).fetchone()
    if existing_confirmed:
        raise HTTPException(409, "This email address is already linked to a Waypoint account.")

    # Block if it's someone's primary account email (confirmed)
    primary = db.execute(text(
        "SELECT id FROM users WHERE LOWER(email)=:email AND is_verified=1"
    ), {"email": addr}).fetchone()
    if primary:
        raise HTTPException(409, "This email address is already linked to a Waypoint account.")

    # Block duplicate pending for THIS user
    existing_mine = db.execute(text(
        "SELECT id FROM user_source_emails WHERE LOWER(email)=:email AND user_id=:uid"
    ), {"email": addr, "uid": user["id"]}).fetchone()
    if existing_mine:
        raise HTTPException(409, "You already have this email address (pending or confirmed).")

    # Check global daily cap before creating
    cutoff_day = _expiry_cutoff_for_issued_within(timedelta(hours=24))
    daily = db.execute(text(
        "SELECT COUNT(*) FROM email_tokens WHERE type='verify_source' AND expires_at > :cutoff"
    ), {"cutoff": cutoff_day}).scalar()
    if daily and daily >= RESEND_DAILY_MAX:
        raise HTTPException(429, "Daily verification limit reached. Try again tomorrow.")

    # Create record
    se_id = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO user_source_emails (id, user_id, email, status, created_at) "
        "VALUES (:id, :uid, :email, 'pending', :now)"
    ), {"id": se_id, "uid": user["id"], "email": addr, "now": _now().isoformat()})
    db.flush()

    tok = _issue_token(db, user["id"], se_id)
    db.commit()

    try:
        _send_verification(addr, tok)
    except Exception as e:
        import logging; logging.getLogger("waypoint").warning(f"Could not send source email verification to {addr}: {e}")

    return {"ok": True, "id": se_id, "email": addr, "status": "pending"}


@router.delete("/{se_id}")
def remove_source_email(se_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    row = db.execute(text(
        "SELECT id FROM user_source_emails WHERE id=:id AND user_id=:uid"
    ), {"id": se_id, "uid": user["id"]}).fetchone()
    if not row:
        raise HTTPException(404, "Source email not found.")
    db.execute(text("DELETE FROM user_source_emails WHERE id=:id"), {"id": se_id})
    db.commit()
    return {"ok": True}


@router.post("/{se_id}/resend")
def resend_verification(se_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    row = db.execute(text(
        "SELECT id, email, status FROM user_source_emails WHERE id=:id AND user_id=:uid"
    ), {"id": se_id, "uid": user["id"]}).mappings().fetchone()
    if not row:
        raise HTTPException(404, "Source email not found.")
    if row["status"] == "confirmed":
        raise HTTPException(400, "This email is already confirmed.")

    _check_resend_limits(db, se_id)

    tok = _issue_token(db, user["id"], se_id)
    db.commit()

    try:
        _send_verification(row["email"], tok)
    except Exception as e:
        import logging; logging.getLogger("waypoint").warning(f"Resend failed for {row['email']}: {e}")
        raise HTTPException(500, "Could not send verification email.")

    return {"ok": True}


@router.get("/verify", response_class=HTMLResponse)
def verify_source_email(token: str, db: Session = Depends(get_db)):
    """Token-gated — no session required."""
    row = db.execute(text(
        "SELECT * FROM email_tokens WHERE token=:tok AND type='verify_source' AND used_at IS NULL"
    ), {"tok": token}).mappings().fetchone()

    def _page(title, msg, success=True):
        color = "#2e7d32" if success else "#c62828"
        return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:Georgia,serif;background:#f5f0e8;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0}}
.card{{background:#fff;border-radius:12px;padding:40px 32px;max-width:380px;text-align:center;
box-shadow:0 2px 16px rgba(0,0,0,.08)}}
h2{{margin:0 0 12px;font-size:22px;color:#2c2825}} p{{color:#6b635a;line-height:1.6;margin:0 0 24px}}
a{{display:inline-block;background:#b5651d;color:#fff;padding:12px 24px;border-radius:8px;
text-decoration:none;font-size:15px}}</style></head>
<body><div class="card"><div style="font-size:32px;margin-bottom:16px">✦</div>
<h2 style="color:{color}">{title}</h2><p>{msg}</p>
<a href="{FRONTEND_URL}">Open Waypoint</a></div></body></html>"""

    if not row:
        return _page("Invalid or expired link",
                     "This verification link has already been used or has expired.", success=False)

    # Check expiry
    try:
        exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if _now() > exp:
            return _page("Link expired",
                         f"This link expired after {TOKEN_TTL_HOURS} hours. Request a new one from your Profile page.", success=False)
    except Exception:
        pass

    se_id = row["meta"]
    se = db.execute(text(
        "SELECT * FROM user_source_emails WHERE id=:id"
    ), {"id": se_id}).mappings().fetchone()

    if not se:
        return _page("Not found", "This source email entry no longer exists.", success=False)

    if se["status"] == "confirmed":
        return _page("Already confirmed", "This email address is already confirmed on your account.", success=True)

    now = _now().isoformat()
    db.execute(text(
        "UPDATE user_source_emails SET status='confirmed', verified_at=:now WHERE id=:id"
    ), {"now": now, "id": se_id})
    db.execute(text(
        "UPDATE email_tokens SET used_at=:now WHERE token=:tok"
    ), {"now": now, "tok": token})
    db.commit()

    email_addr = se["email"]
    return _page("Email confirmed ✓",
                 f"{email_addr} has been added to your Waypoint account. You can now forward travel emails from this address.")
