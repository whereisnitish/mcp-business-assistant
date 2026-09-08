"""OpenAI-compatible LLM provider.

Serves OpenAI itself, OpenRouter, and any endpoint implementing the same chat
completions contract (vLLM, LiteLLM, Ollama's compat layer, Azure OpenAI via a
compatible gateway). They differ only in ``base_url`` and model name, which is why
one class covers all of them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.core.config import Settings
from app.core.exceptions import LLMConfigurationError, LLMError, LLMTimeoutError
from app.core.logging import Timer, get_logger, safe_extra
from app.mcp.types import ToolSpec
from app.providers.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMRole,
    LLMToolCall,
    LLMUsage,
    tool_spec_to_openai_schema,
)

logger = get_logger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """Chat completions against any OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings, *, client: AsyncOpenAI | None = None) -> None:
        if settings.llm_api_key is None:
            raise LLMConfigurationError(
                f"LLM_API_KEY is required for llm_provider={settings.llm_provider!r}."
            )
        self.name = settings.llm_provider
        self._model = settings.llm_model
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_tokens
        self._max_retries = settings.llm_max_retries
        self._timeout = settings.llm_timeout_seconds
        self._client = client or AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            # Retries are handled here rather than by the SDK so that every attempt
            # is logged and counted against our own budget.
            max_retries=0,
        )

    # --------------------------------------------------------------- conversion --
    @staticmethod
    def _to_wire_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Translate internal messages into the chat-completions wire format."""
        wire: list[dict[str, Any]] = []
        for message in messages:
            if message.role == LLMRole.TOOL:
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                )
                continue

            payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": _dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
                # The API requires content to be present (possibly null) alongside
                # tool calls; an empty string is rejected by some gateways.
                payload["content"] = message.content or None
            wire.append(payload)
        return wire

    @staticmethod
    def _from_wire_response(response: Any, provider: str) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            LLMToolCall.from_json_arguments(call.id, call.function.name, call.function.arguments)
            for call in (message.tool_calls or [])
            if getattr(call, "function", None) is not None
        ]
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            model=getattr(response, "model", "unknown"),
            provider=provider,
            usage=LLMUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )

    # ------------------------------------------------------------------ requests --
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_wire_messages(messages),
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            request["tools"] = [tool_spec_to_openai_schema(spec) for spec in tools]
            request["tool_choice"] = tool_choice

        return await self._complete_with_retries(request)

    async def _complete_with_retries(self, request: dict[str, Any]) -> LLMResponse:
        """Issue the request, retrying only failures that retrying can fix.

        Rate limits and transient connection errors are retried with exponential
        backoff. Authentication failures and 4xx responses are not: the same request
        will fail identically, and retrying only delays a clear error.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            timer = Timer()
            try:
                response = await self._client.chat.completions.create(**request)
            # APITimeoutError must be caught BEFORE APIConnectionError: it is a
            # subclass, so the broader handler would otherwise swallow it and the
            # caller would never see a timeout distinguished from a network fault.
            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                backoff = 2.0**attempt
                logger.warning(
                    "LLM request failed; retrying",
                    extra=safe_extra(
                        {
                            "event": "llm.retry",
                            "provider": self.name,
                            "attempt": attempt + 1,
                            "backoff_seconds": backoff,
                            "error_type": type(exc).__name__,
                        }
                    ),
                )
                await asyncio.sleep(backoff)
                continue
            except AuthenticationError as exc:
                # Deliberately does not echo the provider's message, which can
                # contain a key fragment.
                raise LLMError("The model provider rejected the configured credentials.") from exc
            except APIStatusError as exc:
                raise LLMError(
                    f"The model provider returned HTTP {exc.status_code}.",
                    details={"status_code": exc.status_code},
                ) from exc
            except Exception as exc:
                raise LLMError(f"The model provider failed ({type(exc).__name__}).") from exc

            logger.info(
                "LLM completion received",
                extra=safe_extra(
                    {
                        "event": "llm.completion",
                        "provider": self.name,
                        "model": request["model"],
                        "duration_ms": timer.elapsed_ms,
                        "tool_call_count": len(response.choices[0].message.tool_calls or []),
                    }
                ),
            )
            return self._from_wire_response(response, self.name)

        # Retries exhausted. A timeout keeps its own exception type so callers can
        # distinguish "too slow" from "refused", which drive different responses.
        if isinstance(last_error, APITimeoutError):
            raise LLMTimeoutError(
                f"The model provider did not respond within {self._timeout:g}s."
            ) from last_error
        raise LLMError(
            f"The model provider failed after {self._max_retries + 1} attempts "
            f"({type(last_error).__name__ if last_error else 'unknown'})."
        )

    async def health_check(self) -> bool:
        try:
            await self._client.models.list()
        except Exception:
            logger.warning("LLM health check failed", extra=safe_extra({"event": "llm.unhealthy"}))
            return False
        return True

    async def aclose(self) -> None:
        await self._client.close()


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
