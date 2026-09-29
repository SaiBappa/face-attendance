"""
Demo data: 60 days of a realistic airport team, so Insights and the kiosks can be shown to
prospective airports before any real staff are enrolled. Every row is tagged demo=1 and
clear() removes exactly those rows (plus demo kiosks/people) — real data is never touched.
"""
import json
import random
from datetime import date, datetime, timedelta

import alerts
import roster
import safety
from brain import MOODS
from store import db

STAFF = [
    ("Aishath Shiuna", "Immigration", "Officer", "07:00", "dv"),
    ("Mohamed Rasheed", "Immigration", "Supervisor", "07:00", "dv"),
    ("Fathimath Nuha", "Customer Service", "Agent", "08:00", "en"),
    ("Ibrahim Shafeeq", "Security", "Screener", "06:00", "dv"),
    ("Hawwa Zeena", "Security", "Screener", "06:00", "dv"),
    ("Ahmed Naseem", "Ground Handling", "Ramp Lead", "05:30", "dv"),
    ("Ali Riyaz", "Ground Handling", "Loader", "05:30", "en"),
    ("Mariyam Leena", "Customer Service", "Agent", "08:00", "en"),
    ("Hussain Zahir", "Facilities", "Technician", "08:00", "en"),
    ("Aminath Saba", "Duty Free", "Sales Associate", "09:00", "en"),
    ("Priya Nair", "Duty Free", "Store Manager", "09:00", "hi"),
    ("Chen Wei", "Customer Service", "Mandarin Desk", "08:00", "zh"),
    ("Olga Ivanova", "Customer Service", "Russian Desk", "08:00", "ru"),
    ("Moosa Didi", "Facilities", "Cleaning Lead", "06:00", "dv"),
    ("Shiyam Adam", "Security", "Supervisor", "06:00", "dv"),
    ("Nashwa Ali", "F&B", "Barista", "07:00", "en"),
    ("Rizwan Khan", "F&B", "Chef", "06:30", "en"),
    ("Luca Bianchi", "Ground Handling", "Trainer", "08:00", "it"),
]

KIOSKS = [
    ("Staff-Entrance", "Staff Entrance", "Staff Entrance · Level 0", "Clock in, check your day, say hi.", "lagoon",
     [{"icon": "🕌", "title": "Prayer room", "text": "Level 1, next to the staff canteen."},
      {"icon": "🩺", "title": "First aid", "text": "Medical room is behind Security Checkpoint B."},
      {"icon": "🍽️", "title": "Staff canteen", "text": "Level 1, open 05:00–23:00. Today: garudhiya & rice."}]),
    ("Arrivals-Hall", "Arrivals", "Welcome to the Maldives", "Arrivals Hall · International", "sunset",
     [{"icon": "🚤", "title": "Resort transfers", "text": "Seaplane lounge and speedboat jetty are straight ahead, exit 2."},
      {"icon": "🚕", "title": "Taxi & Malé ferry", "text": "Taxis outside exit 1. Airport–Malé ferry every 10 minutes from the jetty."},
      {"icon": "💱", "title": "Currency exchange", "text": "Bank counters are to your left after customs, open 24/7."},
      {"icon": "🕌", "title": "Prayer room", "text": "Next to the arrivals information desk."}]),
    ("Departures-Gate-3", "Departures", "Gate 3 · Departures", "Relax — we'll call your flight.", "aurora",
     [{"icon": "☕", "title": "Café", "text": "Crossroads Café is 30 m to the right of Gate 3."},
      {"icon": "🚻", "title": "Restrooms", "text": "Between Gate 2 and Gate 3."},
      {"icon": "🛍️", "title": "Duty free", "text": "Main duty-free store is back towards Gate 1."}]),
    ("Staff-Canteen", "Canteen", "Staff Canteen", "Refuel. Recharge. Reconnect.", "mono",
     [{"icon": "🥗", "title": "Menu", "text": "Today: tuna curry, roshi, fresh fruit. Healthy option: grilled reef fish."}]),
]

CONVERSATIONS = [
    ("staff", "en", "I'm so tired today, double shift yesterday", "share_bad", 0.2),
    ("staff", "en", "Good morning Aura!", "greeting", 0.8),
    ("staff", "en", "How many hours have I worked this week?", "attendance", 0.5),
    ("staff", "dv", "ކިހިނެއް؟", "how_are_you", 0.6),
    ("staff", "en", "Tell me a joke", "joke", 0.7),
    ("staff", "en", "I got promoted!!", "share_good", 0.97),
    ("staff", "en", "Do I look okay for the VIP arrival?", "compliment", 0.65),
    ("staff", "en", "Thank you, you always make my morning", "thanks", 0.95),
    ("staff", "hi", "मैं आज थोड़ा बीमार महसूस कर रही हूँ", "share_bad", 0.2),
    ("staff", "zh", "今天很忙，但是很开心", "share_good", 0.8),
    ("visitor", "en", "Where can I find the seaplane transfer?", "directions", 0.55),
    ("visitor", "ru", "Где можно обменять деньги?", "directions", 0.5),
    ("visitor", "zh", "请问出租车在哪里？", "directions", 0.5),
    ("visitor", "it", "C'è il wifi gratuito?", "wifi", 0.55),
    ("visitor", "de", "Mein Flug hat Verspätung, was soll ich tun?", "flight", 0.3),
    ("visitor", "en", "What's the wifi password?", "wifi", 0.5),
    ("visitor", "ar", "أين غرفة الصلاة؟", "directions", 0.55),
    ("visitor", "fr", "Bonjour ! Où sont les toilettes ?", "directions", 0.55),
    ("visitor", "en", "I lost my passport!", "lost", 0.1),
    ("visitor", "en", "The queue at immigration took an hour, not happy", "complaint", 0.15),
    ("visitor", "ja", "こんにちは！", "greeting", 0.8),
    ("visitor", "es", "¡Gracias por la ayuda!", "thanks", 0.9),
]

REPLIES = {
    "share_bad": "I'm sorry you're feeling that way. Take a slow breath and a sip of water — I'll check on you later.",
    "greeting": "Good morning! Lovely to see you — have a brilliant shift.",
    "attendance": "You've worked 38h 20m this week across 5 shifts, with average breaks of 42 minutes.",
    "how_are_you": "ރަނގަޅު، ޝުކުރިއްޔާ! ތިބާ ކިހިނެއް؟",
    "joke": "Why did the suitcase go to therapy? It had too much emotional baggage. 🧳",
    "share_good": "That's wonderful! Your good energy is contagious — keep it going.",
    "compliment": "You look ready to own the day. Confidence suits you.",
    "thanks": "Always a pleasure! Have a great day.",
    "directions": "Resort transfers: Seaplane lounge and speedboat jetty are straight ahead, exit 2.",
    "wifi": "Free Wi-Fi: network “Airport-Free-WiFi”, no password.",
    "flight": "For live flight times please check the flight information screens or ask the airline counter.",
    "lost": "I've flagged this for the team. Please stay here; a staff member will come to help.",
    "complaint": "Thank you for telling me — I've recorded it so the team can follow up. I'm sorry for the trouble.",
}

ATTIRE = ["navy", "white", "black", "light grey", "sky blue", "teal", "maroon", "charcoal"]


def _pick_mood(rng, base):
    """base valence bias -> FER+ label"""
    r = rng.random() + base
    if r > 1.05:
        return "happiness"
    if r > 0.55:
        return "neutral"
    if r > 0.45:
        return "surprise"
    if r > 0.25:
        return rng.choice(["sadness", "neutral"])
    return rng.choice(["sadness", "anger", "fear", "contempt"])


def seed(days: int = 60) -> dict:
    rng = random.Random(42)
    today = date.today()
    counts = {"events": 0, "sightings": 0, "interactions": 0}
    with db() as conn:
        clear(conn)
        for name, zone, headline, subtitle, theme, info in KIOSKS:
            conn.execute("INSERT OR IGNORE INTO kiosks (name, zone, headline, subtitle, theme, info, demo) VALUES (?,?,?,?,?,?,1)",
                         (name, zone, headline, subtitle, theme, json.dumps(info, ensure_ascii=False)))
        # ramp safety pack: department rules (kept when demo data is removed — they suit real teams too)
        for dept, ppe, fat in (("Ground Handling", ["hi_vis", "id_badge", "ear", "shoes", "gloves"], 1),
                               ("Security", ["id_badge"], 1), ("Facilities", ["shoes", "gloves"], 0)):
            if not conn.execute("SELECT 1 FROM safety_rules WHERE department=?", (dept,)).fetchone():
                safety.save_rule(conn, dept, {"ppe": ppe, "fatigue": fat})
        conn.execute("INSERT INTO announcements (kiosk, text, level, created) VALUES ('*', ?, 'info', ?)",
                     ("Staff townhall on Thursday 14:00 in the training room — snacks provided! (demo)", datetime.now().isoformat(timespec="seconds")))
        prng = random.Random(7)  # pass details on their own stream so the rest of the demo stays identical
        for i, (name, dept, role, shift, lang) in enumerate(STAFF):
            bday = (today + timedelta(days=0 if i == 2 else rng.randint(3, 300))).strftime("%m-%d")
            dob = f"{prng.randint(1975, 2003)}-{bday}"
            # one expired pass (4) and one about to expire (9) so the People screen shows both states
            expiry = today + timedelta(days=-12 if i == 4 else 9 if i == 9 else prng.randint(60, 700))
            zone = {"Security": "green", "Ground Handling": "red"}.get(dept) or prng.choice(["orange", "blue", "yellow", "white"])
            conn.execute(
                """INSERT OR IGNORE INTO people (name, role, department, shift_start, birthday, joined, language, mood_consent,
                                                 record_card, dob, pass_expiry, zone, demo, created)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (name, role, dept, shift, bday, f"{rng.randint(2015, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                 lang, 0 if i == 17 else 1, f"RC-{10200 + i * 7}", dob if bday != "02-29" else None, expiry.isoformat(), zone,
                 datetime.now().isoformat(timespec="seconds")))

        for d in range(days, -1, -1):
            day = today - timedelta(days=d)
            wd = day.weekday()  # Mon=0
            for i, (name, dept, role, shift, lang) in enumerate(STAFF):
                # personalities: 0 = always early star, 5 = chronically late, 7 = mood sliding in the last week
                # two rest days a week (staggered); Ali (6) skips rest lately. Everyone else is rostered.
                if wd in (i % 7, (i + 1) % 7) and not (i == 6 and d <= 10):
                    continue
                _roster_shift(conn, name, dept, role, shift, day)
                counts["shifts"] = counts.get("shifts", 0) + 1
                if rng.random() < 0.035 or (i == 5 and d in (3, 9)):   # the odd no-show (Ahmed a couple of times)
                    continue
                sh, sm = int(shift[:2]), int(shift[3:])
                offset = rng.gauss(-6, 5)
                if i == 0:
                    offset = rng.gauss(-14, 3)
                if i == 5:
                    offset = rng.gauss(9, 6)
                if wd == 6:  # Sunday rush after the weekend
                    offset += 3
                t_in = datetime(day.year, day.month, day.day, sh, sm) + timedelta(minutes=offset)
                if day == today and t_in > datetime.now():
                    continue
                base = 0.35 + (0.25 if i in (0, 9, 15) else 0) - (0.2 if wd == 6 else 0)
                if i == 7 and d <= 6:
                    base -= 0.65
                if i == 11:
                    base += 0.15
                attire = rng.choice(ATTIRE[:3]) if dept in ("Immigration", "Security") else rng.choice(ATTIRE)
                kiosk = "Staff-Entrance" if rng.random() < 0.85 else "Staff-Canteen"
                seq = [("IN", t_in)]
                brk = t_in + timedelta(hours=rng.uniform(3.2, 4.5))
                brk_len = rng.gauss(45, 10) if i != 13 else rng.gauss(85, 10)
                seq.append(("BREAK", brk))
                seq.append(("BACK", brk + timedelta(minutes=max(10, brk_len))))
                t_out = t_in + timedelta(hours=rng.gauss(9.1, 0.5) + (1.3 if i == 1 and rng.random() < 0.4 else 0)
                                         + (4.5 if i == 6 and d <= 8 and rng.random() < 0.6 else 0))   # Ali: double shifts lately
                seq.append(("OUT", t_out))
                for action, ts in seq:
                    if ts > datetime.now():
                        break
                    mood = _pick_mood(rng, base + (0.1 if action == "OUT" else 0) - (0.1 if 13 <= ts.hour <= 15 else 0))
                    consent = i != 17
                    conn.execute(
                        "INSERT INTO events (ts, day, employee, action, kiosk, similarity, mood, attire, demo) VALUES (?,?,?,?,?,?,?,?,1)",
                        (ts.isoformat(timespec="seconds"), ts.date().isoformat(), name, action,
                         "Staff-Canteen" if action in ("BREAK", "BACK") else kiosk, round(rng.uniform(0.45, 0.8), 3),
                         mood if consent else None, attire))
                    conn.execute(
                        "INSERT INTO sightings (ts, day, hour, kiosk, person, kind, mood, valence, confidence, attire, demo) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                        (ts.isoformat(timespec="seconds"), ts.date().isoformat(), ts.hour,
                         "Staff-Canteen" if action in ("BREAK", "BACK") else kiosk, name, "staff",
                         mood if consent else None, MOODS[mood][0] if consent else None,
                         round(rng.uniform(0.5, 0.95), 2), attire))
                    counts["events"] += 1
                    counts["sightings"] += 1
                    rules = safety.rules_for(conn, dept)
                    if action == "IN" and (rules["ppe"] or rules["fatigue"]):
                        items = {k: ("missing" if rng.random() < {"ear": 0.08, "gloves": 0.05}.get(k, 0.01) else "ok") for k in rules["ppe"]}
                        rested = rng.choice([2, 2, 3]) if (i == 6 and d <= 8) else rng.choice([3, 4, 4, 5])
                        safety.record(conn, name, {"department": dept}, kiosk, items,
                                      round(rng.uniform(0.18, 0.4), 3) if "hi_vis" in rules["ppe"] else None,
                                      rested if rules["fatigue"] else None, demo=1, now=ts + timedelta(seconds=30))
                        counts["safety_checks"] = counts.get("safety_checks", 0) + 1
                    if action == "IN":
                        conn.execute(
                            "INSERT INTO interactions (ts, day, kiosk, person, kind, channel, lang, reply, intent, mood, engine, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                            (ts.isoformat(timespec="seconds"), ts.date().isoformat(), kiosk, name, "staff", "greeting", "en",
                             f"Good morning, {name.split()[0]}! Navy suits you — very professional.", "greet:IN",
                             mood if consent else None, "jev+templates"))
                        counts["interactions"] += 1
            # (today's staff who haven't arrived yet are simply "upcoming" on the roster)
            # visitors at the public kiosks
            for kiosk, peak in (("Arrivals-Hall", (10, 14, 22)), ("Departures-Gate-3", (7, 12, 18))):
                for _ in range(rng.randint(35, 70)):
                    h = min(23, max(5, int(rng.gauss(rng.choice(peak), 2))))
                    ts = datetime(day.year, day.month, day.day, h, rng.randint(0, 59), rng.randint(0, 59))
                    if ts > datetime.now():
                        continue
                    vb = 0.5 if kiosk == "Arrivals-Hall" else 0.25
                    if h >= 22 or h <= 6:
                        vb -= 0.25
                    mood = _pick_mood(rng, vb)
                    conn.execute(
                        "INSERT INTO sightings (ts, day, hour, kiosk, kind, mood, valence, confidence, demo) VALUES (?,?,?,?,?,?,?,?,1)",
                        (ts.isoformat(timespec="seconds"), ts.date().isoformat(), h, kiosk, "visitor", mood, MOODS[mood][0], round(rng.uniform(0.5, 0.95), 2)))
                    counts["sightings"] += 1
            # assistance requests at the public kiosks
            helpers = [n for n, dept, *_ in STAFF if dept == "Customer Service"]
            for _ in range(rng.randint(3, 9)):
                h = min(23, max(5, int(rng.gauss(rng.choice((10, 14, 20)), 3))))
                ts = datetime(day.year, day.month, day.day, h, rng.randint(0, 59), rng.randint(0, 59))
                if ts > datetime.now():
                    continue
                kind = rng.choices(["wheelchair", "porter", "lost_item", "medical", "other", "lost_person", "security"],
                                   [30, 22, 18, 8, 15, 3, 4])[0]
                ack = ts + timedelta(minutes=max(0.5, rng.gauss(3.5 if h < 20 else 6, 2)))
                done = ack + timedelta(minutes=max(2, rng.gauss(14, 6)))
                status = "done" if done < datetime.now() else "acknowledged" if ack < datetime.now() else "open"
                if rng.random() < 0.04:
                    status = "cancelled"
                conn.execute(
                    """INSERT INTO requests (ts, day, kiosk, kind, details, lang, encounter, source, status, assigned_to, ack_ts, done_ts, demo)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (ts.isoformat(timespec="seconds"), day.isoformat(), rng.choice(["Arrivals-Hall", "Departures-Gate-3"]), kind,
                     rng.choice([None, None, "Blue suitcase left near belt 2", "Elderly passenger, 2 bags", "Needs help to transfer desk"]),
                     rng.choice(["en", "ru", "zh", "it", "de", "ar"]), f"demo-{rng.getrandbits(32):x}",
                     rng.choice(["button", "button", "conversation"]), status,
                     rng.choice(helpers) if status in ("acknowledged", "done") else None,
                     ack.isoformat(timespec="seconds") if status in ("acknowledged", "done") else None,
                     done.isoformat(timespec="seconds") if status in ("done", "cancelled") else None))
                counts["requests"] = counts.get("requests", 0) + 1
            # conversations
            for _ in range(rng.randint(8, 22)):
                kind, lang, text, intent, senti = rng.choice(CONVERSATIONS)
                h = rng.randint(6, 22)
                ts = datetime(day.year, day.month, day.day, h, rng.randint(0, 59))
                if ts > datetime.now():
                    continue
                person = None
                kiosk = rng.choice(["Arrivals-Hall", "Departures-Gate-3"])
                if kind == "staff":
                    person = rng.choice(STAFF)[0]
                    kiosk = rng.choice(["Staff-Entrance", "Staff-Canteen"])
                if person == "Mariyam Leena" and d <= 6:
                    text, intent, senti = "Honestly it's been a rough week", "share_bad", 0.15
                conn.execute(
                    "INSERT INTO interactions (ts, day, kiosk, person, kind, encounter, channel, lang, user_text, reply, intent, sentiment, engine, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (ts.isoformat(timespec="seconds"), ts.date().isoformat(), kiosk, person, kind, f"demo-{rng.getrandbits(32):x}",
                     rng.choice(["voice", "voice", "text"]), lang, text, REPLIES.get(intent, "Happy to help!"), intent,
                     round(min(1, max(0, rng.gauss(senti, 0.08))), 2), rng.choice(["jev+templates", "jev+claude"])))
                counts["interactions"] += 1
        # the next two weeks of roster, same pattern
        for d in range(1, 15):
            day = today + timedelta(days=d)
            for i, (name, dept, role, shift, lang) in enumerate(STAFF):
                if day.weekday() not in (i % 7, (i + 1) % 7):
                    _roster_shift(conn, name, dept, role, shift, day)
                    counts["shifts"] = counts.get("shifts", 0) + 1
        alerts.scan(conn)  # today's live alerts (tagged demo, never sent to webhooks)
        conn.execute("INSERT INTO memories (person, ts, fact, kind, demo) VALUES (?,?,?,?,1)",
                     ("Mariyam Leena", (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"), "said they were tired", "tired"))
        conn.execute("INSERT INTO memories (person, ts, fact, kind, demo) VALUES (?,?,?,?,1)",
                     ("Fathimath Nuha", (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds"), "is training for the Malé half marathon", "note"))
    return {"ok": True, **counts, "people": len(STAFF), "kiosks": len(KIOSKS)}


POSTS = {"Immigration": ("Arrivals-Hall", "Immigration desk"), "Security": ("Departures-Gate-3", "Screening lane"),
         "Ground Handling": ("Apron", "Ramp"), "Customer Service": ("Arrivals-Hall", "Info desk"),
         "Duty Free": ("Departures-Gate-3", "Store"), "F&B": ("Staff-Canteen", "Café"), "Facilities": ("Terminal", "Rounds")}


def _roster_shift(conn, name, dept, role, shift, day):
    st = datetime.strptime(shift, "%H:%M")
    loc, pos = POSTS.get(dept, ("Terminal", role))
    roster.save(conn, [{"person": name, "day": day.isoformat(), "start": shift, "end": (st + timedelta(hours=9)).strftime("%H:%M"),
                        "position": pos, "location": loc, "department": dept}], "demo", replace=False, demo=1)


def clear(conn=None) -> dict:
    own = conn is None
    conn = conn or db()
    try:
        n = 0
        for t in ("events", "sightings", "interactions", "memories", "people", "kiosks", "requests", "safety_checks", "shifts", "alerts"):
            n += conn.execute(f"DELETE FROM {t} WHERE demo=1").rowcount
        conn.execute("DELETE FROM announcements WHERE text LIKE '%(demo)'")
        if own:
            conn.commit()
        return {"ok": True, "removed": n}
    finally:
        if own:
            conn.close()
