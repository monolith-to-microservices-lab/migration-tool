"""Reconciliation / validation.

The migration-tool cannot read User DB or Sales DB directly, so every check here
goes through the services' public GET endpoints.
"""

from __future__ import annotations

from .http import HttpError
from .logging_config import get_logger
from .models import (
    Divergence,
    EntityType,
    ImportAction,
    ItemStatus,
    OrphanSale,
    datetimes_equal,
)
from .runtime import Runtime

logger = get_logger("migration_tool.validation")


def _cmp_user(legacy, remote: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    if int(remote["id"]) != legacy.id:
        out.append(("id", str(legacy.id), str(remote["id"])))
    if remote["name"] != legacy.name:
        out.append(("name", legacy.name, str(remote["name"])))
    if not datetimes_equal(legacy.created_at, _parse_dt(remote["created_at"])):
        out.append(("created_at", legacy.created_at.isoformat(), str(remote["created_at"])))
    return out


def _cmp_sale(legacy, remote: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    if int(remote["id"]) != legacy.id:
        out.append(("id", str(legacy.id), str(remote["id"])))
    if int(remote["user_id"]) != legacy.user_id:
        out.append(("user_id", str(legacy.user_id), str(remote["user_id"])))
    if remote["item_name"] != legacy.item_name:
        out.append(("item_name", legacy.item_name, str(remote["item_name"])))
    if int(remote["quantity"]) != legacy.quantity:
        out.append(("quantity", str(legacy.quantity), str(remote["quantity"])))
    if not datetimes_equal(legacy.created_at, _parse_dt(remote["created_at"])):
        out.append(("created_at", legacy.created_at.isoformat(), str(remote["created_at"])))
    return out


def _parse_dt(value: str):
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def validate_users(rt: Runtime, run_id: str | None, stats) -> tuple[list[Divergence], list[str]]:
    divergences: list[Divergence] = []
    reasons: list[str] = []
    found = 0
    legacy_count = rt.legacy.count_users()

    for legacy in rt.legacy.iter_users():
        try:
            remote = rt.user_client.get_user(legacy.id)
        except HttpError as exc:
            reasons.append(f"user {legacy.id}: validation request failed ({exc})")
            continue
        if remote is None:
            divergences.append(
                Divergence(entity=EntityType.USER, legacy_id=legacy.id, field="__missing__",
                           legacy_value="present", destination_value="absent")
            )
            continue
        found += 1
        diffs = _cmp_user(legacy, remote)
        for field, lv, dv in diffs:
            divergences.append(
                Divergence(entity=EntityType.USER, legacy_id=legacy.id, field=field,
                           legacy_value=lv, destination_value=dv)
            )
        if not diffs and run_id is not None:
            _mark_validated(rt, run_id, EntityType.USER, legacy.id)

    if found != legacy_count:
        reasons.append(f"user count mismatch: legacy={legacy_count} reachable_in_service={found}")
    if stats is not None:
        stats.users_validated = found - len({d.legacy_id for d in divergences})
    logger.info("validate.users", extra={"legacy": legacy_count, "found": found,
                                         "divergences": len(divergences)})
    return divergences, reasons


def validate_sales(rt: Runtime, run_id: str | None, stats) -> tuple[list[Divergence], list[str]]:
    divergences: list[Divergence] = []
    reasons: list[str] = []
    found = 0
    legacy_count = rt.legacy.count_sales()

    for legacy in rt.legacy.iter_sales():
        try:
            remote = rt.sales_client.get_sale(legacy.id)
        except HttpError as exc:
            reasons.append(f"sale {legacy.id}: validation request failed ({exc})")
            continue
        if remote is None:
            divergences.append(
                Divergence(entity=EntityType.SALE, legacy_id=legacy.id, field="__missing__",
                           legacy_value="present", destination_value="absent")
            )
            continue
        found += 1
        diffs = _cmp_sale(legacy, remote)
        for field, lv, dv in diffs:
            divergences.append(
                Divergence(entity=EntityType.SALE, legacy_id=legacy.id, field=field,
                           legacy_value=lv, destination_value=dv)
            )
        if not diffs and run_id is not None:
            _mark_validated(rt, run_id, EntityType.SALE, legacy.id)

    if found != legacy_count:
        reasons.append(f"sale count mismatch: legacy={legacy_count} reachable_in_service={found}")
    if stats is not None:
        stats.sales_validated = found - len({d.legacy_id for d in divergences})
    logger.info("validate.sales", extra={"legacy": legacy_count, "found": found,
                                         "divergences": len(divergences)})
    return divergences, reasons


def check_referential_integrity(rt: Runtime, stats) -> tuple[list[OrphanSale], list[str]]:
    """For every legacy sale, confirm its user_id resolves in the User Service."""
    reasons: list[str] = []
    missing_users: set[int] = set()
    known_users: set[int] = set()

    for user_id in rt.legacy.distinct_sale_user_ids():
        try:
            remote = rt.user_client.get_user(user_id)
        except HttpError as exc:
            reasons.append(f"referential check for user {user_id} failed ({exc})")
            missing_users.add(user_id)
            continue
        (known_users if remote is not None else missing_users).add(user_id)

    orphans: list[OrphanSale] = []
    checked = 0
    for sale_id, user_id in rt.legacy.iter_sale_refs():
        checked += 1
        if user_id in missing_users:
            orphans.append(OrphanSale(sale_id=sale_id, user_id=user_id))

    if stats is not None:
        stats.checked_sales_refs = checked
        stats.orphan_sales = len(orphans)
    logger.info("validate.referential", extra={"checked": checked, "orphans": len(orphans),
                                               "missing_users": len(missing_users)})
    return orphans, reasons


def _mark_validated(rt: Runtime, run_id: str, entity: EntityType, legacy_id: int) -> None:
    item = rt.state.get_item(run_id, entity, legacy_id)
    if item is not None and item.action in (ImportAction.CREATED.value, ImportAction.UNCHANGED.value):
        rt.state.set_item_status(item.id, ItemStatus.VALIDATED)
