"""Let a user answer a list by typing its number.

Twilio's WhatsApp API has no interactive list message: a tap cannot come back
because there is nothing to tap. The rows still have to be reachable, so every
reply that carries rows is remembered against the sender's number, and a bare
"3" in the next message is translated into that row's id.

Meta Cloud API keeps its real buttons; this path simply also works there, which
is why it lives in one place rather than inside a transport.
"""

from __future__ import annotations

import json

from . import db, keywords
from .whatsapp import Inbound, Reply

SESSION_KIND = "rows"
PEER_SCOPE = "peer"
TTL_SECONDS = 3600


def remember(conn, phone: str, scope: str, reply: Reply) -> None:
    """Store the row ids most recently offered to this number in this scope.

    The scope is the host slug, or PEER_SCOPE. Without it one customer with
    menus open at two businesses shares a single list, and "1" resolves
    against whichever menu happened to arrive last -- booking the wrong
    service at the wrong venue.
    """
    if not reply.rows:
        return
    db.put_session(conn, phone, scope, SESSION_KIND,
                   json.dumps([row.id for row in reply.rows]),
                   ttl_seconds=TTL_SECONDS)


def resolve(conn, msg: Inbound, scope: str = PEER_SCOPE) -> Inbound:
    """Turn a bare number into the row id it stood for.

    Anything else is returned unchanged. An out-of-range number is left alone
    too, so the command layer can answer it in context rather than silently
    doing nothing.
    """
    text = msg.text.strip()
    intent = keywords.canonical(text)
    # isdigit() is True for "²" and other non-ASCII digits, which int() then
    # rejects. Require ASCII so a stray character cannot raise mid-dispatch.
    numeric = text.isascii() and text.isdigit()
    if msg.is_tap or not (numeric or intent in ("yes", "no")):
        return msg

    row = db.pop_session(conn, msg.from_phone, scope)
    if row is None or row["kind"] != SESSION_KIND:
        return msg
    try:
        ids = json.loads(row["payload"])
    except json.JSONDecodeError:
        return msg

    def keep() -> Inbound:
        # Put the list back: the rows are still valid, the answer was not.
        db.put_session(conn, msg.from_phone, scope, SESSION_KIND,
                       row["payload"], ttl_seconds=TTL_SECONDS)
        return msg

    if intent in ("yes", "no"):
        # A confirmation screen offers exactly "Confirm" then "Start over".
        prefix = "b:ok:" if intent == "yes" else "b:restart"
        chosen = next((i for i in ids if i.startswith(prefix)), None)
        if chosen is None:
            return keep()
    else:
        index = int(text) - 1
        if not (0 <= index < len(ids)):
            return keep()
        chosen = ids[index]

    return Inbound(
        from_phone=msg.from_phone,
        text=chosen,
        message_id=msg.message_id,
        profile_name=msg.profile_name,
        is_tap=True,
    )
