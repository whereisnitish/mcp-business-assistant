"""LLM provider abstraction.

The application talks to language models exclusively through :class:`LLMProvider`.
Nothing outside :mod:`app.providers.llm` imports ``openai``, and the agent has no
idea which model is answering it.

That isolation buys three concrete things:

* **Testability.** :class:`~app.providers.llm.fake_provider.FakeLLMProvider` replaces
  the model with a scripted sequence, so agent behaviour -- including multi-step tool
  loops and error recovery -- is tested deterministically with no API key and no
  network.
* **Portability.** OpenAI, OpenRouter, vLLM, Ollama and any other OpenAI-compatible
  endpoint differ only by base URL and model name.
* **Substitutability.** A provider with a genuinely different wire format (Anthropic's
  Messages API, a local transformers pipeline) is a new subclass, not a refactor.

The message and tool-call types below are modelled on the OpenAI chat format because
it is the closest thing to a lingua franca for tool calling, but they are plain
dataclasses -- no SDK type leaks through this boundary.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.mcp.types import ToolSpec


class LLMRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json_arguments(cls, call_id: str, name: str, raw_arguments: str | None) -> LLMToolCall:
        """Build a tool call from the JSON string the wire format carries.

        Models do occasionally emit malformed JSON. Rather than raising -- which
        would abort the whole request -- the arguments become an empty mapping and
        the schema validation step reports a usable error the model can act on.
        """
        if not raw_arguments:
            return cls(id=call_id, name=name, arguments={})
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return cls(id=call_id, name=name, arguments={"__malformed_arguments__": raw_arguments})
        return cls(
            id=call_id,
            name=name,
            arguments=parsed if isinstance(parsed, dict) else {"value": parsed},
        )


@dataclass(slots=True)
class LLMMessage:
    """One message in a chat exchange."""

    role: LLMRole
    content: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> LLMMessage:
        return cls(role=LLMRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> LLMMessage:
        return cls(role=LLMRole.USER, content=content)

    @classmethod
    def assistant(
        cls, content: str | None = None, tool_calls: list[LLMToolCall] | None = None
    ) -> LLMMessage:
        return cls(role=LLMRole.ASSISTANT, content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, *, tool_call_id: str, name: str, content: str) -> LLMMessage:
        return cls(role=LLMRole.TOOL, content=content, tool_call_id=tool_call_id, name=name)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token accounting, when the provider reports it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A single completion."""

    content: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = "unknown"
    provider: str = "unknown"
    usage: LLMUsage = field(default_factory=LLMUsage)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def tool_spec_to_openai_schema(spec: ToolSpec) -> dict[str, Any]:
    """Render a :class:`ToolSpec` in the OpenAI ``tools`` format.

    Shared by every OpenAI-compatible provider. The permission level is appended to
    the description so the model knows an action will need approval and can say so
    up front -- this is *advisory only*. Enforcement is entirely backend-side, and a
    model that ignores the hint changes nothing about what actually executes.
    """
    description = spec.description
    if spec.permission.value == "high_risk":
        description = f"{description} (Requires explicit human approval before it will run.)"

    return {
        "type": "function",
        "function": {
            "name": spec.qualified_name,
            "description": description[:1024],
            "parameters": spec.input_schema or {"type": "object", "properties": {}},
        },
    }


class LLMProvider(ABC):
    """Interface every language model provider implements."""

    #: Stable identifier recorded in audit logs.
    name: str = "unknown"

    @property
    def is_mock(self) -> bool:
        """True for development/test providers that do not call a real model."""
        return False

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """Generate one completion.

        Args:
            messages: Full conversation so far, oldest first.
            tools: Tools the model may call. When empty the model must answer in prose.
            temperature: Sampling temperature; the provider default applies when None.
            max_tokens: Response length ceiling.
            tool_choice: ``"auto"``, ``"none"``, or ``"required"``.

        Raises:
            LLMError: The provider failed.
            LLMTimeoutError: The provider did not respond in time.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Whether the provider is usable right now."""

    async def aclose(self) -> None:
        """Release any held resources. Overridden by providers with HTTP clients."""
        return None
