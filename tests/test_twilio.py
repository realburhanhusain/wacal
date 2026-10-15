from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from wacal import db, main, numbering
from wacal.whatsapp import (
    CloudTransport,
    MemoryTransport,
    Reply,
    Row,
    TwilioTransport,
    build_transport,
    parse_twilio_form,
    render_numbered,
    twilio_signature,
    valid_twilio_signature,
)

TOKEN = "twilio-auth-token"
URL = "https://book.example.com/twilio/webhook/dr-sharma"
SERVICES = [{"name": "Consultation", "duration_mins": 30, "price": 150}]


# --- rendering -----------------------------------------------------------


def test_rows_render_as_a_numbered_menu():
    reply = Reply(text="Choose a service.", rows=[
        Row(id="b:svc:0", title="Consultation", description="30 min · 150 SAR"),
        Row(id="b:svc:1", title="Follow-up", description=""),
    ])
    body = render_numbered(reply)
    assert "1. Consultation - 30 min · 150 SAR" in body
    assert "2. Follow-up" in body
    assert body.rstrip().endswith("Reply with a number.")


def test_plain_text_is_unchanged():
    assert render_numbered(Reply(text="Cancelled ABC123.")) == "Cancelled ABC123."


def test_numbered_menu_is_capped_at_ten_rows():
    reply = Reply(text="x", rows=[Row(id=f"b:slot:0:{i}", title=f"t{i}")
                                  for i in range(25)])
    body = render_numbered(reply)
    assert "10. t9" in body
    assert "11. t10" not in body


def test_twilio_payload_shape():
    transport = TwilioTransport("AC123", TOKEN, "+14155238886")
    payload = transport.payload("+966500000055",
                                Reply(text="hi", rows=[Row(id="a", title="A")]))
    assert payload["From"] == "whatsapp:+14155238886"
    assert payload["To"] == "whatsapp:+966500000055"
    assert "1. A" in payload["Body"]


def test_twilio_from_accepts_either_form():
    assert TwilioTransport("AC1", TOKEN, "whatsapp:+1").from_number == "whatsapp:+1"
    assert TwilioTransport("AC1", TOKEN, "+1").from_number == "whatsapp:+1"


# --- transport selection -------------------------------------------------


def test_build_transport_prefers_cloud_then_twilio_then_memory(monkeypatch):
    for name in ("WA_TRANSPORT", "WA_PHONE_NUMBER_ID", "WA_TOKEN",
                 "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                 "TWILIO_WHATSAPP_FROM"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(build_transport(), MemoryTransport)

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+14155238886")
    assert isinstance(build_transport(), TwilioTransport)

    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WA_TOKEN", "t")
    assert isinstance(build_transport(), CloudTransport)

    monkeypatch.setenv("WA_TRANSPORT", "twilio")
    assert isinstance(build_transport(), TwilioTransport)


def test_forced_transport_without_credentials_fails_loudly(monkeypatch):
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                 "TWILIO_WHATSAPP_FROM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WA_TRANSPORT", "twilio")
    with pytest.raises(RuntimeError, match="TWILIO_ACCOUNT_SID"):
        build_transport()

    monkeypatch.setenv("WA_TRANSPORT", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="unknown WA_TRANSPORT"):
        build_transport()


# --- signatures ----------------------------------------------------------


def test_signature_matches_twilios_algorithm():
    params = {"From": "whatsapp:+1", "Body": "hi", "MessageSid": "SM1"}
    expected = base64.b64encode(hmac.new(
        TOKEN.encode(),
        (URL + "BodyhiFromwhatsapp:+1MessageSidSM1").encode(),
        hashlib.sha1,
    ).digest()).decode()
    assert twilio_signature(URL, params, TOKEN) == expected
    assert valid_twilio_signature(URL, params, expected, TOKEN)
    assert not valid_twilio_signature(URL, params, "wrong", TOKEN)
    assert not valid_twilio_signature(URL, params, None, TOKEN)
    # A different URL must not validate -- this is the proxy trap.
    assert not valid_twilio_signature(URL + "x", params, expected, TOKEN)


def test_absent_token_cannot_verify():
    assert valid_twilio_signature(URL, {}, None, "") is True


def test_parse_twilio_form():
    messages = parse_twilio_form({
        "From": "whatsapp:+966500000055", "Body": "  hi  ",
        "MessageSid": "SM123", "ProfileName": "Ravi",
    })
    assert len(messages) == 1
    assert messages[0].from_phone == "+966500000055"
    assert messages[0].text == "hi"
    assert messages[0].profile_name == "Ravi"
    assert messages[0].is_tap is False
    assert parse_twilio_form({"Body": "hi"}) == []


# --- numbering -----------------------------------------------------------


def test_typed_number_becomes_the_row_id(conn):
    from wacal.whatsapp import Inbound

    reply = Reply(text="pick", rows=[Row(id="b:svc:0", title="A"),
                                     Row(id="b:svc:1", title="B")])
    numbering.remember(conn, "+1", "peer", reply)

    resolved = numbering.resolve(conn, Inbound(from_phone="+1", text="2",
                                               message_id="m1"))
    assert resolved.text == "b:svc:1"
    assert resolved.is_tap is True


def test_out_of_range_number_keeps_the_list(conn):
    from wacal.whatsapp import Inbound

    numbering.remember(conn, "+1", "peer", Reply(text="pick",
                                         rows=[Row(id="b:svc:0", title="A")]))
    first = numbering.resolve(conn, Inbound(from_phone="+1", text="7",
                                            message_id="m1"))
    assert first.text == "7"
    # Still resolvable afterwards: the rows were not consumed.
    second = numbering.resolve(conn, Inbound(from_phone="+1", text="1",
                                             message_id="m2"))
    assert second.text == "b:svc:0"


def test_yes_and_no_map_to_confirm_and_restart(conn):
    from wacal.whatsapp import Inbound

    confirm = Reply(text="confirm?", rows=[
        Row(id="b:ok:0:1760000000", title="Confirm"),
        Row(id="b:restart:0", title="Start over"),
    ])
    numbering.remember(conn, "+1", "peer", confirm)
    assert numbering.resolve(conn, Inbound(from_phone="+1", text="YES",
                                           message_id="m1")).text \
        == "b:ok:0:1760000000"

    numbering.remember(conn, "+1", "peer", confirm)
    assert numbering.resolve(conn, Inbound(from_phone="+1", text="नहीं",
                                           message_id="m2")).text \
        == "b:restart:0"


def test_yes_without_a_confirm_row_is_left_alone(conn):
    from wacal.whatsapp import Inbound

    numbering.remember(conn, "+1", "peer", Reply(text="pick",
                                         rows=[Row(id="b:svc:0", title="A")]))
    assert numbering.resolve(conn, Inbound(from_phone="+1", text="yes",
                                           message_id="m1")).text == "yes"


def test_a_number_with_no_remembered_list_is_left_alone(conn):
    from wacal.whatsapp import Inbound

    assert numbering.resolve(conn, Inbound(from_phone="+1", text="3",
                                           message_id="m1")).text == "3"


# --- routes --------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "admin-key")
    monkeypatch.setattr(main, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_PUBLIC_URL", "")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    with TestClient(main.app, base_url="http://testserver") as test_client:
        yield test_client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def twilio_post(client, path: str, params: dict, *, sign: bool = True):
    url = f"http://testserver{path}"
    headers = {}
    if sign:
        headers["X-Twilio-Signature"] = twilio_signature(url, params, TOKEN)
    return client.post(path, data=params, headers=headers)


def test_unsigned_twilio_request_is_rejected(client):
    response = twilio_post(client, "/twilio/webhook",
                           {"From": "whatsapp:+1", "Body": "help",
                            "MessageSid": "SM1", "ProfileName": "Ravi"},
                           sign=False)
    assert response.status_code == 403
    assert main.app.state.transport.sent == []


def test_signed_twilio_peer_message_gets_help(client):
    response = twilio_post(client, "/twilio/webhook",
                           {"From": "whatsapp:+966500000001", "Body": "help",
                            "MessageSid": "SM2", "ProfileName": "Burhan"})
    assert response.status_code == 200
    _, reply = main.app.state.transport.sent[0]
    assert "free <name|phone>" in reply.text


def test_full_twilio_business_booking_by_number(client):
    conn = main.app.state.conn
    owner = db.create_user(conn, "+966500000001", "Dr Sharma", "Asia/Riyadh")
    db.create_host(conn, "dr-sharma", owner["id"], "Dr Sharma Clinic",
                   services=json.dumps(SERVICES), work_days="0,1,2,3,4,5,6",
                   work_start="09:00", work_end="17:00", slot_minutes=30)

    customer = {"From": "whatsapp:+966500000055", "ProfileName": "Ravi"}
    sent = main.app.state.transport.sent

    twilio_post(client, "/twilio/webhook/dr-sharma",
                {**customer, "Body": "hi", "MessageSid": "SM10"})
    assert "1. Consultation" in render_numbered(sent[-1][1])

    twilio_post(client, "/twilio/webhook/dr-sharma",
                {**customer, "Body": "1", "MessageSid": "SM11"})
    assert "Choose a date" in sent[-1][1].text

    twilio_post(client, "/twilio/webhook/dr-sharma",
                {**customer, "Body": "1", "MessageSid": "SM12"})
    assert "Open times" in sent[-1][1].text

    twilio_post(client, "/twilio/webhook/dr-sharma",
                {**customer, "Body": "1", "MessageSid": "SM13"})
    assert "Please confirm" in sent[-1][1].text

    twilio_post(client, "/twilio/webhook/dr-sharma",
                {**customer, "Body": "yes", "MessageSid": "SM14"})
    # Two messages go out: the customer's confirmation, then the owner's notice.
    assert sent[-2][0] == "+966500000055"
    assert "Confirmed" in sent[-2][1].text
    assert sent[-1][0] == "+966500000001"
    assert "New booking" in sent[-1][1].text
    assert conn.execute(
        "SELECT count(*) AS n FROM events WHERE status = 'confirmed'"
    ).fetchone()["n"] == 1


def test_twilio_retry_is_deduped(client):
    params = {"From": "whatsapp:+966500000001", "Body": "help",
              "MessageSid": "SM20", "ProfileName": "Burhan"}
    twilio_post(client, "/twilio/webhook", params)
    twilio_post(client, "/twilio/webhook", params)
    assert len(main.app.state.transport.sent) == 1


def test_twilio_unknown_host(client):
    twilio_post(client, "/twilio/webhook/ghost",
                {"From": "whatsapp:+1", "Body": "hi", "MessageSid": "SM30",
                 "ProfileName": "X"})
    assert "not active" in main.app.state.transport.sent[-1][1].text


def test_public_url_override_is_used_for_verification(client, monkeypatch):
    """Behind a TLS-terminating proxy, request.url is not what Twilio signed."""
    public = "https://book.example.com/twilio/webhook"
    monkeypatch.setenv("TWILIO_PUBLIC_URL", public)
    params = {"From": "whatsapp:+966500000001", "Body": "help",
              "MessageSid": "SM40", "ProfileName": "Burhan"}

    # Signed with the local URL: rejected, because the override is in force.
    local = twilio_signature("http://testserver/twilio/webhook", params, TOKEN)
    assert client.post("/twilio/webhook", data=params,
                       headers={"X-Twilio-Signature": local}).status_code == 403

    signature = twilio_signature(public, params, TOKEN)
    assert client.post("/twilio/webhook", data=params,
                       headers={"X-Twilio-Signature": signature}
                       ).status_code == 200


def test_root_endpoint_lists_every_webhook(client):
    body = client.get("/").json()
    assert body["service"] == "wacal"
    assert body["webhooks"]["twilio_business"] == "/twilio/webhook/{slug}"
