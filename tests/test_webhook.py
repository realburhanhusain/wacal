from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from wacal import db, main
from wacal.whatsapp import MemoryTransport

SECRET = "test-secret"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "APP_SECRET", SECRET)
    monkeypatch.setattr(main, "VERIFY_TOKEN", "tok")
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    db.create_user(main.app.state.conn, "+966500000002", "Ahmed", "Asia/Riyadh")
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def post(client, body: dict, *, sign: bool = True):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return client.post("/webhook", content=raw, headers=headers)


def message_body(text: str, message_id: str = "wamid.1") -> dict:
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "966500000002", "profile": {"name": "Ahmed"}}],
        "messages": [{"from": "966500000002", "id": message_id,
                      "type": "text", "text": {"body": text}}],
    }}]}]}


def test_verification_handshake(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe",
                                        "hub.verify_token": "tok",
                                        "hub.challenge": "42"})
    assert (ok.status_code, ok.text) == (200, "42")
    assert client.get("/webhook", params={"hub.mode": "subscribe",
                                          "hub.verify_token": "wrong"}
                      ).status_code == 403


def test_unsigned_request_is_rejected(client):
    assert post(client, message_body("help"), sign=False).status_code == 401
    assert main.app.state.transport.sent == []


def test_signed_message_gets_a_reply(client):
    assert post(client, message_body("help")).status_code == 200
    to, reply = main.app.state.transport.sent[0]
    # Meta's wa_id has no "+"; it is normalised so the same person is one user
    # row whichever transport they arrive on.
    assert to == "+966500000002"
    assert "free <name|phone>" in reply.text


def test_retry_of_the_same_message_id_is_ignored(client):
    post(client, message_body("help"))
    post(client, message_body("help"))
    assert len(main.app.state.transport.sent) == 1


def test_malformed_json_is_a_400(client):
    raw = b"{not json"
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    response = client.post("/webhook", content=raw,
                           headers={"X-Hub-Signature-256": f"sha256={digest}"})
    assert response.status_code == 400
