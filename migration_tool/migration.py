"""Snapshot orchestration: the initial, one-shot legacy -> services migration.

Order is fixed and enforced:

    Users -> validate Users -> Sales -> validate Sales -> referential integrity

If a phase fails the run stops there (status FAILED) and later phases do not run.
Every row this run *creates* is recorded in the state store together with the
exact payload that was sent, so `rollback` can act precisely.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeGuard

from .http import HttpError
from .logging_config import get_logger
from .models import (
    EntityType,
    ImportAction,
    ItemStatus,
    MigrationStats,
    RunReport,
    RunStatus,
    SaleImportPayload,
    UserImportPayload,
    canonical_json,
    payload_hash,
)
from .report import build_envelope, write_files
from .runtime import Runtime
from .validation import check_referential_integrity, validate_sales, validate_users

if TYPE_CHECKING:
    from .state import MigrationItem

logger = get_logger("migration_tool.migration")

# A FAILED run is intentionally resumable - imports are idempotent, so `resume`
# re-drives it from its last recorded state. COMPLETED / rolled-back runs are not.
_RESUME_BLOCKED = {
    RunStatus.COMPLETED,
    RunStatus.ROLLED_BACK,
    RunStatus.ROLLBACK_FAILED,
}


class SnapshotError(RuntimeError):
    def __init__(self, message: str, report: RunReport) -> None:
        super().__init__(message)
        self.report = report


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"mig_{now:%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def run_dry(rt: Runtime) -> RunReport:
    """Read-only preflight: counts + basic problem detection. No writes, no state."""
    started = datetime.now(UTC)
    stats = MigrationStats()
    reasons: list[str] = []

    rt.legacy.ping()
    stats.users_found = rt.legacy.count_users()
    stats.sales_found = rt.legacy.count_sales()

    if not rt.user_client.health():
        reasons.append(f"User Service unreachable at {rt.settings.user_service_url}")
    if not rt.sales_client.health():
        reasons.append(f"Sales Service unreachable at {rt.settings.sales_service_url}")

    legacy_orphans = rt.legacy.legacy_orphan_sales()
    if legacy_orphans:
        reasons.append(
            f"{len(legacy_orphans)} legacy sale(s) reference a missing legacy user "
            f"(e.g. sale {legacy_orphans[0][0]} -> user {legacy_orphans[0][1]})"
        )

    status = RunStatus.COMPLETED if not reasons else RunStatus.FAILED
    report = RunReport(
        run_id="(dry-run)",
        status=status,
        dry_run=True,
        started_at=started,
        finished_at=datetime.now(UTC),
        legacy_source=rt.settings.masked_legacy_url(),
        stats=stats,
        reasons=reasons,
    )
    logger.info(
        "snapshot.dry_run",
        extra={"users": stats.users_found, "sales": stats.sales_found, "result": status.value},
    )
    return report


# --------------------------------------------------------------------------- #
# Snapshot / resume
# --------------------------------------------------------------------------- #


def run_snapshot(rt: Runtime, *, resume_run_id: str | None = None) -> RunReport:
    if resume_run_id is not None:
        run = rt.state.get_run(resume_run_id)
        if run is None:
            raise KeyError(f"unknown run_id: {resume_run_id}")
        if RunStatus(run.status) in _RESUME_BLOCKED:
            raise ValueError(f"run {resume_run_id} is already {run.status}; nothing to resume")
        run_id = run.run_id
        started_at = run.started_at
        logger.info("snapshot.resume", extra={"run_id": run_id, "from_status": run.status})
    else:
        run_id = new_run_id()
        run = rt.state.create_run(
            run_id, mode="snapshot", legacy_source=rt.settings.masked_legacy_url()
        )
        started_at = run.started_at
        logger.info("snapshot.start", extra={"run_id": run_id})

    stats = MigrationStats()
    report = RunReport(
        run_id=run_id,
        status=RunStatus.STARTED,
        started_at=started_at,
        legacy_source=rt.settings.masked_legacy_url(),
        stats=stats,
    )

    try:
        # -- Phase 1: migrate Users ------------------------------------
        _migrate_users(rt, run_id, stats)
        _persist(rt, run_id, RunStatus.USERS_MIGRATED, report)
        if stats.users_conflict or stats.users_failed:
            report.reasons.append(
                f"{stats.users_conflict} conflicting user(s), "
                f"{stats.users_failed} failed user import(s)"
            )
            return _finalize(rt, run_id, RunStatus.FAILED, report)

        # -- Phase 2: validate Users ---------------------------------
        divs, reasons = validate_users(rt, run_id, stats)
        report.divergences += divs
        report.reasons += reasons
        if divs or reasons:
            report.reasons.append("MIGRATION VALIDATION FAILED (users); Sales phase skipped")
            _persist(rt, run_id, RunStatus.FAILED, report)
            return _finalize(rt, run_id, RunStatus.FAILED, report)
        _persist(rt, run_id, RunStatus.USERS_VALIDATED, report)

        # -- Phase 3: migrate Sales --------------------------------
        _migrate_sales(rt, run_id, stats)
        _persist(rt, run_id, RunStatus.SALES_MIGRATED, report)
        if stats.sales_conflict or stats.sales_failed:
            report.reasons.append(
                f"{stats.sales_conflict} conflicting sale(s), "
                f"{stats.sales_failed} failed sale import(s)"
            )
            return _finalize(rt, run_id, RunStatus.FAILED, report)

        # -- Phase 4: validate Sales ------------------------------
        divs, reasons = validate_sales(rt, run_id, stats)
        report.divergences += divs
        report.reasons += reasons
        if divs or reasons:
            report.reasons.append("MIGRATION VALIDATION FAILED (sales)")
            return _finalize(rt, run_id, RunStatus.FAILED, report)
        _persist(rt, run_id, RunStatus.SALES_VALIDATED, report)

        # -- Phase 5: logical referential integrity Sales -> User --
        orphans, reasons = check_referential_integrity(rt, stats)
        report.orphan_sales += orphans
        report.reasons += reasons
        if orphans or reasons:
            report.reasons.append(f"{len(orphans)} orphan sale(s); run cannot be COMPLETED")
            return _finalize(rt, run_id, RunStatus.FAILED, report)
        _persist(rt, run_id, RunStatus.VALIDATED, report)

        return _finalize(rt, run_id, RunStatus.COMPLETED, report)

    except HttpError as exc:
        report.reasons.append(f"aborted on HTTP error: {exc}")
        _finalize(rt, run_id, RunStatus.FAILED, report, error=str(exc))
        raise SnapshotError(str(exc), report) from exc
    except Exception as exc:  # noqa: BLE001
        report.reasons.append(f"aborted on unexpected error: {exc}")
        _finalize(rt, run_id, RunStatus.FAILED, report, error=repr(exc))
        raise


# --------------------------------------------------------------------------- #
# Phase implementations
# --------------------------------------------------------------------------- #


def _already_done(item: MigrationItem | None) -> TypeGuard[MigrationItem]:
    return (
        item is not None
        and item.status in (ItemStatus.IMPORTED.value, ItemStatus.VALIDATED.value)
        and item.action in (ImportAction.CREATED.value, ImportAction.UNCHANGED.value)
    )


def _migrate_users(rt: Runtime, run_id: str, stats: MigrationStats) -> None:
    stats.users_found = rt.legacy.count_users()
    for legacy in rt.legacy.iter_users():
        existing = rt.state.get_item(run_id, EntityType.USER, legacy.id)
        if _already_done(existing):
            stats.add_import(EntityType.USER, ImportAction(existing.action))
            continue

        payload = UserImportPayload(id=legacy.id, name=legacy.name, created_at=legacy.created_at)
        body = payload.model_dump(mode="json")
        try:
            result = rt.user_client.import_user(payload)
        except HttpError as exc:
            stats.add_import(EntityType.USER, ImportAction.FAILED)
            stats.http_failures += 1
            rt.state.upsert_item(
                run_id=run_id,
                entity=EntityType.USER,
                legacy_id=legacy.id,
                destination="user-service",
                action=ImportAction.FAILED,
                status=ItemStatus.FAILED,
                error=str(exc),
                payload_hash=payload_hash(body),
                payload_json=canonical_json(body),
            )
            logger.warning(
                "migration.item",
                extra={
                    "run_id": run_id,
                    "entity": "user",
                    "legacy_id": legacy.id,
                    "operation": "import",
                    "result": "failed",
                    "error": str(exc),
                },
            )
            continue

        stats.add_import(EntityType.USER, result.action)
        ok = result.action in (ImportAction.CREATED, ImportAction.UNCHANGED)
        rt.state.upsert_item(
            run_id=run_id,
            entity=EntityType.USER,
            legacy_id=legacy.id,
            destination="user-service",
            action=result.action,
            status=ItemStatus.IMPORTED if ok else ItemStatus.FAILED,
            error=None if ok else canonical_json(result.detail or {}),
            payload_hash=payload_hash(body),
            payload_json=canonical_json(body),
        )
        logger.info(
            "migration.item",
            extra={
                "run_id": run_id,
                "entity": "user",
                "legacy_id": legacy.id,
                "operation": "import",
                "result": result.action.value,
            },
        )


def _migrate_sales(rt: Runtime, run_id: str, stats: MigrationStats) -> None:
    stats.sales_found = rt.legacy.count_sales()
    for legacy in rt.legacy.iter_sales():
        existing = rt.state.get_item(run_id, EntityType.SALE, legacy.id)
        if _already_done(existing):
            stats.add_import(EntityType.SALE, ImportAction(existing.action))
            continue

        payload = SaleImportPayload(
            id=legacy.id,
            user_id=legacy.user_id,
            item_name=legacy.item_name,
            quantity=legacy.quantity,
            created_at=legacy.created_at,
        )
        body = payload.model_dump(mode="json")
        try:
            result = rt.sales_client.import_sale(payload)
        except HttpError as exc:
            stats.add_import(EntityType.SALE, ImportAction.FAILED)
            stats.http_failures += 1
            rt.state.upsert_item(
                run_id=run_id,
                entity=EntityType.SALE,
                legacy_id=legacy.id,
                destination="sales-service",
                action=ImportAction.FAILED,
                status=ItemStatus.FAILED,
                error=str(exc),
                payload_hash=payload_hash(body),
                payload_json=canonical_json(body),
            )
            logger.warning(
                "migration.item",
                extra={
                    "run_id": run_id,
                    "entity": "sale",
                    "legacy_id": legacy.id,
                    "operation": "import",
                    "result": "failed",
                    "error": str(exc),
                },
            )
            continue

        stats.add_import(EntityType.SALE, result.action)
        ok = result.action in (ImportAction.CREATED, ImportAction.UNCHANGED)
        rt.state.upsert_item(
            run_id=run_id,
            entity=EntityType.SALE,
            legacy_id=legacy.id,
            destination="sales-service",
            action=result.action,
            status=ItemStatus.IMPORTED if ok else ItemStatus.FAILED,
            error=None if ok else canonical_json(result.detail or {}),
            payload_hash=payload_hash(body),
            payload_json=canonical_json(body),
        )
        logger.info(
            "migration.item",
            extra={
                "run_id": run_id,
                "entity": "sale",
                "legacy_id": legacy.id,
                "operation": "import",
                "result": result.action.value,
            },
        )


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #


def _persist(rt: Runtime, run_id: str, status: RunStatus, report: RunReport) -> None:
    report.status = status
    rt.state.update_run(run_id, status=status, stats=build_envelope(report))


def _finalize(
    rt: Runtime, run_id: str, status: RunStatus, report: RunReport, *, error: str | None = None
) -> RunReport:
    report.status = status
    report.finished_at = datetime.now(UTC)
    rt.state.update_run(
        run_id, status=status, stats=build_envelope(report), finished=True, error=error
    )
    try:
        write_files(report, rt.settings.report_dir)
    except OSError as exc:  # report is best-effort, never fail the run on it
        logger.warning("report.write_failed", extra={"run_id": run_id, "error": str(exc)})
    logger.info("snapshot.finish", extra={"run_id": run_id, "result": status.value})
    return report
