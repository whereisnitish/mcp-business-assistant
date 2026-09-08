"""OpenAI-compatible provider tests, with no API key and no network.

The provider accepts an injected client, which is what makes its error handling and
retry policy testable at all -- these are exactly the paths that are impossible to
exercise reliably against a real endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)
from pydantic import SecretStr

from app.core.config import Settings
from app.core.exceptions import LLMError, LLMTimeoutError
from app.providers.llm.base import LLMMessage
from app.providers.llm.openai_provider import OpenAICompatibleProvider
from tests.fixtures.factories import make_tool_spec

pytestmark = pytest.mark.unit


async def _noop() -> None:
    """An awaitable that does nothing -- stands in for ``asyncio.sleep``."""
    return None


class FakeCompletions:
    """Stands in for ``client.chat.completions``."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.completions = FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def completion(content: str | None = None, tool_calls: list[Any] | None = None) -> Any:
    """Build a response shaped like the OpenAI SDK's."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    return SimpleNamespace(choices=[choice], model="gpt-4o-mini", usage=usage)


def wire_tool_call(call_id: str, name: str, arguments: str) -> Any:
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments), type="function"
    )


def build_provider(
    settings: Settings, outcomes: list[Any]
) -> tuple[OpenAICompatibleProvider, FakeClient]:
    client = FakeClient(outcomes)
    configured = settings.model_copy(
        update={"llm_provider": "openai", "llm_api_key": SecretStr("sk-test"), "llm_max_retries": 2}
    )
    return OpenAICompatibleProvider(configured, client=client), client  # type: ignore[arg-type]


def http_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError("boom", response=response, body=None)


class TestResponseParsing:
    async def test_text_response(self, settings: Settings) -> None:
        provider, _ = build_provider(settings, [completion(content="Hello there.")])

        response = await provider.complete([LLMMessage.user("hi")])

        assert response.content == "Hello there."
        assert not response.wants_tools
        assert response.usage.total_tokens == 18

    async def test_tool_calls_are_parsed(self, settings: Settings) -> None:
        provider, _ = build_provider(
            settings,
            [completion(tool_calls=[wire_tool_call("c1", "crm__get_leads", '{"status": "new"}')])],
        )

        response = await provider.complete([LLMMessage.user("leads")])

        assert response.wants_tools
        assert response.tool_calls[0].name == "crm__get_leads"
        assert response.tool_calls[0].arguments == {"status": "new"}

    async def test_malformed_tool_arguments_do_not_raise(self, settings: Settings) -> None:
        """A model emitting broken JSON must be recoverable, not fatal."""
        provider, _ = build_provider(
            settings, [completion(tool_calls=[wire_tool_call("c1", "crm__get_leads", "{oops")])]
        )

        response = await provider.complete([LLMMessage.user("leads")])

        assert "__malformed_arguments__" in response.tool_calls[0].arguments


class TestRequestConstruction:
    async def test_tools_are_sent_when_offered(self, settings: Settings) -> None:
        provider, client = build_provider(settings, [completion(content="ok")])

        await provider.complete([LLMMessage.user("hi")], tools=[make_tool_spec("crm__get_leads")])

        request = client.completions.requests[0]
        assert request["tools"][0]["function"]["name"] == "crm__get_leads"
        assert request["tool_choice"] == "auto"

    async def test_no_tools_key_when_none_are_available(self, settings: Settings) -> None:
        """Sending an empty tools array is rejected by some gateways."""
        provider, client = build_provider(settings, [completion(content="ok")])

        await provider.complete([LLMMessage.user("hi")], tools=None)

        assert "tools" not in client.completions.requests[0]


class TestErrorHandling:
    async def test_a_timeout_maps_to_a_timeout_error(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`APITimeoutError` subclasses `APIConnectionError`.

        If the broader handler is ordered first it swallows the timeout, and the
        caller can never tell "too slow" from "refused".
        """
        import asyncio

        monkeypatch.setattr(asyncio, "sleep", lambda _seconds: _noop())

        timeouts = [APITimeoutError(request=httpx.Request("POST", "http://x")) for _ in range(3)]
        provider, _ = build_provider(settings, timeouts)

        with pytest.raises(LLMTimeoutError):
            await provider.complete([LLMMessage.user("hi")])

    async def test_authentication_failures_do_not_echo_the_provider_message(
        self, settings: Settings
    ) -> None:
        """A provider's auth error can contain a fragment of the key."""
        request = httpx.Request("POST", "http://x")
        response = httpx.Response(401, request=request)
        error = AuthenticationError("invalid key sk-abcdef123456", response=response, body=None)
        provider, _ = build_provider(settings, [error])

        with pytest.raises(LLMError) as raised:
            await provider.complete([LLMMessage.user("hi")])

        assert "sk-abcdef123456" not in raised.value.message
        assert "rejected the configured credentials" in raised.value.message

    async def test_a_4xx_is_not_retried(self, settings: Settings) -> None:
        """Retrying a bad request only delays a clear error."""
        provider, client = build_provider(settings, [http_error(400), completion(content="never")])

        with pytest.raises(LLMError):
            await provider.complete([LLMMessage.user("hi")])

        assert len(client.completions.requests) == 1


class TestRetries:
    async def test_a_rate_limit_is_retried_and_can_succeed(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)

        request = httpx.Request("POST", "http://x")
        rate_limited = RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        )
        provider, client = build_provider(
            settings, [rate_limited, completion(content="Recovered.")]
        )

        response = await provider.complete([LLMMessage.user("hi")])

        assert response.content == "Recovered."
        assert len(client.completions.requests) == 2

    async def test_retries_are_bounded(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)

        failures = [APIConnectionError(request=httpx.Request("POST", "http://x")) for _ in range(5)]
        provider, client = build_provider(settings, failures)

        with pytest.raises(LLMError):
            await provider.complete([LLMMessage.user("hi")])

        # max_retries=2 means three attempts in total, then give up.
        assert len(client.completions.requests) == 3


class TestLifecycle:
    async def test_closing_releases_the_http_client(self, settings: Settings) -> None:
        provider, client = build_provider(settings, [])
        await provider.aclose()
        assert client.closed

    def test_it_does_not_present_itself_as_a_mock(self, settings: Settings) -> None:
        provider, _ = build_provider(settings, [])
        assert not provider.is_mock
