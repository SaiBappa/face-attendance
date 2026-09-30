"""
Supervisor alerts: a background scan (every ALERT_SCAN_SECONDS) that turns the roster and the
live attendance / assistance / safety data into operational alerts.

  no_show        rostered shift started NO_SHOW_MINUTES ago and nobody clocked in
  late           clocked in more than LATE_ALERT_MINUTES after the rostered start
  no_clock_out   still clocked in CLOCKOUT_GRACE_MINUTES after the shift ended
  long_break     on BREAK for more than BREAK_MAX_MINUTES
  understaffed   a department has at least UNDERSTAFF_MIN fewer people on duty than rostered
  assist_sla     an assistance request has waited longer than 2 x ASSIST_SLA_MINUTES
  safety         a clock-in safety check was flagged (missing PPE / high fatigue), or entry refused at an enforced location
  pass_expired   someone with an expired security pass was recognised at a kiosk (logged live by
                 the kiosk, not by the scan; at most once per person per kiosk every PASS_ALERT_MINUTES)
  spoof          a photo or screen showing a staff member's face was held up to a kiosk (failed the
                 liveness check; logged live, at most once per person per kiosk every PASS_ALERT_MINUTES)
  area_zone      someone entered an area their security pass zone doesn't cover (logged live at the gate)
  passback       entered an area again without an exit, or exited without an entry (logged live at the gate)
  overstay       inside an area longer than its maximum stay
  capacity       more people inside an area than its capacity
  muster         an emergency muster (roll call) was started (logged live)
  no_exit        entered an area and never exited (ACCESS_STALE_HOURS), or still inside after clocking OUT

Each alert has a stable `key`, so re-scans never duplicate it. New alerts are posted to the
department's webhook (Admin → Alerts → routing) or ALERT_WEBHOOK_URL, and shown in the Admin
alerts feed where a supervisor acknowledges or resolves them.
"""
import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

import access
import roster
from store import db

SCAN_S = int(os.environ.get("ALERT_SCAN_SECONDS", "60"))
NO_SHOW_MIN = int(os.environ.get("NO_SHOW_MINUTES", "15"))
LATE_MIN = int(os.environ.get("LATE_ALERT_MINUTES", "10"))
CLOCKOUT_GRACE = int(os.environ.get("CLOCKOUT_GRACE_MINUTES", "60"))
BREAK_MAX = int(os.environ.get("BREAK_MAX_MINUTES", "60"))
UNDERSTAFF_MIN = int(os.environ.get("UNDERSTAFF_MIN", "2"))
ASSIST_SLA = int(os.environ.get("ASSIST_SLA_MINUTES", "5"))
WEBHOOK = os.environ.get("ALERT_WEBHOOK_URL", "")
PASS_ALERT_MIN = int(os.environ.get("PASS_ALERT_MINUTES", "10"))

TYPES = {  # type -> (icon, label, severity)
    "no_show": ("🚫", "No-show", "high"),
    "late": ("⏰", "Late arrival", "medium"),
    "no_clock_out": ("🕘", "Missed clock-out", "low"),
    "long_break": ("☕", "Long break", "low"),
    "understaffed": ("👥", "Understaffed", "high"),
    "assist_sla": ("🛎", "Assistance waiting", "high"),
    "safety": ("🦺", "Safety check flagged", "high"),
    "pass_expired": ("⛔", "Expired security pass", "high"),
    "spoof": ("📵", "Photo / screen at kiosk", "high"),
    "area_zone": ("🚷", "Not authorised for area", "high"),
    "passback": ("🔁", "Entry / exit mismatch", "low"),
    "overstay": ("⏳", "Overstay in area", "medium"),
    "capacity": ("🚪", "Area over capacity", "high"),
    "no_exit": ("🚶", "No exit recorded", "low"),
    "muster": ("🚨", "Emergency muster", "high"),
}


def _add(conn, key, type_, text, now, person=None, department=None, kiosk=None, severity=None, demo=0) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO alerts (key, ts, day, type, severity, person, department, kiosk, text, demo)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (key, now.isoformat(timespec="seconds"), now.date().isoformat(), type_, severity or TYPES[type_][2],
         person, department, kiosk, text, demo))
    return cur.rowcount > 0


def pass_incident(conn, person, department, kiosk, expiry, now: datetime = None) -> bool:
    """Log an expired-security-pass incident at a kiosk. Repeat sightings of the same person at the same
    kiosk within PASS_ALERT_MINUTES are one incident. Returns True when a new alert was created."""
    now = now or datetime.now()
    since = (now - timedelta(minutes=PASS_ALERT_MIN)).isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM alerts WHERE type='pass_expired' AND person=? AND kiosk IS ? AND ts>=? LIMIT 1",
                    (person, kiosk, since)).fetchone():
        return False
    return _add(conn, f"pass_expired|{person}|{kiosk}|{now.isoformat(timespec='seconds')}", "pass_expired",
                f"{person} tried to use the {kiosk or 'kiosk'} kiosk with a security pass that expired on {expiry}. "
                "All attendance actions were blocked and the floor siren sounded.",
                now, person, department, kiosk)


def spoof_incident(conn, person, department, kiosk, now: datetime = None) -> bool:
    """Log a spoofing attempt: a photo or screen of `person` failed the kiosk's liveness check. Repeat
    attempts at the same kiosk within PASS_ALERT_MINUTES are one incident. True when a new alert was created."""
    now = now or datetime.now()
    since = (now - timedelta(minutes=PASS_ALERT_MIN)).isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM alerts WHERE type='spoof' AND person=? AND kiosk IS ? AND ts>=? LIMIT 1",
                    (person, kiosk, since)).fetchone():
        return False
    return _add(conn, f"spoof|{person}|{kiosk}|{now.isoformat(timespec='seconds')}", "spoof",
                f"Someone held up a photo or screen showing {person} at the {kiosk or 'kiosk'} kiosk. "
                "It failed the liveness check, so nothing was recorded.",
                now, person, department, kiosk)


def gate_incident(conn, flag, person, department, kiosk, area, zone=None, min_zone=None, missed_exit=False,
                  now: datetime = None) -> bool:
    """A flagged pass through a gate (access.record's `flag`). True when a new alert was created."""
    now = now or datetime.now()
    ts = now.isoformat(timespec="seconds")
    if flag == "zone":
        text = (f"{person} entered {area} at {kiosk} with a {zone.capitalize() if zone else 'no'} zone pass on file — "
                f"this area needs {min_zone.capitalize()} or higher. They are recorded inside; please check.")
        return _add(conn, f"area_zone|{person}|{area}|{ts}", "area_zone", text, now, person, department, kiosk)
    if flag == "passback":
        text = (f"{person} entered {area} at {kiosk} again without an exit being recorded — possible tailgating or a missed exit."
                if missed_exit else
                f"{person} left {area} at {kiosk} without an entry being recorded — possible tailgating on the way in.")
        return _add(conn, f"passback|{person}|{area}|{ts}", "passback", text, now, person, department, kiosk)
    return False


def scan(conn, now: datetime = None) -> int:
    """Create any alerts that are due. Returns how many were new."""
    now = now or datetime.now()
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    new = 0
    depts = {r["name"]: r["department"] for r in conn.execute("SELECT name, department FROM people")}

    # --- roster: no-shows, lateness, missed clock-outs
    for r in roster.adherence(conn, yesterday, today, now):
        st, en = roster.window(r)
        if now - st > timedelta(hours=12) and r["status"] != "no_clock_out":
            continue  # only live-ish shifts; history is in Insights
        key = f"{r['person']}|{r['day']}|{r['start']}"
        where = f" ({r['position']}, {r['location']})" if r.get("position") and r.get("location") else ""
        if not r["in"] and now >= st + timedelta(minutes=NO_SHOW_MIN) and now <= en:
            new += _add(conn, "no_show|" + key, "no_show",
                        f"{r['person']} has not clocked in for the {r['start']}–{r['end']} shift{where} ({int((now - st).total_seconds() // 60)} min).",
                        now, r["person"], r["department"], demo=r["demo"])
        if r["in"] and r["late_min"] >= LATE_MIN and datetime.fromisoformat(r["in"]) > now - timedelta(hours=3):
            new += _add(conn, "late|" + key, "late", f"{r['person']} clocked in {r['late_min']} min late for the {r['start']} shift.",
                        now, r["person"], r["department"], demo=r["demo"])
        if r["status"] == "no_clock_out" and now >= en + timedelta(minutes=CLOCKOUT_GRACE) and now - en < timedelta(hours=12):
            new += _add(conn, "no_clock_out|" + key, "no_clock_out",
                        f"{r['person']} is still clocked in {int((now - en).total_seconds() // 60)} min after their shift ended at {r['end']}. Forgot to clock out, or unpaid overtime?",
                        now, r["person"], r["department"], demo=r["demo"])

    # --- long breaks (latest action today is BREAK)
    for r in conn.execute(
            """SELECT e.employee, e.ts, e.kiosk FROM events e JOIN (SELECT employee, MAX(id) id FROM events WHERE day=? GROUP BY employee) m
               ON m.id=e.id WHERE e.action='BREAK'""", (today,)):
        mins = int((now - datetime.fromisoformat(r["ts"])).total_seconds() // 60)
        if mins > BREAK_MAX:
            new += _add(conn, f"long_break|{r['employee']}|{r['ts']}", "long_break",
                        f"{r['employee']} has been on break for {mins} min (limit {BREAK_MAX}).", now, r["employee"], depts.get(r["employee"]), r["kiosk"])

    # --- understaffing per department right now
    rows = roster.adherence(conn, yesterday, today, now)
    planned = defaultdict(int)
    for r in rows:
        st, en = roster.window(r)
        if st + timedelta(minutes=NO_SHOW_MIN) <= now < en:
            planned[r["department"] or "Unassigned"] += 1
    on_duty = defaultdict(int)
    for r in conn.execute(
            """SELECT e.employee FROM events e JOIN (SELECT employee, MAX(id) id FROM events WHERE day>=? GROUP BY employee) m
               ON m.id=e.id WHERE e.action IN ('IN','BACK')""", (yesterday,)):
        on_duty[depts.get(r["employee"]) or "Unassigned"] += 1
    for dept, n in planned.items():
        gap = n - on_duty.get(dept, 0)
        if gap >= UNDERSTAFF_MIN:
            # one alert per department per hour block
            new += _add(conn, f"understaffed|{dept}|{now:%Y-%m-%dT%H}", "understaffed",
                        f"{dept} is short {gap}: {on_duty.get(dept, 0)} on duty vs {n} rostered.", now, department=dept)

    # --- assistance requests waiting too long
    for r in conn.execute("SELECT * FROM requests WHERE status='open' AND demo=0"):
        mins = int((now - datetime.fromisoformat(r["ts"])).total_seconds() // 60)
        if mins >= 2 * ASSIST_SLA:
            new += _add(conn, f"assist_sla|{r['id']}", "assist_sla",
                        f"Assistance request #{r['id']} ({r['kind'].replace('_', ' ')}) at {r['kiosk']} has waited {mins} min with nobody assigned.",
                        now, kiosk=r["kiosk"])

    # --- flagged safety checks (today)
    for r in conn.execute("SELECT * FROM safety_checks WHERE day=? AND result='flagged' AND demo=0", (today,)):
        bits = []
        if r["missing"]:
            bits.append("missing " + r["missing"].replace(",", ", ").replace("_", " "))
        if r["fatigue_level"] == "high":
            bits.append(f"HIGH fatigue risk ({r['fatigue_score']})")
        lead = f"{r['person']} was refused entry at {r['kiosk']}:" if r["blocked"] else f"{r['person']} clocked in with"
        new += _add(conn, f"safety|{r['id']}", "safety", f"{lead} " + " and ".join(bits) + ".",
                    now, r["person"], r["department"], r["kiosk"])

    # --- areas: overstay, over capacity, entries with no exit (access.py)
    for a in access.due_alerts(conn, now):
        new += _add(conn, a["key"], a["type"], a["text"], now, a["person"], depts.get(a["person"]), a["kiosk"])
    return new


def _route(conn, department) -> str:
    if department:
        r = conn.execute("SELECT webhook FROM alert_routes WHERE department=?", (department,)).fetchone()
        if r and r["webhook"]:
            return r["webhook"]
    r = conn.execute("SELECT webhook FROM alert_routes WHERE department='*'").fetchone()
    return (r["webhook"] if r and r["webhook"] else None) or WEBHOOK


async def deliver():
    """Post not-yet-notified alerts to their webhook (at most 20 per scan)."""
    with db() as conn:
        pending = [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE notified=0 AND demo=0 ORDER BY id LIMIT 20")]
        routes = {a["id"]: _route(conn, a["department"]) for a in pending}
    if not pending:
        return
    sent = []
    async with httpx.AsyncClient(timeout=8) as client:
        for a in pending:
            url = routes[a["id"]]
            if url:
                icon, label, _ = TYPES.get(a["type"], ("⚠️", a["type"], ""))
                try:
                    await client.post(url, json={"text": f"{icon} {label}: {a['text']}", "alert": a})
                except httpx.HTTPError as e:
                    print("alert webhook failed", e.__class__.__name__)
                    continue
            sent.append(a["id"])
    with db() as conn:
        conn.executemany("UPDATE alerts SET notified=1 WHERE id=?", [(i,) for i in sent])


async def loop():
    await asyncio.sleep(5)
    while True:
        try:
            with db() as conn:
                scan(conn)
            await deliver()
        except Exception as e:  # never let the loop die
            print("alert scan failed", e.__class__.__name__, str(e)[:200])
        await asyncio.sleep(SCAN_S)


def settings() -> dict:
    return {"scan_seconds": SCAN_S, "no_show_minutes": NO_SHOW_MIN, "late_alert_minutes": LATE_MIN,
            "clockout_grace_minutes": CLOCKOUT_GRACE, "area_exit_grace_minutes": access.EXIT_GRACE_MIN, "pass_alert_minutes": PASS_ALERT_MIN, "break_max_minutes": BREAK_MAX, "understaff_min": UNDERSTAFF_MIN,
            "webhook": bool(WEBHOOK), "types": {k: {"icon": v[0], "label": v[1], "severity": v[2]} for k, v in TYPES.items()}}
