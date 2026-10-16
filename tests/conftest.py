from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wacal import db, gcal
from wacal.commands import handle
from wacal.gcal import ExternalCalendarError
from wacal.whatsapp import Inbound

RIYADH = "Asia/Riyadh"
# Thursday 2026-10-15, 06:00 UTC == 09:00 Asia/Riyadh.
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)

OWNER = "+966500000001"
GUEST = "+966500000002"


class FakeGoogle:
    """Stands in for GoogleCalendar. Records what the app asked it to do."""

    def __init__(self) -> None:
        self.busy_intervals: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.fail_busy = False
        self.fail_create = False
        self.fail_delete = False
        self.refresh_to: str | None = None
        self.next_event_id = "gcal-1"

    def _maybe_refresh(self, on_refresh):
        if self.refresh_to and on_refresh:
            on_refresh(self.refresh_to)
            self.refresh_to = None

    def busy(self, token_json, calendar_id, start, end, *, on_refresh=None):
        self._maybe_refresh(on_refresh)
        if self.fail_busy:
            raise ExternalCalendarError("fake outage")
        return list(self.busy_intervals)

    def create_event(self, token_json, calendar_id, *, start, end, title,
                     description="", location="", on_refresh=None):
        self._maybe_refresh(on_refresh)
        if self.fail_create:
            raise ExternalCalendarError("fake insert failure")
        self.created.append({"start": start, "end": end, "title": title,
                             "description": description, "location": location})
        return self.next_event_id

    def list_events(self, token_json, calendar_id, start, end, *, limit=50,
                    on_refresh=None):
        self._maybe_refresh(on_refresh)
        if self.fail_busy:
            raise ExternalCalendarError("fake outage")
        return [
            {"id": f"ext-{index}", "summary": "External meeting",
             "start": s, "end": e}
            for index, (s, e) in enumerate(self.busy_intervals)
        ]

    def delete_event(self, token_json, calendar_id, external_id, *,
                     on_refresh=None):
        self._maybe_refresh(on_refresh)
        if self.fail_delete:
            raise ExternalCalendarError("fake delete failure")
        self.deleted.append(external_id)


@pytest.fixture(autouse=True)
def secrets_configured(monkeypatch):
    """The app refuses to sign states or store tokens without keys."""
    from wacal import crypto

    monkeypatch.setenv("WACAL_STATE_SECRET", "test-state-secret")
    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def fake_google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gcal, "PROVIDER", fake)
    return fake


@pytest.fixture()
def people(conn):
    owner = db.create_user(conn, OWNER, "Burhan", RIYADH)
    guest = db.create_user(conn, GUEST, "Ahmed", RIYADH)
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
