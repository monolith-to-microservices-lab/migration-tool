"""Thin client for the User Service HTTP API.

The migration-tool NEVER touches User PostgreSQL directly - every read and write
goes through this service.
"""

from __future__ import annotations

from ..http import HttpError, RetryingClient
from ..models import ImportAction, ImportResult, UserImportPayload

DESTINATION = "user-service"


class UserServiceClient:
    def __init__(self, client: RetryingClient) -> None:
        self._c = client

    # -- connectivity -------------------------------------------------
    def health(self) -> bool:
        try:
            r = self._c.request("GET", "/health")
        except HttpError:
            return False
        return r.status_code == 200

    # -- write (import only) --------------------------------------
    def import_user(self, payload: UserImportPayload) -> ImportResult:
        body = payload.model_dump(mode="json")
        r = self._c.request("POST", "/internal/users/import", json=body)
        if r.status_code == 201:
            return ImportResult(action=ImportAction.CREATED, http_status=201, record=r.json().get("user"))
        if r.status_code == 200:
            return ImportResult(action=ImportAction.UNCHANGED, http_status=200, record=r.json().get("user"))
        if r.status_code == 409:
            return ImportResult(action=ImportAction.CONFLICT, http_status=409, detail=_safe_json(r))
        if r.status_code == 422:
            return ImportResult(action=ImportAction.FAILED, http_status=422, detail=_safe_json(r))
        # Any other status escaped the retry policy -> treat as a hard failure.
        raise HttpError(
            f"unexpected status {r.status_code} importing user {payload.id}: {r.text[:300]}",
            attempts=1,
            last_status=r.status_code,
        )

    # -- read ------------------------------------------------------
    def get_user(self, user_id: int) -> dict | None:
        r = self._c.request("GET", f"/users/{user_id}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        raise HttpError(
            f"unexpected status {r.status_code} reading user {user_id}",
            attempts=1,
            last_status=r.status_code,
        )

    # -- delete (rollback only) ----------------------------------
    def delete_user(self, user_id: int) -> bool:
        """True if the row was deleted, False if it was already absent (404)."""
        r = self._c.request("DELETE", f"/users/{user_id}")
        if r.status_code in (200, 204):
            return True
        if r.status_code == 404:
            return False
        raise HttpError(
            f"unexpected status {r.status_code} deleting user {user_id}",
            attempts=1,
            last_status=r.status_code,
        )


def _safe_json(r) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"raw": r.text[:500]}
