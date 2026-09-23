"""Controlled rollback of the DATA a snapshot run created.

Scope (deliberately narrow):
  * removes, via the services' HTTP APIs, ONLY rows this run inserted
    (state item ``action == 'created'``);
  * never touches ``action == 'unchanged'`` rows - they pre-dated the run;
  * never issues TRUNCATE / DELETE ALL - it deletes by explicit id;
  * never writes to the Legacy DB (it still holds every original row);
  * Sales are removed before Users (reverse of the migration order);
  * before deleting a row it re-reads the destination and compares it to the
    stored payload snapshot - a drifted row is a ROLLBACK CONFLICT and is left
    in place.

This is *data migration rollback*, valid only while the legacy monolith is still
the source of truth and the new services take no real traffic. See README.md.
"""

from __future__ import annotations

import json

from .http import HttpError
from .logging_config import get_logger
from .models import (
    EntityType,
    ImportAction,
    ItemStatus,
    RollbackItemResult,
    RollbackReport,
    RunStatus,
    datetimes_equal,
)
from .runtime import Runtime

logger = get_logger("migration_tool.rollback")


class RollbackRefused(RuntimeError):
    """Raised when safety gates are not satisfied - nothing was deleted."""


def _parse_dt(value):
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _matches_snapshot(entity: EntityType, stored: dict, remote: dict) -> tuple[bool, str]:
    fields = (
        ["id", "name"] if entity is EntityType.USER else ["id", "user_id", "item_name", "quantity"]
    )
    for f in fields:
        if str(stored.get(f)) != str(remote.get(f)):
            return False, f"{f}: migrated={stored.get(f)!r} now={remote.get(f)!r}"
    try:
        if not datetimes_equal(_parse_dt(stored["created_at"]), _parse_dt(remote["created_at"])):
            return False, (
                f"created_at: migrated={stored['created_at']!r} now={remote['created_at']!r}"
            )
    except (KeyError, ValueError):
        return False, "created_at: unparseable / missing"
    return True, ""


def run_rollback(
    rt: Runtime, run_id: str, *, confirm: bool = False, dry_run: bool = False
) -> RollbackReport:
    run = rt.state.get_run(run_id)
    if run is None:
        raise KeyError(f"unknown run_id: {run_id}")
    if run.status == RunStatus.ROLLED_BACK.value:
        raise RollbackRefused(f"run {run_id} is already ROLLED_BACK")

    report = RollbackReport(
        run_id=run_id, status=RunStatus(run.status), dry_run=dry_run, confirmed=confirm
    )

    # --- safety gates (skipped for dry-run, which never writes) -----------
    if not dry_run:
        if not rt.settings.allow_destructive_rollback:
            raise RollbackRefused(
                "ALLOW_DESTRUCTIVE_ROLLBACK is false. Refusing to delete anything. "
                "Set it to true (and pass --confirm) once you are sure this is safe."
            )
        if not confirm:
            raise RollbackRefused("missing --confirm. Refusing to delete anything.")

    logger.info("rollback.start", extra={"run_id": run_id, "dry_run": dry_run})

    # --- Phase A: Sales (reverse order: Sales before Users) --------------
    sales_items = rt.state.items_for_run(
        run_id, entity=EntityType.SALE, action=ImportAction.CREATED
    )
    _process(rt, EntityType.SALE, sales_items, report, dry_run)

    sales_unresolved = report.conflicts + report.failures
    if sales_unresolved:
        report.reasons.append(
            f"{sales_unresolved} sale(s) could not be rolled back safely; "
            "User rollback skipped (reverse-order safety)"
        )
        report.status = RunStatus.ROLLBACK_FAILED
        if not dry_run:
            rt.state.update_run(run_id, status=RunStatus.ROLLBACK_FAILED)
        logger.info("rollback.finish", extra={"run_id": run_id, "result": report.status.value})
        return report

    # --- Phase B: Users --------------------------------------------------
    user_items = rt.state.items_for_run(run_id, entity=EntityType.USER, action=ImportAction.CREATED)
    _process(rt, EntityType.USER, user_items, report, dry_run)

    unresolved = report.conflicts + report.failures
    if dry_run:
        report.status = RunStatus(run.status)  # unchanged
    elif unresolved:
        report.status = RunStatus.ROLLBACK_FAILED
        report.reasons.append(f"{unresolved} item(s) unresolved after rollback")
        rt.state.update_run(run_id, status=RunStatus.ROLLBACK_FAILED)
    else:
        report.status = RunStatus.ROLLED_BACK
        rt.state.update_run(run_id, status=RunStatus.ROLLED_BACK, finished=True)

    logger.info(
        "rollback.finish",
        extra={"run_id": run_id, "result": report.status.value, "dry_run": dry_run},
    )
    return report


def _process(
    rt: Runtime,
    entity: EntityType,
    items: list,
    report: RollbackReport,
    dry_run: bool,
) -> None:
    get_remote = rt.sales_client.get_sale if entity is EntityType.SALE else rt.user_client.get_user
    delete_remote = (
        rt.sales_client.delete_sale if entity is EntityType.SALE else rt.user_client.delete_user
    )

    for item in items:
        legacy_id = item.legacy_id
        stored = json.loads(item.payload_json) if item.payload_json else {}
        try:
            remote = get_remote(legacy_id)
        except HttpError as exc:
            _record(report, entity, legacy_id, "failed", str(exc))
            _set_status(rt, item, ItemStatus.ROLLBACK_FAILED, dry_run, str(exc))
            continue

        if remote is None:
            _record(report, entity, legacy_id, "already_absent", "destination row not present")
            _set_status(rt, item, ItemStatus.ROLLED_BACK, dry_run)
            _bump_absent(report, entity)
            continue

        matches, why = _matches_snapshot(entity, stored, remote)
        if not matches:
            _record(report, entity, legacy_id, "conflict", f"ROLLBACK CONFLICT - {why}")
            _set_status(rt, item, ItemStatus.ROLLBACK_CONFLICT, dry_run, why)
            logger.warning(
                "rollback.conflict",
                extra={"entity": entity.value, "legacy_id": legacy_id, "reason": why},
            )
            continue

        if dry_run:
            _record(report, entity, legacy_id, "would_delete", "matches snapshot")
            if entity is EntityType.SALE:
                report.would_delete_sales += 1
            else:
                report.would_delete_users += 1
            continue

        try:
            delete_remote(legacy_id)
            still_there = get_remote(legacy_id)  # validate removal
        except HttpError as exc:
            _record(report, entity, legacy_id, "failed", str(exc))
            _set_status(rt, item, ItemStatus.ROLLBACK_FAILED, dry_run, str(exc))
            continue

        if still_there is not None:
            _record(report, entity, legacy_id, "failed", "row still present after DELETE")
            _set_status(rt, item, ItemStatus.ROLLBACK_FAILED, dry_run, "still present")
            continue

        _record(report, entity, legacy_id, "deleted", "")
        _set_status(rt, item, ItemStatus.ROLLED_BACK, dry_run)
        if entity is EntityType.SALE:
            report.sales_deleted += 1
        else:
            report.users_deleted += 1
        logger.info(
            "rollback.item",
            extra={
                "run_id": report.run_id,
                "entity": entity.value,
                "legacy_id": legacy_id,
                "operation": "delete",
                "result": "deleted",
            },
        )


def _record(
    report: RollbackReport, entity: EntityType, legacy_id: int, outcome: str, detail: str
) -> None:
    report.items.append(
        RollbackItemResult(entity=entity, legacy_id=legacy_id, outcome=outcome, detail=detail)
    )
    if outcome == "conflict":
        report.conflicts += 1
    elif outcome == "failed":
        report.failures += 1


def _bump_absent(report: RollbackReport, entity: EntityType) -> None:
    if entity is EntityType.SALE:
        report.sales_already_absent += 1
    else:
        report.users_already_absent += 1


def _set_status(
    rt: Runtime, item, status: ItemStatus, dry_run: bool, error: str | None = None
) -> None:
    if dry_run:
        return
    rt.state.set_item_status(item.id, status, error=error)
