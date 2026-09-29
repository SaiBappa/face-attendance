"""
Analytics behind the Insights page: attendance behaviour, mood, conversations, locations.

Everything is computed on request from the raw tables (events, sightings, interactions);
at a few hundred staff and a few months of data this is well under a second in SQLite+Python.
"""
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import safety
from brain import MOODS
from store import db

SHIFT_START = os.environ.get("SHIFT_START", "08:00")
GRACE_MIN = int(os.environ.get("LATE_GRACE_MINUTES", "5"))
LOW_MOOD = float(os.environ.get("LOW_MOOD_THRESHOLD", "-0.25"))
MIN_HOUR_SAMPLES = 5      # hours with fewer mood readings are left blank rather than drawn as spikes


def _mins(ts: str) -> int:
    t = ts[11:16]
    return int(t[:2]) * 60 + int(t[3:5])


def _hhmm(m) -> str:
    if m is None:
        return None
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def _dur(m) -> str:
    if not m:
        return "0m"
    m = int(round(m))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def workdays(rows) -> dict:
    """events rows (ordered by ts) -> {(person, day): {in, out, hours_min, break_min, breaks}}"""
    by = defaultdict(list)
    for r in rows:
        by[(r["employee"], r["day"])].append(r)
    out = {}
    for key, evs in by.items():
        first_in = next((e for e in evs if e["action"] == "IN"), None)
        last_out = next((e for e in reversed(evs) if e["action"] == "OUT"), None)
        brk, n_breaks, open_break = 0, 0, None
        for e in evs:
            if e["action"] == "BREAK":
                open_break = e["ts"]
                n_breaks += 1
            elif e["action"] == "BACK" and open_break:
                brk += (datetime.fromisoformat(e["ts"]) - datetime.fromisoformat(open_break)).total_seconds() / 60
                open_break = None
        worked = None
        if first_in and last_out and last_out["ts"] > first_in["ts"]:
            worked = (datetime.fromisoformat(last_out["ts"]) - datetime.fromisoformat(first_in["ts"])).total_seconds() / 60 - brk
        out[key] = {
            "in": _mins(first_in["ts"]) if first_in else None,
            "out": _mins(last_out["ts"]) if last_out else None,
            "worked": worked, "break": brk, "breaks": n_breaks,
        }
    return out


def _profiles(conn) -> dict:
    return {r["name"]: dict(r) for r in conn.execute("SELECT * FROM people")}


def _shift_min(profile) -> int:
    s = (profile or {}).get("shift_start") or SHIFT_START
    return int(s[:2]) * 60 + int(s[3:5])


def _valence(label):
    return MOODS.get(label, (None,))[0]


def _group(pairs) -> dict:
    out = defaultdict(list)
    for k, v in pairs:
        out[k].append(v)
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def overview(date_from: str, date_to: str, kiosk: str = None) -> dict:
    with db() as conn:
        profiles = _profiles(conn)
        kf, ka = ("AND kiosk=?", [kiosk]) if kiosk else ("", [])
        ev = conn.execute(f"SELECT * FROM events WHERE day BETWEEN ? AND ? {kf} ORDER BY ts", [date_from, date_to] + ka).fetchall()
        si = conn.execute(f"SELECT * FROM sightings WHERE day BETWEEN ? AND ? {kf} ORDER BY ts", [date_from, date_to] + ka).fetchall()
        it = conn.execute(f"SELECT * FROM interactions WHERE day BETWEEN ? AND ? {kf} ORDER BY ts", [date_from, date_to] + ka).fetchall()
        rq = conn.execute(f"SELECT * FROM requests WHERE day BETWEEN ? AND ? {kf} ORDER BY ts", [date_from, date_to] + ka).fetchall()
        # previous period of equal length, for trend arrows
        d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
        span = (d1 - d0).days + 1
        p0, p1 = (d0 - timedelta(days=span)).isoformat(), (d0 - timedelta(days=1)).isoformat()
        prev_si = conn.execute(f"SELECT kind, day, valence FROM sightings WHERE valence IS NOT NULL AND day BETWEEN ? AND ? {kf}", [p0, p1] + ka).fetchall()
        prev_ev = conn.execute(f"SELECT * FROM events WHERE day BETWEEN ? AND ? {kf} ORDER BY ts", [p0, p1] + ka).fetchall()

    wd = workdays(ev)
    prev_wd = workdays(prev_ev)

    def on_time(key, rec):
        return rec["in"] is not None and rec["in"] <= _shift_min(profiles.get(key[0])) + GRACE_MIN

    def rate(wdx):
        ins = [(k, r) for k, r in wdx.items() if r["in"] is not None]
        return round(100 * sum(on_time(k, r) for k, r in ins) / len(ins), 1) if ins else None

    staff_si = [s for s in si if s["kind"] == "staff" and s["valence"] is not None]
    vis_si = [s for s in si if s["kind"] == "visitor" and s["valence"] is not None]

    # ---- daily series
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(span)]
    daily = []
    for d in days:
        recs = {k: r for k, r in wd.items() if k[1] == d}
        daily.append({
            "day": d,
            "present": len(recs),
            "on_time": sum(on_time(k, r) for k, r in recs.items()),
            "late": sum(1 for k, r in recs.items() if r["in"] is not None and not on_time(k, r)),
            "avg_hours": _avg([r["worked"] / 60 for r in recs.values() if r["worked"]]),
            "staff_mood": _avg([s["valence"] for s in staff_si if s["day"] == d]),
            "visitor_mood": _avg([s["valence"] for s in vis_si if s["day"] == d]),
            "interactions": sum(1 for x in it if x["day"] == d and x["channel"] != "greeting"),
        })

    # ---- mood mix / by hour / by weekday
    mix_staff = Counter(s["mood"] for s in staff_si)
    mix_vis = Counter(s["mood"] for s in vis_si)
    by_hour = []
    for h in range(24):
        st = [s["valence"] for s in staff_si if s["hour"] == h]
        vi = [s["valence"] for s in vis_si if s["hour"] == h]
        if st or vi:
            by_hour.append({"hour": h, "n": len(st) + len(vi), "n_staff": len(st), "n_visitor": len(vi),
                            "staff": _avg(st) if len(st) >= MIN_HOUR_SAMPLES else None,
                            "visitor": _avg(vi) if len(vi) >= MIN_HOUR_SAMPLES else None})
    by_weekday = []
    for i, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        st = [s["valence"] for s in staff_si if date.fromisoformat(s["day"]).weekday() == i]
        recs = [(k, r) for k, r in wd.items() if date.fromisoformat(k[1]).weekday() == i and r["in"] is not None]
        by_weekday.append({"weekday": name, "mood": _avg(st),
                           "on_time_pct": round(100 * sum(on_time(k, r) for k, r in recs) / len(recs), 1) if recs else None})

    # ---- arrivals heatmap weekday x hour
    heat = [[0] * 24 for _ in range(7)]
    for e in ev:
        if e["action"] == "IN":
            heat[date.fromisoformat(e["day"]).weekday()][int(e["ts"][11:13])] += 1

    # ---- locations
    kiosks = defaultdict(lambda: {"sightings": 0, "staff": [], "visitor": [], "interactions": 0, "visitors": 0})
    for s in si:
        k = kiosks[s["kiosk"] or "—"]
        k["sightings"] += 1
        if s["kind"] == "visitor":
            k["visitors"] += 1
        if s["valence"] is not None:
            k[s["kind"]].append(s["valence"])
    for x in it:
        if x["channel"] != "greeting":
            kiosks[x["kiosk"] or "—"]["interactions"] += 1
    locations = sorted(({"kiosk": n, "sightings": v["sightings"], "visitors": v["visitors"], "interactions": v["interactions"],
                         "staff_mood": _avg(v["staff"]), "visitor_mood": _avg(v["visitor"])} for n, v in kiosks.items()),
                       key=lambda r: -r["sightings"])

    # ---- people
    people_rows, alerts = [], []
    names = sorted({k[0] for k in wd} | {s["person"] for s in staff_si if s["person"]})
    for name in names:
        prof = profiles.get(name) or {}
        recs = {k[1]: r for k, r in wd.items() if k[0] == name}
        ins = [r for r in recs.values() if r["in"] is not None]
        ontime = [on_time((name, d), r) for d, r in recs.items() if r["in"] is not None]
        moods = [s for s in staff_si if s["person"] == name]
        vals = [s["valence"] for s in moods]
        # trend: last 7 days of the range vs the 7 before
        cut = (d1 - timedelta(days=6)).isoformat()
        recent = _avg([s["valence"] for s in moods if s["day"] >= cut])
        earlier = _avg([s["valence"] for s in moods if s["day"] < cut])
        trend = round(recent - earlier, 3) if recent is not None and earlier is not None else None
        # consecutive on-time streak ending at the latest day
        streak = 0
        for d in sorted(recs, reverse=True):
            if recs[d]["in"] is not None and on_time((name, d), recs[d]):
                streak += 1
            else:
                break
        convo = [x for x in it if x["person"] == name and x["channel"] != "greeting"]
        row = {
            "name": name, "department": prof.get("department"), "role": prof.get("role"),
            "days": len(recs), "on_time_pct": round(100 * sum(ontime) / len(ontime), 1) if ontime else None,
            "avg_in": _hhmm(_avg([r["in"] for r in ins])), "avg_out": _hhmm(_avg([r["out"] for r in recs.values() if r["out"] is not None])),
            "avg_hours": _avg([r["worked"] / 60 for r in recs.values() if r["worked"]]),
            "avg_break": _avg([r["break"] for r in recs.values() if r["breaks"]]),
            "mood": _avg(vals), "mood_trend": trend,
            "dominant_mood": Counter(s["mood"] for s in moods).most_common(1)[0][0] if moods else None,
            "streak": streak, "interactions": len(convo),
            "sentiment": _avg([x["sentiment"] for x in convo]),
            "last_seen": max([s["ts"] for s in moods] + [e["ts"] for e in ev if e["employee"] == name], default=None),
            "consent": bool(prof.get("mood_consent", 1)),
            "flags": [],
        }
        # wellbeing: low mood on >=3 of the person's last 5 observed days
        by_day = defaultdict(list)
        for s in moods:
            by_day[s["day"]].append(s["valence"])
        last5 = [sum(v) / len(v) for d, v in sorted(by_day.items())[-5:]]
        if len(last5) >= 3 and sum(v < LOW_MOOD for v in last5) >= 3:
            row["flags"].append("wellbeing")
            alerts.append({"person": name, "type": "wellbeing", "severity": "high",
                           "text": f"{name} has looked low on {sum(v < LOW_MOOD for v in last5)} of their last {len(last5)} days. A friendly check-in may help."})
        if trend is not None and trend <= -0.35:
            row["flags"].append("mood_drop")
            alerts.append({"person": name, "type": "mood_drop", "severity": "medium",
                           "text": f"{name}'s mood this week is noticeably lower than the week before."})
        late_recent = [d for d in sorted(recs)[-5:] if recs[d]["in"] is not None and not on_time((name, d), recs[d])]
        if len(late_recent) >= 3:
            row["flags"].append("late")
            alerts.append({"person": name, "type": "late", "severity": "medium",
                           "text": f"{name} arrived late on {len(late_recent)} of their last {min(5, len(recs))} shifts."})
        long_breaks = [r for r in recs.values() if r["break"] > 75]
        if len(long_breaks) >= 3:
            row["flags"].append("long_breaks")
            alerts.append({"person": name, "type": "long_breaks", "severity": "low",
                           "text": f"{name} took breaks longer than 75 min on {len(long_breaks)} days."})
        if streak >= 15 and not row["flags"]:
            row["flags"].append("star")
            alerts.append({"person": name, "type": "kudos", "severity": "positive",
                           "text": f"{name} is on a {streak}-shift on-time streak — worth a shout-out."})
        people_rows.append(row)

    # ---- departments
    depts = defaultdict(list)
    for r in people_rows:
        depts[r["department"] or "Unassigned"].append(r)
    departments = [{"department": d, "people": len(rs), "on_time_pct": _avg([r["on_time_pct"] for r in rs]),
                    "mood": _avg([r["mood"] for r in rs]), "avg_hours": _avg([r["avg_hours"] for r in rs])}
                   for d, rs in sorted(depts.items())]

    # ---- conversations
    talk = [x for x in it if x["channel"] != "greeting"]
    intents = Counter(x["intent"] for x in talk if x["intent"])
    languages = Counter(x["lang"] for x in talk if x["lang"])
    engines = Counter((x["engine"] or "").split("+")[-1] for x in talk)

    all_in = [r["in"] for r in wd.values() if r["in"] is not None]

    # previous-period comparisons only when that period is comparably covered (>= half the days with data)
    def covered(prev_days, cur_days):
        return len(prev_days) >= max(1, len(cur_days)) / 2
    cur_days = {s["day"] for s in si}
    prev_staff = [r for r in prev_si if r["kind"] == "staff"]
    prev_vis = [r for r in prev_si if r["kind"] == "visitor"]
    prev_on_time = rate(prev_wd) if covered({k[1] for k in prev_wd}, {k[1] for k in wd}) else None
    kpis = {
        "staff_seen": len({k[0] for k in wd}),
        "shifts": len(wd),
        "on_time_pct": rate(wd), "on_time_prev": prev_on_time,
        "avg_arrival": _hhmm(_avg(all_in)),
        "avg_hours": _avg([r["worked"] / 60 for r in wd.values() if r["worked"]]),
        "avg_break": _avg([r["break"] for r in wd.values() if r["breaks"]]),
        "staff_mood": _avg([s["valence"] for s in staff_si]),
        "staff_mood_prev": _avg([s["valence"] for s in prev_staff]) if covered({r["day"] for r in prev_staff}, cur_days) else None,
        "visitor_mood": _avg([s["valence"] for s in vis_si]),
        "visitor_mood_prev": _avg([s["valence"] for s in prev_vis]) if covered({r["day"] for r in prev_vis}, cur_days) else None,
        "visitors": sum(1 for s in si if s["kind"] == "visitor"),
        "conversations": len(talk),
        "languages": len(languages),
        "sentiment": _avg([x["sentiment"] for x in talk]),
        "alerts": sum(1 for a in alerts if a["severity"] in ("high", "medium")),
    }
    # ---- assistance requests: volume, response (open -> acknowledged) and resolution times
    def mins(a, b):
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60 if a and b else None
    resp = [mins(r["ts"], r["ack_ts"]) for r in rq if r["ack_ts"]]
    reso = [mins(r["ts"], r["done_ts"]) for r in rq if r["status"] == "done" and r["done_ts"]]
    sla = int(os.environ.get("ASSIST_SLA_MINUTES", "5"))
    assist = {
        "total": len(rq), "open": sum(r["status"] in ("open", "acknowledged") for r in rq),
        "done": sum(r["status"] == "done" for r in rq), "cancelled": sum(r["status"] == "cancelled" for r in rq),
        "median_response_min": round(statistics.median(resp), 1) if resp else None,
        "median_resolution_min": round(statistics.median(reso), 1) if reso else None,
        "within_sla_pct": round(100 * sum(x <= sla for x in resp) / len(resp), 1) if resp else None, "sla_min": sla,
        "by_kind": dict(Counter(r["kind"] for r in rq).most_common()),
        "by_kiosk": dict(Counter(r["kiosk"] for r in rq).most_common()),
        "by_hour": [sum(1 for r in rq if int(r["ts"][11:13]) == h) for h in range(24)],
        "by_source": dict(Counter(r["source"] for r in rq)),
        "responders": sorted(
            ({"name": n, "handled": len(v), "median_response_min": round(statistics.median(v), 1)}
             for n, v in _group([(r["assigned_to"], mins(r["ts"], r["ack_ts"])) for r in rq if r["assigned_to"] and r["ack_ts"]]).items()),
            key=lambda x: -x["handled"])[:10],
    }

    with db() as conn:
        safety_block = safety.summary(conn, date_from, date_to)
    for h in safety_block["high_fatigue"][:5]:
        alerts.append({"person": h["person"], "type": "fatigue", "severity": "high",
                       "text": f"{h['person']} clocked in with HIGH fatigue risk ({h['score']}): " + "; ".join(f["text"] for f in h["factors"][:2]) + "."})

    # keep kudos meaningful: only the three longest streaks
    concern = {a["person"] for a in alerts if a["severity"] in ("high", "medium")}
    for r in people_rows:
        if any(h["person"] == r["name"] for h in safety_block["high_fatigue"]):
            r["flags"].append("fatigue")
        if r["name"] in concern and "star" in r["flags"]:
            r["flags"].remove("star")
    kudos = sorted((a for a in alerts if a["type"] == "kudos" and a["person"] not in concern),
                   key=lambda a: -next(r["streak"] for r in people_rows if r["name"] == a["person"]))
    alerts = [a for a in alerts if a["type"] != "kudos"] + kudos[:3]
    sev = {"high": 0, "medium": 1, "low": 2, "positive": 3}
    return {
        "from": date_from, "to": date_to, "kpis": kpis, "daily": daily,
        "mood_mix": {"staff": dict(mix_staff), "visitor": dict(mix_vis)},
        "mood_by_hour": by_hour, "by_weekday": by_weekday, "arrivals_heat": heat,
        "locations": locations, "people": people_rows, "departments": departments, "assist": assist,
        "safety": safety_block,
        "alerts": sorted(alerts, key=lambda a: sev[a["severity"]]),
        "intents": dict(intents.most_common()), "languages": dict(languages.most_common()),
        "engines": dict(engines),
        "moods_meta": {k: {"valence": v[0], "word": v[1], "emoji": v[2]} for k, v in MOODS.items()},
    }


def person(name: str, date_from: str, date_to: str) -> dict:
    with db() as conn:
        prof = conn.execute("SELECT * FROM people WHERE name=?", (name,)).fetchone()
        ev = conn.execute("SELECT * FROM events WHERE employee=? AND day BETWEEN ? AND ? ORDER BY ts", (name, date_from, date_to)).fetchall()
        si = conn.execute("SELECT * FROM sightings WHERE person=? AND day BETWEEN ? AND ? ORDER BY ts", (name, date_from, date_to)).fetchall()
        it = conn.execute("SELECT * FROM interactions WHERE person=? AND day BETWEEN ? AND ? ORDER BY id DESC LIMIT 500",
                          (name, date_from, date_to)).fetchall()
        mem = conn.execute("SELECT * FROM memories WHERE person=? AND substr(ts,1,10) BETWEEN ? AND ? ORDER BY id DESC LIMIT 100",
                           (name, date_from, date_to)).fetchall()
        checks = [dict(r) for r in conn.execute("SELECT * FROM safety_checks WHERE person=? AND day BETWEEN ? AND ? ORDER BY ts DESC",
                                                (name, date_from, date_to))]
    prof = dict(prof) if prof else {"name": name}
    wd = workdays(ev)
    shift = _shift_min(prof)
    moods_by_day = defaultdict(list)
    for s in si:
        if s["valence"] is not None:
            moods_by_day[s["day"]].append(s)
    days = []
    for d in sorted(set(k[1] for k in wd) | set(moods_by_day)):
        r = wd.get((name, d), {})
        ms = moods_by_day.get(d, [])
        days.append({
            "day": d, "in": _hhmm(r.get("in")), "out": _hhmm(r.get("out")),
            "worked": _dur(r.get("worked")) if r.get("worked") else None,
            "worked_h": round(r["worked"] / 60, 2) if r.get("worked") else None,
            "break_min": round(r.get("break") or 0), "late_min": (r["in"] - shift) if r.get("in") is not None and r["in"] > shift + GRACE_MIN else 0,
            "mood": _avg([s["valence"] for s in ms]),
            "dominant": Counter(s["mood"] for s in ms).most_common(1)[0][0] if ms else None,
            "attire": Counter(s["attire"] for s in ms if s["attire"]).most_common(1)[0][0] if any(s["attire"] for s in ms) else None,
        })
    return {
        "profile": prof, "days": days,
        "mood_mix": dict(Counter(s["mood"] for s in si if s["mood"])),
        "attire_mix": dict(Counter(s["attire"] for s in si if s["attire"])),
        "interactions": [dict(r) for r in it], "memories": [dict(r) for r in mem], "safety_checks": checks,
        "moods_meta": {k: {"valence": v[0], "word": v[1], "emoji": v[2]} for k, v in MOODS.items()},
    }


def person_stats(conn, name: str, today: date, profile: dict) -> dict:
    """Small stats used by the kiosk greeting: early minutes, on-time streak, yesterday, this month."""
    since = (today - timedelta(days=45)).isoformat()
    ev = conn.execute("SELECT * FROM events WHERE employee=? AND day>=? ORDER BY ts", (name, since)).fetchall()
    wd = workdays(ev)
    shift = _shift_min(profile)
    stats = {}
    t = wd.get((name, today.isoformat()))
    if t and t["in"] is not None:
        stats["early_minutes"] = shift - t["in"]
    streak = 0
    for d in sorted((k[1] for k in wd), reverse=True):
        r = wd[(name, d)]
        if r["in"] is not None and r["in"] <= shift + GRACE_MIN:
            streak += 1
        else:
            break
    stats["on_time_streak"] = streak
    y = wd.get((name, (today - timedelta(days=1)).isoformat()))
    if y and y["worked"] and y["worked"] > 9.5 * 60:
        stats["stayed_late_yesterday"] = True
    if t and t["worked"]:
        stats["worked_today"] = _dur(t["worked"])
    stats["days_this_month"] = sum(1 for k in wd if k[1][:7] == today.isoformat()[:7])
    return stats


def attendance_text(conn, name: str, today: date) -> str:
    ev = conn.execute("SELECT * FROM events WHERE employee=? AND day=? ORDER BY ts", (name, today.isoformat())).fetchall()
    if not ev:
        return "You haven't clocked in yet today."
    r = workdays(ev)[(name, today.isoformat())]
    parts = []
    if r["in"] is not None:
        parts.append(f"You clocked in at {_hhmm(r['in'])}")
        end = r["out"] if r["out"] is not None else (datetime.now().hour * 60 + datetime.now().minute)
        parts.append(f"{_dur(end - r['in'] - r['break'])} on shift so far" if r["out"] is None else f"worked {_dur(r['worked'])}")
    if r["breaks"]:
        parts.append(f"{r['breaks']} break{'s' if r['breaks'] > 1 else ''} ({_dur(r['break'])})")
    return ", ".join(parts) + f". Last action: {ev[-1]['action']} at {ev[-1]['ts'][11:16]}."
