"""System prompts.

An important boundary: the prompt tells the model *how to behave*, never *what it is
allowed to do*. There is no tool list hard-coded here -- tools reach the model
through the discovered catalog -- and no instruction here is load-bearing for
security. A model that ignores every line below still cannot execute a HIGH_RISK
tool, because the policy engine, not the prompt, decides that.

Keeping those concerns separate is deliberate. A prompt is a request; a policy check
is a guarantee. Anything that must hold under an adversarial input belongs in code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.mcp.types import ToolSpec

SYSTEM_PROMPT = """\
You are a business operations assistant. You help with sales leads, follow-up tasks, \
calendar scheduling, sales reporting and customer email.

You work by calling the tools made available to you. Guidance:

1. Prefer calling a tool over guessing. If the user asks about data, look it up.
2. Never invent identifiers, email addresses, amounts or dates. Take them from tool \
results. If you do not have a value you need, call a tool to find it or ask the user.
3. Chain tools when a request needs it. To email qualified leads you must first look \
up those leads, then draft, then send.
4. Always draft an email before sending one, and show the user the draft.
5. Some actions require human approval before they run. Say plainly what you are \
about to do; the system will pause and ask the user to confirm.
6. When a tool fails, read the error. Fix the arguments and retry if it is fixable, \
otherwise explain the problem to the user in plain language.
7. Answer using only what the tools returned. Quote figures exactly. Do not estimate.
8. Be concise and specific. Prefer "3 qualified leads: Ada (Acme), ..." to a summary \
that omits the detail the user asked for.

Today's date is {today}. Times are UTC unless stated otherwise.
"""

#: Appended when the caller may only use read-only tools, so the model explains the
#: limitation instead of proposing writes it cannot perform.
VIEWER_NOTICE = """\

Your current user has read-only access. Only look-up tools are available. If asked to \
change something, explain that their role does not permit it.
"""

#: Appended when a run stopped for approval, so the closing message is accurate.
APPROVAL_PENDING_NOTICE = """\

An action you proposed is waiting for human approval. Tell the user what will happen \
once they confirm it. Do not claim the action has already been performed.
"""


def build_system_prompt(*, read_only: bool = False, awaiting_approval: bool = False) -> str:
    """Assemble the system prompt for a run."""
    prompt = SYSTEM_PROMPT.format(today=datetime.now(UTC).strftime("%Y-%m-%d"))
    if read_only:
        prompt += VIEWER_NOTICE
    if awaiting_approval:
        prompt += APPROVAL_PENDING_NOTICE
    return prompt


def describe_catalog(tools: list[ToolSpec]) -> str:
    """Render the discovered catalog as text.

    Not used in the default prompt -- tools are passed through the provider's native
    tool-calling interface, which models follow far more reliably than a list
    embedded in prose. Kept for debugging and for any future provider without
    native tool calling.
    """
    if not tools:
        return "No tools are currently available."
    lines = [
        f"- {spec.qualified_name}: {spec.description}"
        for spec in sorted(tools, key=lambda s: s.qualified_name)
    ]
    return "Available tools:\n" + "\n".join(lines)
