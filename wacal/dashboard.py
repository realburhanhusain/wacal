"""Admin API for business mode: register a host, read bookings and stats.

Guarded by a single ADMIN_KEY in the x-api-key header. With ADMIN_KEY unset
every route returns 503 rather than running unauthenticated -- an admin API
that silently opens itself when a variable is missing is worse than no API.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import busy, db
from .gcal import ExternalCalendarError
from .slots import parse_hhmm, schedule_for
from .whatsapp import normalise_phone

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def require_admin(key: str | None) -> None:
    expected = os.environ.get("ADMIN_KEY", "")
    if not expected:
        raise HTTPException(status_code=503,
                            detail="ADMIN_KEY is not set on this server")
    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="bad api key")


class Service(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    duration_mins: int = Field(default=30, ge=5, le=480)
    price: float = Field(default=0, ge=0)


class WorkingHours(BaseModel):
    start: str = "09:00"
    end: str = "17:00"

    @field_validator("start", "end")
    @classmethod
    def valid_time(cls, value: str) -> str:
        try:
            parsed = parse_hhmm(value)
        except (ValueError, IndexError) as exc:
            raise ValueError("expected HH:MM") from exc
        return f"{parsed:%H:%M}"

    @model_validator(mode="after")
    def end_after_start(self):
        # An inverted window makes work_window return None for every day, so
        # the page silently shows no openings at all.
        if parse_hhmm(self.end) <= parse_hhmm(self.start):
            raise ValueError("working_hours.end must be after start")
        return self


class HostIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,38}$")
    business_name: str = Field(min_length=1, max_length=120)
    owner_phone: str = Field(min_length=6, max_length=20)
    owner_name: str = ""
    owner_email: str = ""
    location: str = ""
    currency: str = "SAR"
    timezone: str = "Asia/Riyadh"
    services: list[Service] = []
    working_days: list[int] = [0, 1, 2, 3, 4]
    working_hours: WorkingHours = WorkingHours()
    slot_minutes: int = Field(default=30, ge=5, le=480)

    @field_validator("working_days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        if not value or any(d < 0 or d > 6 for d in value):
            raise ValueError("working_days must be 0..6 with Monday=0")
        return sorted(set(value))

    @field_validator("owner_phone")
    @classmethod
    def canonical_phone(cls, value: str) -> str:
        phone = normalise_phone(value)
        if len(phone) < 7:
            raise ValueError("owner_phone needs at least 6 digits")
        return phone

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value


def _known_timezone(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone {value!r}") from exc
    return value


class HostPatch(BaseModel):
    business_name: str | None = None
    location: str | None = None
    currency: str | None = None
    owner_email: str | None = None
    timezone: str | None = None
    services: list[Service] | None = None
    working_days: list[int] | None = None
    working_hours: WorkingHours | None = None
    slot_minutes: int | None = Field(default=None, ge=5, le=480)

    @field_validator("working_days")
    @classmethod
    def valid_days(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if not value or any(d < 0 or d > 6 for d in value):
            raise ValueError("working_days must be 0..6 with Monday=0")
        return sorted(set(value))

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        return _known_timezone(value)


class HostPut(BaseModel):
    """A full replacement of the editable host configuration."""

    business_name: str = Field(min_length=1, max_length=120)
    location: str = ""
    currency: str = "SAR"
    owner_email: str = ""
    timezone: str | None = None
    services: list[Service] = []
    working_days: list[int] = [0, 1, 2, 3, 4]
    working_hours: WorkingHours = WorkingHours()
    slot_minutes: int = Field(default=30, ge=5, le=480)

    @field_validator("working_days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        if not value or any(d < 0 or d > 6 for d in value):
            raise ValueError("working_days must be 0..6 with Monday=0")
        return sorted(set(value))

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        return _known_timezone(value)


def _host_view(conn, host) -> dict:
    owner = db.get_user(conn, host["owner_id"])
    schedule = schedule_for(owner, host) if owner else {}
    return {
        "slug": host["slug"],
        "business_name": host["business_name"],
        "location": host["location"],
        "currency": host["currency"],
        "owner_phone": owner["phone"] if owner else None,
        "owner_name": owner["display_name"] if owner else None,
        "owner_email": host["owner_email"],
        "timezone": schedule.get("timezone"),
        "timezone_source": "host" if host["timezone"] else "owner",
        "working_hours": {"start": schedule.get("work_start"),
                          "end": schedule.get("work_end")},
        "working_days": [int(d) for d in str(schedule.get("work_days", "")).split(",")
                         if d.strip()],
        "slot_minutes": schedule.get("slot_minutes"),
        "schedule_source": "host" if host["work_start"] else "owner",
        "services": json.loads(host["services"]),
        "google_calendar_connected":
            db.get_calendar_link(conn, host["owner_id"]) is not None,
        "created_at": host["created_at"],
    }


@router.post("/register", status_code=201)
async def register(payload: HostIn, request: Request,
                   x_api_key: str | None = Header(default=None)):
    require_admin(x_api_key)
    conn = request.app.state.conn

    if db.get_host(conn, payload.slug) is not None:
        raise HTTPException(status_code=409, detail="slug already registered")

    owner = db.get_user_by_phone(conn, payload.owner_phone)
    if owner is None:
        # Only a brand-new owner takes the host's schedule as their personal
        # one. Overwriting an existing user's own hours and timezone would
        # contradict the whole point of storing opening hours on the host.
        owner = db.create_user(conn, payload.owner_phone,
                               payload.owner_name or payload.business_name,
                               payload.timezone)
        db.update_user(
            conn, owner["id"],
            work_start=payload.working_hours.start,
            work_end=payload.working_hours.end,
            work_days=",".join(str(d) for d in payload.working_days),
        )
    host = db.create_host(
        conn, payload.slug, owner["id"], payload.business_name,
        location=payload.location,
        services=json.dumps([s.model_dump() for s in payload.services]),
        currency=payload.currency,
        owner_email=payload.owner_email,
        timezone=payload.timezone,
        # Opening hours and timezone are stored on the host as well, so a
        # second business under the same owner can differ.
        work_start=payload.working_hours.start,
        work_end=payload.working_hours.end,
        work_days=",".join(str(d) for d in payload.working_days),
        slot_minutes=payload.slot_minutes,
    )
    base = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    return {
        "slug": host["slug"],
        "owner_phone": owner["phone"],
        "webhook_url": f"{base}/webhook/{host['slug']}",
        "twilio_webhook_url": f"{base}/twilio/webhook/{host['slug']}",
        "connect_google": "send 'connect google' from the owner's WhatsApp",
        "next_steps": [
            f"1. Point the WhatsApp number's webhook at {base}/webhook/"
            f"{host['slug']} (Meta Cloud API) or {base}/twilio/webhook/"
            f"{host['slug']} (Twilio)",
            "2. From the owner's WhatsApp, send: connect google",
            "3. Share the WhatsApp number with customers",
        ],
    }


@router.get("/{slug}")
async def get_host(slug: str, request: Request,
                   x_api_key: str | None = Header(default=None)):
    require_admin(x_api_key)
    conn = request.app.state.conn
    host = db.get_host(conn, slug)
    if host is None:
        raise HTTPException(status_code=404, detail="unknown slug")
    return _host_view(conn, host)


@router.patch("/{slug}")
async def patch_host(slug: str, payload: HostPatch, request: Request,
                     x_api_key: str | None = Header(default=None)):
    require_admin(x_api_key)
    conn = request.app.state.conn
    if db.get_host(conn, slug) is None:
        raise HTTPException(status_code=404, detail="unknown slug")

    fields = _editable_fields(payload, partial=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    host = db.update_host(conn, slug, **fields)
    return {"slug": host["slug"], "updated": sorted(fields)}


@router.put("/{slug}")
async def put_host(slug: str, payload: HostPut, request: Request,
                   x_api_key: str | None = Header(default=None)):
    """Replace the whole editable configuration, including opening hours."""
    require_admin(x_api_key)
    conn = request.app.state.conn
    if db.get_host(conn, slug) is None:
        raise HTTPException(status_code=404, detail="unknown slug")
    db.update_host(conn, slug, **_editable_fields(payload, partial=False))
    return _host_view(conn, db.get_host(conn, slug))


def _editable_fields(payload, *, partial: bool) -> dict:
    """Translate a request model into db.update_host keyword arguments."""
    fields: dict[str, object] = {}
    for name in ("business_name", "location", "currency", "owner_email",
                 "timezone", "slot_minutes"):
        value = getattr(payload, name, None)
        if value is not None or not partial:
            fields[name] = value
    if payload.services is not None or not partial:
        fields["services"] = json.dumps(
            [s.model_dump() for s in (payload.services or [])]
        )
    if payload.working_days is not None or not partial:
        fields["work_days"] = ",".join(str(d) for d in payload.working_days)
    if payload.working_hours is not None or not partial:
        fields["work_start"] = payload.working_hours.start
        fields["work_end"] = payload.working_hours.end
    return fields


@router.get("/{slug}/bookings")
async def bookings(slug: str, request: Request,
                   days: int = Query(default=7, ge=1, le=365),
                   x_api_key: str | None = Header(default=None)):
    require_admin(x_api_key)
    conn = request.app.state.conn
    host = db.get_host(conn, slug)
    if host is None:
        raise HTTPException(status_code=404, detail="unknown slug")

    now = db.now_utc()
    rows = db.host_bookings(conn, slug, now, now + timedelta(days=days))
    return {
        "slug": host["slug"],
        "business_name": host["business_name"],
        "days": days,
        "count": len(rows),
        "bookings": [
            {
                "ref": r["ref"],
                "start_utc": r["start_utc"],
                "end_utc": r["end_utc"],
                "service": r["service"],
                "customer_name": r["customer_name"],
                "customer_phone": r["customer_phone"],
                "mirrored_to_google": bool(r["external_id"]),
            }
            for r in rows
        ],
    }


@router.get("/{slug}/stats")
async def stats(slug: str, request: Request,
                days: int = Query(default=30, ge=1, le=365),
                x_api_key: str | None = Header(default=None)):
    require_admin(x_api_key)
    conn = request.app.state.conn
    host = db.get_host(conn, slug)
    if host is None:
        raise HTTPException(status_code=404, detail="unknown slug")

    now = db.now_utc()
    since = now - timedelta(days=days)
    totals, by_service = db.host_stats(conn, slug, since)
    confirmed = totals["confirmed"] or 0
    cancelled = totals["cancelled"] or 0
    return {
        "slug": host["slug"],
        "window_days": days,
        # Forward-looking counts: what is actually on the books.
        "upcoming_7_days": len(db.host_bookings(conn, slug, now,
                                                now + timedelta(days=7))),
        "upcoming_30_days": len(db.host_bookings(conn, slug, now,
                                                 now + timedelta(days=30))),
        # Backward-looking counts over window_days, by when it was booked.
        "confirmed": confirmed,
        "cancelled": cancelled,
        "distinct_customers": totals["customers"] or 0,
        "cancellation_rate": round(cancelled / (confirmed + cancelled), 3)
        if confirmed + cancelled else 0.0,
        "by_service": [{"service": r["service"], "count": r["n"]}
                       for r in by_service],
    }


@router.get("/{slug}/calendar")
async def external_calendar(slug: str, request: Request,
                            days: int = Query(default=7, ge=1, le=90),
                            x_api_key: str | None = Header(default=None)):
    """The owner's own Google Calendar events, titles included.

    This is the owner's calendar shown to the owner's admin key, which is why
    titles are allowed here and never in a free/busy reply to a third party.
    """
    require_admin(x_api_key)
    conn = request.app.state.conn
    host = db.get_host(conn, slug)
    if host is None:
        raise HTTPException(status_code=404, detail="unknown slug")

    now = db.now_utc()
    try:
        events = busy.external_events(conn, host["owner_id"], now,
                                      now + timedelta(days=days))
    except ExternalCalendarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if events is None:
        return {"slug": host["slug"], "connected": False, "events": []}
    return {"slug": host["slug"], "connected": True, "days": days,
            "count": len(events), "events": events}
