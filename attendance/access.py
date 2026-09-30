"""
Areas and access: who is inside which area right now.

A location (kiosk) runs in one of two modes (kiosks.mode):
  attendance  the original IN / BREAK / BACK / OUT clock, optionally limited to some of the buttons
              (kiosks.actions, a comma list; empty = all four)
  gate        hands-free entry/exit for an area (kiosks.area): the camera recognises a live face and
              the movement is recorded straight away, no taps. kiosks.direction says what this
              screen does: 'in' (entry only), 'out' (exit only) or 'both' (toggles on each pass).

An area can ask why people are entering (areas table): with ask_reason on, an entry is only recorded
once the person picks one of the area's reasons on the gate screen (restricted rooms, stores, plant
rooms…). Exits stay hands-free.

Per area (Admin → Areas) there can also be: the lowest pass zone colour allowed in (an entry by someone
whose pass doesn't cover it is still recorded — they are inside — but flagged and alerted at once), a
maximum stay and a capacity. alerts.py turns those, entry/exit mismatches (passback) and entries with
no exit into supervisor alerts; `due_alerts()` below says which are due.

Several gates can share one area, so someone can enter at one and leave at another. Movements are
kept in their own table (`access`), never in `events`: rosters, lateness, break alerts and fatigue
hours keep reading attendance only. Presence is not bounded by the calendar day (an overnight stay
stays inside); an entry older than STALE_HOURS is reported as stale (probably a missed exit) and an
admin can close it with a manual OUT.
"""
import json
import os
from datetime import datetime, timedelta

MODES = ("attendance", "gate", "muster")   # muster: emergency muster point (muster.py)
DIRECTIONS = ("both", "in", "out")
ATTENDANCE_ACTIONS = ("IN", "BREAK", "BACK", "OUT")
STALE_HOURS = float(os.environ.get("ACCESS_STALE_HOURS", "16"))
# same person at the same gate within this: a repeat, not a new pass. The kiosk itself holds someone until they
# leave the frame, so this only covers a face-detection blip; longer would swallow a quick out-and-back.
REPEAT_SECONDS = int(os.environ.get("GATE_REPEAT_SECONDS", "10"))
EXIT_GRACE_MIN = int(os.environ.get("AREA_EXIT_GRACE_MINUTES", "30"))  # still inside this long after clocking OUT -> alert
# authorised airside zone colours on the security pass, highest access first (people.zone)
ZONES = ("green", "red", "orange", "blue", "yellow", "white")


def kiosk_actions(k: dict) -> tuple:
    """Attendance buttons offered at a location; none at a gate or muster point."""
    if (k.get("mode") or "attendance") != "attendance":
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


def settings(conn, area: str) -> dict:
    r = conn.execute("SELECT * FROM areas WHERE name=?", (area,)).fetchone()
    try:
        reasons = [x for x in json.loads(r["reasons"] or "[]") if str(x).strip()] if r else []
    except ValueError:
        reasons = []
    return {"name": area, "reasons": reasons, "ask_reason": bool(r and r["ask_reason"] and reasons),
            "min_zone": (r["min_zone"] if r and r["min_zone"] in ZONES else None),
            "max_minutes": (r["max_minutes"] or None) if r else None, "capacity": (r["capacity"] or None) if r else None}


def _pos_int(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def save_settings(conn, area: str, reasons, ask_reason: bool, min_zone=None, max_minutes=None, capacity=None):
    clean, seen = [], set()
    for x in reasons or []:
        x = str(x).strip()[:60]
        if x and x.lower() not in seen:
            seen.add(x.lower()); clean.append(x)
    zone = (min_zone or "").strip().lower() or None
    if zone and zone not in ZONES:
        raise ValueError("Zone must be one of: " + ", ".join(z.capitalize() for z in ZONES))
    conn.execute("""INSERT INTO areas (name, reasons, ask_reason, min_zone, max_minutes, capacity) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(name) DO UPDATE SET reasons=excluded.reasons, ask_reason=excluded.ask_reason,
                    min_zone=excluded.min_zone, max_minutes=excluded.max_minutes, capacity=excluded.capacity""",
                 (area, json.dumps(clean[:30], ensure_ascii=False), 1 if ask_reason and clean else 0,
                  zone, _pos_int(max_minutes), _pos_int(capacity)))


def zone_ok(person_zone, min_zone) -> bool:
    """Does a pass of `person_zone` cover an area open to `min_zone` and higher? No rule = everyone.
    No zone on file while the area has a rule = not authorised."""
    if not min_zone:
        return True
    z = (person_zone or "").strip().lower()
    return z in ZONES and ZONES.index(z) <= ZONES.index(min_zone)


class ReasonRequired(ValueError):
    pass


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
    cfg = settings(conn, area)
    pz = conn.execute("SELECT zone FROM people WHERE name=?", (person,)).fetchone()
    pz = (pz["zone"] or None) if pz else None
    authorised = zone_ok(pz, cfg["min_zone"])
    return {"area": area, "direction": direction, "inside": inside, "next": nxt,
            "stale": inside and _stale(last["ts"], now), "authorised": authorised,
            "zone": pz, "min_zone": cfg["min_zone"],
            "since": last["ts"] if inside else None, "last_kiosk": last["kiosk"] if last else None,
            "reason": last["reason"] if inside else None,
            # still at the gate they just used: the next pass is a repeat, don't ask for a reason again
            "repeat": _repeat(last, k.get("name"), now),
            # someone not authorised isn't asked why: the entry is flagged either way
            "ask_reason": cfg["ask_reason"] and nxt == "IN" and authorised,
            "reasons": cfg["reasons"] if cfg["ask_reason"] and nxt == "IN" and authorised else []}


def _repeat(last, kiosk, now: datetime) -> bool:
    return bool(last) and kiosk is not None and last["kiosk"] == kiosk \
        and now - datetime.fromisoformat(last["ts"]) < timedelta(seconds=REPEAT_SECONDS)


def record(conn, person: str, k: dict, now: datetime, similarity=None, source: str = "gate",
           move: str = None, note: str = None, reason: str = None) -> dict:
    """Record one pass through a gate. Returns the movement plus what it corrected (missed exit/entry) and
    its `flag`: zone (entered an area their pass doesn't cover) or passback (entry/exit mismatch).
    Raises ReasonRequired for an entry into an ask-reason area without one of its reasons."""
    st = state(conn, person, k, now)
    last = last_move(conn, person, st["area"])
    # still standing at the same gate: repeat the last result instead of toggling them straight back out
    if source == "gate" and st["repeat"]:
        return {**st, "move": last["direction"], "duplicate": True, "ts": last["ts"], "reason": last["reason"]}
    move = move or st["next"]
    reason = (reason or "").strip() or None
    if move == "IN" and source == "gate" and st["authorised"]:
        offered = settings(conn, st["area"])["reasons"]
        if reason and reason not in offered:
            raise ReasonRequired(f"{reason!r} isn't a reason for {st['area']}")
        if st["ask_reason"] and not reason:
            raise ReasonRequired(f"Choose why you're entering {st['area']}")
    else:
        reason = None
    # an entry while already inside = the last exit wasn't seen; an exit while outside = entry wasn't seen.
    # A stale entry (> STALE_HOURS) is the no-exit alert's business, not a passback.
    missed_exit = move == "IN" and st["inside"]
    missed_entry = move == "OUT" and not st["inside"]
    flag = None
    if source == "gate":
        if move == "IN" and not st["authorised"]:
            flag = "zone"
        elif (missed_exit and not st["stale"]) or missed_entry:
            flag = "passback"
    ts = now.isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO access (ts, day, person, area, kiosk, direction, similarity, source, note, reason, flag) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, now.date().isoformat(), person, st["area"], k["name"], move, similarity, source, note, reason, flag))
    return {**st, "move": move, "duplicate": False, "ts": ts, "reason": reason, "flag": flag,
            "missed_exit": missed_exit, "missed_entry": missed_entry}


def _inside(conn):
    """Latest movement per (person, area) where it is an entry, oldest first."""
    return conn.execute(
        """SELECT a.* FROM access a
           JOIN (SELECT MAX(id) id FROM access GROUP BY person, area) m ON m.id = a.id
           WHERE a.direction='IN' ORDER BY a.ts""").fetchall()


def _minutes(ts: str, now: datetime) -> int:
    return int((now - datetime.fromisoformat(ts)).total_seconds() // 60)


def presence(conn, now: datetime) -> list:
    """Every area with the people currently inside (latest movement = IN), busiest first."""
    rows = _inside(conn)
    areas = {r["area"]: [] for r in conn.execute(
        "SELECT DISTINCT area FROM kiosks WHERE mode='gate' AND area IS NOT NULL AND TRIM(area)!=''")}
    gates = {}
    for g in conn.execute("SELECT name, area, direction, last_seen FROM kiosks WHERE mode='gate' AND area IS NOT NULL"):
        gates.setdefault(g["area"].strip(), []).append({"name": g["name"], "direction": g["direction"] or "both",
                                                         "last_seen": g["last_seen"]})
    for r in rows:
        areas.setdefault(r["area"], []).append({"person": r["person"], "since": r["ts"], "kiosk": r["kiosk"],
                                                "reason": r["reason"], "flag": r["flag"], "stale": _stale(r["ts"], now)})
    cfgs = {a: settings(conn, a) for a in areas}
    for a, people in areas.items():
        mx = cfgs[a]["max_minutes"]
        for x in people:
            x["overstay"] = bool(mx) and not x["stale"] and _minutes(x["since"], now) > mx
    out = [{"area": a, "inside": sorted(p, key=lambda x: x["since"]), "count": sum(not x["stale"] for x in p),
            "stale": sum(x["stale"] for x in p), "gates": gates.get(a, []), "settings": cfgs[a]}
           for a, p in areas.items()]
    return sorted(out, key=lambda x: (-x["count"], x["area"].lower()))


def area_count(conn, area: str, now: datetime) -> int:
    cut = (now - timedelta(hours=STALE_HOURS)).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT COUNT(*) FROM access a JOIN (SELECT MAX(id) id FROM access WHERE area=? GROUP BY person) m ON m.id = a.id
           WHERE a.direction='IN' AND a.ts>=?""", (area, cut)).fetchone()[0]


def due_alerts(conn, now: datetime) -> list:
    """Area alerts that are due now, as dicts for alerts._add. Keys are stable, so re-scans never repeat one:
      overstay   inside longer than the area's maximum stay (once per entry)
      capacity   more people inside than the area's capacity (once per area per hour)
      no_exit    entry with no exit for STALE_HOURS, or still inside EXIT_GRACE_MIN after clocking OUT (once per entry)"""
    out, counts, cfgs = [], {}, {}
    for r in _inside(conn):
        if r["demo"]:
            continue
        area, person, mins = r["area"], r["person"], _minutes(r["ts"], now)
        cfg = cfgs.setdefault(area, settings(conn, area))
        entry = f"{person}|{area}|{r['ts']}"
        if _stale(r["ts"], now):
            out.append({"key": "no_exit|" + entry, "type": "no_exit", "person": person, "kiosk": r["kiosk"],
                        "text": f"{person} entered {area} at {r['ts'][:16].replace('T', ' ')} and no exit has been recorded since "
                                f"({mins // 60} h). Left without passing a gate? Mark them out in Areas once confirmed."})
            continue
        counts[area] = counts.get(area, 0) + 1
        if cfg["max_minutes"] and mins > cfg["max_minutes"]:
            out.append({"key": "overstay|" + entry, "type": "overstay", "person": person, "kiosk": r["kiosk"],
                        "text": f"{person} has been inside {area} for {mins} min (limit {cfg['max_minutes']})"
                                + (f" — reason: {r['reason']}" if r["reason"] else "") + ". Check they are OK."})
        # clocked out of attendance after entering, but no exit from the area
        out_ev = conn.execute("SELECT ts FROM events WHERE employee=? AND action='OUT' AND ts>? ORDER BY ts DESC LIMIT 1",
                              (person, r["ts"])).fetchone()
        if out_ev and _minutes(out_ev["ts"], now) >= EXIT_GRACE_MIN:
            out.append({"key": "no_exit|" + entry, "type": "no_exit", "person": person, "kiosk": r["kiosk"],
                        "text": f"{person} clocked out at {out_ev['ts'][11:16]} but is still recorded inside {area} "
                                f"(entered {r['ts'][11:16]}). Missed the exit gate, or still inside?"})
    for area, n in counts.items():
        cap = cfgs[area]["capacity"]
        if cap and n > cap:
            out.append({"key": f"capacity|{area}|{now:%Y-%m-%dT%H}", "type": "capacity", "person": None, "kiosk": None,
                        "text": f"{area} has {n} people inside — over its capacity of {cap}."})
    return out
