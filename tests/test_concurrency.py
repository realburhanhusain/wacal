"""Concurrency proof for the double-booking guard.

The existing double-tap test is sequential: it exercises the re-check, not the
lock. These drive the real HTTP path from several threads at once, which is the
only arrangement that can interleave two requests between the "is it free?"
read and the INSERT.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from wacal import db, main
from wacal.whatsapp import MemoryTransport

SECRET = "test-secret"
SERVICES = [{"name": "Consultation", "duration_mins": 30, "price": 150}]
OWNER = "+966500000001"


class LockingTransport(MemoryTransport):
    """MemoryTransport with a lock, since several threads append at once."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    async def send(self, to, reply):
        with self._lock:
            self.sent.append((to, reply))
        return {}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "APP_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_KEY", "k")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = LockingTransport()
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def signed(client, path: str, body: dict):
    raw = json.dumps(body).encode()
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(path, content=raw, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    })


def message(text: str, message_id: str, phone: str, name: str = "Ravi"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": phone, "profile": {"name": name}}],
        "messages": [{"from": phone, "id": message_id, "type": "text",
                      "text": {"body": text}}],
    }}]}]}


def confirmed(conn) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM events WHERE status = 'confirmed'"
    ).fetchone()["n"]


@pytest.fixture()
def clinic(client):
    conn = main.app.state.conn
    owner = db.create_user(conn, OWNER, "Dr Sharma", "Asia/Riyadh")
    db.create_host(conn, "clinic", owner["id"], "Clinic",
                   services=json.dumps(SERVICES), work_start="09:00",
                   work_end="17:00", work_days="0,1,2,3,4,5,6",
                   slot_minutes=30)
    return conn


def walk_to_confirm_button(client, phone: str, prefix: str) -> str:
    """Drive one customer as far as the Confirm row and return its id."""
    transport = main.app.state.transport
    signed(client, "/webhook/clinic", message("hi", f"{prefix}-1", phone))
    service = transport.sent[-1][1].rows[0].id
    signed(client, "/webhook/clinic", message(service, f"{prefix}-2", phone))
    day = transport.sent[-1][1].rows[0].id
    signed(client, "/webhook/clinic", message(day, f"{prefix}-3", phone))
    slot = transport.sent[-1][1].rows[0].id
    signed(client, "/webhook/clinic", message(slot, f"{prefix}-4", phone))
    return transport.sent[-1][1].rows[0].id


def test_two_customers_confirming_the_same_slot_produce_one_booking(clinic,
                                                                   client):
    """Two racing confirmations, one booking.

    Verified with tests/mutation_check.py: this particular test still passes
    with the lock removed, because two threads rarely interleave inside the
    narrow window. The eight-thread test below is the one that actually fails
    without the lock. Keep both: this is the readable case, that is the proof.
    """
    first = "+966500000055"
    second = "+966500000066"
    first_button = walk_to_confirm_button(client, first, "a")
    second_button = walk_to_confirm_button(client, second, "b")
    # Both hold a Confirm button for the same instant.
    assert first_button.rsplit(":", 1)[-1] == second_button.rsplit(":", 1)[-1]

    start = threading.Barrier(2)

    def confirm(phone: str, button: str, message_id: str):
        start.wait(timeout=5)
        return signed(client, "/webhook/clinic",
                      message(button, message_id, phone)).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(
            lambda args: confirm(*args),
            [(first, first_button, "a-5"), (second, second_button, "b-5")],
        ))

    assert codes == [200, 200]
    assert confirmed(clinic) == 1
    # The loser is told, not silently ignored.
    texts = " ".join(reply.text for _, reply in main.app.state.transport.sent)
    assert "Confirmed." in texts
    assert ("taken while you were deciding" in texts
            or "just booked by someone else" in texts)


def test_eight_threads_on_one_slot_still_book_once(clinic, client):
    phones = [f"+96650000{index:04d}" for index in range(10, 18)]
    buttons = {phone: walk_to_confirm_button(client, phone, f"p{index}")
               for index, phone in enumerate(phones)}

    start = threading.Barrier(len(phones))

    def confirm(phone: str):
        start.wait(timeout=10)
        return signed(client, "/webhook/clinic",
                      message(buttons[phone], f"{phone}-go", phone)).status_code

    with ThreadPoolExecutor(max_workers=len(phones)) as pool:
        codes = list(pool.map(confirm, phones))

    assert set(codes) == {200}
    assert confirmed(clinic) == 1


def test_the_same_message_id_from_two_threads_is_handled_once(clinic, client):
    """Meta can redeliver concurrently, not just sequentially."""
    phone = "+966500000099"
    body = message("hi", "same-id", phone)
    start = threading.Barrier(4)

    def post(_):
        start.wait(timeout=5)
        return signed(client, "/webhook/clinic", body).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(post, range(4)))

    assert set(codes) == {200}
    seen = main.app.state.conn.execute(
        "SELECT count(*) AS n FROM seen_messages WHERE message_id = 'same-id'"
    ).fetchone()["n"]
    assert seen == 1
    # One inbound message, one reply queued.
    assert main.app.state.conn.execute(
        "SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1


def test_the_index_alone_stops_a_double_book(request):
    """The partial unique index must hold even with the lock out of the way.

    Two separate connections to the same file, writing the same owner and
    start. No threads: the lock is what makes a threaded version serialise, so
    a threaded version proves nothing and was order-dependent in the suite.
    What is under test is the backstop -- that SQLite's violation is recognised
    and raised as SlotTaken instead of leaking sqlite3.IntegrityError.
    """
    from datetime import datetime, timedelta, timezone

    path = pathlib.Path(__file__).resolve().parent / "race.db"
    for leftover in path.parent.glob("race.db*"):
        leftover.unlink(missing_ok=True)
    request.addfinalizer(
        lambda: [p.unlink(missing_ok=True) for p in path.parent.glob("race.db*")]
    )

    first = db.connect(path)
    second = db.connect(path)
    request.addfinalizer(first.close)
    request.addfinalizer(second.close)

    owner = db.create_user(first, OWNER, "Dr Sharma", "Asia/Riyadh")
    start_at = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    end_at = start_at + timedelta(minutes=30)

    db.create_event(first, owner["id"], owner["id"], start_at, end_at, "first")
    with pytest.raises(db.SlotTaken):
        db.create_event(second, owner["id"], owner["id"], start_at, end_at,
                        "second")
    assert confirmed(second) == 1
