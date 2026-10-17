"""Client for the Messy-Order-to-ERP agent's human-review endpoints.

A merchant admin's release or rejection of a draft sales order is relayed here; the
agent re-validates the draft and starts the ERP Bridge + Safety Gate workflow.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OrderDeskError(RuntimeError):
    """The agent is unreachable or failed (HTTP 502)."""


class OrderDeskRejected(ValueError):
    """The agent refused the transition; `status` is its HTTP status (404/409/422)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class OrderDeskClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise OrderDeskError("order desk is unavailable") from exc
        if response.status_code in (404, 409, 422):
            try:
                message = str(response.json().get("error", "rejected"))[:300]
            except ValueError:
                message = "rejected"
            raise OrderDeskRejected(response.status_code, message)
        if response.status_code != 200:
            logger.warning("order desk returned HTTP %s", response.status_code)
            raise OrderDeskError("order desk failed")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise OrderDeskError("order desk returned an invalid payload") from exc

    async def release(
        self,
        intake_id: str,
        *,
        merchant_id: str,
        approved_by: str,
        line_overrides: list[dict[str, Any]],
        customer_ref: str | None,
    ) -> dict[str, Any]:
        return await self._post(
            f"/v1/drafts/{intake_id}/release",
            {
                "merchant_id": merchant_id,
                "approved_by": approved_by,
                "line_overrides": line_overrides,
                "customer_ref": customer_ref,
            },
        )

    async def reject(
        self, intake_id: str, *, merchant_id: str, rejected_by: str, reason: str
    ) -> dict[str, Any]:
        return await self._post(
            f"/v1/drafts/{intake_id}/reject",
            {"merchant_id": merchant_id, "rejected_by": rejected_by, "reason": reason},
        )
