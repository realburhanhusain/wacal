"""Verify the concurrency tests have teeth.

Run directly, not under pytest collection:

    python tests/mutation_check.py

It removes the serialising lock, runs the concurrency suite, and expects it to
FAIL. A suite that still passes without the lock was not testing the lock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wacal import db  # noqa: E402


class NoLock:
    """Stands in for db.LOCK and serialises nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main() -> int:
    db.LOCK = NoLock()
    print("Running tests/test_concurrency.py with db.LOCK disabled...")
    code = pytest.main([
        str(Path(__file__).resolve().parent / "test_concurrency.py"),
        "-q", "-p", "no:cacheprovider", "--timeout=60",
    ])
    if code == 0:
        print("\nMUTATION SURVIVED: the suite passes without the lock, so it "
              "is not proving what it claims.")
        return 1
    print(f"\nMutation killed (pytest exit {code}): removing the lock breaks "
          f"the suite, which is the point.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
