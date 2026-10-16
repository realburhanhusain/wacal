"""SQLite storage. This app owns the calendar, so these tables are the
source of truth for availability -- there is no external provider to sync with.

All instants are stored as UTC ISO-8601 strings ("2026-10-15T09:00:00+00:00")
so that lexical ordering matches chronological ordering in SQL.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import string
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("WACAL_DB", Path.home() / ".wacal" / "wacal.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    phone         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'UTC',
    work_start    TEXT NOT NULL DEFAULT '09:00',
    work_end      TEXT NOT NULL DEFAULT '17:00',
    work_days     TEXT NOT NULL DEFAULT '0,1,2,3,4',   -- Monday=0
    slot_minutes  INTEGER NOT NULL DEFAULT 30,
    created_at    TEXT NOT NULL
);

-- An owner must grant a viewer before that viewer can see free/busy.
-- This replaces the OAuth consent step a Google/Outlook build would need.
CREATE TABLE IF NOT EXISTS grants (
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    viewer_id  INTEGER NOT NULL REFERENCES users(id),
    can_book   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, viewer_id)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    ref          TEXT NOT NULL UNIQUE,
    owner_id     INTEGER NOT NULL REFERENCES users(id),
    created_by   INTEGER NOT NULL REFERENCES users(id),
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'confirmed',
    created_at   TEXT NOT NULL,
    -- Business mode only: which host and which service was booked.
    host_slug    TEXT REFERENCES hosts(slug),
    service      TEXT,
    -- Google Calendar event id, so a cancel here deletes it there too.
    external_id  TEXT
);

-- One row per user who connected an external calendar. The external calendar
-- is an extra busy source: its events block slots, and bookings made here are
-- mirrored into it so the user's own app shows them.
CREATE TABLE IF NOT EXISTS calendar_links (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id),
    provider     TEXT NOT NULL DEFAULT 'google',
    calendar_id  TEXT NOT NULL DEFAULT 'primary',
    token_json   TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT '',
    linked_at    TEXT NOT NULL
);

-- Business mode: a host is a user plus a public booking slug and a service
-- menu. Anyone holding the link can book, so no grant is required.
-- Opening hours live on the host, not the owner, so one person can run two
-- businesses with different schedules. NULL means "inherit the owner's".
CREATE TABLE IF NOT EXISTS hosts (
    slug          TEXT PRIMARY KEY,
    owner_id      INTEGER NOT NULL REFERENCES users(id),
    business_name TEXT NOT NULL,
    location      TEXT NOT NULL DEFAULT '',
    services      TEXT NOT NULL DEFAULT '[]',
    currency      TEXT NOT NULL DEFAULT 'SAR',
    created_at    TEXT NOT NULL,
    owner_email   TEXT NOT NULL DEFAULT '',
    work_start    TEXT,
    work_end      TEXT,
    work_days     TEXT,
    slot_minutes  INTEGER
);

-- Stops a double-tapped WhatsApp button from creating two identical bookings.
CREATE UNIQUE INDEX IF NOT EXISTS events_no_double_book
    ON events(owner_id, start_utc) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS events_by_owner_window
    ON events(owner_id, start_utc, end_utc);

CREATE INDEX IF NOT EXISTS events_by_host
    ON events(host_slug, start_utc);

CREATE INDEX IF NOT EXISTS events_by_booker
    ON events(created_by, end_utc);

-- Meta retries a webhook until it gets a 200, so the same message id can
-- arrive several times. Seen ids are rejected before any booking happens.
CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);

-- Short-lived conversation state. Today it holds the row ids last offered
-- to a number, so a typed "3" can stand in for tapping row 3.
CREATE TABLE IF NOT EXISTS sessions (
    phone      TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'peer',
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (phone, scope)
);

-- Every outbound message is queued here first and marked sent only once the
-- provider accepts it. Before this, a failed send to the customer skipped the
-- owner's notification entirely and left no trace of a confirmed booking.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    phone      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent_at, id);

-- OAuth state tokens are single use. Without this a replayed or forged state
-- lets someone attach their own Google account to another user's row.
CREATE TABLE IF NOT EXISTS oauth_states (
    nonce      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""

REF_ALPHABET = string.ascii_uppercase + string.digits


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Normalise any aware datetime to a UTC ISO string."""
    return dt.astimezone(timezone.utc).isoformat()


def new_ref() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(6))


# One process-wide connection is shared across request threads, so every
# statement is serialised through this lock. Use LOCK around a read-then-write
# sequence too, or two requests can both see a slot as free. See main.py.
#
# It is load-bearing twice over: without it, concurrent writers on one
# connection also raise "cannot start a transaction within a transaction",
# because tx() issues BEGIN IMMEDIATE on a connection another thread has
# already opened a transaction on. tests/mutation_check.py demonstrates both.
LOCK = threading.RLock()


def connect(path: Path | str | None = None, *,
            on_migrate=None) -> sqlite3.Connection:
    """Open the database, applying any pending migrations.

    on_migrate receives the list of steps applied, so a caller can report an
    upgrade without calling migrate() a second time and always seeing nothing.
    """
    target = Path(path) if path else DEFAULT_DB
    if target != Path(":memory:"):
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    applied = migrate(conn)
    if on_migrate is not None:
        on_migrate(applied)
    return conn


# Columns added after the first release. A database created by an older build
# keeps its rows; CREATE TABLE IF NOT EXISTS would not have added these.
ADDED_COLUMNS = {
    "events": [
        ("host_slug", "TEXT"),
        ("service", "TEXT"),
        ("external_id", "TEXT"),
    ],
    "hosts": [
        ("owner_email", "TEXT NOT NULL DEFAULT ''"),
        ("work_start", "TEXT"),
        ("work_end", "TEXT"),
        ("work_days", "TEXT"),
        ("slot_minutes", "INTEGER"),
    ],
}


# Tables whose shape changed incompatibly. These hold only ephemeral state,
# so recreating them is cheaper and safer than rewriting rows in place.
REBUILT_TABLES = {
    "sessions": ("scope", """
        CREATE TABLE sessions (
            phone      TEXT NOT NULL,
            scope      TEXT NOT NULL DEFAULT 'peer',
            kind       TEXT NOT NULL,
            payload    TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (phone, scope)
        )"""),
}


def migrate(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, (required_column, create_sql) in REBUILT_TABLES.items():
        present = {row["name"]
                   for row in conn.execute(f"PRAGMA table_info({table})")}
        if present and required_column not in present:
            conn.execute(f"DROP TABLE {table}")
            conn.execute(create_sql)
            applied.append(f"{table} (rebuilt)")

    # One canonical phone shape. Rows written before normalisation existed are
    # bare digits, which makes the same person two users across transports.
    bare = conn.execute(
        "SELECT count(*) AS n FROM users WHERE phone NOT LIKE '+%'"
    ).fetchone()["n"]
    if bare:
        with tx(conn):
            conn.execute("UPDATE users SET phone = '+' || phone"
                         " WHERE phone NOT LIKE '+%'")
        applied.append(f"users.phone normalised ({bare} rows)")
    for table, columns in ADDED_COLUMNS.items():
        present = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                applied.append(f"{table}.{name}")
    return applied


@contextmanager
def tx(conn: sqlite3.Connection):
    with LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


# --- users ---------------------------------------------------------------


def get_user_by_phone(conn, phone: str):
    return conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()


def get_user(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user(conn, phone: str, display_name: str, tz: str = "UTC"):
    with tx(conn):
        conn.execute(
            "INSERT INTO users (phone, display_name, timezone, created_at)"
            " VALUES (?, ?, ?, ?)",
            (phone, display_name, tz, iso(now_utc())),
        )
    return get_user_by_phone(conn, phone)


def update_user(conn, user_id: int, **fields):
    allowed = {"display_name", "timezone", "work_start", "work_end",
               "work_days", "slot_minutes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx(conn):
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?",
                     (*fields.values(), user_id))
    return get_user(conn, user_id)


def find_user(conn, needle: str):
    """Resolve a phone number or a display name (case-insensitive, unique match).

    Returns (row, error). A name matching several users is an error, not a guess.
    """
    digits = "".join(c for c in needle if c.isdigit())
    if digits:
        row = conn.execute(
            "SELECT * FROM users WHERE phone = ? OR phone = ?",
            (digits, "+" + digits),
        ).fetchone()
        if row:
            return row, None
    rows = conn.execute(
        "SELECT * FROM users WHERE lower(display_name) = lower(?)", (needle,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0], None
    if len(rows) > 1:
        return None, f"several people are called {needle}; use their phone number"
    return None, f"nobody here matches {needle}"


# --- grants --------------------------------------------------------------


def grant(conn, owner_id: int, viewer_id: int, can_book: bool = True):
    with tx(conn):
        conn.execute(
            "INSERT INTO grants (owner_id, viewer_id, can_book, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(owner_id, viewer_id) DO UPDATE SET can_book = excluded.can_book",
            (owner_id, viewer_id, int(can_book), iso(now_utc())),
        )


def revoke(conn, owner_id: int, viewer_id: int) -> bool:
    with tx(conn):
        cur = conn.execute(
            "DELETE FROM grants WHERE owner_id = ? AND viewer_id = ?",
            (owner_id, viewer_id),
        )
    return cur.rowcount > 0


def get_grant(conn, owner_id: int, viewer_id: int):
    if owner_id == viewer_id:
        return {"can_book": 1}  # you can always see and book your own calendar
    return conn.execute(
        "SELECT * FROM grants WHERE owner_id = ? AND viewer_id = ?",
        (owner_id, viewer_id),
    ).fetchone()


def grants_given(conn, owner_id: int):
    return conn.execute(
        "SELECT u.display_name, u.phone, g.can_book FROM grants g"
        " JOIN users u ON u.id = g.viewer_id WHERE g.owner_id = ?"
        " ORDER BY u.display_name",
        (owner_id,),
    ).fetchall()


def grants_received(conn, viewer_id: int):
    return conn.execute(
        "SELECT u.display_name, u.phone, g.can_book FROM grants g"
        " JOIN users u ON u.id = g.owner_id WHERE g.viewer_id = ?"
        " ORDER BY u.display_name",
        (viewer_id,),
    ).fetchall()


# --- events --------------------------------------------------------------


def busy_intervals(conn, owner_id: int, start: datetime, end: datetime):
    """Confirmed events for one owner that overlap [start, end).

    Returns (start_utc, end_utc) strings only -- never titles. Free/busy for
    someone else's calendar must not leak what the meeting is about.
    """
    rows = conn.execute(
        "SELECT start_utc, end_utc FROM events"
        " WHERE owner_id = ? AND status = 'confirmed'"
        "   AND start_utc < ? AND end_utc > ?"
        " ORDER BY start_utc",
        (owner_id, iso(end), iso(start)),
    ).fetchall()
    return [(r["start_utc"], r["end_utc"]) for r in rows]


class SlotTaken(Exception):
    """That owner already has a confirmed event starting at that instant."""


class RefCollision(Exception):
    """Two events drew the same short ref. Retried internally."""


# SQLite reports a unique index violation by column list, not by index name:
# "UNIQUE constraint failed: events.owner_id, events.start_utc".
DOUBLE_BOOK_MESSAGE = "events.owner_id, events.start_utc"
REF_COLLISION_MESSAGE = "events.ref"
REF_ATTEMPTS = 5


def create_event(conn, owner_id: int, created_by: int, start: datetime,
                 end: datetime, title: str, *, host_slug: str | None = None,
                 service: str | None = None):
    for attempt in range(REF_ATTEMPTS):
        try:
            return _insert_event(conn, owner_id, created_by, start, end, title,
                                 host_slug, service)
        except RefCollision:
            # Six characters of 36 collide often enough to matter past a few
            # thousand events; a fresh ref is all that is needed.
            if attempt == REF_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


def _insert_event(conn, owner_id, created_by, start, end, title, host_slug,
                  service):
    ref = new_ref()
    try:
        with tx(conn):
            conn.execute(
                "INSERT INTO events (ref, owner_id, created_by, start_utc,"
                " end_utc, title, created_at, host_slug, service)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ref, owner_id, created_by, iso(start), iso(end), title,
                 iso(now_utc()), host_slug, service),
            )
    except sqlite3.IntegrityError as exc:
        if DOUBLE_BOOK_MESSAGE in str(exc):
            raise SlotTaken(iso(start)) from exc
        if REF_COLLISION_MESSAGE in str(exc):
            raise RefCollision(ref) from exc
        raise
    return conn.execute("SELECT * FROM events WHERE ref = ?", (ref,)).fetchone()


def get_event(conn, ref: str):
    return conn.execute("SELECT * FROM events WHERE ref = ?", (ref.upper(),)).fetchone()


def cancel_event(conn, ref: str) -> bool:
    with tx(conn):
        cur = conn.execute(
            "UPDATE events SET status = 'cancelled'"
            " WHERE ref = ? AND status = 'confirmed'",
            (ref.upper(),),
        )
    return cur.rowcount > 0


def events_for(conn, user_id: int, since: datetime, limit: int = 20):
    """Upcoming events the user owns or booked."""
    return conn.execute(
        "SELECT e.*, o.display_name AS owner_name, c.display_name AS booker_name"
        " FROM events e"
        " JOIN users o ON o.id = e.owner_id"
        " JOIN users c ON c.id = e.created_by"
        " WHERE (e.owner_id = ? OR e.created_by = ?)"
        "   AND e.status = 'confirmed' AND e.end_utc > ?"
        " ORDER BY e.start_utc LIMIT ?",
        (user_id, user_id, iso(since), limit),
    ).fetchall()


def set_external_id(conn, ref: str, external_id: str) -> None:
    with tx(conn):
        conn.execute("UPDATE events SET external_id = ? WHERE ref = ?",
                     (external_id, ref.upper()))


# --- calendar links ------------------------------------------------------


def get_calendar_link(conn, user_id: int):
    return conn.execute(
        "SELECT * FROM calendar_links WHERE user_id = ?", (user_id,)
    ).fetchone()


def save_calendar_link(conn, user_id: int, token_json: str, *,
                       provider: str = "google", calendar_id: str = "primary",
                       account: str = ""):
    with tx(conn):
        conn.execute(
            "INSERT INTO calendar_links (user_id, provider, calendar_id,"
            " token_json, account, linked_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET provider = excluded.provider,"
            " calendar_id = excluded.calendar_id, token_json = excluded.token_json,"
            " account = excluded.account, linked_at = excluded.linked_at",
            (user_id, provider, calendar_id, token_json, account, iso(now_utc())),
        )


def delete_calendar_link(conn, user_id: int) -> bool:
    with tx(conn):
        cur = conn.execute("DELETE FROM calendar_links WHERE user_id = ?",
                           (user_id,))
    return cur.rowcount > 0


# --- hosts (business mode) ----------------------------------------------


def get_host(conn, slug: str):
    return conn.execute("SELECT * FROM hosts WHERE slug = ?",
                        (slug.lower(),)).fetchone()


def hosts_of(conn, owner_id: int):
    return conn.execute(
        "SELECT * FROM hosts WHERE owner_id = ? ORDER BY slug", (owner_id,)
    ).fetchall()


def create_host(conn, slug: str, owner_id: int, business_name: str, *,
                location: str = "", services: str = "[]",
                currency: str = "SAR", owner_email: str = "",
                work_start: str | None = None, work_end: str | None = None,
                work_days: str | None = None,
                slot_minutes: int | None = None):
    with tx(conn):
        conn.execute(
            "INSERT INTO hosts (slug, owner_id, business_name, location,"
            " services, currency, created_at, owner_email, work_start,"
            " work_end, work_days, slot_minutes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug.lower(), owner_id, business_name, location, services,
             currency, iso(now_utc()), owner_email, work_start, work_end,
             work_days, slot_minutes),
        )
    return get_host(conn, slug)


def update_host(conn, slug: str, **fields):
    allowed = {"business_name", "location", "services", "currency",
               "owner_email", "work_start", "work_end", "work_days",
               "slot_minutes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx(conn):
        conn.execute(f"UPDATE hosts SET {sets} WHERE slug = ?",
                     (*fields.values(), slug.lower()))
    return get_host(conn, slug)


def host_bookings(conn, slug: str, since: datetime, until: datetime):
    return conn.execute(
        "SELECT e.*, c.display_name AS customer_name, c.phone AS customer_phone"
        " FROM events e JOIN users c ON c.id = e.created_by"
        " WHERE e.host_slug = ? AND e.status = 'confirmed'"
        "   AND e.start_utc >= ? AND e.start_utc < ?"
        " ORDER BY e.start_utc",
        (slug.lower(), iso(since), iso(until)),
    ).fetchall()


def host_stats(conn, slug: str, since: datetime):
    row = conn.execute(
        "SELECT"
        "   sum(status = 'confirmed') AS confirmed,"
        "   sum(status = 'cancelled') AS cancelled,"
        "   count(DISTINCT created_by) AS customers"
        " FROM events WHERE host_slug = ? AND created_at >= ?",
        (slug.lower(), iso(since)),
    ).fetchone()
    by_service = conn.execute(
        "SELECT service, count(*) AS n FROM events"
        " WHERE host_slug = ? AND status = 'confirmed' AND created_at >= ?"
        " GROUP BY service ORDER BY n DESC",
        (slug.lower(), iso(since)),
    ).fetchall()
    return row, by_service


def customer_bookings(conn, user_id: int, slug: str, since: datetime):
    return conn.execute(
        "SELECT * FROM events WHERE created_by = ? AND host_slug = ?"
        "   AND status = 'confirmed' AND end_utc > ?"
        " ORDER BY start_utc",
        (user_id, slug.lower(), iso(since)),
    ).fetchall()


def already_seen(conn, message_id: str) -> bool:
    if not message_id:
        return False
    return conn.execute(
        "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
    ).fetchone() is not None


def mark_seen(conn, message_id: str) -> bool:
    """Record a message id. False if it was already there.

    Call this only once the message has been handled. Marking it first means a
    crash mid-handling is remembered as done, and the provider's retry -- the
    one chance to recover -- is discarded.
    """
    if not message_id:
        return True
    try:
        with tx(conn):
            conn.execute(
                "INSERT INTO seen_messages (message_id, seen_at) VALUES (?, ?)",
                (message_id, iso(now_utc())),
            )
    except sqlite3.IntegrityError:
        return False
    return True


# --- sessions ------------------------------------------------------------


def put_session(conn, phone: str, scope: str, kind: str, payload: str,
                ttl_seconds: int = 3600):
    from datetime import timedelta

    expires = now_utc() + timedelta(seconds=ttl_seconds)
    with tx(conn):
        conn.execute(
            "INSERT INTO sessions (phone, scope, kind, payload, expires_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(phone, scope) DO UPDATE SET kind = excluded.kind,"
            " payload = excluded.payload, expires_at = excluded.expires_at",
            (phone, scope, kind, payload, iso(expires)),
        )


def pop_session(conn, phone: str, scope: str):
    row = conn.execute(
        "SELECT * FROM sessions WHERE phone = ? AND scope = ?", (phone, scope)
    ).fetchone()
    if row is None:
        return None
    with tx(conn):
        conn.execute("DELETE FROM sessions WHERE phone = ? AND scope = ?",
                     (phone, scope))
    if row["expires_at"] < iso(now_utc()):
        return None
    return row


# --- oauth states --------------------------------------------------------


def create_oauth_state(conn, nonce: str, user_id: int) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO oauth_states (nonce, user_id, created_at)"
            " VALUES (?, ?, ?)",
            (nonce, user_id, iso(now_utc())),
        )


def consume_oauth_state(conn, nonce: str, user_id: int) -> bool:
    """True once per nonce. A forged or replayed state has no row to consume."""
    with tx(conn):
        cur = conn.execute(
            "DELETE FROM oauth_states WHERE nonce = ? AND user_id = ?",
            (nonce, user_id),
        )
    return cur.rowcount > 0


# --- outbox --------------------------------------------------------------


def queue_outbound(conn, phone: str, reply) -> int:
    """Persist one outbound message. Returns its outbox id."""
    import json as _json

    payload = _json.dumps({
        "text": reply.text,
        "button": reply.button,
        "header": reply.header,
        "rows": [{"id": row.id, "title": row.title,
                  "description": row.description} for row in reply.rows],
    }, ensure_ascii=False)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO outbox (phone, payload, created_at) VALUES (?, ?, ?)",
            (phone, payload, iso(now_utc())),
        )
    return cur.lastrowid


def reply_from_row(row):
    """Rebuild a Reply from a queued payload."""
    import json as _json

    from .whatsapp import Reply, Row

    data = _json.loads(row["payload"])
    return Reply(
        text=data["text"],
        rows=[Row(id=r["id"], title=r["title"], description=r["description"])
              for r in data.get("rows", [])],
        button=data.get("button", "Pick a slot"),
        header=data.get("header", ""),
    )


def pending_outbound(conn, *, limit: int = 50, only: list[int] | None = None):
    if only is not None:
        if not only:
            return []
        marks = ",".join("?" for _ in only)
        return conn.execute(
            f"SELECT * FROM outbox WHERE sent_at IS NULL AND id IN ({marks})"
            " ORDER BY id", only).fetchall()
    return conn.execute(
        "SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def mark_outbound_sent(conn, outbox_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?",
                     (iso(now_utc()), outbox_id))


def record_outbound_failure(conn, outbox_id: int, error: str = "") -> None:
    with tx(conn):
        conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ?"
            " WHERE id = ?", (error[:500], outbox_id))


# --- housekeeping --------------------------------------------------------


def prune(conn, *, days: int = 7) -> dict[str, int]:
    """Drop expired conversation state and old dedupe rows.

    Nothing else deletes from these tables, so without this they grow forever.
    """
    from datetime import timedelta

    cutoff = iso(now_utc() - timedelta(days=days))
    now = iso(now_utc())
    with tx(conn):
        seen = conn.execute("DELETE FROM seen_messages WHERE seen_at < ?",
                            (cutoff,)).rowcount
        sessions = conn.execute("DELETE FROM sessions WHERE expires_at < ?",
                                (now,)).rowcount
        states = conn.execute("DELETE FROM oauth_states WHERE created_at < ?",
                              (cutoff,)).rowcount
        outbox = conn.execute(
            "DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at < ?",
            (cutoff,)).rowcount
    return {"seen_messages": seen, "sessions": sessions,
            "oauth_states": states, "outbox": outbox}
