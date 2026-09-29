"""
Aura's brain: understanding what people say, replying in their language, and composing the
personal greeting a staff member sees when the kiosk recognises them.

  * Jev (TypeSafe "System One" decision model) answers the structured questions:
    intent, sentiment, wellbeing flags, which info card answers a question, which compliment
    fits best. It is fast and cheap but never writes text.
  * Text comes from Claude when ANTHROPIC_API_KEY is set (free-form, any language, with the
    person's history as context), otherwise from the templates in phrases.py.
  * Everything degrades gracefully: no Jev -> keyword rules; no Claude -> templates.

No images ever leave the building: only the words spoken/typed and the derived signals
(mood label, clothing colour, attendance stats) are sent to Jev / Claude.
"""
import json
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx

import flights as flightdata
import phrases

JEV_URL = os.environ.get("JEV_URL", "https://api.typesafe.ai/v1").rstrip("/")
JEV_KEY = os.environ.get("TYPESAFE_API_KEY", "")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-opus-5")
AIRPORT = os.environ.get("AIRPORT_NAME", "Velana International Airport")
WIFI = os.environ.get("WIFI_INFO", "network “Airport-Free-WiFi”, no password")
UNIFORM_COLORS = {c.strip() for c in os.environ.get("UNIFORM_COLORS", "").lower().split(",") if c.strip()}
WEEKEND = {d.strip().lower()[:3] for d in os.environ.get("WEEKEND_DAYS", "fri,sat").split(",")}

try:  # optional: free-form replies
    import anthropic

    _claude = anthropic.AsyncAnthropic(timeout=15.0, max_retries=1) if os.environ.get("ANTHROPIC_API_KEY") else None
except ImportError:
    _claude = None

# FER+ label -> (valence -1..1, friendly word, emoji). Shared with insights.
MOODS = {
    "happiness": (1.0, "happy", "😊"),
    "surprise": (0.3, "surprised", "😮"),
    "neutral": (0.0, "calm", "😌"),
    "contempt": (-0.4, "unimpressed", "😏"),
    "sadness": (-0.7, "low", "😔"),
    "fear": (-0.6, "anxious", "😟"),
    "disgust": (-0.6, "uneasy", "😣"),
    "anger": (-0.8, "tense", "😠"),
}


def engines() -> dict:
    return {"jev": bool(JEV_KEY), "claude": _claude is not None, "llm_model": LLM_MODEL if _claude else None}


# ----------------------------------------------------------------------------- Jev
async def jev(state: str, questions: dict, timeout: float = 4.0) -> Optional[dict]:
    """One Jev /systemone call. Returns the answers dict, or None if Jev is unavailable."""
    if not JEV_KEY or not questions:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{JEV_URL}/systemone",
                headers={"Authorization": f"Bearer {JEV_KEY}"},
                json={"model": JEV_MODEL, "state": state[:12000], "questions": questions},
            )
        if r.status_code >= 400:
            print("jev error", r.status_code, r.text[:300])
            return None
        return r.json().get("answers")
    except httpx.HTTPError as e:
        print("jev unreachable", e.__class__.__name__)
        return None


async def jev_status() -> bool:
    if not JEV_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{JEV_URL}/models", headers={"Authorization": f"Bearer {JEV_KEY}"})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


# ----------------------------------------------------------------------------- language
SCRIPTS = [
    ("dv", r"[ހ-޿]"), ("ur", r"[ٹڈڑںھہے]"),
    ("ar", r"[؀-ۿ]"), ("hi", r"[ऀ-ॿ]"), ("bn", r"[ঀ-৿]"),
    ("ta", r"[஀-௿]"), ("si", r"[඀-෿]"), ("th", r"[฀-๿]"),
    ("ko", r"[가-힯]"), ("ja", r"[぀-ヿ]"), ("zh", r"[一-鿿]"),
    ("ru", r"[Ѐ-ӿ]"),
]

# Latin-script languages Jev tells apart (keys are ISO 639-1)
LATIN_LANGS = {
    "en": "English", "fr": "French", "de": "German", "it": "Italian", "es": "Spanish",
    "pt": "Portuguese", "nl": "Dutch", "tr": "Turkish", "id": "Indonesian", "ms": "Malay",
    "dv": "Dhivehi written in Latin letters (e.g. 'kihineh', 'rangalhu', 'shukuriyya')",
}


# Dhivehi typed/spoken in Latin letters is common; Jev alone tends to call it English
ROMAN_DV = r"\b(kihineh|kihiney|rangalhu|shukuriyya|shukuriyyaa|haalu|kobaa|kiyaa|varah|baaru|dhen|miadhu)\b"
INFO_INTENTS = {"directions", "wifi", "flight", "other"}


def script_language(text: str) -> Optional[str]:
    for lang, pattern in SCRIPTS:
        if re.search(pattern, text):
            return lang
    if re.search(ROMAN_DV, text.lower()):
        return "dv"
    return None


# ----------------------------------------------------------------------------- understanding
KEYWORDS = [  # used only when Jev is not reachable
    ("thanks", r"\b(thanks|thank you|shukuriyya|merci|danke|grazie|gracias|спасибо|谢谢|شكرا)"),
    ("goodbye", r"\b(bye|goodbye|see you|ciao|adios|до свидания|再见)"),
    ("wifi", r"(wi-?fi|internet|password|charg)"),
    ("directions", r"\b(where|toilet|restroom|prayer|gate|exit|taxi|cafe|lounge)"),
    ("flight", r"\b(flight|delay|boarding|check-?in|baggage|luggage)"),
    ("lost", r"\b(lost|help me|emergency|missing)"),
    ("joke", r"\b(joke|funny)"),
    ("share_bad", r"\b(tired|sick|stress|sad|bad day|headache|exhausted)"),
    ("share_good", r"\b(happy|great day|excited|good news|promoted)"),
    ("attendance", r"\b(shift|hours|clock|break|overtime)"),
    ("how_are_you", r"\b(how are you|kihineh)"),
    ("greeting", r"\b(hi|hello|hey|salaam|assalaam|bonjour|hola|привет|你好)"),
]


FLIGHT_WORDS_RX = r"(flight|fly|plane|gate|board|delay|depart|arriv|land|check.?in|рейс|вылет|航班|登机|رحلة|flug|volo|vol\b|vuelo|उड़ान|फ़्लाइट|便)"


async def understand(text: str, who: str, location: str, lang_hint: Optional[str], cards: list,
                     flight_list: Optional[list] = None) -> dict:
    """Intent, sentiment, wellbeing flags, language, the matching info card, the flight asked
    about, and whether the person needs a staff member (assistance request)."""
    script = script_language(text)
    q = {
        "intent": {"type": "choice", "instructions": "What does the speaker want from the airport kiosk assistant?",
                   "criteria": phrases.INTENTS},
        "sentiment": {"type": "score", "instructions": "How positive is the speaker feeling?",
                      "criteria": ["very negative", "negative", "neutral", "positive", "very positive"]},
        "tired": {"type": "noul", "instructions": "Does the speaker say or imply they are tired or exhausted?"},
        "unwell": {"type": "noul", "instructions": "Does the speaker say they are sick, in pain or unwell?"},
        "stressed": {"type": "noul", "instructions": "Is the speaker stressed, anxious, upset or overwhelmed?"},
        "celebrating": {"type": "noul", "instructions": "Is the speaker celebrating or sharing happy news?"},
    }
    if not lang_hint and not script:
        q["lang"] = {"type": "choice", "instructions": "Main language of the speaker's words",
                     "criteria": LATIN_LANGS}
    if cards:
        crit = {f"c{i}": f"{c.get('title', '')}: {c.get('text', '')}"[:300] for i, c in enumerate(cards)}
        crit["none"] = "none of these answers the question"
        q["card"] = {"type": "choice", "instructions": "Which information card best answers the speaker?",
                     "criteria": crit}
    q["assist"] = {"type": "choice", "instructions": "Does the speaker need a staff member to physically come and help? If so, with what?",
                   "criteria": {**{k: v[1] for k, v in phrases.ASSIST_KINDS.items() if k != "other"},
                                "other": "needs a staff member for something else", "none": "no staff member needed"}}
    # which flight? a flight number is matched directly; otherwise let Jev pick from the board
    flight = flightdata.find_by_number(text, flight_list or [])
    cands = []
    if not flight and flight_list and re.search(FLIGHT_WORDS_RX, text.lower()):
        cands = flightdata.upcoming_for_choice(flight_list)
        if cands:
            crit = {f"f{i}": f"{f['number']} {f.get('airline') or ''} {'to' if f['dir'] == 'dep' else 'from'} {f.get('city')} ({f.get('city_iata')}) at {(f.get('scheduled') or '')[11:16]}"
                    for i, f in enumerate(cands)}
            crit["none"] = "no specific flight can be identified from what they said"
            q["flight"] = {"type": "choice", "criteria": crit, "instructions": (
                "Which flight is the speaker asking about? If they name a destination ('to X') pick the departing flight to X; "
                "if they name an origin ('from X') pick the arriving flight from X. Choose none only if no city, airline or number is given.")}
    state = f"A {who} is talking to the wall-mounted assistant at {location}, {AIRPORT}.\nThey said: \"{text}\""
    a = await jev(state, q)

    out = {"lang": lang_hint or script, "flags": {}, "card": None, "engine": "jev" if a else "rules"}
    if a:
        out["intent"] = a["intent"]["choice"]
        out["intent_confidence"] = a["intent"].get("confidence")
        out["sentiment"] = round(float(a["sentiment"]["score"]) / 4, 3)
        out["flags"] = {k: round(float(a[k]["noul"]), 2) for k in ("tired", "unwell", "stressed", "celebrating")}
        if "lang" in a:
            out["lang"] = a["lang"]["choice"]
        if "flight" in a:
            # accept the leading flight if it clearly beats the other flights, even when "none" edges ahead
            probs = sorted(((k, v) for k, v in a["flight"]["probabilities"].items() if k != "none"), key=lambda kv: -kv[1])
            if probs and probs[0][1] >= 0.3 and (len(probs) == 1 or probs[0][1] >= 2 * probs[1][1]):
                flight = cands[int(probs[0][0][1:])]
        ch = a["assist"]["choice"]
        if ch != "none" and (a["assist"]["probabilities"].get(ch) or 0) >= 0.5:
            out["assist"] = ch
        # info cards only answer questions, never social chat ("I'm tired" is not a canteen query)
        if ("card" in a and a["card"]["choice"] != "none" and out["intent"] in INFO_INTENTS
                and (a["card"].get("confidence") or 0) > 0.3):
            out["card"] = cards[int(a["card"]["choice"][1:])]
    else:
        low = text.lower()
        out["intent"] = next((i for i, rx in KEYWORDS if re.search(rx, low)), "other")
        out["sentiment"] = {"share_bad": 0.2, "complaint": 0.25, "share_good": 0.85, "thanks": 0.8}.get(out["intent"], 0.5)
    if flight:
        out["flight"] = flight
        if out["intent"] in ("other", "directions", "greeting"):
            out["intent"] = "flight"
    if out["intent"] == "lost" and not out.get("assist"):
        out["assist"] = "lost_item"
    out["lang"] = out["lang"] or "en"
    return out


# ----------------------------------------------------------------------------- replies
def _history_text(history: list) -> str:
    lines = []
    for h in history:
        if h.get("user_text"):
            lines.append(f"[{h['ts'][:16]}] them: {h['user_text']}")
        if h.get("reply"):
            lines.append(f"[{h['ts'][:16]}] Aura: {h['reply']}")
    return "\n".join(lines[-24:])


async def _claude_reply(text: str, lang: str, u: dict, ctx: dict) -> Optional[dict]:
    if _claude is None:
        return None
    person = ctx.get("person")
    profile = ctx.get("profile") or {}
    system = f"""You are Aura, the warm, witty digital host on a wall-mounted screen at {ctx['location']} in {AIRPORT}.
You talk with airport staff (who you know by name and remember) and with travellers (anonymous).
Style: friendly, concise (1–3 short sentences, it is shown on a screen and read aloud), no markdown.
Always reply in the language with ISO code "{lang}" ({phrases.LANGUAGE_NAMES.get(lang, lang)}).
Be kind and uplifting. Compliment effort, style choices (e.g. outfit colour), punctuality and attitude.
Never comment on body shape, weight, skin, age, attractiveness or other physical traits; never guess
health conditions. If someone seems unwell or distressed, be supportive and suggest a supervisor or
first-aid point. Flight facts: use only the live flight data given below; never guess gates or times.
Only state facts about the airport that appear in the kiosk info below.

Kiosk info:
{json.dumps(ctx.get('cards') or [], ensure_ascii=False)}
Wi-Fi: {WIFI}
Local time: {ctx['now']}"""
    about = "a traveller (anonymous)"
    if person:
        about = f"staff member {person}; profile {json.dumps({k: v for k, v in profile.items() if v and k not in ('notes', 'demo')}, ensure_ascii=False)}"
        if ctx.get("attendance_text"):
            about += f"\nToday's attendance: {ctx['attendance_text']}"
        if ctx.get("memories"):
            about += "\nThings you remember about them: " + "; ".join(m["fact"] for m in ctx["memories"])
        if ctx.get("history"):
            about += "\nRecent conversations:\n" + _history_text(ctx["history"])
    if u.get("flight"):
        about += f"\nLive data for the flight they mean: {json.dumps(u['flight'], ensure_ascii=False)}"
    elif u["intent"] == "flight":
        about += "\nNo flight identified yet: ask for the flight number (it is on the boarding pass)."
    if u.get("assist"):
        about += f"\nThey may need staff help ({u['assist']}); the screen will show a 'call a team member' button — mention it."
    if ctx.get("mood"):
        about += f"\nTheir facial expression right now looks {MOODS.get(ctx['mood'], (0, ctx['mood']))[1]}."
    signals = f"Detected intent: {u['intent']}; sentiment {u.get('sentiment')} (0=very negative, 1=very positive); flags {u.get('flags')}"
    try:
        resp = await _claude.messages.create(
            model=LLM_MODEL,
            max_tokens=1024,
            output_config={
                "effort": "low",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "reply": {"type": "string"},
                            "remember": {"type": "string", "description": "one short durable fact worth remembering about this staff member, or empty"},
                        },
                        "required": ["reply", "remember"],
                        "additionalProperties": False,
                    },
                },
            },
            system=system,
            messages=[{"role": "user", "content": f"Speaker: {about}\n{signals}\n\nThey said: {text}"}],
        )
        if resp.stop_reason == "refusal":
            return None
        body = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(body)
        return {"reply": data["reply"].strip(), "remember": (data.get("remember") or "").strip()}
    except Exception as e:  # network, rate limit, bad JSON: fall back to templates
        print("claude reply failed", e.__class__.__name__, str(e)[:200])
        return None


async def reply(text: str, ctx: dict) -> dict:
    """ctx: location, person, profile, cards, attendance_text, memories, history, mood, lang_hint, now."""
    who = "staff member" if ctx.get("person") else "traveller"
    u = await understand(text, who, ctx["location"], ctx.get("lang_hint"), ctx.get("cards") or [], ctx.get("flights"))
    lang = u["lang"]

    remember = ""
    llm = await _claude_reply(text, lang, u, ctx)
    if llm:
        text_out, used, engine = llm["reply"], lang, f"{u['engine']}+claude"
        remember = llm["remember"]
    else:
        intent = u["intent"]
        card = u.get("card")
        card_text = f"{card['title']}: {card['text']}" if card else None
        if intent == "attendance":
            card_text = ctx.get("attendance_text")
        if intent in ("directions", "attendance") and not card_text:
            intent = "other"
        if card_text and intent in ("other", "flight"):
            intent = "directions"  # an info card answers it better than the generic line
        first = (ctx.get("profile") or {}).get("nickname") or (ctx.get("person") or "").split(" ")[0]
        text_out, used = phrases.render(
            intent, lang, name_part=f", {first}" if first else "", location=ctx["location"],
            airport=AIRPORT, wifi=WIFI, time=ctx["now"], card=card_text or "",
        )
        if u["intent"] == "flight":
            if u.get("flight"):
                text_out, used = phrases.render_flight(u["flight"], lang)
            elif ctx.get("flights") and not card_text:
                used = lang if lang in phrases.FLIGHT_ASK else "en"
                text_out = phrases.FLIGHT_ASK[used]
        # offer the "call a team member" button, except for plain venting ("I'm tired") at a staff kiosk
        if u.get("assist") and intent != "lost" and (intent != "share_bad" or u["assist"] == "medical"):
            offer = phrases.ASSIST_OFFER[used if used in phrases.ASSIST_OFFER else "en"]
            text_out = offer if intent in ("other", "greeting") else f"{text_out} {offer}"
        engine = f"{u['engine']}+templates"

    # wellbeing memories: remembered for a few days, used in the next greeting
    facts = []
    if ctx.get("person"):
        labels = {"tired": "said they were tired", "unwell": "said they were not feeling well",
                  "stressed": "seemed stressed", "celebrating": "was celebrating some good news"}
        for flag, p in (u.get("flags") or {}).items():
            if p >= 0.75:
                facts.append({"kind": flag, "fact": labels[flag]})
        if remember:
            facts.append({"kind": "note", "fact": remember})
    return {
        "reply": text_out, "lang": used, "locale": phrases.LOCALES.get(used, "en-GB"),
        "intent": u["intent"], "sentiment": u.get("sentiment"), "flags": u.get("flags"),
        "engine": engine, "facts": facts,
        "flight": u.get("flight"), "assist": u.get("assist"),
    }


# ----------------------------------------------------------------------------- greeting
ATTIRE_LINES = {
    "white": "Crisp white today — sharp and fresh.",
    "black": "Classic black — sleek and confident.",
    "charcoal": "Charcoal tones — understated and smart.",
    "grey": "That grey is effortlessly professional.",
    "light grey": "Light grey — calm, clean, polished.",
    "navy": "Navy suits you — very professional.",
    "blue": "That blue brings serious lagoon energy.",
    "sky blue": "Sky blue — matching the Maldivian horizon!",
    "teal": "Teal like the reef — love it.",
    "green": "Green looks fresh on you today.",
    "olive": "Olive is a great choice — earthy and calm.",
    "red": "Bold red — confident choice!",
    "maroon": "Maroon is such a classy colour.",
    "orange": "Sunset orange — you're lighting up the terminal.",
    "brown": "Warm brown tones — very put-together.",
    "yellow": "Sunshine yellow — instant good vibes.",
    "mustard": "Mustard — stylish and warm.",
    "purple": "Purple — regal energy today.",
    "pink": "Pink looks great — cheerful and bright.",
}
MOOD_LINES = {
    "happiness": ["That smile just brightened the whole terminal.", "Love the energy — keep smiling!"],
    "neutral": ["Calm and focused — nice.", "Steady and ready. Nice."],
    "surprise": ["You look like you've got a story to tell!"],
    "sadness": ["If today feels heavy, take it one step at a time — you've got this.",
                "Sending you a little extra sunshine today ☀️"],
    "anger": ["Deep breath — whatever it is, you're not alone.", "Tough moment? A glass of water and a pause work wonders."],
    "fear": ["It's okay — take a moment, the team has your back."],
    "disgust": ["Hang in there — tea break is never too far away."],
    "contempt": ["Hang in there — tea break is never too far away."],
}
DAY_LINES = {
    "sun": "A fresh week begins — let's set the tone.",
    "mon": "Monday momentum — you've got this.",
    "tue": "Tuesday — steady and strong.",
    "wed": "Halfway through the week already!",
    "thu": "Last stretch before the weekend.",
    "fri": "Working on a Friday — the airport runs because of people like you.",
    "sat": "Weekend shift hero — thank you.",
}


def _time_greeting(now: datetime) -> str:
    h = now.hour
    if h < 5:
        return "Good night"
    if h < 12:
        return "Good morning"
    if h < 17:
        return "Good afternoon"
    return "Good evening"


def greeting_candidates(g: dict) -> dict:
    """g: first, now, profile, mood, mood_consent, attire, action, stats, memories.
    Returns {headline, special, compliments[], behaviour[], chips[]}."""
    now: datetime = g["now"]
    first = g["first"]
    prof = g.get("profile") or {}
    stats = g.get("stats") or {}
    special = []
    headline = f"{_time_greeting(now)}, {first}"

    if prof.get("birthday") and prof["birthday"] == now.strftime("%m-%d"):
        headline = f"Happy Birthday, {first}! 🎂"
        special.append("The whole terminal is celebrating you today.")
    if prof.get("joined") and prof["joined"][5:] == now.strftime("%m-%d") and prof["joined"][:4] < now.strftime("%Y"):
        yrs = now.year - int(prof["joined"][:4])
        special.append(f"{yrs} year{'s' if yrs > 1 else ''} with the team today — thank you! 🎉")
    for m in g.get("memories") or []:
        if m["kind"] == "tired":
            special.append("Last time you told me you were tired — hope you got some good rest.")
        elif m["kind"] == "unwell":
            special.append("You weren't feeling well last time — hope you're better today.")
        elif m["kind"] == "celebrating":
            special.append("Still glowing from that good news? 🎉")
        elif m["kind"] == "stressed":
            special.append("You seemed under pressure last time — hope today is lighter.")
        if special:
            break

    compliments = []
    attire = g.get("attire") or {}
    colour = attire.get("name")
    if colour:
        if colour in UNIFORM_COLORS:
            compliments.append("Uniform on point today ✔")
        elif colour in ATTIRE_LINES:
            compliments.append(ATTIRE_LINES[colour])
    mood = g.get("mood") if g.get("mood_consent") else None
    if mood in MOOD_LINES:
        compliments.append(random.choice(MOOD_LINES[mood]))

    behaviour = []
    action = g.get("action")
    early = stats.get("early_minutes")
    if action == "IN" and early is not None:
        if early >= 3:
            behaviour.append(f"You're {early} minutes early — the team notices!")
        elif early >= 0:
            behaviour.append("Right on time. Perfect.")
        else:
            behaviour.append("Busy morning? Take a breath — you're here now, and that's what counts.")
    streak = stats.get("on_time_streak") or 0
    if streak >= 3:
        behaviour.append(f"🔥 {streak}-day on-time streak. Keep it alive!")
    if stats.get("stayed_late_yesterday"):
        behaviour.append("You stayed late yesterday — thank you for going the extra mile.")
    if action == "OUT" and stats.get("worked_today"):
        behaviour.append(f"{stats['worked_today']} on shift today — well-earned rest.")
    if action == "OUT" and stats.get("next_shift"):
        behaviour.append(f"See you {stats['next_shift']}.")
    if action == "BREAK":
        behaviour.append("Enjoy your break — hydrate and stretch!")
    if action == "BACK":
        behaviour.append("Welcome back — recharged and ready?")
    wd = now.strftime("%a").lower()
    if now.hour < 5 or now.hour >= 22:
        behaviour.append("Night-shift hero — the terminal never sleeps thanks to you.")
    elif wd in WEEKEND:
        behaviour.append("Weekend shift — the airport runs because of people like you.")
    else:
        behaviour.append(DAY_LINES.get(wd, "Have a great shift!"))

    chips = []
    if mood:
        v, word, emoji = MOODS.get(mood, (0, mood, "🙂"))
        chips.append({"icon": emoji, "text": f"Looking {word}"})
    if colour:
        chips.append({"icon": "●", "color": attire.get("hex"), "text": colour.title()})
    if streak >= 2:
        chips.append({"icon": "🔥", "text": f"{streak}-day streak"})
    if stats.get("shift_today"):
        chips.append({"icon": "🗓", "text": stats["shift_today"]})
    elif stats.get("next_shift") and action in ("OUT", None):
        chips.append({"icon": "🗓", "text": "Next: " + stats["next_shift"]})
    if stats.get("days_this_month"):
        chips.append({"icon": "✅", "text": f"{stats['days_this_month']} day{'' if stats['days_this_month'] == 1 else 's'} this month"})
    return {"headline": headline, "special": special, "compliments": compliments, "behaviour": behaviour, "chips": chips}


async def compose_greeting(g: dict) -> dict:
    """Pick the lines to show. Jev chooses the compliment that fits the moment best."""
    c = greeting_candidates(g)
    lines = list(c["special"][:1])
    pool = c["compliments"] + c["behaviour"][:2]
    if len(pool) > 1:
        state = (f"Staff member {g['first']} just walked up to the kiosk at {g['now']:%H:%M} on a {g['now']:%A}. "
                 f"Action: {g.get('action') or 'none yet'}. Facial expression: {g.get('mood') or 'unknown'}. "
                 f"Outfit colour: {(g.get('attire') or {}).get('name') or 'unknown'}. Stats: {json.dumps(g.get('stats') or {})}.")
        a = await jev(state, {"best": {
            "type": "choice", "instructions": "Which message would feel most genuine, kind and relevant to this person right now?",
            "criteria": {f"l{i}": t for i, t in enumerate(pool)}}}, timeout=2.5)
        if a:
            best = pool[int(a["best"]["choice"][1:])]
            pool.remove(best)
            pool.insert(0, best)
    for line in pool:
        if len(lines) >= 3:
            break
        if line not in lines:
            lines.append(line)
    # one behaviour/day line if the compliments crowded it out
    if not any(line in c["behaviour"] for line in lines) and c["behaviour"]:
        lines = lines[:2] + [c["behaviour"][0]]
    return {"headline": c["headline"], "lines": lines, "chips": c["chips"]}
