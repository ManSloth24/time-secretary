from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .circle_back_service import get_circle_back_context
from .config import Settings
from .models import CheckinPrompt, ProcessChange, ProcessObservation, ProjectNote, Reminder, ReportRun, RunMetric, SecureCapture, TimeEntry, TodoItem
from .utils import duration_minutes, ensure_timezone, simple_slug, utcnow
from .work_hours_service import totals_for_period
from .work_intelligence_service import summarize_work_intelligence


TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "reports"


@dataclass(frozen=True)
class ReportResult:
    report_type: str
    period_start: datetime
    period_end: datetime
    summary_text: str
    markdown: str
    path: str | None


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _period(report_type: str, now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    now = ensure_timezone(now, settings)
    start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if report_type == "daily":
        return start_day, start_day + timedelta(days=1)
    if report_type == "weekly":
        start = start_day - timedelta(days=start_day.weekday())
        return start, start + timedelta(days=7)
    if report_type == "monthly":
        start = start_day.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end
    if report_type in {"yearly", "year", "ytd"}:
        return start_day.replace(month=1, day=1), start_day + timedelta(days=1)
    raise ValueError(f"Unknown report type: {report_type}")


def _entries(session: Session, start: datetime, end: datetime) -> list[TimeEntry]:
    return list(
        session.scalars(
            select(TimeEntry)
            .where(TimeEntry.interval_start >= start, TimeEntry.interval_start < end)
            .order_by(TimeEntry.interval_start.asc())
        )
    )


def _todos(session: Session, start: datetime, end: datetime) -> list[TodoItem]:
    return list(
        session.scalars(
            select(TodoItem)
            .where(TodoItem.created_at >= start, TodoItem.created_at < end)
            .order_by(TodoItem.created_at.asc())
        )
    )


def _all_open_or_overdue_todos(session: Session, end: datetime) -> list[TodoItem]:
    return list(
        session.scalars(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]), TodoItem.created_at < end)
            .order_by(TodoItem.due_at.asc(), TodoItem.created_at.asc())
        )
    )


def _reminders(session: Session, start: datetime, end: datetime) -> list[Reminder]:
    return list(
        session.scalars(
            select(Reminder)
            .where(Reminder.remind_at >= start, Reminder.remind_at < end)
            .order_by(Reminder.remind_at.asc())
        )
    )


def _notes(session: Session, start: datetime, end: datetime) -> list[ProjectNote]:
    return list(
        session.scalars(
            select(ProjectNote)
            .where(ProjectNote.created_at >= start, ProjectNote.created_at < end)
            .order_by(ProjectNote.created_at.asc())
        )
    )


def _secure_captures(session: Session, start: datetime, end: datetime) -> list[SecureCapture]:
    return list(
        session.scalars(
            select(SecureCapture)
            .where(SecureCapture.created_at >= start, SecureCapture.created_at < end)
            .order_by(SecureCapture.created_at.asc())
        )
    )


def _build_context(
    session: Session,
    report_type: str,
    start: datetime,
    end: datetime,
    settings: Settings,
    now: datetime,
) -> dict[str, object]:
    entries = _entries(session, start, end)
    todos_created = _todos(session, start, end)
    open_todos = _all_open_or_overdue_todos(session, end)
    reminders = _reminders(session, start, end + timedelta(days=7 if report_type == "daily" else 0))
    notes = _notes(session, start, end)
    secure_captures = _secure_captures(session, start, end)
    work_period = "today" if report_type == "daily" else "week" if report_type == "weekly" else "month" if report_type == "monthly" else "year"
    work_hours = totals_for_period(session, work_period, settings=settings, now=now)
    intelligence = summarize_work_intelligence(session, settings=settings, start=start, end=end)

    minutes_by_primary: Counter[str] = Counter()
    minutes_by_secondary: Counter[str] = Counter()
    minutes_by_project: Counter[str] = Counter()
    timeline = []
    for entry in entries:
        minutes = duration_minutes(entry.interval_start, entry.interval_end)
        minutes_by_primary[entry.category_primary] += minutes
        minutes_by_secondary[entry.category_secondary] += minutes
        if entry.project_name:
            minutes_by_project[entry.project_name] += minutes
        timeline.append(
            {
                "time": f"{ensure_timezone(entry.interval_start, settings).strftime('%H:%M')}-{ensure_timezone(entry.interval_end, settings).strftime('%H:%M')}",
                "text": entry.raw_text,
                "category": f"{entry.category_primary}/{entry.category_secondary}",
                "project": entry.project_name or "",
            }
        )

    work_entries = [entry for entry in entries if entry.category_primary == "Work"]
    first_work_entry = work_entries[0] if work_entries else None
    last_work_entry = work_entries[-1] if work_entries else None
    commute_minutes = sum(
        duration_minutes(entry.interval_start, entry.interval_end)
        for entry in entries
        if entry.category_secondary in {"commute_to_work", "commute_from_work", "commute_home"}
    )
    context_switches = 0
    previous_key = None
    for entry in entries:
        key = entry.project_name or entry.category_secondary or entry.category_primary
        if previous_key is not None and key != previous_key:
            context_switches += 1
        previous_key = key
    unassigned_work_entries = [
        entry for entry in work_entries if not entry.project_name and entry.category_secondary != "commute_to_work"
    ]

    total_minutes = sum(minutes_by_primary.values())
    missed_count = session.scalar(
        select(func.count())
        .select_from(CheckinPrompt)
        .where(
            CheckinPrompt.status == "missed",
            CheckinPrompt.scheduled_for_start >= start,
            CheckinPrompt.scheduled_for_start < end,
        )
    ) or 0
    completed_todos = [
        todo
        for todo in session.scalars(
            select(TodoItem)
            .where(TodoItem.completed_at >= start, TodoItem.completed_at < end)
            .order_by(TodoItem.completed_at.asc())
        )
    ]
    overdue_todos = [
        todo for todo in open_todos if todo.due_at and ensure_timezone(todo.due_at, settings) < end
    ]
    unassigned_notes = [note for note in notes if not note.project_name]
    decisions = [note for note in notes if note.note_type == "decision"]
    action_items = [note for note in notes if note.note_type == "action_item"]
    process_changes = list(session.scalars(select(ProcessChange).where(ProcessChange.occurred_at >= start, ProcessChange.occurred_at < end).order_by(ProcessChange.occurred_at.asc())))
    process_observations = list(session.scalars(select(ProcessObservation).where(ProcessObservation.observed_at >= start, ProcessObservation.observed_at < end).order_by(ProcessObservation.observed_at.asc())))
    run_metrics = list(session.scalars(select(RunMetric).where(RunMetric.created_at >= start, RunMetric.created_at < end).order_by(RunMetric.created_at.asc())))
    circle_back = get_circle_back_context(session, start=start, end=end, settings=settings, now=now)
    captured_count = (
        len(circle_back.new_todos)
        + len(circle_back.new_reminders)
        + len(circle_back.project_notes)
        + len(circle_back.inbox_items)
    )

    narrative = (
        f"Tracked {total_minutes / 60:.1f} hours across {len(entries)} entries. "
        f"Work was {minutes_by_primary['Work'] / 60:.1f} hours, "
        f"home was {minutes_by_primary['Home'] / 60:.1f} hours, "
        f"and unknown was {minutes_by_primary['Unknown'] / 60:.1f} hours."
    )
    if completed_todos:
        narrative += f" Completed {len(completed_todos)} todo(s)."
    if overdue_todos:
        narrative += f" {len(overdue_todos)} todo(s) need attention."
    if report_type == "daily" and captured_count:
        narrative += (
            f" You captured {captured_count} item(s) today: "
            f"{len(circle_back.new_todos)} todos, {len(circle_back.project_notes)} project notes, "
            f"{len(circle_back.ideas)} ideas, {len(circle_back.inbox_items)} inbox items. "
            "Reply 'review inbox' or 'agenda tomorrow'."
        )
    if secure_captures:
        narrative += f" {len(secure_captures)} secure note(s) captured. Review dashboard."

    return {
        "report_type": report_type,
        "period_start": start,
        "period_end": end,
        "period_end_inclusive": end - timedelta(days=1),
        "entries": entries,
        "timeline": timeline,
        "total_hours": total_minutes / 60,
        "minutes_by_primary": minutes_by_primary,
        "minutes_by_secondary": minutes_by_secondary,
        "minutes_by_project": minutes_by_project,
        "todos_created": todos_created,
        "completed_todos": completed_todos,
        "open_todos": open_todos,
        "overdue_todos": overdue_todos,
        "reminders": reminders,
        "notes": notes,
        "secure_captures": secure_captures,
        "decisions": decisions,
        "action_items": action_items,
        "process_changes": process_changes,
        "process_observations": process_observations,
        "run_metrics": run_metrics,
        "work_hours": work_hours,
        "work_intelligence": intelligence,
        "unassigned_notes": unassigned_notes,
        "unassigned_work_entries": unassigned_work_entries,
        "first_work_entry": first_work_entry,
        "last_work_entry": last_work_entry,
        "commute_minutes": commute_minutes,
        "context_switches": context_switches,
        "circle_back": circle_back,
        "captured_count": captured_count,
        "missed_count": missed_count,
        "narrative": narrative,
        "settings": settings,
    }


def generate_report(
    session: Session,
    report_type: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> ReportResult:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    start, end = _period(report_type, now, settings)
    context = _build_context(session, report_type, start, end, settings, now)
    template_name = "yearly.md.j2" if report_type in {"year", "ytd", "yearly"} else f"{report_type}.md.j2"
    template = _env().get_template(template_name)
    markdown = template.render(**context)
    summary = str(context["narrative"])

    path = None
    if settings.save_reports_to_disk:
        reports_dir = Path(settings.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{start.date()}-{simple_slug(report_type)}.md"
        if report_type == "monthly":
            filename = f"{start:%Y-%m}-{simple_slug(report_type)}.md"
        elif report_type == "weekly":
            filename = f"{start.date()}-to-{(end - timedelta(days=1)).date()}-{simple_slug(report_type)}.md"
        elif report_type in {"year", "ytd", "yearly"}:
            filename = f"{start:%Y}-ytd.md"
        report_path = reports_dir / filename
        report_path.write_text(markdown, encoding="utf-8")
        path = str(report_path)

    run = ReportRun(
        report_type=report_type,
        period_start=start,
        period_end=end,
        generated_at=utcnow(),
        summary_text=summary,
        report_path=path,
        sent_by_sms=False,
    )
    session.add(run)
    session.flush()
    return ReportResult(report_type, start, end, summary, markdown, path)


def project_allocations(entries: list[TimeEntry]) -> dict[str, float]:
    totals: defaultdict[str, int] = defaultdict(int)
    for entry in entries:
        if entry.project_name:
            totals[entry.project_name] += duration_minutes(entry.interval_start, entry.interval_end)
    return {name: minutes / 60 for name, minutes in sorted(totals.items())}
