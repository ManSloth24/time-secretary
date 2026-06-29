from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .briefing_service import BRIEFING_CAPTURE_TYPES, generate_briefing
from .config import Settings
from .inbox_service import add_inbox_item
from .models import Project, SecureCapture
from .project_memory_service import create_project_note, find_project
from .reminder_service import create_reminder_from_text
from .secretary_service import create_time_entry_from_text
from .todo_service import create_todo_from_text
from .utils import ensure_timezone, utcnow
from .work_intelligence_service import (
    create_process_change,
    create_process_observation,
    create_run_metric,
    get_or_create_run,
)


ALLOWED_CAPTURE_TYPES = {
    "work_note",
    "run_change",
    "observation",
    "todo",
    "reminder",
    "project_update",
    "decision",
    "time_entry",
    "run_metric",
    "process_result",
    "briefing_request",
    "report_request",
    "meeting_prep_request",
}


@dataclass(frozen=True)
class SecureCaptureResult:
    capture: SecureCapture
    message: str
    local_url: str | None = None


class SecureCaptureError(RuntimeError):
    pass


def validate_secure_capture_secret(payload_secret: str | None, settings: Settings) -> None:
    if not settings.secure_capture_enabled:
        raise SecureCaptureError("Secure capture is disabled")
    if not settings.secure_capture_token:
        raise SecureCaptureError("Secure capture token is not configured")
    if not payload_secret or payload_secret != settings.secure_capture_token:
        raise SecureCaptureError("Invalid secure capture secret")


def _timestamp(value: str | None, settings: Settings) -> datetime:
    if value:
        try:
            return ensure_timezone(datetime.fromisoformat(value.replace("Z", "+00:00")), settings)
        except ValueError:
            pass
    return datetime.now(settings.timezone)


def _project(session: Session, project_name: str | None) -> Project | None:
    if not project_name:
        return None
    project = find_project(session, project_name)
    if project is None:
        project = Project(name=project_name.strip(), category_default="Work", active=True)
        project.aliases = []
        session.add(project)
        session.flush()
    return project


def _safe_payload(payload: dict[str, Any], settings: Settings) -> str:
    safe = {
        "capture_type": payload.get("capture_type"),
        "project": payload.get("project"),
        "run_name": payload.get("run_name"),
        "source": payload.get("source"),
        "created_at": payload.get("created_at"),
    }
    if settings.log_secure_capture_body:
        safe["text"] = payload.get("text")
    return json.dumps(safe, default=str)


def _payload_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def process_secure_capture(
    session: Session,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> SecureCaptureResult:
    validate_secure_capture_secret(str(payload.get("secret") or ""), settings)

    capture_type = str(payload.get("capture_type") or "work_note").strip()
    if capture_type not in ALLOWED_CAPTURE_TYPES:
        capture_type = "work_note"
    text = str(payload.get("text") or "").strip()
    if not text:
        raise SecureCaptureError("Secure capture text is required")

    timestamp = _timestamp(str(payload.get("created_at") or ""), settings)
    project_name = str(payload.get("project") or "").strip() or None
    run_name = str(payload.get("run_name") or "").strip() or None
    source = str(payload.get("source") or "iphone_shortcut").strip() or "iphone_shortcut"
    project = _project(session, project_name)

    capture = SecureCapture(
        capture_type=capture_type,
        text=text,
        project_id=project.id if project else None,
        project_name=project.name if project else project_name,
        run_name=run_name,
        source=source,
        sensitivity="sensitive",
        processed_status="pending",
        created_at=timestamp,
        received_at=utcnow(),
        raw_payload_json=_safe_payload(payload, settings),
    )
    session.add(capture)
    session.flush()

    message = "Secure capture saved for review."
    try:
        if capture_type in {"work_note", "project_update", "decision"}:
            note_type = "decision" if capture_type == "decision" else "update" if capture_type == "project_update" else "note"
            note = create_project_note(
                session,
                text,
                settings=settings,
                note_type=note_type,
                title=text[:80],
                body=text,
                now=timestamp,
                sensitivity="sensitive",
            )
            if project:
                note.project_id = project.id
                note.project_name = project.name
            capture.linked_project_note_id = note.id
            capture.processed_status = "processed"
            message = f"Secure {note_type} captured."
        elif capture_type == "todo":
            todo = create_todo_from_text(session, text, settings=settings, now=timestamp)
            todo.sensitivity = "sensitive" if hasattr(todo, "sensitivity") else "normal"
            capture.linked_todo_id = todo.id
            capture.processed_status = "processed"
            message = f"Secure todo #{todo.id} captured."
        elif capture_type == "reminder":
            reminder, todo = create_reminder_from_text(session, text, settings=settings, now=timestamp)
            capture.linked_reminder_id = reminder.id
            if todo:
                capture.linked_todo_id = todo.id
            capture.processed_status = "processed"
            message = f"Secure reminder #{reminder.id} captured."
        elif capture_type == "time_entry":
            entry = create_time_entry_from_text(session, text, settings=settings, now=timestamp, source="secure_mobile_capture")
            entry.sensitivity = "sensitive"
            capture.processed_status = "processed"
            message = f"Secure time entry #{entry.id} captured."
        elif capture_type == "run_change":
            change = create_process_change(
                session,
                text,
                settings=settings,
                project_name=project.name if project else project_name,
                run_name=run_name,
                secure_capture_id=capture.id,
                now=timestamp,
            )
            capture.linked_change_id = change.id
            capture.linked_run_id = change.run_id
            capture.processed_status = "processed"
            message = f"Secure process change #{change.id} captured."
        elif capture_type in {"observation", "process_result"}:
            observation = create_process_observation(
                session,
                text,
                settings=settings,
                project_name=project.name if project else project_name,
                run_name=run_name,
                secure_capture_id=capture.id,
                now=timestamp,
            )
            capture.linked_observation_id = observation.id
            capture.linked_run_id = observation.run_id
            capture.processed_status = "processed"
            message = f"Secure observation #{observation.id} captured."
        elif capture_type == "run_metric":
            metric = create_run_metric(
                session,
                text,
                settings=settings,
                project_name=project.name if project else project_name,
                run_name=run_name,
                source="secure_mobile_capture",
                secure_capture_id=capture.id,
                now=timestamp,
            )
            capture.linked_metric_id = metric.id
            capture.linked_run_id = metric.run_id
            capture.processed_status = "processed"
            message = f"Secure metric #{metric.id} captured."
        elif capture_type in BRIEFING_CAPTURE_TYPES:
            briefing_type = "meeting_prep" if capture_type == "meeting_prep_request" else "topic"
            briefing = generate_briefing(
                session,
                text,
                settings=settings,
                request_source="secure_capture",
                briefing_type=briefing_type,
                topic=project.name if project else project_name,
                include_sensitive=_payload_bool(
                    payload.get("include_sensitive"),
                    settings.briefing_include_sensitive_default,
                ),
                created_from_secure_capture_id=capture.id,
                now=timestamp,
            )
            capture.processed_status = "processed" if briefing.report is not None else briefing.request.status
            message = briefing.message
            session.flush()
            return SecureCaptureResult(capture=capture, message=message, local_url=briefing.local_url)
        else:
            inbox = add_inbox_item(
                session,
                text,
                settings=settings,
                interpreted_type="SecureCapture",
                suggested_next_action="Review this sensitive capture locally.",
                confidence=0.2,
                sensitivity="sensitive",
            )
            capture.processed_status = "needs_review"
            message = f"Secure capture #{capture.id} saved for review."
    except Exception as exc:
        capture.processed_status = "failed"
        raise SecureCaptureError(str(exc)) from exc

    session.flush()
    return SecureCaptureResult(capture=capture, message=message)
