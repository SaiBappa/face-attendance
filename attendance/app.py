"""
Aura — Face Attendance backend (Starlette + SQLite).

Thin layer between the wall kiosks, the admin/insights pages and the local ML services:
  recognizer (face match + emotion + attire), listener (speech -> text + language),
  and the brain (Jev decisions, optional Claude replies, templates).

Kiosk (no PIN, LAN only):
  POST /api/recognize        camera frame -> best match + mood + attire; logs one sighting per approach
  POST /api/greet            personal greeting lines for a recognised staff member
  POST /api/event            IN / BREAK / BACK / OUT (only the buttons the location offers)
  POST /api/access           hands-free pass through a gate-mode location: records IN/OUT of its area
  POST /api/talk             text -> reply in the speaker's language (stored per person)
  POST /api/listen           voice clip -> transcript + reply
  GET  /api/kiosk/{name}     screen config, images, announcements, live pulse
  GET  /api/flights          departures/arrivals board (live feed or demo)
  POST /api/assist           raise an assistance request; GET /api/assist/{id} its live status
  POST /api/safety/precheck  PPE items + fatigue risk for a worker about to clock in
  POST /api/safety/check     record the confirmed checklist + self-rated restedness
  Each location decides its safety check (PUT /api/kiosks/{name} {safety_rule, safety_enforce}): none, "@department"
  (each worker's own department rule, on IN) or one named rule everyone entering (IN / BACK) must meet; when
  enforced, /api/event refuses entry without a recent passing check at that kiosk
  An expired security pass (people.pass_expiry before today) blocks /api/event and the safety endpoints
  (403), and each approach is logged as a `pass_expired` supervisor alert.
  Liveness: a match only counts once the recognizer scores the face as a live person, not a photo or
  screen (LIVENESS, LIVENESS_THRESHOLD, LIVENESS_FRAMES); repeated failures raise a `spoof` alert. A live
  match carries a signed, short-lived token that /api/event requires, so a clock-in can't be posted
  for someone the camera never saw.
Assist desk (X-Admin-Pin = ASSIST_PIN or ADMIN_PIN):
  /assist page, GET /api/requests, PUT /api/requests/{id}
Admin (X-Admin-Pin):
  /api/status, /api/events, /api/export.csv, /api/employees…   (original attendance admin)
  /api/people…, /api/kiosks…, /api/media…, /api/announcements…
  /api/insights, /api/insights/person/{name}, /api/status/mood, /api/interactions(.csv), /api/demo, /api/engines
  /api/roster…  (CSV import, JSON push from HR systems, edit, coverage)   /api/alerts…  (feed, ack, routing)
  /api/access/presence (who is inside each area), /api/access (movements), /api/access.csv, /api/access/out
  PUT /api/areas/{name} {reasons: [...], ask_reason, min_zone, max_minutes, capacity}  per-area rules
  /api/muster… emergency roll call: GET (active board + history), POST start, POST {id}/end, POST {id}/mark,
  GET {id}.csv; kiosks with mode 'muster' check people in as safe (POST /api/muster/checkin, no PIN)
Each location has a mode (PUT /api/kiosks/{name} {mode, actions, area, direction}, see access.py):
  attendance (default: IN / BREAK / BACK / OUT, or a chosen subset) or gate (hands-free area entry/exit).

Camera frames and voice clips are processed in memory and discarded. Stored: face vectors plus a
small face crop per enrolled photo and per daily-best kiosk photo of recognised staff (recognizer;
daily bests are kept DAILY_KEEP_DAYS, default 7), names, timestamps, derived mood labels and
conversation text. Visitor faces are never kept.
"""
import asyncio
import contextlib
import csv
import hashlib
import hmac
import io
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import access
import alerts
import muster
import brain
import flights
import insights
import phrases
import roster
import safety
from store import db, init_db

COMPREFACE_URL = os.environ.get("COMPREFACE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("COMPREFACE_API_KEY", "")
LISTENER_URL = os.environ.get("LISTENER_URL", "http://localhost:8001").rstrip("/")
THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.90"))
DUP_WINDOW = int(os.environ.get("DUPLICATE_WINDOW_SECONDS", "60"))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "2468")
ASSIST_PIN = os.environ.get("ASSIST_PIN") or ADMIN_PIN   # for service-desk agents who shouldn't see admin
ASSIST_WEBHOOK_URL = os.environ.get("ASSIST_WEBHOOK_URL", "")
SAFETY_WEBHOOK_URL = os.environ.get("SAFETY_WEBHOOK_URL") or ASSIST_WEBHOOK_URL
MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.join(os.path.dirname(os.environ.get("DB_PATH", "./attendance.db")) or ".", "media"))
# on = staff (with consent) + anonymous visitors; staff_only; visitors_only; off
MOOD_TRACKING = os.environ.get("MOOD_TRACKING", "on").lower()
RETENTION_DAYS = int(os.environ.get("INTERACTION_RETENTION_DAYS", "365"))
# let the recognizer keep each recognised person's best 2 photos a day (for a week) to track changes
ADAPTIVE_LEARNING = os.environ.get("ADAPTIVE_LEARNING", "on").lower() not in ("off", "0", "false", "no")
# Liveness (anti-spoofing): on = a photo/screen of a staff member never opens their actions;
# monitor = score + alert only (for tuning the threshold on a new camera); off
LIVENESS = os.environ.get("LIVENESS", "on").lower()
LIVENESS_THRESHOLD = float(os.environ.get("LIVENESS_THRESHOLD", "0.5"))
LIVENESS_FRAMES = max(1, int(os.environ.get("LIVENESS_FRAMES", "2")))   # matched frames averaged per verdict
LIVENESS_SURE = float(os.environ.get("LIVENESS_SURE", "0.9"))          # ...unless the first one is this clear
SPOOF_ALERT_FRAMES = int(os.environ.get("SPOOF_ALERT_FRAMES", "4"))     # failed verdicts before a `spoof` alert
EVENT_TOKEN_SECONDS = int(os.environ.get("EVENT_TOKEN_SECONDS", "600"))
os.makedirs(MEDIA_DIR, exist_ok=True)

ACTIONS = ("IN", "BREAK", "BACK", "OUT")
# Which action makes sense next, given the last one today. Used only to highlight a button;
# the employee can still tap any action.
NEXT_ACTION = {None: "IN", "OUT": "IN", "IN": "OUT", "BACK": "OUT", "BREAK": "BACK"}

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ----------------------------------------------------------------------------- database
def _purge_old():
    cut = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    with db() as c:
        c.execute("DELETE FROM interactions WHERE day < ? AND demo=0", (cut,))
        c.execute("DELETE FROM sightings WHERE day < ? AND demo=0", (cut,))


init_db()
_purge_old()


def now() -> datetime:
    return datetime.now()  # container TZ is set via the TZ env var


def stamp(ts: datetime = None) -> str:
    return (ts or now()).isoformat(timespec="seconds")


def last_action_today(conn, employee: str):
    return conn.execute(
        "SELECT action, ts FROM events WHERE employee=? AND day=? ORDER BY id DESC LIMIT 1",
        (employee, date.today().isoformat()),
    ).fetchone()


def profile(conn, name: str) -> dict:
    r = conn.execute("SELECT * FROM people WHERE name=?", (name,)).fetchone()
    return dict(r) if r else {"name": name, "mood_consent": 1}


def pass_expired(prof: dict) -> bool:
    """True when the security pass expired before today (valid through its expiry date). No date = not known expired."""
    exp = (prof.get("pass_expiry") or "").strip()
    return bool(exp) and exp < date.today().isoformat()


def pass_incident(conn, employee: str, prof: dict, kiosk: Optional[str]):
    """Log an expired-pass incident and push it to the supervisor webhook right away (not on the next scan)."""
    if alerts.pass_incident(conn, employee, prof.get("department"), kiosk, prof.get("pass_expiry")):
        task = asyncio.create_task(alerts.deliver())
        _bg.add(task)
        task.add_done_callback(_bg.discard)


def refuse_expired_pass(conn, employee: str, kiosk: Optional[str]):
    prof = profile(conn, employee)
    if pass_expired(prof):
        pass_incident(conn, employee, prof, kiosk)
        conn.commit()  # the `with db()` block rolls back on the exception below; keep the incident
        raise HTTPException(403, f"Security pass expired on {prof['pass_expiry']}. Report to the pass office — your supervisor has been notified.")


def refuse_unsafe_entry(conn, employee: str, kiosk: Optional[str], action: str):
    """An enforced location only lets people in (IN / BACK) after a passing safety check there."""
    if action not in safety.ENTRY_ACTIONS or not kiosk:
        return
    rules = safety.effective(conn, profile(conn, employee).get("department"), kiosk, action)
    if rules["enforce"] and not safety.entry_cleared(conn, employee, kiosk):
        raise HTTPException(403, f"Safety requirements for {rules['location']} are not met — complete the safety check "
                                 "with all required PPE before entering.")


def kiosk_row(conn, name: str) -> dict:
    r = conn.execute("SELECT * FROM kiosks WHERE name=?", (name,)).fetchone()
    if not r:
        conn.execute("INSERT OR IGNORE INTO kiosks (name, zone, headline) VALUES (?,?,?)", (name, name, name))
        r = conn.execute("SELECT * FROM kiosks WHERE name=?", (name,)).fetchone()
    d = dict(r)
    try:
        d["info"] = json.loads(d.get("info") or "[]")
    except ValueError:
        d["info"] = []
    return d


def mood_allowed(kind: str, consent: bool = True) -> bool:
    if MOOD_TRACKING == "off":
        return False
    if kind == "staff":
        return consent and MOOD_TRACKING in ("on", "staff_only")
    return MOOD_TRACKING in ("on", "visitors_only")


# ----------------------------------------------------------------------------- helpers
def require_pin(request: Request):
    pin = request.headers.get("x-admin-pin") or request.query_params.get("pin")
    if pin != ADMIN_PIN:
        raise HTTPException(401, "Invalid admin PIN")


def require_staff(request: Request):
    pin = request.headers.get("x-admin-pin") or request.query_params.get("pin")
    if pin not in (ADMIN_PIN, ASSIST_PIN):
        raise HTTPException(401, "Invalid PIN")


def cf_headers() -> dict:
    if not API_KEY or API_KEY.startswith("PASTE"):
        raise HTTPException(503, "COMPREFACE_API_KEY is not configured on the server")
    return {"x-api-key": API_KEY}


# One pooled client for every recognizer call: creating a client (and a TCP connection) per frame
# cost ~5 ms. Opened in lifespan(); created lazily if a call arrives first.
_cf_client: Optional[httpx.AsyncClient] = None


def _recognizer_client() -> httpx.AsyncClient:
    global _cf_client
    if _cf_client is None or _cf_client.is_closed:
        # retry connect failures (Docker's DNS occasionally stalls, the recognizer may be restarting)
        _cf_client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=4),
                                       transport=httpx.AsyncHTTPTransport(retries=2))
    return _cf_client


async def cf(method: str, path: str, **kw) -> httpx.Response:
    try:
        r = await _recognizer_client().request(
            method, f"{COMPREFACE_URL}/api/v1/recognition{path}", headers=cf_headers(), **kw
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Recognizer unreachable: {e.__class__.__name__}")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        raise HTTPException(r.status_code, f"Recognizer: {msg}")
    return r


async def read_upload(request: Request):
    form = await request.form()
    up = form.get("file")
    if up is None:
        raise HTTPException(400, "file field required")
    data = await up.read()
    return (up.filename or "frame.jpg", data, up.content_type or "image/jpeg"), form


def _range(request: Request, default_days: int = 0):
    date_to = request.query_params.get("to") or date.today().isoformat()
    date_from = request.query_params.get("from") or (date.fromisoformat(date_to) - timedelta(days=default_days)).isoformat()
    return date_from, date_to


# ----------------------------------------------------------------------------- pages
# Wall screens stay open for days; "no-cache" makes them revalidate on every load so a UI update
# shows up on the next refresh instead of whatever copy Safari cached.
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


def page(name: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC, name), headers=NO_CACHE)


async def kiosk_page(request):
    return page("kiosk.html")


APP_ICONS = [
    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
]

# The staff screens install as their own apps (own id + scope) next to the kiosk: slug -> (name, short name, colour)
STAFF_APPS = {
    "admin": ("Aura Admin", "Admin", "#f4f6f9"),
    "insights": ("Aura Insights", "Insights", "#f3f4f1"),
    "assist": ("Aura Assist", "Assist", "#f3f4f1"),
}


def app_manifest(**fields) -> JSONResponse:
    return JSONResponse({"display": "standalone", "orientation": "any", "icons": APP_ICONS, **fields},
                        media_type="application/manifest+json", headers=NO_CACHE)


async def kiosk_manifest(request):
    # Built per kiosk: iPadOS launches a Home Screen web app at the manifest's start_url, so it must
    # carry this screen's ?kiosk=… (and ?autostart=1 etc.) or every iPad would open as "Main".
    # The id is per kiosk too, so Chrome/Android treat each kiosk as its own installable app.
    query = request.url.query
    kiosk = request.query_params.get("kiosk", "")
    return app_manifest(
        id="/" + (f"?kiosk={kiosk}" if kiosk else ""),
        name=f"Aura · {kiosk}" if kiosk else "Aura",
        short_name="Aura",
        start_url="/" + (f"?{query}" if query else ""),
        scope="/",
        background_color="#031a2b",
        theme_color="#031a2b",
    )


async def staff_manifest(request):
    slug = request.path_params["app"]
    if slug not in STAFF_APPS:
        raise HTTPException(404, "unknown app")
    name, short, colour = STAFF_APPS[slug]
    return app_manifest(id=f"/{slug}", name=name, short_name=short, start_url=f"/{slug}", scope=f"/{slug}",
                        background_color=colour, theme_color=colour)


async def service_worker(request):
    # Served from the root (not /static) so its scope covers every page; no-cache so updates roll out.
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript", headers=NO_CACHE)


async def admin_page(request):
    return page("admin.html")


async def assist_page(request):
    return page("assist.html")


async def insights_page(request):
    return page("insights.html")


async def health(request):
    ok = True
    try:
        await cf("GET", "/subjects")
    except HTTPException:
        ok = False
    return JSONResponse({"ok": ok, "threshold": THRESHOLD, "liveness": LIVENESS, "liveness_threshold": LIVENESS_THRESHOLD})


# ----------------------------------------------------------------------------- liveness + event tokens
def _event_secret() -> bytes:
    """HMAC key for event tokens, kept next to the database so it survives restarts."""
    p = os.path.join(os.path.dirname(os.path.abspath(os.environ.get("DB_PATH", "./attendance.db"))), ".event_secret")
    try:
        with open(p, "rb") as fh:
            key = fh.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    with contextlib.suppress(OSError):
        with open(p, "wb") as fh:
            fh.write(key)
    return key


EVENT_SECRET = _event_secret()


def _sign(employee: str, kiosk: str, exp: int) -> str:
    return hmac.new(EVENT_SECRET, f"{employee}\n{kiosk}\n{exp}".encode(), hashlib.sha256).hexdigest()


def event_token(employee: str, kiosk: str) -> str:
    """Proof that the camera saw `employee`, live, at `kiosk` just now (see record_event)."""
    exp = int(time.time()) + EVENT_TOKEN_SECONDS
    return f"{exp}.{_sign(employee, kiosk, exp)}"


def event_token_ok(token, employee: str, kiosk: str) -> bool:
    try:
        exp, sig = str(token).split(".", 1)
        exp = int(exp)
    except (ValueError, TypeError):
        return False
    return exp >= time.time() and hmac.compare_digest(sig, _sign(employee, kiosk, exp))


# encounter -> {"who", "scores": last LIVENESS_FRAMES liveness scores, "fails", "alerted", "t"}
_liveness: dict = {}


def _liveness_verdict(encounter: str, employee: str, score) -> str:
    """"live", "checking" (need another frame) or "spoof" for a matched face. A missing score (liveness
    off, or the recognizer has no liveness models) counts as live, so attendance never stops working."""
    if LIVENESS == "off" or score is None:
        return "live"
    if not encounter:
        return "live" if score >= LIVENESS_THRESHOLD else "spoof"
    if len(_liveness) > 500:  # forget stale approaches
        for k in [k for k, v in _liveness.items() if time.time() - v["t"] > 600]:
            _liveness.pop(k, None)
    e = _liveness.get(encounter)
    if not e or e["who"] != employee:
        e = _liveness[encounter] = {"who": employee, "scores": [], "fails": 0, "alerted": False}
    e["t"] = time.time()
    e["scores"] = (e["scores"] + [score])[-LIVENESS_FRAMES:]
    if len(e["scores"]) < LIVENESS_FRAMES:
        return "live" if score >= LIVENESS_SURE else "checking"
    if sum(e["scores"]) / len(e["scores"]) >= LIVENESS_THRESHOLD:
        return "live"
    e["fails"] += 1
    return "spoof"


def _spoof_alert(encounter: str, employee: str, kiosk: str):
    """One `spoof` supervisor alert per approach, once it has failed SPOOF_ALERT_FRAMES times."""
    e = _liveness.get(encounter)
    if not e or e["alerted"] or e["fails"] < SPOOF_ALERT_FRAMES:
        return
    e["alerted"] = True
    with db() as conn:
        created = alerts.spoof_incident(conn, employee, profile(conn, employee).get("department"), kiosk)
    if created:
        task = asyncio.create_task(alerts.deliver())
        _bg.add(task)
        task.add_done_callback(_bg.discard)


# (employee, kiosk) -> (hi-vis share, time) from the latest matched camera frame. Safety checks use this
# server-side reading, never a value sent by the kiosk page, so hi-vis can't be "confirmed" without being seen.
_hivis_seen: dict = {}
HIVIS_FRESH_S = 180


def camera_hivis(employee: str, kiosk: str):
    v = _hivis_seen.get((employee, kiosk))
    return v[0] if v and time.time() - v[1] <= HIVIS_FRESH_S else None


# ----------------------------------------------------------------------------- kiosk API
# encounter id -> {"frames": n, "logged": bool, "t": last seen}; one sighting per approach
_encounters: dict = {}


def _log_sighting(encounter: str, kiosk: str, person: Optional[str], face: dict, consent: bool):
    e = _encounters.setdefault(encounter, {"frames": 0, "logged": False})
    e["frames"] += 1
    e["t"] = time.time()
    if len(_encounters) > 500:  # forget stale approaches
        for k in [k for k, v in _encounters.items() if time.time() - v.get("t", 0) > 600]:
            _encounters.pop(k, None)
    if e["logged"]:
        return
    kind = "staff" if person else "visitor"
    if kind == "visitor" and e["frames"] < 3:  # give recognition a few frames before calling it a visitor
        return
    emo = face.get("emotion") or {}
    attire = (face.get("attire") or {}).get("name")
    allowed = mood_allowed(kind, consent)
    if kind == "visitor" and not allowed:
        e["logged"] = True
        return
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO sightings (ts, day, hour, kiosk, person, kind, mood, valence, confidence, attire) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (stamp(ts), ts.date().isoformat(), ts.hour, kiosk, person, kind,
             emo.get("label") if allowed else None,
             brain.MOODS.get(emo.get("label"), (None,))[0] if allowed else None,
             emo.get("confidence") if allowed else None,
             attire if kind == "staff" else None),
        )
    e["logged"] = True


def _wants_extras(encounter: str) -> bool:
    """Emotion + clothing (~11 ms per frame) are only needed on frames that get logged or shown.
    Matched frames always get them (via extras_min_similarity). Unmatched frames get them only while
    this approach's visitor sighting is still pending, from the frame that will log it (the 3rd) on."""
    e = _encounters.get(encounter) if encounter else None
    return bool(e) and not e.get("logged") and e.get("frames", 0) >= 2


async def recognize(request: Request):
    """Send one camera frame; get back the best matching employee (or none) plus mood/attire.
    A Server-Timing header reports per-stage times (the kiosk's ?debug=1 overlay reads it)."""
    t0 = time.perf_counter()
    upload, form = await read_upload(request)
    encounter = (form.get("encounter") or "").strip()
    t1 = time.perf_counter()
    r = await cf(
        "POST",
        "/recognize",
        params={"limit": 1, "prediction_count": 1, "det_prob_threshold": 0.8,
                "extras": int(_wants_extras(encounter)), "extras_min_similarity": THRESHOLD,
                "learn": int(ADAPTIVE_LEARNING),
                **({"liveness_min_similarity": THRESHOLD, "learn_min_liveness": LIVENESS_THRESHOLD}
                   if LIVENESS != "off" else {})},
        files={"file": upload},
    )
    t2 = time.perf_counter()
    body = r.json()
    resp = _recognize_result(body.get("result", []), (form.get("kiosk") or "Main").strip(), encounter)
    rt = body.get("timing") or {}
    stages = {"upload": (t1 - t0) * 1000, "recognizer": (t2 - t1) * 1000,
              **{k: rt[k] for k in ("decode", "detect", "embed", "match", "extras", "liveness") if k in rt},
              "total": (time.perf_counter() - t0) * 1000}
    resp.headers["Server-Timing"] = ", ".join(f"{k};dur={v:.1f}" for k, v in stages.items())
    return resp


def _recognize_result(faces: list, kiosk: str, encounter: str) -> JSONResponse:
    if not faces:
        return JSONResponse({"face": False, "matched": False})
    face = faces[0]
    base = {"face": True, "size": face.get("size"), "emotion": face.get("emotion"), "attire": face.get("attire"),
            "hivis": face.get("hivis")}
    subjects = face.get("subjects") or []
    sim = float(subjects[0]["similarity"]) if subjects else 0.0
    if not subjects or sim < THRESHOLD:
        if encounter:
            _log_sighting(encounter, kiosk, None, face, True)
        if not mood_allowed("visitor"):
            base["emotion"] = None
        return JSONResponse({**base, "matched": False, "similarity": round(sim, 3) if subjects else None})
    employee = subjects[0]["subject"]
    live = face.get("liveness")
    verdict = _liveness_verdict(encounter, employee, live)
    if verdict != "live":
        if verdict == "spoof":
            _spoof_alert(encounter, employee, kiosk)
        if LIVENESS != "monitor":
            # a photo/screen of a staff member: say nothing about who it shows, offer no actions
            return JSONResponse({**base, "emotion": None, "matched": False, "liveness": verdict, "liveness_score": live})
    with db() as conn:
        last = last_action_today(conn, employee)
        prof = profile(conn, employee)
        consent = bool(prof.get("mood_consent", 1))
        k = kiosk_row(conn, kiosk)
        # at a muster point safety comes first: an expired pass never locks the screen or blocks a check-in
        expired = pass_expired(prof) and k.get("mode") != "muster"
        gate = access.state(conn, employee, k, now()) if access.is_gate(k) else None
        if expired:
            pass_incident(conn, employee, prof, kiosk)
    if encounter:
        _log_sighting(encounter, kiosk, employee, face, consent)
    hv = face.get("hivis")   # None: torso not in frame — keep the last real reading rather than forget it
    if hv is not None:
        if len(_hivis_seen) > 500:
            for k in [k for k, v in _hivis_seen.items() if time.time() - v[1] > HIVIS_FRESH_S]:
                _hivis_seen.pop(k, None)
        _hivis_seen[(employee, kiosk)] = (float(hv), time.time())
    if not mood_allowed("staff", consent):
        base["emotion"] = None
    if expired:  # the kiosk locks the screen; no actions are offered
        return JSONResponse({**base, "matched": True, "employee": employee, "similarity": round(sim, 3),
                             "pass_expired": True, "pass_expiry": prof.get("pass_expiry"), "suggested": None})
    last_action = last["action"] if last else None
    return JSONResponse(
        {
            **base,
            "matched": True,
            "employee": employee,
            "similarity": round(sim, 3),
            "last_action": last_action,
            "last_ts": last["ts"] if last else None,
            "suggested": NEXT_ACTION.get(last_action, "IN"),
            "gate": gate,
            "liveness_score": live,
            "token": event_token(employee, kiosk),
        }
    )


def _save_interaction(conn, *, kiosk, person, encounter, channel, lang=None, user_text=None,
                      reply=None, intent=None, sentiment=None, mood=None, engine=None):
    ts = now()
    conn.execute(
        """INSERT INTO interactions (ts, day, kiosk, person, kind, encounter, channel, lang, user_text, reply, intent, sentiment, mood, engine)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (stamp(ts), ts.date().isoformat(), kiosk, person, "staff" if person else "visitor", encounter, channel,
         lang, user_text, reply, intent, sentiment, mood, engine),
    )


async def greet(request: Request):
    """Personal greeting for a recognised staff member (and, with action, the after-tap line)."""
    p = await request.json()
    employee = (p.get("employee") or "").strip()
    if not employee:
        raise HTTPException(400, "employee required")
    kiosk = (p.get("kiosk") or "Main").strip()
    today = date.today()
    with db() as conn:
        prof = profile(conn, employee)
        stats = insights.person_stats(conn, employee, today, prof)
        since = stamp(now() - timedelta(days=3))
        mems = [dict(m) for m in conn.execute(
            "SELECT * FROM memories WHERE person=? AND ts>=? AND kind!='note' ORDER BY id DESC LIMIT 3", (employee, since))]
        # mention a wellbeing memory only once
        greeted_about = conn.execute(
            "SELECT 1 FROM interactions WHERE person=? AND channel='greeting' AND ts>=? AND reply LIKE '%last time%' LIMIT 1",
            (employee, since)).fetchone()
    consent = bool(prof.get("mood_consent", 1)) and mood_allowed("staff")
    first = prof.get("nickname") or employee.split(" ")[0]
    g = await brain.compose_greeting({
        "first": first, "now": now(), "profile": prof, "mood": p.get("mood"), "mood_consent": consent,
        "attire": p.get("attire"), "action": p.get("action"), "stats": stats,
        "memories": [] if greeted_about else mems,
    })
    with db() as conn:
        _save_interaction(conn, kiosk=kiosk, person=employee, encounter=p.get("encounter"), channel="greeting",
                          lang="en", reply=" ".join([g["headline"]] + g["lines"]),
                          intent=f"greet:{p.get('action') or 'arrive'}", mood=p.get("mood") if consent else None,
                          engine="jev+templates" if brain.engines()["jev"] else "templates")
    return JSONResponse({**g, "language": prof.get("language") or "en"})


async def record_event(request: Request):
    payload = await request.json()
    employee = (payload.get("employee") or "").strip()
    action = (payload.get("action") or "").upper()
    kiosk = (payload.get("kiosk") or "").strip() or None
    similarity = payload.get("similarity")
    if not employee:
        raise HTTPException(400, "employee required")
    if action not in ACTIONS:
        raise HTTPException(400, f"action must be one of {ACTIONS}")
    if not event_token_ok(payload.get("token"), employee, (payload.get("kiosk") or "Main").strip()):
        raise HTTPException(403, "Not verified by the camera — please step in front of the kiosk again. "
                                 "If this keeps happening, reload this screen.")
    ts = now()
    with db() as conn:
        if kiosk:
            offered = access.kiosk_actions(kiosk_row(conn, kiosk))
            if action not in offered:
                raise HTTPException(400, f"{action} isn't available at {kiosk}" +
                                    (f" — use {' / '.join(offered)}." if offered else " — this location records area entry and exit only."))
        refuse_expired_pass(conn, employee, kiosk)
        refuse_unsafe_entry(conn, employee, kiosk, action)
        last = last_action_today(conn, employee)
        if last and last["action"] == action:
            if ts - datetime.fromisoformat(last["ts"]) < timedelta(seconds=DUP_WINDOW):
                return JSONResponse({"ok": True, "duplicate": True, "ts": last["ts"]})
        consent = bool(profile(conn, employee).get("mood_consent", 1)) and mood_allowed("staff")
        conn.execute(
            "INSERT INTO events (ts, day, employee, action, kiosk, similarity, mood, attire) VALUES (?,?,?,?,?,?,?,?)",
            (stamp(ts), ts.date().isoformat(), employee, action, kiosk, similarity,
             payload.get("mood") if consent else None, payload.get("attire")),
        )
    return JSONResponse({"ok": True, "duplicate": False, "ts": stamp(ts)})


async def gate_pass(request: Request):
    """Hands-free pass through a gate-mode location: the camera saw `employee` live (token), the server
    decides IN or OUT from the gate's direction and where they are now. Never touches attendance."""
    p = await request.json()
    employee = (p.get("employee") or "").strip()
    kiosk = (p.get("kiosk") or "").strip()
    if not employee or not kiosk:
        raise HTTPException(400, "employee and kiosk required")
    if not event_token_ok(p.get("token"), employee, kiosk):
        raise HTTPException(403, "Not verified by the camera — please look at the screen again.")
    with db() as conn:
        k = kiosk_row(conn, kiosk)
        if not access.is_gate(k):
            raise HTTPException(400, f"{kiosk} is not set up as a gate")
        refuse_expired_pass(conn, employee, kiosk)
        try:
            out = access.record(conn, employee, k, now(), similarity=p.get("similarity"), reason=p.get("reason"))
        except access.ReasonRequired as e:
            raise HTTPException(400, str(e))
        out["count"] = access.area_count(conn, out["area"], now())
        if out.get("flag") and alerts.gate_incident(conn, out["flag"], employee, profile(conn, employee).get("department"),
                                                    kiosk, out["area"], out.get("zone"), out.get("min_zone"), out.get("missed_exit")):
            task = asyncio.create_task(alerts.deliver())   # after this block commits the alert
            _bg.add(task)
            task.add_done_callback(_bg.discard)
    return JSONResponse({"ok": True, **out})


async def _converse(text: str, p: dict) -> dict:
    kiosk = (p.get("kiosk") or "Main").strip()
    employee = (p.get("employee") or "").strip() or None
    encounter = p.get("encounter")
    today = date.today()
    with db() as conn:
        k = kiosk_row(conn, kiosk)
        ctx = {"location": k.get("zone") or k.get("headline") or kiosk, "cards": k["info"], "now": now().strftime("%A %H:%M"),
               "person": employee, "mood": p.get("mood"), "lang_hint": p.get("lang_hint"),
               "flights": await flights.all_flights()}
        if employee:
            ctx["profile"] = profile(conn, employee)
            ctx["attendance_text"] = insights.attendance_text(conn, employee, today)
            ctx["memories"] = [dict(m) for m in conn.execute(
                "SELECT fact, ts FROM memories WHERE person=? ORDER BY id DESC LIMIT 8", (employee,))]
            ctx["history"] = [dict(h) for h in reversed(conn.execute(
                "SELECT ts, user_text, reply FROM interactions WHERE person=? AND channel!='greeting' ORDER BY id DESC LIMIT 12",
                (employee,)).fetchall())]
        elif encounter:  # visitors: only this conversation, nothing from before
            ctx["history"] = [dict(h) for h in conn.execute(
                "SELECT ts, user_text, reply FROM interactions WHERE encounter=? ORDER BY id", (encounter,))]
    out = await brain.reply(text, ctx)
    with db() as conn:
        _save_interaction(conn, kiosk=kiosk, person=employee, encounter=encounter, channel=p.get("channel") or "text",
                          lang=out["lang"], user_text=text, reply=out["reply"], intent=out["intent"],
                          sentiment=out["sentiment"], mood=p.get("mood"), engine=out["engine"])
        for f in out.pop("facts", []):
            conn.execute("INSERT INTO memories (person, ts, fact, kind) VALUES (?,?,?,?)",
                         (employee, stamp(), f["fact"], f["kind"]))
    return out


async def talk(request: Request):
    p = await request.json()
    text = (p.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    return JSONResponse(await _converse(text[:1000], p))


async def listen(request: Request):
    """Voice clip -> listener (Whisper) -> transcript + detected language -> reply."""
    upload, form = await read_upload(request)
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(f"{LISTENER_URL}/transcribe", files={"file": upload})
        heard = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(503, f"Speech service unavailable: {e.__class__.__name__}")
    text = (heard.get("text") or "").strip()
    if not text:
        return JSONResponse({"transcript": "", "reply": None})
    lang = heard.get("language") if (heard.get("probability") or 0) >= 0.5 else None
    p = {k: form.get(k) for k in ("kiosk", "employee", "encounter", "mood")}
    p.update(channel="voice", lang_hint=lang)
    out = await _converse(text[:1000], p)
    return JSONResponse({"transcript": text, "heard_language": heard.get("language"), **out})


async def kiosk_config(request: Request):
    name = request.path_params["name"]
    today = date.today().isoformat()
    ts = now()
    with db() as conn:
        k = kiosk_row(conn, name)
        conn.execute("UPDATE kiosks SET last_seen=? WHERE name=?", (stamp(ts), name))
        media = [dict(m) for m in conn.execute(
            "SELECT id, file, caption, mime FROM media WHERE kiosk IN (?, '*') ORDER BY position, id", (name,))]
        ann = [dict(a) for a in conn.execute(
            """SELECT text, level FROM announcements WHERE kiosk IN (?, '*')
               AND (starts IS NULL OR starts='' OR starts<=?) AND (ends IS NULL OR ends='' OR ends>=?) ORDER BY id DESC""",
            (name, stamp(ts), stamp(ts)))]
        on_duty = conn.execute(
            """SELECT COUNT(*) FROM (SELECT e.action FROM events e JOIN (SELECT employee, MAX(id) id FROM events WHERE day=? GROUP BY employee) m
               ON m.id=e.id) WHERE action IN ('IN','BACK')""", (today,)).fetchone()[0]
        vis = conn.execute("SELECT AVG(valence), COUNT(*) FROM sightings WHERE day=? AND kind='visitor' AND kiosk=?", (today, name)).fetchone()
        allv = conn.execute("SELECT AVG(valence) FROM sightings WHERE day=? AND valence IS NOT NULL", (today,)).fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM interactions WHERE day=? AND kiosk=? AND channel!='greeting'", (today, name)).fetchone()[0]
        k["offered"] = list(access.kiosk_actions(k))
        if access.is_gate(k):
            k["inside"] = access.area_count(conn, k["area"].strip(), ts)
        if k.get("mode") == "muster":
            mu = muster.active(conn)
            k["muster"] = {"id": mu["id"], "started": mu["started"], **muster.totals(conn, mu["id"])} if mu else None
    for m in media:
        m["url"] = f"/media/{m['file']}"
    return JSONResponse({
        "kiosk": k, "media": media, "announcements": ann, "airport": brain.AIRPORT, "wifi": brain.WIFI,
        "pulse": {"on_duty": on_duty, "visitor_mood": vis[0], "visitors_today": vis[1], "mood_today": allv, "chats_today": chats},
        "features": {"listener": True, "mood": MOOD_TRACKING != "off", "flights": flights.PROVIDER != "off",
                     "assist": list(phrases.ASSIST_KINDS)},
    })


async def flight_board(request: Request):
    kiosk = request.query_params.get("kiosk")
    direction = request.query_params.get("direction") or "both"
    if kiosk:
        with db() as conn:
            direction = kiosk_row(conn, kiosk).get("flights") or "both"
    if direction == "off":
        return JSONResponse({"departures": [], "arrivals": [], "off": True})
    return JSONResponse(await flights.board(direction, int(request.query_params.get("limit", 8))))


# ---- assistance requests
_bg: set = set()  # keeps webhook tasks referenced until they finish

def _request_public(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "status": r["status"], "ts": r["ts"], "ack_ts": r["ack_ts"],
            "assigned": (r["assigned_to"] or "").split(" ")[0] or None}


async def _notify(req: dict):
    if not ASSIST_WEBHOOK_URL:
        return
    icon, label = phrases.ASSIST_KINDS.get(req["kind"], ("💬", req["kind"]))
    text = f"{icon} Assistance #{req['id']} — {label} at {req['kiosk']}" + (f": {req['details']}" if req.get("details") else "")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(ASSIST_WEBHOOK_URL, json={"text": text, "request": req})
    except httpx.HTTPError as e:
        print("assist webhook failed", e.__class__.__name__)


async def assist_create(request: Request):
    p = await request.json()
    kind = p.get("kind") or "other"
    if kind not in phrases.ASSIST_KINDS:
        raise HTTPException(400, f"kind must be one of {list(phrases.ASSIST_KINDS)}")
    ts = now()
    kiosk = (p.get("kiosk") or "Main").strip()
    with db() as conn:
        k = kiosk_row(conn, kiosk)
        # one open request per approach and kind: repeated taps don't create duplicates
        dup = conn.execute("SELECT * FROM requests WHERE encounter=? AND kind=? AND status IN ('open','acknowledged') AND encounter IS NOT NULL",
                           (p.get("encounter"), kind)).fetchone()
        if dup:
            return JSONResponse(_request_public(dup))
        cur = conn.execute(
            "INSERT INTO requests (ts, day, kiosk, kind, details, lang, person, encounter, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (stamp(ts), ts.date().isoformat(), kiosk, kind, (p.get("details") or "").strip()[:500] or None,
             p.get("lang"), (p.get("employee") or "").strip() or None, p.get("encounter"), p.get("source") or "button"))
        r = conn.execute("SELECT * FROM requests WHERE id=?", (cur.lastrowid,)).fetchone()
    req = dict(r)
    req["location"] = k.get("headline") or kiosk
    task = asyncio.create_task(_notify(req))
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return JSONResponse(_request_public(r))


async def assist_status(request: Request):
    with db() as conn:
        r = conn.execute("SELECT * FROM requests WHERE id=?", (int(request.path_params["req_id"]),)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    return JSONResponse(_request_public(r))


async def assist_cancel(request: Request):
    """The traveller changed their mind (only while nobody has picked it up)."""
    with db() as conn:
        conn.execute("UPDATE requests SET status='cancelled', done_ts=? WHERE id=? AND status='open'",
                     (stamp(), int(request.path_params["req_id"])))
    return JSONResponse({"ok": True})


async def requests_list(request: Request):
    require_staff(request)
    statuses = [x for x in (request.query_params.get("status") or "open,acknowledged").split(",") if x]
    date_from, date_to = _range(request, default_days=0)
    live = [x for x in statuses if x in ("open", "acknowledged")]
    closed = [x for x in statuses if x in ("done", "cancelled")]
    # live requests regardless of date; closed ones only within the date range — one call for the whole board
    conds, args = [], []
    if live:
        conds.append(f"status IN ({','.join('?' * len(live))})")
        args += live
    if closed:
        conds.append(f"(status IN ({','.join('?' * len(closed))}) AND day BETWEEN ? AND ?)")
        args += closed + [date_from, date_to]
    sql = "SELECT r.*, COALESCE(k.zone, r.kiosk) AS location FROM requests r LEFT JOIN kiosks k ON k.name = r.kiosk WHERE (" + (" OR ".join(conds) or "0") + ")"
    if request.query_params.get("kiosk"):
        sql += " AND r.kiosk=?"
        args.append(request.query_params["kiosk"])
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY r.id DESC LIMIT 300", args)]
        staff = [r["name"] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    return JSONResponse({"rows": rows, "kinds": {k: {"icon": v[0], "label": v[1]} for k, v in phrases.ASSIST_KINDS.items()},
                         "staff": staff, "now": stamp(), "sla_min": int(os.environ.get("ASSIST_SLA_MINUTES", "5"))})


async def request_update(request: Request):
    require_staff(request)
    rid = int(request.path_params["req_id"])
    p = await request.json()
    status = p.get("status")
    if status and status not in ("open", "acknowledged", "done", "cancelled"):
        raise HTTPException(400, "bad status")
    with db() as conn:
        r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        fields = {}
        if "assigned_to" in p:
            fields["assigned_to"] = (p["assigned_to"] or "").strip() or None
        if "notes" in p:
            fields["notes"] = p["notes"]
        if status:
            fields["status"] = status
            if status == "acknowledged" and not r["ack_ts"]:
                fields["ack_ts"] = stamp()
            if status in ("done", "cancelled"):
                fields["done_ts"] = stamp()   # no ack_ts invented: skipped acknowledgements don't fake 0-min responses
            if status == "open":
                fields.update(ack_ts=None, done_ts=None)
        if fields:
            conn.execute(f"UPDATE requests SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), rid))
    return JSONResponse({"ok": True})


# ---- ramp safety pack
async def _post_webhook(url: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(url, json=payload)
    except httpx.HTTPError as e:
        print("webhook failed", e.__class__.__name__)


async def safety_precheck(request: Request):
    p = await request.json()
    employee = (p.get("employee") or "").strip()
    if not employee:
        raise HTTPException(400, "employee required")
    with db() as conn:
        refuse_expired_pass(conn, employee, (p.get("kiosk") or "").strip() or None)
        return JSONResponse(safety.precheck(conn, employee, profile(conn, employee),
                                            camera_hivis(employee, (p.get("kiosk") or "Main").strip()),
                                            kiosk=(p.get("kiosk") or "").strip() or None, action=p.get("action") or "IN"))


async def safety_check(request: Request):
    p = await request.json()
    employee = (p.get("employee") or "").strip()
    if not employee:
        raise HTTPException(400, "employee required")
    rested = p.get("rested")
    rested = int(rested) if rested in (1, 2, 3, 4, 5, "1", "2", "3", "4", "5") else None
    kiosk = (p.get("kiosk") or "").strip() or None
    # same proof as /api/event: a check that clears an enforced entry must come from the camera, not a script
    if not event_token_ok(p.get("token"), employee, kiosk or "Main"):
        raise HTTPException(403, "Not verified by the camera — please step in front of the kiosk again.")
    with db() as conn:
        refuse_expired_pass(conn, employee, kiosk)
        prof = profile(conn, employee)
        try:
            out = safety.record(conn, employee, prof, kiosk, p.get("items") or {}, camera_hivis(employee, kiosk or "Main"), rested,
                                action=p.get("action") or "IN", auto=bool(p.get("auto")))
        except ValueError as e:
            raise HTTPException(409, str(e))
    if out["result"] == "flagged" and SAFETY_WEBHOOK_URL:
        bits = []
        if out["missing"]:
            bits.append("missing " + ", ".join(safety.PPE[m][1] for m in out["missing"]))
        if out["fatigue"] and out["fatigue"]["level"] == "high":
            bits.append(f"HIGH fatigue risk ({out['fatigue']['score']}): " + "; ".join(f["text"] for f in out["fatigue"]["factors"]))
        task = asyncio.create_task(_post_webhook(SAFETY_WEBHOOK_URL, {
            "text": (f"⛔ Entry refused — {employee} ({prof.get('department') or '—'}) at {out['location']}: " if out["blocked"] else
                     f"⚠️ Safety check — {employee} ({prof.get('department') or '—'}) at {kiosk}: ") + " · ".join(bits),
            "safety": {"person": employee, **out}}))
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    return JSONResponse(out)


async def safety_rules(request: Request):
    require_pin(request)
    with db() as conn:
        if request.method == "PUT":
            dept = request.path_params.get("department", "").strip()
            if not dept:
                raise HTTPException(400, "department required")
            safety.save_rule(conn, dept, await request.json())
            return JSONResponse({"ok": True})
        depts = [r[0] for r in conn.execute("SELECT DISTINCT department FROM people WHERE department IS NOT NULL AND department!='' ORDER BY 1")]
        return JSONResponse({"rules": safety.all_rules(conn), "departments": depts, "locations": safety.all_locations(conn),
                             "catalog": {k: {"icon": v[0], "label": v[1]} for k, v in safety.PPE.items()}})


async def safety_rule_delete(request: Request):
    require_pin(request)
    with db() as conn:
        conn.execute("DELETE FROM safety_rules WHERE department=?", (request.path_params["department"],))
        conn.execute("UPDATE kiosks SET safety_rule=NULL WHERE safety_rule=?", (request.path_params["department"],))
    return JSONResponse({"ok": True})


# ---- roster
async def roster_list(request: Request):
    require_pin(request)
    date_from, date_to = _range(request, default_days=0)
    person = request.query_params.get("person") or None
    dept = request.query_params.get("department") or None
    with db() as conn:
        rows = roster.adherence(conn, date_from, date_to)
        people = [dict(r) for r in conn.execute("SELECT name, department, role FROM people ORDER BY name COLLATE NOCASE")]
    rows = [r for r in rows if (not person or r["person"] == person) and (not dept or r["department"] == dept)]
    return JSONResponse({"shifts": rows, "people": people, "from": date_from, "to": date_to})


async def roster_import(request: Request):
    """CSV upload (multipart 'file'); replace=1 (default) swaps existing shifts for the same person+day."""
    require_pin(request)
    form = await request.form()
    up = form.get("file")
    if up is None:
        raise HTTPException(400, "file field required")
    raw = await up.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    shifts, errors = roster.parse_csv(text)
    if request.query_params.get("dry") in ("1", "true"):
        return JSONResponse({"preview": shifts[:50], "count": len(shifts), "errors": errors})
    unknown = _unknown_people(shifts)  # before saving: save() creates profiles for rostered departments
    with db() as conn:
        out = roster.save(conn, shifts, "csv", replace=form.get("replace", "1") in ("1", "true", "on"))
    return JSONResponse({**out, "errors": errors, "unknown_people": unknown})


def _unknown_people(shifts) -> list:
    with db() as conn:
        known = {r["name"] for r in conn.execute("SELECT name FROM people")}
    return sorted({s["person"] for s in shifts} - known)


async def roster_push(request: Request):
    """JSON push from an HR / rostering system: {"shifts": [{person, day, start, end, position?, location?, department?}], "replace": true}"""
    require_pin(request)
    p = await request.json()
    shifts, errors = roster.validate(p.get("shifts") if isinstance(p, dict) else p)
    unknown = _unknown_people(shifts)
    with db() as conn:
        out = roster.save(conn, shifts, "api", replace=(p.get("replace", True) if isinstance(p, dict) else True))
    return JSONResponse({**out, "errors": errors, "unknown_people": unknown})


async def roster_shift(request: Request):
    """POST = add one shift, PUT /{id} = edit, DELETE /{id} = remove."""
    require_pin(request)
    with db() as conn:
        if request.method == "DELETE":
            conn.execute("DELETE FROM shifts WHERE id=?", (int(request.path_params["shift_id"]),))
            return JSONResponse({"ok": True})
        shifts, errors = roster.validate([await request.json()])
        if errors:
            raise HTTPException(400, errors[0])
        s1 = shifts[0]
        if request.method == "PUT":
            conn.execute("UPDATE shifts SET person=?, day=?, start=?, end=?, position=?, location=?, department=?, notes=?, source='manual' WHERE id=?",
                         (s1["person"], s1["day"], s1["start"], s1["end"], s1.get("position"), s1.get("location"),
                          s1.get("department"), s1.get("notes"), int(request.path_params["shift_id"])))
        else:
            roster.save(conn, shifts, "manual", replace=False)
    return JSONResponse({"ok": True})


async def roster_template(request: Request):
    d = date.today()
    body = "name,date,start,end,position,location,department\n" + "\n".join(
        f"Aishath Shiuna,{(d + timedelta(days=i)).isoformat()},07:00,16:00,Immigration desk 3,Arrivals-Hall,Immigration" for i in range(3)) + \
        f"\nAhmed Naseem,{d.isoformat()},22:00,06:30,Ramp lead night,Apron,Ground Handling\n"
    return PlainTextResponse(body, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="roster_template.csv"'})


async def roster_coverage(request: Request):
    require_pin(request)
    day = request.query_params.get("day") or date.today().isoformat()
    with db() as conn:
        return JSONResponse({"day": day, "department": request.query_params.get("department"),
                             "hours": roster.coverage(conn, day, request.query_params.get("department") or None)})


# ---- supervisor alerts
async def alerts_list(request: Request):
    require_staff(request)
    statuses = [x for x in (request.query_params.get("status") or "new,ack").split(",") if x]
    date_from, date_to = _range(request, default_days=6)
    with db() as conn:
        alerts.scan(conn)  # make the feed current even between background scans
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM alerts WHERE status IN ({','.join('?' * len(statuses))}) AND day BETWEEN ? AND ? ORDER BY id DESC LIMIT 300",
            (*statuses, date_from, date_to))]
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM alerts WHERE day>=? GROUP BY status",
                                                              ((date.today() - timedelta(days=6)).isoformat(),))}
    return JSONResponse({"rows": rows, "counts": counts, "settings": alerts.settings(), "now": stamp()})


async def alert_update(request: Request):
    require_staff(request)
    p = await request.json()
    status = p.get("status")
    if status not in ("new", "ack", "resolved"):
        raise HTTPException(400, "status must be new, ack or resolved")
    with db() as conn:
        aid = int(request.path_params["alert_id"])
        if status == "new":  # reopened: nobody owns it any more
            conn.execute("UPDATE alerts SET status='new', ack_by=NULL, ack_ts=NULL, notes=COALESCE(?, notes) WHERE id=?", (p.get("notes"), aid))
        else:
            conn.execute("UPDATE alerts SET status=?, ack_by=COALESCE(?, ack_by), ack_ts=COALESCE(ack_ts, ?), notes=COALESCE(?, notes) WHERE id=?",
                         (status, p.get("by"), stamp(), p.get("notes"), aid))
    return JSONResponse({"ok": True})


async def alert_routes(request: Request):
    require_pin(request)
    with db() as conn:
        if request.method == "PUT":
            p = await request.json()
            dept = request.path_params["department"]
            conn.execute("INSERT INTO alert_routes (department, supervisor, webhook) VALUES (?,?,?) "
                         "ON CONFLICT(department) DO UPDATE SET supervisor=excluded.supervisor, webhook=excluded.webhook",
                         (dept, (p.get("supervisor") or "").strip() or None, (p.get("webhook") or "").strip() or None))
            return JSONResponse({"ok": True})
        if request.method == "DELETE":
            conn.execute("DELETE FROM alert_routes WHERE department=?", (request.path_params["department"],))
            return JSONResponse({"ok": True})
        return JSONResponse({"routes": [dict(r) for r in conn.execute("SELECT * FROM alert_routes ORDER BY department")],
                             "departments": [r[0] for r in conn.execute("SELECT DISTINCT department FROM people WHERE department IS NOT NULL AND department!='' ORDER BY 1")]})


# ----------------------------------------------------------------------------- admin API
async def status(request: Request):
    """Current state of every employee who has an event on the given day (default today)."""
    require_pin(request)
    day = request.query_params.get("day") or date.today().isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT e.employee, e.action, e.ts, e.kiosk, e.mood
               FROM events e
               JOIN (SELECT employee, MAX(id) AS id FROM events WHERE day=? GROUP BY employee) m
                 ON m.id = e.id
               ORDER BY e.employee""",
            (day,),
        ).fetchall()
    return JSONResponse({"day": day, "rows": [dict(r) for r in rows]})


async def events(request: Request):
    require_pin(request)
    date_from, date_to = _range(request)
    employee = request.query_params.get("employee")
    limit = int(request.query_params.get("limit", 500))
    sql = "SELECT * FROM events WHERE day BETWEEN ? AND ?"
    args = [date_from, date_to]
    if employee:
        sql += " AND employee = ?"
        args.append(employee)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return JSONResponse({"rows": [dict(r) for r in rows]})


async def export_csv(request: Request):
    require_pin(request)  # PIN comes as ?pin= so the browser can open it as a download link
    date_from, date_to = _range(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, employee, action, kiosk, similarity, mood, attire FROM events WHERE day BETWEEN ? AND ? ORDER BY id",
            (date_from, date_to),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "employee", "action", "kiosk", "similarity", "mood", "attire"])
    for r in rows:
        w.writerow([r["ts"], r["employee"], r["action"], r["kiosk"] or "", r["similarity"] or "", r["mood"] or "", r["attire"] or ""])
    fname = f"attendance_{date_from}_to_{date_to}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


async def delete_event(request: Request):
    require_pin(request)
    with db() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (int(request.path_params["event_id"]),))
    return JSONResponse({"ok": True})


def _cover_score(f: dict, today: date) -> tuple:
    """Rank a photo as someone's profile picture: kiosk daily bests first (they show the current
    look), each scored by quality less 0.05 per day of age so a recent good photo beats an older
    slightly sharper one; enrolled photos after them, newest first."""
    if f.get("kind") == "learned":
        try:
            age = (today - date.fromisoformat(f.get("day") or "")).days
        except ValueError:
            age = 7
        return (1, float(f.get("quality") or 0) - 0.05 * max(0, age), f.get("ts") or "")
    return (0, 0.0, f.get("ts") or "")


async def employees(request: Request):
    require_pin(request)
    subjects = (await cf("GET", "/subjects")).json().get("subjects", [])
    faces = (await cf("GET", "/faces", params={"size": 10000, "details": 1})).json().get("faces", [])
    counts: dict = {}
    learned: dict = {}
    covers: dict = {}
    today = date.today()
    for f in faces:
        c = learned if f.get("kind") == "learned" else counts
        c[f["subject"]] = c.get(f["subject"], 0) + 1
        if f.get("has_image"):
            s, key = f["subject"], _cover_score(f, today)
            if s not in covers or key > covers[s][0]:
                covers[s] = (key, f["image_id"])
    with db() as conn:
        profs = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM people")}
    return JSONResponse(
        {"employees": [{"name": s, "photos": counts.get(s, 0), "learned": learned.get(s, 0),
                        "cover": covers[s][1] if s in covers else None, "profile": profs.get(s)}
                       for s in sorted(subjects, key=str.lower)]}
    )


async def employee_photos(request: Request):
    """One person's photos: enrolled ones and the daily bests learned at the kiosk (newest first)."""
    require_pin(request)
    name = request.path_params["name"]
    if not await _enrolled(name):
        return JSONResponse({"photos": [], "learning": ADAPTIVE_LEARNING})
    faces = (await cf("GET", "/faces", params={"subject": name})).json().get("faces", [])
    enrolled = [f for f in faces if f.get("kind") != "learned"]
    learned = sorted((f for f in faces if f.get("kind") == "learned"), key=lambda f: f.get("ts") or "", reverse=True)
    return JSONResponse({"photos": enrolled + learned, "learning": ADAPTIVE_LEARNING})


async def employee_photo_image(request: Request):
    require_pin(request)
    r = await cf("GET", f"/faces/{request.path_params['image_id']}/img", params={"subject": request.path_params["name"]})
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def employee_photo_delete(request: Request):
    require_pin(request)
    await cf("DELETE", f"/faces/{request.path_params['image_id']}", params={"subject": request.path_params["name"]})
    return JSONResponse({"ok": True})


async def enrol_photo(request: Request):
    """Add one face photo to an employee (creates the employee if new)."""
    require_pin(request)
    name = request.path_params["name"].strip()
    if not name:
        raise HTTPException(400, "name required")
    with db() as conn:
        missing = missing_enrolment(profile(conn, name))
    if missing:
        raise HTTPException(400, f"Save {', '.join(missing)} for {name} before adding photos")
    upload, _ = await read_upload(request)
    r = await cf("POST", "/faces", params={"subject": name, "det_prob_threshold": 0.8}, files={"file": upload})
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO people (name, created) VALUES (?,?)", (name, stamp()))
    return JSONResponse({"ok": True, "image_id": r.json().get("image_id"), "employee": name})


async def _enrolled(name: str) -> bool:
    return name in (await cf("GET", "/subjects")).json().get("subjects", [])


async def delete_employee(request: Request):
    """Removes face vectors and everything personal (profile, moods, conversations, memories).
    Attendance events are kept for payroll. Works for profile-only (not enrolled) people too."""
    require_pin(request)
    name = request.path_params["name"]
    if await _enrolled(name):
        await cf("DELETE", f"/subjects/{name}")
    with db() as conn:
        for t, col in (("people", "name"), ("sightings", "person"), ("interactions", "person"), ("memories", "person")):
            conn.execute(f"DELETE FROM {t} WHERE {col}=?", (name,))
    return JSONResponse({"ok": True})


async def rename_employee(request: Request):
    require_pin(request)
    name = request.path_params["name"]
    new = ((await request.json()).get("name") or "").strip()
    if not new:
        raise HTTPException(400, "name required")
    if await _enrolled(name):
        await cf("PUT", f"/subjects/{name}", json={"subject": new})
    with db() as conn:
        conn.execute("UPDATE events SET employee=? WHERE employee=?", (new, name))
        conn.execute("UPDATE access SET person=? WHERE person=?", (new, name))
        for t, col in (("people", "name"), ("sightings", "person"), ("interactions", "person"), ("memories", "person")):
            conn.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (new, name))
    return JSONResponse({"ok": True})


PROFILE_FIELDS = ("role", "department", "shift_start", "birthday", "joined", "language", "nickname", "mood_consent", "notes",
                  "record_card", "dob", "pass_expiry", "zone")

# authorised airside zone colours, highest access first
ZONES = access.ZONES
# must be on file before a face can be enrolled
REQUIRED_FOR_ENROL = {"record_card": "record card number", "pass_expiry": "security pass expiry", "zone": "authorised zone"}


def missing_enrolment(prof: dict) -> list:
    return [label for f, label in REQUIRED_FOR_ENROL.items() if not prof.get(f)]


def _iso_date(v, label: str):
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise HTTPException(400, f"{label} must be a date (YYYY-MM-DD)")


async def people_list(request: Request):
    require_pin(request)
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE")]
    return JSONResponse({"people": rows})


async def people_save(request: Request):
    require_pin(request)
    name = request.path_params["name"]
    p = await request.json()
    vals = {f: p.get(f) for f in PROFILE_FIELDS if f in p}
    if "mood_consent" in vals:
        vals["mood_consent"] = 1 if vals["mood_consent"] in (1, True, "1", "true", "on") else 0
    for f in REQUIRED_FOR_ENROL:  # once set, mandatory fields can be changed but not blanked
        if f in vals and not str(vals[f] or "").strip():
            raise HTTPException(400, f"{REQUIRED_FOR_ENROL[f].capitalize()} is required")
    if "record_card" in vals:
        vals["record_card"] = str(vals["record_card"]).strip().upper()
    if "zone" in vals:
        vals["zone"] = str(vals["zone"]).strip().lower()
        if vals["zone"] not in ZONES:
            raise HTTPException(400, "Zone must be one of: " + ", ".join(z.capitalize() for z in ZONES))
    if "dob" in vals:
        vals["dob"] = _iso_date(vals["dob"], "Date of birth")
        if vals["dob"] and vals["dob"] >= date.today().isoformat():
            raise HTTPException(400, "Date of birth must be in the past")
    with db() as conn:
        old = profile(conn, name)
    if vals.get("dob"):  # the kiosk's birthday greeting comes from the date of birth
        vals["birthday"] = vals["dob"][5:]
    elif "dob" in vals and old.get("dob"):  # date of birth removed: drop the birthday derived from it
        vals["birthday"] = None
    if "pass_expiry" in vals:
        vals["pass_expiry"] = _iso_date(vals["pass_expiry"], "Security pass expiry")
    with db() as conn:
        if vals.get("record_card"):
            dup = conn.execute("SELECT name FROM people WHERE record_card=? AND name!=?", (vals["record_card"], name)).fetchone()
            if dup:
                raise HTTPException(409, f"Record card {vals['record_card']} already belongs to {dup['name']}")
        conn.execute("INSERT OR IGNORE INTO people (name, created) VALUES (?,?)", (name, stamp()))
        if vals:
            conn.execute(f"UPDATE people SET {', '.join(f'{k}=?' for k in vals)} WHERE name=?", (*vals.values(), name))
        if vals.get("mood_consent") == 0:  # withdrawing consent also clears past mood readings
            conn.execute("UPDATE sightings SET mood=NULL, valence=NULL, confidence=NULL WHERE person=?", (name,))
            conn.execute("UPDATE events SET mood=NULL WHERE employee=?", (name,))
    return JSONResponse({"ok": True})


async def forget_person(request: Request):
    """Right to be forgotten for conversations: wipes interactions + memories for one person."""
    require_pin(request)
    name = request.path_params["name"]
    with db() as conn:
        conn.execute("DELETE FROM interactions WHERE person=?", (name,))
        conn.execute("DELETE FROM memories WHERE person=?", (name,))
    return JSONResponse({"ok": True})


async def access_presence(request: Request):
    """Who is inside each area right now (gate-mode locations)."""
    require_pin(request)
    with db() as conn:
        areas = access.presence(conn, now())
    return JSONResponse({"areas": areas, "stale_hours": access.STALE_HOURS})


def _access_rows(request: Request):
    date_from, date_to = _range(request)
    sql, args = "SELECT * FROM access WHERE day BETWEEN ? AND ?", [date_from, date_to]
    for key in ("area", "person", "kiosk"):
        if request.query_params.get(key):
            sql += f" AND {key}=?"
            args.append(request.query_params[key])
    with db() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [int(request.query_params.get("limit", 2000))])]


async def area_save(request: Request):
    require_pin(request)
    name = request.path_params["name"].strip()
    p = await request.json()
    if not name:
        raise HTTPException(400, "area name required")
    if p.get("ask_reason") and not [x for x in (p.get("reasons") or []) if str(x).strip()]:
        raise HTTPException(400, "Add at least one reason to ask for")
    with db() as conn:
        try:
            access.save_settings(conn, name, p.get("reasons") or [], bool(p.get("ask_reason")),
                                 p.get("min_zone"), p.get("max_minutes"), p.get("capacity"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return JSONResponse({"ok": True, **access.settings(conn, name)})


def _deliver_alerts_soon():
    task = asyncio.create_task(alerts.deliver())
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def muster_checkin(request: Request):
    """Face check-in at a muster point during an emergency. Expired passes are not refused here."""
    p = await request.json()
    employee, kiosk = (p.get("employee") or "").strip(), (p.get("kiosk") or "").strip()
    if not employee or not kiosk:
        raise HTTPException(400, "employee and kiosk required")
    if not event_token_ok(p.get("token"), employee, kiosk):
        raise HTTPException(403, "Not verified by the camera — please look at the screen again.")
    with db() as conn:
        if kiosk_row(conn, kiosk).get("mode") != "muster":
            raise HTTPException(400, f"{kiosk} is not a muster point")
        return JSONResponse(muster.checkin(conn, employee, kiosk, now()))


async def muster_overview(request: Request):
    require_pin(request)
    with db() as conn:
        m = muster.active(conn)
        areas = sorted({r["area"] for r in conn.execute("SELECT DISTINCT area FROM access")} |
                       {r["area"].strip() for r in conn.execute("SELECT area FROM kiosks WHERE mode='gate' AND area IS NOT NULL AND TRIM(area)!=''")})
        points = [r["name"] for r in conn.execute("SELECT name FROM kiosks WHERE mode='muster' ORDER BY name")]
        return JSONResponse({"active": muster.board(conn, m["id"]) if m else None, "history": muster.history(conn),
                             "areas": areas, "points": points})


async def muster_start(request: Request):
    require_pin(request)
    p = await request.json()
    note = (p.get("note") or "").strip()[:300]
    with db() as conn:
        try:
            out = muster.start(conn, p.get("areas") or [], bool(p.get("on_duty")), note, (p.get("by") or "").strip()[:80], now())
        except ValueError as e:
            raise HTTPException(409, str(e))
        where = "all areas" if out["areas"] == ["*"] else ", ".join(out["areas"])
        alerts._add(conn, f"muster|{out['id']}", "muster",
                    f"EMERGENCY muster started for {where}{' and everyone on duty' if p.get('on_duty') else ''}: "
                    f"{out['total']} people to account for." + (f" {note}" if note else ""), now())
    _deliver_alerts_soon()
    return JSONResponse({"ok": True, **out})


async def muster_mark(request: Request):
    require_pin(request)
    p = await request.json()
    with db() as conn:
        try:
            muster.mark(conn, request.path_params["mid"], (p.get("person") or "").strip(), p.get("safe", True) is not False,
                        (p.get("by") or "").strip()[:80], (p.get("note") or "").strip()[:200], now())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return JSONResponse({"ok": True, **muster.totals(conn, request.path_params["mid"])})


async def muster_end(request: Request):
    require_pin(request)
    p = await request.json()
    with db() as conn:
        try:
            return JSONResponse({"ok": True, **muster.end(conn, request.path_params["mid"], (p.get("by") or "").strip()[:80],
                                                         p.get("mark_out", True) is not False, now())})
        except ValueError as e:
            raise HTTPException(409, str(e))


async def muster_csv(request: Request):
    require_pin(request)
    with db() as conn:
        b = muster.board(conn, request.path_params["mid"])
    if not b:
        raise HTTPException(404, "No such muster")
    buf = io.StringIO()
    w = csv.writer(buf)
    m = b["muster"]
    w.writerow(["muster", m["id"], "started", m["started"], "ended", m["ended"] or "", "areas", " / ".join(m["areas"])])
    w.writerow(["person", "department", "status", "on_list", "last_area", "inside_since", "safe_at", "safe_by", "safe_kiosk", "note"])
    for x in b["people"]:
        w.writerow([x["person"], x["department"] or "", x["status"], "yes" if x["on_list"] else "no (checked in)", x["area"] or "",
                    x["since"] or "", x["safe_ts"] or "", x["safe_by"] or "", x["safe_kiosk"] or "", x["note"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="muster-{m["id"]}.csv"'})


async def access_log(request: Request):
    require_pin(request)
    return JSONResponse({"rows": _access_rows(request)})


async def access_csv(request: Request):
    require_pin(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "person", "area", "direction", "reason", "flag", "kiosk", "similarity", "source", "note"])
    for r in reversed(_access_rows(request)):
        w.writerow([r["ts"], r["person"], r["area"], r["direction"], r["reason"] or "", r["flag"] or "", r["kiosk"] or "",
                    r["similarity"] or "", r["source"], r["note"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="area-access.csv"'})


async def access_out(request: Request):
    """Admin closes someone's presence in an area (missed exit), recorded as a manual OUT."""
    require_pin(request)
    p = await request.json()
    person, area = (p.get("person") or "").strip(), (p.get("area") or "").strip()
    if not person or not area:
        raise HTTPException(400, "person and area required")
    with db() as conn:
        out = access.record(conn, person, {"name": None, "area": area, "direction": "out"}, now(),
                            source="admin", move="OUT", note=(p.get("note") or "Marked out by admin").strip()[:200])
    return JSONResponse({"ok": True, **out})


async def kiosks_list(request: Request):
    require_pin(request)
    with db() as conn:
        rows = []
        for r in conn.execute("SELECT name FROM kiosks ORDER BY name COLLATE NOCASE").fetchall():
            k = kiosk_row(conn, r["name"])
            k["media"] = [dict(m) for m in conn.execute("SELECT * FROM media WHERE kiosk=? ORDER BY position, id", (r["name"],))]
            k["offered"] = list(access.kiosk_actions(k))
            rows.append(k)
        shared = [dict(m) for m in conn.execute("SELECT * FROM media WHERE kiosk='*' ORDER BY position, id")]
    return JSONResponse({"kiosks": rows, "shared_media": shared})


async def kiosk_save(request: Request):
    require_pin(request)
    name = request.path_params["name"]
    p = await request.json()
    with db() as conn:
        kiosk_row(conn, name)
        fields = {k: p[k] for k in ("zone", "headline", "subtitle", "theme", "language") if k in p}
        if p.get("flights") in ("both", "departures", "arrivals", "off"):
            fields["flights"] = p["flights"]
        if "voice" in p:
            fields["voice"] = 1 if p["voice"] else 0
        if "info" in p:
            fields["info"] = json.dumps(p["info"] or [], ensure_ascii=False)
        if "safety_rule" in p:
            rule = (p["safety_rule"] or "").strip() or None
            if rule and rule != safety.DEPT_RULE and not conn.execute("SELECT 1 FROM safety_rules WHERE department=?", (rule,)).fetchone():
                raise HTTPException(400, f"No safety rule named {rule!r}")
            fields["safety_rule"] = rule
        if "safety_enforce" in p:
            fields["safety_enforce"] = 1 if p["safety_enforce"] else 0
        if "mode" in p:
            if p["mode"] not in access.MODES:
                raise HTTPException(400, f"mode must be one of {access.MODES}")
            fields["mode"] = p["mode"]
        if "actions" in p:
            fields["actions"] = access.clean_actions(p["actions"])
        if "area" in p:
            fields["area"] = (p["area"] or "").strip()[:80] or None
        if "direction" in p:
            if p["direction"] not in access.DIRECTIONS:
                raise HTTPException(400, f"direction must be one of {access.DIRECTIONS}")
            fields["direction"] = p["direction"]
        cur = kiosk_row(conn, name)
        if fields.get("mode", cur.get("mode")) == "gate" and not fields.get("area", cur.get("area")):
            raise HTTPException(400, "A gate needs an area (e.g. Departure Hall)")
        if fields:
            conn.execute(f"UPDATE kiosks SET {', '.join(f'{k}=?' for k in fields)} WHERE name=?", (*fields.values(), name))
    return JSONResponse({"ok": True})


async def kiosk_delete(request: Request):
    """Deletes a location and its media. A screen still open on it re-registers it with defaults."""
    require_pin(request)
    name = request.path_params["name"]
    with db() as conn:
        files = [r["file"] for r in conn.execute("SELECT file FROM media WHERE kiosk=?", (name,))]
        conn.execute("DELETE FROM media WHERE kiosk=?", (name,))
        conn.execute("DELETE FROM announcements WHERE kiosk=?", (name,))
        conn.execute("DELETE FROM kiosks WHERE name=?", (name,))
    for f in files:
        try:
            os.remove(os.path.join(MEDIA_DIR, f))
        except OSError:
            pass
    return JSONResponse({"ok": True})


ALLOWED_MEDIA = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4"}


async def media_upload(request: Request):
    require_pin(request)
    kiosk = request.path_params["name"]
    form = await request.form()
    up = form.get("file")
    if up is None:
        raise HTTPException(400, "file field required")
    mime = up.content_type or ""
    if mime not in ALLOWED_MEDIA:
        raise HTTPException(400, f"Unsupported file type {mime}; use JPG, PNG, WebP, GIF or MP4")
    fname = uuid.uuid4().hex + ALLOWED_MEDIA[mime]
    with open(os.path.join(MEDIA_DIR, fname), "wb") as f:
        f.write(await up.read())
    with db() as conn:
        conn.execute("INSERT INTO media (kiosk, file, caption, mime, created) VALUES (?,?,?,?,?)",
                     (kiosk, fname, (form.get("caption") or "").strip(), mime, stamp()))
    return JSONResponse({"ok": True, "file": fname})


async def media_delete(request: Request):
    require_pin(request)
    mid = int(request.path_params["media_id"])
    with db() as conn:
        r = conn.execute("SELECT file FROM media WHERE id=?", (mid,)).fetchone()
        conn.execute("DELETE FROM media WHERE id=?", (mid,))
    if r:
        try:
            os.remove(os.path.join(MEDIA_DIR, r["file"]))
        except OSError:
            pass
    return JSONResponse({"ok": True})


async def media_caption(request: Request):
    require_pin(request)
    p = await request.json()
    with db() as conn:
        conn.execute("UPDATE media SET caption=COALESCE(?, caption), position=COALESCE(?, position) WHERE id=?",
                     (p.get("caption"), p.get("position"), int(request.path_params["media_id"])))
    return JSONResponse({"ok": True})


async def announcements(request: Request):
    require_pin(request)
    if request.method == "POST":
        p = await request.json()
        if not (p.get("text") or "").strip():
            raise HTTPException(400, "text required")
        if (p.get("level") or "info") not in ("info", "alert"):
            raise HTTPException(400, "level must be info or alert")
        for f in ("starts", "ends"):
            if p.get(f):
                try:
                    p[f] = datetime.fromisoformat(p[f]).isoformat(timespec="seconds")
                except ValueError:
                    raise HTTPException(400, f"{f} must be an ISO date-time")
        with db() as conn:
            conn.execute("INSERT INTO announcements (kiosk, text, level, starts, ends, created) VALUES (?,?,?,?,?,?)",
                         (p.get("kiosk") or "*", p["text"].strip(), p.get("level") or "info", p.get("starts"), p.get("ends"), stamp()))
        return JSONResponse({"ok": True})
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM announcements ORDER BY id DESC")]
    return JSONResponse({"announcements": rows})


async def announcement_delete(request: Request):
    require_pin(request)
    with db() as conn:
        conn.execute("DELETE FROM announcements WHERE id=?", (int(request.path_params["ann_id"]),))
    return JSONResponse({"ok": True})


async def insights_overview(request: Request):
    require_pin(request)
    date_from, date_to = _range(request, default_days=29)
    return JSONResponse(insights.overview(date_from, date_to, request.query_params.get("kiosk") or None))


async def mood_today(request: Request):
    require_pin(request)
    day = request.query_params.get("day") or date.today().isoformat()
    days = max(2, min(int(request.query_params.get("days", 14)), 90))
    return JSONResponse(insights.mood_today(day, days))


async def insights_person(request: Request):
    require_pin(request)
    date_from, date_to = _range(request, default_days=29)
    return JSONResponse(insights.person(request.path_params["name"], date_from, date_to))


def _interaction_query(request: Request):
    date_from, date_to = _range(request, default_days=6)
    sql = "SELECT * FROM interactions WHERE day BETWEEN ? AND ?"
    args = [date_from, date_to]
    for key, col in (("person", "person"), ("kiosk", "kiosk"), ("kind", "kind"), ("channel", "channel"), ("lang", "lang"), ("intent", "intent")):
        v = request.query_params.get(key)
        if v:
            sql += f" AND {col}=?"
            args.append(v)
    if request.query_params.get("talk") in ("1", "true"):
        sql += " AND channel != 'greeting'"
    q = request.query_params.get("q")
    if q:
        sql += " AND (user_text LIKE ? OR reply LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    return sql, args


async def interactions_list(request: Request):
    require_pin(request)
    sql, args = _interaction_query(request)
    limit = min(int(request.query_params.get("limit", 300)), 2000)
    with db() as conn:
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    return JSONResponse({"rows": [dict(r) for r in rows]})


async def interactions_csv(request: Request):
    require_pin(request)
    sql, args = _interaction_query(request)
    with db() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["ts", "kiosk", "kind", "person", "channel", "lang", "user_text", "reply", "intent", "sentiment", "mood", "engine"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] if r[c] is not None else "" for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="aura_interactions.csv"'})


async def demo_data(request: Request):
    require_pin(request)
    import demo
    if request.method == "DELETE":
        return JSONResponse(demo.clear())
    return JSONResponse(demo.seed())


async def engine_status(request: Request):
    require_pin(request)
    out = {**brain.engines(), "jev_ok": await brain.jev_status(), "mood_tracking": MOOD_TRACKING,
           "retention_days": RETENTION_DAYS, "flights": flights.status(), "assist_webhook": bool(ASSIST_WEBHOOK_URL),
           "safety_webhook": bool(SAFETY_WEBHOOK_URL), "liveness": LIVENESS, "liveness_threshold": LIVENESS_THRESHOLD}
    async with httpx.AsyncClient(timeout=4) as client:
        for key, url in (("recognizer", f"{COMPREFACE_URL}/healthcheck"), ("listener", f"{LISTENER_URL}/healthcheck")):
            try:
                out[key] = (await client.get(url)).json()
            except (httpx.HTTPError, ValueError):
                out[key] = None
    return JSONResponse(out)


# ----------------------------------------------------------------------------- app
async def on_http_exception(request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/", kiosk_page),
    Route("/manifest.webmanifest", kiosk_manifest),
    Route("/{app}/manifest.webmanifest", staff_manifest),
    Route("/sw.js", service_worker),
    Route("/{app}/manifest.webmanifest", staff_manifest),
    Route("/sw.js", service_worker),
    Route("/admin", admin_page),
    Route("/insights", insights_page),
    Route("/assist", assist_page),
    Route("/api/flights", flight_board),
    Route("/api/assist", assist_create, methods=["POST"]),
    Route("/api/assist/{req_id:int}", assist_status),
    Route("/api/assist/{req_id:int}/cancel", assist_cancel, methods=["POST"]),
    Route("/api/requests", requests_list),
    Route("/api/roster", roster_list),
    Route("/api/roster", roster_push, methods=["POST"]),
    Route("/api/roster/import", roster_import, methods=["POST"]),
    Route("/api/roster/template.csv", roster_template),
    Route("/api/roster/coverage", roster_coverage),
    Route("/api/roster/shift", roster_shift, methods=["POST"]),
    Route("/api/roster/shift/{shift_id:int}", roster_shift, methods=["PUT", "DELETE"]),
    Route("/api/alerts", alerts_list),
    Route("/api/alerts/{alert_id:int}", alert_update, methods=["PUT"]),
    Route("/api/alerts/routes", alert_routes),
    Route("/api/alerts/routes/{department}", alert_routes, methods=["PUT", "DELETE"]),
    Route("/api/safety/precheck", safety_precheck, methods=["POST"]),
    Route("/api/safety/check", safety_check, methods=["POST"]),
    Route("/api/safety/rules", safety_rules),
    Route("/api/safety/rules/{department}", safety_rules, methods=["PUT"]),
    Route("/api/safety/rules/{department}", safety_rule_delete, methods=["DELETE"]),
    Route("/api/requests/{req_id:int}", request_update, methods=["PUT"]),
    Route("/api/health", health),
    Route("/api/recognize", recognize, methods=["POST"]),
    Route("/api/greet", greet, methods=["POST"]),
    Route("/api/event", record_event, methods=["POST"]),
    Route("/api/access", gate_pass, methods=["POST"]),
    Route("/api/access", access_log),
    Route("/api/access.csv", access_csv),
    Route("/api/access/presence", access_presence),
    Route("/api/access/out", access_out, methods=["POST"]),
    Route("/api/areas/{name}", area_save, methods=["PUT"]),
    Route("/api/muster", muster_overview),
    Route("/api/muster/start", muster_start, methods=["POST"]),
    Route("/api/muster/checkin", muster_checkin, methods=["POST"]),
    Route("/api/muster/{mid:int}/mark", muster_mark, methods=["POST"]),
    Route("/api/muster/{mid:int}/end", muster_end, methods=["POST"]),
    Route("/api/muster/{mid:int}.csv", muster_csv),
    Route("/api/talk", talk, methods=["POST"]),
    Route("/api/listen", listen, methods=["POST"]),
    Route("/api/kiosk/{name}", kiosk_config),
    Route("/api/status", status),
    Route("/api/status/mood", mood_today),
    Route("/api/events", events),
    Route("/api/events/{event_id:int}", delete_event, methods=["DELETE"]),
    Route("/api/export.csv", export_csv),
    Route("/api/employees", employees),
    Route("/api/employees/{name}/photo", enrol_photo, methods=["POST"]),
    Route("/api/employees/{name}/photos", employee_photos),
    Route("/api/employees/{name}/photos/{image_id}", employee_photo_image),
    Route("/api/employees/{name}/photos/{image_id}", employee_photo_delete, methods=["DELETE"]),
    Route("/api/employees/{name}", delete_employee, methods=["DELETE"]),
    Route("/api/employees/{name}", rename_employee, methods=["PUT"]),
    Route("/api/people", people_list),
    Route("/api/people/{name}", people_save, methods=["PUT"]),
    Route("/api/people/{name}/conversations", forget_person, methods=["DELETE"]),
    Route("/api/kiosks", kiosks_list),
    Route("/api/kiosks/{name}", kiosk_save, methods=["PUT"]),
    Route("/api/kiosks/{name}", kiosk_delete, methods=["DELETE"]),
    Route("/api/kiosks/{name}/media", media_upload, methods=["POST"]),
    Route("/api/media/{media_id:int}", media_delete, methods=["DELETE"]),
    Route("/api/media/{media_id:int}", media_caption, methods=["PUT"]),
    Route("/api/announcements", announcements, methods=["GET", "POST"]),
    Route("/api/announcements/{ann_id:int}", announcement_delete, methods=["DELETE"]),
    Route("/api/insights", insights_overview),
    Route("/api/insights/person/{name}", insights_person),
    Route("/api/interactions", interactions_list),
    Route("/api/interactions.csv", interactions_csv),
    Route("/api/demo", demo_data, methods=["POST", "DELETE"]),
    Route("/api/engines", engine_status),
    Mount("/media", StaticFiles(directory=MEDIA_DIR), name="media"),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(alerts.loop())  # supervisor alerts: scan every ALERT_SCAN_SECONDS
    _recognizer_client()
    yield
    task.cancel()
    if _cf_client is not None:
        await _cf_client.aclose()


app = Starlette(routes=routes, exception_handlers={HTTPException: on_http_exception}, lifespan=lifespan)
