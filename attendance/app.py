"""
Aura — Face Attendance backend (Starlette + SQLite).

Thin layer between the wall kiosks, the admin/insights pages and the local ML services:
  recognizer (face match + emotion + attire), listener (speech -> text + language),
  and the brain (Jev decisions, optional Claude replies, templates).

Kiosk (no PIN, LAN only):
  POST /api/recognize        camera frame -> best match + mood + attire; logs one sighting per approach
  POST /api/greet            personal greeting lines for a recognised staff member
  POST /api/event            IN / BREAK / BACK / OUT
  POST /api/talk             text -> reply in the speaker's language (stored per person)
  POST /api/listen           voice clip -> transcript + reply
  GET  /api/kiosk/{name}     screen config, images, announcements, live pulse
Admin (X-Admin-Pin):
  /api/status, /api/events, /api/export.csv, /api/employees…   (original attendance admin)
  /api/people…, /api/kiosks…, /api/media…, /api/announcements…
  /api/insights, /api/insights/person/{name}, /api/interactions(.csv), /api/demo, /api/engines

Camera frames and voice clips are processed in memory and discarded. Only face vectors
(recognizer), names, timestamps, derived mood labels and conversation text are stored.
"""
import csv
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
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import brain
import insights
from store import db, init_db

COMPREFACE_URL = os.environ.get("COMPREFACE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("COMPREFACE_API_KEY", "")
LISTENER_URL = os.environ.get("LISTENER_URL", "http://localhost:8001").rstrip("/")
THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.90"))
DUP_WINDOW = int(os.environ.get("DUPLICATE_WINDOW_SECONDS", "60"))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "2468")
MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.join(os.path.dirname(os.environ.get("DB_PATH", "./attendance.db")) or ".", "media"))
# on = staff (with consent) + anonymous visitors; staff_only; visitors_only; off
MOOD_TRACKING = os.environ.get("MOOD_TRACKING", "on").lower()
RETENTION_DAYS = int(os.environ.get("INTERACTION_RETENTION_DAYS", "365"))
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


def cf_headers() -> dict:
    if not API_KEY or API_KEY.startswith("PASTE"):
        raise HTTPException(503, "COMPREFACE_API_KEY is not configured on the server")
    return {"x-api-key": API_KEY}


async def cf(method: str, path: str, **kw) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(
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
async def kiosk_page(request):
    return FileResponse(os.path.join(STATIC, "kiosk.html"))


async def admin_page(request):
    return FileResponse(os.path.join(STATIC, "admin.html"))


async def insights_page(request):
    return FileResponse(os.path.join(STATIC, "insights.html"))


async def health(request):
    ok = True
    try:
        await cf("GET", "/subjects")
    except HTTPException:
        ok = False
    return JSONResponse({"ok": ok, "threshold": THRESHOLD})


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


async def recognize(request: Request):
    """Send one camera frame; get back the best matching employee (or none) plus mood/attire."""
    upload, form = await read_upload(request)
    kiosk = (form.get("kiosk") or "Main").strip()
    encounter = (form.get("encounter") or "").strip()
    r = await cf(
        "POST",
        "/recognize",
        params={"limit": 1, "prediction_count": 1, "det_prob_threshold": 0.8},
        files={"file": upload},
    )
    faces = r.json().get("result", [])
    if not faces:
        return JSONResponse({"face": False, "matched": False})
    face = faces[0]
    base = {"face": True, "size": face.get("size"), "emotion": face.get("emotion"), "attire": face.get("attire")}
    subjects = face.get("subjects") or []
    sim = float(subjects[0]["similarity"]) if subjects else 0.0
    if not subjects or sim < THRESHOLD:
        if encounter:
            _log_sighting(encounter, kiosk, None, face, True)
        if not mood_allowed("visitor"):
            base["emotion"] = None
        return JSONResponse({**base, "matched": False, "similarity": round(sim, 3) if subjects else None})
    employee = subjects[0]["subject"]
    with db() as conn:
        last = last_action_today(conn, employee)
        consent = bool(profile(conn, employee).get("mood_consent", 1))
    if encounter:
        _log_sighting(encounter, kiosk, employee, face, consent)
    if not mood_allowed("staff", consent):
        base["emotion"] = None
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
    ts = now()
    with db() as conn:
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


async def _converse(text: str, p: dict) -> dict:
    kiosk = (p.get("kiosk") or "Main").strip()
    employee = (p.get("employee") or "").strip() or None
    encounter = p.get("encounter")
    today = date.today()
    with db() as conn:
        k = kiosk_row(conn, kiosk)
        ctx = {"location": k.get("headline") or k.get("zone") or kiosk, "cards": k["info"], "now": now().strftime("%A %H:%M"),
               "person": employee, "mood": p.get("mood"), "lang_hint": p.get("lang_hint")}
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
    for m in media:
        m["url"] = f"/media/{m['file']}"
    return JSONResponse({
        "kiosk": k, "media": media, "announcements": ann, "airport": brain.AIRPORT, "wifi": brain.WIFI,
        "pulse": {"on_duty": on_duty, "visitor_mood": vis[0], "visitors_today": vis[1], "mood_today": allv, "chats_today": chats},
        "features": {"listener": True, "mood": MOOD_TRACKING != "off"},
    })


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


async def employees(request: Request):
    require_pin(request)
    subjects = (await cf("GET", "/subjects")).json().get("subjects", [])
    faces = (await cf("GET", "/faces", params={"size": 10000})).json().get("faces", [])
    counts: dict = {}
    for f in faces:
        counts[f["subject"]] = counts.get(f["subject"], 0) + 1
    with db() as conn:
        profs = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM people")}
    return JSONResponse(
        {"employees": [{"name": s, "photos": counts.get(s, 0), "profile": profs.get(s)} for s in sorted(subjects, key=str.lower)]}
    )


async def enrol_photo(request: Request):
    """Add one face photo to an employee (creates the employee if new)."""
    require_pin(request)
    name = request.path_params["name"].strip()
    if not name:
        raise HTTPException(400, "name required")
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
        for t, col in (("people", "name"), ("sightings", "person"), ("interactions", "person"), ("memories", "person")):
            conn.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (new, name))
    return JSONResponse({"ok": True})


PROFILE_FIELDS = ("role", "department", "shift_start", "birthday", "joined", "language", "nickname", "mood_consent", "notes")


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
    with db() as conn:
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


async def kiosks_list(request: Request):
    require_pin(request)
    with db() as conn:
        rows = []
        for r in conn.execute("SELECT name FROM kiosks ORDER BY name COLLATE NOCASE").fetchall():
            k = kiosk_row(conn, r["name"])
            k["media"] = [dict(m) for m in conn.execute("SELECT * FROM media WHERE kiosk=? ORDER BY position, id", (r["name"],))]
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
        if "voice" in p:
            fields["voice"] = 1 if p["voice"] else 0
        if "info" in p:
            fields["info"] = json.dumps(p["info"] or [], ensure_ascii=False)
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
           "retention_days": RETENTION_DAYS}
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
    Route("/admin", admin_page),
    Route("/insights", insights_page),
    Route("/api/health", health),
    Route("/api/recognize", recognize, methods=["POST"]),
    Route("/api/greet", greet, methods=["POST"]),
    Route("/api/event", record_event, methods=["POST"]),
    Route("/api/talk", talk, methods=["POST"]),
    Route("/api/listen", listen, methods=["POST"]),
    Route("/api/kiosk/{name}", kiosk_config),
    Route("/api/status", status),
    Route("/api/events", events),
    Route("/api/events/{event_id:int}", delete_event, methods=["DELETE"]),
    Route("/api/export.csv", export_csv),
    Route("/api/employees", employees),
    Route("/api/employees/{name}/photo", enrol_photo, methods=["POST"]),
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

app = Starlette(routes=routes, exception_handlers={HTTPException: on_http_exception})
