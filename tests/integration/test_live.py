"""End-to-end against the running services: snapshot -> idempotent re-run.

Does NOT roll back (so it is safe to run repeatedly); rollback has its own
dedicated manual demo in the README.
"""

from __future__ import annotations

from migration_tool.migration import run_snapshot
from migration_tool.models import RunStatus


def test_live_snapshot_then_idempotent_rerun(live_runtime):
    rt = live_runtime

    legacy_users = rt.legacy.count_users()
    legacy_sales = rt.legacy.count_sales()

    r1 = run_snapshot(rt)
    assert r1.status == RunStatus.COMPLETED, r1.reasons
    assert r1.stats.users_found == legacy_users
    assert r1.stats.sales_found == legacy_sales
    assert r1.stats.users_created + r1.stats.users_unchanged == legacy_users
    assert r1.stats.sales_created + r1.stats.sales_unchanged == legacy_sales
    assert r1.stats.orphan_sales == 0

    r2 = run_snapshot(rt)
    assert r2.status == RunStatus.COMPLETED
    assert r2.stats.users_created == 0
    assert r2.stats.sales_created == 0
    assert r2.stats.users_unchanged == legacy_users
    assert r2.stats.sales_unchanged == legacy_sales
