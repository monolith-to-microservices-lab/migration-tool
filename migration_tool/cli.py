"""`python -m migration_tool <command>` - the operator-facing CLI.

Commands
    snapshot   run the full initial migration (Users -> validate -> Sales -> validate -> integrity)
    validate   run only reconciliation / validation against the services
    rollback   delete (via APIs) only the rows a given run created
    status     show a recorded run
    resume     continue an interrupted run from its last known state
    runs       list recorded runs
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import typer

from .config import get_settings
from .logging_config import get_logger, setup_logging
from .migration import SnapshotError, run_dry, run_snapshot
from .models import MigrationStats, RunReport, RunStatus
from .report import render_text
from .rollback import RollbackRefused, run_rollback
from .runtime import build_runtime
from .validation import check_referential_integrity, validate_sales, validate_users

app = typer.Typer(add_completion=False, help="Legacy monolith -> microservices migration coordinator.")
logger = get_logger("migration_tool.cli")


def _boot():
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    return settings


@app.command()
def snapshot(
    dry_run: bool = typer.Option(False, "--dry-run", help="Read + preflight only. No writes."),
    batch_size: int = typer.Option(None, "--batch-size", help="Override BATCH_SIZE for this run."),
):
    """Run the full initial migration."""
    settings = _boot()
    if batch_size:
        settings.batch_size = batch_size
    with build_runtime(settings) as rt:
        if dry_run:
            report = run_dry(rt)
            typer.echo(render_text(report))
            raise typer.Exit(0 if report.status == RunStatus.COMPLETED else 1)
        try:
            report = run_snapshot(rt)
        except SnapshotError as exc:
            typer.echo(render_text(exc.report))
            raise typer.Exit(1) from exc
    typer.echo(render_text(report))
    raise typer.Exit(0 if report.status == RunStatus.COMPLETED else 1)


@app.command()
def resume(run_id: str = typer.Option(..., "--run-id", help="Run to continue.")):
    """Continue an interrupted run. Idempotent: never creates duplicates."""
    settings = _boot()
    with build_runtime(settings) as rt:
        try:
            report = run_snapshot(rt, resume_run_id=run_id)
        except SnapshotError as exc:
            typer.echo(render_text(exc.report))
            raise typer.Exit(1) from exc
        except (KeyError, ValueError) as exc:
            typer.echo(f"error: {exc}")
            raise typer.Exit(2) from exc
    typer.echo(render_text(report))
    raise typer.Exit(0 if report.status == RunStatus.COMPLETED else 1)


@app.command()
def validate(
    run_id: str = typer.Option(None, "--run-id", help="Optional: also mark items of this run validated."),
):
    """Reconcile legacy vs. services (Users, Sales, logical Sales -> User)."""
    settings = _boot()
    with build_runtime(settings) as rt:
        stats = MigrationStats()
        stats.users_found = rt.legacy.count_users()
        stats.sales_found = rt.legacy.count_sales()
        report = RunReport(
            run_id=run_id or "(validate)",
            status=RunStatus.STARTED,
            started_at=datetime.now(timezone.utc),
            legacy_source=settings.masked_legacy_url(),
            stats=stats,
        )
        d1, r1 = validate_users(rt, run_id, stats)
        d2, r2 = validate_sales(rt, run_id, stats)
        orphans, r3 = check_referential_integrity(rt, stats)
        report.divergences = d1 + d2
        report.orphan_sales = orphans
        report.reasons = r1 + r2 + r3
        report.finished_at = datetime.now(timezone.utc)
        report.status = (
            RunStatus.VALIDATED
            if not (report.divergences or report.orphan_sales or report.reasons)
            else RunStatus.FAILED
        )
    typer.echo(render_text(report))
    raise typer.Exit(0 if report.status == RunStatus.VALIDATED else 1)


@app.command()
def rollback(
    run_id: str = typer.Option(..., "--run-id", help="Run whose CREATED rows to remove."),
    confirm: bool = typer.Option(False, "--confirm", help="Required for actual deletion."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted. No writes."),
):
    """Safely roll back the DATA a snapshot run created (Sales, then Users)."""
    settings = _boot()
    with build_runtime(settings) as rt:
        try:
            report = run_rollback(rt, run_id, confirm=confirm, dry_run=dry_run)
        except RollbackRefused as exc:
            typer.echo(f"rollback refused: {exc}")
            raise typer.Exit(3) from exc
        except KeyError as exc:
            typer.echo(f"error: {exc}")
            raise typer.Exit(2) from exc
    _print_rollback(report)
    ok = report.status in (RunStatus.ROLLED_BACK,) or (report.dry_run and report.conflicts == 0)
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(run_id: str = typer.Option(..., "--run-id")):
    """Show a recorded run and its item tally."""
    _boot()
    with build_runtime(get_settings()) as rt:
        run = rt.state.get_run(run_id)
        if run is None:
            typer.echo(f"no such run: {run_id}")
            raise typer.Exit(2)
        items = rt.state.items_for_run(run_id)
        tally: dict[str, int] = {}
        for it in items:
            tally[f"{it.entity_type}/{it.action}/{it.status}"] = (
                tally.get(f"{it.entity_type}/{it.action}/{it.status}", 0) + 1
            )
        out = {
            "run_id": run.run_id,
            "mode": run.mode,
            "dry_run": run.dry_run,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "legacy_source": run.legacy_source,
            "error": run.error,
            "items_total": len(items),
            "items_by_kind": tally,
            "report": run.stats_json,
        }
    typer.echo(json.dumps(out, indent=2, default=str))


@app.command()
def runs():
    """List recorded runs (newest first)."""
    _boot()
    with build_runtime(get_settings()) as rt:
        rows = rt.state.list_runs()
        if not rows:
            typer.echo("(no runs)")
            return
        for r in rows:
            started = r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "?"
            typer.echo(f"{r.run_id}  {r.status:<16}  started={started}  dry_run={r.dry_run}")


def _print_rollback(report) -> None:
    lines = [
        "Rollback",
        "========",
        "",
        f"Run ID:    {report.run_id}",
        f"Mode:      {'DRY-RUN' if report.dry_run else 'execute'}",
        f"Confirmed: {report.confirmed}",
        "",
    ]
    if report.dry_run:
        lines += [
            "Would delete:",
            f"  Sales: {report.would_delete_sales}",
            f"  Users: {report.would_delete_users}",
            "",
            f"Already absent - Sales: {report.sales_already_absent}  Users: {report.users_already_absent}",
            f"Conflicts: {report.conflicts}",
        ]
    else:
        lines += [
            "Deleted:",
            f"  Sales: {report.sales_deleted}  (already absent: {report.sales_already_absent})",
            f"  Users: {report.users_deleted}  (already absent: {report.users_already_absent})",
            "",
            f"Conflicts: {report.conflicts}    Failures: {report.failures}",
        ]
    if report.conflicts or report.failures:
        lines += ["", "Unresolved items:"]
        for it in report.items:
            if it.outcome in ("conflict", "failed"):
                lines.append(f"  {it.entity} {it.legacy_id}: {it.outcome} - {it.detail}")
    if report.reasons:
        lines += ["", "Notes:"] + [f"  - {r}" for r in report.reasons]
    lines += ["", f"RESULT: {report.status.value}", ""]
    typer.echo("\n".join(lines))


if __name__ == "__main__":
    app()
