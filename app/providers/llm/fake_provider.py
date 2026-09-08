"""Scripted LLM provider for tests. **Not for production use.**

Returns a pre-programmed sequence of responses, which turns "does the agent behave
correctly?" into an ordinary deterministic unit test. Because the agent loop treats
tool calls as data, a script like::

    FakeLLMProvider([
        tool_call_response("crm__get_leads", {"status": "qualified"}),
        text_response("You have 3 qualified leads."),
    ])

exercises the full plan -> authorise -> execute -> respond cycle with no API key, no
network and no flakiness. Tests that assert on approval behaviour or error recovery
depend on knowing exactly what the model will do, which a real model cannot promise.

Every call is recorded on :attr:`FakeLLMProvider.calls`, so a test can assert on
what the agent actually sent -- for instance that discovered tools were offered, or
that a tool result was fed back into the next turn.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from app.core.exceptions import LLMError
from app.mcp.types import ToolSpec
from app.providers.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMToolCall

#: A scripted step: either a fixed response, or a callable that builds one from the
#: messages it was given (useful for asserting on context mid-conversation).
ScriptStep = LLMResponse | Callable[[list[LLMMessage], list[ToolSpec] | None], LLMResponse]


def text_response(content: str, *, finish_reason: str = "stop") -> LLMResponse:
    """A plain prose answer with no tool calls."""
    return LLMResponse(
        content=content, finish_reason=finish_reason, provider="fake", model="fake-1"
    )


def tool_call_response(
    tool_name: str, arguments: dict[str, Any] | None = None, *, call_id: str | None = None
) -> LLMResponse:
    """A response requesting exactly one tool call."""
    return LLMResponse(
        content=None,
        tool_calls=[
            LLMToolCall(
                id=call_id or f"call_{uuid.uuid4().hex[:8]}",
                name=tool_name,
                arguments=arguments or {},
            )
        ],
        finish_reason="tool_calls",
        provider="fake",
        model="fake-1",
    )


def multi_tool_call_response(calls: Sequence[tuple[str, dict[str, Any]]]) -> LLMResponse:
    """A response requesting several tool calls at once (parallel tool calling)."""
    return LLMResponse(
        content=None,
        tool_calls=[
            LLMToolCall(id=f"call_{index}_{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls)
        ],
        finish_reason="tool_calls",
        provider="fake",
        model="fake-1",
    )


class FakeLLMProvider(LLMProvider):
    """Replays a fixed script of responses."""

    name = "fake"

    def __init__(
        self,
        script: Sequence[ScriptStep] | None = None,
        *,
        default: LLMResponse | None = None,
        healthy: bool = True,
        raise_on_call: Exception | None = None,
    ) -> None:
        """
        Args:
            script: Responses to return, in order.
            default: Returned once the script is exhausted. When None, an exhausted
                script raises -- an agent looping more than expected is a bug worth
                failing on, not one to paper over with a canned reply.
            healthy: What :meth:`health_check` reports.
            raise_on_call: Raised instead of responding, for testing LLM failures.
        """
        self._script: list[ScriptStep] = list(script or [])
        self._default = default
        self._healthy = healthy
        self._raise_on_call = raise_on_call
        #: Every ``(messages, tools)`` pair the agent sent, for assertions.
        self.calls: list[tuple[list[LLMMessage], list[ToolSpec] | None]] = []

    @property
    def is_mock(self) -> bool:
        return True

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def last_tools_offered(self) -> list[str]:
        """Qualified names of the tools offered on the most recent call."""
        if not self.calls:
            return []
        _, tools = self.calls[-1]
        return [spec.qualified_name for spec in tools or []]

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        self.calls.append((list(messages), list(tools) if tools is not None else None))

        if self._raise_on_call is not None:
            raise self._raise_on_call

        if not self._script:
            if self._default is not None:
                return self._default
            raise LLMError(
                f"FakeLLMProvider script exhausted after {len(self.calls)} call(s). "
                "Add another scripted response, or pass default= if the extra call is expected."
            )

        step = self._script.pop(0)
        return step(messages, tools) if callable(step) else step

    async def health_check(self) -> bool:
        return self._healthy
