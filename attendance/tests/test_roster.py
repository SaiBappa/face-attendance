from datetime import datetime

import pytest

import roster
from conftest import DAY, add_event, add_shift, at


# ----------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("raw, want", [
    ("8:30", "08:30"), ("08.30", "08:30"), ("0830", "08:30"), ("8h30", "08:30"), ("8", "08:00"),
    ("8pm", "20:00"), ("8:15 PM", "20:15"), ("12am", "00:00"), ("12pm", "12:00"), ("24:00", "00:00"),
    ("25:00", None), ("8:75", None), ("", None), ("noon", None),
])
def test_time(raw, want):
    assert roster._time(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("2026-09-30", "2026-09-30"), ("30/09/2026", "2026-09-30"), ("30.09.2026", "2026-09-30"),
    ("30 Sep 2026", "2026-09-30"), ("2026/09/30", None), ("", None),
])
def test_date(raw, want):
    assert roster._date(raw) == want


def test_parse_csv_aliases_semicolons_and_bom():
    text = "﻿Employee;Shift Date;From;To;Dept\nAli;30/09/2026;8:00;16:00;Ramp\n"
    shifts, errors = roster.parse_csv(text)
    assert errors == []
    assert shifts == [{"person": "Ali", "day": "2026-09-30", "start": "08:00", "end": "16:00", "position": None,
                       "location": None, "department": "Ramp", "notes": None}]


def test_parse_csv_missing_columns():
    shifts, errors = roster.parse_csv("name,date,start\nAli,2026-09-30,08:00\n")
    assert shifts == [] and "end" in errors[0]


def test_parse_csv_reports_bad_rows_and_keeps_good_ones():
    text = "name,date,start,end\nAli,2026-09-30,08:00,16:00\nSara,soon,08:00,16:00\nOmar,2026-09-30,09:00,09:00\n\n"
    shifts, errors = roster.parse_csv(text)
    assert [s["person"] for s in shifts] == ["Ali"]
    assert len(errors) == 2 and errors[0].startswith("Row 3") and errors[1].startswith("Row 4")


def test_validate_json_push():
    shifts, errors = roster.validate([{"name": "Ali", "date": "2026-09-30", "start": "8", "end": "16"}, {"name": "x"}])
    assert shifts[0]["start"] == "08:00" and len(errors) == 1


def test_save_replaces_same_person_and_day(conn):
    s = {"person": "Ali", "day": DAY, "start": "08:00", "end": "16:00", "department": "Ramp"}
    roster.save(conn, [s], "csv")
    res = roster.save(conn, [{**s, "start": "10:00"}], "csv")
    assert res == {"imported": 1, "replaced": 1}
    assert [r["start"] for r in roster.shifts_between(conn, DAY, DAY)] == ["10:00"]
    assert conn.execute("SELECT department FROM people WHERE name='Ali'").fetchone()[0] == "Ramp"


# ----------------------------------------------------------------------------- windows
def test_overnight_window():
    st, en = roster.window({"day": DAY, "start": "22:00", "end": "06:00"})
    assert st == at("22:00") and en == datetime(2026, 10, 1, 6, 0)


def test_planned_start_is_the_first_shift(conn):
    add_shift(conn, "Ali", "14:00", "18:00")
    add_shift(conn, "Ali", "06:00", "10:00")
    assert roster.planned_start(conn, "Ali", DAY) == 6 * 60
    assert roster.planned_start(conn, "Nobody", DAY) is None


# ----------------------------------------------------------------------------- adherence
def one(conn, now):
    [r] = roster.adherence(conn, DAY, DAY, now)
    return r


def test_on_time_within_grace(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("08:05"))
    r = one(conn, at("09:00"))
    assert r["status"] == "in_progress" and r["late_min"] == 0


def test_late_beyond_grace(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("08:20"))
    assert one(conn, at("09:00"))["late_min"] == 20


def test_early_clock_in_is_not_negative_lateness(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("06:00"))
    assert one(conn, at("09:00"))["late_min"] == 0


@pytest.mark.parametrize("now, status", [("07:00", "upcoming"), ("08:30", "late_pending"), ("09:00", "no_show")])
def test_no_clock_in_statuses(conn, now, status):
    add_shift(conn, "Ali", "08:00", "16:00")
    assert one(conn, at(now))["status"] == status


def test_early_leave_and_overtime(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_shift(conn, "Sara", "08:00", "16:00")
    for p in ("Ali", "Sara"):
        add_event(conn, p, "IN", at("08:00"))
    add_event(conn, "Ali", "OUT", at("15:00"))
    add_event(conn, "Sara", "OUT", at("17:00"))
    rows = {r["person"]: r for r in roster.adherence(conn, DAY, DAY, at("18:00"))}
    assert rows["Ali"]["early_leave_min"] == 60 and rows["Ali"]["overtime_min"] == 0
    assert rows["Sara"]["overtime_min"] == 60 and rows["Sara"]["early_leave_min"] == 0
    assert rows["Ali"]["status"] == rows["Sara"]["status"] == "worked"


def test_overnight_shift_clock_out_next_day(conn):
    add_shift(conn, "Ali", "22:00", "06:00")
    add_event(conn, "Ali", "IN", at("21:55"))
    add_event(conn, "Ali", "OUT", datetime(2026, 10, 1, 6, 5))
    r = one(conn, datetime(2026, 10, 1, 8, 0))
    assert r["status"] == "worked" and r["late_min"] == 0 and r["early_leave_min"] == 0


def test_missed_clock_out(conn):
    add_shift(conn, "Ali", "08:00", "16:00")
    add_event(conn, "Ali", "IN", at("08:00"))
    assert one(conn, at("17:30"))["status"] == "no_clock_out"


def test_summary(conn):
    past = "2026-01-15"   # summary() uses the real clock, so use a day that is over
    add_shift(conn, "Ali", "08:00", "16:00", day=past, department="Ramp")
    add_shift(conn, "Sara", "08:00", "16:00", day=past, department="Ramp")
    add_event(conn, "Ali", "IN", at("08:30", past))
    add_event(conn, "Ali", "OUT", at("16:00", past))
    s = roster.summary(conn, past, past)
    assert (s["shifts"], s["no_shows"], s["attendance_pct"], s["late_pct"]) == (2, 1, 50.0, 100.0)
    assert s["recent_no_shows"][0]["person"] == "Sara"
    assert roster.summary(conn, "2026-02-01", "2026-02-02")["attendance_pct"] is None
