"""Regression tests for the defects found in the two code reviews.

Each test names the behaviour that was wrong before, so a future change that
reintroduces it fails here rather than in production.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wacal import db, gcal, main, numbering
from wacal.business import handle_business
from wacal.slots import ambiguous_day, fmt_slot, local_date
from wacal.whatsapp import (
    Inbound,
    MemoryTransport,
    Reply,
    Row,
    normalise_phone,
    twilio_signature,
)

from .conftest import GUEST, NOW, OWNER, RIYADH, say

SERVICES = [{"name": "Consultation", "duration_mins": 30, "price": 150}]
CUSTOMER = "+966500000055"
TOKEN = "twilio-auth-token"


# --- unverifiable webhooks fail closed -----------------------------------


@pytest.fixture()
def meta_only(monkeypatch):
    """A Meta-only deployment: Cloud secret set, Twilio secret absent."""
    monkeypatch.setattr(main, "APP_SECRET", "real-meta-secret")
    monkeypatch.setattr(main, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.delenv("WACAL_ALLOW_UNSIGNED", raising=False)
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    db.create_user(main.app.state.conn, "+966500000009", "Victim", RIYADH)
    db.create_user(main.app.state.conn, "+966500000010", "Attacker", RIYADH)
    with TestClient(main.app) as client:
        yield client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def test_unsigned_twilio_cannot_impersonate_on_a_meta_deployment(meta_only):
    """Both transports' routes are mounted, so the unused one must be shut.

    This was exploitable: POST From=<victim> Body="share <attacker>" with no
    signature was believed, and granted the attacker the victim's calendar.
    """
    response = meta_only.post("/twilio/webhook", data={
        "From": "whatsapp:+966500000009",
        "Body": "share +966500000010",
        "MessageSid": "SPOOF1",
    })
    assert response.status_code == 403
    assert "signing secret is not configured" in response.json()["detail"]
    conn = main.app.state.conn
    assert conn.execute("SELECT count(*) AS n FROM grants").fetchone()["n"] == 0
    assert main.app.state.transport.sent == []


def test_the_escape_hatch_is_explicit(meta_only, monkeypatch):
    monkeypatch.setenv("WACAL_ALLOW_UNSIGNED", "1")
    response = meta_only.post("/twilio/webhook", data={
        "From": "whatsapp:+966500000009", "Body": "help", "MessageSid": "OK1",
    })
    assert response.status_code == 200


def test_twilio_public_url_is_a_base_not_a_whole_url(monkeypatch):
    """Substituting the whole URL made every route but one fail to verify."""
    monkeypatch.setattr(main, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_PUBLIC_URL", "https://pub.example.com/twilio/webhook")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    owner = db.create_user(main.app.state.conn, "+966500000001", "Clinic", RIYADH)
    db.create_host(main.app.state.conn, "salon", owner["id"], "Salon",
                   services=json.dumps(SERVICES), work_days="0,1,2,3,4,5,6")
    try:
        with TestClient(main.app) as client:
            params = {"From": "whatsapp:+966500000055", "Body": "hi",
                      "MessageSid": "SM1", "ProfileName": "Ravi"}
            signed = twilio_signature(
                "https://pub.example.com/twilio/webhook/salon", params, TOKEN)
            response = client.post("/twilio/webhook/salon", data=params,
                                   headers={"X-Twilio-Signature": signed})
            assert response.status_code == 200
    finally:
        main.app.state.conn.close()
        main.app.state.conn = None
        main.app.state.transport = None


# --- OAuth state ---------------------------------------------------------


def test_no_state_secret_means_no_signing(monkeypatch):
    """A published default key would let anyone mint a state for any user."""
    monkeypatch.delenv("WACAL_STATE_SECRET", raising=False)
    monkeypatch.delenv("WA_APP_SECRET", raising=False)
    monkeypatch.delenv("WACAL_ALLOW_UNSIGNED", raising=False)
    with pytest.raises(gcal.NotConfigured):
        gcal.sign_state(1, "nonce")


def test_state_tokens_are_single_use(conn):
    user = db.create_user(conn, OWNER, "Burhan", RIYADH)
    nonce = gcal.new_nonce()
    db.create_oauth_state(conn, nonce, user["id"])
    assert db.consume_oauth_state(conn, nonce, user["id"]) is True
    assert db.consume_oauth_state(conn, nonce, user["id"]) is False


def test_a_state_we_never_issued_cannot_be_spent(conn):
    user = db.create_user(conn, OWNER, "Burhan", RIYADH)
    assert db.consume_oauth_state(conn, gcal.new_nonce(), user["id"]) is False


def test_a_state_for_another_user_cannot_be_spent(conn):
    first = db.create_user(conn, OWNER, "Burhan", RIYADH)
    second = db.create_user(conn, GUEST, "Ahmed", RIYADH)
    nonce = gcal.new_nonce()
    db.create_oauth_state(conn, nonce, first["id"])
    assert db.consume_oauth_state(conn, nonce, second["id"]) is False


def test_connect_records_a_nonce(conn, people, monkeypatch):
    owner, _ = people
    monkeypatch.setattr(gcal, "auth_url", lambda state: f"https://t/{state}")
    say(conn, OWNER, "connect google")
    row = conn.execute("SELECT * FROM oauth_states").fetchone()
    assert row["user_id"] == owner["id"]


# --- double booking and refs --------------------------------------------


def test_slot_taken_is_raised_not_leaked(conn):
    """SQLite names the columns, not the index, in a unique violation."""
    user = db.create_user(conn, OWNER, "Burhan", RIYADH)
    start = datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    db.create_event(conn, user["id"], user["id"], start, end, "A")
    with pytest.raises(db.SlotTaken):
        db.create_event(conn, user["id"], user["id"], start, end, "B")


def test_a_ref_collision_is_retried(conn, monkeypatch):
    user = db.create_user(conn, OWNER, "Burhan", RIYADH)
    start = datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)
    first = db.create_event(conn, user["id"], user["id"], start,
                            start + timedelta(minutes=30), "A")

    refs = iter([first["ref"], "FRESH1"])
    monkeypatch.setattr(db, "new_ref", lambda: next(refs))
    second = db.create_event(conn, user["id"], user["id"],
                             start + timedelta(hours=1),
                             start + timedelta(hours=1, minutes=30), "B")
    assert second["ref"] == "FRESH1"


def test_a_digit_free_ref_is_still_recognised(conn):
    """The alphabet is A-Z0-9, so a ref need not contain a digit at all."""
    owner = db.create_user(conn, OWNER, "Clinic", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    host = db.create_host(conn, "c", owner["id"], "Clinic",
                          services=json.dumps(SERVICES),
                          work_days="0,1,2,3,4,5,6")
    customer = db.create_user(conn, CUSTOMER, "Ravi", RIYADH)
    start = NOW + timedelta(days=1)
    conn.execute(
        "INSERT INTO events (ref, owner_id, created_by, start_utc, end_utc,"
        " title, created_at, host_slug, service)"
        " VALUES ('ABCDEF', ?, ?, ?, ?, 'x', ?, 'c', 'Consultation')",
        (owner["id"], customer["id"], db.iso(start),
         db.iso(start + timedelta(minutes=30)), db.iso(NOW)),
    )
    out = handle_business(conn, host, Inbound(from_phone=CUSTOMER,
                                              text="cancel ABCDEF",
                                              message_id="m1"), now=NOW)
    assert "Cancelled ABCDEF" in out.reply.text


def test_a_word_after_cancel_is_not_read_as_a_ref(conn):
    owner = db.create_user(conn, OWNER, "Clinic", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    host = db.create_host(conn, "c", owner["id"], "Clinic",
                          services=json.dumps(SERVICES))
    db.create_user(conn, CUSTOMER, "Ravi", RIYADH)
    out = handle_business(conn, host, Inbound(from_phone=CUSTOMER,
                                              text="cancel please",
                                              message_id="m1"), now=NOW)
    assert "No booking with ref" not in out.reply.text


# --- dispatch robustness -------------------------------------------------


@pytest.fixture()
def dispatch_client(monkeypatch):
    monkeypatch.setattr(main, "APP_SECRET", "")
    monkeypatch.setattr(main, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("WACAL_ALLOW_UNSIGNED", "1")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def cloud_body(text: str, message_id: str, phone: str = "+966500000002"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": phone, "profile": {"name": "Ahmed"}}],
        "messages": [{"from": phone, "id": message_id, "type": "text",
                      "text": {"body": text}}],
    }}]}]}


def test_a_non_ascii_digit_does_not_crash_the_dispatch(dispatch_client):
    """"²".isdigit() is True but int("²") raises."""
    response = dispatch_client.post("/webhook", json=cloud_body("²", "wamid.1"))
    assert response.status_code == 200
    assert main.app.state.transport.sent


def test_a_failed_send_is_retried_and_the_booking_is_kept(dispatch_client,
                                                          monkeypatch):
    """A send failure used to skip the owner's notice and leave no trace."""
    transport = main.app.state.transport
    calls = {"n": 0}

    async def flaky(to, reply):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider down")
        transport.sent.append((to, reply))
        return {}

    monkeypatch.setattr(transport, "send", flaky)
    dispatch_client.post("/webhook", json=cloud_body("help", "wamid.2"))

    conn = main.app.state.conn
    pending = conn.execute(
        "SELECT * FROM outbox WHERE sent_at IS NULL").fetchall()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1

    # A later drain delivers it; nothing was lost.
    import asyncio

    sent = asyncio.run(main.drain_outbox(conn, transport))
    assert sent == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL"
    ).fetchone()["n"] == 0


def test_prune_clears_old_rows(conn):
    db.mark_seen(conn, "old-message")
    conn.execute("UPDATE seen_messages SET seen_at = '2020-01-01T00:00:00+00:00'")
    db.put_session(conn, "+1", "peer", "rows", "[]", ttl_seconds=-1)
    removed = db.prune(conn)
    assert removed["seen_messages"] == 1
    assert removed["sessions"] == 1


# --- scoped numbering ----------------------------------------------------


def test_one_customer_two_hosts_keep_separate_menus(conn):
    """Keyed by phone alone, "1" resolved against whichever menu came last."""
    numbering.remember(conn, CUSTOMER, "salon",
                       Reply(text="x", rows=[Row(id="b:slot:0:aaaa:111",
                                                 title="salon")]))
    numbering.remember(conn, CUSTOMER, "clinic",
                       Reply(text="x", rows=[Row(id="b:slot:0:bbbb:222",
                                                 title="clinic")]))
    salon = numbering.resolve(conn, Inbound(from_phone=CUSTOMER, text="1",
                                           message_id="m1"), "salon")
    clinic = numbering.resolve(conn, Inbound(from_phone=CUSTOMER, text="1",
                                            message_id="m2"), "clinic")
    assert salon.text == "b:slot:0:aaaa:111"
    assert clinic.text == "b:slot:0:bbbb:222"


# --- identity ------------------------------------------------------------


def test_phone_normalisation_is_one_shape():
    assert normalise_phone("966500000001") == "+966500000001"
    assert normalise_phone("whatsapp:+966500000001") == "+966500000001"
    assert normalise_phone("+966 50 000 0001") == "+966500000001"
    assert normalise_phone("(966) 50-000-0001") == "+966500000001"
    assert normalise_phone("") == ""


def test_old_bare_phone_rows_are_migrated(conn):
    conn.execute(
        "INSERT INTO users (phone, display_name, timezone, created_at)"
        " VALUES ('966500000077', 'Legacy', 'UTC', '2026-01-01T00:00:00+00:00')")
    applied = db.migrate(conn)
    assert any("users.phone" in step for step in applied)
    assert db.get_user_by_phone(conn, "+966500000077")["display_name"] == "Legacy"


def test_a_bare_name_registers_instead_of_looping(conn):
    out = say(conn, "+10000000001", "Ravi", name="")
    assert "Welcome Ravi" in out.reply.text
    assert db.get_user_by_phone(conn, "+10000000001")["display_name"] == "Ravi"


def test_a_command_word_is_never_taken_as_a_name(conn):
    out = say(conn, "+10000000002", "cancel", name="")
    assert "What name should people see?" in out.reply.text
    assert db.get_user_by_phone(conn, "+10000000002") is None


def test_a_new_peer_user_is_told_their_timezone(conn):
    out = say(conn, "+10000000003", "hi", name="Sara")
    assert "Times are shown in UTC" in out.reply.text


# --- business mode fixes -------------------------------------------------


@pytest.fixture()
def clinic(conn):
    owner = db.create_user(conn, OWNER, "Dr Sharma", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    return db.create_host(conn, "dr-sharma", owner["id"], "Dr Sharma Clinic",
                          location="123 King Fahd Road",
                          services=json.dumps(SERVICES),
                          work_start="09:00", work_end="17:00",
                          work_days="0,1,2,3,4,5,6", slot_minutes=30)


def biz(conn, host, text, *, phone=CUSTOMER, tap=False, name="Ravi", now=NOW):
    return handle_business(
        conn, db.get_host(conn, host["slug"]),
        Inbound(from_phone=phone, text=text, message_id=f"h{id(text)}{text}",
                profile_name=name, is_tap=tap),
        now=now,
    )


def test_a_new_customer_inherits_the_host_timezone(conn, clinic):
    """Rendering in UTC showed a Riyadh 09:30 slot as 06:30 to the customer."""
    out = biz(conn, clinic, "hi")
    assert db.get_user_by_phone(conn, CUSTOMER)["timezone"] == RIYADH
    dates = biz(conn, clinic, out.reply.rows[0].id, tap=True)
    slots = biz(conn, clinic, dates.reply.rows[0].id, tap=True)
    assert slots.reply.rows[0].title == "09:30-10:00"
    assert RIYADH in slots.reply.rows[0].description


def test_the_owner_can_use_peer_commands_on_the_business_number(conn, clinic,
                                                               monkeypatch):
    monkeypatch.setattr(gcal, "auth_url", lambda state: f"https://t/{state}")
    out = biz(conn, clinic, "connect google", phone=OWNER, name="Dr Sharma")
    assert "https://t/" in out.reply.text

    blocked = biz(conn, clinic, "block tomorrow 14:00-15:00 Focus",
                  phone=OWNER, name="Dr Sharma")
    assert "Blocked" in blocked.reply.text

    # A greeting still gets the agenda.
    agenda = biz(conn, clinic, "hi", phone=OWNER, name="Dr Sharma")
    assert "next 7 days" in agenda.reply.text or "nothing booked" in agenda.reply.text


def test_closed_blocks_a_whole_day(conn, clinic):
    out = biz(conn, clinic, "closed tomorrow Eid", phone=OWNER,
              name="Dr Sharma")
    assert "Closed" in out.reply.text
    ref = out.reply.text.split("Ref ")[1][:6]

    # The customer is offered a later day instead.
    menu = biz(conn, clinic, "hi")
    dates = biz(conn, clinic, menu.reply.rows[0].id, tap=True)
    assert all("tomorrow" not in row.title for row in dates.reply.rows)

    reopened = biz(conn, clinic, f"cancel {ref}", phone=OWNER, name="Dr Sharma")
    assert "free again" in reopened.reply.text


def test_closed_refuses_when_the_day_already_has_bookings(conn, clinic):
    menu = biz(conn, clinic, "hi")
    dates = biz(conn, clinic, menu.reply.rows[0].id, tap=True)
    slots = biz(conn, clinic, dates.reply.rows[0].id, tap=True)
    summary = biz(conn, clinic, slots.reply.rows[0].id, tap=True)
    biz(conn, clinic, summary.reply.rows[0].id, tap=True)

    out = biz(conn, clinic, "closed today", phone=OWNER, name="Dr Sharma")
    assert "already has 1 booking(s)" in out.reply.text


def test_reschedule_moves_the_booking_instead_of_adding_one(conn, clinic):
    menu = biz(conn, clinic, "hi")
    dates = biz(conn, clinic, menu.reply.rows[0].id, tap=True)
    slots = biz(conn, clinic, dates.reply.rows[0].id, tap=True)
    summary = biz(conn, clinic, slots.reply.rows[0].id, tap=True)
    first = biz(conn, clinic, summary.reply.rows[0].id, tap=True)
    old_ref = first.reply.text.split("Ref ")[1][:6]

    moving = biz(conn, clinic, "reschedule")
    assert f"Moving booking {old_ref}" in moving.reply.text
    dates2 = biz(conn, clinic, moving.reply.rows[0].id, tap=True)
    slots2 = biz(conn, clinic, dates2.reply.rows[0].id, tap=True)
    summary2 = biz(conn, clinic, slots2.reply.rows[1].id, tap=True)
    assert f"replaces booking {old_ref}" in summary2.reply.text

    done = biz(conn, clinic, summary2.reply.rows[0].id, tap=True)
    assert f"Booking {old_ref} has been cancelled" in done.reply.text
    assert db.get_event(conn, old_ref)["status"] == "cancelled"
    assert conn.execute(
        "SELECT count(*) AS n FROM events WHERE status = 'confirmed'"
    ).fetchone()["n"] == 1


def test_a_dead_end_always_offers_rows(conn, clinic):
    """"No openings" with no rows left the customer nothing to tap."""
    db.update_host(conn, "dr-sharma", work_days="0")  # Mondays only
    conn.execute("UPDATE users SET work_days = '0' WHERE id = ?",
                 (clinic["owner_id"],))
    menu = biz(conn, clinic, "hi")
    out = biz(conn, clinic, menu.reply.rows[0].id, tap=True)
    assert out.reply.rows  # the service menu, not a dead end


def test_the_list_section_title_follows_the_step(conn, clinic):
    from wacal.whatsapp import CloudTransport

    transport = CloudTransport("123", "token")
    menu = biz(conn, clinic, "hi")
    payload = transport.payload(CUSTOMER, menu.reply)
    section = payload["interactive"]["action"]["sections"][0]["title"]
    assert section == "Services"  # was hard-coded to "Slots"


# --- timezone and DST ----------------------------------------------------


def test_the_date_label_comes_from_the_viewers_timezone(conn, people):
    owner, guest = people
    db.update_user(conn, guest["id"], timezone="America/Los_Angeles")
    say(conn, OWNER, f"share {GUEST}")
    out = say(conn, GUEST, f"free {OWNER} 2026-10-16")
    for row in out.reply.rows:
        hour = int(row.title[:2])
        # A Riyadh morning slot is the previous evening in Los Angeles, so the
        # date beside the time must be the viewer's date.
        if hour >= 22:
            assert "Thu 15 Oct" in row.description


def test_dst_fall_back_labels_stay_distinct(conn):
    """Europe/London 2026-10-25: 01:30 happens twice."""
    user = db.create_user(conn, OWNER, "Burhan", "Europe/London")
    db.update_user(conn, user["id"], work_start="00:30", work_end="02:30",
                   work_days="0,1,2,3,4,5,6", slot_minutes=30)
    from wacal.slots import free_slots

    day = datetime(2026, 10, 25, tzinfo=timezone.utc).date()
    slots = free_slots(db.get_user(conn, user["id"]), day, [],
                       now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert ambiguous_day(slots, "Europe/London") is True
    labels = [fmt_slot(s, e, "Europe/London", show_offset=True)
              for s, e in slots]
    assert len(labels) == len(set(labels))
    assert any("+0100" in label for label in labels)
    assert any("+0000" in label for label in labels)


def test_local_date_crosses_the_day_boundary():
    moment = datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc)
    assert local_date(moment, RIYADH).day == 16
    assert local_date(moment, "America/Los_Angeles").day == 15


# --- admin API -----------------------------------------------------------


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "k")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    with TestClient(main.app) as client:
        yield client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


HOST_BODY = {
    "slug": "dr-sharma",
    "business_name": "Dr Sharma Clinic",
    "owner_phone": "966500000001",
    "timezone": RIYADH,
    "services": SERVICES,
    "working_days": [0, 1, 2, 3, 4],
    "working_hours": {"start": "09:00", "end": "17:00"},
}


def post_host(client, **overrides):
    return client.post("/dashboard/register", headers={"x-api-key": "k"},
                       json={**HOST_BODY, **overrides})


def test_register_does_not_rewrite_an_existing_owners_schedule(admin_client):
    conn = main.app.state.conn
    owner = db.create_user(conn, "+966500000001", "Burhan", RIYADH)
    db.update_user(conn, owner["id"], work_start="07:00", work_end="12:00",
                   work_days="4,5")

    assert post_host(admin_client).status_code == 201
    kept = db.get_user(conn, owner["id"])
    assert (kept["work_start"], kept["work_end"]) == ("07:00", "12:00")
    assert kept["work_days"] == "4,5"
    # The host carries the business hours instead.
    host = db.get_host(conn, "dr-sharma")
    assert (host["work_start"], host["work_end"]) == ("09:00", "17:00")


def test_register_normalises_the_owner_phone(admin_client):
    assert post_host(admin_client).status_code == 201
    assert db.get_user_by_phone(main.app.state.conn, "+966500000001")


def test_an_inverted_window_is_rejected(admin_client):
    response = post_host(admin_client, slug="x1",
                         working_hours={"start": "17:00", "end": "09:00"})
    assert response.status_code == 422


def test_an_unknown_timezone_is_rejected(admin_client):
    response = post_host(admin_client, slug="x2", timezone="Mars/Olympus")
    assert response.status_code == 422
