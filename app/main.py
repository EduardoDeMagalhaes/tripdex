from fastapi import FastAPI, Request, UploadFile, File, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os, subprocess

from app.database import Base, engine
from app.routers import trips, segments, emails, lookup, enrich, auth
from app.routers import parse_dialog, parse_assist, parse_planner, parse_connect, calendar
from app.routers import source_emails
from app.routers import feedback
from app.routers import custom_types

Base.metadata.create_all(bind=engine)

# ── DB migrations (safe, idempotent) ──────────────────────────────────────────
def _run_migrations():
    from sqlalchemy import inspect, text
    with engine.connect() as conn:
        cols = [c["name"] for c in inspect(engine).get_columns("users")] if inspect(engine).has_table("users") else []
        if "is_disabled" not in cols and cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_disabled INTEGER NOT NULL DEFAULT 0"))
            conn.commit()
        # Indexes (IF NOT EXISTS = safe to re-run)
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_user_id ON trips(user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_segments_trip_id ON segments(trip_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_segments_departs ON segments(trip_id, departs_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_email_tokens_user ON email_tokens(user_id, type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_raw_emails_trip ON raw_emails(trip_id)"))
        conn.commit()
        if "calendar_token" not in cols and cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN calendar_token TEXT"))
            conn.commit()
    # trips table migrations
    trip_cols = [c["name"] for c in inspect(engine).get_columns("trips")] if inspect(engine).has_table("trips") else []
    if "calendar_token" not in trip_cols and trip_cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE trips ADD COLUMN calendar_token TEXT"))
            conn.commit()

_run_migrations()

app = FastAPI(title="Waypoint", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_TOKEN = os.getenv("SECRET_TOKEN", "change_me")

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired as _SE
import os as _os

def _decode_session(token: str):
    secret = _os.getenv("SESSION_SECRET", "changeme")
    try:
        return URLSafeTimedSerializer(secret).loads(token, max_age=60*60*24*30)
    except (BadSignature, _SE):
        return None

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Static files and health — always public
    if not path.startswith("/api"):
        return await call_next(request)

    # Auth endpoints — always public
    if path.startswith("/api/auth"):
        return await call_next(request)

    # Calendar ICS feeds — public (token-secured at handler level)
    if path.endswith(".ics") or path.endswith("/calendar-token"):
        return await call_next(request)

    # Email-link-clicked, token-in-the-URL endpoints — "no session required, the
    # token IS the auth" by design (source-email confirmation, orphan-segment
    # one-click assignment). These are opened straight from an email client with
    # no wp_session cookie, so they must be reachable without one; each verifies
    # its own token server-side before doing anything.
    if path == "/api/source-emails/verify" or path.startswith("/api/emails/resolve/"):
        return await call_next(request)

    from fastapi.responses import JSONResponse

    # Check session cookie first
    session_token = request.cookies.get("wp_session")
    if session_token and _decode_session(session_token):
        return await call_next(request)

    # Fall back to legacy SECRET_TOKEN for CLI / email ingest scripts
    token = request.headers.get("X-Token") or request.query_params.get("token")
    if token == SECRET_TOKEN:
        return await call_next(request)

    return JSONResponse({"detail": "Unauthorized"}, status_code=401)

app.include_router(trips.router)
app.include_router(segments.router)
app.include_router(emails.router)
app.include_router(parse_dialog.router)
app.include_router(parse_assist.router)
app.include_router(parse_planner.router)
app.include_router(parse_connect.router)
app.include_router(lookup.router)
app.include_router(enrich.router)
app.include_router(auth.router)
app.include_router(calendar.router)
app.include_router(source_emails.router)
app.include_router(feedback.router)
app.include_router(custom_types.router)

@app.get("/health")
def health():
    return {"status": "ok"}



# ── DEPLOY ENDPOINT ──
DEPLOY_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ALLOWED = ["static/index.html","app/routers/parse.py","app/routers/lookup.py","app/routers/trips.py","app/routers/segments.py","app/main.py"]

@app.post("/api/deploy")
async def deploy_file(file: UploadFile = File(...), path: str = "", restart: bool = False, x_token: str = Header(None)):
    if x_token != SECRET_TOKEN: raise HTTPException(401, "Unauthorized")
    if path not in ALLOWED: raise HTTPException(400, f"Path not allowed: {path}")
    target = os.path.join(DEPLOY_ROOT, path)
    content = await file.read()
    open(target, "wb").write(content)
    if restart: subprocess.Popen(["sudo","systemctl","restart","waypoint"])
    return {"ok": True, "path": path, "bytes": len(content)}

from fastapi.responses import FileResponse

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")

# Add middleware to prevent HTML caching
@app.middleware("http")
async def no_cache_html(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html") or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
    return response

if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
