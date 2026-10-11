"""Password hashing and signed, expiring bearer tokens (standard library only)."""

import base64
import hashlib
import hmac
import json
import os
import time

ITERATIONS = 100_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sign(payload: bytes, secret: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def create_token(subject: str, secret: str, *, ttl: int = 3600, now: float | None = None) -> str:
    now = time.time() if now is None else now
    body = json.dumps({"sub": subject, "exp": int(now + ttl)}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded}.{_sign(encoded.encode(), secret)}"


def decode_token(token: str, secret: str, *, now: float | None = None) -> str | None:
    """Return the subject for a valid, unexpired token; otherwise None."""
    try:
        encoded, signature = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(encoded.encode(), secret), signature):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    now = time.time() if now is None else now
    if not isinstance(claims, dict) or claims.get("exp", 0) < now:
        return None
    return claims.get("sub")
