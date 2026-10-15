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
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response

from . import db, numbering
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
    yield


app = FastAPI(title="wacal", version="0.3.0", lifespan=lifespan)
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


def valid_signature(raw: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 over the exact raw request body.

    With no app secret configured this returns True, which is acceptable only
    for local runs. Set WA_APP_SECRET before pointing a real number at this.
    """
    if not APP_SECRET:
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
            and params.get("hub.verify_token") == VERIFY_TOKEN):
        return Response(content=params.get("hub.challenge", ""),
                        media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


async def _dispatch(messages, host_slug: str | None):
    """Run each inbound message and deliver every reply it produced."""
    database, sender = conn(), transport()
    host = None
    if host_slug is not None:
        host = db.get_host(database, host_slug)

    # Callers answer 200 regardless: a non-200 makes the provider redeliver.
    for message in messages:
        # The whole decide-then-write sequence is serialised: two taps on the
        # same slot must not both read it as free before either one inserts.
        with db.LOCK:
            if not db.mark_seen(database, message.message_id):
                continue
            # A typed number stands in for a tapped row; see wacal.numbering.
            message = numbering.resolve(database, message)
            if host_slug is not None and host is None:
                outcome = _unknown_host()
            elif host is not None:
                outcome = handle_business(database, host, message)
            else:
                outcome = handle(database, message)
            numbering.remember(database, message.from_phone, outcome.reply)
            for phone, note in outcome.notify:
                numbering.remember(database, phone, note)
        await sender.send(message.from_phone, outcome.reply)
        for phone, note in outcome.notify:
            await sender.send(phone, note)
    return {"status": "ok"}


async def _dispatch_cloud(raw: bytes, signature: str | None,
                          host_slug: str | None):
    if not valid_signature(raw, signature):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed json")
    return await _dispatch(parse_webhook(body), host_slug)


async def _dispatch_twilio(request: Request, signature: str | None,
                           host_slug: str | None):
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    # Twilio signs the exact URL it was configured with, including the scheme.
    # Behind a proxy that terminates TLS, set TWILIO_PUBLIC_URL to that value.
    url = os.environ.get("TWILIO_PUBLIC_URL") or str(request.url)
    if not valid_twilio_signature(url, params, signature, TWILIO_AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="bad twilio signature")
    return await _dispatch(parse_twilio_form(params), host_slug)


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
    host = db.get_host(conn(), host_slug)
    if host is None:
        raise HTTPException(status_code=404, detail="unknown slug")
    owner = db.get_user(conn(), host["owner_id"])
    return {
        "slug": host["slug"],
        "business_name": host["business_name"],
        "services": len(json.loads(host["services"])),
        "google_calendar_connected":
            db.get_calendar_link(conn(), host["owner_id"]) is not None,
        "timezone": owner["timezone"] if owner else None,
    }
