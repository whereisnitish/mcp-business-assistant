"""HubSpot CRM integration.

A real implementation against the HubSpot CRM v3 Contacts API, provided to
demonstrate that the ``CRMInterface`` seam genuinely supports a third-party system
-- the MCP tools, the agent and the prompts are all unchanged when this provider is
selected via ``CRM_PROVIDER=hubspot``.

**Not covered by the automated test suite**, because doing so would require live
HubSpot credentials. It is exercised by the optional ``@pytest.mark.live`` suite,
which is deselected by default. Treat it as reviewed-but-unverified code.

API reference: https://developers.hubspot.com/docs/api/crm/contacts
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger, safe_extra
from app.integrations.base import IntegrationError, IntegrationNotConfiguredError
from app.integrations.crm.base import CRMInterface
from app.models.database.enums import LeadStatus
from app.models.schemas.domain import LeadDTO

logger = get_logger(__name__)

HUBSPOT_API_BASE = "https://api.hubapi.com"

#: HubSpot's lifecycle stages do not map one-to-one onto our pipeline, so the
#: translation is explicit in both directions rather than implied by string casing.
_STATUS_TO_HUBSPOT: dict[LeadStatus, str] = {
    LeadStatus.NEW: "lead",
    LeadStatus.CONTACTED: "marketingqualifiedlead",
    LeadStatus.QUALIFIED: "salesqualifiedlead",
    LeadStatus.UNQUALIFIED: "other",
    LeadStatus.WON: "customer",
    LeadStatus.LOST: "other",
}
_STATUS_FROM_HUBSPOT: dict[str, LeadStatus] = {
    "lead": LeadStatus.NEW,
    "subscriber": LeadStatus.NEW,
    "marketingqualifiedlead": LeadStatus.CONTACTED,
    "salesqualifiedlead": LeadStatus.QUALIFIED,
    "opportunity": LeadStatus.QUALIFIED,
    "customer": LeadStatus.WON,
    "other": LeadStatus.UNQUALIFIED,
}

_CONTACT_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "company",
    "phone",
    "hs_lead_status",
    "lifecyclestage",
    "hubspot_owner_id",
    "createdate",
    "lastmodifieddate",
]


class HubSpotCRMIntegration(CRMInterface):
    """CRM backed by the HubSpot REST API."""

    provider_name = "hubspot"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        if settings.hubspot_access_token is None:
            raise IntegrationNotConfiguredError("hubspot", "HUBSPOT_ACCESS_TOKEN is not set")
        self._token = settings.hubspot_access_token
        self._timeout = 20.0
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ http --
    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue an authenticated request and normalise every failure mode.

        The bearer token is built here and never stored on the request object we
        log, so an exception rendering cannot expose it.
        """
        client = self._client or httpx.AsyncClient(base_url=HUBSPOT_API_BASE, timeout=self._timeout)
        try:
            response = await client.request(
                method,
                path,
                headers={
                    "Authorization": f"Bearer {self._token.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                **kwargs,
            )
            if response.status_code == 401:
                raise IntegrationError(
                    "hubspot", "authentication rejected (check the access token)"
                )
            if response.status_code == 429:
                raise IntegrationError("hubspot", "rate limit exceeded; retry later")
            response.raise_for_status()
            return dict(response.json()) if response.content else {}
        except httpx.TimeoutException as exc:
            raise IntegrationError("hubspot", "request timed out") from exc
        except httpx.HTTPStatusError as exc:
            # Status code only -- the body may echo submitted contact data.
            raise IntegrationError(
                "hubspot", f"HTTP {exc.response.status_code}", details={"path": path}
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("hubspot", "network error") from exc
        finally:
            if self._owns_client:
                await client.aclose()

    # ---------------------------------------------------------------- mapping --
    @staticmethod
    def _to_dto(contact: dict[str, Any]) -> LeadDTO:
        props: dict[str, Any] = contact.get("properties") or {}
        full_name = " ".join(
            part for part in (props.get("firstname"), props.get("lastname")) if part
        ).strip()
        stage = (props.get("lifecyclestage") or "").lower()
        now = datetime.now(UTC)
        return LeadDTO(
            id=str(contact.get("id", "")),
            full_name=full_name or props.get("email") or "Unknown",
            email=props.get("email") or "",
            company=props.get("company"),
            phone=props.get("phone"),
            source="hubspot",
            status=_STATUS_FROM_HUBSPOT.get(stage, LeadStatus.NEW),
            score=0,
            estimated_value=None,
            notes=None,
            created_at=_parse_ts(props.get("createdate")) or now,
            updated_at=_parse_ts(props.get("lastmodifieddate")) or now,
        )

    # ------------------------------------------------------------- operations --
    async def health_check(self) -> bool:
        await self._request("GET", "/crm/v3/objects/contacts", params={"limit": 1})
        return True

    async def get_leads(
        self,
        *,
        status: LeadStatus | None = None,
        source: str | None = None,
        company: str | None = None,
        min_score: int | None = None,
        limit: int = 25,
    ) -> list[LeadDTO]:
        filters: list[dict[str, Any]] = []
        if status is not None:
            filters.append(
                {
                    "propertyName": "lifecyclestage",
                    "operator": "EQ",
                    "value": _STATUS_TO_HUBSPOT[status],
                }
            )
        if company:
            filters.append(
                {"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": company}
            )

        if filters:
            payload = {
                "filterGroups": [{"filters": filters}],
                "properties": _CONTACT_PROPERTIES,
                "limit": min(limit, 100),
            }
            data = await self._request("POST", "/crm/v3/objects/contacts/search", json=payload)
        else:
            data = await self._request(
                "GET",
                "/crm/v3/objects/contacts",
                params={"limit": min(limit, 100), "properties": ",".join(_CONTACT_PROPERTIES)},
            )
        return [self._to_dto(item) for item in data.get("results", [])]

    async def get_lead(self, lead_id: str) -> LeadDTO | None:
        try:
            data = await self._request(
                "GET",
                f"/crm/v3/objects/contacts/{lead_id}",
                params={"properties": ",".join(_CONTACT_PROPERTIES)},
            )
        except IntegrationError as exc:
            if exc.details.get("path", "").endswith(lead_id) and "404" in str(exc):
                return None
            raise
        return self._to_dto(data) if data else None

    async def search_leads(self, query: str, *, limit: int = 25) -> list[LeadDTO]:
        payload = {"query": query, "properties": _CONTACT_PROPERTIES, "limit": min(limit, 100)}
        data = await self._request("POST", "/crm/v3/objects/contacts/search", json=payload)
        return [self._to_dto(item) for item in data.get("results", [])]

    async def create_lead(
        self,
        *,
        full_name: str,
        email: str,
        company: str | None = None,
        phone: str | None = None,
        source: str | None = None,
        status: LeadStatus = LeadStatus.NEW,
        score: int = 0,
        estimated_value: int | None = None,
        notes: str | None = None,
    ) -> LeadDTO:
        first, _, last = full_name.strip().partition(" ")
        properties = {
            "firstname": first,
            "lastname": last or first,
            "email": email,
            "company": company,
            "phone": phone,
            "lifecyclestage": _STATUS_TO_HUBSPOT[status],
        }
        data = await self._request(
            "POST",
            "/crm/v3/objects/contacts",
            json={"properties": {k: v for k, v in properties.items() if v is not None}},
        )
        logger.info("hubspot contact created", extra=safe_extra({"event": "crm.hubspot_created"}))
        return self._to_dto(data)

    async def update_lead_status(
        self, lead_id: str, status: LeadStatus, *, note: str | None = None
    ) -> LeadDTO | None:
        data = await self._request(
            "PATCH",
            f"/crm/v3/objects/contacts/{lead_id}",
            json={"properties": {"lifecyclestage": _STATUS_TO_HUBSPOT[status]}},
        )
        return self._to_dto(data) if data else None


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a HubSpot ISO-8601 timestamp, tolerating a trailing ``Z``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
