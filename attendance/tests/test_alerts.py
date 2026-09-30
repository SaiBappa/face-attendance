from datetime import timedelta

import alerts
from conftest import add_event, add_person, add_shift, at


def types(conn):
    return sorted(r[0] for r in conn.execute("SELECT type FROM alerts"))


def test_no_show_alerts_once(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    assert alerts.scan(conn, at("08:10")) == 0          # still inside NO_SHOW_MINUTES
    assert alerts.scan(conn, at("08:20")) == 1
    assert alerts.scan(conn, at("08:30")) == 0          # same key: never repeated
    assert types(conn) == ["no_show"]


def test_late_alert(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("08:15"))
    alerts.scan(conn, at("09:00"))
    assert types(conn) == ["late"]


def test_on_time_raises_nothing(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("07:58"))
    assert alerts.scan(conn, at("09:00")) == 0


def test_missed_clock_out(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("08:00"))
    alerts.scan(conn, at("16:30"))
    assert types(conn) == []
    alerts.scan(conn, at("17:05"))
    assert types(conn) == ["no_clock_out"]


def test_long_break(conn):
    add_event(conn, "Ali", "IN", at("08:00"))
    add_event(conn, "Ali", "BREAK", at("12:00"))
    alerts.scan(conn, at("12:30"))
    assert types(conn) == []
    alerts.scan(conn, at("13:01"))
    assert types(conn) == ["long_break"]


def test_understaffed(conn):
    for p in ("Ali", "Sara", "Omar"):
        add_person(conn, p, department="Ramp")
        add_shift(conn, p, "08:00", "16:00", department="Ramp")
    add_event(conn, "Ali", "IN", at("08:00"))
    alerts.scan(conn, at("09:00"))
    assert types(conn).count("understaffed") == 1       # 1 on duty vs 3 rostered


def test_pass_incident_is_one_per_window(conn):
    t = at("09:00")
    assert alerts.pass_incident(conn, "Ali", "Ramp", "Main", "2026-09-01", t) is True
    assert alerts.pass_incident(conn, "Ali", "Ramp", "Main", "2026-09-01", t + timedelta(minutes=1)) is False
    assert alerts.pass_incident(conn, "Ali", "Ramp", "Gate 2", "2026-09-01", t + timedelta(minutes=1)) is True
    later = t + timedelta(minutes=alerts.PASS_ALERT_MIN + 1)
    assert alerts.pass_incident(conn, "Ali", "Ramp", "Main", "2026-09-01", later) is True


def test_spoof_incident_is_one_per_window(conn):
    t = at("09:00")
    assert alerts.spoof_incident(conn, "Ali", "Ramp", "Main", t) is True
    assert alerts.spoof_incident(conn, "Ali", "Ramp", "Main", t + timedelta(minutes=2)) is False


def test_gate_incident(conn):
    t = at("09:00")
    assert alerts.gate_incident(conn, "zone", "Ali", None, "G1", "Server Room", zone="white", min_zone="blue", now=t)
    assert alerts.gate_incident(conn, "passback", "Ali", None, "G1", "Server Room", missed_exit=True, now=t)
    assert alerts.gate_incident(conn, None, "Ali", None, "G1", "Server Room", now=t) is False
    assert types(conn) == ["area_zone", "passback"]


def test_route_prefers_department_then_default(conn):
    conn.execute("INSERT INTO alert_routes (department, webhook) VALUES ('*', 'https://default'), ('Ramp', 'https://ramp')")
    assert alerts._route(conn, "Ramp") == "https://ramp"
    assert alerts._route(conn, "Cargo") == "https://default"
    assert alerts._route(conn, None) == "https://default"
