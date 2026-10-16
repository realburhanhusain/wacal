"""One-time codes for a phone number nobody has proved.

An inbound WhatsApp message already proves its sender: Meta signs the webhook
and the number is observed, not claimed. So the conversation needs no OTP, and
adding one there would be security theatre that costs every user a round trip.

What is unproved is a number somebody *types*: POST /dashboard/register names
an owner_phone, creates a user row for it and then sends that number other
people's booking details. A typo there leaks a stranger's calendar to a
stranger. This module is the proof for that one case, switched on with
WACAL_REQUIRE_OTP=1.

The code is never stored. Only a keyed digest is, so a readable database file
does not hand over a live code -- and the digest is bound to the phone, so a
row copied onto another number verifies nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db, templates
from .whatsapp import Reply

CODE_DIGITS = 6
TTL_MINUTES = 10
MAX_ATTEMPTS = 5
# Long enough that a resend cannot be used to spray codes at a number, short
# enough that a genuine "it never arrived" is not a five-minute wait.
RESEND_SECONDS = 60


class TooSoon(RuntimeError):
    """A resend was asked for before the previous code had a chance."""


@dataclass
class Verdict:
    ok: bool
    reason: str = ""


def pepper() -> str:
    """Key for the digest.

    WACAL_TOKEN_KEY already exists and is already required in any deployment
    storing calendar tokens, so reuse it rather than inventing a second key
    nobody sets. Without it the digest is a plain hash of a six-digit number,
    which is trivially reversed -- acceptable only because the code expires in
    ten minutes and dies after five wrong guesses, and that is exactly what
    the unset case is: a local run.
    """
    return os.environ.get("WACAL_OTP_PEPPER", "").strip() \
        or os.environ.get("WACAL_TOKEN_KEY", "").strip()


def generate_code() -> str:
    """A uniformly random code, zero padded. randbelow, never randint."""
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def hash_code(phone: str, code: str) -> str:
    return hmac.new(pepper().encode(), f"{phone}:{code}".encode(),
                    hashlib.sha256).hexdigest()


def otp_reply(code: str) -> Reply:
    """The message carrying a code.

    Outside the 24-hour window -- the normal case for a number that has never
    messaged the app -- only the authentication template is delivered, so the
    code is passed as both the body variable and the button's copy-code value.
    The text is the same thing in free-form, used inside the window and by the
    in-memory transport.
    """
    return Reply(
        text=(f"{code} is your verification code. It expires in "
              f"{TTL_MINUTES} minutes. Do not share it."),
        template=templates.build(templates.OTP, code,
                                 button_params=(code,)),
    )


def start(conn, phone: str, *, now: datetime | None = None) -> tuple[str, Reply]:
    """Issue a code for this number. Returns (code, reply to send).

    The caller queues the reply; this module does not send, so an OTP goes
    through the same outbox, window check and retry as every other message.
    """
    now = now or db.now_utc()
    existing = db.get_otp(conn, phone)
    if existing is not None:
        issued = datetime.fromisoformat(existing["created_at"])
        waited = (now - issued).total_seconds()
        if waited < RESEND_SECONDS:
            raise TooSoon(f"a code was already sent; try again in "
                          f"{int(RESEND_SECONDS - waited)}s")
    code = generate_code()
    db.put_otp(conn, phone, hash_code(phone, code),
               now + timedelta(minutes=TTL_MINUTES))
    return code, otp_reply(code)


def check(conn, phone: str, code: str, *,
          now: datetime | None = None) -> Verdict:
    """Verify a code, consuming the challenge on success.

    A wrong code counts an attempt and the challenge dies at MAX_ATTEMPTS;
    without that, six digits is a few seconds of guessing.
    """
    now = now or db.now_utc()
    row = db.get_otp(conn, phone)
    if row is None:
        return Verdict(False, "no code has been requested for this number")
    if datetime.fromisoformat(row["expires_at"]) <= now:
        db.clear_otp(conn, phone)
        return Verdict(False, "that code has expired; request a new one")
    if row["attempts"] >= MAX_ATTEMPTS:
        db.clear_otp(conn, phone)
        return Verdict(False, "too many wrong attempts; request a new code")
    supplied = "".join(ch for ch in (code or "") if ch.isdigit())
    if not hmac.compare_digest(row["code_hash"], hash_code(phone, supplied)):
        attempts = db.bump_otp_attempts(conn, phone)
        left = max(0, MAX_ATTEMPTS - attempts)
        if left == 0:
            db.clear_otp(conn, phone)
            return Verdict(False, "too many wrong attempts; request a new code")
        return Verdict(False, f"wrong code; {left} attempt(s) left")
    db.mark_phone_verified(conn, phone)
    return Verdict(True)
