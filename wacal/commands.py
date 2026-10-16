"""Command parsing and business rules.

handle() is pure with respect to WhatsApp: it takes an Inbound and returns an
Outcome holding the reply plus any notifications for third parties. The caller
decides how to deliver them.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

from . import busy, db, gcal, keywords, templates
from .booking import (
    CALENDAR_UNREACHABLE,
    KNOWN_VERBS,
    Outcome,
    book,
    ensure_user,
    mirror,
    prefetch_busy,
    release,
    slots_on,
    text,
)
from .gcal import ExternalCalendarError, NotConfigured
from .slots import (
    ambiguous_day,
    fmt_dt,
    fmt_slot,
    is_ambiguous,
    is_skipped,
    local_date,
    offset_label,
    mutual_free_slots,
    parse_day,
    parse_hhmm,
    schedule_for,
    to_utc,
    tz_of,
    work_window,
)
from .whatsapp import Inbound, Reply, Row

LOOKAHEAD_DAYS = 14
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

HELP = (
    "Commands:\n"
    "• free <name|phone> [today|tomorrow|mon|YYYY-MM-DD] — see open slots and book one\n"
    "• block <day> <HH:MM-HH:MM> [title] — block your own time\n"
    "• closed <day> — block a whole working day, holidays included\n"
    "• my — your upcoming bookings with their refs\n"
    "• cancel <ref> — unblock a slot\n"
    "• share <phone> — let someone see your free/busy and book you\n"
    "• unshare <phone> / shared — manage and list access\n"
    "• connect google / disconnect google — use your Google Calendar too\n"
    "• mylink — your business booking link, if you have one\n"
    "• name <your name> / tz <Area/City> / hours 09:00-17:00 / days mon-fri\n"
    "• me — your settings"
)

def handle(conn, msg: Inbound, *, now: datetime | None = None) -> Outcome:
    now = now or db.now_utc()
    existing = db.get_user_by_phone(conn, msg.from_phone) is not None
    user, early = ensure_user(conn, msg)
    if early is not None:
        return early
    if not existing:
        # A newly created user is told which timezone their times are in.
        # Meta always supplies a profile name, so auto-registered peer users
        # never saw this hint and silently lived in UTC.
        return text(
            f"Welcome {user['display_name']}.\n"
            f"Times are shown in {user['timezone']} — change it with: "
            f"tz Asia/Riyadh\n\n{HELP}"
        )

    raw = msg.text.strip()
    if not raw:
        return text(HELP)

    if msg.is_tap or raw.startswith("book:"):
        return _do_tap(conn, user, raw, now)

    verb, _, rest = raw.partition(" ")
    rest = rest.strip()
    # A whole-message alias wins over the first word, so "my bookings" and
    # "मेरी बुकिंग" both reach the same handler. Matching the whole message
    # means there is no argument left, hence rest is cleared. See wacal.keywords.
    whole = keywords.canonical(raw)
    if whole:
        verb, rest = whole, ""
    else:
        verb = keywords.canonical_verb(verb)

    if verb in ("help", "book"):
        return text(HELP)
    if verb == "me":
        return _do_me(conn, user)
    if verb in ("name", "register"):
        return _do_name(conn, user, rest)
    if verb in ("tz", "timezone"):
        return _do_tz(conn, user, rest)
    if verb == "hours":
        return _do_hours(conn, user, rest)
    if verb == "days":
        return _do_days(conn, user, rest)
    if verb == "share":
        return _do_share(conn, user, rest)
    if verb == "unshare":
        return _do_unshare(conn, user, rest)
    if verb == "shared":
        return _do_shared(conn, user)
    if verb == "free":
        return _do_free(conn, user, rest, now)
    if verb == "block":
        return _do_block(conn, user, rest, now)
    if verb == "closed":
        return _do_closed(conn, user, rest, now)
    if verb == "my":
        return _do_my(conn, user, now)
    if verb in ("cancel", "unblock"):
        return _do_cancel(conn, user, rest)
    if verb == "connect":
        return _do_connect(conn, user)
    if verb == "disconnect":
        return _do_disconnect(conn, user)
    if verb in ("mylink", "link"):
        return _do_mylink(conn, user)

    return text(f"Did not understand {verb!r}.\n\n{HELP}")


# --- settings ------------------------------------------------------------


def _do_me(conn, user) -> Outcome:
    days = ",".join(
        DAY_NAMES[int(d)] for d in user["work_days"].split(",") if d.strip()
    )
    link = db.get_calendar_link(conn, user["id"])
    external = (
        f"google {link['account'] or 'connected'}" if link else "not connected"
    )
    return text(
        f"{user['display_name']} ({user['phone']})\n"
        f"timezone {user['timezone']}\n"
        f"hours {user['work_start']}-{user['work_end']}\n"
        f"days {days or 'none'}\n"
        f"slot {user['slot_minutes']} min\n"
        f"external calendar: {external}\n"
        f"people who can book you: {len(db.grants_given(conn, user['id']))}"
    )


def _do_connect(conn, user) -> Outcome:
    nonce = gcal.new_nonce()
    try:
        url = gcal.auth_url(gcal.sign_state(user["id"], nonce))
    except NotConfigured as exc:
        return text(f"Google Calendar is not set up on this server. {exc}")
    # Recorded so the callback can spend it once and only once.
    db.create_oauth_state(conn, nonce, user["id"])
    return text(
        "Open this link within 15 minutes and allow access. Your Google "
        "Calendar events will then block slots here, and bookings made here "
        f"will appear there:\n{url}"
    )


def _do_disconnect(conn, user) -> Outcome:
    if db.delete_calendar_link(conn, user["id"]):
        return text(
            "Google Calendar disconnected. Existing bookings stay here, but "
            "your Google events no longer block slots."
        )
    return text("No calendar was connected.")


def _do_mylink(conn, user) -> Outcome:
    hosts = db.hosts_of(conn, user["id"])
    if not hosts:
        return text(
            "You have no business booking page. An admin creates one with "
            "POST /dashboard/register."
        )
    base = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    lines = ["Your booking pages:"]
    for host in hosts:
        lines.append(f"• {host['business_name']} — webhook {base}/webhook/{host['slug']}")
    return text("\n".join(lines))


def _do_name(conn, user, rest) -> Outcome:
    if not rest:
        return text("Usage: name Burhan Husain")
    db.update_user(conn, user["id"], display_name=rest)
    return text(f"Name set to {rest}.")


def _do_tz(conn, user, rest) -> Outcome:
    if not rest:
        return text("Usage: tz Asia/Riyadh")
    if str(tz_of(rest)) != rest and rest.upper() != "UTC":
        return text(f"Unknown timezone {rest}. Use an IANA name like Asia/Riyadh.")
    db.update_user(conn, user["id"], timezone=rest)
    return text(f"Timezone set to {rest}.")


def _do_hours(conn, user, rest) -> Outcome:
    start, _, end = rest.partition("-")
    try:
        start_t, end_t = parse_hhmm(start.strip()), parse_hhmm(end.strip())
    except (ValueError, IndexError):
        return text("Usage: hours 09:00-17:00")
    if end_t <= start_t:
        return text("End time must be after start time.")
    db.update_user(
        conn, user["id"],
        work_start=f"{start_t:%H:%M}", work_end=f"{end_t:%H:%M}",
    )
    return text(f"Working hours set to {start_t:%H:%M}-{end_t:%H:%M} {user['timezone']}.")


def _do_days(conn, user, rest) -> Outcome:
    value = rest.lower().replace(" ", "")
    if not value:
        return text("Usage: days mon-fri   or   days sun,mon,tue,wed,thu")
    picked: list[int] = []
    if "-" in value and "," not in value:
        first, _, last = value.partition("-")
        if first[:3] in DAY_NAMES and last[:3] in DAY_NAMES:
            index = DAY_NAMES.index(first[:3])
            stop = DAY_NAMES.index(last[:3])
            while True:
                picked.append(index)
                if index == stop:
                    break
                index = (index + 1) % 7
    else:
        for token in value.split(","):
            if token[:3] in DAY_NAMES:
                picked.append(DAY_NAMES.index(token[:3]))
            elif token.isdigit() and 0 <= int(token) <= 6:
                picked.append(int(token))
    if not picked:
        return text("Could not read those days. Usage: days mon-fri")
    ordered = sorted(set(picked))
    db.update_user(conn, user["id"],
                   work_days=",".join(str(d) for d in ordered))
    return text("Working days: " + ", ".join(DAY_NAMES[d] for d in ordered))


# --- sharing -------------------------------------------------------------


def _do_share(conn, user, rest) -> Outcome:
    if not rest:
        return text("Usage: share +966500000000")
    target, error = db.find_user(conn, rest)
    if error:
        return text(
            f"{error}. They must message this number once before you can share "
            "with them."
        )
    if target["id"] == user["id"]:
        return text("You already have your own calendar.")
    db.grant(conn, user["id"], target["id"], can_book=True)
    note = Reply(
        text=f"{user['display_name']} shared their calendar with you. "
             f"Send: free {user['phone']} tomorrow"
    )
    return Outcome(
        Reply(text=f"{target['display_name']} can now see your free time and book you."),
        notify=[(target["phone"], note)],
    )


def _do_unshare(conn, user, rest) -> Outcome:
    if not rest:
        return text("Usage: unshare +966500000000")
    target, error = db.find_user(conn, rest)
    if error:
        return text(error)
    if db.revoke(conn, user["id"], target["id"]):
        return text(f"{target['display_name']} can no longer see your calendar.")
    return text(f"{target['display_name']} did not have access.")


def _do_shared(conn, user) -> Outcome:
    given = db.grants_given(conn, user["id"])
    got = db.grants_received(conn, user["id"])
    lines = ["Can book you:"]
    lines += [f"• {r['display_name']} ({r['phone']})" for r in given] or ["• nobody"]
    lines.append("You can book:")
    lines += [f"• {r['display_name']} ({r['phone']})" for r in got] or ["• nobody"]
    return text("\n".join(lines))


# --- availability and booking -------------------------------------------


def _do_free(conn, user, rest, now) -> Outcome:
    if not rest:
        return text("Usage: free <name|phone> [today|tomorrow|YYYY-MM-DD]")

    parts = rest.split()
    # "free tomorrow" is a question about your own calendar. Without this the
    # day word is looked up as a person: "nobody here matches tomorrow".
    if len(parts) == 1 and parse_day(parts[0], today=_local_today(user, now)):
        parts = [user["phone"], parts[0]]
    requested_day = None
    if len(parts) > 1:
        requested_day = parse_day(parts[-1], today=_local_today(user, now))
        if requested_day is not None:
            parts = parts[:-1]
    who = " ".join(parts)

    owner, error = db.find_user(conn, who)
    if error:
        return text(error)

    access = db.get_grant(conn, owner["id"], user["id"])
    if not access:
        return text(
            f"{owner['display_name']} has not shared their calendar with you.\n"
            f"Ask them to send: share {user['phone']}"
        )

    first_day = requested_day or _local_today(owner, now)
    span = 1 if requested_day else LOOKAHEAD_DAYS
    found: list[tuple[datetime, datetime]] = []
    day_used = first_day
    try:
        # One read of both calendars for the whole lookahead, not one per day.
        prefetched = prefetch_busy(conn, owner, user, first_day,
                                   first_day + timedelta(days=span))
        for offset in range(span):
            day_used = first_day + timedelta(days=offset)
            found = slots_on(conn, owner, user, day_used, now,
                              prefetched=prefetched)
            if found:
                break
    except ExternalCalendarError:
        return text(CALENDAR_UNREACHABLE.format(name=owner["display_name"]))

    if not found:
        when = "that day" if requested_day else f"the next {LOOKAHEAD_DAYS} days"
        return text(f"No open slots for {owner['display_name']} in {when}.")

    if not access["can_book"]:
        listing = ", ".join(fmt_slot(s, e, user["timezone"]) for s, e in found[:10])
        return text(
            f"{owner['display_name']} is free on {day_used:%a %d %b}: {listing} "
            f"({user['timezone']}). You have view-only access."
        )

    # On a DST fall-back day two slots share a wall-clock label, so show the
    # offset. The date comes from the viewer's timezone, not the owner's: the
    # owner's Friday slot can be the viewer's Thursday.
    offsets = ambiguous_day(found[:10], user["timezone"])
    rows = [
        Row(
            id=f"book:{owner['id']}:{int(start.timestamp())}",
            title=fmt_slot(start, end, user["timezone"], show_offset=offsets),
            description=f"{local_date(start, user['timezone']):%a %d %b}"
                        f" · {user['timezone']}",
        )
        for start, end in found[:10]
    ]
    more = ""
    if len(found) > 10:
        more = f"\nShowing first 10 of {len(found)}."
    return Outcome(
        Reply(
            header=f"{owner['display_name']} · {day_used:%a %d %b}",
            text=f"Open slots for {owner['display_name']} on {day_used:%a %d %b}, "
                 f"shown in {user['timezone']}.{more}",
            rows=rows,
            button="Book a slot",
        )
    )


def _do_tap(conn, user, raw, now) -> Outcome:
    try:
        _, owner_id_text, epoch_text = raw.split(":", 2)
        owner_id = int(owner_id_text)
        start = datetime.fromtimestamp(int(epoch_text), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return text("That button is not readable. Send: free <name> tomorrow")

    owner = db.get_user(conn, owner_id)
    if owner is None:
        return text("That person is no longer registered.")

    access = db.get_grant(conn, owner["id"], user["id"])
    if not access or not access["can_book"]:
        return text(f"You cannot book {owner['display_name']}.")

    end = start + timedelta(minutes=owner["slot_minutes"])
    if start <= now:
        return text("That slot has already started. Ask for a fresh list.")

    try:
        still_free = any(
            s == start
            for s, _ in slots_on(
                conn, owner, user,
                start.astimezone(tz_of(owner["timezone"])).date(), now)
        )
    except ExternalCalendarError:
        return text(CALENDAR_UNREACHABLE.format(name=owner["display_name"]))
    if not still_free:
        return text(
            f"{fmt_dt(start, user['timezone'])} is no longer free. "
            f"Send: free {owner['phone']} {start.astimezone(tz_of(user['timezone'])):%Y-%m-%d}"
        )

    title = f"{user['display_name']} / {owner['display_name']}"
    try:
        event, mirror_note = book(
            conn, owner, user, start, end, title,
            description=f"Booked via WhatsApp by {user['phone']}")
    except db.SlotTaken:
        return text("Someone took that slot a moment ago. Ask for a fresh list.")

    confirm = Reply(
        text=f"Booked {owner['display_name']} — {fmt_dt(start, user['timezone'])} "
             f"to {end.astimezone(tz_of(user['timezone'])):%H:%M} "
             f"({user['timezone']}).\nRef {event['ref']} · cancel with: cancel {event['ref']}"
    )
    if owner["id"] == user["id"]:
        confirm.text += mirror_note
        return Outcome(confirm)

    owner_note = Reply(
        text=f"{user['display_name']} booked you — "
             f"{fmt_dt(start, owner['timezone'])} to "
             f"{end.astimezone(tz_of(owner['timezone'])):%H:%M} "
             f"({owner['timezone']}).\nRef {event['ref']} · cancel with: "
             f"cancel {event['ref']}{mirror_note}",
        # Delivered even when the owner is outside their 24-hour window; see
        # wacal.templates.
        template=templates.build(
            templates.BOOKING_CONFIRMED,
            f"{user['display_name']} booked you",
            f"{fmt_dt(start, owner['timezone'])} to "
            f"{end.astimezone(tz_of(owner['timezone'])):%H:%M} "
            f"({owner['timezone']})",
            event["ref"],
        ),
    )
    return Outcome(confirm, notify=[(owner["phone"], owner_note)])


def _do_block(conn, user, rest, now) -> Outcome:
    parts = rest.split()
    if len(parts) < 2:
        return text("Usage: block tomorrow 14:00-15:00 Focus time")
    day = parse_day(parts[0], today=_local_today(user, now))
    if day is None:
        return text(f"Could not read the day {parts[0]!r}.")
    start_text, _, end_text = parts[1].partition("-")
    try:
        start_t, end_t = parse_hhmm(start_text), parse_hhmm(end_text)
    except (ValueError, IndexError):
        return text("Usage: block tomorrow 14:00-15:00 Focus time")
    if end_t <= start_t:
        return text("End time must be after start time.")

    zone_name = user["timezone"]
    zone = tz_of(zone_name)
    rest_words = parts[2:]

    # A spring-forward gap: the time the user typed does not exist.
    if is_skipped(day, start_t, zone_name):
        return text(
            f"{start_t:%H:%M} does not exist on {day:%a %d %b} in "
            f"{zone_name} — the clocks go forward. Pick another time."
        )

    # A fall-back repetition: the time happens twice. Honour an explicit
    # offset if given, otherwise say which one was taken and how to get the
    # other, instead of silently making the second hour unbookable.
    fold = 0
    requested_offset = next(
        (word for word in rest_words
         if re.fullmatch(r"[+-]\d{4}", word)), None)
    if requested_offset:
        rest_words = [word for word in rest_words if word != requested_offset]

    ambiguous = is_ambiguous(day, start_t, zone_name)
    if ambiguous and requested_offset:
        if offset_label(to_utc(day, start_t, zone_name, fold=1),
                        zone_name) == requested_offset:
            fold = 1
        elif offset_label(to_utc(day, start_t, zone_name, fold=0),
                          zone_name) != requested_offset:
            return text(
                f"{requested_offset} is not one of the two offsets in effect "
                f"at {start_t:%H:%M} on {day:%a %d %b}."
            )

    start = to_utc(day, start_t, zone_name, fold=fold)
    # The duration is the wall-clock difference, added to the resolved start.
    # Converting the end time independently is wrong across a transition: on a
    # fall-back night 02:00 is unambiguous while 01:30 is not, so "01:30-02:00"
    # resolved to 90 real minutes instead of the 30 the user asked for.
    duration = (datetime.combine(day, end_t)
                - datetime.combine(day, start_t))
    end = start + duration
    if end <= start:
        return text("End time must be after start time.")
    title = " ".join(rest_words) or "Busy"

    try:
        overlap = busy.busy_for(conn, user["id"], start, end)
    except ExternalCalendarError:
        return text(CALENDAR_UNREACHABLE.format(name="your"))
    if overlap:
        return text("That overlaps something already on your calendar.")
    try:
        event, mirror_note = book(conn, user, user, start, end, title,
                                  description="Blocked via WhatsApp")
    except db.SlotTaken:
        return text("That slot is already blocked.")
    note = ""
    if ambiguous and not requested_offset:
        other = offset_label(to_utc(day, start_t, zone_name, fold=1), zone_name)
        note = (
            f"\nThe clocks go back on {day:%a %d %b}, so {start_t:%H:%M} "
            f"happens twice. This is the first one "
            f"({offset_label(start, zone_name)}). For the other, repeat the "
            f"command with {other} on the end."
        )
    # Across a fall-back the wall clock can run backwards ("01:30-01:00"),
    # which is right but unreadable without the offsets.
    marks = (f" {offset_label(start, zone_name)}",
             f" {offset_label(end, zone_name)}") if ambiguous else ("", "")
    return text(
        f"Blocked {fmt_dt(start, zone_name)}{marks[0]}-"
        f"{end.astimezone(zone):%H:%M}{marks[1]} — {title}\n"
        f"Ref {event['ref']} · unblock with: cancel {event['ref']}"
        f"{mirror_note}{note}"
    )


def _do_closed(conn, user, rest, now) -> Outcome:
    """Block a whole working day. The holiday command.

    Marking a clinic closed used to need an admin PATCH of working_days, which
    no business owner is going to do. One public holiday without this and the
    page takes bookings on a day nobody is there.
    """
    if not rest:
        return text("Usage: closed tomorrow   or   closed 2026-12-25")
    day = parse_day(rest.split()[0], today=_local_today(user, now))
    if day is None:
        return text(f"Could not read the day {rest.split()[0]!r}.")
    reason = " ".join(rest.split()[1:]) or "Closed"

    # Cover the user's own hours and every host they own, since one WhatsApp
    # number can run several booking pages with different windows.
    schedules = [schedule_for(user)]
    hosts = db.hosts_of(conn, user["id"])
    schedules += [schedule_for(user, host) for host in hosts]
    windows = [w for w in (work_window(s, day) for s in schedules) if w]
    if not windows:
        return text(f"{day:%a %d %b} is already outside your working days.")

    start = min(w[0] for w in windows)
    end = max(w[1] for w in windows)
    existing = db.busy_intervals(conn, user["id"], start, end)
    if existing:
        return text(
            f"{day:%a %d %b} already has {len(existing)} booking(s). "
            f"Cancel them first with: my"
        )
    try:
        event, mirror_note = book(conn, user, user, start, end, reason,
                                  description="Closed via WhatsApp")
    except db.SlotTaken:
        return text(f"{day:%a %d %b} is already blocked.")
    zone = user["timezone"]
    return text(
        f"Closed {day:%a %d %b} — {start.astimezone(tz_of(zone)):%H:%M} to "
        f"{end.astimezone(tz_of(zone)):%H:%M} ({zone}) is now blocked across "
        f"{len(windows)} schedule(s).\n"
        f"Ref {event['ref']} · reopen with: cancel {event['ref']}{mirror_note}"
    )


def _do_my(conn, user, now) -> Outcome:
    rows = db.events_for(conn, user["id"], now)
    if not rows:
        return text("Nothing upcoming.")
    lines = [f"Upcoming ({user['timezone']}):"]
    for row in rows:
        start = datetime.fromisoformat(row["start_utc"])
        end = datetime.fromisoformat(row["end_utc"])
        if row["owner_id"] == user["id"] and row["created_by"] == user["id"]:
            whose = "yours"
        elif row["owner_id"] == user["id"]:
            whose = f"booked by {row['booker_name']}"
        else:
            whose = f"with {row['owner_name']}"
        lines.append(
            f"• {row['ref']} {fmt_dt(start, user['timezone'])}-"
            f"{end.astimezone(tz_of(user['timezone'])):%H:%M} · {whose}"
        )
    return text("\n".join(lines))


def _do_cancel(conn, user, rest) -> Outcome:
    ref = rest.strip().upper()
    if not ref:
        return text("Usage: cancel ABC123   (get refs with: my)")
    event = db.get_event(conn, ref)
    if event is None:
        return text(f"No booking with ref {ref}.")
    if user["id"] not in (event["owner_id"], event["created_by"]):
        return text("Only the owner or the person who booked it can cancel.")
    if event["status"] != "confirmed":
        return text(f"{ref} is already cancelled.")
    # release() deletes the mirrored copy by its stored external id, never by
    # the local ref.
    stale = release(conn, event)

    start = datetime.fromisoformat(event["start_utc"])
    confirm = Reply(text=f"Cancelled {ref} — {fmt_dt(start, user['timezone'])}. "
                         f"The slot is free again.{stale}")
    other_id = (
        event["created_by"] if user["id"] == event["owner_id"] else event["owner_id"]
    )
    if other_id == user["id"]:
        return Outcome(confirm)
    other = db.get_user(conn, other_id)
    note = Reply(
        text=f"{user['display_name']} cancelled {ref} — "
             f"{fmt_dt(start, other['timezone'])} ({other['timezone']}).",
        template=templates.build(
            templates.BOOKING_CANCELLED,
            f"{user['display_name']} cancelled a booking with you",
            f"{fmt_dt(start, other['timezone'])} ({other['timezone']})",
            ref,
        ),
    )
    return Outcome(confirm, notify=[(other["phone"], note)])


def _local_today(user, now: datetime) -> date:
    return now.astimezone(tz_of(user["timezone"])).date()
