"""Security primitives: credential hashing, payload integrity and log redaction.

Three responsibilities live here, all of them deliberately small and dependency-free
so they can be unit tested exhaustively:

1. **API key handling** -- keys are stored as SHA-256 digests, never in plaintext.
2. **Payload integrity** -- :func:`hash_payload` produces a canonical, stable hash of
   tool arguments so an approved action cannot be swapped for a different one
   between approval time and execution time.
3. **Redaction** -- :func:`redact` scrubs anything credential-shaped before it can
   reach a log line, an audit row or an API response.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Final

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
REDACTED: Final = "[REDACTED]"

#: Substrings that mark a mapping key as sensitive. Matched case-insensitively
#: against the *whole* key, so ``llm_api_key`` and ``X-API-Key`` both match.
SENSITIVE_KEY_PARTS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "auth",
        "credential",
        "private_key",
        "access_key",
        "session_id",
        "cookie",
        "signature",
    }
)

#: Value-level patterns for credentials that appear inside free text (e.g. an LLM
#: echoing a key back inside an error message).
_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),  # OpenAI-style keys
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{16,}\b"),  # OpenRouter keys
    re.compile(r"\bpat-[A-Za-z0-9\-]{10,}\b"),  # HubSpot private app tokens
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
)

_MAX_REDACT_DEPTH: Final = 12


def is_sensitive_key(key: str) -> bool:
    """Return ``True`` if *key* names a value that must never be logged."""
    normalised = key.strip().lower().replace("-", "_")
    if normalised in {"auth", "credential"}:
        return True
    return any(part.replace("-", "_") in normalised for part in SENSITIVE_KEY_PARTS)


def redact_text(value: str) -> str:
    """Replace credential-shaped substrings inside free text."""
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact secrets from an arbitrary JSON-like structure.

    Mapping keys that look sensitive have their values replaced wholesale; strings
    are scanned for credential-shaped tokens. Depth is bounded so that a cyclic or
    pathologically nested payload cannot stall the logging path.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and is_sensitive_key(key)
            else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


def truncate(value: Any, max_chars: int = 4000) -> Any:
    """Bound the size of a value destined for a log line or audit row."""
    if isinstance(value, str) and len(value) > max_chars:
        return f"{value[:max_chars]}...[truncated {len(value) - max_chars} chars]"
    return value


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def generate_api_key(prefix: str = "mba") -> str:
    """Generate a fresh, URL-safe API key. Only ever returned to the user once."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage.

    A plain SHA-256 is appropriate here (rather than a slow KDF such as bcrypt)
    because API keys are high-entropy machine-generated secrets, not user-chosen
    passwords, so offline brute force is not a realistic threat. *User passwords
    would require a KDF* -- this project does not store any.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, expected_hash: str) -> bool:
    """Constant-time comparison of an API key against a stored digest."""
    return hmac.compare_digest(hash_api_key(api_key), expected_hash)


def constant_time_equals(left: str, right: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(left, right)


# --------------------------------------------------------------------------- #
# Payload integrity
# --------------------------------------------------------------------------- #
def canonical_json(payload: Any) -> str:
    """Serialise *payload* deterministically.

    Sorted keys and fixed separators mean logically identical payloads always
    produce byte-identical output, which is what makes the hash comparable across
    processes and across time.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )


def hash_payload(payload: Any) -> str:
    """Return a stable SHA-256 digest of a JSON-like payload.

    Used to bind an :class:`~app.models.database.approval.ApprovalRequest` to the
    exact arguments a human approved. Any mutation of the arguments -- including a
    reordering that changes semantics, such as a different recipient list --
    produces a different digest and the execution is refused.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def verify_payload(payload: Any, expected_hash: str) -> bool:
    """Constant-time verification that *payload* still matches *expected_hash*."""
    return hmac.compare_digest(hash_payload(payload), expected_hash)
