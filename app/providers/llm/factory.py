"""LLM provider factory.

The only place that maps configuration onto a concrete provider. Everything else
depends on the :class:`~app.providers.llm.base.LLMProvider` interface.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMConfigurationError
from app.core.logging import get_logger, safe_extra
from app.providers.llm.base import LLMProvider
from app.providers.llm.fake_provider import FakeLLMProvider
from app.providers.llm.heuristic_provider import HeuristicLLMProvider
from app.providers.llm.openai_provider import OpenAICompatibleProvider

logger = get_logger(__name__)

#: OpenRouter's OpenAI-compatible endpoint, used when no explicit base URL is set.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Instantiate the configured provider.

    A mock provider in a production environment is flagged loudly: it is a
    legitimate configuration for a demo deployment, and a silent one waiting to be
    discovered by a confused user is not.
    """
    settings = settings or get_settings()
    provider = _instantiate(settings)

    if provider.is_mock and settings.is_production:
        logger.warning(
            (
                "a MOCK LLM provider is configured in production; "
                "responses are rule-based, not generated"
            ),
            extra=safe_extra({"event": "llm.mock_in_production", "provider": provider.name}),
        )

    logger.info(
        "LLM provider initialised",
        extra=safe_extra(
            {
                "event": "llm.provider_initialised",
                "provider": provider.name,
                "is_mock": provider.is_mock,
                "model": settings.llm_model if not provider.is_mock else "n/a",
            }
        ),
    )
    return provider


def _instantiate(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "heuristic":
            return HeuristicLLMProvider()
        case "fake":
            # Only meaningful when a test injects a script; on its own it answers
            # with a fixed string, which is why it is not the demo default.
            return FakeLLMProvider(default=None)
        case "openai" | "openai_compatible":
            return OpenAICompatibleProvider(settings)
        case "openrouter":
            if not settings.llm_base_url:
                settings = settings.model_copy(update={"llm_base_url": OPENROUTER_BASE_URL})
            return OpenAICompatibleProvider(settings)
        case unknown:  # pragma: no cover -- unreachable while the Literal is enforced
            raise LLMConfigurationError(f"Unsupported llm_provider {unknown!r}.")
