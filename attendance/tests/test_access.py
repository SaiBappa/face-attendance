from datetime import timedelta

import pytest

import access
from conftest import add_event, add_person, at

GATE = {"name": "G1", "mode": "gate", "area": "Server Room", "direction": "both"}


def gate(**kw):
    return {**GATE, **kw}


# ----------------------------------------------------------------------------- zones / config
@pytest.mark.parametrize("person, rule, ok", [
    ("white", None, True),        # no rule = everyone
    (None, None, True),
    ("green", "blue", True),      # green is the highest access
    ("blue", "blue", True),
    ("white", "blue", False),
    (None, "blue", False),        # no zone on file while the area has a rule
    ("purple", "white", False),   # unknown colour
    (" Red ", "orange", True),    # tolerant of case / whitespace
])
def test_zone_ok(person, rule, ok):
    assert access.zone_ok(person, rule) is ok


def test_kiosk_actions():
    assert access.kiosk_actions({}) == access.ATTENDANCE_ACTIONS
    assert access.kiosk_actions({"actions": "out,in"}) == ("IN", "OUT")   # kept in canonical order
    assert access.kiosk_actions({"actions": "bogus"}) == access.ATTENDANCE_ACTIONS
    assert access.kiosk_actions({"mode": "gate", "actions": "IN"}) == ()


def test_clean_actions():
    assert access.clean_actions(["in", "break"]) == "IN,BREAK"
    assert access.clean_actions("IN,BREAK,BACK,OUT") is None   # all four = NULL
    assert access.clean_actions("") is None


def test_save_settings_rejects_unknown_zone(conn):
    with pytest.raises(ValueError):
        access.save_settings(conn, "Server Room", [], False, min_zone="purple")


def test_ask_reason_needs_reasons(conn):
    access.save_settings(conn, "Server Room", [" ", ""], ask_reason=True)
    assert access.settings(conn, "Server Room")["ask_reason"] is False


# ----------------------------------------------------------------------------- toggling
def test_both_way_gate_toggles_in_then_out(conn):
    first = access.record(conn, "Ali", GATE, at("09:00:00"))
    second = access.record(conn, "Ali", GATE, at("09:05:00"))
    assert (first["move"], second["move"]) == ("IN", "OUT")
    assert first["flag"] is None and second["flag"] is None


def test_repeat_within_window_is_a_duplicate(conn):
    access.record(conn, "Ali", GATE, at("09:00:00"))
    again = access.record(conn, "Ali", GATE, at("09:00:05"))
    assert again["duplicate"] is True and again["move"] == "IN"
    assert conn.execute("SELECT COUNT(*) FROM access").fetchone()[0] == 1


def test_quick_out_and_back_after_repeat_window_toggles(conn):
    t = at("09:00:00")
    access.record(conn, "Ali", GATE, t)
    out = access.record(conn, "Ali", GATE, t + timedelta(seconds=access.REPEAT_SECONDS + 1))
    assert out["move"] == "OUT" and not out["duplicate"]


def test_repeat_only_applies_to_the_same_gate(conn):
    t = at("09:00:00")
    access.record(conn, "Ali", GATE, t)
    other = access.record(conn, "Ali", gate(name="G2"), t + timedelta(seconds=2))
    assert other["move"] == "OUT" and not other["duplicate"]


def test_stale_entry_counts_as_outside(conn):
    t = at("06:00:00")
    access.record(conn, "Ali", GATE, t)
    later = access.record(conn, "Ali", GATE, t + timedelta(hours=access.STALE_HOURS + 1))
    assert later["move"] == "IN"
    assert later["flag"] is None   # a missed exit that old is the no-exit alert's business


# ----------------------------------------------------------------------------- flags
def test_exit_without_entry_is_passback(conn):
    r = access.record(conn, "Ali", gate(direction="out"), at("09:00:00"))
    assert r["move"] == "OUT" and r["missed_entry"] and r["flag"] == "passback"


def test_second_entry_without_exit_is_passback(conn):
    k = gate(direction="in")
    access.record(conn, "Ali", k, at("09:00:00"))
    r = access.record(conn, "Ali", k, at("09:10:00"))
    assert r["move"] == "IN" and r["missed_exit"] and r["flag"] == "passback"


def test_admin_moves_are_never_flagged(conn):
    r = access.record(conn, "Ali", GATE, at("09:00:00"), source="admin", move="OUT")
    assert r["flag"] is None


def test_unauthorised_entry_is_recorded_and_flagged(conn):
    access.save_settings(conn, "Server Room", [], False, min_zone="blue")
    add_person(conn, "Ali", zone="white")
    r = access.record(conn, "Ali", GATE, at("09:00:00"))
    assert r["move"] == "IN" and r["flag"] == "zone"
    assert access.last_move(conn, "Ali", "Server Room")["direction"] == "IN"


# ----------------------------------------------------------------------------- entry reasons
@pytest.fixture
def reasons(conn):
    access.save_settings(conn, "Server Room", ["Maintenance", "Audit"], ask_reason=True)


def test_entry_needs_a_reason(conn, reasons):
    with pytest.raises(access.ReasonRequired):
        access.record(conn, "Ali", GATE, at("09:00:00"))
    assert conn.execute("SELECT COUNT(*) FROM access").fetchone()[0] == 0


def test_entry_rejects_a_reason_not_offered(conn, reasons):
    with pytest.raises(access.ReasonRequired):
        access.record(conn, "Ali", GATE, at("09:00:00"), reason="Lunch")


def test_entry_with_reason_and_exit_without(conn, reasons):
    r = access.record(conn, "Ali", GATE, at("09:00:00"), reason="Audit")
    assert r["reason"] == "Audit"
    out = access.record(conn, "Ali", GATE, at("09:30:00"))
    assert out["move"] == "OUT" and out["reason"] is None


def test_unauthorised_person_is_not_asked_why(conn, reasons):
    access.save_settings(conn, "Server Room", ["Audit"], True, min_zone="green")
    add_person(conn, "Ali", zone="white")
    st = access.state(conn, "Ali", GATE, at("09:00:00"))
    assert st["ask_reason"] is False
    assert access.record(conn, "Ali", GATE, at("09:00:00"))["flag"] == "zone"


# ----------------------------------------------------------------------------- presence + alerts
def test_presence_and_area_count(conn):
    now = at("10:00:00")
    access.record(conn, "Ali", GATE, at("09:00:00"))
    access.record(conn, "Sara", GATE, at("09:10:00"))
    access.record(conn, "Sara", GATE, at("09:20:00"))   # left again
    [room] = access.presence(conn, now)
    assert room["area"] == "Server Room" and room["count"] == 1
    assert [p["person"] for p in room["inside"]] == ["Ali"]
    assert access.area_count(conn, "Server Room", now) == 1


def _alert_types(conn, now):
    return sorted(a["type"] for a in access.due_alerts(conn, now))


def test_overstay_alert(conn):
    access.save_settings(conn, "Server Room", [], False, max_minutes=30)
    access.record(conn, "Ali", GATE, at("09:00:00"))
    assert _alert_types(conn, at("09:30:00")) == []
    assert _alert_types(conn, at("09:31:00")) == ["overstay"]


def test_capacity_alert(conn):
    access.save_settings(conn, "Server Room", [], False, capacity=1)
    access.record(conn, "Ali", GATE, at("09:00:00"))
    assert _alert_types(conn, at("09:05:00")) == []
    access.record(conn, "Sara", GATE, at("09:01:00"))
    assert _alert_types(conn, at("09:05:00")) == ["capacity"]


def test_no_exit_alert_when_stale(conn):
    access.record(conn, "Ali", GATE, at("06:00:00"))
    later = at("06:00:00") + timedelta(hours=access.STALE_HOURS + 1)
    assert _alert_types(conn, later) == ["no_exit"]


def test_no_exit_alert_after_clocking_out(conn):
    access.record(conn, "Ali", GATE, at("09:00:00"))
    add_event(conn, "Ali", "OUT", at("12:00:00"))
    assert _alert_types(conn, at("12:00:00") + timedelta(minutes=access.EXIT_GRACE_MIN - 1)) == []
    assert _alert_types(conn, at("12:00:00") + timedelta(minutes=access.EXIT_GRACE_MIN)) == ["no_exit"]


def test_alert_keys_are_stable_across_scans(conn):
    access.save_settings(conn, "Server Room", [], False, max_minutes=30)
    access.record(conn, "Ali", GATE, at("09:00:00"))
    k1 = [a["key"] for a in access.due_alerts(conn, at("09:40:00"))]
    k2 = [a["key"] for a in access.due_alerts(conn, at("09:50:00"))]
    assert k1 == k2
