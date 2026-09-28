"""
Demo data: 60 days of a realistic airport team, so Insights and the kiosks can be shown to
prospective airports before any real staff are enrolled. Every row is tagged demo=1 and
clear() removes exactly those rows (plus demo kiosks/people) — real data is never touched.
"""
import json
import random
from datetime import date, datetime, timedelta

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
        conn.execute("INSERT INTO announcements (kiosk, text, level, created) VALUES ('*', ?, 'info', ?)",
                     ("Staff townhall on Thursday 14:00 in the training room — snacks provided! (demo)", datetime.now().isoformat(timespec="seconds")))
        for i, (name, dept, role, shift, lang) in enumerate(STAFF):
            bday = (today + timedelta(days=0 if i == 2 else rng.randint(3, 300))).strftime("%m-%d")
            conn.execute(
                """INSERT OR IGNORE INTO people (name, role, department, shift_start, birthday, joined, language, mood_consent, demo, created)
                   VALUES (?,?,?,?,?,?,?,?,1,?)""",
                (name, role, dept, shift, bday, f"{rng.randint(2015, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                 lang, 0 if i == 17 else 1, datetime.now().isoformat(timespec="seconds")))

        for d in range(days, -1, -1):
            day = today - timedelta(days=d)
            wd = day.weekday()  # Mon=0
            for i, (name, dept, role, shift, lang) in enumerate(STAFF):
                # personalities: 0 = always early star, 5 = chronically late, 7 = mood sliding in the last week
                if rng.random() < (0.12 if wd in (4, 5) else 0.06):
                    continue  # day off
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
                t_out = t_in + timedelta(hours=rng.gauss(9.1, 0.5) + (1.3 if i == 1 and rng.random() < 0.4 else 0))
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
                    if action == "IN":
                        conn.execute(
                            "INSERT INTO interactions (ts, day, kiosk, person, kind, channel, lang, reply, intent, mood, engine, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                            (ts.isoformat(timespec="seconds"), ts.date().isoformat(), kiosk, name, "staff", "greeting", "en",
                             f"Good morning, {name.split()[0]}! Navy suits you — very professional.", "greet:IN",
                             mood if consent else None, "jev+templates"))
                        counts["interactions"] += 1
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
        conn.execute("INSERT INTO memories (person, ts, fact, kind, demo) VALUES (?,?,?,?,1)",
                     ("Mariyam Leena", (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"), "said they were tired", "tired"))
        conn.execute("INSERT INTO memories (person, ts, fact, kind, demo) VALUES (?,?,?,?,1)",
                     ("Fathimath Nuha", (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds"), "is training for the Malé half marathon", "note"))
    return {"ok": True, **counts, "people": len(STAFF), "kiosks": len(KIOSKS)}


def clear(conn=None) -> dict:
    own = conn is None
    conn = conn or db()
    try:
        n = 0
        for t in ("events", "sightings", "interactions", "memories", "people", "kiosks"):
            n += conn.execute(f"DELETE FROM {t} WHERE demo=1").rowcount
        conn.execute("DELETE FROM announcements WHERE text LIKE '%(demo)'")
        if own:
            conn.commit()
        return {"ok": True, "removed": n}
    finally:
        if own:
            conn.close()
