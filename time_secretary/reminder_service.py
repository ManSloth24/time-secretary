from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .classification_service import find_project_by_alias
from .config import Settings
from .models import Reminder, TodoItem
from .natural_date_parser import parse_natural_datetime, strip_date_phrases
from .todo_service import create_todo_from_text
from .utils import ensure_timezone, parse_duration, utcnow


def _reminder_title(text: str) -> str:
    body = text.strip()
    remind_match = re.search(r"\bremind me\b(.+?)\bto\b(.+)$", body, flags=re.I)
    if remind_match:
        return strip_date_phrases(remind_match.group(2)).strip(" .:-")
    ask_match = re.search(r"\bask me\b(.+?)\bif\b(.+)$", body, flags=re.I)
    if ask_match:
        return "Ask: " + strip_date_phrases(ask_match.group(2)).strip(" .:-")
    body = re.sub(r"^(reminder|remind me|ask me)\s*:?\s*", "", body, flags=re.I)
    return strip_date_phrases(body).strip(" .:-") or text.strip()


def create_reminder_from_text(
    session: Session,
    text: str,
    *,
    settings: Settings,
    source_sms_id: int | None = None,
    now: datetime | None = None,
    create_related_todo: bool = True,
) -> tuple[Reminder, TodoItem | None]:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    remind_at = parse_natural_datetime(text, now=now, settings=settings)
    if remind_at is None:
        remind_at = now + timedelta(hours=1)

    title = _reminder_title(text)
    project, _ = find_project_by_alias(session, text)
    todo = None
    if create_related_todo and title:
        todo = create_todo_from_text(
            session,
            title,
            settings=settings,
            source_sms_id=source_sms_id,
            now=now,
            default_priority="normal",
        )
        todo.remind_at = remind_at
        todo.next_review_at = remind_at

    reminder = Reminder(
        title=title,
        body=text.strip(),
        remind_at=remind_at,
        status="scheduled",
        next_review_at=remind_at,
        capture_status="scheduled",
        related_todo_id=todo.id if todo else None,
        related_project_id=project.id if project else None,
        created_from_sms_id=source_sms_id,
        created_at=now,
        updated_at=now,
    )
    session.add(reminder)
    session.flush()
    return reminder, todo


def due_reminders(session: Session, now: datetime) -> list[Reminder]:
    return list(
        session.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed"]), Reminder.remind_at <= now)
            .order_by(Reminder.remind_at.asc())
        )
    )


def recent_active_reminder(session: Session) -> Reminder | None:
    return session.scalar(
        select(Reminder)
        .where(Reminder.status.in_(["sent", "snoozed", "scheduled"]))
        .order_by(Reminder.sent_at.desc(), Reminder.remind_at.desc())
    )


def mark_reminder_sent(session: Session, reminder: Reminder, sent_at: datetime | None = None) -> None:
    reminder.status = "sent"
    reminder.capture_status = "sent"
    reminder.sent_at = sent_at or utcnow()
    session.flush()


def complete_reminder(session: Session, reminder: Reminder | None = None) -> Reminder | None:
    reminder = reminder or recent_active_reminder(session)
    if reminder is None:
        return None
    reminder.status = "done"
    reminder.capture_status = "completed"
    if reminder.related_todo_id:
        todo = session.get(TodoItem, reminder.related_todo_id)
        if todo:
            todo.status = "done"
            todo.completed_at = utcnow()
    session.flush()
    return reminder


def cancel_reminder(session: Session, reminder_id: int | None = None) -> Reminder | None:
    reminder = session.get(Reminder, reminder_id) if reminder_id else recent_active_reminder(session)
    if reminder is None:
        return None
    reminder.status = "canceled"
    reminder.capture_status = "dismissed"
    session.flush()
    return reminder


def snooze_reminder(
    session: Session,
    *,
    settings: Settings,
    reminder_id: int | None = None,
    duration_text: str | None = None,
    until_text: str | None = None,
    now: datetime | None = None,
) -> Reminder | None:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    reminder = session.get(Reminder, reminder_id) if reminder_id else recent_active_reminder(session)
    if reminder is None:
        return None

    remind_at = None
    if duration_text:
        duration = parse_duration(duration_text)
        if duration:
            remind_at = now + duration
    if remind_at is None and until_text:
        remind_at = parse_natural_datetime(until_text, now=now, settings=settings)
    if remind_at is None:
        remind_at = now + timedelta(minutes=30)

    reminder.remind_at = remind_at
    reminder.next_review_at = remind_at
    reminder.status = "snoozed"
    reminder.capture_status = "snoozed"
    reminder.snooze_count = (reminder.snooze_count or 0) + 1
    session.flush()
    return reminder


def reminder_line(reminder: Reminder, settings: Settings) -> str:
    when = ensure_timezone(reminder.remind_at, settings).strftime("%a %H:%M")
    return f"#{reminder.id} {reminder.title} at {when}"
