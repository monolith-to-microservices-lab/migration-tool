"""Domain models, enums and small comparison helpers shared across the tool."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Sub-second clock skew tolerated when comparing timestamps. Matches the
# tolerance the User / Sales services themselves use on import.
CREATED_AT_TOLERANCE_SECONDS = 1.0


class RunStatus(StrEnum):
    STARTED = "STARTED"
    USERS_MIGRATED = "USERS_MIGRATED"
    USERS_VALIDATED = "USERS_VALIDATED"
    SALES_MIGRATED = "SALES_MIGRATED"
    SALES_VALIDATED = "SALES_VALIDATED"
    VALIDATED = "VALIDATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class EntityType(StrEnum):
    USER = "user"
    SALE = "sale"


class ImportAction(StrEnum):
    CREATED = "created"      # this run inserted the row -> owned by this run
    UNCHANGED = "unchanged"  # row already existed identically -> NOT owned
    CONFLICT = "conflict"    # id exists with different data (HTTP 409)
    FAILED = "failed"        # transport / server error after retries


class ItemStatus(StrEnum):
    IMPORTED = "imported"
    VALIDATED = "validated"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_CONFLICT = "rollback_conflict"
    ROLLBACK_FAILED = "rollback_failed"


# --------------------------------------------------------------------------- #
# Legacy rows
# --------------------------------------------------------------------------- #

class LegacyUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime


class LegacySale(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    item_name: str
    quantity: int
    created_at: datetime


# --------------------------------------------------------------------------- #
# Import payloads (exactly what we send to the services)
# --------------------------------------------------------------------------- #

class UserImportPayload(BaseModel):
    id: int
    name: str
    created_at: datetime


class SaleImportPayload(BaseModel):
    id: int
    user_id: int
    item_name: str
    quantity: int
    created_at: datetime


class ImportResult(BaseModel):
    action: ImportAction
    http_status: int
    record: dict | None = None
    detail: dict | None = None  # conflict / error body


# --------------------------------------------------------------------------- #
# Validation output
# --------------------------------------------------------------------------- #

class Divergence(BaseModel):
    entity: EntityType
    legacy_id: int
    field: str
    legacy_value: str
    destination_value: str


class OrphanSale(BaseModel):
    sale_id: int
    user_id: int
    reason: str = "USER_NOT_FOUND"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def datetimes_equal(a: datetime, b: datetime) -> bool:
    return abs((to_utc(a) - to_utc(b)).total_seconds()) <= CREATED_AT_TOLERANCE_SECONDS


def canonical_json(payload: dict) -> str:
    """Deterministic JSON used for the stored rollback snapshot + its hash."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class MigrationStats(BaseModel):
    users_found: int = 0
    users_created: int = 0
    users_unchanged: int = 0
    users_conflict: int = 0
    users_failed: int = 0
    users_validated: int = 0

    sales_found: int = 0
    sales_created: int = 0
    sales_unchanged: int = 0
    sales_conflict: int = 0
    sales_failed: int = 0
    sales_validated: int = 0

    http_failures: int = 0

    checked_sales_refs: int = 0
    orphan_sales: int = 0

    def add_import(self, entity: EntityType, action: ImportAction) -> None:
        prefix = "users" if entity is EntityType.USER else "sales"
        field = f"{prefix}_{action.value}"
        setattr(self, field, getattr(self, field) + 1)


class RollbackItemResult(BaseModel):
    entity: EntityType
    legacy_id: int
    outcome: str  # deleted | already_absent | conflict | failed | would_delete
    detail: str = ""


class RollbackReport(BaseModel):
    run_id: str
    status: RunStatus
    dry_run: bool = False
    confirmed: bool = False
    sales_deleted: int = 0
    sales_already_absent: int = 0
    users_deleted: int = 0
    users_already_absent: int = 0
    conflicts: int = 0
    failures: int = 0
    would_delete_sales: int = 0
    would_delete_users: int = 0
    items: list[RollbackItemResult] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class RunReport(BaseModel):
    run_id: str
    status: RunStatus
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    legacy_source: str = ""
    stats: MigrationStats = Field(default_factory=MigrationStats)
    divergences: list[Divergence] = Field(default_factory=list)
    orphan_sales: list[OrphanSale] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
