"""Rate limiting, and the background outbox drain."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from wacal import db, main, ratelimit
from wacal.whatsapp import MemoryTransport

PHONE = "+966500000002"


# --- the limiter itself --------------------------------------------------


def test_a_burst_is_refused_once_then_silently():
    limiter = ratelimit.RateLimiter(burst=3, burst_seconds=20, sustained=0)
    now = 1000.0

    assert limiter.check(PHONE, now=now) is None
    assert limiter.check(PHONE, now=now) is None
    assert limiter.check(PHONE, now=now) is None

    first = limiter.check(PHONE, now=now)
    assert first and "Too many messages" in first
    # Replying to every message of a flood is itself a flood.
    assert limiter.check(PHONE, now=now) == ""
    assert limiter.check(PHONE, now=now) == ""


def test_the_window_slides():
    limiter = ratelimit.RateLimiter(burst=2, burst_seconds=10, sustained=0)
    assert limiter.check(PHONE, now=0) is None
    assert limiter.check(PHONE, now=1) is None
    assert limiter.check(PHONE, now=2) != None  # noqa: E711 - refused

    # Past the window, allowed again.
    assert limiter.check(PHONE, now=60) is None


def test_hammering_extends_the_block():
    limiter = ratelimit.RateLimiter(burst=2, burst_seconds=10, sustained=0)
    limiter.check(PHONE, now=0)
    limiter.check(PHONE, now=1)
    for moment in range(2, 12):
        limiter.check(PHONE, now=moment)
    # The refused attempts count, so at t=12 the window is still full.
    assert limiter.check(PHONE, now=12) is not None


def test_the_sustained_window_catches_a_slow_flood():
    limiter = ratelimit.RateLimiter(burst=100, burst_seconds=1,
                                    sustained=5, sustained_seconds=600)
    for index in range(5):
        assert limiter.check(PHONE, now=index * 30) is None
    refusal = limiter.check(PHONE, now=200)
    assert refusal and "a lot of messages" in refusal


def test_senders_are_independent():
    limiter = ratelimit.RateLimiter(burst=1, burst_seconds=10, sustained=0)
    assert limiter.check("+1", now=0) is None
    assert limiter.check("+2", now=0) is None
    assert limiter.check("+1", now=0) is not None


def test_it_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WACAL_RATE_BURST", "0")
    monkeypatch.setenv("WACAL_RATE_SUSTAINED", "0")
    limiter = ratelimit.from_environment()
    assert limiter.enabled is False
    for _ in range(100):
        assert limiter.check(PHONE) is None


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("WACAL_RATE_BURST", "3")
    monkeypatch.setenv("WACAL_RATE_BURST_SECONDS", "7")
    monkeypatch.setenv("WACAL_RATE_SUSTAINED", "nonsense")
    limiter = ratelimit.from_environment()
    assert (limiter.burst, limiter.burst_seconds) == (3, 7)
    assert limiter.sustained == 120  # unparseable falls back to the default


def test_reset_clears_one_key_or_all():
    limiter = ratelimit.RateLimiter(burst=1, burst_seconds=10, sustained=0)
    limiter.check("+1", now=0)
    limiter.check("+2", now=0)
    limiter.reset("+1")
    assert limiter.check("+1", now=0) is None
    assert limiter.check("+2", now=0) is not None
    limiter.reset()
    assert limiter.check("+2", now=0) is None


# --- through the webhook -------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "APP_SECRET", "")
    monkeypatch.setenv("WACAL_ALLOW_UNSIGNED", "1")
    monkeypatch.setattr(main, "limiter",
                        ratelimit.RateLimiter(burst=3, burst_seconds=20,
                                              sustained=0))
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    db.create_user(main.app.state.conn, PHONE, "Ahmed", "Asia/Riyadh")
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def body(text: str, message_id: str):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": PHONE, "profile": {"name": "Ahmed"}}],
        "messages": [{"from": PHONE, "id": message_id, "type": "text",
                      "text": {"body": text}}],
    }}]}]}


def test_a_flood_gets_one_warning_then_nothing(client):
    for index in range(3):
        assert client.post("/webhook", json=body("help", f"m{index}")
                           ).status_code == 200
    sent = main.app.state.transport.sent
    assert len(sent) == 3

    # Fourth is refused with an explanation.
    client.post("/webhook", json=body("help", "m3"))
    assert "Too many messages" in sent[-1][1].text
    before = len(sent)

    # Everything after that is dropped quietly.
    for index in range(4, 10):
        client.post("/webhook", json=body("help", f"m{index}"))
    assert len(sent) == before


def test_a_throttled_message_is_not_redelivered_forever(client):
    for index in range(6):
        client.post("/webhook", json=body("help", f"m{index}"))
    seen = main.app.state.conn.execute(
        "SELECT count(*) AS n FROM seen_messages").fetchone()["n"]
    # Every message, throttled or not, is recorded so the provider stops.
    assert seen == 6


def test_a_throttled_sender_does_not_block_another(client):
    for index in range(5):
        client.post("/webhook", json=body("help", f"m{index}"))

    other = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "+966500000003", "profile": {"name": "Sara"}}],
        "messages": [{"from": "+966500000003", "id": "other1", "type": "text",
                      "text": {"body": "help"}}],
    }}]}]}
    client.post("/webhook", json=other)
    to, reply = main.app.state.transport.sent[-1]
    assert to == "+966500000003"
    assert "free <name|phone>" in reply.text


# --- the background drain ------------------------------------------------


def test_the_drainer_retries_what_a_failed_send_left_behind(client,
                                                            monkeypatch):
    transport = main.app.state.transport
    conn = main.app.state.conn
    attempts = {"n": 0}

    async def flaky(to, reply, *, force_template=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("provider down")
        transport.sent.append((to, reply))
        return {}

    monkeypatch.setattr(transport, "send", flaky)
    client.post("/webhook", json=body("help", "drain1"))

    pending = conn.execute(
        "SELECT * FROM outbox WHERE sent_at IS NULL").fetchall()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1

    # What the background task does on its tick.
    assert asyncio.run(main.drain_outbox(conn, transport)) == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL"
    ).fetchone()["n"] == 0


def test_the_drain_interval_is_configurable(monkeypatch):
    import importlib

    monkeypatch.setenv("WACAL_DRAIN_SECONDS", "15")
    reloaded = importlib.reload(main)
    try:
        assert reloaded.DRAIN_SECONDS == 15
        monkeypatch.setenv("WACAL_DRAIN_SECONDS", "1")
        # Floored, so a misconfiguration cannot spin the loop.
        assert importlib.reload(main).DRAIN_SECONDS == 5
    finally:
        monkeypatch.delenv("WACAL_DRAIN_SECONDS", raising=False)
        importlib.reload(main)


def test_the_drainer_survives_a_send_that_keeps_failing(client, monkeypatch):
    transport = main.app.state.transport
    conn = main.app.state.conn

    async def always_fails(to, reply):
        raise RuntimeError("still down")

    monkeypatch.setattr(transport, "send", always_fails)
    client.post("/webhook", json=body("help", "drain2"))

    for _ in range(3):
        assert asyncio.run(main.drain_outbox(conn, transport)) == 0
    row = conn.execute("SELECT * FROM outbox").fetchone()
    # Still pending, with the attempt count as the record of what happened.
    assert row["sent_at"] is None
    assert row["attempts"] == 4
