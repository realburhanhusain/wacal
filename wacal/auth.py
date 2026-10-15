"""Google Calendar OAuth routes.

There is no password login anywhere in this app. The user asks for a link over
WhatsApp, where Meta has already authenticated their number, and that link
carries a signed, 15-minute, single-user state token. The callback trusts the
token, not a session cookie.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from . import db, gcal

router = APIRouter(prefix="/auth/google", tags=["auth"])


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width'>"
        "<style>body{font:16px/1.5 system-ui;margin:3rem auto;max-width:34rem;"
        "padding:0 1rem}h1{font-size:1.3rem}</style>"
        f"<h1>{title}</h1><p>{body}</p>"
    )


@router.get("/start")
async def start(request: Request, state: str = Query(...)):
    """Bounce a signed state token straight to Google's consent screen."""
    try:
        gcal.verify_state(state)
    except (ValueError, gcal.NotConfigured) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        url = gcal.auth_url(state)
    except gcal.NotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url)


@router.get("/callback")
async def callback(request: Request,
                   state: str = Query(default=""),
                   code: str = Query(default=""),
                   error: str = Query(default="")):
    if error:
        return _page("Not connected", f"Google reported: {error}")
    if not code:
        return _page("Not connected", "Google did not return an authorisation code.")
    conn = request.app.state.conn
    try:
        user_id, nonce = gcal.verify_state(state)
    except (ValueError, gcal.NotConfigured) as exc:
        return _page("Link expired",
                     f"{exc}. Send <b>connect google</b> on WhatsApp again.")

    # Single use. A forged or replayed state has no row to spend, which is the
    # difference between "signed" and "actually issued by us to this user".
    if not db.consume_oauth_state(conn, nonce, user_id):
        return _page("Link already used",
                     "That link was already used or was not issued by us. "
                     "Send <b>connect google</b> on WhatsApp again.")

    user = db.get_user(conn, user_id)
    if user is None:
        return _page("Unknown user", "That link is no longer valid.")

    try:
        token_json, account = gcal.exchange_code(code)
    except gcal.NotConfigured as exc:
        return _page("Not configured", str(exc))
    except Exception as exc:  # noqa: BLE001 - shown to the user, not swallowed
        return _page("Could not finish", f"Token exchange failed: {exc}")

    db.save_calendar_link(conn, user_id, token_json, account=account)
    return _page(
        "Calendar connected",
        f"{account or 'Your Google Calendar'} is linked to "
        f"{user['display_name']}. Your Google events now block slots, and "
        "bookings made over WhatsApp appear in Google. You can close this tab."
    )
