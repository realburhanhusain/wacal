"""Property tests for the slot engine.

Example tests pin the cases we thought of. These assert the invariants that
must hold for any schedule, any timezone and any set of busy intervals, which
is where the DST and grid-alignment bugs were hiding.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wacal.slots import (
    free_slots,
    mutual_free_slots,
    parse_day,
    schedule_for,
    tz_of,
    work_window,
)

# Zones chosen for awkwardness: a DST fall-back, a DST spring-forward, a
# half-hour offset, a 45-minute offset, and one with no DST at all.
ZONES = [
    "UTC", "Europe/London", "America/Los_Angeles", "Asia/Riyadh",
    "Asia/Kolkata", "Asia/Kathmandu", "Australia/Lord_Howe", "Pacific/Chatham",
]

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def make_schedule(zone: str, start: time, end: time, days: str,
                  slot_minutes: int) -> dict:
    """A schedule mapping, the same shape slots.schedule_for returns."""
    return {
        "id": 1,
        "timezone": zone,
        "work_start": f"{start:%H:%M}",
        "work_end": f"{end:%H:%M}",
        "work_days": days,
        "slot_minutes": slot_minutes,
    }


times = st.builds(time, st.integers(0, 23), st.sampled_from([0, 15, 30, 45]))
slot_sizes = st.sampled_from([5, 10, 15, 20, 30, 45, 60, 90, 120])
days_strategy = st.lists(st.integers(0, 6), min_size=1, max_size=7,
                         unique=True).map(
    lambda ds: ",".join(str(d) for d in sorted(ds)))
dates = st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31))
all_days = "0,1,2,3,4,5,6"


@st.composite
def productive_schedules(draw):
    """A schedule that actually yields at least two slots on any day.

    Drawing start and end independently produces end <= start most of the
    time, and a schedule with no window yields no slots. Filtering those out
    with assume() made hypothesis raise FailedHealthCheck on roughly one run
    in three -- an intermittent failure with no counterexample, which looks
    exactly like a real flake. Construct the window instead.
    """
    zone = draw(st.sampled_from(ZONES))
    slot_minutes = draw(slot_sizes)
    # Leave room for at least two slots, and keep the window inside one day.
    span = draw(st.integers(min_value=2 * slot_minutes,
                            max_value=max(2 * slot_minutes, 600)))
    start_minute = draw(st.integers(min_value=0,
                                    max_value=max(0, 1439 - span)))
    start = time(start_minute // 60, start_minute % 60)
    end_minute = start_minute + span
    end = time(end_minute // 60, end_minute % 60)
    return make_schedule(zone, start, end, all_days, slot_minutes)


@given(zone=st.sampled_from(ZONES), start=times, end=times, days=days_strategy,
       slot_minutes=slot_sizes, day=dates)
@SETTINGS
def test_every_slot_lies_inside_the_working_window(zone, start, end, days,
                                                   slot_minutes, day):
    schedule = make_schedule(zone, start, end, days, slot_minutes)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = free_slots(schedule, day, [], now=now)

    window = work_window(schedule, day)
    if not slots:
        return
    assert window is not None
    window_start, window_end = window
    for slot_start, slot_end in slots:
        assert slot_start >= window_start
        assert slot_end <= window_end
        assert slot_end - slot_start == timedelta(minutes=slot_minutes)


@given(schedule=productive_schedules(), day=dates)
@SETTINGS
def test_slots_are_aligned_and_never_overlap_each_other(schedule, day):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = free_slots(schedule, day, [], now=now)
    if not slots:
        return

    window_start = work_window(schedule, day)[0]
    size = timedelta(minutes=schedule["slot_minutes"])
    for index, (slot_start, slot_end) in enumerate(slots):
        offset = slot_start - window_start
        assert offset % size == timedelta(0), "slot off the grid"
        if index:
            assert slot_start >= slots[index - 1][1], "slots overlap"


@given(zone=st.sampled_from(ZONES), start=times, end=times, days=days_strategy,
       slot_minutes=slot_sizes, day=dates, lead=st.integers(0, 600))
@SETTINGS
def test_no_slot_starts_before_the_lead_time(zone, start, end, days,
                                             slot_minutes, day, lead):
    schedule = make_schedule(zone, start, end, days, slot_minutes)
    now = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
    slots = free_slots(schedule, day, [], now=now, min_lead_minutes=lead)
    earliest = now + timedelta(minutes=lead)
    for slot_start, _ in slots:
        assert slot_start >= earliest


@given(zone=st.sampled_from(ZONES), start=times, end=times, days=days_strategy,
       slot_minutes=slot_sizes, day=dates,
       busy_offsets=st.lists(st.tuples(st.integers(0, 1440),
                                       st.integers(1, 240)),
                             max_size=6))
@SETTINGS
def test_no_slot_overlaps_a_busy_interval(zone, start, end, days, slot_minutes,
                                          day, busy_offsets):
    schedule = make_schedule(zone, start, end, days, slot_minutes)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    midnight = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
    busy = [
        ((midnight + timedelta(minutes=offset)).isoformat(),
         (midnight + timedelta(minutes=offset + length)).isoformat())
        for offset, length in busy_offsets
    ]
    slots = free_slots(schedule, day, busy, now=now)

    parsed = [(datetime.fromisoformat(s), datetime.fromisoformat(e))
              for s, e in busy]
    for slot_start, slot_end in slots:
        for busy_start, busy_end in parsed:
            assert not (slot_start < busy_end and slot_end > busy_start)


@given(schedule=productive_schedules(), day=dates)
@SETTINGS
def test_booking_a_slot_removes_exactly_that_slot(schedule, day):
    """The round trip the whole app depends on."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = free_slots(schedule, day, [], now=now)
    if len(slots) < 2:
        return

    taken = slots[len(slots) // 2]
    busy = [(taken[0].isoformat(), taken[1].isoformat())]
    after = free_slots(schedule, day, busy, now=now)

    assert taken not in after
    assert set(after) == set(slots) - {taken}


@given(zone=st.sampled_from(ZONES), other=st.sampled_from(ZONES), start=times,
       end=times, days=days_strategy, slot_minutes=slot_sizes, day=dates)
@SETTINGS
def test_mutual_slots_are_a_subset_of_the_owners(zone, other, start, end, days,
                                                 slot_minutes, day):
    owner = make_schedule(zone, start, end, days, slot_minutes)
    requester = {**make_schedule(other, start, end, days, slot_minutes),
                 "id": 2}
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    owner_only = free_slots(owner, day, [], now=now)
    mutual = mutual_free_slots(owner, [], requester, [], day, now=now)
    assert set(mutual) <= set(owner_only)

    # Ignoring the requester's working hours can only widen the offer.
    ignoring = mutual_free_slots(owner, [], requester, [], day, now=now,
                                 requester_hours=False)
    assert set(mutual) <= set(ignoring) <= set(owner_only)


@given(zone=st.sampled_from(ZONES), start=times, end=times, days=days_strategy,
       slot_minutes=slot_sizes, day=dates)
@SETTINGS
def test_slot_generation_never_raises(zone, start, end, days, slot_minutes,
                                     day):
    """Including DST transition days, where local arithmetic tends to blow up."""
    schedule = make_schedule(zone, start, end, days, slot_minutes)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    free_slots(schedule, day, [], now=now)
    work_window(schedule, day)


@given(day=dates, zone=st.sampled_from(ZONES))
@SETTINGS
def test_slot_labels_are_unique_once_the_offset_is_shown(day, zone):
    """A DST fall-back day repeats a wall clock; the offset disambiguates."""
    from wacal.slots import ambiguous_day, fmt_slot

    schedule = make_schedule(zone, time(0, 0), time(23, 30), all_days, 30)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = free_slots(schedule, day, [], now=now)
    if not slots:
        return

    show_offset = ambiguous_day(slots, zone)
    labels = [fmt_slot(s, e, zone, show_offset=show_offset) for s, e in slots]
    assert len(labels) == len(set(labels))


@given(text=st.sampled_from(["today", "tomorrow", "monday", "mon", "sun",
                             "2026-12-25", "", "   ", "nonsense", "2026-02-30",
                             "99", "tomorrowx"]),
       today=dates)
@SETTINGS
def test_parse_day_returns_a_date_or_none_and_never_raises(text, today):
    result = parse_day(text, today=today)
    assert result is None or isinstance(result, date)
    if result is not None and text.strip().lower() not in ("", "today"):
        assert result >= today or text.startswith("2026")


@given(zone=st.sampled_from(ZONES), host_start=times, host_end=times,
       owner_start=times, owner_end=times)
@SETTINGS
def test_a_host_schedule_overrides_only_what_it_sets(zone, host_start,
                                                     host_end, owner_start,
                                                     owner_end):
    owner = make_schedule(zone, owner_start, owner_end, "0,1,2", 30)
    host = {"work_start": f"{host_start:%H:%M}", "work_end": None,
            "work_days": None, "slot_minutes": 45}
    merged = schedule_for(owner, host)

    assert merged["work_start"] == f"{host_start:%H:%M}"
    assert merged["work_end"] == owner["work_end"]
    assert merged["work_days"] == owner["work_days"]
    assert merged["slot_minutes"] == 45
    # The timezone always comes from the owner.
    assert merged["timezone"] == zone
    assert tz_of(merged["timezone"]) is not None


def test_a_known_dst_fall_back_day_is_detected():
    """A fixed case, so the property above cannot pass by never seeing one."""
    from wacal.slots import ambiguous_day

    schedule = make_schedule("Europe/London", time(0, 0), time(3, 0),
                             "0,1,2,3,4,5,6", 30)
    slots = free_slots(schedule, date(2026, 10, 25), [],
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert ambiguous_day(slots, "Europe/London") is True


def test_a_spring_forward_day_loses_an_hour_not_the_whole_day():
    schedule = make_schedule("Europe/London", time(0, 0), time(4, 0),
                             "0,1,2,3,4,5,6", 30)
    slots = free_slots(schedule, date(2026, 3, 29), [],
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    # 00:00-04:00 local spans three real hours on this date, so six 30 minute
    # slots, not eight.
    assert len(slots) == 6
