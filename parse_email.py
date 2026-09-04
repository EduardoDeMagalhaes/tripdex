#!/usr/bin/env python3
"""
Postfix pipe script for travel@emdm.ch
Receives raw email on stdin, extracts text + PDFs, calls /api/emails/ingest.

Install:
  chmod +x /home/eduardo/tripdex/parse_email.py
  Add to /etc/aliases:  travel: "|/home/eduardo/tripdex/parse_email.py"
  Run: newaliases
"""

import sys
import os
import email
import email.policy
import json
import urllib.request
import urllib.error
import logging
import traceback
import io
import base64

# ── Config ───────────────────────────────────────────────────────────────────
API_URL   = "http://localhost:8000/api/emails/ingest"
API_TOKEN = "d5f9e9b215da795ef927a399c3eba355"
LOG_FILE  = "/var/log/waypoint-email.log"
VENV_SITE = "/home/eduardo/tripdex/venv/lib/python3.12/site-packages"
MAX_IMAGES         = 6                 # cap per email — GPT-4o cost/latency guard
MAX_IMAGE_BYTES     = 15 * 1024 * 1024  # 15MB per image (post-HEIC-conversion), matches nginx client_max_body_size elsewhere
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
HEIC_CONTENT_TYPES  = {"image/heic", "image/heif"}

# Add venv to path so pdfplumber is available
if VENV_SITE not in sys.path:
    sys.path.insert(0, VENV_SITE)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tripdex-email")


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from a PDF attachment using pdfplumber."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
    except Exception as e:
        log.warning(f"PDF extraction failed: {e}")
        return ""


def _heic_to_jpeg_b64(data: bytes) -> str | None:
    """Convert HEIC/HEIF bytes to a base64 JPEG data URI-ready string, or None on failure."""
    try:
        import pillow_heif
        from PIL import Image
        heif_file = pillow_heif.read_heif(data)
        img = Image.frombytes(heif_file.mode, heif_file.size, heif_file.data, "raw")
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        log.warning(f"HEIC conversion failed: {e}")
        return None


def extract_images(msg: email.message.Message) -> list[str]:
    """
    Extract image attachments (jpg/png/webp/gif + HEIC/HEIF converted to jpeg) as a
    list of data-URI strings ('data:image/jpeg;base64,...'), capped at MAX_IMAGES.
    Inline images referenced only via Content-ID are still picked up — Tripdex
    doesn't need to distinguish inline vs attached, just "is there a picture here".
    """
    images: list[str] = []
    if not msg.is_multipart():
        return images

    for part in msg.walk():
        if len(images) >= MAX_IMAGES:
            log.warning(f"Hit MAX_IMAGES={MAX_IMAGES}, ignoring remaining image attachments")
            break

        ct = part.get_content_type()
        if ct not in IMAGE_CONTENT_TYPES and ct not in HEIC_CONTENT_TYPES:
            continue

        raw = part.get_payload(decode=True)
        if not raw:
            continue
        if len(raw) > MAX_IMAGE_BYTES:
            log.warning(f"Skipping image ({ct}, {len(raw)} bytes) — over MAX_IMAGE_BYTES")
            continue

        if ct in HEIC_CONTENT_TYPES:
            b64 = _heic_to_jpeg_b64(raw)
            if not b64:
                continue
            images.append(f"data:image/jpeg;base64,{b64}")
        else:
            b64 = base64.b64encode(raw).decode("ascii")
            images.append(f"data:{ct};base64,{b64}")

    return images


def extract_body(msg: email.message.Message) -> tuple[str, list[str]]:
    """
    Extract plain text body and any PDF text from a parsed email message.
    Returns (body_text, [pdf_texts]).
    """
    body_parts = []
    pdf_texts  = []

    if msg.is_multipart():
        for part in msg.walk():
            ct   = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")

            if ct == "text/plain" and "attachment" not in disp:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_parts.append(part.get_payload(decode=True).decode(charset, errors="replace"))
                except Exception:
                    pass

            elif ct == "application/pdf" or part.get_filename("").lower().endswith(".pdf"):
                pdf_data = part.get_payload(decode=True)
                if pdf_data:
                    pdf_text = extract_text_from_pdf(pdf_data)
                    if pdf_text:
                        pdf_texts.append(pdf_text)

            elif ct == "text/html" and not body_parts:
                # Fallback: strip HTML tags if no plain text found
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = part.get_payload(decode=True).decode(charset, errors="replace")
                    import re
                    plain = re.sub(r"<[^>]+>", " ", html)
                    plain = re.sub(r"\s+", " ", plain).strip()
                    body_parts.append(plain)
                except Exception:
                    pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            body_parts.append(msg.get_payload(decode=True).decode(charset, errors="replace"))
        except Exception:
            pass

    return "\n\n".join(body_parts), pdf_texts


def call_ingest_api(payload: dict) -> dict:
    """POST to the Tripdex ingest endpoint."""
    data    = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Token":      API_TOKEN,
    }
    req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API HTTP {e.code}: {body}")


def main():
    # Read raw email from stdin
    raw_email = sys.stdin.buffer.read()
    log.info(f"Received email ({len(raw_email)} bytes)")

    try:
        msg = email.message_from_bytes(raw_email, policy=email.policy.default)
    except Exception as e:
        log.error(f"Failed to parse email: {e}")
        sys.exit(0)  # Exit 0 so Postfix doesn't bounce

    message_id   = str(msg.get("Message-ID", "") or "").strip()
    from_address = str(msg.get("From", "") or "").strip()
    subject      = str(msg.get("Subject", "") or "").strip()

    if not message_id:
        import hashlib, time
        message_id = f"<generated-{hashlib.md5(raw_email).hexdigest()}@tripdex>"

    log.info(f"Processing: message_id={message_id} from={from_address} subject={subject}")

    # Extract body + PDF text + image attachments
    body_text, pdf_texts = extract_body(msg)
    images = extract_images(msg)

    # Combine body + PDF content
    full_text = body_text
    if pdf_texts:
        full_text += "\n\n--- PDF ATTACHMENT ---\n\n" + "\n\n---\n\n".join(pdf_texts)

    if not full_text.strip() and not images:
        log.warning("No text content or images extracted from email")
        sys.exit(0)

    # Truncate to ~12000 chars to stay within GPT context
    if len(full_text) > 12000:
        full_text = full_text[:12000] + "\n[truncated]"

    if images:
        log.info(f"Extracted {len(images)} image(s)")

    # Call the ingest API
    payload = {
        "message_id":   message_id,
        "from_address": from_address,
        "subject":      subject,
        "body_text":    full_text,
        "images":       images,
    }

    try:
        result = call_ingest_api(payload)
        log.info(f"Ingest result: {result}")
        if result.get("segments_created", 0) > 0:
            log.info(f"✓ Created {result['segments_created']} segment(s) for trip {result.get('trip_id')}")
        else:
            log.warning(f"No segments created: {result.get('parse_status')} — {result.get('error', '')}")
    except Exception as e:
        log.error(f"Ingest API call failed: {e}\n{traceback.format_exc()}")
        # Don't bounce the email — just log and exit cleanly
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
