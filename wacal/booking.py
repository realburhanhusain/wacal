"""The booking engine both conversation modes sit on.

Peer mode (wacal.commands) and business mode (wacal.business) differ in their
authorisation model and their conversation shape, not in their arithmetic. This
module owns what they share: the reply envelope, registration, availability
across both calendar sources, and the write paths for booking and cancelling.

business.py used to import these as private names from commands.py, and the
cancel path existed twice in near-identical form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import busy, db
from .slots import mutual_free_slots
from .whatsapp import Inbound, Reply

# Words that must never be mistaken for somebody's name.
KNOWN_VERBS = {
    "help", "book", "me", "name", "register", "tz", "timezone", "hours",
    "days", "share", "unshare", "shared", "free", "slots", "availability",
    "block", "closed", "my", "mine", "bookings", "cancel", "unblock",
    "connect", "disconnect", "mylink", "link", "reschedule", "change",
    "yes", "no",
}

CALENDAR_UNREACHABLE = (
    "Cannot read {name}'s connected Google Calendar right now, so I will not "
    "guess at free time. Try again in a moment."
)

MIRROR_WRITE_FAILED = ("\nNote: this booking is held here but could not be "
                       "written to your Google Calendar.")
MIRROR_DELETE_FAILED = ("\nNote: the copy in Google Calendar could not be "
                        "removed; delete it there as well.")


@dataclass
class Outcome:
    reply: Reply
    notify: list[tuple[str, Reply]] = field(default_factory=list)


def text(body: str) -> Outcome:
    return Outcome(Reply(text=body))


# --- registration --------------------------------------------------------


def ensure_user(conn, msg: Inbound, *, welcome: str = "", tz: str = "UTC"):
    """Resolve the sender to a user row, registering them if needed.

    Returns (user, outcome). A non-None outcome is the reply to send instead of
    continuing -- a request for their name.

    tz is the timezone a newly created user starts in. Business mode passes the
    host's, because every reply renders in the viewer's timezone and a customer
    who has never heard of this app cannot be asked to set one first.
    """
    from . import keywords

    user = db.get_user_by_phone(conn, msg.from_phone)
    if user is not None:
        return user, None

    if msg.profile_name:
        return db.create_user(conn, msg.from_phone, msg.profile_name, tz), None

    lowered = msg.text.lower()
    for prefix in ("name ", "register ", "i am ", "im "):
        if lowered.startswith(prefix):
            chosen = msg.text[len(prefix):].strip()
            if chosen:
                return db.create_user(conn, msg.from_phone, chosen, tz), None
            break
    else:
        # A bare "Ravi" is the obvious answer to "what name should people
        # see?", so accept it. Without this the prompt repeated forever. A
        # known verb or alias is never mistaken for a name.
        candidate = " ".join(msg.text.split())
        first = candidate.split()[0].lower() if candidate else ""
        if (candidate and len(candidate) <= 40
                and keywords.canonical(candidate) is None
                and first not in KNOWN_VERBS):
            return db.create_user(conn, msg.from_phone, candidate, tz), None

    return None, text(
        "Welcome. What name should people see?\nReply: name Burhan"
    )


# --- availability --------------------------------------------------------


def prefetch_busy(conn, owner, requester, first_day: date, last_day: date,
                  *, mutual: bool = True):
    """Both parties' busy intervals for a whole date range, in one pass.

    Scanning day by day meant one Google round trip per candidate day -- up to
    60 for a single "pick a service" tap, serially, while holding the lock.
    """
    start = datetime.combine(first_day, datetime.min.time(),
                             tzinfo=timezone.utc) - timedelta(days=1)
    end = datetime.combine(last_day, datetime.min.time(),
                           tzinfo=timezone.utc) + timedelta(days=2)
    owner_busy = busy.busy_for(conn, owner["id"], start, end)
    if not mutual or owner["id"] == requester["id"]:
        return owner_busy, owner_busy
    return owner_busy, busy.busy_for(conn, requester["id"], start, end)


def slots_on(conn, owner, requester, day: date, now: datetime,
             *, slot_minutes: int | None = None, mutual: bool = True,
             requester_hours: bool = True, prefetched=None):
    """Free slots on one day, across both people and both calendar sources.

    Raises ExternalCalendarError when a connected Google Calendar cannot be
    read. Callers must pass that on to the user instead of offering slots.

    prefetched is an (owner_busy, requester_busy) pair covering at least this
    day; pass it when looping over days so the calendars are read once.
    """
    if prefetched is not None:
        owner_busy, requester_busy = prefetched
    else:
        # A day either side, so an event starting the previous evening and
        # running past midnight still counts as busy.
        owner_busy, requester_busy = prefetch_busy(
            conn, owner, requester, day - timedelta(days=1),
            day + timedelta(days=1), mutual=mutual,
        )

    effective_requester = (
        owner if (not mutual or owner["id"] == requester["id"]) else requester
    )
    return mutual_free_slots(
        owner, owner_busy, effective_requester, requester_busy, day, now=now,
        slot_minutes=slot_minutes, requester_hours=requester_hours,
    )


# --- writes --------------------------------------------------------------


def mirror(conn, owner, event, *, description: str = "",
           location: str = "") -> str:
    """Copy the booking to the owner's external calendar, if they linked one.

    Returns a sentence to append to the owner's message when the copy failed,
    so a silent divergence between the two calendars cannot go unnoticed.
    """
    if db.get_calendar_link(conn, owner["id"]) is None:
        return ""
    if busy.mirror_create(conn, owner, event, description=description,
                          location=location):
        return ""
    return MIRROR_WRITE_FAILED


def book(conn, owner, requester, start: datetime, end: datetime, title: str,
         *, host_slug: str | None = None, service: str | None = None,
         description: str = "", location: str = ""):
    """Create a confirmed event and mirror it outward.

    Returns (event, mirror_note). Raises db.SlotTaken if the owner already has
    a confirmed event starting at that instant.
    """
    event = db.create_event(conn, owner["id"], requester["id"], start, end,
                            title, host_slug=host_slug, service=service)
    return event, mirror(conn, owner, event, description=description,
                         location=location)


def release(conn, event) -> str:
    """Cancel an event and remove its mirrored copy.

    Returns a note for the user when the external copy could not be deleted;
    the local cancellation stands either way.
    """
    db.cancel_event(conn, event["ref"])
    if event["external_id"] and not busy.mirror_delete(conn, event):
        return MIRROR_DELETE_FAILED
    return ""
