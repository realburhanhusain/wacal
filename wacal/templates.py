"""Template messages -- the only thing WhatsApp delivers outside 24 hours.

Meta opens a 24-hour customer service window when a user sends an inbound
message, and closes it 24 hours after the last one. Inside the window any
free-form message is delivered. Outside it, only a template that Meta approved
in advance goes through; a free-form send is rejected (error 131047) and the
message is simply lost. Up to now every wacal notification was free-form, so an
owner's "new booking" note silently failed whenever the owner had not texted
the app that day.

A notification is declared here as a *kind*. The approved template name lives
in the Meta account rather than in this repo, so each kind reads its name from
the environment:

    WA_TEMPLATE_BOOKING_CONFIRMED=wacal_booking_confirmed
    WA_TEMPLATE_BOOKING_CANCELLED=wacal_booking_cancelled
    WA_TEMPLATE_OTP=wacal_otp
    WA_TEMPLATE_LANGUAGE=en          # whole-account default, e.g. en or en_US

A kind with no name configured has no template. That is not an error while the
recipient is inside their window; it is a refusal to send once they are
outside, because pretending to send is how a confirmed booking goes unnoticed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

BOOKING_CONFIRMED = "booking_confirmed"
BOOKING_CANCELLED = "booking_cancelled"
BOOKING_REMINDER = "booking_reminder"
OTP = "otp"

KINDS = (BOOKING_CONFIRMED, BOOKING_CANCELLED, BOOKING_REMINDER, OTP)

# Meta's own cap on a template body variable.
PARAM_TEXT = 1024

DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class Template:
    """One approved template, already bound to this message's variables.

    name is what Meta approved. On Twilio's WhatsApp channel the equivalent is
    a Content template, whose "name" is its ContentSid (HX...); the transport
    tells the two apart, nothing above it needs to.

    body_params fill {{1}}, {{2}}, ... in the template body, in order. A
    mismatch against the approved body is rejected by Meta at send time, which
    is the one failure this module cannot catch locally.

    button_params exist for authentication templates, whose one-time-code
    button carries the code a second time.
    """

    kind: str
    name: str
    language: str = DEFAULT_LANGUAGE
    body_params: tuple[str, ...] = ()
    button_params: tuple[str, ...] = ()
    button_sub_type: str = "url"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "language": self.language,
            "body_params": list(self.body_params),
            "button_params": list(self.button_params),
            "button_sub_type": self.button_sub_type,
        }

    @staticmethod
    def from_dict(data: dict | None) -> "Template | None":
        if not data:
            return None
        return Template(
            kind=data.get("kind", ""),
            name=data["name"],
            language=data.get("language", DEFAULT_LANGUAGE),
            body_params=tuple(data.get("body_params", ())),
            button_params=tuple(data.get("button_params", ())),
            button_sub_type=data.get("button_sub_type", "url"),
        )


def env_key(kind: str) -> str:
    return f"WA_TEMPLATE_{kind.upper()}"


def name_for(kind: str) -> str:
    return os.environ.get(env_key(kind), "").strip()


def language() -> str:
    return os.environ.get("WA_TEMPLATE_LANGUAGE", DEFAULT_LANGUAGE).strip() \
        or DEFAULT_LANGUAGE


def clip_param(value: object) -> str:
    """One variable, flattened the way Meta requires.

    A template variable may not contain a newline or a tab, and may not be
    empty: both are rejected at send time with a parameter error rather than a
    helpful one. Collapsing whitespace here keeps the same booking text usable
    as free-form inside the window and as a variable outside it.
    """
    text = " ".join(str(value).split())
    if not text:
        text = "-"
    return text if len(text) <= PARAM_TEXT else text[: PARAM_TEXT - 1] + "…"


def build(kind: str, *body_params: object,
          button_params: tuple[object, ...] = ()) -> Template:
    """The template for this kind, with its variables already bound.

    A kind whose name is not configured still returns a Template, with an
    empty name. `name` is the test for "can actually be sent"; carrying the
    kind regardless is what lets a refusal say which variable to set rather
    than printing WA_TEMPLATE_<KIND> at an operator.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown template kind {kind!r} "
                         f"(have: {', '.join(KINDS)})")
    name = name_for(kind)
    return Template(
        kind=kind,
        name=name,
        language=language(),
        body_params=tuple(clip_param(p) for p in body_params),
        button_params=tuple(clip_param(p) for p in button_params),
        button_sub_type=os.environ.get("WA_TEMPLATE_BUTTON_SUB_TYPE",
                                       "url").strip() or "url",
    )


def allow_freeform_outside_window() -> bool:
    """Let a free-form message be attempted outside the 24-hour window.

    For local runs and for the in-memory transport, where there is no Meta
    account to reject it. In production this turns a visible refusal into an
    invisible provider-side drop.
    """
    return os.environ.get("WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW", "").lower() \
        in ("1", "true", "yes")


def require_otp() -> bool:
    """Whether an asserted phone number must prove itself before use.

    An inbound WhatsApp message already proves its sender, so this is not about
    the conversation. It guards the one place a number is *claimed* rather than
    observed: POST /dashboard/register, which names an owner_phone that nobody
    has verified and then messages it with bookings.
    """
    return os.environ.get("WACAL_REQUIRE_OTP", "").lower() in (
        "1", "true", "yes")


def configured() -> dict[str, str]:
    """Which kinds have a template name, for /dashboard and start.py."""
    return {kind: name_for(kind) for kind in KINDS}


@dataclass
class Status:
    """Why a message can or cannot be delivered right now."""

    deliverable: bool
    force_template: bool = False
    reason: str = ""
    missing: list[str] = field(default_factory=list)


def delivery_status(*, inside_window: bool, template: Template | None,
                    kind: str = "") -> Status:
    """Decide how -- or whether -- one queued message may be sent.

    Inside the window anything goes. Outside it a template is mandatory, and
    with none configured the honest answer is that this message cannot be
    delivered, not that it was sent.
    """
    if inside_window:
        return Status(True, force_template=False)
    if template is not None and template.name:
        return Status(True, force_template=True)
    if template is not None and not kind:
        kind = template.kind
    if allow_freeform_outside_window():
        return Status(True, force_template=False,
                      reason="outside the 24-hour window; sending free-form "
                             "because WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW is "
                             "set")
    key = env_key(kind) if kind in KINDS else "WA_TEMPLATE_<KIND>"
    return Status(
        False,
        reason=("outside the 24-hour customer service window and no approved "
                f"template is configured for this message. Set {key} to an "
                "approved template name, or set "
                "WACAL_ALLOW_FREEFORM_OUTSIDE_WINDOW=1 for local runs."),
        missing=[key],
    )
