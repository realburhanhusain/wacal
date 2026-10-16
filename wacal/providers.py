"""External calendar providers, looked up by name.

`calendar_links.provider` has always been written and never read: every call
site reached straight for the Google implementation, so a row saying "outlook"
would have been handed to Google silently. One registry, consulted per link,
and an unknown provider is an error rather than a wrong answer.

A second provider needs four methods -- busy, create_event, list_events,
delete_event -- registered here, plus its own OAuth route and a connect verb.
Nothing else in the app knows which provider a user has.
"""

from __future__ import annotations

GOOGLE = "google"


class UnknownProvider(RuntimeError):
    """A calendar_links row names a provider this build cannot talk to."""


def _registry() -> dict[str, object]:
    from . import gcal

    # gcal.PROVIDER is the test seam, so resolve it on every lookup rather
    # than capturing it at import time.
    return {GOOGLE: gcal.PROVIDER}


def available() -> list[str]:
    return sorted(_registry())


def for_name(name: str | None):
    """The provider object for a stored provider name."""
    key = (name or GOOGLE).strip().lower()
    registry = _registry()
    if key not in registry:
        raise UnknownProvider(
            f"calendar provider {key!r} is not supported by this build "
            f"(have: {', '.join(sorted(registry))}). The affected user must "
            f"reconnect a supported calendar."
        )
    return registry[key]


def for_link(link):
    """The provider object for a calendar_links row."""
    keys = link.keys() if hasattr(link, "keys") else link
    name = link["provider"] if "provider" in keys else GOOGLE
    return for_name(name)
