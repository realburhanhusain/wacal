from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wacal import db
from wacal.commands import handle
from wacal.whatsapp import CloudTransport, Inbound, parse_webhook

RIYADH = "Asia/Riyadh"
# Thursday 2026-10-15, 06:00 UTC == 09:00 Asia/Riyadh.
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)

OWNER = "+966500000001"
GUEST = "+966500000002"


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def people(conn):
    owner = db.create_user(conn, OWNER, "Burhan", RIYADH)
    guest = db.create_user(conn, GUEST, "Ahmed", RIYADH)
    # Thursday is a working day for both.
    db.update_user(conn, owner["id"], work_days="0,1,2,3,4,5,6")
    db.update_user(conn, guest["id"], work_days="0,1,2,3,4,5,6")
    return db.get_user(conn, owner["id"]), db.get_user(conn, guest["id"])


def say(conn, phone, text, *, tap=False, now=NOW, name=""):
    return handle(
        conn,
        Inbound(from_phone=phone, text=text, message_id=f"m{id(text)}",
                profile_name=name, is_tap=tap),
        now=now,
    )


def test_unknown_sender_is_asked_for_a_name(conn):
    out = say(conn, "+10000000000", "hello")
    assert "name" in out.reply.text.lower()
    assert db.get_user_by_phone(conn, "+10000000000") is None


def test_profile_name_auto_registers(conn):
    say(conn, "+10000000000", "hi", name="Sara")
    assert db.get_user_by_phone(conn, "+10000000000")["display_name"] == "Sara"


def test_free_requires_a_grant(conn, people):
    out = say(conn, GUEST, f"free {OWNER} today")
    assert "not shared" in out.reply.text
    assert out.reply.rows == []


def test_share_then_see_slots(conn, people):
    share = say(conn, OWNER, f"share {GUEST}")
    assert share.notify and share.notify[0][0] == GUEST

    out = say(conn, GUEST, f"free {OWNER} today")
    # 09:00-17:00 Riyadh in 30 minute slots, capped at the Cloud API limit of 10.
    assert len(out.reply.rows) == 10
    assert out.reply.rows[0].title == "09:30-10:00"  # 15 min lead time skips 09:00


def test_booking_blocks_the_slot_for_everyone(conn, people):
    say(conn, OWNER, f"share {GUEST}")
    rows = say(conn, GUEST, f"free {OWNER} today").reply.rows
    picked = rows[0]

    booked = say(conn, GUEST, picked.id, tap=True)
    assert "Booked Burhan" in booked.reply.text
    assert booked.notify[0][0] == OWNER
    assert "Ahmed booked you" in booked.notify[0][1].text

    again = say(conn, GUEST, f"free {OWNER} today").reply.rows
    assert picked.title not in [r.title for r in again]


def test_double_tap_does_not_double_book(conn, people):
    say(conn, OWNER, f"share {GUEST}")
    picked = say(conn, GUEST, f"free {OWNER} today").reply.rows[0]
    say(conn, GUEST, picked.id, tap=True)
    second = say(conn, GUEST, picked.id, tap=True)
    assert "no longer free" in second.reply.text
    count = conn.execute(
        "SELECT count(*) AS n FROM events WHERE status = 'confirmed'"
    ).fetchone()["n"]
    assert count == 1


def test_cancel_frees_the_slot_and_tells_the_other_party(conn, people):
    say(conn, OWNER, f"share {GUEST}")
    picked = say(conn, GUEST, f"free {OWNER} today").reply.rows[0]
    ref = say(conn, GUEST, picked.id, tap=True).reply.text.split("Ref ")[1][:6]

    out = say(conn, OWNER, f"cancel {ref}")
    assert "Cancelled" in out.reply.text
    assert out.notify[0][0] == GUEST

    titles = [r.title for r in say(conn, GUEST, f"free {OWNER} today").reply.rows]
    assert picked.title in titles


def test_stranger_cannot_cancel(conn, people):
    say(conn, OWNER, f"share {GUEST}")
    picked = say(conn, GUEST, f"free {OWNER} today").reply.rows[0]
    ref = say(conn, GUEST, picked.id, tap=True).reply.text.split("Ref ")[1][:6]

    db.create_user(conn, "+10000000009", "Nosy", RIYADH)
    out = say(conn, "+10000000009", f"cancel {ref}")
    assert "Only the owner" in out.reply.text
    assert db.get_event(conn, ref)["status"] == "confirmed"


def test_revoke_removes_visibility(conn, people):
    say(conn, OWNER, f"share {GUEST}")
    say(conn, OWNER, f"unshare {GUEST}")
    assert "not shared" in say(conn, GUEST, f"free {OWNER} today").reply.text


def test_requesters_own_booking_hides_the_slot(conn, people):
    owner, guest = people
    say(conn, OWNER, f"share {GUEST}")
    # Ahmed blocks 10:00-11:00 Riyadh for himself.
    say(conn, GUEST, "block today 10:00-11:00 Gym")
    titles = [r.title for r in say(conn, GUEST, f"free {OWNER} today").reply.rows]
    assert "10:00-10:30" not in titles
    assert "11:00-11:30" in titles


def test_free_busy_never_leaks_titles(conn, people):
    owner, guest = people
    say(conn, OWNER, "block today 12:00-13:00 Board review salary")
    say(conn, OWNER, f"share {GUEST}")
    out = say(conn, GUEST, f"free {OWNER} today")
    blob = out.reply.text + " ".join(r.title + r.description for r in out.reply.rows)
    assert "salary" not in blob.lower()


def test_timezones_render_per_viewer(conn, people):
    owner, guest = people
    db.update_user(conn, guest["id"], timezone="Europe/London")
    say(conn, OWNER, f"share {GUEST}")
    out = say(conn, GUEST, f"free {OWNER} today")
    titles = [r.title for r in out.reply.rows]
    # Burhan's 09:30 Riyadh is 07:30 London, before Ahmed's own working day,
    # so the first offer is the earliest slot inside both windows: 09:00 London
    # (= 11:00 Riyadh). Everything is rendered in the viewer's timezone.
    assert titles[0] == "09:00-09:30"
    assert "07:30-08:00" not in titles


def test_non_working_day_rolls_forward(conn, people):
    owner, guest = people
    db.update_user(conn, owner["id"], work_days="6")  # Sundays only
    say(conn, OWNER, f"share {GUEST}")
    out = say(conn, GUEST, f"free {OWNER}")
    assert out.reply.rows
    assert "Sun 18 Oct" in out.reply.header


def test_explicit_day_does_not_roll_forward(conn, people):
    owner, guest = people
    db.update_user(conn, owner["id"], work_days="6")
    say(conn, OWNER, f"share {GUEST}")
    out = say(conn, GUEST, f"free {OWNER} 2026-10-15")
    assert "No open slots" in out.reply.text


def test_past_slot_is_refused(conn, people):
    owner, guest = people
    say(conn, OWNER, f"share {GUEST}")
    picked = say(conn, GUEST, f"free {OWNER} today").reply.rows[0]
    later = NOW + timedelta(days=1)
    out = say(conn, GUEST, picked.id, tap=True, now=later)
    assert "already started" in out.reply.text


def test_hours_and_days_settings(conn, people):
    say(conn, OWNER, "hours 08:00-09:00")
    say(conn, OWNER, "days mon-fri")
    owner = db.get_user_by_phone(conn, OWNER)
    assert (owner["work_start"], owner["work_end"]) == ("08:00", "09:00")
    assert owner["work_days"] == "0,1,2,3,4"


def test_webhook_parsing_handles_taps_and_status_callbacks():
    assert parse_webhook({"entry": [{"changes": [{"value": {
        "statuses": [{"status": "delivered"}]}}]}]}) == []

    messages = parse_webhook({"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "966500000002", "profile": {"name": "Ahmed"}}],
        "messages": [{
            "from": "966500000002", "id": "wamid.1", "type": "interactive",
            "interactive": {"type": "list_reply",
                            "list_reply": {"id": "book:1:1760000000"}},
        }],
    }}]}]})
    assert len(messages) == 1
    assert messages[0].is_tap
    assert messages[0].text == "book:1:1760000000"
    assert messages[0].profile_name == "Ahmed"


def test_message_ids_are_deduped(conn):
    assert db.mark_seen(conn, "wamid.1") is True
    assert db.mark_seen(conn, "wamid.1") is False


def test_cloud_payload_respects_row_limits():
    from wacal.whatsapp import Reply, Row

    transport = CloudTransport("123", "token")
    rows = [Row(id=f"book:1:{i}", title="x" * 40, description="y" * 200)
            for i in range(25)]
    payload = transport.payload("966500000002", Reply(text="hi", rows=rows))
    sent = payload["interactive"]["action"]["sections"][0]["rows"]
    assert len(sent) == 10
    assert all(len(r["title"]) <= 24 for r in sent)
    assert all(len(r["description"]) <= 72 for r in sent)
