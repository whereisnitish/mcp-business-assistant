"""Credential handling, redaction and payload integrity."""

from __future__ import annotations

import pytest

from app.core.security import (
    REDACTED,
    canonical_json,
    generate_api_key,
    hash_api_key,
    hash_payload,
    is_sensitive_key,
    redact,
    truncate,
    verify_api_key,
    verify_payload,
)

pytestmark = pytest.mark.unit


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "PASSWORD",
            "api_key",
            "API-Key",
            "llm_api_key",
            "Authorization",
            "smtp_password",
            "access_token",
            "hubspot_access_token",
            "private_key",
            "session_id",
        ],
    )
    def test_sensitive_keys_are_recognised(self, key: str) -> None:
        assert is_sensitive_key(key)

    @pytest.mark.parametrize(
        "key", ["email", "full_name", "subject", "body", "tool_name", "status"]
    )
    def test_ordinary_keys_are_not_redacted(self, key: str) -> None:
        assert not is_sensitive_key(key)

    def test_nested_secrets_are_removed(self) -> None:
        payload = {
            "user": "ada@example.com",
            "config": {"smtp_password": "hunter2", "host": "smtp.example.com"},
            "list": [{"token": "abc123"}],
        }
        cleaned = redact(payload)

        assert cleaned["config"]["smtp_password"] == REDACTED
        assert cleaned["config"]["host"] == "smtp.example.com"
        assert cleaned["list"][0]["token"] == REDACTED
        assert cleaned["user"] == "ada@example.com"

    @pytest.mark.parametrize(
        "text",
        [
            "my key is sk-abcdefghijklmnopqrstuvwx",
            "Authorization: Bearer abcdefghijklmnopqrstuvwx",
            "token ghp_abcdefghijklmnopqrstuvwxyz12",
            "hubspot pat-na1-abcdefghijkl",
        ],
    )
    def test_credential_shaped_values_in_free_text_are_scrubbed(self, text: str) -> None:
        """A model can echo a key back inside an error string."""
        assert REDACTED in redact(text)

    def test_deeply_nested_structures_terminate(self) -> None:
        """A pathological payload must not stall the logging path."""
        payload: dict = {"level": 0}
        current = payload
        for depth in range(1, 40):
            current["next"] = {"level": depth}
            current = current["next"]

        assert redact(payload) is not None

    def test_truncate_bounds_large_values(self) -> None:
        assert "truncated" in str(truncate("x" * 5000, 100))
        assert truncate("short", 100) == "short"


class TestApiKeys:
    def test_generated_keys_are_unique_and_prefixed(self) -> None:
        first, second = generate_api_key(), generate_api_key()
        assert first != second
        assert first.startswith("mba_")
        assert len(first) > 30

    def test_verification_matches_only_the_right_key(self) -> None:
        key = generate_api_key()
        digest = hash_api_key(key)

        assert verify_api_key(key, digest)
        assert not verify_api_key(generate_api_key(), digest)

    def test_digest_does_not_contain_the_key(self) -> None:
        key = generate_api_key()
        assert key not in hash_api_key(key)


class TestPayloadIntegrity:
    def test_hash_is_stable_across_key_order(self) -> None:
        """Two logically identical payloads must hash identically.

        Without this, an approval could fail verification purely because a mapping
        was rebuilt in a different order.
        """
        assert hash_payload({"a": 1, "b": [1, 2]}) == hash_payload({"b": [1, 2], "a": 1})

    @pytest.mark.parametrize(
        "tampered",
        [
            {"to": ["attacker@evil.example"], "subject": "Hi"},
            {"to": ["ada@example.com"], "subject": "Hi!"},
            {"to": ["ada@example.com", "extra@evil.example"], "subject": "Hi"},
            {"to": ["ada@example.com"]},
        ],
    )
    def test_any_mutation_breaks_verification(self, tampered: dict) -> None:
        original = {"to": ["ada@example.com"], "subject": "Hi"}
        digest = hash_payload(original)

        assert verify_payload(original, digest)
        assert not verify_payload(tampered, digest)

    def test_list_order_is_significant(self) -> None:
        """Recipient order changes meaning, so it must change the hash."""
        first = hash_payload({"to": ["a@example.com", "b@example.com"]})
        second = hash_payload({"to": ["b@example.com", "a@example.com"]})
        assert first != second

    def test_canonical_json_is_deterministic(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
