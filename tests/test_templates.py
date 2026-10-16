"""The 24-hour window and the template messages that survive it.

Every test here unsets WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW, which the rest of
the suite leaves on, so these exercise the real provider rule rather than the
local escape hatch.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from wacal import db, main, templates
from wacal.whatsapp import (
    CloudTransport,
    MemoryTransport,
    Reply,
    Row,
    TemplateRequired,
    TwilioTransport,
)

from .conftest import GUEST, OWNER

CONFIRMED = "wacal_booking_confirmed"


@pytest.fixture(autouse=True)
def strict_window(monkeypatch):
    """The production rule: no free-form message outside the window."""
    monkeypatch.delenv("WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW", raising=False)


@pytest.fixture()
def with_templates(monkeypatch):
    monkeypatch.setenv("WA_TEMPLATE_BOOKING_CONFIRMED", CONFIRMED)
    monkeypatch.setenv("WA_TEMPLATE_LANGUAGE", "en_US")


# --- the window itself ---------------------------------------------------


def test_a_number_we_have_never_heard_from_is_outside_the_window(conn):
    """The optimistic default would drop the first message to every contact."""
    assert db.within_window(conn, OWNER) is False


def test_an_inbound_message_opens_the_window_and_it_expires(conn):
    db.touch_inbound(conn, OWNER)
    assert db.within_window(conn, OWNER) is True

    later = db.now_utc() + timedelta(hours=24, minutes=1)
    assert db.within_window(conn, OWNER, now=later) is False


def test_the_window_is_per_number(conn):
    db.touch_inbound(conn, OWNER)
    assert db.within_window(conn, GUEST) is False


# --- what the delivery decision is -------------------------------------


def test_inside_the_window_a_plain_message_is_sent_as_text():
    status = templates.delivery_status(inside_window=True, template=None)
    assert status.deliverable is True
    assert status.force_template is False


def test_outside_the_window_a_template_is_forced(with_templates):
    template = templates.build(templates.BOOKING_CONFIRMED, "a", "b", "c")
    status = templates.delivery_status(inside_window=False, template=template)
    assert status.deliverable is True
    assert status.force_template is True


def test_outside_the_window_with_no_template_the_message_is_refused():
    status = templates.delivery_status(inside_window=False, template=None,
                                       kind=templates.BOOKING_CONFIRMED)
    assert status.deliverable is False
    assert "WA_TEMPLATE_BOOKING_CONFIRMED" in status.reason
    assert status.missing == ["WA_TEMPLATE_BOOKING_CONFIRMED"]


def test_the_escape_hatch_allows_free_form_again(monkeypatch):
    monkeypatch.setenv("WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW", "1")
    status = templates.delivery_status(inside_window=False, template=None)
    assert status.deliverable is True
    assert status.force_template is False


def test_an_unconfigured_kind_keeps_its_kind_but_has_no_name():
    """The kind survives so a refusal can name the variable to set."""
    template = templates.build(templates.BOOKING_CANCELLED, "x")
    assert template.name == ""
    assert template.kind == templates.BOOKING_CANCELLED

    status = templates.delivery_status(inside_window=False, template=template)
    assert status.deliverable is False
    assert status.missing == ["WA_TEMPLATE_BOOKING_CANCELLED"]


def test_an_unknown_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        templates.build("not_a_kind", "x")


# --- the Cloud API payload ----------------------------------------------


def test_the_cloud_template_payload_names_the_template_and_its_variables(
        with_templates):
    transport = CloudTransport("pnid", "tok")
    reply = Reply(text="ignored",
                  template=templates.build(templates.BOOKING_CONFIRMED,
                                           "Ravi booked you", "Sat 09:00",
                                           "AB12CD"))
    payload = transport.payload("+966500000001", reply, force_template=True)

    assert payload["type"] == "template"
    assert payload["template"]["name"] == CONFIRMED
    assert payload["template"]["language"] == {"code": "en_US"}
    body = payload["template"]["components"][0]
    assert body["type"] == "body"
    assert [p["text"] for p in body["parameters"]] == [
        "Ravi booked you", "Sat 09:00", "AB12CD"]


def test_a_template_variable_never_contains_a_newline(with_templates):
    """Meta rejects a parameter with a newline, tab or four spaces in it."""
    template = templates.build(templates.BOOKING_CONFIRMED,
                               "two\nlines\tand   spaces")
    assert template.body_params == ("two lines and spaces",)


def test_an_empty_variable_becomes_a_placeholder(with_templates):
    assert templates.build(templates.BOOKING_CONFIRMED, "").body_params == ("-",)


def test_a_template_with_no_variables_sends_no_components(monkeypatch):
    monkeypatch.setenv("WA_TEMPLATE_BOOKING_CONFIRMED", CONFIRMED)
    reply = Reply(text="x",
                  template=templates.build(templates.BOOKING_CONFIRMED))
    payload = CloudTransport("p", "t").payload("+1", reply,
                                               force_template=True)
    assert "components" not in payload["template"]


def test_the_otp_template_repeats_the_code_on_its_button(monkeypatch):
    monkeypatch.setenv("WA_TEMPLATE_OTP", "wacal_otp")
    reply = Reply(text="x", template=templates.build(templates.OTP, "123456",
                                                     button_params=("123456",)))
    payload = CloudTransport("p", "t").payload("+1", reply,
                                               force_template=True)
    button = payload["template"]["components"][1]
    assert button["type"] == "button"
    assert button["index"] == "0"
    assert button["parameters"][0]["text"] == "123456"


def test_forcing_a_template_that_is_not_there_raises_rather_than_sending_text():
    """Falling back to text here is a send nobody receives, reported as sent."""
    transport = CloudTransport("p", "t")
    with pytest.raises(TemplateRequired):
        transport.payload("+1", Reply(text="hello"), force_template=True)


def test_rows_are_still_an_interactive_list_inside_the_window():
    payload = CloudTransport("p", "t").payload(
        "+1", Reply(text="pick", rows=[Row(id="a", title="A")]))
    assert payload["type"] == "interactive"


# --- Twilio -------------------------------------------------------------


def test_twilio_sends_a_content_sid_and_numbered_variables(monkeypatch):
    monkeypatch.setenv("WA_TEMPLATE_BOOKING_CONFIRMED", "HX0123456789")
    transport = TwilioTransport("AC1", "tok", "+14155238886")
    reply = Reply(text="x",
                  template=templates.build(templates.BOOKING_CONFIRMED,
                                           "Ravi", "Sat 09:00"))
    data = transport.payload("+966500000001", reply, force_template=True)

    assert data["ContentSid"] == "HX0123456789"
    assert json.loads(data["ContentVariables"]) == {"1": "Ravi",
                                                    "2": "Sat 09:00"}
    assert "Body" not in data


def test_twilio_refuses_a_meta_template_name(with_templates):
    """A Meta template name sent to Twilio is error 21656, not a delivery."""
    transport = TwilioTransport("AC1", "tok", "+14155238886")
    reply = Reply(text="x",
                  template=templates.build(templates.BOOKING_CONFIRMED, "a"))
    with pytest.raises(TemplateRequired, match="HX"):
        transport.payload("+966500000001", reply, force_template=True)


# --- the outbox ----------------------------------------------------------


def test_a_queued_template_survives_a_restart(conn, with_templates):
    """The queue is the gap: a notice may only be sent hours later."""
    reply = Reply(text="New booking",
                  template=templates.build(templates.BOOKING_CONFIRMED,
                                           "Ravi", "Sat", "AB12CD"))
    db.queue_outbound(conn, OWNER, reply)
    row = db.pending_outbound(conn)[0]
    restored = db.reply_from_row(row)

    assert restored.template is not None
    assert restored.template.name == CONFIRMED
    assert restored.template.body_params == ("Ravi", "Sat", "AB12CD")


def test_a_queued_reply_without_a_template_restores_as_none(conn):
    db.queue_outbound(conn, OWNER, Reply(text="hi"))
    assert db.reply_from_row(db.pending_outbound(conn)[0]).template is None


# --- the drainer ---------------------------------------------------------


def test_the_drainer_sends_a_template_outside_the_window(conn, with_templates):
    transport = MemoryTransport()
    db.queue_outbound(conn, OWNER, Reply(
        text="New booking",
        template=templates.build(templates.BOOKING_CONFIRMED, "Ravi")))

    assert asyncio.run(main.drain_outbox(conn, transport)) == 1
    assert transport.templated == [True]


def test_the_drainer_sends_plain_text_inside_the_window(conn, with_templates):
    transport = MemoryTransport()
    db.touch_inbound(conn, OWNER)
    db.queue_outbound(conn, OWNER, Reply(
        text="New booking",
        template=templates.build(templates.BOOKING_CONFIRMED, "Ravi")))

    assert asyncio.run(main.drain_outbox(conn, transport)) == 1
    assert transport.templated == [False]


def test_an_undeliverable_message_is_kept_pending_with_the_reason(conn):
    """Reporting this as sent is how a confirmed booking goes unnoticed."""
    transport = MemoryTransport()
    outbox_id = db.queue_outbound(conn, OWNER, Reply(text="New booking"))

    assert asyncio.run(main.drain_outbox(conn, transport)) == 0
    assert transport.sent == []
    row = conn.execute("SELECT * FROM outbox WHERE id = ?",
                       (outbox_id,)).fetchone()
    assert row["sent_at"] is None
    assert row["attempts"] == 1
    assert "24-hour" in row["last_error"]


def test_the_same_message_goes_out_once_the_recipient_replies(conn):
    """The pending row is the recovery: no notification is lost, only held."""
    transport = MemoryTransport()
    db.queue_outbound(conn, OWNER, Reply(text="New booking"))
    assert asyncio.run(main.drain_outbox(conn, transport)) == 0

    db.touch_inbound(conn, OWNER)
    assert asyncio.run(main.drain_outbox(conn, transport)) == 1
    assert transport.sent[0][1].text == "New booking"


def test_a_booking_notice_carries_a_template_when_one_is_configured(
        conn, people, with_templates):
    """Wiring check: the owner's notice is the message the window kills."""
    from .conftest import say

    say(conn, OWNER, f"share {GUEST}")
    picked = say(conn, GUEST, f"free {OWNER} today").reply.rows[0]
    outcome = say(conn, GUEST, picked.id, tap=True)

    assert outcome.notify, "the owner should be notified"
    _phone, note = outcome.notify[0]
    assert note.template is not None
    assert note.template.kind == templates.BOOKING_CONFIRMED
