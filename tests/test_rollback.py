"""Rollback: order, ownership, drift detection, confirmation gate, dry-run."""

from __future__ import annotations

import pytest

from migration_tool.migration import run_snapshot
from migration_tool.models import EntityType, ImportAction, RunStatus
from migration_tool.rollback import RollbackRefused, run_rollback

USERS = [{"id": 1, "name": "Joao"}, {"id": 2, "name": "Maria"}, {"id": 3, "name": "Thiago"}]
SALES = [
    {"id": 100, "user_id": 1, "item_name": "Perfume", "quantity": 1},
    {"id": 101, "user_id": 3, "item_name": "Shampoo", "quantity": 2},
]


def _completed_run(make_runtime, seed_legacy, **kw):
    seed_legacy(USERS, SALES)
    rt = make_runtime(allow_destructive_rollback=True, **kw)
    report = run_snapshot(rt)
    assert report.status == RunStatus.COMPLETED
    return rt, report.run_id


def test_rollback_requires_confirm(make_runtime, seed_legacy):
    rt, run_id = _completed_run(make_runtime, seed_legacy)
    with pytest.raises(RollbackRefused):
        run_rollback(rt, run_id, confirm=False, dry_run=False)


def test_rollback_requires_env_flag(make_runtime, seed_legacy):
    seed_legacy(USERS, SALES)
    rt = make_runtime(allow_destructive_rollback=False)
    run_snapshot(rt)
    run_id = rt.state.latest_run().run_id
    with pytest.raises(RollbackRefused):
        run_rollback(rt, run_id, confirm=True, dry_run=False)


def test_rollback_deletes_sales_before_users(make_runtime, seed_legacy, call_log):
    rt, run_id = _completed_run(make_runtime, seed_legacy)
    call_log.clear()

    report = run_rollback(rt, run_id, confirm=True, dry_run=False)

    assert report.status == RunStatus.ROLLED_BACK
    sale_deletes = [i for i, (svc, m, p) in enumerate(call_log)
                    if m == "DELETE" and p.startswith("/sales/")]
    user_deletes = [i for i, (svc, m, p) in enumerate(call_log)
                    if m == "DELETE" and p.startswith("/users/")]
    assert sale_deletes and user_deletes
    assert max(sale_deletes) < min(user_deletes)


def test_rollback_only_removes_created_items(make_runtime, seed_legacy, fake_user):
    seed_legacy(USERS, SALES)
    # user 2 pre-exists identically -> "unchanged", must survive rollback
    from tests.conftest import DT
    fake_user.store[2] = {"id": 2, "name": "Maria", "created_at": DT.isoformat()}
    rt = make_runtime(allow_destructive_rollback=True)
    run_snapshot(rt)
    run_id = rt.state.latest_run().run_id

    report = run_rollback(rt, run_id, confirm=True, dry_run=False)

    assert report.status == RunStatus.ROLLED_BACK
    assert 2 in fake_user.store            # unchanged -> kept
    assert 1 not in fake_user.store         # created -> removed
    assert 3 not in fake_user.store
    assert report.users_deleted == 2


def test_rollback_refuses_drifted_record(make_runtime, seed_legacy, fake_user):
    rt, run_id = _completed_run(make_runtime, seed_legacy)

    # Someone changed user 3 after the migration created it.
    fake_user.store[3]["name"] = "Thiago Silva"

    report = run_rollback(rt, run_id, confirm=True, dry_run=False)

    assert report.status == RunStatus.ROLLBACK_FAILED
    assert report.conflicts == 1
    assert 3 in fake_user.store  # not deleted
    conflict = next(i for i in report.items if i.outcome == "conflict")
    assert conflict.legacy_id == 3


def test_rollback_dry_run_writes_nothing(make_runtime, seed_legacy, call_log):
    rt, run_id = _completed_run(make_runtime, seed_legacy)
    users_before = dict(rt.state.get_run(run_id).__dict__)
    call_log.clear()

    report = run_rollback(rt, run_id, confirm=False, dry_run=True)

    assert report.would_delete_sales == 2
    assert report.would_delete_users == 3
    assert not any(m == "DELETE" for (svc, m, p) in call_log)
    assert rt.state.get_run(run_id).status == RunStatus.COMPLETED.value  # unchanged


def test_rollback_is_idempotent_when_row_already_absent(make_runtime, seed_legacy, fake_sale):
    rt, run_id = _completed_run(make_runtime, seed_legacy)
    del fake_sale.store[100]  # already gone

    report = run_rollback(rt, run_id, confirm=True, dry_run=False)
    assert report.status == RunStatus.ROLLED_BACK
    assert report.sales_already_absent == 1
    assert report.sales_deleted == 1


def test_legacy_untouched_by_rollback(make_runtime, seed_legacy, legacy_source):
    rt, run_id = _completed_run(make_runtime, seed_legacy)
    run_rollback(rt, run_id, confirm=True, dry_run=False)
    assert legacy_source.count_users() == 3
    assert legacy_source.count_sales() == 2
