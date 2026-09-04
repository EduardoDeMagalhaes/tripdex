# Tripdex — Project Backlog & Context

## Development workflow
- **All new features go to test first** (`test.tripdex.emdm.ch`, port 8001, service: `tripdex-test`)
- Eduardo tests and approves → then promote to live (`tripdex.emdm.ch`, port 8000, service: `tripdex`)
- Never implement new features directly on live
- This rule applies to all emdm.ch projects

## Quick context for new sessions
- **Server**: Hetzner CX22, `178.104.227.162`, Ubuntu 24.04, user `eduardo`
- **App**: `/home/eduardo/tripdex/` — FastAPI + SQLite + OpenAI GPT-4o
- **Frontend**: `/home/eduardo/tripdex/static/index.html` — vanilla JS, single file
- **Service**: `sudo systemctl restart tripdex` — runs on `localhost:8000`
- **Domain**: `emdm.ch` via Nginx reverse proxy
- **Keys**: `.env` file has `OPENAI_API_KEY`, `AVIATIONSTACK_KEY`, `SECRET_TOKEN`, `ADMIN_EMAIL`
- **Stack**: Python venv at `venv/`, logs at `logs/dialog.log`

## Architecture notes
- Segments auto-enriched after every save (trains via transport.opendata.ch, flights via AviationStack/AeroDataBox)
- Email ingest: forward to `tripdex@emdm.ch` → parsed by GPT-4o → segments created
- Connection search routing: Swiss/cross-border → transport.opendata.ch | German → DB Vendo (Docker, port 3000) | Austrian/Italian → v6.oebb.transport.rest
- Auth: session cookies + bcrypt
- Frontend: vanilla JS, single `static/index.html`

---

## Error codes

| Code | Location |
|------|----------|
| ERR01 | `loadTrips()` — trips fetch failed on boot |
| ERR02 | `sendDialogTurn()` — main NL add dialog |
| ERR03 | Segment save — inline form catch block |
| ERR04 | Segment save — modal POST/PATCH |
| ERR05 | Trip edit save |
| ERR07 | Edit segment assistant (`_editAssistAddMsg`) |
| ERR08 | Segment move |
| ERR09 | Trip-level assistant |
| ERR10 | Trip planner (`plannerSend`) |
| ERR11 | Add segment (guardrail / date-check path) |
| ERR12 | Connection search (`searchConnections`) |
| ERR13 | PDF upload (`handlePdfFile`) |
| ERR14 | Discard orphans |
| ERR15 | Save profile |

Next free: **ERR16**

---

## Backlog

### 1. Multi-user & authentication ✅ DONE (2026-05-18)
- Session cookie auth, bcrypt passwords, login/logout/register
- Each user sees only their own trips and segments

### 2. Source email management ✅ DONE (2026-05-29)
- `user_source_emails` table: id, user_id, email, status (pending/confirmed), created_at, verified_at
- Verified via email link (48h expiry), globally unique on confirmed emails only
- Ingest routing checks confirmed source emails in addition to primary account email
- Bounce rate-limited: silent drop after 3 unknown-sender attempts in 24h
- Resend: 1h cooldown per address, 20/day global cap
- Profile page: "Source emails" section with anchor nav, To confirm / Confirmed badges, Resend + Remove

### 3. Trip-level assistant ✅ DONE (2026-05-18)
- Persistent chat panel at trip level
- Full context: all segments, dates, notes, enrichment data

### 4. Segment maps
- Minimap thumbnail inside each expanded segment card
- Hotels: pin on exact OSM coordinates (from enrichment lat/lon)
- Flights & trains: both endpoints shown with a route line between them
- Click thumbnail → full map modal
- **Note**: use Leaflet.js via CDN — previous raw OSM tile attempts failed on CORS/canvas/display:none

### 5. Top-level trip creation assistant ✅ DONE (2026-05-18)
- Conversational planner at dashboard level builds full trip from scratch
- Lives at home screen "✦ Plan with AI" action card
- Endpoint: `POST /api/parse/plan`

### 6. Segment date guardrails ✅ DONE (2026-05-29)
- `app/routers/guardrails.py` — pre-GPT checks
- `check_date_out_of_range()` — fires when segment date is outside trip bounds
- Frontend: `appendGuardrailInline()` renders warning + action buttons inline
- Actions: Extend trip dates / Move to matching trip / Add anyway / Cancel

### 7. DB (Deutsche Bahn) connection search ✅ DONE (2026-05-18)
- `db-vendo-client` Docker container on VPS, port 3000
- Wired into `search_connections()` in `app/routers/enrich.py`

### 8. Additional European rail APIs ✅ PARTIALLY DONE (2026-05-29)
- ÖBB (Austria + Italian cross-border): `v6.oebb.transport.rest` — **DONE**
- SNCF (France): not yet implemented
- Trenitalia (Italy domestic): no public HAFAS profile exists; reverse-engineering needed — **PARKED**

### 9. OpenAI token usage log (low priority)
- Shared SQLite DB `/opt/emdm/shared/usage.db` with one `api_usage` table
- Per-endpoint granularity across all emdm.ch apps
- Only worth building if curious about feature-level cost breakdown

### 10. Uncertain email → user confirmation flow ✅ EFFECTIVELY DONE
- `should_ask_user()` + `save_orphan_segments()` + `send_assignment_email()` handle the ambiguous case
- Auto-creates a trip when no close match exists (correct UX for new bookings)
- Orphan tray in Inbox catches anything that doesn't match

### 11. Orphan segments tray ✅ DONE (2026-05-18)
- Segments without a trip land in Inbox tray
- Each orphan has: "Add to trip →" / "Create new trip" / "Discard"
- Backend: `trip_id = NULL` + `parse_status = 'pending_assignment'`

### 12. PDF upload ✅ DONE (2026-05-28)
- PDF upload accessible via Home screen action card
- Inline progress text while parsing

### 13. Avatar system ✅ DONE (2026-05-29)
- 8 default SVG avatars: train, plane, bag, passport, compass, camera, bell, globe
  warm terracotta/beige palette, at `/home/eduardo/tripdex/static/avatars/defaults/`
- Random default assigned once at registration
- `POST /api/auth/avatar` — upload custom photo (Pillow 128×128 crop+resize)
- `POST /api/auth/avatar/pick` — switch to any default by name
- `POST /api/auth/avatar/reset` — revert to initials
- Profile page: tappable circle + 8-option picker row

### 14. Home screen ✅ DONE (2026-05-28)
- Greeting + next trip countdown
- Quick action cards: Plan with AI / Upload PDF / New trip
- Email forwarding tip with tap-to-copy address
- Trip list: 4 upcoming inline, Past trips + More trips as folder rows

### 15. Dark mode ✅ DONE (2026-05-28)
- Full warm dark palette
- Theme picker in Settings: Light / Dark / System

### 16. Sidebar folders ✅ DONE (2026-05-29)
- Past trips / More upcoming trips — toggles open and closed on tap

### 17. Profile / Settings split ✅ DONE (2026-05-28)
- Profile: email, username, home city, airports, source emails, avatar
- Settings: theme picker

### 18–22. Various cleanup ✅ DONE (2026-05-28)
- Avatar initials, header cleanup, API usage widget moved to admin, ERR05 bug fix, frontend refactor

### 23. Feedback system ✅ DONE (2026-05-29)
- `feedback` table: short_id (WPU-###), type, title, description, context, screenshot, status
- `POST /api/feedback` — multipart, auth optional, screenshot optional
- Sequential WPU-### IDs, global across bugs + features
- Email to `ADMIN_EMAIL` with full details + triage-with-Claude copy-paste prompt
- Success screen shows WPU-### ID
- Sidebar entry: 💬 Report a bug / Request a feature
- Triage flow: bring WPU-### to Claude in new chat → Claude reads DB + backlog → recommends priority + action

### 24. Gate display timing ✅ DONE (2026-05-29)
- Gate row always visible in flight details
- Shows `—` until 36h before departure, then real value (or TBA if enriched but no gate yet)
- Delay already gated to 24h since 2026-05-18

---

## Known issues / small fixes
- Trenitalia domestic routes: no API coverage, show "Open Trenitalia" fallback button (not yet implemented)
- SNCF (France): not yet covered by any rail API


### 25. Date field calendar picker
- All date input fields (segment dep/arr, trip start/end) should auto-show a calendar on focus
- Calendar opens pre-scrolled to today's date for quick selection
- User taps a day → fills the field, no typing needed
- Currently fields accept DD.MM.YYYY text input — picker should complement, not replace
