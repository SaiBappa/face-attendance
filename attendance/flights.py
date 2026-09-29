"""
Live flight information for the kiosks: a departures/arrivals board, and answers to
"is EK653 on time?" / "which gate for Dubai?".

Providers (FLIGHT_PROVIDER):
  aerodatabox  AeroDataBox FIDS via API.Market (AERODATABOX_KEY) or RapidAPI (AERODATABOX_RAPIDAPI_KEY)
  json         FLIGHT_FEED_URL returning {"flights": [<normalised flight>, ...]} — for airports that
               expose their own FIDS; same shape as /api/flights returns
  demo         a realistic, always-moving schedule for FLIGHT_AIRPORT (used when no key is set)
  off          no flight features

Every provider is normalised to:
  {id, dir: dep|arr, number, airline, city, city_iata, scheduled, estimated, terminal, gate,
   desk, belt, status, delay_min}
where times are local ISO strings and status is one of STATUSES.
"""
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

AIRPORT = os.environ.get("FLIGHT_AIRPORT", "MLE").upper()
ADB_KEY = os.environ.get("AERODATABOX_KEY", "")
ADB_RAPID_KEY = os.environ.get("AERODATABOX_RAPIDAPI_KEY", "")
FEED_URL = os.environ.get("FLIGHT_FEED_URL", "")
REFRESH_S = int(os.environ.get("FLIGHT_REFRESH_SECONDS", "180"))
PROVIDER = os.environ.get("FLIGHT_PROVIDER") or (
    "aerodatabox" if (ADB_KEY or ADB_RAPID_KEY) else "json" if FEED_URL else "demo")

STATUSES = ("scheduled", "checkin", "boarding", "gate_closed", "departed", "delayed",
            "cancelled", "expected", "approaching", "landed", "diverted")

_cache = {"t": 0.0, "flights": [], "error": None, "source": PROVIDER}


def _hhmm_local(s: Optional[str]) -> Optional[str]:
    """'2026-09-29 14:05+05:00' / ISO -> naive local ISO '2026-09-29T14:05:00'."""
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", s)
    return f"{m.group(1)}T{m.group(2)}:00" if m else None


# ----------------------------------------------------------------------------- AeroDataBox
ADB_STATUS = {
    "Expected": "expected", "EnRoute": "expected", "CheckIn": "checkin", "Boarding": "boarding",
    "GateClosed": "gate_closed", "Departed": "departed", "Delayed": "delayed", "Approaching": "approaching",
    "Arrived": "landed", "Canceled": "cancelled", "CanceledUncertain": "cancelled", "Diverted": "diverted",
}


async def _aerodatabox() -> list:
    if ADB_KEY:
        base, headers = "https://prod.api.market/api/v1/aedbx/aerodatabox", {"x-api-market-key": ADB_KEY}
    else:
        base, headers = "https://aerodatabox.p.rapidapi.com", {
            "x-rapidapi-key": ADB_RAPID_KEY, "x-rapidapi-host": "aerodatabox.p.rapidapi.com"}
    params = {"offsetMinutes": -90, "durationMinutes": 720, "direction": "Both", "withLeg": "false",
              "withCancelled": "true", "withCodeshared": "false", "withCargo": "false", "withPrivate": "false"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{base}/flights/airports/iata/{AIRPORT}", params=params, headers=headers)
    r.raise_for_status()
    data = r.json()
    out = []
    for dir_key, d in (("departures", "dep"), ("arrivals", "arr")):
        for f in data.get(dir_key) or []:
            mv = f.get("movement") or {}
            ap = mv.get("airport") or {}
            sched = _hhmm_local((mv.get("scheduledTime") or {}).get("local"))
            est = _hhmm_local((mv.get("revisedTime") or {}).get("local"))
            delay = None
            if sched and est:
                delay = int((datetime.fromisoformat(est) - datetime.fromisoformat(sched)).total_seconds() // 60)
            status = ADB_STATUS.get(f.get("status"), "scheduled")
            if status in ("expected", "scheduled") and delay and delay >= 15:
                status = "delayed"
            out.append({
                "id": f"{d}-{f.get('number', '').replace(' ', '')}-{sched}",
                "dir": d, "number": (f.get("number") or "").replace(" ", ""),
                "airline": (f.get("airline") or {}).get("name"),
                "city": ap.get("municipalityName") or ap.get("name"), "city_iata": ap.get("iata"),
                "scheduled": sched, "estimated": est, "terminal": mv.get("terminal"),
                "gate": mv.get("gate"), "desk": mv.get("checkInDesk"), "belt": mv.get("baggageBelt"),
                "status": status, "delay_min": delay,
            })
    return out


async def _json_feed() -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(FEED_URL)
    r.raise_for_status()
    return (r.json() or {}).get("flights") or []


# ----------------------------------------------------------------------------- demo
# (arriving no., departing no., airline, city, iata, departure HH:MM) — a plausible Velana day;
# the inbound flight lands 1h40 before the return departure
DEMO_ROUTES = [
    ("EK652", "EK653", "Emirates", "Dubai", "DXB", "09:25"),
    ("QR672", "QR673", "Qatar Airways", "Doha", "DOH", "08:05"),
    ("SQ452", "SQ451", "Singapore Airlines", "Singapore", "SIN", "12:55"),
    ("TK730", "TK731", "Turkish Airlines", "Istanbul", "IST", "07:20"),
    ("EY278", "EY279", "Etihad Airways", "Abu Dhabi", "AUH", "10:10"),
    ("UL101", "UL102", "SriLankan Airlines", "Colombo", "CMB", "10:50"),
    ("6E1131", "6E1132", "IndiGo", "Bengaluru", "BLR", "13:40"),
    ("AI263", "AI264", "Air India", "Delhi", "DEL", "14:30"),
    ("3U8537", "3U8538", "Sichuan Airlines", "Chengdu", "CTU", "02:35"),
    ("SU321", "SU320", "Aeroflot", "Moscow", "SVO", "11:30"),
    ("BA2043", "BA2042", "British Airways", "London", "LGW", "16:05"),
    ("DE2208", "DE2209", "Condor", "Frankfurt", "FRA", "15:15"),
    ("FZ1567", "FZ1568", "flydubai", "Dubai", "DXB", "18:20"),
    ("NR301", "NR302", "Manta Air", "Dhaalu Airport", "DDD", "07:45"),
    ("Q2123", "Q2124", "Maldivian", "Gan", "GAN", "06:30"),
    ("WK64", "WK65", "Edelweiss", "Zurich", "ZRH", "21:40"),
    ("CZ6099", "CZ6100", "China Southern", "Guangzhou", "CAN", "01:50"),
    ("GF147", "GF148", "Gulf Air", "Bahrain", "BAH", "05:55"),
]


def _demo() -> list:
    now = datetime.now().replace(second=0, microsecond=0)
    out = []
    for day_off in (-1, 0, 1):
        day = (now + timedelta(days=day_off)).date()
        rng = random.Random(day.toordinal())       # stable for the whole day
        for i, (arr_no, dep_no, airline, city, iata, dep_t) in enumerate(DEMO_ROUTES):
            for d, number in (("arr", arr_no), ("dep", dep_no)):
                sched = datetime.combine(day, datetime.strptime(dep_t, "%H:%M").time())
                if d == "arr":
                    sched -= timedelta(hours=1, minutes=40)   # arrives well before the return departure
                delay = rng.choice([0, 0, 0, 0, 0, 5, 10, 25, 45, 80])
                cancelled = rng.random() < 0.02
                est = sched + timedelta(minutes=delay)
                mins = (est - now).total_seconds() / 60
                if cancelled:
                    status = "cancelled"
                elif d == "dep":
                    status = ("departed" if mins < -5 else "gate_closed" if mins < 10 else "boarding" if mins < 40
                              else "checkin" if mins < 180 else "delayed" if delay >= 15 else "scheduled")
                    if delay >= 15 and status in ("checkin", "scheduled"):
                        status = "delayed"
                else:
                    status = ("landed" if mins < 0 else "approaching" if mins < 20
                              else "delayed" if delay >= 15 else "expected")
                out.append({
                    "id": f"{d}-{number}-{sched:%Y%m%d}", "dir": d, "number": number, "airline": airline,
                    "city": city, "city_iata": iata, "scheduled": sched.isoformat(), "estimated": est.isoformat(),
                    "terminal": "D" if iata in ("DDD", "GAN") else "I",
                    "gate": None if d == "arr" else str(1 + i % 7), "desk": None if d == "arr" else f"{10 + i % 9 * 3}-{12 + i % 9 * 3}",
                    "belt": str(1 + i % 4) if d == "arr" else None,
                    "status": status, "delay_min": delay,
                })
    return out


# ----------------------------------------------------------------------------- public API
async def all_flights(force: bool = False) -> list:
    if PROVIDER == "off":
        return []
    if not force and time.time() - _cache["t"] < (REFRESH_S if PROVIDER != "demo" else 30) and _cache["flights"]:
        return _cache["flights"]
    try:
        if PROVIDER == "aerodatabox":
            flights = await _aerodatabox()
        elif PROVIDER == "json":
            flights = await _json_feed()
        else:
            flights = _demo()
        flights.sort(key=lambda f: f.get("estimated") or f.get("scheduled") or "")
        _cache.update(t=time.time(), flights=flights, error=None)
    except Exception as e:  # keep serving the last good board
        _cache.update(t=time.time(), error=f"{e.__class__.__name__}: {str(e)[:120]}")
        print("flight feed failed", _cache["error"])
    return _cache["flights"]


def status() -> dict:
    return {"provider": PROVIDER, "airport": AIRPORT, "flights": len(_cache["flights"]),
            "updated": datetime.fromtimestamp(_cache["t"]).isoformat(timespec="seconds") if _cache["t"] else None,
            "error": _cache["error"]}


async def board(direction: str = "both", limit: int = 8) -> dict:
    """Upcoming flights for a kiosk board: from 20 min ago onwards, departed/landed dropped quickly."""
    now = datetime.now()
    keep = []
    for f in await all_flights():
        t = datetime.fromisoformat(f.get("estimated") or f["scheduled"])
        age = (now - t).total_seconds() / 60
        if age > (10 if f["status"] in ("departed", "landed") else 45) or t - now > timedelta(hours=10):
            continue
        keep.append(f)
    return {
        "departures": [f for f in keep if f["dir"] == "dep"][:limit] if direction in ("both", "departures") else [],
        "arrivals": [f for f in keep if f["dir"] == "arr"][:limit] if direction in ("both", "arrivals") else [],
        "airport": AIRPORT, "provider": PROVIDER,
    }


FLIGHT_NO = re.compile(r"\b([A-Z0-9]{2})\s?-?(\d{1,4})\b")


def find_by_number(text: str, flights: list) -> Optional[dict]:
    """'ek 653', 'EK653', 'flight 6E1131' -> the nearest matching flight."""
    now = datetime.now()
    for code, num in FLIGHT_NO.findall(text.upper()):
        if code.isdigit():
            continue
        want = f"{code}{int(num)}"
        hits = [f for f in flights if re.sub(r"^([A-Z0-9]{2})0*", r"\1", f["number"]) == want]
        if hits:
            return min(hits, key=lambda f: abs((datetime.fromisoformat(f["scheduled"]) - now).total_seconds()))
    return None


def upcoming_for_choice(flights: list, limit: int = 40) -> list:
    """Candidates for Jev to choose from when the speaker names a city/airline instead of a number."""
    now = datetime.now()
    soon = [f for f in flights if timedelta(minutes=-60) < datetime.fromisoformat(f.get("estimated") or f["scheduled"]) - now < timedelta(hours=14)]
    return soon[:limit]
