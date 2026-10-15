"""WhatsApp Cloud API transport, plus an in-memory transport for local runs.

Reply objects are provider-neutral so the command layer can be tested without
a Meta account. Only this module knows the Cloud API payload shape.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field

import httpx

GRAPH_VERSION = os.environ.get("WA_GRAPH_VERSION", "v21.0")

# Cloud API hard limits on an interactive list message.
MAX_ROWS = 10
ROW_TITLE = 24
ROW_DESCRIPTION = 72
BUTTON_TEXT = 20
BODY_TEXT = 1024


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Row:
    id: str
    title: str
    description: str = ""


@dataclass
class Reply:
    """What to send back. Either plain text, or text plus a tappable list."""

    text: str
    rows: list[Row] = field(default_factory=list)
    button: str = "Pick a slot"
    header: str = ""
    # Cloud API shows this above the rows. It was hard-coded to "Slots", which
    # then appeared over the service and date menus too.
    section: str = ""


TWILIO_BODY = 1600


def normalise_phone(value: str) -> str:
    """One canonical phone shape: "+" followed by digits.

    The providers disagree. Meta's wa_id is bare digits ("966500000001"),
    Twilio sends "whatsapp:+966500000001", and an admin registering a host
    types whatever they like. Without this, the same person is two user rows
    depending on which transport they came in on.
    """
    digits = "".join(character for character in value if character.isdigit())
    return f"+{digits}" if digits else ""


def render_numbered(reply: Reply) -> str:
    """Flatten a reply with rows into numbered plain text.

    Used by transports with no interactive list message. The numbers line up
    with wacal.numbering, which maps a typed "3" back to the third row id.
    """
    if not reply.rows:
        return reply.text
    lines = [reply.text, ""]
    for index, row in enumerate(reply.rows[:MAX_ROWS], start=1):
        suffix = f" - {row.description}" if row.description else ""
        lines.append(f"{index}. {row.title}{suffix}")
    lines.append("")
    lines.append("Reply with a number.")
    return "\n".join(lines)


class Transport:
    async def send(self, to: str, reply: Reply) -> dict:  # pragma: no cover
        raise NotImplementedError


class MemoryTransport(Transport):
    """Collects messages instead of sending them. Used by tests and sim.py."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Reply]] = []

    async def send(self, to: str, reply: Reply) -> dict:
        self.sent.append((to, reply))
        return {"messages": [{"id": f"local.{len(self.sent)}"}]}


class CloudTransport(Transport):
    def __init__(self, phone_number_id: str, token: str) -> None:
        self.url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"
        self.token = token

    def payload(self, to: str, reply: Reply) -> dict:
        base = {"messaging_product": "whatsapp", "recipient_type": "individual",
                "to": to}
        if not reply.rows:
            return {**base, "type": "text",
                    "text": {"preview_url": False,
                             "body": clip(reply.text, BODY_TEXT)}}
        rows = [
            {"id": r.id, "title": clip(r.title, ROW_TITLE),
             **({"description": clip(r.description, ROW_DESCRIPTION)}
                if r.description else {})}
            for r in reply.rows[:MAX_ROWS]
        ]
        interactive = {
            "type": "list",
            "body": {"text": clip(reply.text, BODY_TEXT)},
            "action": {"button": clip(reply.button, BUTTON_TEXT),
                       "sections": [{
                           "title": clip(reply.section or reply.button, 24),
                           "rows": rows,
                       }]},
        }
        if reply.header:
            interactive["header"] = {"type": "text",
                                     "text": clip(reply.header, 60)}
        return {**base, "type": "interactive", "interactive": interactive}

    async def send(self, to: str, reply: Reply) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                self.url,
                json=self.payload(to, reply),
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            return response.json()


class TwilioTransport(Transport):
    """Twilio's WhatsApp channel. Plain text only -- no interactive lists.

    Rows are rendered as a numbered menu, and wacal.numbering turns the user's
    typed number back into the row id the command layer expects.
    """

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self.url = (f"https://api.twilio.com/2010-04-01/Accounts/"
                    f"{account_sid}/Messages.json")
        self.auth = (account_sid, auth_token)
        self.from_number = f"whatsapp:{normalise_phone(from_number)}"

    def payload(self, to: str, reply: Reply) -> dict:
        destination = f"whatsapp:{normalise_phone(to)}"
        return {
            "From": self.from_number,
            "To": destination,
            "Body": clip(render_numbered(reply), TWILIO_BODY),
        }

    async def send(self, to: str, reply: Reply) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self.url, data=self.payload(to, reply),
                                         auth=self.auth)
            response.raise_for_status()
            return response.json()


def build_transport() -> Transport:
    """Pick a transport from the environment.

    WA_TRANSPORT forces a choice. Otherwise whichever provider has complete
    credentials wins, and with neither the in-memory transport keeps the app
    runnable for local work.
    """
    choice = os.environ.get("WA_TRANSPORT", "").strip().lower()
    phone_number_id = os.environ.get("WA_PHONE_NUMBER_ID")
    token = os.environ.get("WA_TOKEN")
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "")

    cloud_ready = bool(phone_number_id and token)
    twilio_ready = bool(account_sid and auth_token and from_number)

    if choice == "cloud":
        if not cloud_ready:
            raise RuntimeError("WA_TRANSPORT=cloud needs WA_PHONE_NUMBER_ID "
                               "and WA_TOKEN")
        return CloudTransport(phone_number_id, token)
    if choice == "twilio":
        if not twilio_ready:
            raise RuntimeError("WA_TRANSPORT=twilio needs TWILIO_ACCOUNT_SID, "
                               "TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM")
        return TwilioTransport(account_sid, auth_token, from_number)
    if choice == "memory":
        return MemoryTransport()
    if choice:
        raise RuntimeError(f"unknown WA_TRANSPORT {choice!r}")

    if cloud_ready:
        return CloudTransport(phone_number_id, token)
    if twilio_ready:
        return TwilioTransport(account_sid, auth_token, from_number)
    return MemoryTransport()


@dataclass
class Inbound:
    """One normalised incoming message."""

    from_phone: str
    text: str
    message_id: str
    profile_name: str = ""
    is_tap: bool = False


def twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """Twilio's X-Twilio-Signature: HMAC-SHA1 over the URL plus sorted params.

    Each key and its value are concatenated in key order with no separators,
    appended to the full request URL, then signed and base64 encoded.
    """
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_twilio_signature(url: str, params: dict[str, str],
                           signature: str | None, auth_token: str) -> bool:
    if not auth_token:
        # No token configured means no verification is possible. The caller
        # decides whether that is acceptable; it is not acceptable in production.
        return True
    if not signature:
        return False
    return hmac.compare_digest(
        twilio_signature(url, params, auth_token), signature
    )


def parse_twilio_form(params: dict[str, str]) -> list[Inbound]:
    """One inbound message from a Twilio form post.

    Twilio delivers a single message per request, and its only reply type is
    text -- there are no button payloads to unpack.
    """
    sender = normalise_phone(params.get("From") or "")
    if not sender:
        return []
    return [Inbound(
        from_phone=sender,
        text=(params.get("Body") or "").strip(),
        message_id=params.get("MessageSid") or params.get("SmsMessageSid", ""),
        profile_name=(params.get("ProfileName") or "").strip(),
        is_tap=False,
    )]


def parse_webhook(body: dict) -> list[Inbound]:
    """Pull user messages out of a Cloud API webhook body.

    Status callbacks (delivered/read) carry no 'messages' key and yield nothing.
    """
    out: list[Inbound] = []
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = {
                normalise_phone(c.get("wa_id") or ""):
                    (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts", []) or []
            }
            for message in value.get("messages", []) or []:
                sender = normalise_phone(message.get("from", ""))
                kind = message.get("type")
                text = ""
                is_tap = False
                if kind == "text":
                    text = (message.get("text") or {}).get("body", "")
                elif kind == "interactive":
                    inter = message.get("interactive") or {}
                    picked = inter.get("list_reply") or inter.get("button_reply") or {}
                    text = picked.get("id", "")
                    is_tap = True
                elif kind == "button":
                    text = (message.get("button") or {}).get("payload", "")
                    is_tap = True
                else:
                    text = ""
                out.append(
                    Inbound(
                        from_phone=sender,
                        text=text.strip(),
                        message_id=message.get("id", ""),
                        profile_name=names.get(sender, ""),
                        is_tap=is_tap,
                    )
                )
    return out
