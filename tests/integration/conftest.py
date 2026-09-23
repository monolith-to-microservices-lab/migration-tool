"""Integration suite - runs only against the real services.

Deselected by default (its directory is not in `testpaths`). Run explicitly:

    pytest tests/integration -q

It is skipped automatically unless the legacy DB and both services are reachable
and the env vars are set (LEGACY_DATABASE_URL, USER_SERVICE_URL, SALES_SERVICE_URL).

NOTE: it calls the real import + delete endpoints. Point it at a lab environment.
"""

from __future__ import annotations

import os

import httpx
import pytest

from migration_tool.config import Settings
from migration_tool.runtime import build_runtime


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}/health", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    s = Settings()
    if not (_reachable(s.user_service_url) and _reachable(s.sales_service_url)):
        pytest.skip("User/Sales services not reachable")
    return s


@pytest.fixture
def live_runtime(live_settings, tmp_path):
    live_settings.migration_state_database_url = f"sqlite:///{tmp_path / 'state.db'}"
    live_settings.report_dir = str(tmp_path / "reports")
    live_settings.allow_destructive_rollback = True
    rt = build_runtime(live_settings)
    try:
        rt.legacy.ping()
    except Exception:  # noqa: BLE001
        rt.close()
        pytest.skip("legacy database not reachable")
    yield rt
    rt.close()
