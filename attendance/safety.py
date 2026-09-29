"""
Ramp safety pack: PPE check and fatigue watch at clock-in.

  * Rules are per department (safety_rules): which PPE items must be confirmed, whether the
    fatigue check runs, and the rest/hours limits used for scoring.
  * Hi-vis is detected by the recognizer (share of fluorescent pixels on the torso); every
    other item is confirmed by the worker with one tap.
  * Fatigue risk is a transparent, rule-based score (0-100) — not a medical assessment:
      short rest since last shift, long hours in the last 24 h / 7 days, many consecutive
      days, night work, a recent "tired / unwell" remark to Aura, and the worker's own
      rating of how rested they feel. Each factor is listed so supervisors see *why*.
  * A check with missing PPE or HIGH fatigue is "flagged" and pushed to supervisors.
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
HIVIS_MIN = float(os.environ.get("HIVIS_MIN_SHARE", "0.12"))  # share of fluorescent torso pixels
RESTED = {1: "Exhausted", 2: "Tired", 3: "OK", 4: "Rested", 5: "Very rested"}
DEFAULTS = {"ppe": [], "fatigue": 0, "min_rest_h": 10.0, "max_24h_h": 12.0, "max_7d_h": 60.0, "max_days": 6}


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
        """INSERT INTO safety_rules (department, ppe, fatigue, min_rest_h, max_24h_h, max_7d_h, max_days)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(department) DO UPDATE SET ppe=excluded.ppe, fatigue=excluded.fatigue, min_rest_h=excluded.min_rest_h,
             max_24h_h=excluded.max_24h_h, max_7d_h=excluded.max_7d_h, max_days=excluded.max_days""",
        (department, json.dumps(ppe), 1 if p.get("fatigue") else 0,
         float(p.get("min_rest_h") or DEFAULTS["min_rest_h"]), float(p.get("max_24h_h") or DEFAULTS["max_24h_h"]),
         float(p.get("max_7d_h") or DEFAULTS["max_7d_h"]), int(p.get("max_days") or DEFAULTS["max_days"])))


# ----------------------------------------------------------------------------- fatigue
def _shifts(conn, name: str, now: datetime) -> list:
    """[(in_dt, out_dt|None)] for the last 10 days, from IN/OUT events."""
    since = (now - timedelta(days=10)).date().isoformat()
    rows = conn.execute("SELECT ts, action FROM events WHERE employee=? AND day>=? ORDER BY ts", (name, since)).fetchall()
    shifts, cur = [], None
    for r in rows:
        t = datetime.fromisoformat(r["ts"])
        if r["action"] == "IN":
            if cur:
                shifts.append((cur, None))
            cur = t
        elif r["action"] == "OUT" and cur:
            shifts.append((cur, t))
            cur = None
    if cur:
        shifts.append((cur, None))
    return shifts


def fatigue(conn, name: str, rules: dict, now: datetime = None, rested: int = None) -> dict:
    now = now or datetime.now()
    shifts = [s for s in _shifts(conn, name, now) if s[0] < now]
    # ignore a shift that started in the last minutes (this very clock-in)
    shifts = [s for s in shifts if (now - s[0]).total_seconds() > 600 or s[1]]
    factors, score = [], 0

    def worked_between(a, b):
        tot = 0.0
        for s, e in shifts:
            e = e or min(now, s + timedelta(hours=10))
            lo, hi = max(s, a), min(e, b)
            if hi > lo:
                tot += (hi - lo).total_seconds() / 3600
        return tot

    closed = [s for s in shifts if s[1]]
    rest_h = (now - closed[-1][1]).total_seconds() / 3600 if closed else None
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
def precheck(conn, name: str, profile: dict, hivis_share: float = None) -> dict:
    rules = rules_for(conn, profile.get("department"))
    required = bool(rules["ppe"]) or bool(rules["fatigue"])
    items = []
    for k in rules["ppe"]:
        icon, label = PPE[k]
        auto = None
        if k == "hi_vis" and hivis_share is not None:
            auto = hivis_share >= HIVIS_MIN
        items.append({"key": k, "icon": icon, "label": label, "auto": auto})
    return {"required": required, "department": profile.get("department"), "items": items,
            "ask_rested": bool(rules["fatigue"]),
            "fatigue": fatigue(conn, name, rules) if rules["fatigue"] else None}


def record(conn, name: str, profile: dict, kiosk: str, items: dict, hivis_share, rested, demo: int = 0, now: datetime = None) -> dict:
    now = now or datetime.now()
    rules = rules_for(conn, profile.get("department"))
    fat = fatigue(conn, name, rules, now=now, rested=rested) if rules["fatigue"] else None
    missing = [k for k in rules["ppe"] if items.get(k) != "ok"]
    flagged = bool(missing) or (fat and fat["level"] == "high")
    conn.execute(
        """INSERT INTO safety_checks (ts, day, person, department, kiosk, items, missing, hivis, rested, fatigue_score, fatigue_level, factors, result, demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now.isoformat(timespec="seconds"), now.date().isoformat(), name, profile.get("department"), kiosk,
         json.dumps(items), ",".join(missing) or None, hivis_share, rested,
         fat["score"] if fat else None, fat["level"] if fat else None, json.dumps(fat["factors"]) if fat else None,
         "flagged" if flagged else "pass", demo))
    return {"result": "flagged" if flagged else "pass", "missing": missing, "fatigue": fat}


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
        "recent_flags": [{"person": r["person"], "ts": r["ts"], "missing": r["missing"], "fatigue": r["fatigue_level"], "kiosk": r["kiosk"]}
                         for r in reversed(rows) if r["result"] == "flagged"][:15],
        "catalog": {k: {"icon": v[0], "label": v[1]} for k, v in PPE.items()},
    }
