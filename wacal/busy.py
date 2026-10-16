"""Availability across both calendars, plus mirroring bookings outward.

This is the only place that knows a user may have an external calendar. The
slot engine and the command layer deal in plain busy intervals.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db, gcal
from .gcal import ExternalCalendarError


def _norm(value: str) -> str:
    """Google returns RFC3339 with 'Z'; store everything as UTC with offset."""
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def busy_for(conn, user_id: int, start: datetime, end: datetime,
             *, external: bool = True) -> list[tuple[str, str]]:
    """Busy intervals from this app plus the user's linked calendar.

    Raises ExternalCalendarError if a link exists but cannot be read. Callers
    must surface that rather than treat the window as free.
    """
    intervals = list(db.busy_intervals(conn, user_id, start, end))
    if not external:
        return intervals

    link = db.get_calendar_link(conn, user_id)
    if link is None:
        return intervals

    def on_refresh(token_json: str) -> None:
        db.save_calendar_link(conn, user_id, token_json,
                              provider=link["provider"],
                              calendar_id=link["calendar_id"],
                              account=link["account"])

    external_busy = gcal.PROVIDER.busy(
        db.token_of(link), link["calendar_id"], start, end,
        on_refresh=on_refresh,
    )
    intervals.extend((_norm(s), _norm(e)) for s, e in external_busy)
    return intervals


def mirror_create(conn, owner, event, *, description: str = "",
                  location: str = "") -> bool:
    """Copy a confirmed booking into the owner's external calendar.

    Returns False when there is no link or the copy failed. The booking here
    stands either way -- this app is the authority -- but a False result means
    the owner's other calendar app will not show it, so tell them.
    """
    link = db.get_calendar_link(conn, owner["id"])
    if link is None:
        return False

    def on_refresh(token_json: str) -> None:
        db.save_calendar_link(conn, owner["id"], token_json,
                              provider=link["provider"],
                              calendar_id=link["calendar_id"],
                              account=link["account"])

    try:
        external_id = gcal.PROVIDER.create_event(
            db.token_of(link), link["calendar_id"],
            start=datetime.fromisoformat(event["start_utc"]),
            end=datetime.fromisoformat(event["end_utc"]),
            title=event["title"], description=description, location=location,
            on_refresh=on_refresh,
        )
    except ExternalCalendarError:
        return False
    if external_id:
        db.set_external_id(conn, event["ref"], external_id)
    return bool(external_id)


def external_events(conn, user_id: int, start: datetime, end: datetime,
                    *, limit: int = 50) -> list[dict] | None:
    """Titled events from the user's own linked calendar, or None if unlinked."""
    link = db.get_calendar_link(conn, user_id)
    if link is None:
        return None

    def on_refresh(token_json: str) -> None:
        db.save_calendar_link(conn, user_id, token_json,
                              provider=link["provider"],
                              calendar_id=link["calendar_id"],
                              account=link["account"])

    return gcal.PROVIDER.list_events(db.token_of(link), link["calendar_id"],
                                     start, end, limit=limit,
                                     on_refresh=on_refresh)


def mirror_delete(conn, event) -> bool:
    """Delete the mirrored copy. Returns False if there was nothing to delete."""
    external_id = event["external_id"] if "external_id" in event.keys() else None
    if not external_id:
        return False
    link = db.get_calendar_link(conn, event["owner_id"])
    if link is None:
        return False

    def on_refresh(token_json: str) -> None:
        db.save_calendar_link(conn, event["owner_id"], token_json,
                              provider=link["provider"],
                              calendar_id=link["calendar_id"],
                              account=link["account"])

    try:
        gcal.PROVIDER.delete_event(db.token_of(link), link["calendar_id"],
                                   external_id, on_refresh=on_refresh)
    except ExternalCalendarError:
        return False
    return True
