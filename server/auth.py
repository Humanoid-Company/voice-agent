"""Entry to the site: one password (the ACCESS_PASSWORD setting of the server, never in the code). A right password is
exchanged for a signed token that the page sends with every request, so the password itself is typed once and not kept.
A token is `expiry.signature`, signed with a key made of the password: nothing is stored on the server, and changing the
password signs everybody out."""
from __future__ import annotations

import hashlib
import hmac
import time

TOKEN_DAYS = 30


def _sign(secret: str, expiry: str) -> str:
    return hmac.new(secret.encode(), b"diff-voice-agent:" + expiry.encode(), hashlib.sha256).hexdigest()


def password_ok(secret: str, given: object) -> bool:
    """Constant-time; never true when no password is set."""
    return bool(secret) and isinstance(given, str) and hmac.compare_digest(given.encode(), secret.encode())


def make_token(secret: str, *, days: int = TOKEN_DAYS, now: float | None = None) -> str:
    expiry = str(int((time.time() if now is None else now) + days * 86400))
    return f"{expiry}.{_sign(secret, expiry)}"


def valid_token(secret: str, token: object, *, now: float | None = None) -> bool:
    if not secret or not isinstance(token, str) or "." not in token:
        return False
    expiry, signature = token.split(".", 1)
    if not expiry.isdigit() or int(expiry) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(signature.encode(), _sign(secret, expiry).encode())
