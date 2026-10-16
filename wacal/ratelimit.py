"""Per-sender rate limiting.

A valid signature is not a promise of good behaviour. One number can hold a
button down, or a provider can replay a burst, and every message costs a
booking-engine pass plus possibly a Google round trip -- all of it behind one
process-wide lock, so a single sender can stall everyone.

Two windows, because they catch different things: a short one for a burst, a
long one for a sustained flood. State is in memory, which is correct here
because wacal already refuses to run as more than one process (see
wacal.singleton).
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque


def _setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return max(0, value)


class RateLimiter:
    """Sliding-window counter per key, with a burst and a sustained window."""

    def __init__(self, *, burst: int = 20, burst_seconds: int = 20,
                 sustained: int = 120, sustained_seconds: int = 600) -> None:
        self.burst = burst
        self.burst_seconds = burst_seconds
        self.sustained = sustained
        self.sustained_seconds = sustained_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._warned: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.burst > 0 or self.sustained > 0

    def check(self, key: str, *, now: float | None = None) -> str | None:
        """Record a hit. Returns None to proceed, or a reason to refuse.

        The reason is returned only the first time a key is throttled within
        the burst window. Later refusals return "" -- throttled, say nothing.
        Replying to every message of a flood is itself a flood.
        """
        if not self.enabled:
            return None
        moment = now if now is not None else time.monotonic()
        horizon = max(self.burst_seconds, self.sustained_seconds)

        with self._lock:
            hits = self._hits[key]
            while hits and moment - hits[0] > horizon:
                hits.popleft()

            recent = sum(1 for hit in hits if moment - hit <= self.burst_seconds)
            total = len(hits)
            over_burst = self.burst and recent >= self.burst
            over_sustained = self.sustained and total >= self.sustained

            if not (over_burst or over_sustained):
                hits.append(moment)
                self._warned.pop(key, None)
                return None

            # Count the refused attempt too, so hammering extends the block.
            hits.append(moment)
            last_warned = self._warned.get(key)
            if last_warned is not None and moment - last_warned < self.burst_seconds:
                return ""
            self._warned[key] = moment

        if over_burst:
            return (f"Too many messages at once. Wait {self.burst_seconds} "
                    f"seconds and try again.")
        return ("You have sent a lot of messages. Please wait a few minutes "
                "before trying again.")

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
                self._warned.clear()
            else:
                self._hits.pop(key, None)
                self._warned.pop(key, None)


def from_environment() -> RateLimiter:
    """WACAL_RATE_BURST / _BURST_SECONDS / _SUSTAINED / _SUSTAINED_SECONDS.

    Set WACAL_RATE_BURST=0 and WACAL_RATE_SUSTAINED=0 to disable entirely.
    """
    # Generous on purpose: a customer tapping through service, date, time and
    # confirm sends four messages in a few seconds, and a numbered-reply flow
    # on Twilio sends more. These limits are aimed at a stuck key or a replay,
    # not at ordinary impatience.
    return RateLimiter(
        burst=_setting("WACAL_RATE_BURST", 20),
        burst_seconds=_setting("WACAL_RATE_BURST_SECONDS", 20),
        sustained=_setting("WACAL_RATE_SUSTAINED", 120),
        sustained_seconds=_setting("WACAL_RATE_SUSTAINED_SECONDS", 600),
    )
