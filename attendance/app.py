"""
Face Attendance backend (Starlette + SQLite).

Thin layer between the iPad kiosk pages and CompreFace:
  * POST /api/recognize            -> forwards a camera frame to CompreFace, returns the best match
  * POST /api/event                -> records IN / BREAK / BACK / OUT for a recognised employee
  * GET  /api/status               -> current state per employee for a day        (admin PIN)
  * GET  /api/events, /api/export.csv -> log with filters / CSV for payroll        (admin PIN)
  * /api/employees…                -> list / enrol / rename / delete CompreFace subjects (admin PIN)

Only employee NAMES (CompreFace "subjects") and timestamps are stored here.
Face images live in CompreFace; camera frames are never written to disk.
"""
import csv
import io
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

COMPREFACE_URL = os.environ.get("COMPREFACE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("COMPREFACE_API_KEY", "")
THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.90"))
DUP_WINDOW = int(os.environ.get("DUPLICATE_WINDOW_SECONDS", "60"))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "2468")
DB_PATH = os.environ.get("DB_PATH", "attendance.db")

ACTIONS = ("IN", "BREAK", "BACK", "OUT")
# Which action makes sense next, given the last one today. Used only to highlight a button;
# the employee can still tap any action.
NEXT_ACTION = {None: "IN", "OUT": "IN", "IN": "OUT", "BACK": "OUT", "BREAK": "BACK"}

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ----------------------------------------------------------------------------- database
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts         TEXT NOT NULL,           -- local ISO timestamp
                   day        TEXT NOT NULL,           -- YYYY-MM-DD, for fast filtering
                   employee   TEXT NOT NULL,
                   action     TEXT NOT NULL,
                   kiosk      TEXT,
                   similarity REAL
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_day ON events(day)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_emp ON events(employee, ts)")


init_db()


def now() -> datetime:
    return datetime.now()  # container TZ is set via the TZ env var


def last_action_today(conn: sqlite3.Connection, employee: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT action, ts FROM events WHERE employee=? AND day=? ORDER BY id DESC LIMIT 1",
        (employee, date.today().isoformat()),
    ).fetchone()


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
        raise HTTPException(502, f"CompreFace unreachable: {e.__class__.__name__}")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        raise HTTPException(r.status_code, f"CompreFace: {msg}")
    return r


async def read_upload(request: Request):
    form = await request.form()
    up = form.get("file")
    if up is None:
        raise HTTPException(400, "file field required")
    data = await up.read()
    return (up.filename or "frame.jpg", data, up.content_type or "image/jpeg")


# ----------------------------------------------------------------------------- pages
async def kiosk_page(request):
    return FileResponse(os.path.join(STATIC, "kiosk.html"))


async def admin_page(request):
    return FileResponse(os.path.join(STATIC, "admin.html"))


async def health(request):
    ok = True
    try:
        await cf("GET", "/subjects")
    except HTTPException:
        ok = False
    return JSONResponse({"ok": ok, "threshold": THRESHOLD})


# ----------------------------------------------------------------------------- kiosk API
async def recognize(request: Request):
    """Send one camera frame; get back the best matching employee (or none)."""
    upload = await read_upload(request)
    r = await cf(
        "POST",
        "/recognize",
        params={"limit": 1, "prediction_count": 1, "det_prob_threshold": 0.8},
        files={"file": upload},
    )
    faces = r.json().get("result", [])
    if not faces:
        return JSONResponse({"face": False, "matched": False})
    subjects = faces[0].get("subjects") or []
    if not subjects:
        return JSONResponse({"face": True, "matched": False})
    subj = subjects[0]
    sim = float(subj["similarity"])
    if sim < THRESHOLD:
        return JSONResponse({"face": True, "matched": False, "similarity": round(sim, 3)})
    employee = subj["subject"]
    with db() as conn:
        last = last_action_today(conn, employee)
    last_action = last["action"] if last else None
    return JSONResponse(
        {
            "face": True,
            "matched": True,
            "employee": employee,
            "similarity": round(sim, 3),
            "last_action": last_action,
            "last_ts": last["ts"] if last else None,
            "suggested": NEXT_ACTION.get(last_action, "IN"),
        }
    )


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
        conn.execute(
            "INSERT INTO events (ts, day, employee, action, kiosk, similarity) VALUES (?,?,?,?,?,?)",
            (ts.isoformat(timespec="seconds"), ts.date().isoformat(), employee, action, kiosk, similarity),
        )
    return JSONResponse({"ok": True, "duplicate": False, "ts": ts.isoformat(timespec="seconds")})


# ----------------------------------------------------------------------------- admin API
async def status(request: Request):
    """Current state of every employee who has an event on the given day (default today)."""
    require_pin(request)
    day = request.query_params.get("day") or date.today().isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT e.employee, e.action, e.ts, e.kiosk
               FROM events e
               JOIN (SELECT employee, MAX(id) AS id FROM events WHERE day=? GROUP BY employee) m
                 ON m.id = e.id
               ORDER BY e.employee""",
            (day,),
        ).fetchall()
    return JSONResponse({"day": day, "rows": [dict(r) for r in rows]})


def _range(request: Request):
    date_from = request.query_params.get("from") or date.today().isoformat()
    date_to = request.query_params.get("to") or date_from
    return date_from, date_to


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
            "SELECT ts, employee, action, kiosk, similarity FROM events WHERE day BETWEEN ? AND ? ORDER BY id",
            (date_from, date_to),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "employee", "action", "kiosk", "similarity"])
    for r in rows:
        w.writerow([r["ts"], r["employee"], r["action"], r["kiosk"] or "", r["similarity"] or ""])
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
    return JSONResponse(
        {"employees": [{"name": s, "photos": counts.get(s, 0)} for s in sorted(subjects, key=str.lower)]}
    )


async def enrol_photo(request: Request):
    """Add one face photo to an employee (creates the employee if new)."""
    require_pin(request)
    name = request.path_params["name"].strip()
    if not name:
        raise HTTPException(400, "name required")
    upload = await read_upload(request)
    r = await cf("POST", "/faces", params={"subject": name, "det_prob_threshold": 0.8}, files={"file": upload})
    return JSONResponse({"ok": True, "image_id": r.json().get("image_id"), "employee": name})


async def delete_employee(request: Request):
    require_pin(request)
    await cf("DELETE", f"/subjects/{request.path_params['name']}")
    return JSONResponse({"ok": True})


async def rename_employee(request: Request):
    require_pin(request)
    name = request.path_params["name"]
    new = ((await request.json()).get("name") or "").strip()
    if not new:
        raise HTTPException(400, "name required")
    await cf("PUT", f"/subjects/{name}", json={"subject": new})
    with db() as conn:
        conn.execute("UPDATE events SET employee=? WHERE employee=?", (new, name))
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------- app
async def on_http_exception(request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/", kiosk_page),
    Route("/admin", admin_page),
    Route("/api/health", health),
    Route("/api/recognize", recognize, methods=["POST"]),
    Route("/api/event", record_event, methods=["POST"]),
    Route("/api/status", status),
    Route("/api/events", events),
    Route("/api/events/{event_id:int}", delete_event, methods=["DELETE"]),
    Route("/api/export.csv", export_csv),
    Route("/api/employees", employees),
    Route("/api/employees/{name}/photo", enrol_photo, methods=["POST"]),
    Route("/api/employees/{name}", delete_employee, methods=["DELETE"]),
    Route("/api/employees/{name}", rename_employee, methods=["PUT"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

app = Starlette(routes=routes, exception_handlers={HTTPException: on_http_exception})
