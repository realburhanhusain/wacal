"""Verify the concurrency and property suites have teeth.

Run directly, not under pytest collection:

    python tests/mutation_check.py           # every mutant
    python tests/mutation_check.py lock      # one by name

Each mutant breaks something the suite claims to guarantee, then runs the
relevant tests and expects them to FAIL. A mutant that survives means the
tests would not have caught that regression, which is worth knowing before
trusting a green run.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wacal import db, otp, slots, templates  # noqa: E402

TESTS = Path(__file__).resolve().parent
CONCURRENCY = str(TESTS / "test_concurrency.py")
PROPERTIES = str(TESTS / "test_slot_properties.py")
TEMPLATES = str(TESTS / "test_templates.py")
OTP = str(TESTS / "test_otp.py")


class NoLock:
    """Stands in for db.LOCK and serialises nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def mutate_lock() -> None:
    db.LOCK = NoLock()


def mutate_ignore_busy() -> None:
    """Make the busy-interval filter a no-op."""
    slots._overlaps = lambda *args: False


def mutate_widen_window() -> None:
    """Hand back a working window an hour wider than it should be."""
    original = slots.work_window

    def wider(user, day):
        window = original(user, day)
        if window is None:
            return None
        return window[0], window[1] + timedelta(hours=1)

    slots.work_window = wider


def mutate_drop_lead_time() -> None:
    """Offer slots that have already started."""
    original = slots.free_slots

    def no_lead(user, day, busy, *, now, slot_minutes=None,
                min_lead_minutes=15):
        return original(user, day, busy, now=now, slot_minutes=slot_minutes,
                        min_lead_minutes=-10_000)

    slots.free_slots = no_lead


def mutate_assume_window_open() -> None:
    """Treat every number as inside its 24-hour window."""
    db.within_window = lambda *args, **kwargs: True


def mutate_send_text_outside_window() -> None:
    """Declare an undeliverable message deliverable as free-form text."""
    templates.delivery_status = lambda **kwargs: templates.Status(True)


def mutate_otp_never_expires() -> None:
    original = otp.check

    def forever(conn, phone, code, *, now=None):
        row = db.get_otp(conn, phone)
        if row is not None:
            conn.execute("UPDATE otp_codes SET expires_at = ? WHERE phone = ?",
                         (db.iso(db.now_utc() + timedelta(days=365)), phone))
        return original(conn, phone, code, now=now)

    otp.check = forever


def mutate_otp_unlimited_attempts() -> None:
    """Remove the guess cap, leaving six digits to brute force."""
    otp.MAX_ATTEMPTS = 10 ** 6


MUTANTS = {
    "lock": (mutate_lock, [CONCURRENCY],
             "db.LOCK no longer serialises anything"),
    "ignore-busy": (mutate_ignore_busy, [PROPERTIES],
                    "busy intervals are not subtracted"),
    "widen-window": (mutate_widen_window, [PROPERTIES],
                     "the working window is an hour too long"),
    "drop-lead": (mutate_drop_lead_time, [PROPERTIES],
                  "the minimum lead time is ignored"),
    "window-open": (mutate_assume_window_open, [TEMPLATES],
                    "every number is treated as inside its 24-hour window"),
    "send-anyway": (mutate_send_text_outside_window, [TEMPLATES],
                    "an undeliverable message is sent as free-form text"),
    "otp-forever": (mutate_otp_never_expires, [OTP],
                    "a one-time code never expires"),
    "otp-unlimited": (mutate_otp_unlimited_attempts, [OTP],
                      "a one-time code can be guessed without limit"),
}


def run_one(name: str) -> bool:
    """True if the mutant was killed (the tests failed, as they should)."""
    apply, targets, description = MUTANTS[name]
    print(f"\n=== mutant '{name}': {description} ===")
    apply()
    code = pytest.main([*targets, "-q", "-p", "no:cacheprovider",
                        "--timeout=300", "-x"])
    if code == 0:
        print(f"MUTANT SURVIVED: '{name}' changed behaviour and nothing failed.")
        return False
    print(f"killed (pytest exit {code})")
    return True


def main(argv: list[str]) -> int:
    wanted = argv[1:] or list(MUTANTS)
    unknown = [name for name in wanted if name not in MUTANTS]
    if unknown:
        print(f"unknown mutant(s): {unknown}. Known: {list(MUTANTS)}")
        return 2

    # One mutant per process: they patch module globals and do not compose.
    if len(wanted) > 1:
        import subprocess

        failures = []
        for name in wanted:
            result = subprocess.run([sys.executable, __file__, name])
            if result.returncode != 0:
                failures.append(name)
        if failures:
            print(f"\nSURVIVING MUTANTS: {failures}")
            return 1
        print(f"\nAll {len(wanted)} mutants killed.")
        return 0

    return 0 if run_one(wanted[0]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
