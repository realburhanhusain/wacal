"""Business mode: a public booking page reachable at /webhook/{host_slug}.

A host is an existing user plus a slug, a service menu and a location. Anyone
holding the link can book, so the grant check used in peer mode does not apply
here -- the link is the authorisation.

The conversation carries its own state in the button ids rather than in a
session row. A tap therefore stays valid after a restart, and a stale tap is
re-validated against live availability before anything is written. Each id also
carries a fingerprint of the service it was built for, so editing the menu
between two taps cannot silently book something else.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone

from . import busy, commands, db, keywords
from .commands import (
    Outcome,
    _mirror,
    _slots_on,
    ensure_user,
    prefetch_busy,
    text,
)
from .gcal import ExternalCalendarError
from .slots import (
    ambiguous_day,
    fmt_dt,
    fmt_slot,
    local_date,
    schedule_for,
    tz_of,
    work_window,
)
from .whatsapp import Inbound, Reply, Row

DATE_CHOICES = 7
LOOKAHEAD_DAYS = 60
MAX_ROWS = 10

# Six-letter words a customer may type after "cancel" that are not refs.
REF_LOOKALIKES = {
    "please", "booking", "ticket", "myslot", "sorry", "thanks", "appoin",
    "kindly", "wantto", "mybook",
}


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


def service_token(service: dict, default_duration: int) -> str:
    """A short fingerprint of a service, carried in the button ids.

    The ids used to hold only the menu position. Edit the menu between the
    summary and the Confirm tap and that position points at a different
    service, so the customer books something they never chose.
    """
    duration = int(service.get("duration_mins") or default_duration)
    seed = f"{service['name']}|{duration}".encode()
    return hashlib.sha1(seed).hexdigest()[:4]


def resolve_service(menu: list[dict], index_text: str, token: str,
                    default_duration: int) -> int | None:
    """The menu position a button id refers to, or None if it no longer holds."""
    try:
        index = int(index_text)
    except ValueError:
        return None
    if not 0 <= index < len(menu):
        return None
    if token and service_token(menu[index], default_duration) != token:
        return None
    return index


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
        tz=owner["timezone"],
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
        return _reschedule(conn, host, owner, user, menu, now)

    if user["id"] == owner["id"]:
        if command in ("help", "book") or not raw:
            return _owner_agenda(conn, host, owner, now)
        # One WhatsApp number maps to one webhook, so a business deployment has
        # no peer endpoint. Without this the owner cannot reach connect google,
        # block, closed, hours or days -- the very things the register response
        # tells them to send.
        return commands.handle(conn, msg, now=now)

    if not menu:
        return text(f"{host['business_name']} has no services set up yet.")
    return _show_services(conn, host, owner, user, menu)


# --- steps ---------------------------------------------------------------


def _show_services(conn, host, owner, user, menu, *, prefix: str = "",
                   replacing: str = "") -> Outcome:
    default_duration = schedule_for(owner, host)["slot_minutes"]
    rows = [
        Row(
            id=_pack("svc", index, service_token(service, default_duration), "",
                     replacing),
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
        section="Services",
    ))


def _show_dates(conn, host, owner, user, menu, index, token, now,
                *, replacing: str = "") -> Outcome:
    service = menu[index]
    schedule = schedule_for(owner, host)
    duration = int(service.get("duration_mins") or schedule["slot_minutes"])
    today = now.astimezone(tz_of(schedule["timezone"])).date()

    try:
        # Both calendars are read once for the whole search window, not once
        # per candidate day.
        prefetched = prefetch_busy(conn, schedule, user, today,
                                   today + timedelta(days=LOOKAHEAD_DAYS),
                                   mutual=False)
    except ExternalCalendarError:
        return text(
            f"Cannot read {host['business_name']}'s calendar right now. "
            "Please try again in a moment."
        )

    rows: list[Row] = []
    day = today
    checked = 0
    while len(rows) < DATE_CHOICES and checked < LOOKAHEAD_DAYS:
        checked += 1
        if work_window(schedule, day) is not None:
            slots = _slots_on(conn, schedule, user, day, now,
                              slot_minutes=duration, requester_hours=False,
                              prefetched=prefetched)
            if slots:
                label = ""
                if day == today:
                    label = " (today)"
                elif day == today + timedelta(days=1):
                    label = " (tomorrow)"
                rows.append(Row(
                    id=_pack("day", index, token, day.isoformat(), replacing),
                    title=f"{day:%a %d %b}{label}",
                    description=f"{len(slots)} slot(s) open",
                ))
        day += timedelta(days=1)

    if not rows:
        # A dead end with no rows leaves the customer nothing to tap, so fall
        # back to the menu rather than telling them to "reply".
        return _show_services(
            conn, host, owner, user, menu, replacing=replacing,
            prefix=f"No openings for {service['name']} in the next "
                   f"{LOOKAHEAD_DAYS} days. Choose another service.",
        )
    return Outcome(Reply(
        header=service["name"],
        text=f"{service['name']} · {duration} min · {_money(host, service)}\n"
             f"Choose a date.",
        rows=rows,
        button="Dates",
        section="Dates",
    ))


def _show_slots(conn, host, owner, user, menu, index, token, day, now,
                *, replacing: str = "") -> Outcome:
    service = menu[index]
    schedule = schedule_for(owner, host)
    duration = int(service.get("duration_mins") or schedule["slot_minutes"])
    try:
        slots = _slots_on(conn, schedule, user, day, now, slot_minutes=duration,
                          requester_hours=False)
    except ExternalCalendarError:
        return text(f"Cannot read {host['business_name']}'s calendar right now.")
    if not slots:
        # Offer the remaining dates instead of resetting to the start.
        return _show_dates(conn, host, owner, user, menu, index, token, now,
                           replacing=replacing)

    zone = user["timezone"]
    # On a DST fall-back day two slots share a wall-clock label, so show the
    # offset. The date comes from the viewer's zone: the host's Friday slot can
    # be the customer's Thursday.
    offsets = ambiguous_day(slots[:MAX_ROWS], zone)
    rows = [
        Row(
            id=_pack("slot", index, token, int(start.timestamp()), replacing),
            title=fmt_slot(start, end, zone, show_offset=offsets),
            description=f"{local_date(start, zone):%a %d %b} · {zone}",
        )
        for start, end in slots[:MAX_ROWS]
    ]
    more = (f"\nShowing first {MAX_ROWS} of {len(slots)}."
            if len(slots) > MAX_ROWS else "")
    return Outcome(Reply(
        header=f"{day:%a %d %b}",
        text=f"Open times for {service['name']} on {day:%a %d %b}, "
             f"shown in {zone}.{more}",
        rows=rows,
        button="Times",
        section="Times",
    ))


def _summary(conn, host, owner, user, menu, index, token, start, now,
             *, replacing: str = "") -> Outcome:
    service = menu[index]
    duration = int(service.get("duration_mins")
                   or schedule_for(owner, host)["slot_minutes"])
    end = start + timedelta(minutes=duration)
    zone = user["timezone"]
    moving = (f"\nThis replaces booking {replacing}, which will be cancelled."
              if replacing else "")
    body = (
        "Please confirm.\n\n"
        f"Name: {user['display_name']}\n"
        f"Phone: {user['phone']}\n"
        f"Service: {service['name']} ({duration} min)\n"
        f"Price: {_money(host, service)}\n"
        f"When: {fmt_dt(start, zone)} to {end.astimezone(tz_of(zone)):%H:%M} ({zone})\n"
        f"Where: {host['location'] or host['business_name']}{moving}"
    )
    return Outcome(Reply(
        header="Booking summary",
        text=body,
        rows=[
            Row(id=_pack("ok", index, token, int(start.timestamp()), replacing),
                title="Confirm", description="Book this time"),
            Row(id="b:restart:0", title="Start over",
                description="Choose a different service"),
        ],
        button="Confirm",
        section="Confirm",
    ))


def _confirm(conn, host, owner, user, menu, index, token, start, now,
             *, replacing: str = "") -> Outcome:
    service = menu[index]
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
        return text("That time was just booked by someone else. "
                    "Send 'hi' to retry.")

    # The old booking goes only once the new one exists, so a failure here
    # never leaves the customer with nothing.
    moved = _release_replaced(conn, host, user, replacing)

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
        f"Ref {event['ref']} · to cancel send: cancel {event['ref']}{moved}"
    ))
    owner_note = Reply(text=(
        f"New booking at {host['business_name']}.\n"
        f"{user['display_name']} ({user['phone']})\n"
        f"{service['name']} · {_money(host, service)}\n"
        f"{fmt_dt(start, owner['timezone'])} ({owner['timezone']})\n"
        f"Ref {event['ref']}{moved}{mirror_note}"
    ))
    return Outcome(customer, notify=[(owner["phone"], owner_note)])


def _release_replaced(conn, host, user, ref: str) -> str:
    """Cancel the booking a reschedule is replacing. Returns a note or ""."""
    if not ref:
        return ""
    previous = db.get_event(conn, ref)
    if previous is None or previous["host_slug"] != host["slug"]:
        return ""
    if previous["created_by"] != user["id"] or previous["status"] != "confirmed":
        return ""
    db.cancel_event(conn, ref)
    if previous["external_id"]:
        busy.mirror_delete(conn, previous)
    return f"\nBooking {ref} has been cancelled."


# --- taps ----------------------------------------------------------------


def _pack(step: str, index: int, token: str, payload, replacing: str = "") -> str:
    """Build a button id. The trailing field is the booking being replaced."""
    tail = f":{replacing}" if replacing else ""
    return f"b:{step}:{index}:{token}:{payload}{tail}"


def _tap(conn, host, owner, user, raw: str, now: datetime) -> Outcome:
    menu = services_of(host)
    default_duration = schedule_for(owner, host)["slot_minutes"]
    parts = raw.split(":")
    step = parts[1] if len(parts) > 1 else ""

    if step == "restart":
        if not menu:
            return text(f"{host['business_name']} has no services set up yet.")
        return _show_services(conn, host, owner, user, menu)

    # b:<step>:<index>:<token>:<payload>[:<replacing>]
    if len(parts) < 4:
        return text("Send 'hi' to start a booking.")
    index = resolve_service(menu, parts[2], parts[3], default_duration)
    if index is None:
        return text("That service is no longer offered. Send 'hi' to start again.")
    token = parts[3]
    payload = parts[4] if len(parts) > 4 else ""
    replacing = parts[5] if len(parts) > 5 else ""

    if step == "svc":
        return _show_dates(conn, host, owner, user, menu, index, token, now,
                           replacing=replacing)

    if step == "day":
        try:
            day = date.fromisoformat(payload)
        except ValueError:
            return text("That date is unreadable. Send 'hi' to start again.")
        return _show_slots(conn, host, owner, user, menu, index, token, day, now,
                           replacing=replacing)

    if step in ("slot", "ok"):
        try:
            start = datetime.fromtimestamp(int(payload), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return text("That time is unreadable. Send 'hi' to start again.")
        if step == "slot":
            return _summary(conn, host, owner, user, menu, index, token, start,
                            now, replacing=replacing)
        return _confirm(conn, host, owner, user, menu, index, token, start, now,
                        replacing=replacing)

    return text("Send 'hi' to start a booking.")


# --- other commands -----------------------------------------------------


def _reschedule(conn, host, owner, user, menu, now) -> Outcome:
    """Move an existing booking, rather than adding a second one.

    The ref being replaced rides along in the button ids and is cancelled
    inside _confirm once the new booking exists.
    """
    if not menu:
        return text(f"{host['business_name']} has no services set up yet.")
    rows = db.customer_bookings(conn, user["id"], host["slug"], now)
    if not rows:
        return _show_services(conn, host, owner, user, menu,
                              prefix="You have nothing booked here yet.")
    if len(rows) > 1:
        listed = _my_bookings(conn, host, user, now)
        listed.reply.text += ("\nTo move one, cancel it and book again, or "
                              "send: reschedule <ref>")
        return listed
    ref = rows[0]["ref"]
    start = datetime.fromisoformat(rows[0]["start_utc"])
    return _show_services(
        conn, host, owner, user, menu, replacing=ref,
        prefix=f"Moving booking {ref} from "
               f"{fmt_dt(start, user['timezone'])} ({user['timezone']}). "
               f"Choose the service.",
    )


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
    # Pick the ref out by shape, then discard real words. "cancel booking",
    # "cancel please" and the Hindi "रद्द करें" all carry a six-character
    # second word that is not a ref. Requiring a digit instead would be wrong:
    # the alphabet is A-Z0-9, so plenty of real refs have no digit at all.
    ref = next((token.upper() for token in raw.split()[1:]
                if re.fullmatch(r"[A-Za-z0-9]{6}", token)
                and keywords.canonical(token) is None
                and token.lower() not in REF_LOOKALIKES), "")
    if not ref:
        rows = db.customer_bookings(conn, user["id"], host["slug"], now)
        if len(rows) == 1:
            ref = rows[0]["ref"]
        else:
            return _my_bookings(conn, host, user, now)

    event = db.get_event(conn, ref)
    # The owner's own blocks -- "closed", "block" -- are peer-mode events with
    # no host_slug at all, and this is the only webhook they can reach. Another
    # host's booking stays off limits here even for the person who made it.
    if (event is not None and event["host_slug"] is None
            and user["id"] == event["owner_id"]):
        return commands.handle(
            conn, Inbound(from_phone=user["phone"], text=f"cancel {ref}",
                          message_id=f"delegated-{ref}"),
            now=now,
        )
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
