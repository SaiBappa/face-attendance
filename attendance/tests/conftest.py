"""
Shared fixtures. Every test gets its own empty SQLite file, so tests never touch attendance.db
and never see each other's rows. DB_PATH / MEDIA_DIR are pointed at a temp dir before any app
module is imported, because app.py creates the schema, media dir and event secret on import.
"""
import os
import sys
import tempfile
from datetime import datetime

_TMP = tempfile.mkdtemp(prefix="aura-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "import.db")
os.environ["MEDIA_DIR"] = os.path.join(_TMP, "media")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import store  # noqa: E402

# a fixed "today" so times in tests read naturally
DAY = "2026-09-30"


def at(hhmm: str, day: str = DAY) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init_db()
    c = store.db()
    yield c
    c.close()


def add_person(conn, name, department=None, zone=None):
    conn.execute("INSERT INTO people (name, department, zone, created) VALUES (?,?,?,?)",
                 (name, department, zone, f"{DAY}T00:00:00"))


def add_event(conn, employee, action, ts: datetime, kiosk="Main"):
    conn.execute("INSERT INTO events (ts, day, employee, action, kiosk) VALUES (?,?,?,?,?)",
                 (ts.isoformat(timespec="seconds"), ts.date().isoformat(), employee, action, kiosk))


def add_shift(conn, person, start, end, day=DAY, department=None):
    conn.execute("INSERT INTO shifts (person, day, start, end, department, source) VALUES (?,?,?,?,?,'manual')",
                 (person, day, start, end, department))
