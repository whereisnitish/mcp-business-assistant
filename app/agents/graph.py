"""LangGraph workflow definition.

The graph:

    START -> load_context -> discover_tools -> plan
                                                |
                          +---------------------+---------------------+
                          | tool calls proposed                       | answered directly
                          v                                           v
                    execute_tools -> validate_results ------------> respond -> END
                          ^                    |
                          +--- continue -------+
                               (back to plan)

Two routing decisions drive it:

* after ``plan``: did the model request tools, or did it answer?
* after ``validate_results``: is there more to do, or is the run finished?

The loop back to ``plan`` is what makes multi-step work possible -- fetch leads,
then draft an email using them, then send. It is bounded by ``max_iterations`` so a
model that keeps calling tools cannot run indefinitely.

LangGraph is used purely as an orchestrator here. It holds state, routes between
nodes and enforces the loop; it does not talk to the model. That keeps the LLM
provider abstraction intact and the node functions independently testable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents import nodes
from app.agents.nodes import AgentDependencies
from app.agents.state import AgentState, AgentStatus

#: Node names, referenced by the routers below.
LOAD_CONTEXT = "load_context"
DISCOVER_TOOLS = "discover_tools"
PLAN = "plan"
EXECUTE_TOOLS = "execute_tools"
VALIDATE_RESULTS = "validate_results"
RESPOND = "respond"

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def route_after_plan(state: AgentState) -> str:
    """Execute proposed tools, or go straight to the answer."""
    if state.status == AgentStatus.FAILED:
        return RESPOND
    return EXECUTE_TOOLS if state.has_pending_calls else RESPOND


def route_after_validation(state: AgentState) -> str:
    """Loop back for another planning pass, or finish.

    The run continues only while there is genuinely more to do: it stops on a
    pending approval (a human must act first), on a produced answer, and on the
    iteration ceiling.
    """
    if state.pending_approval is not None:
        return RESPOND
    if state.final_response:
        return RESPOND
    if state.iteration_budget_exhausted:
        return RESPOND
    return PLAN


def build_agent_graph(deps: AgentDependencies) -> Any:
    """Compile the workflow with its dependencies bound.

    Dependencies are bound into the node functions rather than carried in state:
    an MCP manager is a collaborator, not conversation data. The graph is compiled
    per run, which costs microseconds and avoids sharing mutable request-scoped
    objects across concurrent requests.
    """
    graph = StateGraph(AgentState)

    graph.add_node(LOAD_CONTEXT, partial(nodes.load_context, deps=deps))
    graph.add_node(DISCOVER_TOOLS, partial(nodes.discover_tools, deps=deps))
    graph.add_node(PLAN, partial(nodes.plan, deps=deps))
    graph.add_node(EXECUTE_TOOLS, partial(nodes.execute_tools, deps=deps))
    graph.add_node(VALIDATE_RESULTS, partial(nodes.validate_results, deps=deps))
    graph.add_node(RESPOND, partial(nodes.respond, deps=deps))

    graph.add_edge(START, LOAD_CONTEXT)
    graph.add_edge(LOAD_CONTEXT, DISCOVER_TOOLS)
    graph.add_edge(DISCOVER_TOOLS, PLAN)
    graph.add_conditional_edges(
        PLAN, route_after_plan, {EXECUTE_TOOLS: EXECUTE_TOOLS, RESPOND: RESPOND}
    )
    graph.add_edge(EXECUTE_TOOLS, VALIDATE_RESULTS)
    graph.add_conditional_edges(
        VALIDATE_RESULTS, route_after_validation, {PLAN: PLAN, RESPOND: RESPOND}
    )
    graph.add_edge(RESPOND, END)

    return graph.compile()
