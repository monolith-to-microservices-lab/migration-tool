"""Test fixtures: in-memory legacy DB, in-memory state store, fake HTTP services."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from migration_tool.clients import SalesServiceClient, UserServiceClient
from migration_tool.config import Settings
from migration_tool.http import RetryingClient
from migration_tool.legacy import LegacyBase, LegacySaleRow, LegacySource, LegacyUserRow
from migration_tool.runtime import Runtime
from migration_tool.state import StateStore

DT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Legacy DB (SQLite, in-memory)
# --------------------------------------------------------------------------- #

@pytest.fixture
def legacy_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    LegacyBase.metadata.create_all(engine)
    return engine


@pytest.fixture
def seed_legacy(legacy_engine):
    def _seed(users: list[dict], sales: list[dict]) -> None:
        with Session(legacy_engine) as s:
            for u in users:
                s.add(LegacyUserRow(id=u["id"], name=u["name"],
                                    created_at=u.get("created_at", DT)))
            for sale in sales:
                s.add(LegacySaleRow(
                    id=sale["id"], user_id=sale["user_id"], item_name=sale["item_name"],
                    quantity=sale["quantity"], created_at=sale.get("created_at", DT)))
            s.commit()
    return _seed


@pytest.fixture
def legacy_source(legacy_engine):
    return LegacySource(legacy_engine, batch_size=2)


# --------------------------------------------------------------------------- #
# Fake services
# --------------------------------------------------------------------------- #

class FakeService:
    """Minimal in-memory stand-in for the User or Sales service."""

    def __init__(self, entity: str, call_log: list):
        self.entity = entity  # "user" | "sale"
        self.store: dict[int, dict] = {}
        self.call_log = call_log
        self.transient_left: dict[str, int] = {}   # path-prefix -> remaining 503s
        self.hard_500: set[str] = set()            # exact paths that always 500
        self.import_path = f"/internal/{entity}s/import"
        self.collection = f"/{entity}s"

    def _key_fields(self) -> list[str]:
        return ["name"] if self.entity == "user" else ["user_id", "item_name", "quantity"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.call_log.append((self.entity, method, path))

        if path in self.hard_500:
            return httpx.Response(500, json={"detail": "boom"})
        for prefix, left in list(self.transient_left.items()):
            if path.startswith(prefix) and left > 0:
                self.transient_left[prefix] = left - 1
                return httpx.Response(503, json={"detail": "temporarily unavailable"})

        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == self.import_path:
            return self._import(request)
        if method == "GET" and path.startswith(self.collection + "/"):
            return self._get(int(path.rsplit("/", 1)[1]))
        if method == "DELETE" and path.startswith(self.collection + "/"):
            return self._delete(int(path.rsplit("/", 1)[1]))
        return httpx.Response(404, json={"detail": "not found"})

    def _record_key(self) -> str:
        return "user" if self.entity == "user" else "sale"

    def _import(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        rid = body["id"]
        incoming = {k: body[k] for k in self._key_fields()}
        incoming["created_at"] = body["created_at"]
        existing = self.store.get(rid)
        outcome_field = "status" if self.entity == "user" else "outcome"
        if existing is None:
            self.store[rid] = {"id": rid, **incoming}
            return httpx.Response(
                201, json={outcome_field: "created", self._record_key(): self.store[rid]}
            )
        same = all(str(existing[k]) == str(incoming[k]) for k in self._key_fields()) and (
            _dt(existing["created_at"]) == _dt(incoming["created_at"])
        )
        if same:
            return httpx.Response(
                200, json={outcome_field: "unchanged", self._record_key(): existing}
            )
        return httpx.Response(409, json={"detail": "import conflict", "sale_id": rid,
                                         "conflicts": {"incoming": incoming, "existing": existing}})

    def _get(self, rid: int) -> httpx.Response:
        rec = self.store.get(rid)
        if rec is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=rec)

    def _delete(self, rid: int) -> httpx.Response:
        if rid in self.store:
            del self.store[rid]
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})


def _dt(v) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:  # services treat naive timestamps as UTC
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


@pytest.fixture
def call_log() -> list:
    return []


@pytest.fixture
def fake_user(call_log):
    return FakeService("user", call_log)


@pytest.fixture
def fake_sale(call_log):
    return FakeService("sale", call_log)


# --------------------------------------------------------------------------- #
# Runtime wired to fakes
# --------------------------------------------------------------------------- #

@pytest.fixture
def state_store(tmp_path):
    return StateStore.from_url(f"sqlite:///{tmp_path / 'state.db'}")


def pytest_collection_modifyitems(config, items):
    """Auto-mark by directory instead of hand-annotating every test file:
    tests/integration/** -> integration, everything else here -> unit (this
    whole top-level suite is SQLite-in-memory + httpx.MockTransport, see the
    fixtures above - genuinely fast/isolated).
    """
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)


@pytest.fixture
def make_runtime(legacy_source, state_store, fake_user, fake_sale, tmp_path):
    def _make(**overrides) -> Runtime:
        settings = Settings(
            _env_file=None,
            legacy_database_url="sqlite://",
            user_service_url="http://user.test",
            sales_service_url="http://sales.test",
            migration_state_database_url="sqlite://",
            report_dir=str(tmp_path / "reports"),
            http_max_retries=overrides.get("http_max_retries", 3),
            http_backoff_base_seconds=0.0,
            allow_destructive_rollback=overrides.get("allow_destructive_rollback", False),
        )
        user_http = RetryingClient(
            "http://user.test", transport=httpx.MockTransport(fake_user.handler),
            max_retries=settings.http_max_retries, backoff_base=0.0, sleep=lambda _s: None,
        )
        sales_http = RetryingClient(
            "http://sales.test", transport=httpx.MockTransport(fake_sale.handler),
            max_retries=settings.http_max_retries, backoff_base=0.0, sleep=lambda _s: None,
        )
        return Runtime(
            settings=settings,
            legacy=legacy_source,
            state=state_store,
            user_client=UserServiceClient(user_http),
            sales_client=SalesServiceClient(sales_http),
            _user_http=user_http,
            _sales_http=sales_http,
        )
    return _make
