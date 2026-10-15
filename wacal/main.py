"""FastAPI app: webhooks for both transports and both modes.

    POST /webhook                  peer mode, Meta Cloud API
    POST /webhook/{slug}           business mode, Meta Cloud API
    POST /twilio/webhook           peer mode, Twilio
    POST /twilio/webhook/{slug}    business mode, Twilio

Run locally:
    python start.py                migrate, seed a demo host, then serve
    uvicorn wacal.main:app --reload --port 8000

Then expose the port (ngrok, cloudflared) and register the public URL with the
provider: Meta needs WA_VERIFY_TOKEN on the GET handshake, Twilio needs its
"when a message comes in" webhook pointed at the matching /twilio path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from . import db, gcal, numbering
from .auth import router as auth_router
from .business import handle_business
from .commands import handle
from .dashboard import router as dashboard_router
from .whatsapp import (
    build_transport,
    parse_twilio_form,
    parse_webhook,
    valid_twilio_signature,
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Open the database before the first request.

    The dashboard and auth routers read app.state.conn directly, so a lazy
    connection opened only by the webhook route would leave them with None.
    """
    if getattr(application.state, "conn", None) is None:
        application.state.conn = db.connect()
    if getattr(application.state, "transport", None) is None:
        application.state.transport = build_transport()
    removed = db.prune(application.state.conn)
    if any(removed.values()):
        logger.info("pruned %s", removed)
    yield


logger = logging.getLogger("wacal")

app = FastAPI(title="wacal", version="0.4.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(dashboard_router)

VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "dev-verify-token")
APP_SECRET = os.environ.get("WA_APP_SECRET", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

app.state.conn = None
app.state.transport = None


def conn():
    if getattr(app.state, "conn", None) is None:
        app.state.conn = db.connect()
    return app.state.conn


def transport():
    if getattr(app.state, "transport", None) is None:
        app.state.transport = build_transport()
    return app.state.transport


def require_verifiable(secret: str, transport_name: str) -> None:
    """Refuse a webhook whose signature cannot be checked at all.

    Both transports' routes are always mounted, so a Meta-only deployment
    still exposes /twilio/webhook. Returning True when that transport's secret
    is unset made it an unauthenticated command channel: anyone could POST
    From=<victim> Body="share <attacker>" and be believed. Fail closed, and
    make the local escape hatch explicit.
    """
    if secret or gcal.allow_unsigned():
        return
    raise HTTPException(
        status_code=403,
        detail=(f"{transport_name} webhook is disabled: its signing secret is "
                f"not configured. Set it, or set WACAL_ALLOW_UNSIGNED=1 for "
                f"local runs."),
    )


def valid_signature(raw: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 over the exact raw request body."""
    if not APP_SECRET:
        # Only reachable under WACAL_ALLOW_UNSIGNED; see require_verifiable.
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and hmac.compare_digest(params.get("hub.verify_token", ""),
                                    VERIFY_TOKEN)):
        return Response(content=params.get("hub.challenge", ""),
                        media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


async def _dispatch(messages, host_slug: str | None):
    """Run each inbound message and deliver every reply it produced.

    The handlers are synchronous and may make blocking Google Calendar calls,
    so they run in a worker thread. Running them inline froze the event loop
    for the whole call, including /health.
    """
    database, sender = conn(), transport()
    host = None
    if host_slug is not None:
        host = db.get_host(database, host_slug)

    scope = host_slug.lower() if host_slug else numbering.PEER_SCOPE

    for message in messages:
        queued = await run_in_threadpool(_handle_one, database, host,
                                         host_slug, message, scope)
        if queued:
            await drain_outbox(database, sender, only=queued)
    return {"status": "ok"}


def _handle_one(database, host, host_slug: str | None, message,
                scope: str) -> list[int]:
    """Decide and persist one message. Returns the outbox ids to send.

    Everything here is synchronous and runs in a worker thread, lock included.
    Taking db.LOCK on the event loop and then handing the handler to a thread
    deadlocks: the RLock is held by one thread and wanted by another.
    """
    with db.LOCK:
        if db.already_seen(database, message.message_id):
            return []
        # A typed number stands in for a tapped row; see wacal.numbering.
        message = numbering.resolve(database, message, scope)
        try:
            if host_slug is not None and host is None:
                outcome = _unknown_host()
            elif host is not None:
                outcome = handle_business(database, host, message)
            else:
                outcome = handle(database, message)
        except Exception:
            # Do not mark the message seen: the provider's retry is the only
            # chance to recover, and swallowing it loses the message.
            logger.exception("handler failed for %s", message.message_id)
            raise
        numbering.remember(database, message.from_phone, scope, outcome.reply)
        for phone, note in outcome.notify:
            numbering.remember(database, phone, scope, note)
        db.mark_seen(database, message.message_id)
        # Queue every outbound message before sending any, so a failed send is
        # retried rather than silently losing the owner's notification.
        queued = [db.queue_outbound(database, message.from_phone, outcome.reply)]
        queued += [db.queue_outbound(database, phone, note)
                   for phone, note in outcome.notify]
    return queued


async def drain_outbox(database, sender, *, only: list[int] | None = None,
                       limit: int = 50) -> int:
    """Send queued messages, marking each one sent only once it is accepted.

    A send that raises leaves the row pending with an attempt count, which is
    both the retry mechanism and the record of what failed overnight.
    """
    sent = 0
    for row in db.pending_outbound(database, limit=limit, only=only):
        try:
            await sender.send(row["phone"], db.reply_from_row(row))
        except Exception:  # noqa: BLE001 - retried, and logged for the morning
            db.record_outbound_failure(database, row["id"])
            logger.exception("send to %s failed (outbox %s)", row["phone"],
                             row["id"])
            continue
        db.mark_outbound_sent(database, row["id"])
        sent += 1
    return sent


async def _dispatch_cloud(raw: bytes, signature: str | None,
                          host_slug: str | None):
    require_verifiable(APP_SECRET, "Cloud API")
    if not valid_signature(raw, signature):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed json")
    return await _dispatch(parse_webhook(body), host_slug)


async def _dispatch_twilio(request: Request, signature: str | None,
                           host_slug: str | None):
    require_verifiable(TWILIO_AUTH_TOKEN, "Twilio")
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    if not valid_twilio_signature(signed_url(request), params, signature,
                                  TWILIO_AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="bad twilio signature")
    return await _dispatch(parse_twilio_form(params), host_slug)


def signed_url(request: Request) -> str:
    """The URL Twilio signed.

    Twilio signs the exact URL configured in its console, per webhook. Behind a
    proxy that terminates TLS, request.url is http:// internally, so
    TWILIO_PUBLIC_URL supplies the public scheme and host -- but only those.
    Substituting the whole URL would make every path except one fail.
    """
    base = os.environ.get("TWILIO_PUBLIC_URL", "").strip()
    if not base:
        return str(request.url)
    prefix = base.rstrip("/")
    # Accept either a bare origin or a full webhook URL in the variable.
    for suffix in ("/twilio/webhook", "/webhook"):
        if prefix.endswith(suffix):
            prefix = prefix[: -len(suffix)]
            break
    url = f"{prefix}{request.url.path}"
    return f"{url}?{request.url.query}" if request.url.query else url


def _unknown_host():
    from .commands import text

    return text("This booking link is not active.")


@app.get("/")
async def root():
    return {"service": "wacal", "version": app.version,
            "modes": ["peer", "business"],
            "webhooks": {"cloud_peer": "/webhook",
                         "cloud_business": "/webhook/{slug}",
                         "twilio_peer": "/twilio/webhook",
                         "twilio_business": "/twilio/webhook/{slug}"}}


@app.post("/webhook")
async def inbound(request: Request,
                  x_hub_signature_256: str | None = Header(default=None)):
    """Peer mode: two people sharing availability with each other."""
    return await _dispatch_cloud(await request.body(), x_hub_signature_256, None)


@app.post("/webhook/{host_slug}")
async def inbound_business(host_slug: str, request: Request,
                           x_hub_signature_256: str | None = Header(default=None)):
    """Business mode: customers booking one host's public service menu."""
    return await _dispatch_cloud(await request.body(), x_hub_signature_256,
                                 host_slug)


@app.post("/twilio/webhook")
async def twilio_inbound(request: Request,
                         x_twilio_signature: str | None = Header(default=None)):
    """Peer mode over Twilio. Replies are numbered text, not tappable rows."""
    return await _dispatch_twilio(request, x_twilio_signature, None)


@app.post("/twilio/webhook/{host_slug}")
async def twilio_inbound_business(
        host_slug: str, request: Request,
        x_twilio_signature: str | None = Header(default=None)):
    """Business mode over Twilio."""
    return await _dispatch_twilio(request, x_twilio_signature, host_slug)


@app.get("/webhook/{host_slug}/status")
async def host_status(host_slug: str):
    """Unauthenticated liveness check for one booking page.

    Deliberately thin: the business name, timezone and calendar-connection
    state are the owner's business, and this route has no key. The full view
    lives behind GET /dashboard/{slug}.
    """
    if db.get_host(conn(), host_slug) is None:
        raise HTTPException(status_code=404, detail="unknown slug")
    return {"slug": host_slug.lower(), "active": True}

