from __future__ import annotations

import time

import pytest

from wacal import db, gcal
from wacal.gcal import ExternalCalendarError

from .conftest import GUEST, NOW, OWNER, say

TOKEN = '{"token": "stub", "refresh_token": "r"}'


@pytest.fixture()
def linked(conn, people, fake_google):
    owner, guest = people
    db.save_calendar_link(conn, owner["id"], TOKEN, account="burhan@example.com")
    say(conn, OWNER, f"share {GUEST}")
    return owner, guest


def titles(outcome):
    return [row.title for row in outcome.reply.rows]


def test_google_events_block_slots(conn, linked, fake_google):
    # 10:00-11:00 Riyadh is 07:00-08:00 UTC.
    fake_google.busy_intervals = [
        ("2026-10-15T07:00:00Z", "2026-10-15T08:00:00Z")
    ]
    offered = titles(say(conn, GUEST, f"free {OWNER} today"))
    assert "10:00-10:30" not in offered
    assert "10:30-11:00" not in offered
    assert "11:00-11:30" in offered


def test_outage_never_reports_free_time(conn, linked, fake_google):
    fake_google.fail_busy = True
    out = say(conn, GUEST, f"free {OWNER} today")
    assert "Cannot read Burhan's connected Google Calendar" in out.reply.text
    assert out.reply.rows == []


def test_outage_blocks_the_booking_too(conn, linked, fake_google):
    rows = say(conn, GUEST, f"free {OWNER} today").reply.rows
    fake_google.fail_busy = True
    out = say(conn, GUEST, rows[0].id, tap=True)
    assert "Cannot read" in out.reply.text
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0


def test_booking_is_mirrored_and_the_id_is_stored(conn, linked, fake_google):
    rows = say(conn, GUEST, f"free {OWNER} today").reply.rows
    out = say(conn, GUEST, rows[0].id, tap=True)
    ref = out.reply.text.split("Ref ")[1][:6]

    assert len(fake_google.created) == 1
    assert fake_google.created[0]["title"] == "Ahmed / Burhan"
    assert db.get_event(conn, ref)["external_id"] == "gcal-1"
    assert "could not be written" not in out.notify[0][1].text


def test_cancel_deletes_the_mirrored_event(conn, linked, fake_google):
    rows = say(conn, GUEST, f"free {OWNER} today").reply.rows
    ref = say(conn, GUEST, rows[0].id, tap=True).reply.text.split("Ref ")[1][:6]

    out = say(conn, OWNER, f"cancel {ref}")
    # The stored Google id must be used -- not the local ref.
    assert fake_google.deleted == ["gcal-1"]
    assert "could not be removed" not in out.reply.text


def test_failed_delete_is_reported_not_hidden(conn, linked, fake_google):
    rows = say(conn, GUEST, f"free {OWNER} today").reply.rows
    ref = say(conn, GUEST, rows[0].id, tap=True).reply.text.split("Ref ")[1][:6]

    fake_google.fail_delete = True
    out = say(conn, OWNER, f"cancel {ref}")
    assert "could not be removed" in out.reply.text
    assert db.get_event(conn, ref)["status"] == "cancelled"


def test_failed_mirror_keeps_the_booking_and_warns_the_owner(conn, linked,
                                                            fake_google):
    fake_google.fail_create = True
    rows = say(conn, GUEST, f"free {OWNER} today").reply.rows
    out = say(conn, GUEST, rows[0].id, tap=True)
    ref = out.reply.text.split("Ref ")[1][:6]

    assert db.get_event(conn, ref)["status"] == "confirmed"
    assert db.get_event(conn, ref)["external_id"] is None
    assert "could not be written" in out.notify[0][1].text
    # The customer's own confirmation stays clean.
    assert "could not be written" not in out.reply.text


def test_refreshed_token_is_persisted(conn, linked, fake_google):
    from wacal import crypto

    owner, _ = linked
    fake_google.refresh_to = '{"token": "refreshed"}'
    say(conn, GUEST, f"free {OWNER} today")

    link = db.get_calendar_link(conn, owner["id"])
    assert db.token_of(link) == '{"token": "refreshed"}'
    # And it is not sitting in the database in the clear.
    assert crypto.is_encrypted(link["token_json"])
    assert "refreshed" not in link["token_json"]


def test_blocking_own_time_respects_google(conn, people, fake_google):
    owner, _ = people
    db.save_calendar_link(conn, owner["id"], TOKEN)
    fake_google.busy_intervals = [
        ("2026-10-15T11:00:00Z", "2026-10-15T12:00:00Z")  # 14:00-15:00 Riyadh
    ]
    out = say(conn, OWNER, "block today 14:00-15:00 Focus")
    assert "overlaps something already on your calendar" in out.reply.text


def test_connect_and_disconnect(conn, people, fake_google, monkeypatch):
    owner, _ = people
    monkeypatch.setattr(gcal, "auth_url", lambda state: f"https://accounts.test/{state}")
    out = say(conn, OWNER, "connect google")
    assert "https://accounts.test/" in out.reply.text

    db.save_calendar_link(conn, owner["id"], TOKEN, account="b@example.com")
    assert "google b@example.com" in say(conn, OWNER, "me").reply.text

    assert "disconnected" in say(conn, OWNER, "disconnect google").reply.text
    assert db.get_calendar_link(conn, owner["id"]) is None
    assert "not connected" in say(conn, OWNER, "me").reply.text


def test_connect_without_client_secrets_is_explicit(conn, people, monkeypatch):
    monkeypatch.setattr(gcal, "CLIENT_SECRETS_FILE", "./definitely-missing.json")
    out = say(conn, OWNER, "connect google")
    assert "not set up on this server" in out.reply.text


# --- state tokens --------------------------------------------------------


def test_state_token_round_trip(monkeypatch):
    monkeypatch.setenv("WACAL_STATE_SECRET", "s3cret")
    token = gcal.sign_state(42, "nonce-1")
    assert gcal.verify_state(token) == (42, "nonce-1")


def test_tampered_state_is_rejected(monkeypatch):
    monkeypatch.setenv("WACAL_STATE_SECRET", "s3cret")
    token = gcal.sign_state(42, "nonce-1")
    forged = gcal.sign_state(43, "nonce-2")
    # Same secret, different user: swapping the body invalidates the signature.
    mixed = forged[: len(forged) // 2] + token[len(token) // 2:]
    with pytest.raises(ValueError):
        gcal.verify_state(mixed)


def test_state_from_another_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("WACAL_STATE_SECRET", "secret-a")
    token = gcal.sign_state(42, "nonce-1")
    monkeypatch.setenv("WACAL_STATE_SECRET", "secret-b")
    with pytest.raises(ValueError):
        gcal.verify_state(token)


def test_state_expires(monkeypatch):
    monkeypatch.setenv("WACAL_STATE_SECRET", "s3cret")
    issued = time.time() - gcal.STATE_TTL_SECONDS - 1
    token = gcal.sign_state(42, "nonce-1", now=issued)
    with pytest.raises(ValueError, match="expired"):
        gcal.verify_state(token)


def test_redirect_uri_follows_base_url(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://book.example.com/")
    assert gcal.redirect_uri() == "https://book.example.com/auth/google/callback"


def test_naive_external_intervals_are_treated_as_utc():
    from wacal.busy import _norm

    assert _norm("2026-10-15T07:00:00Z") == "2026-10-15T07:00:00+00:00"
    assert _norm("2026-10-15T07:00:00") == "2026-10-15T07:00:00+00:00"
    assert _norm("2026-10-15T10:00:00+03:00") == "2026-10-15T07:00:00+00:00"


def test_external_error_surfaces_from_busy_for(conn, people, fake_google):
    owner, _ = people
    db.save_calendar_link(conn, owner["id"], TOKEN)
    fake_google.fail_busy = True
    from wacal.busy import busy_for

    with pytest.raises(ExternalCalendarError):
        busy_for(conn, owner["id"], NOW, NOW)
