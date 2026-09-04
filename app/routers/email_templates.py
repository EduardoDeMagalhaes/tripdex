"""
email_templates.py — All branded email sending functions for Tripdex.
Standalone — no circular imports. Imported by auth.py and emails.py.
"""
import os, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

FROM_EMAIL   = os.getenv("FROM_EMAIL", "trip.helper@emdm.ch")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://waypoint.emdm.ch")


def send_email(to: str, subject: str, html: str, text: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Tripdex <{FROM_EMAIL}>"
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("localhost") as s:
        s.sendmail(FROM_EMAIL, [to], msg.as_string())


def _email_template(heading: str, body_html: str, cta_url: str, cta_label: str, footnote: str) -> str:
    """Branded HTML email — Tripdex visual identity."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading}</title></head>
<body style="margin:0;padding:0;background:#ede7da;font-family:'DM Sans',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#ede7da;padding:40px 16px;">
<tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">
  <tr><td style="padding-bottom:24px;text-align:center;">
    <span style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#1a1814;letter-spacing:.5px;">
      &#10022; Tripdex
    </span>
  </td></tr>
  <tr><td style="background:#f5f0e8;border-radius:12px;padding:40px 36px;box-shadow:0 2px 8px rgba(0,0,0,.08);">
    <h1 style="margin:0 0 16px;font-family:Georgia,serif;font-size:26px;font-weight:700;color:#1a1814;line-height:1.2;">
      {heading}
    </h1>
    {body_html}
    <table cellpadding="0" cellspacing="0" style="margin:32px 0 0;">
      <tr><td style="background:#c4611a;border-radius:8px;">
        <a href="{cta_url}" style="display:inline-block;padding:14px 32px;color:#fff;
           font-family:'DM Sans',Arial,sans-serif;font-size:15px;font-weight:600;
           text-decoration:none;letter-spacing:.2px;">{cta_label}</a>
      </td></tr>
    </table>
    <p style="margin:20px 0 0;font-size:12px;color:#8a847c;word-break:break-all;">
      Or copy this link: <a href="{cta_url}" style="color:#c4611a;">{cta_url}</a>
    </p>
    <hr style="margin:32px 0;border:none;border-top:1px solid #e0d8cc;">
    <p style="margin:0;font-size:12px;color:#8a847c;line-height:1.6;">{footnote}</p>
  </td></tr>
  <tr><td style="padding-top:24px;text-align:center;">
    <p style="margin:0;font-size:11px;color:#c4bdb4;">Tripdex &middot; trip.helper@emdm.ch</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def send_verification_email(to: str, username: str, token: str):
    link = f"{FRONTEND_URL}/api/auth/verify-email?token={token}"
    subject = "Confirm your Tripdex account"
    text = (
        f"Hi {username},\n\n"
        f"Please confirm your email address to activate your Tripdex account:\n{link}\n\n"
        f"This link expires in 24 hours. If you didn't register, you can safely ignore this email.\n\n"
        f"— Tripdex"
    )
    body = (
        f"<p style=\"margin:0 0 16px;font-size:16px;color:#4a4540;line-height:1.6;\">"
        f"  Hi <strong style=\"color:#1a1814\">{username}</strong>,"
        f"</p>"
        "<p style=\"margin:0;font-size:15px;color:#4a4540;line-height:1.7;\">"
        "  Welcome to Tripdex. Click below to confirm your email address"
        "  and activate your account."
        "</p>"
    )
    html = _email_template(
        heading="Confirm your email",
        body_html=body,
        cta_url=link,
        cta_label="Confirm email address",
        footnote="This link expires in 24 hours. If you didn't create a Tripdex account, you can safely ignore this email."
    )
    send_email(to, subject, html, text)

def send_reset_email(to: str, username: str, token: str):
    link = f"{FRONTEND_URL}/reset-password?token={token}"
    subject = "Reset your Tripdex password"
    text = (
        f"Hi {username},\n\n"
        f"We received a request to reset the password for your Tripdex account.\n\n"
        f"Reset your password here:\n{link}\n\n"
        f"This link expires in 1 hour. If you didn't request this, you can safely ignore this email.\n\n"
        f"— Tripdex"
    )
    body = (
        f"<p style=\"margin:0 0 16px;font-size:16px;color:#4a4540;line-height:1.6;\">"
        f"  Hi <strong style=\"color:#1a1814\">{username}</strong>,"
        f"</p>"
        "<p style=\"margin:0;font-size:15px;color:#4a4540;line-height:1.7;\">"
        "  We received a request to reset the password for your Tripdex account."
        "  Click below to choose a new password."
        "</p>"
    )
    html = _email_template(
        heading="Reset your password",
        body_html=body,
        cta_url=link,
        cta_label="Reset password",
        footnote="This link expires in 1 hour and can only be used once. If you didn't request a password reset, you can safely ignore this email — your account remains secure."
    )
    send_email(to, subject, html, text)


SEGMENT_ICONS = {
    "flight":   "✈",
    "train":    "🚄",
    "hotel":    "🏨",
    "car":      "🚗",
    "activity": "🎟",
    "other":    "📌",
}

def _fmt_segment_row(seg_data: dict) -> str:
    """One-line summary of a segment for the reply email."""
    icon  = SEGMENT_ICONS.get(seg_data.get("type", "other"), "📌")
    typ   = (seg_data.get("type") or "segment").capitalize()
    orig  = seg_data.get("origin") or ""
    dest  = seg_data.get("destination") or ""
    route = f"{orig} → {dest}" if orig and dest else (orig or dest or "")
    date  = ""
    dep   = seg_data.get("departs_at") or seg_data.get("check_in") or ""
    if dep and "T" in dep:
        date = dep[:10]
    elif dep:
        date = dep[:10]
    carrier = seg_data.get("carrier") or seg_data.get("hotel_name") or ""
    ref     = seg_data.get("confirmation_ref") or ""

    parts = [p for p in [route, carrier, ref] if p]
    detail = " · ".join(parts)
    line = f"{icon} {typ}"
    if date:    line += f" &nbsp;·&nbsp; {date}"
    if detail:  line += f" &nbsp;·&nbsp; {detail}"
    return line



def send_assignment_email(to: str, subject: str, segments_data: list, trips_and_tokens: list):
    """
    Send a 'where should we add this?' email.
    trips_and_tokens: list of (token_str, trip_or_None)
    """
    api_url = os.getenv("FRONTEND_URL", "https://waypoint.emdm.ch")
    resolve_base = f"{api_url}/api/emails/resolve"
    reply_subject = f"Re: {subject}" if subject else "Where should we add this booking?"

    seg_rows = "".join(
        '<li style="padding:6px 0;border-bottom:1px solid #e0d8cc;font-size:14px;color:#4a4540;">' +
        _fmt_segment_row(s) + "</li>"
        for s in segments_data
    )
    seg_block = (
        '<ul style="margin:0 0 24px;padding:0;list-style:none;">' + seg_rows + "</ul>"
    )

    # Build trip option buttons
    btn_style = (
        'display:inline-block;margin:6px 4px;padding:10px 18px;' +
        'background:#b5651d;color:#fff;border-radius:8px;' +
        'text-decoration:none;font-size:14px;font-weight:500'
    )
    new_style = btn_style.replace("#b5651d", "#6b7f5e")

    btns_html = ""
    btns_plain = ""
    for (tok, trip) in trips_and_tokens:
        url = f"{resolve_base}/{tok}"
        if trip is None:
            btns_html  += f'<a href="{url}" style="{new_style}">✦ Create new trip</a>\n'
            btns_plain += f"  Create new trip: {url}\n"
        else:
            label = f"{trip.name}"
            if trip.start_date:
                label += f" ({trip.start_date[:7]})"
            btns_html  += f'<a href="{url}" style="{btn_style}">Add to {label}</a>\n'
            btns_plain += f"  Add to {label}: {url}\n"

    body_html = (
        '<p style="margin:0 0 16px;font-size:15px;color:#4a4540;line-height:1.7;">' +
        "We parsed your forwarded email but weren\u2019t sure which trip to add it to." +
        " Tap a button below to assign it:" +
        "</p>" +
        seg_block +
        '<div style="margin:0 0 8px;">' + btns_html + "</div>"
    )
    plain = (
        "We parsed your forwarded email but weren't sure which trip to add it to.\n\n" +
        "Tap one of the links below to assign the segments:\n\n" +
        btns_plain +
        f"\n\nOr open Tripdex to manage it manually: {api_url}\n\n\u2014 Tripdex"
    )

    html = _email_template(
        heading="Where should we add this?",
        body_html=body_html,
        cta_url=api_url,
        cta_label="Open Tripdex",
        footnote="Didn't forward this? You can ignore this email.",
    )
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = reply_subject
        msg["From"]    = f"Tripdex <{FROM_EMAIL}>"
        msg["To"]      = to
        msg["Reply-To"] = "Tripdex <tripdex@emdm.ch>"
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))
        with smtplib.SMTP("localhost") as sv:
            sv.sendmail(FROM_EMAIL, [to], msg.as_string())
    except Exception as e:
        import logging
        logging.getLogger("tripdex").warning(f"Could not send assignment email to {to}: {e}")

def send_ingest_reply(to: str, status: str, subject: str,
                      segments_data: list = None, trip_name: str = None,
                      error: str = None, trip_created: bool = False):
    """Reply to sender summarising what Tripdex did with their forwarded email."""
    app_url = FRONTEND_URL
    reply_subject = f"Re: {subject}" if subject else "Your Tripdex itinerary update"

    if status == "ok" and segments_data:
        count = len(segments_data)
        if trip_created:
            heading = f"Created trip \u2728 and added {count} segment{'s' if count > 1 else ''}"
        else:
            heading = f"Added {count} segment{'s' if count > 1 else ''} to your itinerary"
        if trip_name:
            label = "New trip:" if trip_created else "Trip:"
            trip_line = (
                f'<p style="margin:0 0 20px;font-size:13px;color:#8a847c;">'
                f'{label} <strong style="color:#4a4540">{trip_name}</strong></p>'
            )
        else:
            trip_line = ""
        seg_rows = "".join(
            '<li style="padding:6px 0;border-bottom:1px solid #e0d8cc;'
            'font-size:14px;color:#4a4540;">'
            + _fmt_segment_row(s) + "</li>"
            for s in segments_data
        )
        body_html = (
            '<p style="margin:0 0 16px;font-size:15px;color:#4a4540;line-height:1.7;">'
            "We parsed your forwarded email and added the following to Tripdex:"
            "</p>"
            + trip_line
            + '<ul style="margin:0 0 8px;padding:0;list-style:none;">'
            + seg_rows + "</ul>"
        )
        plain = (
            f"We parsed your forwarded email and added {count} segment(s) to Tripdex"
            + (f" ({trip_name})" if trip_name else "") + ":\n\n"
            + "\n".join(
                "  - " + _fmt_segment_row(s).replace("&nbsp;", " ").replace("·", "|")
                for s in segments_data
            )
            + f"\n\nView your trip: {app_url}\n\n— Tripdex"
        )
        footnote = "Questions or corrections? Reply to this email or edit directly in Tripdex."

    elif status == "no_segments":
        heading = "We couldn\u2019t find any travel details"
        body_html = (
            '<p style="margin:0 0 16px;font-size:15px;color:#4a4540;line-height:1.7;">'
            "We received your forwarded email but couldn\u2019t extract any travel segments from it."
            "</p>"
            '<p style="margin:0;font-size:15px;color:#4a4540;line-height:1.7;">'
            "This can happen with heavily formatted emails or scanned documents. "
            "You can add segments manually in the app, or try forwarding a plain-text version."
            "</p>"
        )
        plain = (
            "We received your forwarded email but couldn't extract any travel segments.\n\n"
            "You can add segments manually in the app, or try forwarding a plain-text version.\n\n"
            f"Open Tripdex: {app_url}\n\n\u2014 Tripdex"
        )
        footnote = "If this keeps happening, reply to this email and we\u2019ll take a look."

    elif status == "no_trip":
        heading = "Couldn\u2019t match your email to a trip"
        body_html = (
            '<p style="margin:0 0 16px;font-size:15px;color:#4a4540;line-height:1.7;">'
            "We parsed your email but couldn\u2019t find a matching trip in your account."
            "</p>"
            '<p style="margin:0;font-size:15px;color:#4a4540;line-height:1.7;">'
            "Open Tripdex to create a trip first, then forward the email again \u2014 "
            "or add the segment manually."
            "</p>"
        )
        plain = (
            "We parsed your email but couldn't find a matching trip in your account.\n\n"
            "Create a trip in Tripdex first, then forward the email again.\n\n"
            f"Open Tripdex: {app_url}\n\n\u2014 Tripdex"
        )
        footnote = "Need help? Reply to this email."

    else:
        heading = "Something went wrong"
        body_html = (
            '<p style="margin:0 0 16px;font-size:15px;color:#4a4540;line-height:1.7;">'
            "We received your email but ran into a problem while processing it."
            "</p>"
            '<p style="margin:0;font-size:15px;color:#4a4540;line-height:1.7;">'
            "Please try forwarding it again, or add your travel details manually in the app."
            "</p>"
        )
        plain = (
            "We received your email but ran into a problem processing it.\n\n"
            "Please try forwarding it again, or add your travel details manually.\n\n"
            f"Open Tripdex: {app_url}\n\n\u2014 Tripdex"
        )
        footnote = "If this keeps happening, reply to this email and we\u2019ll investigate."

    html = _email_template(
        heading=heading,
        body_html=body_html,
        cta_url=app_url,
        cta_label="Open Tripdex",
        footnote=footnote,
    )
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = reply_subject
        msg["From"]    = f"Tripdex <{FROM_EMAIL}>"
        msg["To"]      = to
        msg["Reply-To"] = "Tripdex <tripdex@emdm.ch>"
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("localhost") as s:
            s.sendmail(FROM_EMAIL, [to], msg.as_string())
    except Exception as e:
        import logging
        logging.getLogger("tripdex").warning(f"Could not send ingest reply to {to}: {e}")




def send_unregistered_reply(to: str):
    """Send a branded reply when sender is not a registered user."""
    register_url = FRONTEND_URL
    profile_url  = f"{FRONTEND_URL}#profile"
    subject = "Tripdex — we don't recognise this email address"
    body_text = (
        f"Hi,\n\n"
        f"We received your forwarded email but couldn't match {to} to a Tripdex account.\n\n"
        f"There are two ways to fix this:\n"
        f"1. Create a new Tripdex account using this email address:\n   {register_url}\n\n"
        f"2. Already have an account? Add {to} as a source email in your Profile:\n   {profile_url}\n\n"
        f"Once set up, forward your travel confirmation emails to tripdex@emdm.ch "
        f"and we'll build your itinerary automatically.\n\n"
        f"— Tripdex"
    )
    body_html = (
        "<p style=\"margin:0 0 12px;font-size:15px;color:#4a4540;line-height:1.7;\">"
        "  We received your forwarded email but couldn&#39;t find a Tripdex account"
        f" registered to <strong style=\"color:#1a1814\">{to}</strong>."
        "</p>"
        "<p style=\"margin:0 0 12px;font-size:15px;color:#4a4540;line-height:1.7;\">"
        "  There are two ways to fix this:"
        "</p>"
        "<ul style=\"margin:0 0 16px;padding-left:20px;font-size:14px;color:#4a4540;line-height:1.8;\">"
        "  <li><strong>New to Tripdex?</strong> Create a free account — use this exact email address when you register.</li>"
        f"  <li><strong>Already have an account?</strong> Log in and add <em>{to}</em> as a source email in your Profile.</li>"
        "</ul>"
    )
    html = _email_template(
        heading="Email not recognised",
        body_html=body_html,
        cta_url=register_url,
        cta_label="Create your account",
        footnote="You're receiving this because someone forwarded a travel confirmation from this address to tripdex@emdm.ch."
    )
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Tripdex <{FROM_EMAIL}>"
        msg["To"]      = to
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("localhost") as s:
            s.sendmail(FROM_EMAIL, [to], msg.as_string())
    except Exception as e:
        import logging; logging.getLogger("tripdex").warning(f"Could not send unregistered reply: {e}")

