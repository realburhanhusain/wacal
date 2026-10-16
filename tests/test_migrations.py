"""Upgrade an old database and then use it.

The schema has shipped three shapes. `CREATE TABLE IF NOT EXISTS` is silent
about a table that exists with the wrong columns, so a released database only
survives if migrate() handles it. These tests build the old shapes by hand,
upgrade them, and then drive a real booking through -- which is the part a
column-existence assertion would not catch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wacal import db
from wacal.business import handle_business
from wacal.commands import handle
from wacal.whatsapp import Inbound

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
SERVICES = [{"name": "Consultation", "duration_mins": 30, "price": 150}]

# wacal 0.1: no hosts, no calendar_links, events without host_slug/service/
# external_id, sessions keyed by phone alone, phones stored as bare digits.
V1_SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    phone         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'UTC',
    work_start    TEXT NOT NULL DEFAULT '09:00',
    work_end      TEXT NOT NULL DEFAULT '17:00',
    work_days     TEXT NOT NULL DEFAULT '0,1,2,3,4',
    slot_minutes  INTEGER NOT NULL DEFAULT 30,
    created_at    TEXT NOT NULL
);
CREATE TABLE grants (
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    viewer_id  INTEGER NOT NULL REFERENCES users(id),
    can_book   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, viewer_id)
);
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    ref          TEXT NOT NULL UNIQUE,
    owner_id     INTEGER NOT NULL REFERENCES users(id),
    created_by   INTEGER NOT NULL REFERENCES users(id),
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'confirmed',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX events_no_double_book
    ON events(owner_id, start_utc) WHERE status = 'confirmed';
CREATE TABLE seen_messages (
    message_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);
CREATE TABLE sessions (
    phone      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# wacal 0.2 added hosts, but without owner_email or the schedule columns.
V2_EXTRA = """
CREATE TABLE hosts (
    slug          TEXT PRIMARY KEY,
    owner_id      INTEGER NOT NULL REFERENCES users(id),
    business_name TEXT NOT NULL,
    location      TEXT NOT NULL DEFAULT '',
    services      TEXT NOT NULL DEFAULT '[]',
    currency      TEXT NOT NULL DEFAULT 'SAR',
    created_at    TEXT NOT NULL
);
ALTER TABLE events ADD COLUMN host_slug TEXT;
ALTER TABLE events ADD COLUMN service TEXT;
ALTER TABLE events ADD COLUMN external_id TEXT;
"""


@pytest.fixture()
def old_db(request):
    """A path holding a hand-built old database, cleaned up afterwards."""
    path = Path(__file__).resolve().parent / "legacy.db"

    def clean():
        for leftover in path.parent.glob("legacy.db*"):
            leftover.unlink(missing_ok=True)

    clean()
    request.addfinalizer(clean)
    return path


def build(path: Path, *scripts: str, rows=None) -> None:
    raw = sqlite3.connect(path)
    for script in scripts:
        raw.executescript(script)
    if rows:
        rows(raw)
    raw.commit()
    raw.close()


def seed_v1_rows(raw: sqlite3.Connection) -> None:
    # Bare digits, as the Meta parser used to store them.
    raw.execute(
        "INSERT INTO users (id, phone, display_name, timezone, work_days,"
        " created_at) VALUES (1, '966500000001', 'Burhan', 'Asia/Riyadh',"
        " '0,1,2,3,4,5,6', '2026-01-01T00:00:00+00:00')")
    raw.execute(
        "INSERT INTO users (id, phone, display_name, timezone, work_days,"
        " created_at) VALUES (2, '966500000002', 'Ahmed', 'Asia/Riyadh',"
        " '0,1,2,3,4,5,6', '2026-01-01T00:00:00+00:00')")
    raw.execute(
        "INSERT INTO grants (owner_id, viewer_id, can_book, created_at)"
        " VALUES (1, 2, 1, '2026-01-01T00:00:00+00:00')")
    raw.execute(
        "INSERT INTO events (ref, owner_id, created_by, start_utc, end_utc,"
        " title, created_at) VALUES ('OLD001', 1, 2, "
        "'2026-10-16T07:00:00+00:00', '2026-10-16T07:30:00+00:00',"
        " 'Legacy booking', '2026-01-01T00:00:00+00:00')")
    raw.execute(
        "INSERT INTO sessions (phone, kind, payload, expires_at)"
        " VALUES ('966500000002', 'rows', '[]', '2026-01-01T00:00:00+00:00')")


def test_a_v1_database_upgrades_and_still_works(old_db):
    build(old_db, V1_SCHEMA, rows=seed_v1_rows)

    applied: list[str] = []
    conn = db.connect(old_db, on_migrate=applied.extend)
    try:
        # Every column the current code needs now exists.
        columns = {row["name"]
                   for row in conn.execute("PRAGMA table_info(events)")}
        assert {"host_slug", "service", "external_id"} <= columns
        assert "scope" in {row["name"]
                           for row in conn.execute("PRAGMA table_info(sessions)")}
        assert conn.execute("SELECT count(*) AS n FROM hosts").fetchone()["n"] == 0
        assert any("users.phone" in step for step in applied)
        assert any("sessions" in step for step in applied)

        # The old rows survived, with phones normalised.
        assert db.get_user_by_phone(conn, "+966500000001")["display_name"] == "Burhan"
        legacy = db.get_event(conn, "OLD001")
        assert legacy["title"] == "Legacy booking"
        assert legacy["host_slug"] is None
        assert legacy["external_id"] is None
        assert db.grants_received(conn, 2)[0]["phone"] == "+966500000001"

        # And the upgraded database can take a new peer booking.
        out = handle(conn, Inbound(from_phone="+966500000002", text="free "
                                   "+966500000001 2026-10-16",
                                   message_id="m1", profile_name="Ahmed"),
                     now=NOW)
        assert out.reply.rows
        booked = handle(conn, Inbound(from_phone="+966500000002",
                                       text=out.reply.rows[0].id,
                                       message_id="m2", profile_name="Ahmed",
                                       is_tap=True), now=NOW)
        assert "Booked Burhan" in booked.reply.text

        # The legacy slot is still held: 07:00 UTC must not be offered again.
        offered = {row.title for row in out.reply.rows}
        assert "10:00-10:30" not in offered  # 07:00 UTC is 10:00 Riyadh
    finally:
        conn.close()


def test_a_v2_database_upgrades_and_business_mode_works(old_db):
    def seed(raw):
        seed_v1_rows(raw)
        raw.execute(
            "INSERT INTO hosts (slug, owner_id, business_name, location,"
            " services, currency, created_at) VALUES ('clinic', 1, 'Clinic',"
            " 'Road', ?, 'SAR', '2026-01-01T00:00:00+00:00')",
            (json.dumps(SERVICES),))

    build(old_db, V1_SCHEMA, V2_EXTRA, rows=seed)

    applied: list[str] = []
    conn = db.connect(old_db, on_migrate=applied.extend)
    try:
        host_columns = {row["name"]
                        for row in conn.execute("PRAGMA table_info(hosts)")}
        assert {"owner_email", "work_start", "work_end", "work_days",
                "slot_minutes"} <= host_columns
        assert any("hosts.work_start" in step for step in applied)

        host = db.get_host(conn, "clinic")
        assert host["business_name"] == "Clinic"
        # No host schedule, so it inherits the owner's.
        assert host["work_start"] is None

        out = handle_business(
            conn, host,
            Inbound(from_phone="+966500000055", text="hi", message_id="b1",
                    profile_name="Ravi"), now=NOW)
        assert [row.title for row in out.reply.rows] == ["Consultation"]
    finally:
        conn.close()


def test_migrating_twice_changes_nothing_more(old_db):
    build(old_db, V1_SCHEMA, rows=seed_v1_rows)
    first: list[str] = []
    conn = db.connect(old_db, on_migrate=first.extend)
    try:
        assert first  # something was applied
        assert db.migrate(conn) == []  # idempotent
    finally:
        conn.close()

    second: list[str] = []
    conn = db.connect(old_db, on_migrate=second.extend)
    try:
        assert second == []
    finally:
        conn.close()


def test_the_double_book_index_survives_the_upgrade(old_db):
    """The old index is kept, not silently dropped by the rebuild step."""
    build(old_db, V1_SCHEMA, rows=seed_v1_rows)
    conn = db.connect(old_db)
    try:
        owner = db.get_user_by_phone(conn, "+966500000001")
        start = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)
        with pytest.raises(db.SlotTaken):
            db.create_event(conn, owner["id"], owner["id"], start,
                            start + timedelta(minutes=30), "clash")
    finally:
        conn.close()
