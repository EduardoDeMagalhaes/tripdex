"""
feedback.py — Bug reports and feature requests.
POST /api/feedback — submit (auth optional, screenshot optional)
"""
from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timezone
from typing import Optional
import uuid, os, base64

from app.database import get_db
from app.routers.email_templates import send_email, FROM_EMAIL

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "eduardo@emdm.ch")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://waypoint.emdm.ch")

# Try to get current user softly — don't 401 if not logged in
def _get_user_soft(request: Request, db: Session):
    try:
        from app.routers.auth import COOKIE_NAME, decode_session
        token = request.cookies.get(COOKIE_NAME)
        if not token: return None
        user_id = decode_session(token)
        if not user_id: return None
        row = db.execute(text("SELECT id, username, email FROM users WHERE id=:id"),
                         {"id": user_id}).mappings().fetchone()
        return dict(row) if row else None
    except Exception:
        return None


@router.post("")
async def submit_feedback(
    request: Request,
    type: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    context: str = Form(""),
    screenshot: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    user = _get_user_soft(request, db)
    now = datetime.now(timezone.utc).isoformat()
    fb_id = str(uuid.uuid4())

    # Generate sequential WPU-### id
    last = db.execute(text(
        "SELECT short_id FROM feedback WHERE short_id IS NOT NULL ORDER BY short_id DESC LIMIT 1"
    )).scalar()
    next_num = (int(last.split('-')[1]) + 1) if last else 1
    short_id = f"WPU-{next_num:03d}"


    # Handle screenshot
    screenshot_b64 = None
    screenshot_mime = None
    if screenshot and screenshot.filename:
        data = await screenshot.read()
        if len(data) <= 5 * 1024 * 1024:  # 5MB cap
            screenshot_b64 = base64.b64encode(data).decode()
            screenshot_mime = screenshot.content_type or "image/png"

    db.execute(text("""
        INSERT INTO feedback (id, short_id, user_id, username, type, title, description, context, screenshot, status, created_at)
        VALUES (:id, :short_id, :uid, :uname, :type, :title, :desc, :ctx, :ss, 'open', :now)
    """), {
        "id":       fb_id,
        "short_id": short_id,
        "uid":      user["id"] if user else None,
        "uname":    user["username"] if user else "anonymous",
        "type":     type,
        "title":    title,
        "desc":     description,
        "ctx":      context,
        "ss":       screenshot_b64,
        "now":      now,
    })
    db.commit()

    # Send email notification
    _send_feedback_email(fb_id, short_id, type, title, description, context,
                         user, screenshot_b64, screenshot_mime, now)

    return {"ok": True, "id": fb_id, "short_id": short_id}


def _send_feedback_email(fb_id, short_id, fb_type, title, description, context,
                          user, screenshot_b64, screenshot_mime, created_at):
    type_label  = "🐛 Bug Report" if fb_type == "bug" else "✨ Feature Request"
    type_color  = "#c0522a" if fb_type == "bug" else "#6b7f5e"
    user_line   = f"{user['username']} ({user['email']})" if user else "Anonymous"
    desc_html   = description.replace("\n", "<br>") if description else "<em>No description</em>"
    ctx_html    = f"<code style='font-size:12px;color:#5a504a'>{context}</code>" if context else "—"
    ss_html     = ""
    if screenshot_b64 and screenshot_mime:
        ss_html = (
            f'<div style="margin-top:16px">'
            f'<p style="font-size:12px;font-weight:700;color:#8a847c;text-transform:uppercase;'
            f'letter-spacing:.05em;margin:0 0 8px">Screenshot</p>'
            f'<img src="data:{screenshot_mime};base64,{screenshot_b64}" '
            f'style="max-width:100%;border-radius:8px;border:1px solid #e0d8cc"/></div>'
        )

    body_html = f"""
<p style="margin:0 0 20px">
  <span style="display:inline-block;padding:4px 12px;border-radius:20px;
    background:{type_color};color:#fff;font-size:13px;font-weight:600">{type_label}</span>
</p>
<table style="width:100%;border-collapse:collapse;font-size:14px;color:#4a4540">
  <tr><td style="padding:8px 0;border-bottom:1px solid #e0d8cc;font-weight:600;width:120px">ID</td>
      <td style="padding:8px 0;border-bottom:1px solid #e0d8cc"><strong>{short_id}</strong></td></tr>
  <tr><td style="padding:8px 0;border-bottom:1px solid #e0d8cc;font-weight:600;width:120px">Title</td>
      <td style="padding:8px 0;border-bottom:1px solid #e0d8cc">{title}</td></tr>
  <tr><td style="padding:8px 0;border-bottom:1px solid #e0d8cc;font-weight:600">From</td>
      <td style="padding:8px 0;border-bottom:1px solid #e0d8cc">{user_line}</td></tr>
  <tr><td style="padding:8px 0;border-bottom:1px solid #e0d8cc;font-weight:600">Page</td>
      <td style="padding:8px 0;border-bottom:1px solid #e0d8cc">{ctx_html}</td></tr>
  <tr><td style="padding:8px 12px 0 0;font-weight:600;vertical-align:top">Description</td>
      <td style="padding:8px 0 0">{desc_html}</td></tr>
</table>
{ss_html}
<p style="margin:24px 0 0;font-size:11px;color:#8a847c">ID: {fb_id} · {created_at[:19].replace('T',' ')} UTC</p>
"""
    plain = (
        f"{type_label}\n\n"
        f"ID: {short_id}\nTitle: {title}\nFrom: {user_line}\nPage: {context or '—'}\n\n"
        f"{description or 'No description'}\n\n"
        f"--- TRIAGE WITH CLAUDE ---\n"
        f"Paste this into a new chat:\n\n"
        f"I have a Tripdex feedback report I'd like to triage with you.\n"
        f"Report ID: {short_id}\n\n"
        f"Please:\n"
        f"1. Connect to emdm.ch via SSH MCP\n"
        f"2. Pull the report: SELECT * FROM feedback WHERE short_id='{short_id}' in /home/eduardo/tripdex/tripdex.db\n"
        f"3. Read the backlog at /home/eduardo/tripdex/BACKLOG.md\n"
        f"4. Compare and recommend: ignore / amend / new backlog item + priority"
    )

    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from app.routers.email_templates import _email_template

    triage_prompt = (
        f"I have a Tripdex feedback report I'd like to triage with you.\n\n"
        f"Report ID: **{short_id}**\n\n"
        f"Please:\n"
        f"1. Connect to the emdm.ch server via SSH MCP\n"
        f"2. Run: `python3 -c \"import sqlite3; con=sqlite3.connect('/home/eduardo/tripdex/tripdex.db'); "
        f"row=con.execute(\'SELECT short_id,type,title,description,context,status,created_at,username FROM feedback WHERE short_id=\'\'{short_id}\'\'\').fetchone(); "
        f"print(row); con.close()\"` \n"
        f"3. Read the current backlog at `/home/eduardo/tripdex/BACKLOG.md`\n"
        f"4. Compare the report to the backlog and tell me:\n"
        f"   - Is this a known issue or duplicate?\n"
        f"   - Suggested priority (low / medium / high / critical)\n"
        f"   - Recommendation: ignore / amend existing backlog item / create new backlog item\n\n"
        f"Ready when you are."
    )

    triage_block = f"""
<hr style="margin:32px 0;border:none;border-top:1px solid #e0d8cc;">
<p style="margin:0 0 10px;font-size:12px;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:#8a847c">Triage with Claude — copy &amp; paste to new chat</p>
<div style="background:#2c2825;border-radius:8px;padding:16px 18px;font-family:monospace;
  font-size:12px;color:#f5f0e8;white-space:pre-wrap;word-break:break-word;line-height:1.6">{triage_prompt}</div>
"""

    html = _email_template(
        heading=f"{short_id} — {title}",
        body_html=body_html + triage_block,
        cta_url=FRONTEND_URL,
        cta_label="Open Tripdex",
        footnote="This is an automated notification from Tripdex feedback."
    )
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Tripdex] {short_id} {type_label}: {title}"
        msg["From"]    = f"Tripdex <{FROM_EMAIL}>"
        msg["To"]      = ADMIN_EMAIL
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("localhost") as s:
            s.sendmail(FROM_EMAIL, [ADMIN_EMAIL], msg.as_string())
    except Exception as e:
        import logging
        logging.getLogger("tripdex").warning(f"Could not send feedback email: {e}")
