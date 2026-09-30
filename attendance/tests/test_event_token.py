import time

import app


def test_valid_token():
    tok = app.event_token("Ali", "Main")
    assert app.event_token_ok(tok, "Ali", "Main")


def test_token_is_bound_to_person_and_kiosk():
    tok = app.event_token("Ali", "Main")
    assert not app.event_token_ok(tok, "Sara", "Main")
    assert not app.event_token_ok(tok, "Ali", "Gate 2")


def test_expired_token(monkeypatch):
    tok = app.event_token("Ali", "Main")
    monkeypatch.setattr(time, "time", lambda: int(tok.split(".")[0]) + 1)
    assert not app.event_token_ok(tok, "Ali", "Main")


def test_tampered_expiry_is_rejected():
    exp, sig = app.event_token("Ali", "Main").split(".", 1)
    assert not app.event_token_ok(f"{int(exp) + 3600}.{sig}", "Ali", "Main")


def test_garbage_tokens():
    for tok in (None, "", "abc", "123", "x.y", 42):
        assert not app.event_token_ok(tok, "Ali", "Main")
