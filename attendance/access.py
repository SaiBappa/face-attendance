"""
Areas and access: who is inside which area right now.

A location (kiosk) runs in one of two modes (kiosks.mode):
  attendance  the original IN / BREAK / BACK / OUT clock, optionally limited to some of the buttons
              (kiosks.actions, a comma list; empty = all four)
  gate        hands-free entry/exit for an area (kiosks.area): the camera recognises a live face and
              the movement is recorded straight away, no taps. kiosks.direction says what this
              screen does: 'in' (entry only), 'out' (exit only) or 'both' (toggles on each pass).

Several gates can share one area, so someone can enter at one and leave at another. Movements are
kept in their own table (`access`), never in `events`: rosters, lateness, break alerts and fatigue
hours keep reading attendance only. Presence is not bounded by the calendar day (an overnight stay
stays inside); an entry older than STALE_HOURS is reported as stale (probably a missed exit) and an
admin can close it with a manual OUT.
"""
import os
from datetime import datetime, timedelta

MODES = ("attendance", "gate")
DIRECTIONS = ("both", "in", "out")
ATTENDANCE_ACTIONS = ("IN", "BREAK", "BACK", "OUT")
STALE_HOURS = float(os.environ.get("ACCESS_STALE_HOURS", "16"))
REPEAT_SECONDS = int(os.environ.get("GATE_REPEAT_SECONDS", "60"))   # same person at the same gate: ignore repeats


def kiosk_actions(k: dict) -> tuple:
    """Attendance buttons offered at a location; none at a gate."""
    if (k.get("mode") or "attendance") == "gate":
        return ()
    chosen = [a for a in (k.get("actions") or "").upper().split(",") if a in ATTENDANCE_ACTIONS]
    return tuple(a for a in ATTENDANCE_ACTIONS if a in chosen) or ATTENDANCE_ACTIONS


def clean_actions(v) -> str:
    """Admin input (list or comma string) -> stored comma list; empty/all four -> NULL (= all)."""
    items = v if isinstance(v, (list, tuple)) else str(v or "").split(",")
    keep = [a for a in ATTENDANCE_ACTIONS if a in {str(x).strip().upper() for x in items}]
    return None if not keep or len(keep) == len(ATTENDANCE_ACTIONS) else ",".join(keep)


def is_gate(k: dict) -> bool:
    return (k.get("mode") or "attendance") == "gate" and bool((k.get("area") or "").strip())


def last_move(conn, person: str, area: str):
    return conn.execute("SELECT * FROM access WHERE person=? AND area=? ORDER BY id DESC LIMIT 1",
                        (person, area)).fetchone()


def _stale(ts: str, now: datetime) -> bool:
    return now - datetime.fromisoformat(ts) > timedelta(hours=STALE_HOURS)


def state(conn, person: str, k: dict, now: datetime) -> dict:
    """What a gate would record for `person` right now, and where they stand in its area."""
    area = k["area"].strip()
    last = last_move(conn, person, area)
    inside = bool(last) and last["direction"] == "IN"
    direction = (k.get("direction") or "both").lower()
    # toggling: a stale entry (missed exit) counts as outside, so the next pass is a fresh entry
    nxt = "IN" if direction == "in" else "OUT" if direction == "out" else \
        ("OUT" if inside and not _stale(last["ts"], now) else "IN")
    return {"area": area, "direction": direction, "inside": inside, "next": nxt,
            "since": last["ts"] if inside else None, "last_kiosk": last["kiosk"] if last else None}


def record(conn, person: str, k: dict, now: datetime, similarity=None, source: str = "gate",
           move: str = None, note: str = None) -> dict:
    """Record one pass through a gate. Returns the movement plus what it corrected (missed exit/entry)."""
    st = state(conn, person, k, now)
    last = last_move(conn, person, st["area"])
    # still standing at the same gate: repeat the last result instead of toggling them straight back out
    if last and last["kiosk"] == k["name"] and source == "gate" \
            and now - datetime.fromisoformat(last["ts"]) < timedelta(seconds=REPEAT_SECONDS):
        return {**st, "move": last["direction"], "duplicate": True, "ts": last["ts"]}
    move = move or st["next"]
    ts = now.isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO access (ts, day, person, area, kiosk, direction, similarity, source, note) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, now.date().isoformat(), person, st["area"], k["name"], move, similarity, source, note))
    return {**st, "move": move, "duplicate": False, "ts": ts,
            # an entry while already inside = the last exit wasn't seen; an exit while outside = entry wasn't seen
            "missed_exit": move == "IN" and st["inside"], "missed_entry": move == "OUT" and not st["inside"]}


def presence(conn, now: datetime) -> list:
    """Every area with the people currently inside (latest movement = IN), busiest first."""
    rows = conn.execute(
        """SELECT a.person, a.area, a.kiosk, a.ts FROM access a
           JOIN (SELECT MAX(id) id FROM access GROUP BY person, area) m ON m.id = a.id
           WHERE a.direction='IN' ORDER BY a.ts""").fetchall()
    areas = {r["area"]: [] for r in conn.execute(
        "SELECT DISTINCT area FROM kiosks WHERE mode='gate' AND area IS NOT NULL AND TRIM(area)!=''")}
    gates = {}
    for g in conn.execute("SELECT name, area, direction, last_seen FROM kiosks WHERE mode='gate' AND area IS NOT NULL"):
        gates.setdefault(g["area"].strip(), []).append({"name": g["name"], "direction": g["direction"] or "both",
                                                         "last_seen": g["last_seen"]})
    for r in rows:
        areas.setdefault(r["area"], []).append({"person": r["person"], "since": r["ts"], "kiosk": r["kiosk"],
                                                "stale": _stale(r["ts"], now)})
    out = [{"area": a, "inside": sorted(p, key=lambda x: x["since"]), "count": sum(not x["stale"] for x in p),
            "stale": sum(x["stale"] for x in p), "gates": gates.get(a, [])} for a, p in areas.items()]
    return sorted(out, key=lambda x: (-x["count"], x["area"].lower()))


def area_count(conn, area: str, now: datetime) -> int:
    cut = (now - timedelta(hours=STALE_HOURS)).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT COUNT(*) FROM access a JOIN (SELECT MAX(id) id FROM access WHERE area=? GROUP BY person) m ON m.id = a.id
           WHERE a.direction='IN' AND a.ts>=?""", (area, cut)).fetchone()[0]
