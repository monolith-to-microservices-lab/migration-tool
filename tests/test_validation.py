"""Standalone validation + dry-run + legacy read-only guarantees."""

from __future__ import annotations

from migration_tool.migration import run_dry, run_snapshot
from migration_tool.models import RunStatus
from migration_tool.validation import (
    check_referential_integrity,
    validate_sales,
    validate_users,
)

USERS = [{"id": 1, "name": "Joao"}, {"id": 2, "name": "Maria"}]
SALES = [{"id": 10, "user_id": 1, "item_name": "Perfume", "quantity": 1}]


def test_validate_passes_after_snapshot(make_runtime, seed_legacy):
    seed_legacy(USERS, SALES)
    rt = make_runtime()
    run_snapshot(rt)

    d_users, r_users = validate_users(rt, None, None)
    d_sales, r_sales = validate_sales(rt, None, None)
    orphans, r_ref = check_referential_integrity(rt, None)

    assert not d_users and not r_users
    assert not d_sales and not r_sales
    assert not orphans and not r_ref


def test_validate_sales_reports_missing(make_runtime, seed_legacy, fake_sale):
    seed_legacy(USERS, SALES)
    rt = make_runtime()
    run_snapshot(rt)
    del fake_sale.store[10]  # vanished from the service

    divergences, _ = validate_sales(rt, None, None)
    assert any(d.legacy_id == 10 and d.field == "__missing__" for d in divergences)


def test_dry_run_makes_no_write_calls(make_runtime, seed_legacy, call_log):
    seed_legacy(USERS, SALES)
    rt = make_runtime()

    report = run_dry(rt)

    assert report.dry_run is True
    assert report.status == RunStatus.COMPLETED
    assert report.stats.users_found == 2
    assert report.stats.sales_found == 1
    assert not any(m in ("POST", "DELETE") for (svc, m, p) in call_log)
    # no run persisted for a dry-run
    assert rt.state.list_runs() == []


def test_dry_run_flags_unreachable_service(make_runtime, seed_legacy, fake_user):
    seed_legacy(USERS, SALES)
    rt = make_runtime()
    fake_user.hard_500.add("/health")

    report = run_dry(rt)
    assert report.status == RunStatus.FAILED
    assert any("User Service unreachable" in r for r in report.reasons)


def test_legacy_source_is_read_only_in_practice(make_runtime, seed_legacy, legacy_source):
    """The tool only ever calls SELECT-side helpers; a full snapshot + rollback
    leaves the legacy row counts identical."""
    seed_legacy(USERS, SALES)
    rt = make_runtime(allow_destructive_rollback=True)
    before = (legacy_source.count_users(), legacy_source.count_sales())
    run_snapshot(rt)
    from migration_tool.rollback import run_rollback

    run_rollback(rt, rt.state.latest_run().run_id, confirm=True, dry_run=False)
    after = (legacy_source.count_users(), legacy_source.count_sales())
    assert before == after == (2, 1)
