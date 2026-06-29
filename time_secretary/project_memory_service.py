from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .circle_back_service import default_next_review_at, has_action_language
from .classification_service import find_project_by_alias
from .config import Settings
from .models import Project, ProjectNote, ProjectStatusSnapshot, Reminder, TimeEntry, TodoItem
from .utils import duration_minutes, ensure_timezone, utcnow


def find_project(session: Session, name_or_alias: str) -> Project | None:
    project = session.scalar(select(Project).where(Project.name.ilike(name_or_alias.strip())))
    if project:
        return project
    project, _ = find_project_by_alias(session, name_or_alias)
    return project


def create_project_note(
    session: Session,
    raw_text: str,
    *,
    settings: Settings | None = None,
    note_type: str = "note",
    title: str | None = None,
    body: str | None = None,
    source_sms_id: int | None = None,
    now: datetime | None = None,
    sensitivity: str = "normal",
) -> ProjectNote:
    project, _ = find_project_by_alias(session, raw_text)
    settings = settings or Settings()
    clean_body = body or raw_text.strip()
    clean_title = title or clean_body[:80]
    timestamp = now or utcnow()
    category = project.category_default if project else "Unknown"
    followup = has_action_language(raw_text) or note_type in {"follow_up", "action_item", "risk"}
    note = ProjectNote(
        project_id=project.id if project else None,
        project_name=project.name if project else None,
        note_type=note_type,
        title=clean_title,
        body=clean_body,
        raw_text=raw_text,
        created_from_sms_id=source_sms_id,
        needs_followup=followup,
        capture_status="captured",
        next_review_at=default_next_review_at(
            raw_text,
            category_primary=category,
            interpreted_type=note_type,
            settings=settings,
            now=timestamp,
        ),
        sensitivity=sensitivity,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(note)
    session.flush()
    return note


def summarize_project(session: Session, project: Project, settings: Settings, now: datetime | None = None) -> str:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    entries = session.scalars(
        select(TimeEntry)
        .where(TimeEntry.project_name == project.name)
        .order_by(TimeEntry.interval_start.desc())
        .limit(10)
    ).all()
    todos = session.scalars(
        select(TodoItem)
        .where(TodoItem.project_name == project.name, TodoItem.status.in_(["open", "in_progress", "waiting"]))
        .order_by(TodoItem.created_at.desc())
        .limit(10)
    ).all()
    notes = session.scalars(
        select(ProjectNote)
        .where(ProjectNote.project_name == project.name)
        .order_by(ProjectNote.created_at.desc())
        .limit(5)
    ).all()
    reminders = session.scalars(
        select(Reminder)
        .where(Reminder.related_project_id == project.id, Reminder.status.in_(["scheduled", "snoozed", "sent"]))
        .order_by(Reminder.remind_at.asc())
        .limit(5)
    ).all()

    minutes = sum(duration_minutes(entry.interval_start, entry.interval_end) for entry in entries)
    last_activity = None
    for value in [*(entry.interval_end for entry in entries), *(todo.created_at for todo in todos), *(note.created_at for note in notes)]:
        if value:
            local_value = ensure_timezone(value, settings)
            if last_activity is None or local_value > last_activity:
                last_activity = local_value

    lines = [
        f"{project.name}: {minutes / 60:.1f} recent tracked hours.",
        f"Open todos: {len(todos)}. Upcoming reminders: {len(reminders)}. Recent notes: {len(notes)}.",
    ]
    if todos:
        lines.append("Next actions: " + "; ".join(todo.title for todo in todos[:3]))
    if notes:
        lines.append("Recent context: " + "; ".join(note.title for note in notes[:3]))
    if last_activity:
        lines.append("Last activity: " + ensure_timezone(last_activity, settings).strftime("%Y-%m-%d %H:%M"))

    overdue_count = sum(1 for todo in todos if todo.due_at and ensure_timezone(todo.due_at, settings) < now)
    snapshot = ProjectStatusSnapshot(
        project_id=project.id,
        status_summary="\n".join(lines),
        open_todos_count=len(todos),
        overdue_todos_count=overdue_count,
        last_activity_at=last_activity,
        generated_at=utcnow(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot.status_summary


def project_time_totals(session: Session) -> list[tuple[str, int]]:
    rows = session.execute(
        select(TimeEntry.project_name, func.count(TimeEntry.id))
        .where(TimeEntry.project_name.is_not(None))
        .group_by(TimeEntry.project_name)
    ).all()
    return [(name, count) for name, count in rows]
