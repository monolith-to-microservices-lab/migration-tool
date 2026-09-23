"""Snapshot / validation / idempotency / resume behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

from migration_tool.migration import run_snapshot
from migration_tool.models import EntityType, ImportAction, RunStatus
from migration_tool.state import MigrationItem

USERS = [
    {"id": 1, "name": "Joao"},
    {"id": 2, "name": "Maria"},
    {"id": 3, "name": "Thiago"},
]
SALES = [
    {"id": 100, "user_id": 1, "item_name": "Perfume", "quantity": 1},
    {"id": 101, "user_id": 3, "item_name": "Shampoo", "quantity": 2},
    {"id": 102, "user_id": 3, "item_name": "Creme", "quantity": 1},
]


def test_snapshot_migrates_users_and_sales(make_runtime, seed_legacy, fake_user, fake_sale):
    seed_legacy(USERS, SALES)
    rt = make_runtime()

    report = run_snapshot(rt)

    assert report.status == RunStatus.COMPLETED
    assert set(fake_user.store) == {1, 2, 3}
    assert set(fake_sale.store) == {100, 101, 102}
    assert report.stats.users_created == 3
    assert report.stats.sales_created == 3
    assert report.stats.orphan_sales == 0


def test_ids_are_preserved(make_runtime, seed_legacy, fake_user, fake_sale):
    seed_legacy(USERS, SALES)
    run_snapshot(make_runtime())
    assert fake_user.store[3]["id"] == 3
    assert fake_sale.store[102]["id"] == 102
    assert fake_sale.store[100]["user_id"] == 1


def test_users_run_before_sales(make_runtime, seed_legacy, call_log):
    seed_legacy(USERS, SALES)
    run_snapshot(make_runtime())

    first_sale_write = next(
        i for i, (svc, m, p) in enumerate(call_log) if p == "/internal/sales/import" and m == "POST"
    )
    last_user_write = max(
        i for i, (svc, m, p) in enumerate(call_log) if p == "/internal/users/import" and m == "POST"
    )
    assert last_user_write < first_sale_write


def test_created_belongs_to_run_unchanged_does_not(make_runtime, seed_legacy, fake_user):
    seed_legacy(USERS, [])
    # user 2 already exists in the service, identical -> will be "unchanged"
    fake_user.store[2] = {
        "id": 2,
        "name": "Maria",
        "created_at": datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC).isoformat(),
    }
    rt = make_runtime()
    run_snapshot(rt)

    with rt.state.session() as s:
        items = {i.legacy_id: i for i in s.query(MigrationItem).all()}
    assert items[1].action == ImportAction.CREATED.value
    assert items[2].action == ImportAction.UNCHANGED.value
    assert items[3].action == ImportAction.CREATED.value

    created = rt.state.items_for_run(
        rt.state.latest_run().run_id, entity=EntityType.USER, action=ImportAction.CREATED
    )
    assert {i.legacy_id for i in created} == {1, 3}  # NOT 2


def test_partial_batch_failure_does_not_lose_prior_successes(make_runtime, seed_legacy, fake_user):
    """One user import hard-fails (persistent 500, retries exhausted) in the
    middle of a multi-batch run (batch_size=2, see the legacy_source
    fixture). The users already created before/after the failing one must
    still be recorded as CREATED - a single bad item must not roll back or
    block the rest of the batch/phase.
    """
    import json as _json

    import httpx as _httpx

    seed_legacy(USERS, [])  # 3 users -> two batches of size 2

    # Only user 2's import call hard-fails; 1 and 3 must still succeed. Must
    # be wired in BEFORE make_runtime(), which captures `fake_user.handler`
    # by value into httpx.MockTransport at construction time.
    original_handler = fake_user.handler

    def selective_handler(request):
        if request.method == "POST" and request.url.path == fake_user.import_path:
            body = _json.loads(request.content)
            if body["id"] == 2:
                return _httpx.Response(500, json={"detail": "boom"})
        return original_handler(request)

    fake_user.handler = selective_handler
    rt = make_runtime()

    report = run_snapshot(rt)

    assert report.status == RunStatus.FAILED
    assert report.stats.users_failed == 1
    assert report.stats.users_created == 2

    with rt.state.session() as s:
        items = {i.legacy_id: i.action for i in s.query(MigrationItem).all()}
    assert items[1] == ImportAction.CREATED.value
    assert items[2] == ImportAction.FAILED.value
    assert items[3] == ImportAction.CREATED.value
    # And the failing item never landed in the destination service.
    assert 2 not in fake_user.store
    assert 1 in fake_user.store and 3 in fake_user.store


def test_409_conflict_fails_the_run(make_runtime, seed_legacy, fake_user):
    seed_legacy(USERS, SALES)
    # pre-existing user 2 with a DIFFERENT name -> import conflict (409)
    fake_user.store[2] = {"id": 2, "name": "Different", "created_at": "2000-01-01T00:00:00+00:00"}
    rt = make_runtime()

    report = run_snapshot(rt)

    assert report.status == RunStatus.FAILED
    assert report.stats.users_conflict == 1
    # Sales phase must not have run
    assert rt.sales_client  # sanity
    assert report.stats.sales_created == 0


def test_validation_detects_divergence(make_runtime, seed_legacy, fake_user, monkeypatch):
    seed_legacy(USERS, SALES)
    rt = make_runtime()

    # Corrupt the service response for user 3 right after import, before validation.
    real_get = rt.user_client.get_user

    def poisoned(uid):
        rec = real_get(uid)
        if rec and uid == 3:
            rec = dict(rec, name="Tampered")
        return rec

    monkeypatch.setattr(rt.user_client, "get_user", poisoned)
    report = run_snapshot(rt)

    assert report.status == RunStatus.FAILED
    assert any(d.legacy_id == 3 and d.field == "name" for d in report.divergences)
    assert report.stats.sales_created == 0  # stopped before Sales


def test_orphan_sale_detected(make_runtime, seed_legacy):
    users = [{"id": 1, "name": "Joao"}]
    sales = [
        {"id": 900, "user_id": 1, "item_name": "X", "quantity": 1},
        {"id": 901, "user_id": 999, "item_name": "Y", "quantity": 1},
    ]
    seed_legacy(users, sales)
    rt = make_runtime()
    report = run_snapshot(rt)

    assert report.status == RunStatus.FAILED
    assert [(o.sale_id, o.user_id) for o in report.orphan_sales] == [(901, 999)]
    assert report.stats.orphan_sales == 1


def test_second_run_is_idempotent(make_runtime, seed_legacy, fake_user, fake_sale):
    seed_legacy(USERS, SALES)
    r1 = run_snapshot(make_runtime())
    assert r1.stats.users_created == 3 and r1.stats.sales_created == 3

    r2 = run_snapshot(make_runtime())
    assert r2.status == RunStatus.COMPLETED
    assert r2.stats.users_created == 0
    assert r2.stats.users_unchanged == 3
    assert r2.stats.sales_created == 0
    assert r2.stats.sales_unchanged == 3
    assert set(fake_user.store) == {1, 2, 3}  # no duplicates


def test_resume_continues_without_duplicates(make_runtime, seed_legacy):
    from migration_tool.http import HttpError

    seed_legacy(USERS, SALES)
    rt = make_runtime()

    # Sale 102 fails hard during the first attempt -> run ends FAILED mid-Sales.
    real_import = rt.sales_client.import_sale

    def flaky(payload):
        if payload.id == 102:
            raise HttpError("transient boom", attempts=4)
        return real_import(payload)

    rt.sales_client.import_sale = flaky
    report = run_snapshot(rt)
    assert report.status == RunStatus.FAILED
    run_id = rt.state.latest_run().run_id
    assert report.stats.sales_created == 2
    assert report.stats.sales_failed == 1

    # "Fix" the transient fault and resume the same run.
    rt.sales_client.import_sale = real_import
    report2 = run_snapshot(rt, resume_run_id=run_id)

    assert report2.status == RunStatus.COMPLETED
    assert report2.stats.sales_created == 3
    created_users = rt.state.items_for_run(
        run_id, entity=EntityType.USER, action=ImportAction.CREATED
    )
    assert len(created_users) == 3  # not re-created, just re-counted


def test_snapshot_writes_report_files(make_runtime, seed_legacy, tmp_path):
    seed_legacy(USERS, SALES)
    rt = make_runtime()
    report = run_snapshot(rt)
    run_id = report.run_id
    report_dir = tmp_path / "reports"
    assert (report_dir / f"{run_id}.txt").exists()
    assert (report_dir / f"{run_id}.json").exists()
