from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wacal import db, main
from wacal.business import handle_business
from wacal.slots import schedule_for
from wacal.whatsapp import Inbound, MemoryTransport

from .conftest import GUEST, NOW, OWNER, RIYADH, say

SERVICES = [{"name": "Consultation", "duration_mins": 30, "price": 150}]
KEY = "admin-key"


# --- per-host schedule ---------------------------------------------------


def test_host_hours_override_the_owners(conn):
    owner = db.create_user(conn, OWNER, "Dr Sharma", RIYADH)
    db.update_user(conn, owner["id"], work_start="09:00", work_end="17:00",
                   work_days="0,1,2,3,4", slot_minutes=30)
    host = db.create_host(conn, "evening", owner["id"], "Evening Clinic",
                          work_start="18:00", work_end="21:00",
                          work_days="5,6", slot_minutes=20)

    schedule = schedule_for(db.get_user(conn, owner["id"]), host)
    assert schedule["work_start"] == "18:00"
    assert schedule["work_days"] == "5,6"
    assert schedule["slot_minutes"] == 20
    # The timezone always comes from the owner; a host has none of its own.
    assert schedule["timezone"] == RIYADH


def test_unset_host_fields_inherit_the_owner(conn):
    owner = db.create_user(conn, OWNER, "Dr Sharma", RIYADH)
    db.update_user(conn, owner["id"], work_start="08:00", work_end="12:00")
    host = db.create_host(conn, "inherit", owner["id"], "Inherited")
    schedule = schedule_for(db.get_user(conn, owner["id"]), host)
    assert (schedule["work_start"], schedule["work_end"]) == ("08:00", "12:00")


def test_two_hosts_one_owner_keep_separate_hours(conn):
    owner = db.create_user(conn, OWNER, "Dr Sharma", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    morning = db.create_host(conn, "morning", owner["id"], "Morning Clinic",
                             services=json.dumps(SERVICES),
                             work_start="09:00", work_end="11:00",
                             work_days="0,1,2,3,4,5,6")
    evening = db.create_host(conn, "evening", owner["id"], "Evening Clinic",
                             services=json.dumps(SERVICES),
                             work_start="19:00", work_end="21:00",
                             work_days="0,1,2,3,4,5,6")
    db.create_user(conn, GUEST, "Ravi", RIYADH)

    def first_slot(host):
        service = _send(conn, host, "hi").reply.rows[0]
        dates = _send(conn, host, service.id, tap=True)
        slots = _send(conn, host, dates.reply.rows[0].id, tap=True)
        return slots.reply.rows[0].title

    assert first_slot(morning) == "09:30-10:00"
    assert first_slot(evening) == "19:00-19:30"


def _send(conn, host, text, *, tap=False, now=NOW):
    return handle_business(
        conn, host,
        Inbound(from_phone=GUEST, text=text, message_id=f"x{id(text)}{text}",
                profile_name="Ravi", is_tap=tap),
        now=now,
    )


# --- aliases and Hindi ---------------------------------------------------


def test_peer_aliases(conn, people):
    for greeting in ("hi", "hello", "नमस्ते", "مرحبا", "menu"):
        assert "free <name|phone>" in say(conn, OWNER, greeting).reply.text
    assert "Nothing upcoming" in say(conn, OWNER, "मेरी बुकिंग").reply.text
    assert "Nothing upcoming" in say(conn, OWNER, "my bookings").reply.text


def test_hindi_free_and_cancel_in_peer_mode(conn, people):
    say(conn, OWNER, f"share {GUEST}")
    out = say(conn, GUEST, f"खाली {OWNER} today")
    assert out.reply.rows
    ref = say(conn, GUEST, out.reply.rows[0].id,
              tap=True).reply.text.split("Ref ")[1][:6]
    assert "Cancelled" in say(conn, GUEST, f"रद्द {ref}").reply.text


def test_business_hindi_menu_and_cancel(conn):
    owner = db.create_user(conn, OWNER, "Dr Sharma", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    host = db.create_host(conn, "dr-sharma", owner["id"], "Dr Sharma Clinic",
                          services=json.dumps(SERVICES))
    db.create_user(conn, GUEST, "Ravi", RIYADH)

    assert _send(conn, host, "नमस्ते").reply.rows[0].title == "Consultation"

    service = _send(conn, host, "hi").reply.rows[0]
    dates = _send(conn, host, service.id, tap=True)
    slots = _send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = _send(conn, host, slots.reply.rows[0].id, tap=True)
    ref = _send(conn, host, summary.reply.rows[0].id,
                tap=True).reply.text.split("Ref ")[1][:6]

    assert ref in _send(conn, host, "मेरी बुकिंग").reply.text
    # "रद्द करें" has a second word that is not a ref, so it must not be read
    # as one -- the single booking is resolved instead.
    out = _send(conn, host, "रद्द करें")
    assert f"Cancelled {ref}" in out.reply.text


# --- dashboard additions -------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", KEY)
    monkeypatch.setenv("BASE_URL", "https://book.example.com")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


HOST_BODY = {
    "slug": "dr-sharma",
    "business_name": "Dr Sharma Clinic",
    "owner_phone": "+966500000001",
    "owner_name": "Dr Sharma",
    "owner_email": "clinic@example.com",
    "location": "123 King Fahd Road",
    "timezone": RIYADH,
    "services": SERVICES,
    "working_days": [0, 1, 2, 3, 4, 5],
    "working_hours": {"start": "09:00", "end": "19:00"},
    "slot_minutes": 30,
}


def admin(client, method, path, **kwargs):
    return client.request(method, path, headers={"x-api-key": KEY}, **kwargs)


def test_register_reports_both_webhook_urls(client):
    body = admin(client, "POST", "/dashboard/register", json=HOST_BODY).json()
    assert body["webhook_url"].endswith("/webhook/dr-sharma")
    assert body["twilio_webhook_url"].endswith("/twilio/webhook/dr-sharma")
    assert len(body["next_steps"]) == 3


def test_get_host_returns_the_full_configuration(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    body = admin(client, "GET", "/dashboard/dr-sharma").json()
    assert body["owner_email"] == "clinic@example.com"
    assert body["working_hours"] == {"start": "09:00", "end": "19:00"}
    assert body["working_days"] == [0, 1, 2, 3, 4, 5]
    assert body["slot_minutes"] == 30
    assert body["schedule_source"] == "host"
    assert body["google_calendar_connected"] is False
    assert [s["name"] for s in body["services"]] == ["Consultation"]


def test_get_host_needs_the_key(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    assert client.get("/dashboard/dr-sharma").status_code == 401


def test_put_replaces_hours_and_days(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    body = admin(client, "PUT", "/dashboard/dr-sharma", json={
        "business_name": "Dr Sharma Evening",
        "working_days": [5, 6],
        "working_hours": {"start": "18:00", "end": "22:00"},
        "slot_minutes": 20,
        "services": SERVICES,
    }).json()
    assert body["working_days"] == [5, 6]
    assert body["working_hours"] == {"start": "18:00", "end": "22:00"}
    assert body["slot_minutes"] == 20
    assert body["business_name"] == "Dr Sharma Evening"
    # A full replace clears what the body omitted.
    assert body["location"] == ""


def test_patch_can_change_only_the_hours(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    admin(client, "PATCH", "/dashboard/dr-sharma",
          json={"working_hours": {"start": "07:00", "end": "08:00"}})
    body = admin(client, "GET", "/dashboard/dr-sharma").json()
    assert body["working_hours"] == {"start": "07:00", "end": "08:00"}
    # Everything else is untouched.
    assert body["location"] == "123 King Fahd Road"
    assert [s["name"] for s in body["services"]] == ["Consultation"]


def test_put_and_patch_reject_bad_days(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    assert admin(client, "PUT", "/dashboard/dr-sharma",
                 json={"business_name": "x", "working_days": [7]}
                 ).status_code == 422
    assert admin(client, "PATCH", "/dashboard/dr-sharma",
                 json={"working_days": []}).status_code == 422


def test_stats_include_the_forward_looking_counts(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    body = admin(client, "GET", "/dashboard/dr-sharma/stats").json()
    assert body["upcoming_7_days"] == 0
    assert body["upcoming_30_days"] == 0
    assert body["confirmed"] == 0


def test_external_calendar_route(client, fake_google):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    conn = main.app.state.conn

    unlinked = admin(client, "GET", "/dashboard/dr-sharma/calendar").json()
    assert unlinked == {"slug": "dr-sharma", "connected": False, "events": []}

    owner = db.get_user_by_phone(conn, "+966500000001")
    db.save_calendar_link(conn, owner["id"], '{"token": "x"}')
    fake_google.busy_intervals = [
        ("2026-10-15T07:00:00Z", "2026-10-15T08:00:00Z")
    ]
    body = admin(client, "GET", "/dashboard/dr-sharma/calendar").json()
    assert body["connected"] is True
    assert body["count"] == 1
    assert body["events"][0]["summary"] == "External meeting"

    fake_google.fail_busy = True
    assert admin(client, "GET",
                 "/dashboard/dr-sharma/calendar").status_code == 502


def test_schedule_source_says_owner_when_the_host_sets_nothing(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    conn = main.app.state.conn
    db.update_host(conn, "dr-sharma", work_start=None, work_end=None,
                   work_days=None, slot_minutes=None)
    body = admin(client, "GET", "/dashboard/dr-sharma").json()
    assert body["schedule_source"] == "owner"
    assert body["working_hours"] == {"start": "09:00", "end": "19:00"}


# --- launcher ------------------------------------------------------------


def test_start_seed_is_idempotent(conn):
    import start

    start.seed(conn)
    first = db.get_host(conn, start.DEMO_SLUG)
    assert first["business_name"] == "Demo Clinic"
    assert len(json.loads(first["services"])) == 3

    db.update_host(conn, start.DEMO_SLUG, business_name="Renamed")
    start.seed(conn)
    assert db.get_host(conn, start.DEMO_SLUG)["business_name"] == "Renamed"


def test_migrate_is_a_no_op_on_a_fresh_database(conn):
    assert db.migrate(conn) == []
