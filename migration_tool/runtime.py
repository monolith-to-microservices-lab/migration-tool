"""Wires config -> legacy source + service clients + state store into one object."""

from __future__ import annotations

from dataclasses import dataclass

from .clients import SalesServiceClient, UserServiceClient
from .config import Settings, get_settings
from .http import RetryingClient
from .legacy import LegacySource
from .state import StateStore


@dataclass
class Runtime:
    settings: Settings
    legacy: LegacySource
    state: StateStore
    user_client: UserServiceClient
    sales_client: SalesServiceClient
    _user_http: RetryingClient
    _sales_http: RetryingClient

    def close(self) -> None:
        self._user_http.close()
        self._sales_http.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def build_runtime(settings: Settings | None = None) -> Runtime:
    s = settings or get_settings()

    legacy = LegacySource.from_url(s.legacy_database_url, batch_size=s.batch_size)
    state = StateStore.from_url(s.migration_state_database_url)

    user_http = RetryingClient(
        s.user_service_url,
        timeout=s.http_timeout_seconds,
        max_retries=s.http_max_retries,
        backoff_base=s.http_backoff_base_seconds,
    )
    sales_http = RetryingClient(
        s.sales_service_url,
        timeout=s.http_timeout_seconds,
        max_retries=s.http_max_retries,
        backoff_base=s.http_backoff_base_seconds,
    )
    return Runtime(
        settings=s,
        legacy=legacy,
        state=state,
        user_client=UserServiceClient(user_http),
        sales_client=SalesServiceClient(sales_http),
        _user_http=user_http,
        _sales_http=sales_http,
    )
