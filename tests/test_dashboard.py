from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from wacal import db, main
from wacal.whatsapp import MemoryTransport

KEY = "admin-key"
SECRET = "test-secret"

HOST_BODY = {
    "slug": "dr-sharma",
    "business_name": "Dr Sharma Clinic",
    "owner_phone": "+966500000001",
    "owner_name": "Dr Sharma",
    "location": "123 King Fahd Road",
    "timezone": "Asia/Riyadh",
    "currency": "SAR",
    "services": [
        {"name": "Consultation", "duration_mins": 30, "price": 150},
        {"name": "Follow-up", "duration_mins": 15, "price": 50},
    ],
    "working_days": [0, 1, 2, 3, 4, 5, 6],
    "working_hours": {"start": "09:00", "end": "17:00"},
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", KEY)
    monkeypatch.setenv("BASE_URL", "https://book.example.com")
    monkeypatch.setattr(main, "APP_SECRET", SECRET)
    main.app.state.conn = db.connect(":memory:")
    main.app.state.transport = MemoryTransport()
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.state.conn.close()
    main.app.state.conn = None
    main.app.state.transport = None


def admin(client, method: str, path: str, *, key: str | None = KEY, **kwargs):
    headers = {"x-api-key": key} if key is not None else {}
    return client.request(method, path, headers=headers, **kwargs)


def post_webhook(client, path: str, text: str, message_id: str,
                 phone: str = "+966500000055", name: str = "Ravi"):
    body = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": phone, "profile": {"name": name}}],
        "messages": [{"from": phone, "id": message_id, "type": "text",
                      "text": {"body": text}}],
    }}]}]}
    raw = json.dumps(body).encode()
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(path, content=raw, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    })


def test_missing_admin_key_closes_the_api(client, monkeypatch):
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    response = admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    assert response.status_code == 503
    assert "ADMIN_KEY is not set" in response.json()["detail"]


def test_wrong_and_absent_keys_are_rejected(client):
    assert admin(client, "POST", "/dashboard/register", key="nope",
                 json=HOST_BODY).status_code == 401
    assert admin(client, "POST", "/dashboard/register", key=None,
                 json=HOST_BODY).status_code == 401


def test_register_creates_host_and_owner(client):
    response = admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    assert response.status_code == 201
    assert response.json()["webhook_url"] == \
        "https://book.example.com/webhook/dr-sharma"

    conn = main.app.state.conn
    owner = db.get_user_by_phone(conn, "+966500000001")
    assert owner["timezone"] == "Asia/Riyadh"
    assert owner["work_days"] == "0,1,2,3,4,5,6"
    assert owner["work_start"] == "09:00"
    host = db.get_host(conn, "dr-sharma")
    assert len(json.loads(host["services"])) == 2


def test_duplicate_slug_is_a_conflict(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    assert admin(client, "POST", "/dashboard/register",
                 json=HOST_BODY).status_code == 409


def test_invalid_payloads_are_rejected(client):
    bad_slug = {**HOST_BODY, "slug": "Dr Sharma!"}
    assert admin(client, "POST", "/dashboard/register",
                 json=bad_slug).status_code == 422

    bad_day = {**HOST_BODY, "slug": "x1", "working_days": [9]}
    assert admin(client, "POST", "/dashboard/register",
                 json=bad_day).status_code == 422

    bad_hours = {**HOST_BODY, "slug": "x2",
                 "working_hours": {"start": "nine", "end": "17:00"}}
    assert admin(client, "POST", "/dashboard/register",
                 json=bad_hours).status_code == 422

    bad_duration = {**HOST_BODY, "slug": "x3",
                    "services": [{"name": "Too long", "duration_mins": 999}]}
    assert admin(client, "POST", "/dashboard/register",
                 json=bad_duration).status_code == 422


def test_patch_replaces_the_service_menu(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    response = admin(client, "PATCH", "/dashboard/dr-sharma", json={
        "services": [{"name": "Vaccination", "duration_mins": 20, "price": 90}],
        "location": "New wing",
    })
    assert response.status_code == 200
    host = db.get_host(main.app.state.conn, "dr-sharma")
    assert [s["name"] for s in json.loads(host["services"])] == ["Vaccination"]
    assert host["location"] == "New wing"


def test_patch_with_nothing_to_change_is_a_400(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    assert admin(client, "PATCH", "/dashboard/dr-sharma",
                 json={}).status_code == 400


def test_unknown_slug_is_a_404(client):
    assert admin(client, "GET", "/dashboard/nope/bookings").status_code == 404
    assert admin(client, "GET", "/dashboard/nope/stats").status_code == 404
    assert admin(client, "PATCH", "/dashboard/nope",
                 json={"location": "x"}).status_code == 404


def test_bookings_and_stats_reflect_a_real_booking(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)

    # Walk the customer through the booking flow over the host webhook.
    menu = post_webhook(client, "/webhook/dr-sharma", "hi", "wamid.b1")
    assert menu.status_code == 200
    sent = main.app.state.transport.sent
    service_row = sent[-1][1].rows[0]

    for index, payload in enumerate([service_row.id], start=2):
        post_webhook(client, "/webhook/dr-sharma", payload, f"wamid.b{index}")
    date_row = main.app.state.transport.sent[-1][1].rows[0]
    post_webhook(client, "/webhook/dr-sharma", date_row.id, "wamid.b3")
    slot_row = main.app.state.transport.sent[-1][1].rows[0]
    post_webhook(client, "/webhook/dr-sharma", slot_row.id, "wamid.b4")
    confirm_row = main.app.state.transport.sent[-1][1].rows[0]
    post_webhook(client, "/webhook/dr-sharma", confirm_row.id, "wamid.b5")

    bookings = admin(client, "GET", "/dashboard/dr-sharma/bookings").json()
    assert bookings["count"] == 1
    booking = bookings["bookings"][0]
    assert booking["service"] == "Consultation"
    assert booking["customer_name"] == "Ravi"
    assert booking["mirrored_to_google"] is False

    stats = admin(client, "GET", "/dashboard/dr-sharma/stats").json()
    assert stats["confirmed"] == 1
    assert stats["cancelled"] == 0
    assert stats["distinct_customers"] == 1
    assert stats["cancellation_rate"] == 0.0
    assert stats["by_service"] == [{"service": "Consultation", "count": 1}]


def test_unknown_host_webhook_answers_without_booking(client):
    response = post_webhook(client, "/webhook/ghost", "hi", "wamid.g1")
    assert response.status_code == 200
    to, reply = main.app.state.transport.sent[-1]
    assert "not active" in reply.text


def test_host_status_endpoint(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    body = client.get("/webhook/dr-sharma/status").json()
    assert body["services"] == 2
    assert body["google_calendar_connected"] is False
    assert body["timezone"] == "Asia/Riyadh"
    assert client.get("/webhook/ghost/status").status_code == 404


def test_peer_and_business_webhooks_stay_separate(client):
    admin(client, "POST", "/dashboard/register", json=HOST_BODY)
    # Same customer, peer webhook: no host context, so peer help is returned.
    post_webhook(client, "/webhook", "help", "wamid.p1")
    _, reply = main.app.state.transport.sent[-1]
    assert "free <name|phone>" in reply.text


def test_routers_work_without_a_pre_seeded_connection(monkeypatch):
    """The lifespan must open the database before the first dashboard request.

    Pre-seeding app.state.conn in the other fixtures hid this: on a real
    server the dashboard routes ran with conn = None and returned a 500.
    """
    from pathlib import Path

    monkeypatch.setenv("ADMIN_KEY", KEY)
    monkeypatch.setattr(db, "DEFAULT_DB", Path(":memory:"))
    main.app.state.conn = None
    main.app.state.transport = None
    try:
        with TestClient(main.app) as fresh:
            response = fresh.post("/dashboard/register", headers={"x-api-key": KEY},
                                  json=HOST_BODY)
            assert response.status_code == 201
            assert fresh.get("/webhook/dr-sharma/status").status_code == 200
    finally:
        if main.app.state.conn is not None:
            main.app.state.conn.close()
        main.app.state.conn = None
        main.app.state.transport = None


def test_oauth_callback_rejects_a_bad_state(client):
    response = client.get("/auth/google/callback",
                          params={"state": "forged", "code": "x"})
    assert response.status_code == 200
    assert "Link expired" in response.text


def test_oauth_callback_stores_the_link(client, monkeypatch):
    from wacal import gcal

    conn = main.app.state.conn
    user = db.create_user(conn, "+966500000001", "Burhan", "Asia/Riyadh")
    monkeypatch.setattr(gcal, "exchange_code",
                        lambda code: ('{"token": "t"}', "burhan@example.com"))
    state = gcal.sign_state(user["id"])

    response = client.get("/auth/google/callback",
                          params={"state": state, "code": "auth-code"})
    assert "Calendar connected" in response.text
    assert db.get_calendar_link(conn, user["id"])["account"] == \
        "burhan@example.com"


def test_oauth_callback_reports_a_denied_consent(client):
    response = client.get("/auth/google/callback",
                          params={"error": "access_denied"})
    assert "access_denied" in response.text
