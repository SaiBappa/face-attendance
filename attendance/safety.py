"""
Ramp safety pack: PPE check and fatigue watch at clock-in.

  * Rules (safety_rules) are named profiles: which PPE items must be confirmed, whether the
    fatigue check runs, and the rest/hours limits used for scoring.
  * The location decides (kiosks.safety_rule): None = no safety check there; DEPT_RULE = each
    worker's own department rule (the rule named after their department), on IN; or one named rule
    that everyone entering must meet, on IN and BACK. Rules are never merged.
  * Hi-vis is detected by the recognizer (share of fluorescent pixels on the torso); every
    other item is confirmed by the worker with one tap. The camera decides hi-vis: a tap can't
    override "not seen", and where an enforced location requires hi-vis it must be seen (no reading = missing).
  * Fatigue risk is a transparent, rule-based score (0-100) — not a medical assessment:
      short rest since last shift, long hours in the last 24 h / 7 days, many consecutive
      days, night work, a recent "tired / unwell" remark to Aura, and the worker's own
      rating of how rested they feel. Each factor is listed so supervisors see *why*.
  * A location set to "enforce" refuses entry when required PPE is missing (HIGH fatigue is still only
    flagged — the score is an indicator, not a fitness-for-duty decision): the check is recorded
    as blocked and /api/event rejects the clock-in until a passing check is made there.
  * A check with missing PPE or HIGH fatigue is "flagged" and pushed to supervisors.
  * A rule with auto_pass lets people straight through, no checklist, when the camera confirms every
    PPE item it requires (only CAMERA_ITEMS can be; today that's hi-vis). The fatigue score is still
    worked out from the roster and a HIGH score is still flagged; the rested question is skipped.
"""
import json
import os
from datetime import date, datetime, timedelta

from store import db

PPE = {  # key -> (icon, label)
    "hi_vis": ("🦺", "Hi-vis vest"),
    "id_badge": ("🪪", "Airside ID badge"),
    "ear": ("🎧", "Ear protection"),
    "shoes": ("🥾", "Safety shoes"),
    "gloves": ("🧤", "Gloves"),
    "helmet": ("⛑️", "Hard hat"),
    "eye": ("🥽", "Eye protection"),
}
CAMERA_ITEMS = {"hi_vis"}   # PPE the camera can confirm on its own
HIVIS_MIN = float(os.environ.get("HIVIS_MIN_SHARE", "0.12"))  # share of fluorescent torso pixels
RESTED = {1: "Exhausted", 2: "Tired", 3: "OK", 4: "Rested", 5: "Very rested"}
DEFAULTS = {"ppe": [], "fatigue": 0, "auto_pass": 0, "min_rest_h": 10.0, "max_24h_h": 12.0, "max_7d_h": 60.0, "max_days": 6}
ENTRY_ACTIONS = ("IN", "BACK")   # actions that enter a location's area
CHECK_VALID_S = 900              # an enforced location accepts a passing check this recent


def rules_for(conn, department: str) -> dict:
    r = conn.execute("SELECT * FROM safety_rules WHERE department=?", (department or "",)).fetchone()
    if not r:
        return dict(DEFAULTS)
    d = dict(r)
    d["ppe"] = [x for x in json.loads(d.get("ppe") or "[]") if x in PPE]
    return {**DEFAULTS, **{k: v for k, v in d.items() if v is not None}}


def all_rules(conn) -> list:
    out = []
    for r in conn.execute("SELECT * FROM safety_rules ORDER BY department"):
        d = dict(r)
        d["ppe"] = json.loads(d.get("ppe") or "[]")
        out.append(d)
    return out


def save_rule(conn, department: str, p: dict):
    ppe = [x for x in (p.get("ppe") or []) if x in PPE]
    conn.execute(
        """INSERT INTO safety_rules (department, ppe, fatigue, min_rest_h, max_24h_h, max_7d_h, max_days, auto_pass)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(department) DO UPDATE SET ppe=excluded.ppe, fatigue=excluded.fatigue, min_rest_h=excluded.min_rest_h,
             max_24h_h=excluded.max_24h_h, max_7d_h=excluded.max_7d_h, max_days=excluded.max_days, auto_pass=excluded.auto_pass""",
        (department, json.dumps(ppe), 1 if p.get("fatigue") else 0,
         float(p.get("min_rest_h") or DEFAULTS["min_rest_h"]), float(p.get("max_24h_h") or DEFAULTS["max_24h_h"]),
         float(p.get("max_7d_h") or DEFAULTS["max_7d_h"]), int(p.get("max_days") or DEFAULTS["max_days"]),
         1 if p.get("auto_pass") else 0))


DEPT_RULE = "@department"   # kiosks.safety_rule value: "each worker's own department rule"


def location_rule(conn, kiosk: str) -> dict:
    """The rule linked to a location (None = no safety check; DEPT_RULE = resolved per worker)."""
    r = conn.execute("SELECT name, zone, safety_rule, safety_enforce FROM kiosks WHERE name=?", (kiosk or "",)).fetchone()
    linked = (r["safety_rule"] or None) if r else None
    rule = rules_for(conn, linked) if linked and linked != DEPT_RULE else dict(DEFAULTS)
    return {"name": kiosk, "zone": r["zone"] if r else None, "rule": linked, "ppe": rule["ppe"],
            "fatigue": int(rule["fatigue"] or 0), "enforce": int(r["safety_enforce"] or 0) if r else 0}


def all_locations(conn) -> list:
    return [location_rule(conn, r["name"]) for r in conn.execute("SELECT name FROM kiosks ORDER BY name COLLATE NOCASE")]


def link_location(conn, kiosk: str, rule: str, enforce: bool):
    conn.execute("INSERT OR IGNORE INTO kiosks (name, zone, headline) VALUES (?,?,?)", (kiosk, kiosk, kiosk))
    conn.execute("UPDATE kiosks SET safety_rule=?, safety_enforce=? WHERE name=?",
                 ((rule or "").strip() or None, 1 if enforce else 0, kiosk))


def effective(conn, department: str, kiosk: str, action: str = "IN") -> dict:
    """What this location asks of this worker: nothing, their department's rule (on IN), or the
    location's named rule (on IN / BACK). The location's setting is the only source — never merged."""
    action = (action or "IN").upper()
    loc = location_rule(conn, kiosk) if action in ENTRY_ACTIONS else None
    if not loc or not loc["rule"] or (loc["rule"] == DEPT_RULE and action != "IN"):
        rule = {**DEFAULTS, "ppe": [], "fatigue": 0}
    elif loc["rule"] == DEPT_RULE:
        rule = rules_for(conn, department)   # DEFAULTS (no checks) when the department has no rule
    else:
        rule = rules_for(conn, loc["rule"])
    ppe = [k for k in PPE if k in rule["ppe"]]
    asks = bool(ppe) or bool(rule["fatigue"])
    return {**rule, "ppe": ppe, "fatigue": 1 if rule["fatigue"] else 0,
            "enforce": bool(loc and loc["enforce"]) and bool(ppe),
            "location": ((loc.get("zone") or kiosk) if asks else None), "location_rule": loc["rule"] if loc else None,
            "location_ppe": ppe}


def entry_cleared(conn, name: str, kiosk: str, now: datetime = None) -> bool:
    """For an enforced location: the latest check here in the last CHECK_VALID_S seconds passed."""
    now = now or datetime.now()
    r = conn.execute("SELECT ts, blocked FROM safety_checks WHERE person=? AND kiosk=? ORDER BY id DESC LIMIT 1",
                     (name, kiosk)).fetchone()
    return bool(r) and not r["blocked"] and (now - datetime.fromisoformat(r["ts"])).total_seconds() <= CHECK_VALID_S


# ----------------------------------------------------------------------------- fatigue
OPEN_SHIFT_H = 10   # a shift with no OUT is assumed to last at most this long
REJOIN_H = 1        # an IN this soon after an OUT resumes that shift (a mistaken OUT is not a rest)


def _shifts(conn, name: str, now: datetime) -> list:
    """[(in_dt, out_dt|None)] for the last 10 days, from IN/OUT events, oldest first.
    An IN while a shift is already open is a re-scan (another kiosk, a re-entry), not a new shift —
    unless the open one is older than OPEN_SHIFT_H (a forgotten OUT), which is then closed at that length."""
    since = (now - timedelta(days=10)).date().isoformat()
    rows = conn.execute("SELECT ts, action FROM events WHERE employee=? AND day>=? ORDER BY ts", (name, since)).fetchall()
    shifts, cur = [], None
    for r in rows:
        t = datetime.fromisoformat(r["ts"])
        if r["action"] == "IN":
            if cur and t - cur < timedelta(hours=OPEN_SHIFT_H):
                continue
            if cur:
                shifts.append((cur, cur + timedelta(hours=OPEN_SHIFT_H)))
            elif shifts and t - shifts[-1][1] < timedelta(hours=REJOIN_H):
                cur = shifts.pop()[0]
                continue
            cur = t
        elif r["action"] == "OUT" and cur:
            shifts.append((cur, t))
            cur = None
    if cur:
        shifts.append((cur, None))
    return shifts


def fatigue(conn, name: str, rules: dict, now: datetime = None, rested: int = None) -> dict:
    now = now or datetime.now()
    shifts = [s for s in _shifts(conn, name, now) if s[0] <= now]
    # the shift being started (or re-entered) now is the current one; rest is measured up to its start
    current = now
    if shifts and shifts[-1][1] is None:
        s0 = shifts.pop()[0]
        if now - s0 < timedelta(hours=OPEN_SHIFT_H):
            current = s0
        else:   # a forgotten OUT: that shift is over, this is a new one
            shifts.append((s0, s0 + timedelta(hours=OPEN_SHIFT_H)))
    factors, score = [], 0

    def worked_between(a, b):
        spans = sorted((max(s, a), min(e or min(now, s + timedelta(hours=OPEN_SHIFT_H)), b)) for s, e in shifts)
        tot, reach = 0.0, a
        for lo, hi in spans:   # merged, so overlapping records are never counted twice
            lo = max(lo, reach)
            if hi > lo:
                tot += (hi - lo).total_seconds() / 3600
                reach = hi
        return tot

    # hours already worked in the current shift count too
    if current < now:
        shifts.append((current, None))
    closed = [s for s in shifts if s[1] and s[1] <= current]
    rest_h = max(0.0, (current - closed[-1][1]).total_seconds() / 3600) if closed else None
    if rest_h is not None and rest_h < rules["min_rest_h"]:
        pts = min(40, int(40 * (rules["min_rest_h"] - rest_h) / rules["min_rest_h"]) + 15)
        score += pts
        factors.append({"key": "short_rest", "points": pts, "text": f"Only {rest_h:.1f} h rest since last shift (min {rules['min_rest_h']:g} h)"})
    h24 = worked_between(now - timedelta(hours=24), now)
    if h24 > rules["max_24h_h"] * 0.75:
        pts = 20 if h24 > rules["max_24h_h"] else 10
        score += pts
        factors.append({"key": "hours_24h", "points": pts, "text": f"{h24:.1f} h worked in the last 24 h"})
    h7 = worked_between(now - timedelta(days=7), now)
    if h7 > rules["max_7d_h"] * 0.85:
        pts = 20 if h7 > rules["max_7d_h"] else 10
        score += pts
        factors.append({"key": "hours_7d", "points": pts, "text": f"{h7:.0f} h worked in the last 7 days"})
    days, d = 0, now.date()
    worked_days = {s[0].date() for s in shifts}
    while (d - timedelta(days=1)) in worked_days:
        days += 1
        d -= timedelta(days=1)
    if days >= rules["max_days"]:
        score += 10
        factors.append({"key": "consecutive_days", "points": 10, "text": f"{days} consecutive days worked"})
    if now.hour >= 22 or now.hour < 5:
        score += 10
        factors.append({"key": "night", "points": 10, "text": "Night shift (22:00–05:00 body-clock low)"})
    recent = conn.execute(
        "SELECT kind FROM memories WHERE person=? AND ts>=? AND kind IN ('tired','unwell')",
        (name, (now - timedelta(hours=24)).isoformat(timespec="seconds"))).fetchall()
    if recent:
        score += 15
        factors.append({"key": "said_tired", "points": 15, "text": "Told Aura they were tired or unwell in the last 24 h"})
    if rested is not None:
        pts = {1: 35, 2: 20, 3: 5, 4: 0, 5: 0}.get(int(rested), 0)
        if pts:
            score += pts
            factors.append({"key": "self_rating", "points": pts, "text": f"Self-rated: {RESTED.get(int(rested))}"})
    score = min(100, score)
    level = "high" if score >= 60 else "moderate" if score >= 30 else "low"
    return {"score": score, "level": level, "factors": factors,
            "rest_h": round(rest_h, 1) if rest_h is not None else None, "hours_24h": round(h24, 1), "hours_7d": round(h7, 1),
            "consecutive_days": days}


# ----------------------------------------------------------------------------- checks
def hivis_seen(share, enforce: bool):
    """True / False from the camera's reading; None (worker confirms) only when there is no reading
    and the location isn't enforced — an enforced location never takes hi-vis on trust."""
    if share is None:
        return False if enforce else None
    return share >= HIVIS_MIN


def camera_clears(rules: dict, hivis_share) -> bool:
    """auto_pass rule, and the camera itself confirms every PPE item it requires."""
    ppe = rules["ppe"]
    return bool(rules.get("auto_pass")) and bool(ppe) and all(k in CAMERA_ITEMS for k in ppe) \
        and all(hivis_seen(hivis_share, True) is True for k in ppe if k == "hi_vis")


def precheck(conn, name: str, profile: dict, hivis_share: float = None, kiosk: str = None, action: str = "IN") -> dict:
    rules = effective(conn, profile.get("department"), kiosk, action)
    required = bool(rules["ppe"]) or bool(rules["fatigue"])
    items = []
    for k in rules["ppe"]:
        icon, label = PPE[k]
        gate = rules["enforce"] and k in rules["location_ppe"]   # this item alone can refuse entry here
        auto = hivis_seen(hivis_share, gate) if k == "hi_vis" else None
        camera = None if k != "hi_vis" else "unseen_torso" if hivis_share is None else "seen" if auto else "not_seen"
        items.append({"key": k, "icon": icon, "label": label, "auto": auto, "location": k in rules["location_ppe"],
                      "gate": gate, "locked": auto is False, "camera": camera})   # the worker can't tap over the camera's "not seen"
    return {"required": required, "department": profile.get("department"), "items": items,
            "location": rules["location"], "enforce": rules["enforce"],
            "ask_rested": bool(rules["fatigue"]),
            "auto_pass": camera_clears(rules, hivis_share),   # kiosk skips the checklist
            "fatigue": fatigue(conn, name, rules) if rules["fatigue"] else None}


def record(conn, name: str, profile: dict, kiosk: str, items: dict, hivis_share, rested, demo: int = 0, now: datetime = None,
           action: str = "IN", auto: bool = False) -> dict:
    now = now or datetime.now()
    rules = effective(conn, profile.get("department"), kiosk, action)
    if auto:   # the kiosk skipped the checklist: only valid when the camera itself still confirms everything
        if not camera_clears(rules, hivis_share):
            raise ValueError("The camera can't confirm all required PPE — please complete the safety check.")
        items, rested = {k: "ok" for k in rules["ppe"]}, None
    fat = fatigue(conn, name, rules, now=now, rested=rested) if rules["fatigue"] else None
    items = dict(items)
    if "hi_vis" in rules["ppe"] and hivis_seen(hivis_share, rules["enforce"] and "hi_vis" in rules["location_ppe"]) is False:
        items["hi_vis"] = "missing"   # camera wins over a tap
    missing = [k for k in rules["ppe"] if items.get(k) != "ok"]
    flagged = bool(missing) or bool(fat and fat["level"] == "high")
    # only the location's own items refuse entry; department items and fatigue are flagged, never a lockout
    blocked = rules["enforce"] and any(k in rules["location_ppe"] for k in missing)
    conn.execute(
        """INSERT INTO safety_checks (ts, day, person, department, kiosk, items, missing, hivis, rested, fatigue_score, fatigue_level, factors, result, demo, action, blocked, auto)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now.isoformat(timespec="seconds"), now.date().isoformat(), name, profile.get("department"), kiosk,
         json.dumps(items), ",".join(missing) or None, hivis_share, rested,
         fat["score"] if fat else None, fat["level"] if fat else None, json.dumps(fat["factors"]) if fat else None,
         "flagged" if flagged else "pass", demo, (action or "IN").upper(), 1 if blocked else 0, 1 if auto else 0))
    return {"result": "flagged" if flagged else "pass", "missing": missing, "fatigue": fat, "auto": bool(auto),
            "blocked": blocked, "enforce": rules["enforce"], "location": rules["location"]}


def summary(conn, date_from: str, date_to: str) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM safety_checks WHERE day BETWEEN ? AND ? ORDER BY ts", (date_from, date_to))]
    from collections import Counter, defaultdict
    miss = Counter(m for r in rows for m in (r["missing"] or "").split(",") if m)
    by_dept = defaultdict(list)
    for r in rows:
        by_dept[r["department"] or "—"].append(r)
    high = [r for r in rows if r["fatigue_level"] == "high"]
    latest_high = {}
    for r in high:
        latest_high[r["person"]] = r
    return {
        "checks": len(rows),
        "ppe_compliance_pct": round(100 * sum(1 for r in rows if not r["missing"]) / len(rows), 1) if rows else None,
        "flagged": sum(1 for r in rows if r["result"] == "flagged"),
        "blocked": sum(1 for r in rows if r["blocked"]),
        "missing_items": {k: {"count": v, "icon": PPE.get(k, ("", k))[0], "label": PPE.get(k, ("", k))[1]} for k, v in miss.most_common()},
        "fatigue_levels": dict(Counter(r["fatigue_level"] for r in rows if r["fatigue_level"])),
        "avg_fatigue": round(sum(r["fatigue_score"] for r in rows if r["fatigue_score"] is not None) / max(1, sum(1 for r in rows if r["fatigue_score"] is not None)), 1) if rows else None,
        "hivis_auto_pct": (lambda hv: round(100 * sum(1 for r in hv if (r["hivis"] or 0) >= HIVIS_MIN) / len(hv), 1) if hv else None)(
            [r for r in rows if '"hi_vis"' in (r["items"] or "")]),
        "by_department": [{"department": d, "checks": len(v),
                           "ppe_compliance_pct": round(100 * sum(1 for r in v if not r["missing"]) / len(v), 1),
                           "high_fatigue": sum(1 for r in v if r["fatigue_level"] == "high")} for d, v in sorted(by_dept.items())],
        "high_fatigue": [{"person": p, "ts": r["ts"], "score": r["fatigue_score"], "factors": json.loads(r["factors"] or "[]")}
                         for p, r in sorted(latest_high.items(), key=lambda kv: kv[1]["ts"], reverse=True)][:12],
        "recent_flags": [{"person": r["person"], "ts": r["ts"], "missing": r["missing"], "fatigue": r["fatigue_level"], "kiosk": r["kiosk"],
                          "blocked": bool(r["blocked"])}
                         for r in reversed(rows) if r["result"] == "flagged"][:15],
        "catalog": {k: {"icon": v[0], "label": v[1]} for k, v in PPE.items()},
    }
