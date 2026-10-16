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

from wacal import db, slots  # noqa: E402

TESTS = Path(__file__).resolve().parent
CONCURRENCY = str(TESTS / "test_concurrency.py")
PROPERTIES = str(TESTS / "test_slot_properties.py")


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


MUTANTS = {
    "lock": (mutate_lock, [CONCURRENCY],
             "db.LOCK no longer serialises anything"),
    "ignore-busy": (mutate_ignore_busy, [PROPERTIES],
                    "busy intervals are not subtracted"),
    "widen-window": (mutate_widen_window, [PROPERTIES],
                     "the working window is an hour too long"),
    "drop-lead": (mutate_drop_lead_time, [PROPERTIES],
                  "the minimum lead time is ignored"),
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
