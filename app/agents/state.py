"""Agent state.

The single object threaded through every node of the LangGraph workflow. Making it
an explicit, typed structure rather than a loose dictionary means each node's
contract is visible: what it may read, what it is expected to produce, and what an
inspector sees when a run is replayed from the audit trail.

State is carried as a Pydantic model with ``arbitrary_types_allowed`` so that the
richer domain objects (:class:`ToolSpec`, :class:`ToolResult`) travel intact rather
than being flattened into dictionaries at every hop.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.mcp.types import ToolResult, ToolSpec
from app.models.database.enums import UserRole
from app.providers.llm.base import LLMMessage, LLMToolCall


class AgentStatus(StrEnum):
    """Terminal condition of an agent run."""

    RUNNING = "running"
    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"


class PendingApproval(BaseModel):
    """A high-risk action the run stopped on, surfaced to the caller."""

    approval_id: str
    tool_name: str
    summary: str = Field(description="Plain-language description of what will happen.")
    risk_reason: str = Field(description="Why this action needs a human decision.")
    expires_at: datetime
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentState(BaseModel):
    """Everything one agent run needs to make its next decision."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- identity ----------------------------------------------------------- #
    conversation_id: str
    user_id: str
    user_role: UserRole = UserRole.OPERATOR
    request_id: str | None = None

    # --- request ------------------------------------------------------------ #
    current_request: str
    conversation_history: list[LLMMessage] = Field(default_factory=list)

    # --- discovery ---------------------------------------------------------- #
    available_tools: list[ToolSpec] = Field(
        default_factory=list, description="Tools discovered from the connected MCP servers."
    )
    selected_tools: list[str] = Field(
        default_factory=list, description="Qualified names of every tool the model chose this run."
    )

    # --- execution ---------------------------------------------------------- #
    tool_calls: list[LLMToolCall] = Field(
        default_factory=list,
        description="Calls proposed in the current iteration, not yet executed.",
    )
    tool_results: list[ToolResult] = Field(
        default_factory=list, description="Every result gathered so far in this run."
    )

    # --- outcome ------------------------------------------------------------ #
    final_response: str | None = None
    pending_approval: PendingApproval | None = None
    status: AgentStatus = AgentStatus.RUNNING
    errors: list[str] = Field(default_factory=list)

    # --- loop control ------------------------------------------------------- #
    iteration: int = 0
    max_iterations: int = 6

    # ------------------------------------------------------------------ derived --
    @property
    def has_pending_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_finished(self) -> bool:
        return self.status != AgentStatus.RUNNING

    @property
    def iteration_budget_exhausted(self) -> bool:
        return self.iteration >= self.max_iterations

    def successful_results(self) -> list[ToolResult]:
        return [result for result in self.tool_results if result.success]

    def failed_results(self) -> list[ToolResult]:
        return [result for result in self.tool_results if not result.success]

    def summary(self) -> dict[str, Any]:
        """Compact snapshot for logs and audit entries."""
        return {
            "status": self.status.value,
            "iteration": self.iteration,
            "tools_available": len(self.available_tools),
            "tools_called": len(self.selected_tools),
            "tool_results": len(self.tool_results),
            "errors": len(self.errors),
            "awaiting_approval": self.pending_approval is not None,
        }
