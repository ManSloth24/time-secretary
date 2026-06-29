from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .circle_back_service import default_next_review_at
from .classification_service import classify_text, find_project_by_alias
from .config import Settings
from .models import TodoItem
from .natural_date_parser import parse_natural_datetime, strip_date_phrases
from .utils import ensure_timezone, utcnow


PRIORITIES = {"low", "normal", "high", "urgent"}


def _extract_priority(text: str) -> tuple[str, str]:
    low = text.lower().strip()
    for priority in ("urgent", "high", "normal", "low"):
        if low.startswith(priority + " "):
            return priority, text[len(priority) :].strip()
    return "normal", text.strip()


def _clean_title(text: str) -> str:
    title = re.sub(r"^(todo|follow up|follow-up|i need to|need to|deadline)\s*:?\s*", "", text, flags=re.I)
    title = strip_date_phrases(title)
    title = re.sub(r"^\s*to\s+", "", title, flags=re.I)
    return title.strip(" .:-") or text.strip()


def create_todo_from_text(
    session: Session,
    text: str,
    *,
    settings: Settings,
    source_sms_id: int | None = None,
    source_time_entry_id: int | None = None,
    now: datetime | None = None,
    default_priority: str = "normal",
    status: str = "open",
) -> TodoItem:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    priority, body = _extract_priority(text)
    if default_priority in PRIORITIES and priority == "normal":
        priority = default_priority

    due_at = parse_natural_datetime(body, now=now, settings=settings)
    title = _clean_title(body)
    classification = classify_text(session, body)
    project, project_confidence = find_project_by_alias(session, body)

    todo = TodoItem(
        title=title,
        description=None if title == body.strip() else body.strip(),
        status=status,
        priority=priority,
        project_id=project.id if project else None,
        project_name=project.name if project else classification.project_name,
        category_primary=classification.category_primary,
        due_at=due_at,
        remind_at=None,
        next_review_at=due_at
        or default_next_review_at(
            body,
            category_primary=classification.category_primary,
            interpreted_type="Todo",
            settings=settings,
            now=now,
        ),
        needs_followup=True,
        capture_status="captured",
        created_from_sms_id=source_sms_id,
        created_from_time_entry_id=source_time_entry_id,
        created_at=now,
        updated_at=now,
    )
    if todo.category_primary == "Unknown" and project and project.category_default in {"Work", "Home"}:
        todo.category_primary = project.category_default
        if todo.next_review_at is None:
            todo.next_review_at = default_next_review_at(
                body,
                category_primary=todo.category_primary,
                interpreted_type="Todo",
                settings=settings,
                now=now,
            )
    if project_confidence < 0.4 and not todo.project_name:
        todo.project_name = None
    session.add(todo)
    session.flush()
    return todo


def complete_todo(session: Session, query: str | None = None) -> TodoItem | None:
    query = (query or "").strip()
    todo: TodoItem | None = None
    if query.isdigit():
        todo = session.get(TodoItem, int(query))
    if todo is None and query:
        tokens = [token for token in re.split(r"\s+", query.lower()) if token]
        candidates = session.scalars(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]))
            .order_by(TodoItem.created_at.desc())
        ).all()
        for candidate in candidates:
            title = candidate.title.lower()
            if all(token in title for token in tokens):
                todo = candidate
                break
    if todo is None and not query:
        todo = session.scalar(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]))
            .order_by(TodoItem.created_at.desc())
        )
    if todo is None:
        return None
    todo.status = "done"
    todo.capture_status = "completed"
    todo.needs_followup = False
    todo.completed_at = utcnow()
    session.flush()
    return todo


def cancel_todo(session: Session, query: str) -> TodoItem | None:
    todo = complete_todo(session, query)
    if todo is None:
        return None
    todo.status = "canceled"
    todo.capture_status = "dismissed"
    todo.needs_followup = False
    todo.completed_at = utcnow()
    session.flush()
    return todo


def list_open_todos(
    session: Session,
    *,
    category: str | None = None,
    project_name: str | None = None,
    limit: int = 10,
) -> list[TodoItem]:
    conditions = [TodoItem.status.in_(["open", "in_progress", "waiting"])]
    if category:
        conditions.append(TodoItem.category_primary == category)
    if project_name:
        conditions.append(
            or_(
                TodoItem.project_name.ilike(f"%{project_name}%"),
                TodoItem.title.ilike(f"%{project_name}%"),
            )
        )
    return list(
        session.scalars(
            select(TodoItem)
            .where(*conditions)
            .order_by(TodoItem.priority.desc(), TodoItem.due_at.asc(), TodoItem.created_at.desc())
            .limit(limit)
        )
    )


def todo_line(todo: TodoItem, settings: Settings) -> str:
    due = ""
    if todo.due_at:
        due = " due " + ensure_timezone(todo.due_at, settings).strftime("%a %H:%M")
    project = f" [{todo.project_name}]" if todo.project_name else ""
    return f"#{todo.id} {todo.title}{project}{due}"
