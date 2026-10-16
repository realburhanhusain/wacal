"""One-time codes, and the host registration they guard."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from wacal import db, main, otp
from wacal.whatsapp import MemoryTransport

from .conftest import OWNER, RIYADH

OTP_TEMPLATE = "wacal_otp"
TYPED = "+966500000077"


@pytest.fixture()
def api(monkeypatch):
    """The admin API, with a real database and the in-memory transport."""
    monkeypatch.setenv("ADMIN_KEY", "k")
    monkeypatch.setenv("WA_TEMPLATE_OTP", OTP_TEMPLATE)
    monkeypatch.setattr(main, "APP_SECRET", "secret")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    with TestClient(main.app) as client:
        yield client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


# --- the code itself -----------------------------------------------------


def test_a_code_is_six_digits_and_zero_padded():
    seen = {otp.generate_code() for _ in range(200)}
    assert all(len(code) == 6 and code.isdigit() for code in seen)
    # 200 draws from a million values should not collide into a handful.
    assert len(seen) > 150


def test_the_plain_code_is_never_stored(conn):
    code, _reply = otp.start(conn, TYPED)
    row = db.get_otp(conn, TYPED)
    assert code not in row["code_hash"]
    assert row["code_hash"] != code


def test_a_digest_is_bound_to_its_phone_number(conn):
    """Copying a row onto another number must not verify there."""
    code, _reply = otp.start(conn, TYPED)
    assert otp.hash_code("+966500000099", code) != otp.hash_code(TYPED, code)


def test_the_right_code_verifies_once(conn):
    code, _reply = otp.start(conn, TYPED)
    assert otp.check(conn, TYPED, code).ok is True
    assert db.is_phone_verified(conn, TYPED) is True
    # The challenge is consumed, so a replay of the same code fails.
    assert otp.check(conn, TYPED, code).ok is False


# The literal, deliberately not otp.MAX_ATTEMPTS: a test that reads the
# constant it is checking passes however high the constant is raised.
ALLOWED_GUESSES = 5


def test_the_attempt_cap_is_five(conn):
    assert otp.MAX_ATTEMPTS == ALLOWED_GUESSES


def test_a_wrong_code_is_counted_and_the_challenge_dies(conn):
    _code, _reply = otp.start(conn, TYPED)
    for _ in range(ALLOWED_GUESSES):
        assert otp.check(conn, TYPED, "000000").ok is False
    assert db.get_otp(conn, TYPED) is None
    assert db.is_phone_verified(conn, TYPED) is False


def test_guessing_cannot_outlive_the_attempt_cap(conn):
    """The cap is the whole defence: six digits is seconds of guessing."""
    code, _reply = otp.start(conn, TYPED)
    wrong = "111111" if code != "111111" else "222222"
    for _ in range(ALLOWED_GUESSES):
        otp.check(conn, TYPED, wrong)
    # Even the correct code no longer works; a new one must be requested.
    assert otp.check(conn, TYPED, code).ok is False


def test_an_expired_code_fails(conn):
    code, _reply = otp.start(conn, TYPED)
    later = db.now_utc() + timedelta(minutes=otp.TTL_MINUTES + 1)
    verdict = otp.check(conn, TYPED, code, now=later)
    assert verdict.ok is False
    assert "expired" in verdict.reason


def test_a_resend_is_refused_until_the_cooldown_passes(conn):
    otp.start(conn, TYPED)
    with pytest.raises(otp.TooSoon):
        otp.start(conn, TYPED)

    later = db.now_utc() + timedelta(seconds=otp.RESEND_SECONDS + 1)
    code, _reply = otp.start(conn, TYPED, now=later)
    assert otp.check(conn, TYPED, code, now=later).ok is True


def test_a_new_code_replaces_the_old_one(conn):
    first, _ = otp.start(conn, TYPED)
    later = db.now_utc() + timedelta(seconds=otp.RESEND_SECONDS + 1)
    second, _ = otp.start(conn, TYPED, now=later)

    assert otp.check(conn, TYPED, first, now=later).ok is False
    # The failed attempt above counted against the live challenge, not a
    # stale one, so the new code still works.
    assert otp.check(conn, TYPED, second, now=later).ok is True


def test_an_expired_code_is_pruned(conn):
    otp.start(conn, TYPED)
    conn.execute("UPDATE otp_codes SET expires_at = ?",
                 (db.iso(db.now_utc() - timedelta(minutes=1)),))
    assert db.prune(conn)["otp_codes"] == 1


def test_the_otp_message_carries_the_code_twice(monkeypatch):
    """An authentication template repeats the code on its copy button."""
    monkeypatch.setenv("WA_TEMPLATE_OTP", OTP_TEMPLATE)
    reply = otp.otp_reply("123456")
    assert "123456" in reply.text
    assert reply.template.body_params == ("123456",)
    assert reply.template.button_params == ("123456",)


# --- the admin routes ----------------------------------------------------


def test_verify_start_sends_a_code_and_confirm_accepts_it(api):
    conn = main.app.state.conn
    started = api.post("/dashboard/verify/start", json={"phone": TYPED},
                       headers={"x-api-key": "k"})
    assert started.status_code == 200
    assert started.json()["sent"] is True

    sent_to, reply = main.app.state.transport.sent[-1]
    assert sent_to == TYPED
    code = reply.template.body_params[0]

    confirmed = api.post("/dashboard/verify/confirm",
                         json={"phone": TYPED, "code": code},
                         headers={"x-api-key": "k"})
    assert confirmed.status_code == 200
    assert db.is_phone_verified(conn, TYPED) is True


def test_a_delivered_code_is_scrubbed_from_the_outbox(api):
    """The code is hashed in otp_codes but the message carried it in clear."""
    conn = main.app.state.conn
    api.post("/dashboard/verify/start", json={"phone": TYPED},
             headers={"x-api-key": "k"})
    code = main.app.state.transport.sent[-1][1].template.body_params[0]

    rows = conn.execute("SELECT payload, sent_at FROM outbox").fetchall()
    assert rows and rows[0]["sent_at"] is not None
    assert code not in rows[0]["payload"]


def test_an_undelivered_code_is_kept_so_it_can_be_retried(api, monkeypatch):
    """Redaction is for a sent message; scrubbing a pending one loses it."""
    monkeypatch.delenv("WA_TEMPLATE_OTP", raising=False)
    monkeypatch.delenv("WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW", raising=False)
    conn = main.app.state.conn
    api.post("/dashboard/verify/start", json={"phone": TYPED},
             headers={"x-api-key": "k"})

    row = conn.execute("SELECT payload, sent_at FROM outbox").fetchone()
    assert row["sent_at"] is None
    assert "redacted" not in row["payload"]


def test_verify_confirm_rejects_a_wrong_code(api):
    api.post("/dashboard/verify/start", json={"phone": TYPED},
             headers={"x-api-key": "k"})
    bad = api.post("/dashboard/verify/confirm",
                   json={"phone": TYPED, "code": "000000"},
                   headers={"x-api-key": "k"})
    assert bad.status_code == 400


def test_a_resend_is_rate_limited_to_429(api):
    headers = {"x-api-key": "k"}
    api.post("/dashboard/verify/start", json={"phone": TYPED}, headers=headers)
    again = api.post("/dashboard/verify/start", json={"phone": TYPED},
                     headers=headers)
    assert again.status_code == 429


def test_the_verify_routes_need_the_admin_key(api):
    assert api.post("/dashboard/verify/start",
                    json={"phone": TYPED}).status_code == 401
    assert api.post("/dashboard/verify/confirm",
                    json={"phone": TYPED, "code": "1"}).status_code == 422


def test_register_refuses_an_unverified_owner_when_otp_is_required(
        api, monkeypatch):
    """A typo in owner_phone sends a stranger other people's bookings."""
    monkeypatch.setenv("WACAL_REQUIRE_OTP", "1")
    refused = api.post("/dashboard/register", headers={"x-api-key": "k"},
                       json={"slug": "clinic", "business_name": "Clinic",
                             "owner_phone": TYPED})
    assert refused.status_code == 403
    assert "not verified" in refused.json()["detail"]


def test_register_accepts_a_verified_owner(api, monkeypatch):
    monkeypatch.setenv("WACAL_REQUIRE_OTP", "1")
    headers = {"x-api-key": "k"}
    api.post("/dashboard/verify/start", json={"phone": TYPED}, headers=headers)
    code = main.app.state.transport.sent[-1][1].template.body_params[0]
    api.post("/dashboard/verify/confirm", json={"phone": TYPED, "code": code},
             headers=headers)

    created = api.post("/dashboard/register", headers=headers,
                       json={"slug": "clinic", "business_name": "Clinic",
                             "owner_phone": TYPED})
    assert created.status_code == 201


def test_register_is_unchanged_when_otp_is_not_required(api, monkeypatch):
    monkeypatch.delenv("WACAL_REQUIRE_OTP", raising=False)
    created = api.post("/dashboard/register", headers={"x-api-key": "k"},
                       json={"slug": "clinic2", "business_name": "Clinic",
                             "owner_phone": TYPED})
    assert created.status_code == 201


def test_an_otp_that_cannot_be_delivered_is_reported_as_unsent(api,
                                                               monkeypatch):
    """No template and a cold number means the code reaches nobody."""
    monkeypatch.delenv("WA_TEMPLATE_OTP", raising=False)
    monkeypatch.delenv("WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW", raising=False)
    started = api.post("/dashboard/verify/start", json={"phone": TYPED},
                       headers={"x-api-key": "k"})
    body = started.json()
    assert body["sent"] is False
    assert "WA_TEMPLATE_OTP" in body["error"]


def test_an_inbound_message_opens_the_window_for_that_number(api):
    """The webhook is what makes a later notification deliverable at all."""
    conn = main.app.state.conn
    db.create_user(conn, OWNER, "Burhan", RIYADH)
    assert db.within_window(conn, OWNER) is False

    assert _post_webhook(api, "help", "wamid.otp1").status_code == 200
    assert db.within_window(conn, OWNER) is True


def _post_webhook(client, text: str, message_id: str):
    """Signed over the exact bytes posted, which is what main.py verifies."""
    import hashlib
    import hmac
    import json

    body = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": OWNER.lstrip("+"),
                      "profile": {"name": "Burhan"}}],
        "messages": [{"from": OWNER.lstrip("+"), "id": message_id,
                      "type": "text", "text": {"body": text}}],
    }}]}]}
    raw = json.dumps(body).encode()
    digest = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=raw, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    })
