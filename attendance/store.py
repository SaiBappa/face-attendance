"""
SQLite schema for Aura. One file, created/migrated on start-up.

  events         IN / BREAK / BACK / OUT per employee (the original attendance log)
  people         staff profile: role, department, shift, birthday, language, mood consent
  sightings      one row per approach to a kiosk: who (or anonymous visitor), mood, attire
  interactions   everything said to / by the kiosk, per person or anonymous visitor
  memories       short facts the kiosk remembers about a person ("said they were tired")
  kiosks         per-location screen config: zone, headline, theme, info cards
  media          images/videos shown on a kiosk's ambient screen ('*' = every kiosk)
  announcements  ticker messages per kiosk ('*' = every kiosk)
  requests       passenger/staff assistance requests (wheelchair, medical, lost item…) and their SLA
  safety_rules   per-department PPE items + fatigue-check limits (ramp safety pack)
  safety_checks  one row per clock-in safety check: PPE confirmed/missing, hi-vis share, fatigue score

Rows produced by the demo generator carry demo=1 so they can be removed in one go.
"""
import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "attendance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,           -- local ISO timestamp
    day        TEXT NOT NULL,           -- YYYY-MM-DD, for fast filtering
    employee   TEXT NOT NULL,
    action     TEXT NOT NULL,
    kiosk      TEXT,
    similarity REAL
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_emp ON events(employee, ts);

CREATE TABLE IF NOT EXISTS people (
    name         TEXT PRIMARY KEY,
    role         TEXT,
    department   TEXT,
    shift_start  TEXT,                  -- HH:MM; falls back to SHIFT_START
    birthday     TEXT,                  -- MM-DD
    joined       TEXT,                  -- YYYY-MM-DD
    language     TEXT,                  -- preferred reply language (ISO 639-1)
    nickname     TEXT,
    mood_consent INTEGER NOT NULL DEFAULT 1,
    notes        TEXT,
    demo         INTEGER NOT NULL DEFAULT 0,
    created      TEXT
);

CREATE TABLE IF NOT EXISTS sightings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    day        TEXT NOT NULL,
    hour       INTEGER NOT NULL,
    kiosk      TEXT,
    person     TEXT,                    -- NULL for visitors
    kind       TEXT NOT NULL,           -- staff | visitor
    mood       TEXT,                    -- FER+ label, NULL when not consented
    valence    REAL,                    -- -1 .. 1
    confidence REAL,
    attire     TEXT,
    demo       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sight_day ON sightings(day);
CREATE INDEX IF NOT EXISTS idx_sight_person ON sightings(person, day);

CREATE TABLE IF NOT EXISTS interactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    day        TEXT NOT NULL,
    kiosk      TEXT,
    person     TEXT,                    -- NULL for visitors
    kind       TEXT NOT NULL,           -- staff | visitor
    encounter  TEXT,                    -- groups one conversation at the kiosk
    channel    TEXT NOT NULL,           -- greeting | voice | text
    lang       TEXT,
    user_text  TEXT,
    reply      TEXT,
    intent     TEXT,
    sentiment  REAL,                    -- 0 .. 1 (Jev score, normalised)
    mood       TEXT,
    engine     TEXT,
    demo       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inter_day ON interactions(day);
CREATE INDEX IF NOT EXISTS idx_inter_person ON interactions(person, id);

CREATE TABLE IF NOT EXISTS memories (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    person  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    fact    TEXT NOT NULL,
    kind    TEXT,                       -- tired | unwell | stressed | celebrating | note
    demo    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_person ON memories(person, id);

CREATE TABLE IF NOT EXISTS kiosks (
    name      TEXT PRIMARY KEY,
    zone      TEXT,
    headline  TEXT,
    subtitle  TEXT,
    theme     TEXT DEFAULT 'lagoon',
    language  TEXT DEFAULT 'en',
    voice     INTEGER NOT NULL DEFAULT 1,
    info      TEXT DEFAULT '[]',        -- JSON [{icon, title, text}]
    last_seen TEXT,
    demo      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kiosk    TEXT NOT NULL DEFAULT '*',
    file     TEXT NOT NULL,
    caption  TEXT,
    mime     TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created  TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    kiosk       TEXT,
    kind        TEXT NOT NULL,              -- wheelchair | medical | lost_item | porter | security | other
    details     TEXT,
    lang        TEXT,
    person      TEXT,                       -- staff member who raised it, NULL for travellers
    encounter   TEXT,
    source      TEXT,                       -- button | conversation
    status      TEXT NOT NULL DEFAULT 'open',   -- open | acknowledged | done | cancelled
    assigned_to TEXT,
    ack_ts      TEXT,
    done_ts     TEXT,
    notes       TEXT,
    demo        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_req_status ON requests(status, id);
CREATE INDEX IF NOT EXISTS idx_req_day ON requests(day);

CREATE TABLE IF NOT EXISTS safety_rules (
    department TEXT PRIMARY KEY,
    ppe        TEXT DEFAULT '[]',          -- JSON list of PPE keys (safety.PPE)
    fatigue    INTEGER NOT NULL DEFAULT 0, -- run the fatigue check at clock-in
    min_rest_h REAL, max_24h_h REAL, max_7d_h REAL, max_days INTEGER
);

CREATE TABLE IF NOT EXISTS safety_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    day           TEXT NOT NULL,
    person        TEXT NOT NULL,
    department    TEXT,
    kiosk         TEXT,
    items         TEXT,                    -- JSON {key: ok|missing}
    missing       TEXT,                    -- comma list, NULL when complete
    hivis         REAL,                    -- fluorescent share seen by the camera
    rested        INTEGER,                 -- self-rating 1..5
    fatigue_score INTEGER,
    fatigue_level TEXT,                    -- low | moderate | high
    factors       TEXT,                    -- JSON list explaining the score
    result        TEXT NOT NULL,           -- pass | flagged
    demo          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_safety_day ON safety_checks(day);
CREATE INDEX IF NOT EXISTS idx_safety_person ON safety_checks(person, ts);

CREATE TABLE IF NOT EXISTS announcements (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kiosk   TEXT NOT NULL DEFAULT '*',
    text    TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',   -- info | alert
    starts  TEXT,
    ends    TEXT,
    created TEXT
);
"""

# columns added to pre-existing tables (original install only had `events`)
MIGRATIONS = {
    "events": {"mood": "TEXT", "attire": "TEXT", "demo": "INTEGER NOT NULL DEFAULT 0"},
    # flight board on the ambient screen: departures | arrivals | both | off
    "kiosks": {"flights": "TEXT DEFAULT 'both'"},
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
