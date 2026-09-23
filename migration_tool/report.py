"""Render + persist migration run reports (human text and JSON)."""

from __future__ import annotations

import json
from pathlib import Path

from .models import RunReport, RunStatus

_COMPLETED_REASON = "all phases passed"


def build_envelope(report: RunReport) -> dict:
    """Compact dict stashed into ``migration_runs.stats_json`` for `status`."""
    return {
        "stats": report.stats.model_dump(),
        "divergences": [d.model_dump() for d in report.divergences],
        "orphan_sales": [o.model_dump() for o in report.orphan_sales],
        "reasons": report.reasons,
        "dry_run": report.dry_run,
    }


def render_text(report: RunReport) -> str:
    s = report.stats
    lines: list[str] = []
    add = lines.append

    add("Migration Run")
    add("=============")
    add("")
    add(f"Run ID:   {report.run_id}")
    add(f"Mode:     {'DRY-RUN' if report.dry_run else 'snapshot'}")
    add(f"Started:  {report.started_at.isoformat() if report.started_at else '-'}")
    add(f"Finished: {report.finished_at.isoformat() if report.finished_at else '-'}")
    add(f"Legacy:   {report.legacy_source}")
    add("")
    add("USERS")
    add("")
    add(f"  Legacy records:  {s.users_found:>6}")
    add(f"  Created:         {s.users_created:>6}")
    add(f"  Unchanged:       {s.users_unchanged:>6}")
    add(f"  Conflicts:       {s.users_conflict:>6}")
    add(f"  Failed:          {s.users_failed:>6}")
    add(f"  Validated:       {s.users_validated:>6}")
    add("")
    add("SALES")
    add("")
    add(f"  Legacy records:  {s.sales_found:>6}")
    add(f"  Created:         {s.sales_created:>6}")
    add(f"  Unchanged:       {s.sales_unchanged:>6}")
    add(f"  Conflicts:       {s.sales_conflict:>6}")
    add(f"  Failed:          {s.sales_failed:>6}")
    add(f"  Validated:       {s.sales_validated:>6}")
    add("")
    add("REFERENTIAL INTEGRITY")
    add("")
    add(f"  Checked sales:   {s.checked_sales_refs:>6}")
    add(f"  Orphan sales:    {s.orphan_sales:>6}")
    add("")

    if report.divergences:
        add("DIVERGENCES")
        add("")
        for d in report.divergences[:50]:
            if d.field == "__missing__":
                add(f"  {d.entity} {d.legacy_id}: not found in destination service")
            else:
                add(f"  {d.entity} {d.legacy_id}.{d.field}: legacy={d.legacy_value!r} "
                    f"dest={d.destination_value!r}")
        if len(report.divergences) > 50:
            add(f"  ... and {len(report.divergences) - 50} more")
        add("")

    if report.orphan_sales:
        add("ORPHAN SALES")
        add("")
        for o in report.orphan_sales[:50]:
            add(f"  sale_id: {o.sale_id}  user_id: {o.user_id}  reason: {o.reason}")
        if len(report.orphan_sales) > 50:
            add(f"  ... and {len(report.orphan_sales) - 50} more")
        add("")

    add("RESULT")
    add("")
    add(f"  {report.status.value}")
    reasons = report.reasons or ([_COMPLETED_REASON] if report.status == RunStatus.COMPLETED else [])
    if reasons:
        add("")
        add("  Reasons:")
        for r in reasons:
            add(f"  - {r}")
    return "\n".join(lines) + "\n"


def write_files(report: RunReport, report_dir: str) -> tuple[Path, Path]:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    txt_path = directory / f"{report.run_id}.txt"
    json_path = directory / f"{report.run_id}.json"
    txt_path.write_text(render_text(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, default=str), encoding="utf-8"
    )
    return txt_path, json_path
