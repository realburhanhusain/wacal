"""Business mode: a public booking page reachable at /webhook/{host_slug}.

A host is an existing user plus a slug, a service menu and a location. Anyone
holding the link can book, so the grant check used in peer mode does not apply
here -- the link is the authorisation.

The conversation carries its own state in the button ids rather than in a
session row. A tap therefore stays valid after a restart, and a stale tap is
re-validated against live availability before anything is written.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from . import busy, db, keywords
from .commands import Outcome, _mirror, _slots_on, ensure_user, text
from .gcal import ExternalCalendarError
from .slots import fmt_dt, fmt_slot, schedule_for, tz_of, work_window
from .whatsapp import Inbound, Reply, Row

DATE_CHOICES = 7
MAX_ROWS = 10


def services_of(host) -> list[dict]:
    try:
        parsed = json.loads(host["services"])
    except (TypeError, json.JSONDecodeError):
        return []
    return [s for s in parsed if isinstance(s, dict) and s.get("name")]


def _money(host, service) -> str:
    price = service.get("price")
    if price in (None, "", 0):
        return "Free"
    return f"{price} {host['currency']}"


def handle_business(conn, host, msg: Inbound, *,
                    now: datetime | None = None) -> Outcome:
    """Entry point for a message sent to a host's booking webhook."""
    now = now or db.now_utc()
    owner = db.get_user(conn, host["owner_id"])
    if owner is None:
        return text("This booking page is not available.")

    user, early = ensure_user(
        conn, msg,
        welcome=f"Send anything to book at {host['business_name']}.",
    )
    if early is not None:
        return early

    raw = msg.text.strip()
    menu = services_of(host)
    # Whole-message alias first, then the leading word, so both "cancel" and
    # "cancel 7KQ2MD" work, in English or Hindi. See wacal.keywords.
    command = keywords.canonical(raw) or keywords.canonical_verb(
        raw.partition(" ")[0]
    )

    if raw.startswith("b:"):
        return _tap(conn, host, owner, user, raw, now)

    if command == "my":
        return _my_bookings(conn, host, user, now)
    if command == "cancel":
        return _cancel(conn, host, owner, user, raw, now)
    if command == "reschedule":
        if not menu:
            return text(f"{host['business_name']} has no services set up yet.")
        return _show_services(conn, host, owner, user, menu, prefix="Pick a new time.")

    if user["id"] == owner["id"]:
        return _owner_agenda(conn, host, owner, now)

    if not menu:
        return text(f"{host['business_name']} has no services set up yet.")
    return _show_services(conn, host, owner, user, menu)


# --- steps ---------------------------------------------------------------


def _show_services(conn, host, owner, user, menu, *, prefix: str = "") -> Outcome:
    default_duration = schedule_for(owner, host)["slot_minutes"]
    rows = [
        Row(
            id=f"b:svc:{index}",
            title=service["name"],
            description=f"{service.get('duration_mins') or default_duration} min"
                        f" · {_money(host, service)}",
        )
        for index, service in enumerate(menu[:MAX_ROWS])
    ]
    greeting = prefix or (
        f"Welcome to {host['business_name']}, {user['display_name']}."
    )
    return Outcome(Reply(
        header=host["business_name"],
        text=f"{greeting}\nChoose a service.",
        rows=rows,
        button="Services",
    ))


def _show_dates(conn, host, owner, user, menu, service_index, now) -> Outcome:
    service = menu[service_index]
    schedule = schedule_for(owner, host)
    duration = int(service.get("duration_mins") or schedule["slot_minutes"])
    today = now.astimezone(tz_of(schedule["timezone"])).date()

    rows: list[Row] = []
    day = today
    checked = 0
    while len(rows) < DATE_CHOICES and checked < 60:
        checked += 1
        if work_window(schedule, day) is not None:
            try:
                slots = _slots_on(conn, schedule, user, day, now,
                                  slot_minutes=duration, requester_hours=False)
            except ExternalCalendarError:
                return text(
                    f"Cannot read {host['business_name']}'s calendar right now. "
                    "Please try again in a moment."
                )
            if slots:
                label = ""
                if day == today:
                    label = " (today)"
                elif day == today + timedelta(days=1):
                    label = " (tomorrow)"
                rows.append(Row(
                    id=f"b:day:{service_index}:{day.isoformat()}",
                    title=f"{day:%a %d %b}{label}",
                    description=f"{len(slots)} slot(s) open",
                ))
        day += timedelta(days=1)

    if not rows:
        return text(
            f"No openings for {service['name']} in the next 60 days. "
            f"Reply to pick another service."
        )
    return Outcome(Reply(
        header=service["name"],
        text=f"{service['name']} · {duration} min · {_money(host, service)}\n"
             f"Choose a date.",
        rows=rows,
        button="Dates",
    ))


def _show_slots(conn, host, owner, user, menu, service_index, day, now) -> Outcome:
    service = menu[service_index]
    schedule = schedule_for(owner, host)
    duration = int(service.get("duration_mins") or schedule["slot_minutes"])
    try:
        slots = _slots_on(conn, schedule, user, day, now, slot_minutes=duration,
                          requester_hours=False)
    except ExternalCalendarError:
        return text(f"Cannot read {host['business_name']}'s calendar right now.")
    if not slots:
        return text(f"{day:%a %d %b} just filled up. Send 'hi' to start again.")

    zone = user["timezone"]
    rows = [
        Row(
            id=f"b:slot:{service_index}:{int(start.timestamp())}",
            title=fmt_slot(start, end, zone),
            description=f"{day:%a %d %b} · {zone}",
        )
        for start, end in slots[:MAX_ROWS]
    ]
    more = f"\nShowing first {MAX_ROWS} of {len(slots)}." if len(slots) > MAX_ROWS else ""
    return Outcome(Reply(
        header=f"{day:%a %d %b}",
        text=f"Open times for {service['name']} on {day:%a %d %b}, "
             f"shown in {zone}.{more}",
        rows=rows,
        button="Times",
    ))


def _summary(conn, host, owner, user, menu, service_index, start, now) -> Outcome:
    service = menu[service_index]
    duration = int(service.get("duration_mins")
                   or schedule_for(owner, host)["slot_minutes"])
    end = start + timedelta(minutes=duration)
    zone = user["timezone"]
    body = (
        "Please confirm.\n\n"
        f"Name: {user['display_name']}\n"
        f"Phone: {user['phone']}\n"
        f"Service: {service['name']} ({duration} min)\n"
        f"Price: {_money(host, service)}\n"
        f"When: {fmt_dt(start, zone)} to {end.astimezone(tz_of(zone)):%H:%M} ({zone})\n"
        f"Where: {host['location'] or host['business_name']}"
    )
    return Outcome(Reply(
        header="Booking summary",
        text=body,
        rows=[
            Row(id=f"b:ok:{service_index}:{int(start.timestamp())}",
                title="Confirm", description="Book this time"),
            Row(id="b:restart:0", title="Start over",
                description="Choose a different service"),
        ],
        button="Confirm",
    ))


def _confirm(conn, host, owner, user, menu, service_index, start, now) -> Outcome:
    service = menu[service_index]
    schedule = schedule_for(owner, host)
    duration = int(service.get("duration_mins") or schedule["slot_minutes"])
    end = start + timedelta(minutes=duration)

    if start <= now:
        return text("That time has passed. Send 'hi' to pick another.")

    day = start.astimezone(tz_of(schedule["timezone"])).date()
    try:
        still_free = any(
            s == start
            for s, _ in _slots_on(conn, schedule, user, day, now,
                                  slot_minutes=duration, requester_hours=False)
        )
    except ExternalCalendarError:
        return text(f"Cannot read {host['business_name']}'s calendar right now.")
    if not still_free:
        return text("That time was taken while you were deciding. "
                    "Send 'hi' to see what is left.")

    title = f"{service['name']} — {user['display_name']}"
    try:
        event = db.create_event(conn, owner["id"], user["id"], start, end, title,
                                host_slug=host["slug"], service=service["name"])
    except db.SlotTaken:
        return text("That time was just booked by someone else. Send 'hi' to retry.")

    mirror_note = _mirror(
        conn, owner, event,
        description=f"{service['name']} booked via WhatsApp by "
                    f"{user['display_name']} {user['phone']}",
        location=host["location"],
    )

    zone = user["timezone"]
    customer = Reply(text=(
        f"Confirmed. {service['name']} at {host['business_name']}.\n"
        f"{fmt_dt(start, zone)} to {end.astimezone(tz_of(zone)):%H:%M} ({zone})\n"
        f"{host['location']}\n"
        f"Ref {event['ref']} · to cancel send: cancel {event['ref']}"
    ))
    owner_note = Reply(text=(
        f"New booking at {host['business_name']}.\n"
        f"{user['display_name']} ({user['phone']})\n"
        f"{service['name']} · {_money(host, service)}\n"
        f"{fmt_dt(start, owner['timezone'])} ({owner['timezone']})\n"
        f"Ref {event['ref']}{mirror_note}"
    ))
    return Outcome(customer, notify=[(owner["phone"], owner_note)])


# --- taps ----------------------------------------------------------------


def _tap(conn, host, owner, user, raw: str, now: datetime) -> Outcome:
    menu = services_of(host)
    parts = raw.split(":")
    step = parts[1] if len(parts) > 1 else ""

    def service_index(value: str) -> int | None:
        try:
            index = int(value)
        except ValueError:
            return None
        return index if 0 <= index < len(menu) else None

    if step == "restart":
        if not menu:
            return text(f"{host['business_name']} has no services set up yet.")
        return _show_services(conn, host, owner, user, menu)

    if step == "svc" and len(parts) >= 3:
        index = service_index(parts[2])
        if index is None:
            return text("That service is no longer offered. Send 'hi' to start again.")
        return _show_dates(conn, host, owner, user, menu, index, now)

    if step == "day" and len(parts) >= 4:
        index = service_index(parts[2])
        if index is None:
            return text("That service is no longer offered. Send 'hi' to start again.")
        try:
            day = date.fromisoformat(parts[3])
        except ValueError:
            return text("That date is unreadable. Send 'hi' to start again.")
        return _show_slots(conn, host, owner, user, menu, index, day, now)

    if step in ("slot", "ok") and len(parts) >= 4:
        index = service_index(parts[2])
        if index is None:
            return text("That service is no longer offered. Send 'hi' to start again.")
        try:
            start = datetime.fromtimestamp(int(parts[3]), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return text("That time is unreadable. Send 'hi' to start again.")
        if step == "slot":
            return _summary(conn, host, owner, user, menu, index, start, now)
        return _confirm(conn, host, owner, user, menu, index, start, now)

    return text("Send 'hi' to start a booking.")


# --- other commands -----------------------------------------------------


def _my_bookings(conn, host, user, now) -> Outcome:
    rows = db.customer_bookings(conn, user["id"], host["slug"], now)
    if not rows:
        return text(f"No upcoming bookings at {host['business_name']}. "
                    "Send 'hi' to make one.")
    lines = [f"Your bookings at {host['business_name']} ({user['timezone']}):"]
    for row in rows:
        start = datetime.fromisoformat(row["start_utc"])
        lines.append(f"• {row['ref']} {fmt_dt(start, user['timezone'])} · "
                     f"{row['service'] or 'Appointment'}")
    lines.append("Cancel with: cancel <ref>")
    return text("\n".join(lines))


def _cancel(conn, host, owner, user, raw: str, now) -> Outcome:
    # Pick the ref out by shape. "cancel 7KQ2MD" has one, "cancel booking" and
    # the Hindi "रद्द करें" do not, even though both carry a second word.
    ref = next((token.upper() for token in raw.split()[1:]
                if re.fullmatch(r"[A-Za-z0-9]{6}", token)), "")
    if not ref:
        rows = db.customer_bookings(conn, user["id"], host["slug"], now)
        if len(rows) == 1:
            ref = rows[0]["ref"]
        else:
            return _my_bookings(conn, host, user, now)

    event = db.get_event(conn, ref)
    if event is None or event["host_slug"] != host["slug"]:
        return text(f"No booking with ref {ref} at {host['business_name']}.")
    if user["id"] not in (event["created_by"], event["owner_id"]):
        return text("Only the customer or the business can cancel that booking.")
    if event["status"] != "confirmed":
        return text(f"{ref} is already cancelled.")

    db.cancel_event(conn, ref)
    stale = ""
    if event["external_id"] and not busy.mirror_delete(conn, event):
        stale = "\nNote: the copy in Google Calendar could not be removed."

    start = datetime.fromisoformat(event["start_utc"])
    confirm = Reply(text=f"Cancelled {ref} — {fmt_dt(start, user['timezone'])}. "
                         f"The time is free again.{stale}")
    other_id = (event["owner_id"] if user["id"] == event["created_by"]
                else event["created_by"])
    if other_id == user["id"]:
        return Outcome(confirm)
    other = db.get_user(conn, other_id)
    note = Reply(text=(
        f"{user['display_name']} cancelled {ref} at {host['business_name']} — "
        f"{fmt_dt(start, other['timezone'])} ({other['timezone']})."
    ))
    return Outcome(confirm, notify=[(other["phone"], note)])


def _owner_agenda(conn, host, owner, now) -> Outcome:
    until = now + timedelta(days=7)
    rows = db.host_bookings(conn, host["slug"], now, until)
    if not rows:
        return text(f"{host['business_name']}: nothing booked in the next 7 days.")
    lines = [f"{host['business_name']} — next 7 days ({owner['timezone']}):"]
    for row in rows:
        start = datetime.fromisoformat(row["start_utc"])
        lines.append(
            f"• {row['ref']} {fmt_dt(start, owner['timezone'])} · "
            f"{row['service'] or 'Appointment'} · {row['customer_name']} "
            f"{row['customer_phone']}"
        )
    lines.append("Cancel one with: cancel <ref>")
    return text("\n".join(lines))
