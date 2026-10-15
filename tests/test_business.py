from __future__ import annotations

import json
from datetime import timedelta

import pytest

from wacal import db
from wacal.business import handle_business
from wacal.whatsapp import Inbound

from .conftest import NOW, OWNER, RIYADH

CUSTOMER = "+966500000055"
SERVICES = [
    {"name": "Consultation", "duration_mins": 30, "price": 150},
    {"name": "Follow-up", "duration_mins": 15, "price": 50},
]


@pytest.fixture()
def host(conn):
    owner = db.create_user(conn, OWNER, "Dr Sharma", RIYADH)
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6",
                   work_start="09:00", work_end="17:00")
    # The customer exists up front with a known timezone, because every reply
    # renders times in the viewer's zone. Auto-registration is covered
    # separately in test_unregistered_customer_without_a_profile_name_is_asked.
    db.create_user(conn, CUSTOMER, "Ravi", RIYADH)
    return db.create_host(conn, "dr-sharma", owner["id"], "Dr Sharma Clinic",
                          location="123 King Fahd Road", currency="SAR",
                          services=json.dumps(SERVICES))


def send(conn, host, text, *, phone=CUSTOMER, tap=False, now=NOW, name="Ravi"):
    return handle_business(
        conn, host,
        Inbound(from_phone=phone, text=text, message_id=f"b{id(text)}",
                profile_name=name, is_tap=tap),
        now=now,
    )


def test_customer_sees_the_service_menu_without_any_grant(conn, host):
    out = send(conn, host, "hi")
    assert [r.title for r in out.reply.rows] == ["Consultation", "Follow-up"]
    assert "30 min · 150 SAR" in out.reply.rows[0].description
    # The customer never shared anything, and nobody shared with them.
    customer = db.get_user_by_phone(conn, CUSTOMER)
    assert db.grants_received(conn, customer["id"]) == []


def test_unregistered_customer_without_a_profile_name_is_asked(conn, host):
    out = send(conn, host, "hi", phone="+966500000099", name="")
    assert "What name should people see?" in out.reply.text
    assert db.get_user_by_phone(conn, "+966500000099") is None


def test_full_booking_path(conn, host):
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    assert dates.reply.rows[0].title.endswith("(today)")

    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    assert slots.reply.rows[0].title == "09:30-10:00"

    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    assert "Please confirm" in summary.reply.text
    assert "Consultation (30 min)" in summary.reply.text
    assert "150 SAR" in summary.reply.text
    assert "123 King Fahd Road" in summary.reply.text
    assert [r.title for r in summary.reply.rows] == ["Confirm", "Start over"]

    done = send(conn, host, summary.reply.rows[0].id, tap=True)
    assert "Confirmed" in done.reply.text
    assert done.notify[0][0] == OWNER
    assert "New booking at Dr Sharma Clinic" in done.notify[0][1].text
    assert "Ravi" in done.notify[0][1].text

    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["host_slug"] == "dr-sharma"
    assert row["service"] == "Consultation"
    assert row["title"] == "Consultation — Ravi"


def test_slot_length_follows_the_chosen_service(conn, host):
    menu = send(conn, host, "hi").reply.rows
    follow_up = menu[1]
    dates = send(conn, host, follow_up.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    # 15 minute service, so a 15 minute grid.
    assert slots.reply.rows[0].title == "09:15-09:30"
    assert slots.reply.rows[1].title == "09:30-09:45"


def test_customer_working_hours_do_not_narrow_the_offer(conn, host):
    # A customer in London whose own working day starts at 09:00 London
    # (11:00 Riyadh) must still be offered the clinic's 09:30 Riyadh slot.
    send(conn, host, "hi")
    customer = db.get_user_by_phone(conn, CUSTOMER)
    db.update_user(conn, customer["id"], timezone="Europe/London")

    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    # 09:30 Riyadh (UTC+3) is 07:30 London, which is still BST (UTC+1) in
    # mid-October -- before the clinic's customer starts work, and offered anyway.
    assert slots.reply.rows[0].title == "07:30-08:00"


def test_a_taken_slot_is_refused_at_confirm(conn, host):
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)

    # Another customer takes the same time before the first one confirms.
    other = send(conn, host, slots.reply.rows[0].id, tap=True,
                 phone="+966500000077", name="Sara")
    send(conn, host, other.reply.rows[0].id, tap=True, phone="+966500000077",
         name="Sara")

    out = send(conn, host, summary.reply.rows[0].id, tap=True)
    assert "taken while you were deciding" in out.reply.text
    assert conn.execute(
        "SELECT count(*) AS n FROM events WHERE status = 'confirmed'"
    ).fetchone()["n"] == 1


def test_confirm_in_the_past_is_refused(conn, host):
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    out = send(conn, host, summary.reply.rows[0].id, tap=True,
               now=NOW + timedelta(days=2))
    assert "has passed" in out.reply.text


def test_start_over_returns_to_the_menu(conn, host):
    out = send(conn, host, "b:restart:0", tap=True)
    assert [r.title for r in out.reply.rows] == ["Consultation", "Follow-up"]


def test_my_bookings_and_customer_cancel(conn, host):
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    done = send(conn, host, summary.reply.rows[0].id, tap=True)
    ref = done.reply.text.split("Ref ")[1][:6]

    listed = send(conn, host, "my bookings")
    assert ref in listed.reply.text

    out = send(conn, host, f"cancel {ref}")
    assert f"Cancelled {ref}" in out.reply.text
    assert out.notify[0][0] == OWNER
    assert db.get_event(conn, ref)["status"] == "cancelled"


def test_bare_cancel_with_one_booking_resolves_it(conn, host):
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    ref = send(conn, host, summary.reply.rows[0].id,
               tap=True).reply.text.split("Ref ")[1][:6]

    out = send(conn, host, "cancel")
    assert f"Cancelled {ref}" in out.reply.text


def test_a_ref_from_another_host_is_not_cancellable_here(conn, host):
    owner2 = db.create_user(conn, "+966500000003", "Other Clinic", RIYADH)
    db.update_user(conn, owner2["id"], work_days="0,1,2,3,4,5,6")
    host2 = db.create_host(conn, "other", owner2["id"], "Other Clinic",
                           services=json.dumps(SERVICES))

    service = send(conn, host2, "hi").reply.rows[0]
    dates = send(conn, host2, service.id, tap=True)
    slots = send(conn, host2, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host2, slots.reply.rows[0].id, tap=True)
    ref = send(conn, host2, summary.reply.rows[0].id,
               tap=True).reply.text.split("Ref ")[1][:6]

    out = send(conn, host, f"cancel {ref}")
    assert "No booking with ref" in out.reply.text
    assert db.get_event(conn, ref)["status"] == "confirmed"


def test_owner_texting_their_own_page_gets_the_agenda(conn, host):
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    send(conn, host, summary.reply.rows[0].id, tap=True)

    out = send(conn, host, "hi", phone=OWNER, name="Dr Sharma")
    assert "next 7 days" in out.reply.text
    assert "Ravi" in out.reply.text
    assert CUSTOMER in out.reply.text


def test_empty_service_menu_is_stated(conn, conn_host_without_services):
    out = send(conn, conn_host_without_services, "hi")
    assert "no services set up yet" in out.reply.text


@pytest.fixture()
def conn_host_without_services(conn):
    owner = db.create_user(conn, "+966500000004", "Empty Co", RIYADH)
    return db.create_host(conn, "empty", owner["id"], "Empty Co")


def test_google_busy_blocks_business_slots(conn, host, fake_google):
    owner = db.get_user(conn, host["owner_id"])
    db.save_calendar_link(conn, owner["id"], '{"token": "x"}')
    fake_google.busy_intervals = [
        ("2026-10-15T06:00:00Z", "2026-10-15T09:00:00Z")  # 09:00-12:00 Riyadh
    ]
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    assert slots.reply.rows[0].title == "12:00-12:30"


def test_google_outage_stops_the_business_flow(conn, host, fake_google):
    owner = db.get_user(conn, host["owner_id"])
    db.save_calendar_link(conn, owner["id"], '{"token": "x"}')
    fake_google.fail_busy = True
    service = send(conn, host, "hi").reply.rows[0]
    out = send(conn, host, service.id, tap=True)
    assert "Cannot read Dr Sharma Clinic's calendar" in out.reply.text
    assert out.reply.rows == []


def test_business_booking_is_mirrored_to_google(conn, host, fake_google):
    owner = db.get_user(conn, host["owner_id"])
    db.save_calendar_link(conn, owner["id"], '{"token": "x"}')
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    send(conn, host, summary.reply.rows[0].id, tap=True)

    assert len(fake_google.created) == 1
    assert fake_google.created[0]["location"] == "123 King Fahd Road"
    assert "Consultation" in fake_google.created[0]["title"]


def test_stale_service_index_is_handled(conn, host):
    db.update_host(conn, "dr-sharma", services=json.dumps([]))
    reloaded = db.get_host(conn, "dr-sharma")
    out = send(conn, reloaded, "b:svc:3:abcd", tap=True)
    assert "no longer offered" in out.reply.text


def test_an_edited_menu_invalidates_a_saved_confirm_button(conn, host):
    """The service fingerprint in the id must outrank the menu position."""
    service = send(conn, host, "hi").reply.rows[0]
    dates = send(conn, host, service.id, tap=True)
    slots = send(conn, host, dates.reply.rows[0].id, tap=True)
    summary = send(conn, host, slots.reply.rows[0].id, tap=True)
    assert "Consultation" in summary.reply.text
    confirm_id = summary.reply.rows[0].id

    # The admin reorders the menu; position 0 is now a different service.
    db.update_host(conn, "dr-sharma", services=json.dumps(
        [{"name": "Vaccination", "duration_mins": 30, "price": 90},
         *SERVICES]))
    out = send(conn, db.get_host(conn, "dr-sharma"), confirm_id, tap=True)
    assert "no longer offered" in out.reply.text
    assert conn.execute(
        "SELECT count(*) AS n FROM events").fetchone()["n"] == 0


def test_unreadable_tap_payloads_do_not_crash(conn, host):
    for payload in ("b:", "b:svc", "b:day:0:not-a-date", "b:slot:0:zzz",
                    "b:nonsense:1"):
        out = send(conn, host, payload, tap=True)
        assert out.reply.text
