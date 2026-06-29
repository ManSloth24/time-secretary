from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .classification_service import find_project_by_alias
from .config import Settings
from .models import AppState, ProjectNote, Reminder, SecretaryInboxItem, TodoItem
from .natural_date_parser import parse_natural_datetime
from .utils import combine_local, ensure_timezone, utcnow


ACTION_LANGUAGE_RE = re.compile(
    r"\b(need to|needs?|look into|check|follow up|circle back|ask|order|update|finish|"
    r"review|investigate|add references|call|email|send|schedule|decide)\b",
    flags=re.I,
)

REVIEW_MODE_KEY = "review_mode_current_inbox_id"

WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class CircleBackContext:
    new_todos: list[TodoItem]
    new_reminders: list[Reminder]
    notes_needing_action: list[ProjectNote]
    project_notes: list[ProjectNote]
    ideas: list[ProjectNote]
    followups: list[ProjectNote]
    decisions: list[ProjectNote]
    unassigned_thoughts: list[SecretaryInboxItem]
    inbox_items: list[SecretaryInboxItem]
    overdue_todos: list[TodoItem]
    snoozed_reminders: list[Reminder]
    stale_todos: list[TodoItem]
    stale_notes: list[ProjectNote]
    stale_inbox_items: list[SecretaryInboxItem]


def has_action_language(text: str) -> bool:
    return ACTION_LANGUAGE_RE.search(text or "") is not None


def next_daily_review(now: datetime, settings: Settings) -> datetime:
    candidate = combine_local(now.date(), settings.daily_report_clock, settings)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_home_review(now: datetime, settings: Settings) -> datetime:
    candidate = combine_local(now.date(), settings.default_evening_clock, settings)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_weekly_review(now: datetime, settings: Settings) -> datetime:
    target = WEEKDAY_INDEX.get(settings.weekly_report_day.lower(), 6)
    days = (target - now.weekday()) % 7
    candidate = combine_local(now.date() + timedelta(days=days), settings.weekly_report_clock, settings)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def default_next_review_at(
    text: str,
    *,
    category_primary: str | None,
    interpreted_type: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> datetime:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    parsed = parse_natural_datetime(text, now=now, settings=settings)
    if parsed:
        return parsed

    kind = (interpreted_type or "").lower()
    if kind in {"idea", "backlog"}:
        return next_weekly_review(now, settings)
    if category_primary == "Home":
        return next_home_review(now, settings)
    return next_daily_review(now, settings)


def mark_reviewed(item, now: datetime | None = None) -> None:
    timestamp = now or utcnow()
    if hasattr(item, "last_reviewed_at"):
        item.last_reviewed_at = timestamp
    if hasattr(item, "reviewed_at"):
        item.reviewed_at = timestamp
    if hasattr(item, "review_count"):
        item.review_count = (item.review_count or 0) + 1
    if hasattr(item, "capture_status") and item.capture_status == "captured":
        item.capture_status = "reviewed"


def open_inbox_items(session: Session, limit: int = 20) -> list[SecretaryInboxItem]:
    return list(
        session.scalars(
            select(SecretaryInboxItem)
            .where(SecretaryInboxItem.status == "open")
            .order_by(SecretaryInboxItem.next_review_at.asc(), SecretaryInboxItem.created_at.asc())
            .limit(limit)
        )
    )


def current_review_item(session: Session) -> SecretaryInboxItem | None:
    state = session.get(AppState, REVIEW_MODE_KEY)
    if not state:
        return None
    try:
        item_id = int(state.value)
    except ValueError:
        return None
    item = session.get(SecretaryInboxItem, item_id)
    if item is None or item.status != "open":
        return None
    return item


def set_current_review_item(session: Session, item: SecretaryInboxItem | None) -> None:
    state = session.get(AppState, REVIEW_MODE_KEY)
    if item is None:
        if state:
            session.delete(state)
        session.flush()
        return
    if state is None:
        state = AppState(key=REVIEW_MODE_KEY, value=str(item.id))
        session.add(state)
    else:
        state.value = str(item.id)
    session.flush()


def format_review_item(session: Session, item: SecretaryInboxItem) -> str:
    items = open_inbox_items(session, limit=500)
    index = next((idx for idx, candidate in enumerate(items, start=1) if candidate.id == item.id), 1)
    total = max(len(items), 1)
    suggested = item.interpreted_type or item.suggested_type or "Review"
    if item.suggested_project_name:
        suggested += f", {item.suggested_project_name}"
    return (
        f"Inbox item {index}/{total}:\n"
        f"'{item.raw_text}'\n"
        f"Suggested: {suggested}.\n"
        "Reply: todo, remind tomorrow, assign [project], dismiss, or next."
    )


def start_inbox_review(session: Session) -> str:
    item = open_inbox_items(session, limit=1)
    if not item:
        set_current_review_item(session, None)
        return "Inbox is clear."
    set_current_review_item(session, item[0])
    return format_review_item(session, item[0])


def advance_inbox_review(session: Session) -> str:
    current = current_review_item(session)
    items = open_inbox_items(session, limit=500)
    if not items:
        set_current_review_item(session, None)
        return "Inbox is clear."
    if current is None:
        set_current_review_item(session, items[0])
        return format_review_item(session, items[0])
    next_item = None
    for idx, item in enumerate(items):
        if item.id == current.id and idx + 1 < len(items):
            next_item = items[idx + 1]
            break
    if next_item is None:
        set_current_review_item(session, None)
        return "Inbox review complete."
    set_current_review_item(session, next_item)
    return format_review_item(session, next_item)


def get_circle_back_context(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    settings: Settings,
    now: datetime | None = None,
) -> CircleBackContext:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    review_cutoff = now
    new_todos = list(
        session.scalars(
            select(TodoItem)
            .where(TodoItem.created_at >= start, TodoItem.created_at < end)
            .order_by(TodoItem.created_at.asc())
        )
    )
    new_reminders = list(
        session.scalars(
            select(Reminder)
            .where(Reminder.created_at >= start, Reminder.created_at < end)
            .order_by(Reminder.created_at.asc())
        )
    )
    notes = list(
        session.scalars(
            select(ProjectNote)
            .where(ProjectNote.created_at >= start, ProjectNote.created_at < end)
            .order_by(ProjectNote.created_at.asc())
        )
    )
    inbox_items = list(
        session.scalars(
            select(SecretaryInboxItem)
            .where(SecretaryInboxItem.status == "open")
            .order_by(SecretaryInboxItem.next_review_at.asc(), SecretaryInboxItem.created_at.asc())
        )
    )
    overdue_todos = list(
        session.scalars(
            select(TodoItem)
            .where(
                TodoItem.status.in_(["open", "in_progress", "waiting"]),
                TodoItem.due_at.is_not(None),
                TodoItem.due_at < now,
            )
            .order_by(TodoItem.due_at.asc())
        )
    )
    snoozed_reminders = list(
        session.scalars(
            select(Reminder)
            .where(or_(Reminder.status == "snoozed", Reminder.snooze_count > 0))
            .order_by(Reminder.snooze_count.desc(), Reminder.remind_at.asc())
        )
    )
    stale_todos = list(
        session.scalars(
            select(TodoItem)
            .where(
                TodoItem.status.in_(["open", "in_progress", "waiting"]),
                TodoItem.next_review_at.is_not(None),
                TodoItem.next_review_at <= review_cutoff,
            )
            .order_by(TodoItem.next_review_at.asc())
        )
    )
    stale_notes = list(
        session.scalars(
            select(ProjectNote)
            .where(
                ProjectNote.capture_status.in_(["captured", "reviewed"]),
                ProjectNote.next_review_at.is_not(None),
                ProjectNote.next_review_at <= review_cutoff,
            )
            .order_by(ProjectNote.next_review_at.asc())
        )
    )
    stale_inbox_items = [
        item for item in inbox_items if item.next_review_at and ensure_timezone(item.next_review_at, settings) <= review_cutoff
    ]
    return CircleBackContext(
        new_todos=new_todos,
        new_reminders=new_reminders,
        notes_needing_action=[
            note for note in notes if note.needs_followup and not note.linked_todo_id and not note.linked_reminder_id
        ],
        project_notes=notes,
        ideas=[note for note in notes if note.note_type == "idea"],
        followups=[note for note in notes if note.note_type == "follow_up"],
        decisions=[note for note in notes if note.note_type == "decision"],
        unassigned_thoughts=[item for item in inbox_items if not item.suggested_project_name],
        inbox_items=inbox_items,
        overdue_todos=overdue_todos,
        snoozed_reminders=snoozed_reminders,
        stale_todos=stale_todos,
        stale_notes=stale_notes,
        stale_inbox_items=stale_inbox_items,
    )


def suggest_inbox_metadata(session: Session, text: str) -> dict[str, object]:
    project, confidence = find_project_by_alias(session, text)
    title = re.sub(r"^(capture|note|idea|circle back|follow up)\s*:?\s*", "", text, flags=re.I).strip()
    return {
        "suggested_project_id": project.id if project else None,
        "suggested_project_name": project.name if project else None,
        "suggested_title": title[:240] if title else text[:240],
        "confidence": max(0.2, confidence),
    }
