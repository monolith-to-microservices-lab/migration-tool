"""Thin client for the Sales Service HTTP API.

The migration-tool NEVER touches Sales PostgreSQL directly - every read and write
goes through this service.
"""

from __future__ import annotations

from ..http import HttpError, RetryingClient
from ..models import ImportAction, ImportResult, SaleImportPayload

DESTINATION = "sales-service"


class SalesServiceClient:
    def __init__(self, client: RetryingClient) -> None:
        self._c = client

    def health(self) -> bool:
        try:
            r = self._c.request("GET", "/health")
        except HttpError:
            return False
        return r.status_code == 200

    def import_sale(self, payload: SaleImportPayload) -> ImportResult:
        body = payload.model_dump(mode="json")
        r = self._c.request("POST", "/internal/sales/import", json=body)
        if r.status_code == 201:
            return ImportResult(
                action=ImportAction.CREATED, http_status=201, record=r.json().get("sale")
            )
        if r.status_code == 200:
            return ImportResult(
                action=ImportAction.UNCHANGED, http_status=200, record=r.json().get("sale")
            )
        if r.status_code == 409:
            return ImportResult(action=ImportAction.CONFLICT, http_status=409, detail=_safe_json(r))
        if r.status_code == 422:
            return ImportResult(action=ImportAction.FAILED, http_status=422, detail=_safe_json(r))
        raise HttpError(
            f"unexpected status {r.status_code} importing sale {payload.id}: {r.text[:300]}",
            attempts=1,
            last_status=r.status_code,
        )

    def get_sale(self, sale_id: int) -> dict | None:
        r = self._c.request("GET", f"/sales/{sale_id}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        raise HttpError(
            f"unexpected status {r.status_code} reading sale {sale_id}",
            attempts=1,
            last_status=r.status_code,
        )

    def delete_sale(self, sale_id: int) -> bool:
        r = self._c.request("DELETE", f"/sales/{sale_id}")
        if r.status_code in (200, 204):
            return True
        if r.status_code == 404:
            return False
        raise HttpError(
            f"unexpected status {r.status_code} deleting sale {sale_id}",
            attempts=1,
            last_status=r.status_code,
        )


def _safe_json(r) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"raw": r.text[:500]}
