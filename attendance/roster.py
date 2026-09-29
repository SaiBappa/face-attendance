"""
Rosters: who is scheduled when, and how actual clock-ins compare.

Shifts come from a CSV upload in Admin, a JSON push from an HR / rostering system
(POST /api/roster), or manual edits. A shift may cross midnight (end < start -> ends next day)
and a person may have several shifts a day (split shifts).

Everything that used to compare against a person's fixed `shift_start` now asks this module
first: `planned_start()` returns the rostered start when a shift exists, otherwise None so the
caller falls back to the profile / SHIFT_START.
"""
import csv
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

GRACE_MIN = int(os.environ.get("LATE_GRACE_MINUTES", "5"))
EARLY_WINDOW_H = 3        # a clock-in up to 3 h before a shift belongs to that shift
LATE_OUT_WINDOW_H = 6     # a clock-out up to 6 h after the end belongs to that shift


# ----------------------------------------------------------------------------- parsing
HEADERS = {
    "name": ("name", "employee", "staff", "person", "full name"),
    "date": ("date", "day", "shift date"),
    "start": ("start", "start time", "from", "in", "shift start"),
    "end": ("end", "end time", "to", "out", "shift end"),
    "position": ("position", "role", "duty", "task"),
    "location": ("location", "kiosk", "post", "area", "gate"),
    "department": ("department", "dept", "team"),
    "notes": ("notes", "note", "comment"),
}


def _date(s: str) -> Optional[str]:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _time(s: str) -> Optional[str]:
    m = re.match(r"^\s*(\d{1,2})[:.h]?(\d{2})?\s*(am|pm)?\s*$", (s or "").lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    if h == 24 and mi == 0:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return f"{h:02d}:{mi:02d}"


def parse_csv(text: str) -> tuple:
    """-> (shifts, errors). Header names are matched loosely (see HEADERS); ';' or tab also work."""
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=delim)
    rows = list(reader)
    if not rows:
        return [], ["The file is empty"]
    head = [h.strip().lower() for h in rows[0]]
    col = {}
    for key, names in HEADERS.items():
        for i, h in enumerate(head):
            if h in names:
                col[key] = i
                break
    missing = [k for k in ("name", "date", "start", "end") if k not in col]
    if missing:
        return [], [f"Missing column(s): {', '.join(missing)}. Expected headers like: name, date, start, end, position, location, department"]
    shifts, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        get = lambda k: (r[col[k]].strip() if k in col and col[k] < len(r) else "")
        name, d, s, e = get("name"), _date(get("date")), _time(get("start")), _time(get("end"))
        if not name or not d or not s or not e:
            errors.append(f"Row {n}: need name, a date (YYYY-MM-DD or DD/MM/YYYY) and start/end times (HH:MM)")
            continue
        if s == e:
            errors.append(f"Row {n}: start and end are the same")
            continue
        shifts.append({"person": name, "day": d, "start": s, "end": e, "position": get("position") or None,
                       "location": get("location") or None, "department": get("department") or None, "notes": get("notes") or None})
    return shifts, errors


def validate(items: list) -> tuple:
    """JSON push: [{person|name, day|date, start, end, ...}] -> (shifts, errors)."""
    shifts, errors = [], []
    for i, x in enumerate(items or []):
        name = (x.get("person") or x.get("name") or "").strip()
        d, s, e = _date(x.get("day") or x.get("date") or ""), _time(x.get("start") or ""), _time(x.get("end") or "")
        if not name or not d or not s or not e or s == e:
            errors.append(f"Item {i}: need person, day, start, end")
            continue
        shifts.append({"person": name, "day": d, "start": s, "end": e, "position": x.get("position"),
                       "location": x.get("location"), "department": x.get("department"), "notes": x.get("notes")})
    return shifts, errors


def save(conn, shifts: list, source: str, replace: bool = True, demo: int = 0) -> dict:
    """Insert shifts. With replace, existing (non-demo) shifts for the same person+day are replaced,
    so re-uploading this week's roster is safe."""
    replaced = 0
    if replace:
        for person, day in {(s["person"], s["day"]) for s in shifts}:
            replaced += conn.execute("DELETE FROM shifts WHERE person=? AND day=? AND demo=?", (person, day, demo)).rowcount
    for s in shifts:
        conn.execute(
            "INSERT INTO shifts (person, day, start, end, position, location, department, notes, source, demo) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s["person"], s["day"], s["start"], s["end"], s.get("position"), s.get("location"), s.get("department"),
             s.get("notes"), source, demo))
        if s.get("department"):  # keep profiles in step with the roster's department
            conn.execute("INSERT OR IGNORE INTO people (name, created) VALUES (?, ?)", (s["person"], datetime.now().isoformat(timespec="seconds")))
            conn.execute("UPDATE people SET department=? WHERE name=? AND (department IS NULL OR department='')", (s["department"], s["person"]))
    return {"imported": len(shifts), "replaced": replaced}


# ----------------------------------------------------------------------------- queries
def window(s) -> tuple:
    """shift row -> (start_dt, end_dt) handling overnight shifts."""
    d = date.fromisoformat(s["day"])
    st = datetime.combine(d, datetime.strptime(s["start"], "%H:%M").time())
    en = datetime.combine(d, datetime.strptime(s["end"], "%H:%M").time())
    if en <= st:
        en += timedelta(days=1)
    return st, en


def shifts_between(conn, date_from: str, date_to: str, person: str = None) -> list:
    sql, args = "SELECT * FROM shifts WHERE day BETWEEN ? AND ?", [date_from, date_to]
    if person:
        sql += " AND person=?"
        args.append(person)
    return [dict(r) for r in conn.execute(sql + " ORDER BY day, start", args)]


def planned_start(conn, person: str, day: str) -> Optional[int]:
    """Minutes after midnight of the first rostered shift that day, or None."""
    r = conn.execute("SELECT start FROM shifts WHERE person=? AND day=? ORDER BY start LIMIT 1", (person, day)).fetchone()
    return int(r["start"][:2]) * 60 + int(r["start"][3:]) if r else None


def starts_map(conn, date_from: str, date_to: str) -> dict:
    """{(person, day): first start minute} for fast lookups in analytics."""
    out = {}
    for r in conn.execute("SELECT person, day, MIN(start) AS start FROM shifts WHERE day BETWEEN ? AND ? GROUP BY person, day",
                          (date_from, date_to)):
        out[(r["person"], r["day"])] = int(r["start"][:2]) * 60 + int(r["start"][3:])
    return out


def current_and_next(conn, person: str, now: datetime) -> tuple:
    rows = shifts_between(conn, (now.date() - timedelta(days=1)).isoformat(), (now.date() + timedelta(days=21)).isoformat(), person)
    cur = nxt = None
    for s in rows:
        st, en = window(s)
        if st - timedelta(hours=EARLY_WINDOW_H) <= now <= en and cur is None:
            cur = {**s, "start_dt": st, "end_dt": en}
        elif st > now and nxt is None and (cur is None or st > cur["end_dt"]):
            nxt = {**s, "start_dt": st, "end_dt": en}
    return cur, nxt


def describe(s: dict, now: datetime) -> str:
    if not s:
        return ""
    st = s["start_dt"]
    when = "today" if st.date() == now.date() else "tomorrow" if st.date() == now.date() + timedelta(days=1) else st.strftime("%A %d %b")
    where = f" at {s['location']}" if s.get("location") else ""
    role = f" as {s['position']}" if s.get("position") else ""
    return f"{when} {s['start']}–{s['end']}{where}{role}"


# ----------------------------------------------------------------------------- adherence
def adherence(conn, date_from: str, date_to: str, now: datetime = None) -> list:
    """Every rostered shift in the range with what actually happened."""
    now = now or datetime.now()
    shifts = shifts_between(conn, date_from, date_to)
    if not shifts:
        return []
    lo = (date.fromisoformat(date_from) - timedelta(days=1)).isoformat()
    hi = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
    ev = defaultdict(list)
    for r in conn.execute("SELECT employee, ts, action FROM events WHERE day BETWEEN ? AND ? ORDER BY ts", (lo, hi)):
        ev[r["employee"]].append((datetime.fromisoformat(r["ts"]), r["action"]))
    depts = {r["name"]: r["department"] for r in conn.execute("SELECT name, department FROM people")}
    out = []
    for s in shifts:
        st, en = window(s)
        evs = ev.get(s["person"], [])
        ins = [t for t, a in evs if a == "IN" and st - timedelta(hours=EARLY_WINDOW_H) <= t <= en]
        outs = [t for t, a in evs if a == "OUT" and st <= t <= en + timedelta(hours=LATE_OUT_WINDOW_H)]
        first_in, last_out = (ins[0] if ins else None), (outs[-1] if outs else None)
        if first_in:
            status = "worked" if last_out else ("in_progress" if now <= en + timedelta(hours=1) else "no_clock_out")
        else:
            status = "upcoming" if now < st else ("late_pending" if now < st + timedelta(minutes=60) else "no_show")
        late = max(0, int((first_in - st).total_seconds() // 60)) if first_in else None
        out.append({
            "id": s["id"], "person": s["person"], "department": s.get("department") or depts.get(s["person"]),
            "day": s["day"], "start": s["start"], "end": s["end"], "position": s.get("position"), "location": s.get("location"),
            "in": first_in.isoformat(timespec="minutes") if first_in else None,
            "out": last_out.isoformat(timespec="minutes") if last_out else None,
            "late_min": late if late and late > GRACE_MIN else 0,
            "early_leave_min": max(0, int((en - last_out).total_seconds() // 60)) if last_out and last_out < en - timedelta(minutes=GRACE_MIN) else 0,
            "overtime_min": max(0, int((last_out - en).total_seconds() // 60)) if last_out and last_out > en + timedelta(minutes=15) else 0,
            "planned_h": round((en - st).total_seconds() / 3600, 2),
            "status": status, "demo": s.get("demo", 0),
        })
    return out


def summary(conn, date_from: str, date_to: str, kiosk: str = None) -> dict:
    rows = adherence(conn, date_from, date_to)
    done = [r for r in rows if r["status"] not in ("upcoming", "late_pending")]
    no_show = [r for r in done if r["status"] == "no_show"]
    attended = [r for r in done if r["in"]]
    by_dept = defaultdict(list)
    for r in done:
        by_dept[r["department"] or "Unassigned"].append(r)
    people = defaultdict(lambda: {"shifts": 0, "no_show": 0, "late": 0, "early": 0, "overtime_min": 0})
    for r in done:
        p = people[r["person"]]
        p["shifts"] += 1
        p["no_show"] += r["status"] == "no_show"
        p["late"] += bool(r["late_min"])
        p["early"] += bool(r["early_leave_min"])
        p["overtime_min"] += r["overtime_min"]
    return {
        "shifts": len(done), "upcoming": sum(1 for r in rows if r["status"] == "upcoming"),
        "attendance_pct": round(100 * len(attended) / len(done), 1) if done else None,
        "no_shows": len(no_show),
        "late_pct": round(100 * sum(1 for r in attended if r["late_min"]) / len(attended), 1) if attended else None,
        "avg_late_min": round(sum(r["late_min"] for r in attended if r["late_min"]) / max(1, sum(1 for r in attended if r["late_min"])), 1) if attended else None,
        "early_leaves": sum(1 for r in attended if r["early_leave_min"]),
        "overtime_h": round(sum(r["overtime_min"] for r in attended) / 60, 1),
        "missed_clock_outs": sum(1 for r in done if r["status"] == "no_clock_out"),
        "planned_h": round(sum(r["planned_h"] for r in done), 1),
        "by_department": [{"department": d, "shifts": len(v),
                           "attendance_pct": round(100 * sum(1 for r in v if r["in"]) / len(v), 1),
                           "no_shows": sum(1 for r in v if r["status"] == "no_show"),
                           "late": sum(1 for r in v if r["late_min"]),
                           "overtime_h": round(sum(r["overtime_min"] for r in v) / 60, 1)} for d, v in sorted(by_dept.items())],
        "people": sorted(({"person": k, **v} for k, v in people.items()), key=lambda x: (-x["no_show"], -x["late"])),
        "recent_no_shows": [{"person": r["person"], "day": r["day"], "start": r["start"], "department": r["department"]}
                            for r in sorted(no_show, key=lambda r: (r["day"], r["start"]), reverse=True)[:15]],
    }


def coverage(conn, day: str, department: str = None) -> list:
    """Planned vs actually-on-duty headcount per hour for one day."""
    rows = adherence(conn, day, day)
    depts = {r["name"]: r["department"] for r in conn.execute("SELECT name, department FROM people")}
    ev = defaultdict(list)
    for r in conn.execute("SELECT employee, ts, action FROM events WHERE day=? ORDER BY ts", (day,)):
        ev[r["employee"]].append((datetime.fromisoformat(r["ts"]), r["action"]))
    d0 = datetime.fromisoformat(day + "T00:00:00")
    out = []
    for h in range(24):
        t = d0 + timedelta(hours=h, minutes=30)
        planned = sum(1 for r in rows if (not department or r["department"] == department)
                      and window(r)[0] <= t < window(r)[1])
        actual = 0
        for person, evs in ev.items():
            if department and depts.get(person) != department:
                continue
            state = None
            for ts, a in evs:
                if ts > t:
                    break
                state = a
            actual += state in ("IN", "BACK")
        out.append({"hour": h, "planned": planned, "actual": actual})
    return out
