"""FastAPI dependency wiring.

Every collaborator an endpoint needs is provided here rather than constructed
inside handlers. That is what makes the API testable: a test overrides
``get_mcp_manager`` or ``get_llm_provider`` and the whole stack runs against a fake
without a single production line changing.

Process-lifetime objects (the MCP manager, the LLM provider) live on ``app.state``
and are created once in the lifespan handler -- an MCP manager per request would
spawn five subprocesses per request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import AgentRunner
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger, safe_extra
from app.db.session import get_db_session
from app.mcp.manager import MCPManager
from app.models.database.user import User
from app.providers.llm.base import LLMProvider
from app.repositories.user_repository import UserRepository
from app.security.policy import PolicyEngine
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.conversation_service import ConversationService
from app.services.tool_execution_service import ToolExecutionService

logger = get_logger(__name__)

DBSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_app_settings() -> Settings:
    return get_settings()


AppSettings = Annotated[Settings, Depends(get_app_settings)]


# --------------------------------------------------------------------------- #
# Process-lifetime objects
# --------------------------------------------------------------------------- #
def get_mcp_manager(request: Request) -> MCPManager:
    """The shared MCP manager created during application startup."""
    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is None:  # pragma: no cover -- only reachable if startup failed
        raise RuntimeError("The MCP manager is not initialised.")
    return manager


def get_llm_provider(request: Request) -> LLMProvider:
    """The shared LLM provider created during application startup."""
    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None:  # pragma: no cover
        raise RuntimeError("The LLM provider is not initialised.")
    return provider


Manager = Annotated[MCPManager, Depends(get_mcp_manager)]
LLM = Annotated[LLMProvider, Depends(get_llm_provider)]


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
async def get_current_user(
    session: DBSession,
    settings: AppSettings,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """Resolve the caller.

    With ``AUTH_ENABLED=false`` (the default, for a frictionless demo) every request
    is attributed to a single demo user. That is a deliberate convenience for local
    evaluation, and :func:`app.main.create_app` warns at startup if it is left on
    outside a local environment.

    With authentication enabled the ``X-API-Key`` header is required and resolved by
    **digest**, so the plaintext key is never compared in SQL or written to a query
    log. A real deployment would put OAuth2/OIDC here; the seam is the same.
    """
    users = UserRepository(session)

    if not settings.auth_enabled:
        return await users.get_or_create(email=settings.demo_user_email, full_name="Demo User")

    if not x_api_key:
        raise AuthenticationError("An X-API-Key header is required.")

    user = await users.get_by_api_key(x_api_key)
    if user is None:
        # Same message for an unknown key and an inactive user: distinguishing them
        # would let a caller probe which keys exist.
        logger.warning("rejected an invalid API key", extra=safe_extra({"event": "auth.rejected"}))
        raise AuthenticationError("The supplied API key is not valid.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# Services (request-scoped)
# --------------------------------------------------------------------------- #
def get_audit_service(session: DBSession) -> AuditService:
    return AuditService(session)


AuditDep = Annotated[AuditService, Depends(get_audit_service)]


def get_approval_service(
    session: DBSession, audit: AuditDep, settings: AppSettings
) -> ApprovalService:
    return ApprovalService(session, audit, settings)


ApprovalDep = Annotated[ApprovalService, Depends(get_approval_service)]


def get_conversation_service(session: DBSession, settings: AppSettings) -> ConversationService:
    return ConversationService(session, settings)


ConversationDep = Annotated[ConversationService, Depends(get_conversation_service)]


def get_policy_engine(settings: AppSettings) -> PolicyEngine:
    return PolicyEngine(settings)


PolicyDep = Annotated[PolicyEngine, Depends(get_policy_engine)]


def get_tool_execution_service(
    manager: Manager,
    approvals: ApprovalDep,
    audit: AuditDep,
    policy: PolicyDep,
    settings: AppSettings,
) -> ToolExecutionService:
    return ToolExecutionService(
        manager=manager, approvals=approvals, audit=audit, policy=policy, settings=settings
    )


ToolExecutionDep = Annotated[ToolExecutionService, Depends(get_tool_execution_service)]


def get_agent_runner(
    llm: LLM,
    manager: Manager,
    tools: ToolExecutionDep,
    conversations: ConversationDep,
    audit: AuditDep,
    settings: AppSettings,
) -> AgentRunner:
    return AgentRunner(
        llm=llm,
        manager=manager,
        tools=tools,
        conversations=conversations,
        audit=audit,
        settings=settings,
    )


AgentDep = Annotated[AgentRunner, Depends(get_agent_runner)]
