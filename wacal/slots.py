"""Availability maths.

Working hours live in each user's own timezone, bookings live in UTC. Every
conversion happens here so the rest of the app only ever handles UTC instants.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def tz_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_hhmm(value: str) -> time:
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes or 0))


def schedule_for(owner, host=None) -> dict:
    """The schedule the slot engine should use.

    Peer mode passes the user row alone. Business mode passes the host too: its
    opening hours win where set, and anything left NULL inherits the owner's.
    The timezone always comes from the owner -- a host has no separate one.
    """
    effective = {
        "id": owner["id"],
        "timezone": owner["timezone"],
        "work_start": owner["work_start"],
        "work_end": owner["work_end"],
        "work_days": owner["work_days"],
        "slot_minutes": owner["slot_minutes"],
    }
    if host is None:
        return effective
    keys = host.keys() if hasattr(host, "keys") else host
    for field in ("work_start", "work_end", "work_days", "slot_minutes"):
        if field in keys and host[field] not in (None, ""):
            effective[field] = host[field]
    return effective


def work_window(user, day: date) -> tuple[datetime, datetime] | None:
    """The user's working window on one local calendar day, as UTC instants.

    Returns None on a non-working day.
    """
    work_days = {int(d) for d in user["work_days"].split(",") if d.strip() != ""}
    if day.weekday() not in work_days:
        return None
    zone = tz_of(user["timezone"])
    start_local = datetime.combine(day, parse_hhmm(user["work_start"]), tzinfo=zone)
    end_local = datetime.combine(day, parse_hhmm(user["work_end"]), tzinfo=zone)
    if end_local <= start_local:
        return None
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and a_end > b_start


def free_slots(
    user,
    day: date,
    busy: list[tuple[str, str]],
    *,
    now: datetime,
    slot_minutes: int | None = None,
    min_lead_minutes: int = 15,
) -> list[tuple[datetime, datetime]]:
    """Candidate slots on one local day for one user.

    Slots are aligned to the start of the working window. A slot is dropped if
    it overlaps a busy interval, or if it starts sooner than min_lead_minutes
    from now -- nobody wants a booking that already began.
    """
    window = work_window(user, day)
    if window is None:
        return []
    window_start, window_end = window
    size = timedelta(minutes=slot_minutes or user["slot_minutes"])
    if size <= timedelta(0):
        return []

    busy_dt = [
        (datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in busy
    ]
    earliest = now + timedelta(minutes=min_lead_minutes)

    out: list[tuple[datetime, datetime]] = []
    cursor = window_start
    while cursor + size <= window_end:
        slot_end = cursor + size
        if cursor >= earliest and not any(
            _overlaps(cursor, slot_end, b_start, b_end) for b_start, b_end in busy_dt
        ):
            out.append((cursor, slot_end))
        cursor = slot_end
    return out


def mutual_free_slots(
    owner,
    owner_busy: list[tuple[str, str]],
    requester,
    requester_busy: list[tuple[str, str]],
    day: date,
    *,
    now: datetime,
    slot_minutes: int | None = None,
    requester_hours: bool = True,
) -> list[tuple[datetime, datetime]]:
    """Slots that are free for the owner AND inside the requester's own free time.

    The owner's working hours and slot grid win, because it is their calendar
    being booked. The requester's side only removes conflicts.

    requester_hours=False keeps the conflict check but ignores the requester's
    working window. A customer booking a clinic is not at work, so their office
    hours must not narrow the clinic's offer.
    """
    candidates = free_slots(owner, day, owner_busy, now=now, slot_minutes=slot_minutes)
    if owner["id"] == requester["id"]:
        return candidates

    req_busy = [
        (datetime.fromisoformat(s), datetime.fromisoformat(e))
        for s, e in requester_busy
    ]
    req_window = work_window(requester, day) if requester_hours else None

    kept = []
    for start, end in candidates:
        if any(_overlaps(start, end, b0, b1) for b0, b1 in req_busy):
            continue
        # A requester outside their own working hours is allowed -- they chose
        # to ask -- but their existing bookings are still respected above.
        if req_window and not _overlaps(start, end, req_window[0], req_window[1]):
            continue
        kept.append((start, end))
    return kept


def parse_day(text: str, *, today: date) -> date | None:
    """Accept 'today', 'tomorrow', a weekday name, or YYYY-MM-DD."""
    value = text.strip().lower()
    if value in ("", "today"):
        return today
    if value == "tomorrow":
        return today + timedelta(days=1)
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]
    for index, name in enumerate(weekdays):
        if value in (name, name[:3]):
            ahead = (index - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def fmt_slot(start: datetime, end: datetime, zone: str, *,
             show_offset: bool = False) -> str:
    z = tz_of(zone)
    local_start, local_end = start.astimezone(z), end.astimezone(z)
    label = f"{local_start:%H:%M}-{local_end:%H:%M}"
    return f"{label} {local_start:%z}" if show_offset else label


def ambiguous_day(slots: list[tuple[datetime, datetime]], zone: str) -> bool:
    """True when two slots on this day share a wall-clock label.

    On a DST fall-back night 01:30 happens twice, so "01:30-02:00" would be
    printed for two different instants -- and one slot renders backwards. The
    caller appends the UTC offset to keep the labels distinct.
    """
    z = tz_of(zone)
    offsets = {start.astimezone(z).utcoffset() for start, _ in slots}
    return len(offsets) > 1


def local_date(moment: datetime, zone: str) -> date:
    """The calendar date of an instant for a viewer in this timezone.

    A slot that is Friday for the host can be Thursday for the customer, so
    the date beside a time must come from the same timezone as the time.
    """
    return moment.astimezone(tz_of(zone)).date()


def fmt_dt(dt: datetime | str, zone: str) -> str:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return f"{dt.astimezone(tz_of(zone)):%a %d %b %H:%M}"
