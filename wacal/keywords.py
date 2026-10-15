"""Phrase aliases, so the bot answers more than one wording and one language.

Keys are matched against the whole trimmed, lower-cased message. Hindi has no
case, and these are fixed phrases rather than free text, so a lookup is enough
-- there is no translation step anywhere in this app.
"""

from __future__ import annotations

ALIASES: dict[str, str] = {
    # greetings / menu
    "hi": "help", "hello": "help", "hey": "help", "start": "help",
    "menu": "help", "?": "help",
    "नमस्ते": "help", "नमस्कार": "help", "हेलो": "help", "हाय": "help",
    "مرحبا": "help", "السلام عليكم": "help",

    # start a booking
    "book": "book", "booking": "book",
    "बुक": "book", "बुक करें": "book", "अपॉइंटमेंट": "book",
    "احجز": "book",

    # list my bookings
    "my": "my", "mine": "my", "bookings": "my", "my bookings": "my",
    "appointments": "my", "my appointments": "my",
    "मेरी बुकिंग": "my", "मेरी बुकिंग्स": "my", "मेरे अपॉइंटमेंट": "my",
    "حجوزاتي": "my",

    # cancel
    "cancel": "cancel", "cancel booking": "cancel",
    "रद्द": "cancel", "रद्द करें": "cancel", "कैंसिल": "cancel",
    "إلغاء": "cancel",

    # reschedule
    "reschedule": "reschedule", "change": "reschedule",
    "फिर से बुक करें": "reschedule", "समय बदलें": "reschedule",
    "تغيير": "reschedule",

    # confirm / decline
    "yes": "yes", "y": "yes", "confirm": "yes", "ok": "yes", "okay": "yes",
    "हाँ": "yes", "हां": "yes", "जी": "yes", "जी हाँ": "yes",
    "نعم": "yes",
    "no": "no", "n": "no", "nope": "no",
    "नहीं": "no", "ना": "no",
    "لا": "no",

    # availability
    "free": "free", "slots": "free", "availability": "free",
    "खाली": "free", "उपलब्ध": "free",
}


def canonical(text: str) -> str | None:
    """The canonical command for a whole message, or None if it is not an alias."""
    return ALIASES.get(" ".join(text.lower().split()))


def canonical_verb(verb: str) -> str:
    """Map a single leading word. Unknown words are returned unchanged."""
    return ALIASES.get(verb.lower(), verb.lower())
