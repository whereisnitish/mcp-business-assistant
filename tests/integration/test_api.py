"""API endpoint tests, exercised through the real ASGI stack.

Requests go through routing, dependency injection, validation, the agent, the
policy engine and the error handlers -- only the language model is scripted.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.database.enums import ApprovalStatus
from app.providers.llm.fake_provider import FakeLLMProvider, text_response, tool_call_response
from app.repositories.lead_repository import LeadRepository

pytestmark = pytest.mark.integration


class TestHealth:
    async def test_health_reports_dependency_state(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/health")
        body = response.json()

        assert response.status_code == 200
        assert body["status"] == "ok"
        assert body["database"] is True
        assert body["mcp"]["tool_count"] == 17
        assert body["mcp"]["degraded"] is False

    async def test_health_flags_a_mock_llm_provider(self, api_client: AsyncClient) -> None:
        """A reviewer must be able to tell a real model from a stand-in."""
        assert (await api_client.get("/health")).json()["llm_is_mock"] is True

    async def test_liveness_is_cheap_and_separate(self, api_client: AsyncClient) -> None:
        assert (await api_client.get("/health/live")).json() == {"status": "alive"}

    async def test_every_response_carries_a_request_id(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/health")
        assert response.headers["X-Request-ID"]
        assert float(response.headers["X-Response-Time-ms"]) >= 0

    async def test_an_inbound_request_id_is_honoured(self, api_client: AsyncClient) -> None:
        """Lets a trace span an upstream gateway."""
        response = await api_client.get("/health", headers={"X-Request-ID": "trace-abc"})
        assert response.headers["X-Request-ID"] == "trace-abc"


class TestToolsEndpoint:
    async def test_lists_the_dynamically_discovered_catalog(self, api_client: AsyncClient) -> None:
        body = (await api_client.get("/api/v1/tools")).json()

        assert body["count"] == 17
        assert set(body["servers"]) == {"crm", "tasks", "spreadsheets", "email", "calendar"}
        assert body["counts_by_permission"]["high_risk"] == 2

    async def test_marks_which_tools_need_approval(self, api_client: AsyncClient) -> None:
        tools = (await api_client.get("/api/v1/tools")).json()["tools"]
        gated = {tool["name"] for tool in tools if tool["requires_approval"]}
        assert gated == {"email__send_email", "calendar__create_event"}

    async def test_can_filter_by_permission(self, api_client: AsyncClient) -> None:
        body = (await api_client.get("/api/v1/tools", params={"permission": "read"})).json()
        assert body["count"] == 10
        assert all(tool["permission"] == "read" for tool in body["tools"])

    async def test_publishes_input_schemas(self, api_client: AsyncClient) -> None:
        tools = (await api_client.get("/api/v1/tools")).json()["tools"]
        create_lead = next(tool for tool in tools if tool["name"] == "crm__create_lead")
        assert "full_name" in create_lead["input_schema"]["properties"]

    async def test_permissions_endpoint_documents_the_model(self, api_client: AsyncClient) -> None:
        body = (await api_client.get("/api/v1/tools/permissions")).json()
        assert body["default_for_unregistered_tools"] == "high_risk"
        assert "email__send_email" in body["levels"]["high_risk"]["tools"]


class TestChat:
    async def test_a_read_request_returns_an_answer(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        fake_llm._script.extend(
            [tool_call_response("crm__get_leads", {}), text_response("No leads yet.")]
        )
        response = await api_client.post("/api/v1/chat", json={"message": "show me leads"})
        body = response.json()

        assert response.status_code == 200
        assert body["status"] == "completed"
        assert body["approval_required"] is False
        assert body["tools_called"] == ["crm__get_leads"]
        assert body["response"] == "No leads yet."

    async def test_conversation_continues_across_requests(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        fake_llm._script.extend([text_response("First."), text_response("Second.")])

        first = (await api_client.post("/api/v1/chat", json={"message": "hello"})).json()
        second = (
            await api_client.post(
                "/api/v1/chat",
                json={"message": "again", "conversation_id": first["conversation_id"]},
            )
        ).json()

        assert second["conversation_id"] == first["conversation_id"]

    async def test_an_empty_message_is_rejected(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/api/v1/chat", json={"message": ""})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_a_validation_error_does_not_echo_the_payload(
        self, api_client: AsyncClient
    ) -> None:
        """A rejected body may hold exactly the sensitive data that made it invalid."""
        response = await api_client.post(
            "/api/v1/chat", json={"message": "", "secret_field": "sk-do-not-echo-me"}
        )
        assert "sk-do-not-echo-me" not in response.text


class TestApprovalWorkflow:
    async def test_high_risk_request_returns_approval_required_and_sends_nothing(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider, settings: Settings
    ) -> None:
        fake_llm._script.append(
            tool_call_response(
                "email__send_email",
                {"to": ["ada@example.com"], "subject": "Not yet", "body": "Hello"},
            )
        )
        fake_llm._default = text_response("Waiting for approval.")

        body = (await api_client.post("/api/v1/chat", json={"message": "email Ada"})).json()

        assert body["status"] == "awaiting_approval"
        assert body["approval_required"] is True
        assert body["approval"]["tool_name"] == "email__send_email"
        assert body["approval"]["arguments"]["subject"] == "Not yet"

        outbox = settings.email_outbox_dir / "outbox.jsonl"
        assert not outbox.exists() or "Not yet" not in outbox.read_text(encoding="utf-8")

    async def test_confirming_executes_the_action(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider, settings: Settings
    ) -> None:
        fake_llm._script.append(
            tool_call_response(
                "email__send_email",
                {"to": ["ada@example.com"], "subject": "Confirmed send", "body": "Hello"},
            )
        )
        fake_llm._default = text_response("Sent.")

        chat = (await api_client.post("/api/v1/chat", json={"message": "email Ada"})).json()
        approval_id = chat["approval"]["approval_id"]

        response = await api_client.post(
            f"/api/v1/approvals/{approval_id}/confirm", json={"note": "checked"}
        )
        body = response.json()

        assert response.status_code == 200
        assert body["executed"] is True
        assert body["approval"]["status"] == ApprovalStatus.EXECUTED.value
        assert "Confirmed send" in (settings.email_outbox_dir / "outbox.jsonl").read_text(
            encoding="utf-8"
        )

    async def test_confirming_twice_is_refused(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        fake_llm._script.append(
            tool_call_response(
                "email__send_email", {"to": ["a@example.com"], "subject": "Once", "body": "Hi"}
            )
        )
        fake_llm._default = text_response("ok")

        chat = (await api_client.post("/api/v1/chat", json={"message": "email"})).json()
        approval_id = chat["approval"]["approval_id"]

        assert (
            await api_client.post(f"/api/v1/approvals/{approval_id}/confirm", json={})
        ).status_code == 200
        replay = await api_client.post(f"/api/v1/approvals/{approval_id}/confirm", json={})

        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "approval_invalid_state"

    async def test_rejecting_prevents_execution(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider, settings: Settings
    ) -> None:
        fake_llm._script.append(
            tool_call_response(
                "email__send_email",
                {"to": ["a@example.com"], "subject": "Rejected send", "body": "Hi"},
            )
        )
        fake_llm._default = text_response("ok")

        chat = (await api_client.post("/api/v1/chat", json={"message": "email"})).json()
        approval_id = chat["approval"]["approval_id"]

        body = (
            await api_client.post(f"/api/v1/approvals/{approval_id}/reject", json={"note": "no"})
        ).json()

        assert body["approval"]["status"] == ApprovalStatus.REJECTED.value
        assert body["executed"] is False
        outbox = settings.email_outbox_dir / "outbox.jsonl"
        assert not outbox.exists() or "Rejected send" not in outbox.read_text(encoding="utf-8")

    async def test_pending_approvals_are_listable(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        fake_llm._script.append(
            tool_call_response(
                "email__send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"}
            )
        )
        fake_llm._default = text_response("ok")
        await api_client.post("/api/v1/chat", json={"message": "email"})

        body = (await api_client.get("/api/v1/approvals")).json()
        assert body["count"] == 1
        assert body["approvals"][0]["status"] == ApprovalStatus.PENDING.value

    async def test_unknown_approval_returns_404(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            "/api/v1/approvals/00000000-0000-0000-0000-000000000000/confirm", json={}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestConversations:
    async def test_transcript_is_retrievable(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        fake_llm._script.extend(
            [tool_call_response("crm__get_leads", {}), text_response("Nothing found.")]
        )
        chat = (await api_client.post("/api/v1/chat", json={"message": "leads?"})).json()

        body = (await api_client.get(f"/api/v1/conversations/{chat['conversation_id']}")).json()

        assert body["message_count"] == 4
        assert [message["role"] for message in body["messages"]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]

    async def test_unknown_conversation_returns_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get(
            "/api/v1/conversations/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    async def test_conversations_are_listable(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        fake_llm._default = text_response("hi")
        await api_client.post("/api/v1/chat", json={"message": "one"})
        await api_client.post("/api/v1/chat", json={"message": "two"})

        body = (await api_client.get("/api/v1/conversations")).json()
        assert body["count"] == 2


class TestErrorContract:
    async def test_unknown_route_returns_the_standard_error_shape(
        self, api_client: AsyncClient
    ) -> None:
        response = await api_client.get("/api/v1/does-not-exist")
        body = response.json()
        assert response.status_code == 404
        assert set(body) == {"error", "request_id"}
        assert body["error"]["code"] == "http_404"

    async def test_internal_errors_do_not_leak_details(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider
    ) -> None:
        """An unexpected crash must not return a traceback or internal text.

        The agent runner converts an unexpected exception into a deliberate,
        client-safe ``AgentError``, so the response carries a generic message and a
        request id for correlation -- and none of the original exception text, which
        here contains a database password.
        """
        fake_llm._raise_on_call = RuntimeError("connection string postgresql://user:hunter2@db/app")
        response = await api_client.post("/api/v1/chat", json={"message": "boom"})
        body = response.json()

        assert response.status_code == 500
        assert "hunter2" not in response.text
        assert "postgresql" not in response.text
        assert "RuntimeError" not in response.text
        assert body["error"]["code"] == "agent_error"
        assert body["error"]["message"] == "The assistant failed to process that request."
        assert body["request_id"]


class TestOpenAPI:
    async def test_schema_documents_every_public_route(self, api_client: AsyncClient) -> None:
        paths = (await api_client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/chat" in paths
        assert "/api/v1/approvals/{approval_id}/confirm" in paths
        assert "/api/v1/conversations/{conversation_id}" in paths
        assert "/api/v1/tools" in paths
        assert "/health" in paths


class TestBusinessData:
    async def test_a_write_through_chat_reaches_the_database(
        self, api_client: AsyncClient, fake_llm: FakeLLMProvider, session: AsyncSession
    ) -> None:
        """End-to-end proof that the stack really performs business work."""
        fake_llm._script.extend(
            [
                tool_call_response(
                    "crm__create_lead",
                    {"full_name": "Grace Hopper", "email": "grace@example.com", "company": "Navy"},
                ),
                text_response("Added Grace Hopper."),
            ]
        )
        await api_client.post("/api/v1/chat", json={"message": "add Grace"})

        lead = await LeadRepository(session).get_by_email("grace@example.com")
        assert lead is not None
        assert lead.company == "Navy"
