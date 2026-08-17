"""One-time device-pairing credentials with domain-separated keyed hashes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class PairingError(ValueError):
    """Raised when pairing inputs fail closed."""


@dataclass(frozen=True)
class PairingChallenge:
    device_code: str
    user_code: str
    expires_at: datetime
    poll_interval_seconds: int = 5


def create_pairing_challenge(
    signing_key: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> PairingChallenge:
    _key_bytes(signing_key)
    if not 60 <= int(ttl_seconds) <= 900:
        raise PairingError("pairing lifetime must be between 60 and 900 seconds")
    issued_at = now or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        raise PairingError("pairing time must be timezone-aware")
    raw_user_code = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return PairingChallenge(
        device_code=f"gpa_pair_{secrets.token_urlsafe(32)}",
        user_code=f"{raw_user_code[:4]}-{raw_user_code[4:]}",
        expires_at=issued_at + timedelta(seconds=int(ttl_seconds)),
    )


def normalize_user_code(value: str) -> str:
    normalized = "".join(character for character in str(value).upper() if character not in " -")
    if len(normalized) != 8 or any(character not in _USER_CODE_ALPHABET for character in normalized):
        raise PairingError("pairing code must contain eight valid characters")
    return normalized


def hash_pairing_secret(value: str, signing_key: str, *, purpose: str) -> bytes:
    key = _key_bytes(signing_key)
    if purpose not in {"device", "user"}:
        raise PairingError("unknown pairing secret purpose")
    if purpose == "device":
        normalized = str(value).strip()
        if not normalized.startswith("gpa_pair_") or len(normalized) < 40:
            raise PairingError("invalid device pairing credential")
    else:
        normalized = normalize_user_code(value)
    message = f"gpa-pairing-v1:{purpose}:{normalized}".encode()
    return hmac.new(key, message, hashlib.sha256).digest()


def pairing_secret_matches(
    value: str,
    expected_hash: bytes,
    signing_key: str,
    *,
    purpose: str,
) -> bool:
    try:
        candidate = hash_pairing_secret(value, signing_key, purpose=purpose)
    except PairingError:
        return False
    return hmac.compare_digest(candidate, expected_hash)


def _key_bytes(signing_key: str) -> bytes:
    key = str(signing_key or "").encode()
    if len(key) < 32:
        raise PairingError("pairing signing key must be at least 32 bytes")
    return key


__all__ = [
    "PairingChallenge",
    "PairingError",
    "create_pairing_challenge",
    "hash_pairing_secret",
    "normalize_user_code",
    "pairing_secret_matches",
]
