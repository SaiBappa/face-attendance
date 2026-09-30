"""
Emergency muster (roll call): who has to be accounted for, and who is safe.

Starting a muster snapshots the roll: everyone recorded inside the chosen areas (access.py; '*' = all
areas, including entries with no exit yet — better to look for someone who left than miss someone
inside) and, optionally, everyone currently clocked in (IN / BACK / BREAK). People then check in at a
muster-point kiosk (kiosks.mode = 'muster'): a recognised face is marked safe. Anyone not on the roll who
checks in is added as safe, so the board shows everyone at the assembly point. A supervisor can mark
someone safe by hand (confirmed by radio, at hospital…). Only one muster runs at a time.

Ending a muster can record everyone on the roll as having left the evacuated areas (source 'admin',
note 'Evacuation'), so the area headcounts start clean and no no-exit alerts follow.
"""
import json
from datetime import datetime, timedelta

import access


def _ts(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def active(conn):
    r = conn.execute("SELECT * FROM musters WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def start(conn, areas, on_duty: bool, note: str, by: str, now: datetime) -> dict:
    if active(conn):
        raise ValueError("A muster is already in progress — end it before starting another.")
    scope = sorted({a.strip() for a in (areas or []) if a and a.strip()}) or ["*"]
    cur = conn.execute("INSERT INTO musters (started, started_by, areas, on_duty, note, status) VALUES (?,?,?,?,?, 'active')",
                       (_ts(now), by or None, json.dumps(scope, ensure_ascii=False), 1 if on_duty else 0, note or None))
    mid = cur.lastrowid
    roll = {}
    for r in access._inside(conn):
        if r["demo"] or (scope != ["*"] and r["area"] not in scope):
            continue
        # someone inside two areas: keep the most recent entry as where they were last seen
        if r["person"] not in roll or r["ts"] > roll[r["person"]]["since"]:
            roll[r["person"]] = {"area": r["area"], "since": r["ts"], "stale": access._stale(r["ts"], now)}
    if on_duty:
        since = (now.date() - timedelta(days=1)).isoformat()
        for r in conn.execute(
                """SELECT e.employee, e.action, e.ts FROM events e JOIN (SELECT employee, MAX(id) id FROM events WHERE day>=? GROUP BY employee) m
                   ON m.id=e.id WHERE e.action IN ('IN','BACK','BREAK') AND e.demo=0""", (since,)):
            roll.setdefault(r["employee"], {"area": None, "since": r["ts"], "stale": False, "duty": r["action"]})
    conn.executemany(
        "INSERT INTO muster_roll (muster_id, person, area, since, note, status, on_list) VALUES (?,?,?,?,?, 'missing', 1)",
        [(mid, p, v["area"], v["since"],
          "entry with no exit for a long time — may have left" if v["stale"] else ("on duty" + (" (on break)" if v.get("duty") == "BREAK" else "")) if not v["area"] else None)
         for p, v in roll.items()])
    return {"id": mid, "areas": scope, "total": len(roll)}


def checkin(conn, person: str, kiosk: str, now: datetime) -> dict:
    """Muster-point check-in by face. Returns the person's roll status and the muster totals."""
    m = active(conn)
    if not m:
        return {"active": False}
    r = conn.execute("SELECT * FROM muster_roll WHERE muster_id=? AND person=?", (m["id"], person)).fetchone()
    already = bool(r) and r["status"] == "safe"
    if not r:
        conn.execute("""INSERT INTO muster_roll (muster_id, person, status, safe_ts, safe_by, safe_kiosk, on_list)
                        VALUES (?,?, 'safe', ?, 'kiosk', ?, 0)""", (m["id"], person, _ts(now), kiosk))
    elif not already:
        conn.execute("UPDATE muster_roll SET status='safe', safe_ts=?, safe_by='kiosk', safe_kiosk=? WHERE id=?",
                     (_ts(now), kiosk, r["id"]))
    return {"active": True, "muster": m["id"], "already": already, "on_list": bool(r), **totals(conn, m["id"])}


def mark(conn, mid: int, person: str, safe: bool, by: str, note: str, now: datetime):
    if safe:
        cur = conn.execute("UPDATE muster_roll SET status='safe', safe_ts=?, safe_by='admin', safe_kiosk=NULL, note=COALESCE(?, note) "
                           "WHERE muster_id=? AND person=?", (_ts(now), (f"{note} — {by}" if by else note) or None, mid, person))
    else:   # undo a mistaken check
        cur = conn.execute("UPDATE muster_roll SET status='missing', safe_ts=NULL, safe_by=NULL, safe_kiosk=NULL "
                           "WHERE muster_id=? AND person=? AND on_list=1", (mid, person))
    if not cur.rowcount:
        raise ValueError(f"{person} is not on this muster's list")


def end(conn, mid: int, by: str, mark_out: bool, now: datetime) -> dict:
    m = conn.execute("SELECT * FROM musters WHERE id=?", (mid,)).fetchone()
    if not m or m["status"] != "active":
        raise ValueError("This muster has already ended")
    conn.execute("UPDATE musters SET status='ended', ended=?, ended_by=? WHERE id=?", (_ts(now), by or None, mid))
    out = 0
    if mark_out:
        # only people confirmed safe have left: anyone still missing stays "inside" so area headcounts and
        # no-exit alerts keep pointing at them
        for r in conn.execute("SELECT person, area FROM muster_roll WHERE muster_id=? AND area IS NOT NULL AND status='safe'",
                              (mid,)).fetchall():
            last = access.last_move(conn, r["person"], r["area"])
            if last and last["direction"] == "IN":
                access.record(conn, r["person"], {"name": None, "area": r["area"], "direction": "out"}, now,
                              source="admin", move="OUT", note=f"Evacuation (muster #{mid})")
                out += 1
    return {"marked_out": out, **totals(conn, mid)}


def totals(conn, mid: int) -> dict:
    r = conn.execute("""SELECT COUNT(*) total, SUM(status='safe') safe, SUM(status='missing') missing, SUM(on_list=0) extra
                        FROM muster_roll WHERE muster_id=?""", (mid,)).fetchone()
    return {"total": r["total"] or 0, "safe": r["safe"] or 0, "missing": r["missing"] or 0, "extra": r["extra"] or 0}


def board(conn, mid: int) -> dict:
    m = conn.execute("SELECT * FROM musters WHERE id=?", (mid,)).fetchone()
    if not m:
        return None
    m = dict(m)
    m["areas"] = json.loads(m["areas"] or '["*"]')
    people = [dict(r) for r in conn.execute(
        """SELECT r.*, p.department, p.role FROM muster_roll r LEFT JOIN people p ON p.name = r.person
           WHERE r.muster_id=? ORDER BY r.status='safe', r.area IS NULL, r.area, r.person COLLATE NOCASE""", (mid,))]
    # where each missing person was last recorded (a gate exit after the muster started is a good sign)
    for x in people:
        if x["status"] == "missing" and x["area"]:
            last = access.last_move(conn, x["person"], x["area"])
            if last and last["direction"] == "OUT" and last["ts"] >= m["started"]:
                x["exited"] = last["ts"]
    return {"muster": m, "people": people, **totals(conn, mid)}


def history(conn, limit: int = 20) -> list:
    rows = [dict(r) for r in conn.execute("SELECT * FROM musters ORDER BY id DESC LIMIT ?", (limit,))]
    for r in rows:
        r["areas"] = json.loads(r["areas"] or '["*"]')
        r.update(totals(conn, r["id"]))
    return rows
