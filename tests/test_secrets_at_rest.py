"""The writer lock and the encryption of calendar tokens."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from wacal import crypto, db
from wacal.singleton import AlreadyRunning, WriterLock, guard, lock_path

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def scratch(request):
    path = Path(__file__).resolve().parent / "guard.db"

    def clean():
        for leftover in path.parent.glob("guard.db*"):
            leftover.unlink(missing_ok=True)

    clean()
    request.addfinalizer(clean)
    return path


# --- one writer per database --------------------------------------------


def test_a_second_holder_is_refused(scratch):
    first = WriterLock(scratch)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning, match="already holds"):
            WriterLock(scratch).acquire()
    finally:
        first.release()


def test_the_lock_is_reusable_after_release(scratch):
    # Releasing a lock that was never acquired is a no-op, not an error.
    WriterLock(scratch).release()
    first = WriterLock(scratch)
    first.acquire()
    first.release()
    second = WriterLock(scratch)
    second.acquire()
    second.release()


def test_a_second_process_is_refused(scratch):
    """The case that matters: uvicorn --workers 2."""
    holder = WriterLock(scratch)
    holder.acquire()
    try:
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from wacal.singleton import WriterLock, AlreadyRunning
            try:
                WriterLock({str(scratch)!r}).acquire()
            except AlreadyRunning:
                print("refused")
            else:
                print("acquired")
        """)
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=60)
        assert result.stdout.strip() == "refused", result.stderr
    finally:
        holder.release()


def test_the_holding_pid_is_recorded_for_an_operator(scratch):
    import os

    from wacal.singleton import pid_path

    lock = WriterLock(scratch)
    lock.acquire()
    try:
        assert lock_path(scratch).exists()
        assert pid_path(scratch).read_text() == str(os.getpid())
    finally:
        lock.release()
    # Cleared on a clean release, so a stale pid cannot mislead.
    assert not pid_path(scratch).exists()


def test_the_guard_can_be_opted_out_of(scratch, monkeypatch):
    monkeypatch.setenv("WACAL_ALLOW_MULTIPROCESS", "1")
    assert guard(scratch) is None
    # And nothing is left behind to confuse the next run.
    assert not lock_path(scratch).exists()


def test_an_in_memory_database_needs_no_guard():
    assert guard(":memory:") is None


# --- calendar tokens at rest --------------------------------------------


def test_round_trip(monkeypatch):
    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())
    secret = json.dumps({"refresh_token": "1//abcdEFGH"})
    stored = crypto.encrypt(secret)

    assert crypto.is_encrypted(stored)
    assert "abcdEFGH" not in stored
    assert crypto.decrypt(stored) == secret


def test_storing_without_a_key_is_refused(monkeypatch):
    monkeypatch.delenv("WACAL_TOKEN_KEY", raising=False)
    monkeypatch.delenv("WACAL_ALLOW_PLAINTEXT_TOKENS", raising=False)
    with pytest.raises(crypto.NotConfigured, match="in the clear"):
        crypto.encrypt("secret")


def test_plaintext_can_be_allowed_explicitly(monkeypatch):
    monkeypatch.delenv("WACAL_TOKEN_KEY", raising=False)
    monkeypatch.setenv("WACAL_ALLOW_PLAINTEXT_TOKENS", "1")
    assert crypto.encrypt("secret") == "secret"
    assert crypto.decrypt("secret") == "secret"


def test_a_legacy_plaintext_row_is_still_readable(conn, monkeypatch):
    """A database written before encryption existed must keep working."""
    monkeypatch.setenv("WACAL_ALLOW_PLAINTEXT_TOKENS", "1")
    monkeypatch.delenv("WACAL_TOKEN_KEY", raising=False)
    user = db.create_user(conn, "+966500000001", "Burhan", "Asia/Riyadh")
    db.save_calendar_link(conn, user["id"], '{"token": "legacy"}')

    link = db.get_calendar_link(conn, user["id"])
    assert link["token_json"] == '{"token": "legacy"}'

    # Now a key appears. The old row still reads, and re-saving encrypts it.
    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())
    assert db.token_of(link) == '{"token": "legacy"}'

    db.save_calendar_link(conn, user["id"], '{"token": "legacy"}')
    reread = db.get_calendar_link(conn, user["id"])
    assert crypto.is_encrypted(reread["token_json"])
    assert db.token_of(reread) == '{"token": "legacy"}'


def test_a_changed_key_fails_loudly_rather_than_silently(conn, monkeypatch):
    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())
    user = db.create_user(conn, "+966500000001", "Burhan", "Asia/Riyadh")
    db.save_calendar_link(conn, user["id"], '{"token": "x"}')
    link = db.get_calendar_link(conn, user["id"])

    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())
    with pytest.raises(crypto.NotConfigured, match="key has changed"):
        db.token_of(link)


def test_reading_encrypted_rows_without_a_key_is_explicit(conn, monkeypatch):
    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())
    user = db.create_user(conn, "+966500000001", "Burhan", "Asia/Riyadh")
    db.save_calendar_link(conn, user["id"], '{"token": "x"}')
    link = db.get_calendar_link(conn, user["id"])

    monkeypatch.delenv("WACAL_TOKEN_KEY", raising=False)
    with pytest.raises(crypto.NotConfigured, match="cannot be read"):
        db.token_of(link)


def test_a_malformed_key_is_rejected(monkeypatch):
    monkeypatch.setenv("WACAL_TOKEN_KEY", "not-a-fernet-key")
    with pytest.raises(crypto.NotConfigured, match="valid Fernet key"):
        crypto.encrypt("secret")


def test_the_token_never_reaches_the_database_file(scratch, monkeypatch):
    """End to end, against a real file, inspected with raw SQL."""
    import sqlite3

    monkeypatch.setenv("WACAL_TOKEN_KEY", crypto.generate_key())
    conn = db.connect(scratch)
    try:
        user = db.create_user(conn, "+966500000001", "Burhan", "Asia/Riyadh")
        db.save_calendar_link(conn, user["id"],
                              json.dumps({"refresh_token": "1//SUPERSECRET"}))
    finally:
        conn.close()

    raw = sqlite3.connect(scratch)
    stored = raw.execute("SELECT token_json FROM calendar_links").fetchone()[0]
    raw.close()
    assert "SUPERSECRET" not in stored
    assert "SUPERSECRET" not in scratch.read_bytes().decode("latin-1")
