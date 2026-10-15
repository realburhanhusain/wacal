"""Google Calendar as a second busy source, opt-in per user.

Two deliberate choices here, both reactions to how this usually goes wrong:

* A failure to reach Google raises ExternalCalendarError. It never degrades to
  "assume free". Showing a slot as open because an API call failed is how a
  booking lands on top of a real meeting.
* Bookings are mirrored into Google and the returned event id is stored, so
  cancelling here deletes the event there. Storing a local booking id and
  passing it to the delete call leaves the external calendar blocked forever.

All times crossing this boundary are timezone-aware UTC. Google accepts RFC3339
with an explicit offset; appending "Z" to a naive local datetime silently
shifts the query window by the local offset.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE",
                                     "./client_secrets.json")
STATE_TTL_SECONDS = 900
MAC_BYTES = 16


class ExternalCalendarError(RuntimeError):
    """Google was configured for this user but could not be reached."""


class NotConfigured(RuntimeError):
    """No client_secrets.json, so the OAuth flow cannot start."""


def _state_secret() -> bytes:
    secret = os.environ.get("WACAL_STATE_SECRET")
    if not secret:
        # Derived from the app secret when available so restarts keep working.
        secret = os.environ.get("WA_APP_SECRET") or "dev-state-secret"
    return secret.encode()


def sign_state(user_id: int, *, now: float | None = None) -> str:
    """A short-lived, signed token standing in for a login session.

    The user gets the OAuth link over WhatsApp, where their phone number has
    already been authenticated by the carrier and by Meta. The token binds that
    link to one user id for 15 minutes.
    """
    body = f"{user_id}:{int(now or time.time())}".encode()
    mac = hmac.new(_state_secret(), body, hashlib.sha256).digest()[:MAC_BYTES]
    # The MAC is fixed length and split off by position. A delimiter byte would
    # not work: the digest can contain any byte, including the delimiter.
    return base64.urlsafe_b64encode(body + mac).decode().rstrip("=")


def verify_state(token: str, *, now: float | None = None) -> int:
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode())
        if len(raw) <= MAC_BYTES:
            raise ValueError("too short")
        body, mac = raw[:-MAC_BYTES], raw[-MAC_BYTES:]
        expected = hmac.new(_state_secret(), body, hashlib.sha256).digest()[:MAC_BYTES]
        if not hmac.compare_digest(expected, mac):
            raise ValueError("bad signature")
        user_id_text, _, issued_text = body.decode().partition(":")
        issued = int(issued_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("unreadable state token") from exc
    if (now or time.time()) - issued > STATE_TTL_SECONDS:
        raise ValueError("state token expired")
    return int(user_id_text)


def redirect_uri() -> str:
    base = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/auth/google/callback"


def _flow():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise NotConfigured(
            f"{CLIENT_SECRETS_FILE} not found. Download an OAuth 2.0 Web "
            "application client from the Google Cloud console."
        )
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES)
    flow.redirect_uri = redirect_uri()
    return flow


def auth_url(state: str) -> str:
    url, _ = _flow().authorization_url(
        access_type="offline",       # we need a refresh token
        include_granted_scopes="true",
        prompt="consent",            # otherwise a re-link returns no refresh token
        state=state,
    )
    return url


def exchange_code(code: str) -> tuple[str, str]:
    """Swap an authorization code for stored credentials. Returns (json, email)."""
    flow = _flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    email = ""
    try:
        from googleapiclient.discovery import build

        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        email = service.userinfo().get().execute().get("email", "")
    except Exception:  # noqa: BLE001 - the address is a nicety, not required
        email = ""
    return creds.to_json(), email


class GoogleCalendar:
    """Thin wrapper over the Calendar v3 API for one linked account."""

    def _service(self, token_json: str, on_refresh=None):
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_json), SCOPES
            )
        except (ValueError, KeyError) as exc:
            raise ExternalCalendarError(f"stored token unusable: {exc}") from exc

        if not creds.valid:
            if not creds.refresh_token:
                raise ExternalCalendarError(
                    "token expired and no refresh token; relink required"
                )
            try:
                creds.refresh(GoogleRequest())
            except Exception as exc:  # noqa: BLE001 - surfaces as a user message
                raise ExternalCalendarError(f"token refresh failed: {exc}") from exc
            if on_refresh:
                on_refresh(creds.to_json())
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def busy(self, token_json: str, calendar_id: str, start: datetime,
             end: datetime, *, on_refresh=None) -> list[tuple[str, str]]:
        service = self._service(token_json, on_refresh)
        body = {
            "timeMin": start.astimezone(timezone.utc).isoformat(),
            "timeMax": end.astimezone(timezone.utc).isoformat(),
            "items": [{"id": calendar_id}],
        }
        try:
            result = service.freebusy().query(body=body).execute()
        except Exception as exc:  # noqa: BLE001
            raise ExternalCalendarError(f"freebusy query failed: {exc}") from exc

        calendar = result.get("calendars", {}).get(calendar_id, {})
        if calendar.get("errors"):
            raise ExternalCalendarError(str(calendar["errors"]))
        return [(b["start"], b["end"]) for b in calendar.get("busy", [])]

    def create_event(self, token_json: str, calendar_id: str, *,
                     start: datetime, end: datetime, title: str,
                     description: str = "", location: str = "",
                     on_refresh=None) -> str:
        service = self._service(token_json, on_refresh)
        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
            "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ]},
        }
        try:
            created = service.events().insert(
                calendarId=calendar_id, body=body
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise ExternalCalendarError(f"event insert failed: {exc}") from exc
        return created.get("id", "")

    def list_events(self, token_json: str, calendar_id: str, start: datetime,
                    end: datetime, *, limit: int = 50,
                    on_refresh=None) -> list[dict]:
        """Events in a window, with their titles.

        Only ever shown to the calendar's own owner through the admin API --
        free/busy for anyone else returns intervals without titles.
        """
        service = self._service(token_json, on_refresh)
        try:
            result = service.events().list(
                calendarId=calendar_id,
                timeMin=start.astimezone(timezone.utc).isoformat(),
                timeMax=end.astimezone(timezone.utc).isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=limit,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise ExternalCalendarError(f"event list failed: {exc}") from exc
        return [
            {
                "id": item.get("id", ""),
                "summary": item.get("summary", ""),
                "start": (item.get("start") or {}).get(
                    "dateTime", (item.get("start") or {}).get("date", "")),
                "end": (item.get("end") or {}).get(
                    "dateTime", (item.get("end") or {}).get("date", "")),
            }
            for item in result.get("items", [])
        ]

    def delete_event(self, token_json: str, calendar_id: str,
                     external_id: str, *, on_refresh=None) -> None:
        service = self._service(token_json, on_refresh)
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=external_id
            ).execute()
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            # Already gone on their side is the outcome we wanted.
            if "404" in message or "410" in message or "deleted" in message:
                return
            raise ExternalCalendarError(f"event delete failed: {exc}") from exc


# Swapped out in tests. Keep the attribute, not the class, as the seam.
PROVIDER = GoogleCalendar()
