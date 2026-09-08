"""Rule-based stand-in for a language model. **DEVELOPMENT MOCK -- not an LLM.**

Why this exists: a reviewer should be able to run ``docker compose up --build`` and
exercise the entire system -- dynamic tool discovery, multi-step tool sequences, the
permission engine, the human approval workflow, the audit trail -- without an API
key. A demo that cannot run without a paid credential is a demo most people never
see working.

What it is: deterministic keyword matching over the **dynamically discovered** tool
catalog. It has no model, no learning and no language understanding. It reads the
user's latest message, picks a tool by pattern, and reads earlier tool results to
decide what to do next. Because it works off the same discovered catalog as a real
model, it also proves the discovery path end to end.

What it is *not*: a substitute for a real model. Phrasing outside its rules falls
through to a capability listing. Set ``LLM_PROVIDER=openai`` for genuine reasoning.

Its most useful trait is the multi-step follow-up flow: "send a follow-up email to
qualified leads" fans out into ``crm__get_leads`` -> ``email__draft_email`` ->
``email__send_email``, where the last step is HIGH_RISK and stops for human
approval. That is the flow worth seeing, and it runs with no credentials at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.mcp.types import ToolSpec
from app.providers.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMRole,
    LLMToolCall,
    LLMUsage,
)

logger = get_logger(__name__)

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CALL_COUNTER = "heuristic"


@dataclass(frozen=True, slots=True)
class Intent:
    """A matched user intent and the tool sequence that satisfies it."""

    name: str
    patterns: tuple[str, ...]
    #: Ordered tools to run. Each entry builds arguments from the request text and
    #: from results already gathered this turn.
    steps: tuple[tuple[str, Callable[[str, dict[str, Any]], dict[str, Any]]], ...]

    def matches(self, text: str) -> bool:
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in self.patterns)


# --------------------------------------------------------------------------- #
# Argument builders
# --------------------------------------------------------------------------- #
def _no_args(text: str, results: dict[str, Any]) -> dict[str, Any]:
    return {}


def _lead_status_filter(text: str, results: dict[str, Any]) -> dict[str, Any]:
    for status in ("qualified", "contacted", "unqualified", "won", "lost", "new"):
        if re.search(rf"\b{status}\b", text, re.IGNORECASE):
            return {"status": status, "limit": 25}
    return {"limit": 25}


def _search_query(text: str, results: dict[str, Any]) -> dict[str, Any]:
    match = re.search(r"(?:about|for|named|called)\s+([\w .'-]{2,60})", text, re.IGNORECASE)
    query = match.group(1).strip(" .") if match else text.strip()[:60]
    return {"query": query, "limit": 25}


def _sales_period(text: str, results: dict[str, Any]) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    if re.search(r"\bthis week\b|\bweekly\b", text, re.IGNORECASE):
        start = today - timedelta(days=today.weekday())
    elif re.search(r"\bthis month\b|\bmonthly\b", text, re.IGNORECASE):
        start = today.replace(day=1)
    else:
        start = today - timedelta(days=29)
    return {"start_date": start.isoformat(), "end_date": today.isoformat()}


def _new_task(text: str, results: dict[str, Any]) -> dict[str, Any]:
    match = re.search(
        r"(?:task|reminder|follow[- ]?up)\s*(?:to|for|about|:)?\s*(.{3,200})", text, re.IGNORECASE
    )
    title = match.group(1).strip(" .") if match else "Follow-up task"
    return {
        "title": title[:200],
        "due_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        "priority": "high" if re.search(r"\burgent\b|\basap\b", text, re.IGNORECASE) else "medium",
    }


def _new_lead(text: str, results: dict[str, Any]) -> dict[str, Any]:
    email_match = _EMAIL_PATTERN.search(text)
    name_match = re.search(
        r"(?:add|create)\s+(?:a\s+)?(?:new\s+)?lead\s+(?:for\s+)?([A-Za-z][\w .'-]{1,60})",
        text,
        re.IGNORECASE,
    )
    name = (name_match.group(1).strip(" .") if name_match else "").split(" from ")[0].strip()
    email = email_match.group(0) if email_match else ""
    if not name and email:
        name = email.split("@")[0].replace(".", " ").title()
    company_match = re.search(r"\b(?:at|from)\s+([A-Z][\w &.-]{1,60})", text)
    return {
        "full_name": name or "Unknown Contact",
        "email": email or "unknown@example.com",
        "company": company_match.group(1).strip(" .") if company_match else None,
        "source": "assistant",
    }


def _qualified_lead_emails(results: dict[str, Any]) -> list[str]:
    """Pull recipient addresses out of an earlier CRM result.

    Addresses always come from tool output, never from the request text -- the same
    discipline a real agent needs so that a user (or an injected instruction) cannot
    smuggle in an arbitrary recipient.
    """
    payload = results.get("crm__get_leads") or results.get("crm__search_leads") or {}
    leads = payload.get("leads", []) if isinstance(payload, dict) else []
    return [lead["email"] for lead in leads if isinstance(lead, dict) and lead.get("email")][:20]


def _draft_followup(text: str, results: dict[str, Any]) -> dict[str, Any]:
    recipients = _qualified_lead_emails(results) or ["unknown@example.com"]
    return {
        "to": recipients,
        "subject": "Following up on your interest",
        "body": (
            "Hi,\n\n"
            "Thanks for your interest. I wanted to follow up and see whether you have "
            "any questions we can help with.\n\n"
            "Best regards,\nThe Sales Team"
        ),
    }


def _send_followup(text: str, results: dict[str, Any]) -> dict[str, Any]:
    """Send exactly what was drafted.

    Reusing the draft verbatim is what makes the approval meaningful: the human
    approves a specific message, and that same message is what the send step
    proposes.
    """
    draft = results.get("email__draft_email")
    if isinstance(draft, dict) and draft.get("to"):
        return {
            "to": draft["to"],
            "subject": draft.get("subject", ""),
            "body": draft.get("body", ""),
        }
    return _draft_followup(text, results)


# --------------------------------------------------------------------------- #
# Intent table -- order matters; the first match wins.
# --------------------------------------------------------------------------- #
INTENTS: tuple[Intent, ...] = (
    Intent(
        name="email_followup",
        patterns=(r"\b(send|email)\b.*\blead", r"follow[- ]?up email", r"\bemail\b.*\bqualified\b"),
        steps=(
            ("crm__get_leads", _lead_status_filter),
            ("email__draft_email", _draft_followup),
            ("email__send_email", _send_followup),
        ),
    ),
    Intent(
        name="overdue_tasks",
        patterns=(r"\boverdue\b", r"\bpast due\b", r"\bbehind\b.*\btask"),
        steps=(("tasks__get_overdue_tasks", _no_args),),
    ),
    Intent(
        name="todays_meetings",
        patterns=(
            r"\btoday'?s?\b.*\b(meeting|schedule|calendar)",
            r"\bmeetings? (today|do i have)",
            r"\bwhat'?s on my calendar\b",
        ),
        steps=(("calendar__get_todays_meetings", _no_args),),
    ),
    Intent(
        name="weekly_report",
        patterns=(r"\bweekly report\b", r"\breport\b.*\bweek\b", r"\bthis week'?s sales\b"),
        steps=(("spreadsheets__generate_weekly_report", _no_args),),
    ),
    Intent(
        name="sales_summary",
        patterns=(r"\bsales\b", r"\brevenue\b", r"\bhow much did we (sell|make)\b"),
        steps=(("spreadsheets__get_sales_summary", _sales_period),),
    ),
    Intent(
        name="create_lead",
        patterns=(r"\b(add|create)\b.*\blead\b", r"\bnew lead\b"),
        steps=(("crm__create_lead", _new_lead),),
    ),
    Intent(
        name="create_task",
        patterns=(r"\b(create|add|make)\b.*\b(task|reminder|follow[- ]?up)\b",),
        steps=(("tasks__create_task", _new_task),),
    ),
    Intent(
        name="search_leads",
        patterns=(r"\b(find|search|look up)\b.*\b(lead|contact|customer)\b",),
        steps=(("crm__search_leads", _search_query),),
    ),
    Intent(
        name="list_leads",
        patterns=(r"\blead", r"\bpipeline\b", r"\bprospects?\b"),
        steps=(("crm__get_leads", _lead_status_filter),),
    ),
    Intent(
        name="list_tasks",
        patterns=(r"\btasks?\b", r"\bto[- ]?do\b"),
        steps=(("tasks__get_tasks", _no_args),),
    ),
)


class HeuristicLLMProvider(LLMProvider):
    """Deterministic rule-based planner. Development and demo use only."""

    name = "heuristic"

    @property
    def is_mock(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        available = {spec.qualified_name for spec in tools or []}
        request_text = _latest_user_text(messages)
        gathered = _collect_tool_results(messages)

        intent = next((candidate for candidate in INTENTS if candidate.matches(request_text)), None)

        if intent is not None:
            for tool_name, build_arguments in intent.steps:
                if tool_name in gathered or tool_name not in available:
                    continue
                logger.info(
                    "heuristic provider selected a tool",
                    extra={
                        "event": "llm.heuristic_selection",
                        "intent": intent.name,
                        "tool": tool_name,
                    },
                )
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        LLMToolCall(
                            id=f"{_CALL_COUNTER}_{len(gathered)}_{tool_name}",
                            name=tool_name,
                            arguments=_drop_nulls(build_arguments(request_text, gathered)),
                        )
                    ],
                    finish_reason="tool_calls",
                    provider=self.name,
                    model="heuristic-rules-v1",
                    usage=LLMUsage(),
                )

        return LLMResponse(
            content=_summarise(request_text, gathered, tools or []),
            finish_reason="stop",
            provider=self.name,
            model="heuristic-rules-v1",
            usage=LLMUsage(),
        )

    async def health_check(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Response construction
# --------------------------------------------------------------------------- #
def _latest_user_text(messages: list[LLMMessage]) -> str:
    for message in reversed(messages):
        if message.role == LLMRole.USER and message.content:
            return message.content
    return ""


def _collect_tool_results(messages: list[LLMMessage]) -> dict[str, Any]:
    """Map tool name -> parsed result for tools run **since the latest user message**.

    Scoping to the current turn matters twice over. Results from an earlier turn
    would otherwise make a step look already-done and be skipped -- asking "show me
    the leads" twice would answer the second time from stale data -- and the closing
    summary would repeat findings the user was told about several turns ago.
    """
    results: dict[str, Any] = {}
    for message in _current_turn(messages):
        if message.role != LLMRole.TOOL or not message.name:
            continue
        try:
            results[message.name] = json.loads(message.content or "null")
        except json.JSONDecodeError:
            results[message.name] = message.content
    return results


def _current_turn(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Messages from the most recent user message onwards."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == LLMRole.USER:
            return messages[index:]
    return list(messages)


def _drop_nulls(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if value is not None}


def _summarise(request_text: str, results: dict[str, Any], tools: list[ToolSpec]) -> str:
    """Turn gathered tool results into a plain-language answer.

    Every number quoted here is read from a tool result, never invented -- the same
    property you would want from a real model, enforced here by construction.
    """
    if not results:
        if not request_text:
            return "How can I help with your leads, tasks, calendar or sales data?"
        readable = sorted(spec.qualified_name for spec in tools if spec.permission.value == "read")
        return (
            "I could not match that request to one of my tools. I can look up leads, "
            "search the CRM, list or complete tasks, show overdue work, report on sales, "
            "check your calendar, and draft or send email.\n\n"
            f"Available read-only tools: {', '.join(readable) or 'none discovered'}."
        )

    lines: list[str] = []
    for tool_name, payload in results.items():
        lines.append(_describe_result(tool_name, payload))
    return "\n".join(line for line in lines if line)


def _describe_result(tool_name: str, payload: Any) -> str:
    """One sentence describing a single tool result."""
    if not isinstance(payload, dict):
        return f"{tool_name}: {payload}"

    if tool_name in ("crm__get_leads", "crm__search_leads"):
        leads = payload.get("leads", [])
        if not leads:
            return "I did not find any leads matching that."
        preview = "; ".join(
            (
                f"{lead.get('full_name')} "
                f"({lead.get('company') or 'no company'}) - {lead.get('status')}"
            )
            for lead in leads[:5]
        )
        suffix = f" and {len(leads) - 5} more" if len(leads) > 5 else ""
        return f"Found {payload.get('count', len(leads))} lead(s): {preview}{suffix}."

    if tool_name == "crm__create_lead":
        return (
            f"Added {payload.get('full_name')} ({payload.get('email')}) "
            f"to the CRM as a '{payload.get('status')}' lead."
        )

    if tool_name in ("tasks__get_tasks", "tasks__get_overdue_tasks"):
        tasks = payload.get("tasks", [])
        if not tasks:
            return "There are no matching tasks -- nothing is outstanding."
        preview = "; ".join(
            f"{task.get('title')} (due {task.get('due_at') or 'no date'})" for task in tasks[:5]
        )
        overdue = payload.get("overdue_count", 0)
        return f"Found {payload.get('count', len(tasks))} task(s), {overdue} overdue: {preview}."

    if tool_name == "tasks__create_task":
        return (
            f"Created the task '{payload.get('title')}', due "
            f"{payload.get('due_at') or 'with no deadline'}."
        )

    if tool_name == "spreadsheets__get_sales_summary":
        return (
            f"Between {payload.get('period_start')} and {payload.get('period_end')} there were "
            f"{payload.get('record_count')} sales totalling {payload.get('total_revenue')} "
            f"{payload.get('currency')}, averaging {payload.get('average_order_value')} per order."
        )

    if tool_name == "spreadsheets__generate_weekly_report":
        return (
            f"Weekly report for {payload.get('week_start')} to "
            f"{payload.get('week_end')}: {payload.get('headline')}"
        )

    if tool_name == "calendar__get_todays_meetings":
        events = payload.get("events", [])
        if not events:
            return "You have no meetings scheduled today."
        preview = "; ".join(
            f"{event.get('title')} at {event.get('starts_at')}" for event in events[:5]
        )
        return f"You have {payload.get('count', len(events))} meeting(s) today: {preview}."

    if tool_name == "email__draft_email":
        return (
            f"Drafted an email to {payload.get('recipient_count')} recipient(s) "
            f"with the subject '{payload.get('subject')}'."
        )

    if tool_name == "email__send_email":
        return (
            f"Email sent to {len(payload.get('to', []))} recipient(s) "
            f"via the {payload.get('provider')} provider."
        )

    return f"{tool_name} completed."
