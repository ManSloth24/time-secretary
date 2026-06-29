from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from pathlib import Path

import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.models import (
    BriefingReport,
    BriefingRequest,
    LLMCall,
    LLMReportCache,
    LocationEvent,
    LocationPlace,
    ProcessChange,
    ProcessObservation,
    Project,
    ProjectNote,
    Reminder,
    RunMetric,
    RunRecord,
    SecureCapture,
    SecretaryInboxItem,
    TimeEntry,
    TodoItem,
    WorkDaySummary,
)
from time_secretary.utils import duration_minutes


EXPORT_MODELS = [
    ("time_entries.csv", TimeEntry),
    ("todos.csv", TodoItem),
    ("reminders.csv", Reminder),
    ("project_notes.csv", ProjectNote),
    ("secretary_inbox.csv", SecretaryInboxItem),
    ("projects.csv", Project),
    ("llm_calls.csv", LLMCall),
    ("location_places.csv", LocationPlace),
    ("location_events.csv", LocationEvent),
    ("work_day_summaries.csv", WorkDaySummary),
    ("run_records.csv", RunRecord),
    ("process_changes.csv", ProcessChange),
    ("process_observations.csv", ProcessObservation),
    ("run_metrics.csv", RunMetric),
    ("briefing_requests.csv", BriefingRequest),
]


def _resolve_from_root(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


def _csv_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _write_model_csv(session, model, path: Path) -> None:
    columns = [column.key for column in model.__table__.columns]
    rows = session.scalars(select(model)).all()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_value(getattr(row, column)) for column in columns])


def _write_secure_captures_csv(session, path: Path, *, include_sensitive: bool) -> None:
    columns = [column.key for column in SecureCapture.__table__.columns]
    rows = session.scalars(select(SecureCapture)).all()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            values = []
            for column in columns:
                value = getattr(row, column)
                if column == "text" and not include_sensitive:
                    value = "[redacted sensitive text]"
                if column == "raw_payload_json" and not include_sensitive:
                    value = ""
                values.append(_csv_value(value))
            writer.writerow(values)


def _write_briefing_reports_csv(session, path: Path, *, include_sensitive: bool) -> None:
    columns = [column.key for column in BriefingReport.__table__.columns]
    rows = session.scalars(select(BriefingReport)).all()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            values = []
            for column in columns:
                value = getattr(row, column)
                if column == "full_text" and row.includes_sensitive and not include_sensitive:
                    value = "[redacted sensitive briefing]"
                values.append(_csv_value(value))
            writer.writerow(values)


def _write_llm_report_cache_csv(session, path: Path, *, include_sensitive: bool) -> None:
    columns = [column.key for column in LLMReportCache.__table__.columns]
    rows = session.scalars(select(LLMReportCache)).all()
    sensitive_columns = {"fact_pack_json", "narrative_text", "structured_json"}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            values = []
            for column in columns:
                value = getattr(row, column)
                if column in sensitive_columns and not include_sensitive:
                    value = "[redacted llm report cache]"
                values.append(_csv_value(value))
            writer.writerow(values)


def _write_project_time_csv(session, path: Path) -> None:
    entries = session.scalars(select(TimeEntry).where(TimeEntry.category_primary == "Work")).all()
    totals: dict[str, int] = {}
    for entry in entries:
        project = entry.project_name or "Unassigned Work"
        totals[project] = totals.get(project, 0) + duration_minutes(entry.interval_start, entry.interval_end)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["project", "minutes", "hours"])
        for project, minutes in sorted(totals.items(), key=lambda item: item[1], reverse=True):
            writer.writerow([project, minutes, f"{minutes / 60:.2f}"])


def _write_work_focus_csv(session, path: Path) -> None:
    entries = session.scalars(select(TimeEntry).where(TimeEntry.category_primary == "Work")).all()
    totals: dict[str, int] = {}
    for entry in entries:
        focus = entry.work_focus_type or "unclassified"
        totals[focus] = totals.get(focus, 0) + duration_minutes(entry.interval_start, entry.interval_end)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["work_focus_type", "minutes", "hours"])
        for focus, minutes in sorted(totals.items(), key=lambda item: item[1], reverse=True):
            writer.writerow([focus, minutes, f"{minutes / 60:.2f}"])


def _write_entry_subset_csv(session, path: Path, *, flag: str) -> None:
    rows = session.scalars(select(TimeEntry).where(getattr(TimeEntry, flag).is_(True))).all()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "interval_start", "raw_text", "project_name", "work_focus_type", "reason"])
        reason_field = "delegation_reason" if flag == "delegation_candidate" else "staffing_signal_reason"
        for row in rows:
            writer.writerow([row.id, _csv_value(row.interval_start), row.raw_text, row.project_name or "", row.work_focus_type or "", getattr(row, reason_field) or ""])


def _write_staffing_summary(session, path: Path) -> None:
    rows = session.scalars(select(TimeEntry).where(TimeEntry.staffing_signal.is_(True))).all()
    lines = ["# Staffing Justification Summary", ""]
    if not rows:
        lines.append("No staffing signals recorded yet.")
    for row in rows:
        lines.append(f"- {row.raw_text} ({row.project_name or 'Unassigned Work'}): {row.staffing_signal_reason or 'staffing signal'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_data(
    *,
    settings: Settings | None = None,
    output_dir: Path | None = None,
) -> list[Path]:
    settings = settings or Settings.from_env(ROOT_DIR / ".env")
    output_dir = output_dir or (_resolve_from_root(settings.backups_dir) / "exports")
    output_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine_from_url(settings.database_url)
    init_db(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    paths: list[Path] = []
    try:
        with SessionLocal() as session:
            for filename, model in EXPORT_MODELS:
                path = output_dir / filename
                _write_model_csv(session, model, path)
                paths.append(path)
            secure_path = output_dir / "secure_captures.csv"
            _write_secure_captures_csv(
                session,
                secure_path,
                include_sensitive=settings.export_include_sensitive,
            )
            paths.append(secure_path)
            briefing_reports_path = output_dir / "briefing_reports.csv"
            _write_briefing_reports_csv(
                session,
                briefing_reports_path,
                include_sensitive=settings.export_include_sensitive,
            )
            paths.append(briefing_reports_path)
            llm_report_cache_path = output_dir / "llm_report_cache.csv"
            _write_llm_report_cache_csv(
                session,
                llm_report_cache_path,
                include_sensitive=settings.export_include_sensitive,
            )
            paths.append(llm_report_cache_path)
            extras = [
                ("project_time.csv", _write_project_time_csv),
                ("work_focus.csv", _write_work_focus_csv),
            ]
            for filename, writer in extras:
                path = output_dir / filename
                writer(session, path)
                paths.append(path)
            delegation_path = output_dir / "delegation_candidates.csv"
            _write_entry_subset_csv(session, delegation_path, flag="delegation_candidate")
            paths.append(delegation_path)
            staffing_path = output_dir / "staffing_signals.csv"
            _write_entry_subset_csv(session, staffing_path, flag="staffing_signal")
            paths.append(staffing_path)
            summary_path = output_dir / "staffing_justification_summary.md"
            _write_staffing_summary(session, summary_path)
            paths.append(summary_path)
    finally:
        engine.dispose()
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Time Secretary data to CSV files.")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    settings = Settings.from_env(args.env_file)
    paths = export_data(
        settings=settings,
        output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
    )
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
