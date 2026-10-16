"""Encryption for secrets held at rest.

Only one thing needs it today: the Google refresh token in calendar_links. A
refresh token is a standing grant to read and write someone's calendar, and it
was stored as plain JSON, so the database file was the whole secret.

    python -m wacal.crypto        # print a key for WACAL_TOKEN_KEY

Values are stored as "enc:v1:<fernet token>". Anything without that prefix is
read back as-is, so a database written before encryption existed keeps working
and re-encrypts itself the next time each token is saved.
"""

from __future__ import annotations

import os

PREFIX = "enc:v1:"


class NotConfigured(RuntimeError):
    """No WACAL_TOKEN_KEY, and plaintext was not explicitly allowed."""


def allow_plaintext() -> bool:
    return os.environ.get("WACAL_ALLOW_PLAINTEXT_TOKENS", "").lower() in (
        "1", "true", "yes")


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _fernet():
    key = os.environ.get("WACAL_TOKEN_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet

    try:
        return Fernet(key.encode())
    except Exception as exc:  # noqa: BLE001 - a bad key must not be guessed at
        raise NotConfigured(
            "WACAL_TOKEN_KEY is not a valid Fernet key. Generate one with "
            "`python -m wacal.crypto`."
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage, or refuse to store it in the clear."""
    box = _fernet()
    if box is None:
        if allow_plaintext():
            return plaintext
        raise NotConfigured(
            "Refusing to store a calendar token in the clear. Set "
            "WACAL_TOKEN_KEY (see `python -m wacal.crypto`), or set "
            "WACAL_ALLOW_PLAINTEXT_TOKENS=1 for local work."
        )
    return PREFIX + box.encrypt(plaintext.encode()).decode()


def decrypt(stored: str) -> str:
    """Read a stored secret, whether or not it was encrypted."""
    if not stored or not stored.startswith(PREFIX):
        # Written before encryption existed. Returned as-is so the account
        # keeps working; it is re-encrypted on the next save.
        return stored
    box = _fernet()
    if box is None:
        raise NotConfigured(
            "This database holds encrypted calendar tokens but "
            "WACAL_TOKEN_KEY is not set, so they cannot be read."
        )
    from cryptography.fernet import InvalidToken

    try:
        return box.decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise NotConfigured(
            "A stored calendar token could not be decrypted with the current "
            "WACAL_TOKEN_KEY. The key has changed; affected users must send "
            "`connect google` again."
        ) from exc


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(PREFIX)


if __name__ == "__main__":  # pragma: no cover - operator helper
    print(generate_key())
