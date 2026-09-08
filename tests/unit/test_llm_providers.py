"""LLM provider abstraction tests."""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.exceptions import LLMConfigurationError, LLMError
from app.models.database.enums import PermissionLevel
from app.providers.llm.base import (
    LLMMessage,
    LLMRole,
    LLMToolCall,
    tool_spec_to_openai_schema,
)
from app.providers.llm.factory import build_llm_provider
from app.providers.llm.fake_provider import FakeLLMProvider, text_response, tool_call_response
from app.providers.llm.heuristic_provider import HeuristicLLMProvider
from app.providers.llm.openai_provider import OpenAICompatibleProvider
from tests.fixtures.factories import make_tool_spec

pytestmark = pytest.mark.unit


class TestToolSchemaConversion:
    def test_tool_spec_becomes_an_openai_function(self) -> None:
        schema = tool_spec_to_openai_schema(
            make_tool_spec(
                "crm__get_leads", input_schema={"type": "object", "properties": {"a": {}}}
            )
        )
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "crm__get_leads"
        assert schema["function"]["parameters"]["properties"] == {"a": {}}

    def test_high_risk_tools_are_advertised_as_needing_approval(self) -> None:
        """Advisory only -- it helps the model set expectations, not bypass anything."""
        schema = tool_spec_to_openai_schema(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK)
        )
        assert "human approval" in schema["function"]["description"]

    def test_descriptions_are_bounded(self) -> None:
        schema = tool_spec_to_openai_schema(
            make_tool_spec("crm__get_leads", description="x" * 3000)
        )
        assert len(schema["function"]["description"]) <= 1024


class TestMalformedToolArguments:
    def test_invalid_json_is_flagged_rather_than_raised(self) -> None:
        """A model emitting broken JSON must not abort the request."""
        call = LLMToolCall.from_json_arguments("1", "crm__get_leads", "{not json")
        assert "__malformed_arguments__" in call.arguments

    def test_valid_json_is_parsed(self) -> None:
        call = LLMToolCall.from_json_arguments("1", "crm__get_leads", '{"status": "new"}')
        assert call.arguments == {"status": "new"}

    def test_missing_arguments_become_an_empty_mapping(self) -> None:
        assert LLMToolCall.from_json_arguments("1", "t", None).arguments == {}

    def test_a_non_object_payload_is_wrapped(self) -> None:
        assert LLMToolCall.from_json_arguments("1", "t", "42").arguments == {"value": 42}


class TestFakeProvider:
    async def test_responses_are_replayed_in_order(self) -> None:
        provider = FakeLLMProvider([tool_call_response("crm__get_leads"), text_response("Done.")])

        first = await provider.complete([LLMMessage.user("hi")])
        second = await provider.complete([LLMMessage.user("hi")])

        assert first.wants_tools
        assert second.content == "Done."
        assert provider.call_count == 2

    async def test_an_exhausted_script_fails_loudly(self) -> None:
        """An unexpected extra call is a bug worth failing on."""
        provider = FakeLLMProvider([text_response("only one")])
        await provider.complete([])
        with pytest.raises(LLMError):
            await provider.complete([])

    async def test_calls_are_recorded_for_assertions(self) -> None:
        provider = FakeLLMProvider([text_response("hi")])
        tools = [make_tool_spec("crm__get_leads")]
        await provider.complete([LLMMessage.user("q")], tools=tools)

        assert provider.last_tools_offered() == ["crm__get_leads"]
        assert provider.calls[0][0][0].content == "q"

    async def test_it_reports_itself_as_a_mock(self) -> None:
        assert FakeLLMProvider().is_mock


class TestHeuristicProvider:
    """The credential-free demo brain. Deterministic by construction."""

    TOOLS = [
        make_tool_spec("crm__get_leads"),
        make_tool_spec("crm__search_leads"),
        make_tool_spec("crm__create_lead", permission=PermissionLevel.WRITE),
        make_tool_spec("tasks__get_overdue_tasks"),
        make_tool_spec("tasks__create_task", permission=PermissionLevel.WRITE),
        make_tool_spec("calendar__get_todays_meetings"),
        make_tool_spec("spreadsheets__get_sales_summary"),
        make_tool_spec("spreadsheets__generate_weekly_report"),
        make_tool_spec("email__draft_email"),
        make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
    ]

    @pytest.mark.parametrize(
        ("request_text", "expected_tool"),
        [
            ("what tasks are overdue?", "tasks__get_overdue_tasks"),
            ("show me today's meetings", "calendar__get_todays_meetings"),
            ("what meetings do I have today", "calendar__get_todays_meetings"),
            ("generate the weekly report", "spreadsheets__generate_weekly_report"),
            ("how much revenue this month?", "spreadsheets__get_sales_summary"),
            ("show me qualified leads", "crm__get_leads"),
            ("find the lead for Acme", "crm__search_leads"),
            ("add a lead for Ada Lovelace ada@acme.io", "crm__create_lead"),
            ("create a task to call Ada", "tasks__create_task"),
        ],
    )
    async def test_intents_map_to_the_expected_tool(
        self, request_text: str, expected_tool: str
    ) -> None:
        response = await HeuristicLLMProvider().complete(
            [LLMMessage.user(request_text)], tools=self.TOOLS
        )
        assert response.tool_calls[0].name == expected_tool

    async def test_it_is_deterministic(self) -> None:
        provider = HeuristicLLMProvider()
        messages = [LLMMessage.user("show me qualified leads")]

        first = await provider.complete(messages, tools=self.TOOLS)
        second = await provider.complete(messages, tools=self.TOOLS)

        assert first.tool_calls[0].name == second.tool_calls[0].name
        assert first.tool_calls[0].arguments == second.tool_calls[0].arguments

    async def test_status_filters_are_extracted_from_the_request(self) -> None:
        response = await HeuristicLLMProvider().complete(
            [LLMMessage.user("show me qualified leads")], tools=self.TOOLS
        )
        assert response.tool_calls[0].arguments["status"] == "qualified"

    async def test_the_email_flow_chains_three_tools(self) -> None:
        """The multi-step path a reviewer sees without any credentials."""
        provider = HeuristicLLMProvider()
        messages = [LLMMessage.user("send a follow-up email to qualified leads")]

        first = await provider.complete(messages, tools=self.TOOLS)
        assert first.tool_calls[0].name == "crm__get_leads"

        messages += [
            LLMMessage.assistant(tool_calls=first.tool_calls),
            LLMMessage.tool(
                tool_call_id=first.tool_calls[0].id,
                name="crm__get_leads",
                content=json.dumps(
                    {"leads": [{"email": "ada@acme.io", "full_name": "Ada"}], "count": 1}
                ),
            ),
        ]
        second = await provider.complete(messages, tools=self.TOOLS)
        assert second.tool_calls[0].name == "email__draft_email"
        assert second.tool_calls[0].arguments["to"] == ["ada@acme.io"]

    async def test_recipients_come_only_from_tool_results(self) -> None:
        """Addresses must never be lifted out of the user's prose."""
        provider = HeuristicLLMProvider()
        messages = [LLMMessage.user("send a follow-up email to leads at attacker@evil.example")]

        first = await provider.complete(messages, tools=self.TOOLS)
        messages += [
            LLMMessage.assistant(tool_calls=first.tool_calls),
            LLMMessage.tool(
                tool_call_id=first.tool_calls[0].id,
                name="crm__get_leads",
                content=json.dumps({"leads": [{"email": "real@crm.example"}], "count": 1}),
            ),
        ]
        second = await provider.complete(messages, tools=self.TOOLS)

        assert second.tool_calls[0].arguments["to"] == ["real@crm.example"]
        assert "attacker@evil.example" not in str(second.tool_calls[0].arguments)

    async def test_results_from_an_earlier_turn_are_not_reused(self) -> None:
        """Each user turn must re-query rather than answer from stale results."""
        provider = HeuristicLLMProvider()
        messages = [
            LLMMessage.user("show me leads"),
            LLMMessage.assistant(tool_calls=[LLMToolCall(id="1", name="crm__get_leads")]),
            LLMMessage.tool(
                tool_call_id="1", name="crm__get_leads", content='{"leads": [], "count": 0}'
            ),
            LLMMessage.assistant(content="No leads."),
            LLMMessage.user("show me leads"),
        ]
        response = await provider.complete(messages, tools=self.TOOLS)
        assert response.tool_calls and response.tool_calls[0].name == "crm__get_leads"

    async def test_unmatched_requests_explain_what_is_possible(self) -> None:
        response = await HeuristicLLMProvider().complete(
            [LLMMessage.user("what is the meaning of life?")], tools=self.TOOLS
        )
        assert not response.tool_calls
        assert "could not match" in (response.content or "")

    async def test_unavailable_tools_are_never_proposed(self) -> None:
        """It plans against the discovered catalog, not a hard-coded list."""
        response = await HeuristicLLMProvider().complete(
            [LLMMessage.user("what tasks are overdue?")], tools=[make_tool_spec("crm__get_leads")]
        )
        assert not response.tool_calls


class TestProviderFactory:
    def test_heuristic_is_the_credential_free_default(self, settings: Settings) -> None:
        provider = build_llm_provider(settings.model_copy(update={"llm_provider": "heuristic"}))
        assert isinstance(provider, HeuristicLLMProvider)
        assert provider.is_mock

    def test_openai_requires_a_key(self, settings: Settings) -> None:
        with pytest.raises(LLMConfigurationError):
            OpenAICompatibleProvider(
                settings.model_copy(update={"llm_provider": "openai", "llm_api_key": None})
            )

    def test_openrouter_gets_its_base_url(self, settings: Settings) -> None:
        from pydantic import SecretStr

        provider = build_llm_provider(
            settings.model_copy(
                update={"llm_provider": "openrouter", "llm_api_key": SecretStr("sk-test-key")}
            )
        )
        assert isinstance(provider, OpenAICompatibleProvider)
        assert not provider.is_mock


class TestMessageConversion:
    def test_tool_calls_and_results_render_in_wire_format(self) -> None:
        messages = [
            LLMMessage.system("be helpful"),
            LLMMessage.user("show leads"),
            LLMMessage.assistant(
                tool_calls=[
                    LLMToolCall(id="c1", name="crm__get_leads", arguments={"status": "new"})
                ]
            ),
            LLMMessage.tool(tool_call_id="c1", name="crm__get_leads", content='{"count": 0}'),
        ]
        wire = OpenAICompatibleProvider._to_wire_messages(messages)

        assert wire[0]["role"] == "system"
        assert wire[2]["tool_calls"][0]["function"]["name"] == "crm__get_leads"
        assert json.loads(wire[2]["tool_calls"][0]["function"]["arguments"]) == {"status": "new"}
        assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"count": 0}'}

    def test_assistant_content_is_null_not_empty_when_calling_tools(self) -> None:
        """Some gateways reject an empty string alongside tool calls."""
        wire = OpenAICompatibleProvider._to_wire_messages(
            [LLMMessage.assistant(content=None, tool_calls=[LLMToolCall(id="c", name="t")])]
        )
        assert wire[0]["content"] is None

    def test_roles_round_trip(self) -> None:
        assert LLMRole("assistant") is LLMRole.ASSISTANT
