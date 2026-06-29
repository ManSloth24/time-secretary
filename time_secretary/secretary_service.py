from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .briefing_service import generate_briefing, is_briefing_request_text, sms_safe_briefing_reply
from .circle_back_service import (
    advance_inbox_review,
    current_review_item,
    default_next_review_at,
    get_circle_back_context,
    has_action_language,
    next_weekly_review,
    set_current_review_item,
    start_inbox_review,
)
from .classification_service import (
    add_project,
    add_project_aliases,
    classify_text,
)
from .command_parser import ParsedCommand, parse_command
from .config import Settings
from .inbox_service import add_inbox_item, dismiss_inbox_item, resolve_inbox_item
from .llm import llm_service
from .llm.schemas import SmsIntent
from .location_service import (
    current_location_category,
    get_or_create_place,
    list_places,
    location_status,
    parse_location_command,
    record_location_event,
    upsert_manual_location_event_for_day,
)
from .models import CheckinPrompt, ClassificationRule, ProjectNote, Reminder, SecretaryInboxItem, TimeEntry, TodoItem
from .natural_date_parser import parse_natural_datetime, strip_date_phrases
from .project_memory_service import create_project_note, find_project, summarize_project
from .reminder_service import (
    cancel_reminder,
    complete_reminder,
    create_reminder_from_text,
    reminder_line,
    snooze_reminder,
)
from .report_service import generate_report
from .scheduler_service import PAUSE_KEY, clear_state, pause_status_text, set_state
from .todo_service import complete_todo, create_todo_from_text, list_open_todos, todo_line
from .utils import ensure_timezone, human_dt, interval_for_now, parse_duration, utcnow
from .work_focus_service import apply_work_focus
from .work_hours_service import generate_work_day_summary, work_hours_summary_text
from .work_intelligence_service import create_process_change, create_process_observation, create_run_metric


@dataclass
class ProcessResult:
    reply: str
    created: dict[str, list[int]] = field(default_factory=dict)

    def add(self, key: str, value: int) -> None:
        self.created.setdefault(key, []).append(value)


def _latest_open_prompt(session: Session, now: datetime, settings: Settings) -> CheckinPrompt | None:
    cutoff = now - timedelta(minutes=max(settings.checkin_grace_minutes, settings.checkin_interval_minutes * 4))
    return session.scalar(
        select(CheckinPrompt)
        .where(
            CheckinPrompt.status.in_(["pending", "sent"]),
            CheckinPrompt.scheduled_for_end <= now,
            CheckinPrompt.scheduled_for_end >= cutoff,
        )
        .order_by(CheckinPrompt.scheduled_for_end.desc())
    )


def create_time_entry_from_text(
    session: Session,
    text: str,
    *,
    settings: Settings,
    now: datetime | None = None,
    source: str = "sms",
) -> TimeEntry:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    prompt = _latest_open_prompt(session, now, settings)
    if prompt:
        interval_start = ensure_timezone(prompt.scheduled_for_start, settings)
        interval_end = ensure_timezone(prompt.scheduled_for_end, settings)
        prompt.status = "answered"
    else:
        interval_start, interval_end = interval_for_now(now, settings.checkin_interval_minutes)

    classification = classify_text(session, text)
    category_primary = classification.category_primary
    category_secondary = classification.category_secondary
    if classification.classification_confidence < 0.55:
        location_category = current_location_category(session)
        normalized = text.strip().lower()
        if normalized == "lunch" and location_category == "work":
            category_primary = "Work"
            category_secondary = "lunch_at_work"
        elif normalized == "lunch" and location_category == "home":
            category_primary = "Home"
            category_secondary = "meal"

    entry = TimeEntry(
        prompt_id=prompt.id if prompt else None,
        interval_start=interval_start,
        interval_end=interval_end,
        raw_text=text.strip(),
        normalized_text=classification.normalized_text,
        category_primary=category_primary,
        category_secondary=category_secondary,
        project_name=classification.project_name,
        project_confidence=classification.project_confidence,
        classification_confidence=classification.classification_confidence,
        source=source,
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    session.flush()
    apply_work_focus(session, entry)
    session.flush()
    return entry


def _looks_like_activity(session: Session, text: str) -> bool:
    if not text.strip():
        return False
    low = text.lower()
    explicit_activity = re.search(
        r"\b(worked|working|drove|driving|commuted|meeting about|answered|logged|spent|picked up|laundry|dinner|gym|lunch)\b",
        low,
    )
    if has_action_language(low) and not explicit_activity:
        return False
    if explicit_activity:
        return True
    if re.search(r"\b(review task|project work|data analysis)\b", low):
        return True
    return classify_text(session, text).classification_confidence >= 0.6


def _note_type(text: str) -> str:
    low = text.lower()
    if "decision" in low:
        return "decision"
    if "idea" in low:
        return "idea"
    if "risk" in low or "concern" in low or "issue" in low:
        return "risk"
    if "meeting action item" in low or "action item" in low:
        return "action_item"
    if "follow up" in low or "follow-up" in low or "circle back" in low:
        return "follow_up"
    if "project update" in low or low.startswith("update"):
        return "update"
    return "note"


def _clean_note_body(text: str) -> str:
    body = re.sub(
        r"^(note for|project update|decision|idea|remember that|remember|meeting action item|follow up|follow-up|circle back)\s*:?\s*",
        "",
        text.strip(),
        flags=re.I,
    )
    body = re.sub(r"\s+", " ", body).strip()
    return body or text.strip()


def _format_collection(empty: str, lines: list[str]) -> str:
    if not lines:
        return empty
    return "\n".join(lines)


def _agenda(session: Session, day: str, settings: Settings, now: datetime) -> str:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day == "tomorrow":
        start += timedelta(days=1)
    end = start + timedelta(days=1)
    todos = list(
        session.scalars(
            select(TodoItem)
            .where(
                TodoItem.status.in_(["open", "in_progress", "waiting"]),
                ((TodoItem.due_at >= start) & (TodoItem.due_at < end)) | (TodoItem.due_at.is_(None)),
            )
            .order_by(TodoItem.due_at.asc(), TodoItem.created_at.asc())
            .limit(8)
        )
    )
    reminders = list(
        session.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed", "sent"]), Reminder.remind_at >= start, Reminder.remind_at < end)
            .order_by(Reminder.remind_at.asc())
            .limit(8)
        )
    )
    lines = [f"Agenda for {start.date()}:"]
    lines.extend("Todo " + todo_line(todo, settings) for todo in todos)
    lines.extend("Reminder " + reminder_line(reminder, settings) for reminder in reminders)
    return _format_collection(f"No agenda items for {start.date()}.", lines)


def _help_text(secretary_only: bool = False) -> str:
    if secretary_only:
        return (
            "Secretary commands: capture ..., idea ..., circle back ..., review inbox, "
            "make todo <id>, dismiss <id>, reminders, agenda today, notes <project>."
        )
    return (
        "Commands: pause 1h, resume, status, report today/week/month, skip, "
        "todo ..., done, review inbox, capture ..., reminders, agenda today, project add <name>."
    )


def _today_bounds(settings: Settings, now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _capture_summary(session: Session, settings: Settings, now: datetime) -> str:
    start, end = _today_bounds(settings, now)
    context = get_circle_back_context(session, start=start, end=end, settings=settings, now=now)
    return (
        f"You captured {len(context.new_todos) + len(context.new_reminders) + len(context.project_notes) + len(context.inbox_items)} items today: "
        f"{len(context.new_todos)} todos, {len(context.project_notes)} project notes, "
        f"{len(context.ideas)} ideas, {len(context.inbox_items)} inbox items."
    )


def _circle_back_lines(session: Session, settings: Settings, now: datetime) -> list[str]:
    start, end = _today_bounds(settings, now)
    context = get_circle_back_context(session, start=start, end=end, settings=settings, now=now)
    lines: list[str] = []
    lines.extend(f"Todo #{todo.id}: {todo.title}" for todo in context.stale_todos[:5])
    lines.extend(f"Note #{note.id}: {note.title}" for note in context.notes_needing_action[:5])
    lines.extend(f"Inbox #{item.id}: {item.suggested_title or item.raw_text}" for item in context.inbox_items[:5])
    lines.extend(f"Overdue #{todo.id}: {todo.title}" for todo in context.overdue_todos[:5])
    return lines


def _get_inbox_item(session: Session, item_id: str) -> SecretaryInboxItem | None:
    if not item_id.strip().isdigit():
        return None
    return session.get(SecretaryInboxItem, int(item_id.strip()))


def _convert_inbox_to_todo(
    session: Session,
    item: SecretaryInboxItem,
    *,
    settings: Settings,
    now: datetime,
) -> TodoItem:
    todo = create_todo_from_text(
        session,
        item.suggested_title or item.raw_text,
        settings=settings,
        source_sms_id=item.created_from_sms_id or item.sms_message_id,
        now=now,
    )
    if item.suggested_project_name and not todo.project_name:
        todo.project_name = item.suggested_project_name
        todo.project_id = item.suggested_project_id
    item.converted_to_type = "TodoItem"
    item.converted_to_id = todo.id
    resolve_inbox_item(session, item)
    return todo


def _convert_inbox_to_reminder(
    session: Session,
    item: SecretaryInboxItem,
    *,
    when_text: str,
    settings: Settings,
    now: datetime,
) -> Reminder:
    title = item.suggested_title or item.raw_text
    reminder, todo = create_reminder_from_text(
        session,
        f"remind me {when_text} to {title}",
        settings=settings,
        source_sms_id=item.created_from_sms_id or item.sms_message_id,
        now=now,
    )
    item.converted_to_type = "Reminder"
    item.converted_to_id = reminder.id
    resolve_inbox_item(session, item)
    return reminder


def _assign_inbox_to_project(
    session: Session,
    item: SecretaryInboxItem,
    *,
    project_name: str,
    settings: Settings,
    now: datetime,
) -> ProjectNote:
    project = add_project(session, project_name.strip(), aliases=[], category=item.suggested_category or "Unknown")
    item.suggested_project_id = project.id
    item.suggested_project_name = project.name
    item.status = "reviewed"
    item.reviewed_at = now
    note = create_project_note(
        session,
        item.raw_text,
        settings=settings,
        note_type=item.interpreted_type.lower() if item.interpreted_type else "note",
        title=item.suggested_title or item.raw_text[:80],
        body=item.raw_text,
        source_sms_id=item.created_from_sms_id or item.sms_message_id,
        now=now,
    )
    note.project_id = project.id
    note.project_name = project.name
    return note


def _format_items(empty: str, lines: list[str], limit: int = 8) -> str:
    if not lines:
        return empty
    return "\n".join(lines[:limit])


def _learn_from_last_entry(session: Session, entry: TimeEntry) -> None:
    if not entry.raw_text:
        return
    pattern = re.escape(entry.raw_text.strip())
    existing = session.scalar(select(ClassificationRule).where(ClassificationRule.pattern == pattern))
    if existing is None:
        session.add(
            ClassificationRule(
                name=f"correction-{entry.id}",
                pattern=pattern,
                category_primary=entry.category_primary,
                category_secondary=entry.category_secondary,
                project_name=entry.project_name,
                priority=120,
                active=True,
            )
        )


def _parse_time_today(value: str, *, now: datetime, settings: Settings) -> datetime | None:
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", value, flags=re.I)
    if not match:
        return parse_natural_datetime(value, now=now, settings=settings)
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _handle_location_command(
    session: Session,
    body: str,
    *,
    settings: Settings,
    now: datetime,
) -> ProcessResult:
    command = parse_location_command(body)
    if command is None:
        return ProcessResult("I could not parse that location command.")

    if command.name == "location_event":
        event = record_location_event(
            session,
            place_name=command.args["place"],
            event_type=command.args["event_type"],
            settings=settings,
            source="manual_sms",
            occurred_at=now,
            raw_payload={"source": "sms_location_command", "body": body},
        )
        if event.place_name == "Work":
            generate_work_day_summary(session, now.date(), settings=settings)
        verb = {"arrived": "Arrived at", "left": "Left", "snapshot": "Marked at"}.get(event.event_type, event.event_type)
        return ProcessResult(f"{verb} {event.place_name} at {human_dt(event.occurred_at, settings)}.", {"location_event": [event.id]})

    if command.name == "add_place":
        place = get_or_create_place(session, command.args["place"])
        return ProcessResult(f"Added place {place.name} ({place.category}).")

    if command.name == "list_places":
        places = list_places(session)
        if not places:
            return ProcessResult("No places saved yet.")
        return ProcessResult("Places: " + "; ".join(f"{place.name} ({place.category})" for place in places))

    if command.name == "location_status":
        return ProcessResult(location_status(session, settings))

    return ProcessResult("I could not parse that location command.")


def _handle_review_mode_reply(
    session: Session,
    text: str,
    *,
    settings: Settings,
    now: datetime,
) -> ProcessResult | None:
    item = current_review_item(session)
    if item is None:
        return None

    low = text.strip().lower()
    if low == "next":
        return ProcessResult(advance_inbox_review(session))

    if low in {"dismiss", "done"}:
        dismiss_inbox_item(session, item)
        next_text = advance_inbox_review(session)
        return ProcessResult(f"Dismissed inbox item #{item.id}. {next_text}")

    if low == "todo":
        todo = _convert_inbox_to_todo(session, item, settings=settings, now=now)
        next_text = advance_inbox_review(session)
        return ProcessResult(f"Made todo #{todo.id}. {next_text}", {"todo": [todo.id]})

    if low.startswith("remind "):
        when_text = low.replace("remind", "", 1).strip() or "tomorrow"
        reminder = _convert_inbox_to_reminder(
            session,
            item,
            when_text=when_text,
            settings=settings,
            now=now,
        )
        next_text = advance_inbox_review(session)
        return ProcessResult(f"Scheduled reminder #{reminder.id}. {next_text}", {"reminder": [reminder.id]})

    if low.startswith("snooze "):
        when_text = low.replace("snooze", "", 1).strip()
        if when_text == "week":
            item.next_review_at = next_weekly_review(now, settings)
        else:
            item.next_review_at = parse_natural_datetime(when_text, now=now, settings=settings) or next_weekly_review(now, settings)
        item.reviewed_at = now
        next_text = advance_inbox_review(session)
        return ProcessResult(f"Snoozed inbox item #{item.id}. {next_text}")

    if low.startswith("assign "):
        project_text = text.strip()[7:].strip()
        note = _assign_inbox_to_project(
            session,
            item,
            project_name=project_text,
            settings=settings,
            now=now,
        )
        next_text = advance_inbox_review(session)
        return ProcessResult(f"Assigned inbox item #{item.id} to {note.project_name}. {next_text}")

    return None


def _execute_command(
    session: Session,
    command: ParsedCommand,
    *,
    settings: Settings,
    now: datetime,
    sms_message_id: int | None,
) -> ProcessResult:
    name = command.name
    args = command.args

    if name == "location":
        return _handle_location_command(session, args.get("body", ""), settings=settings, now=now)

    if name == "work_hours":
        period = args.get("period", "week")
        return ProcessResult(work_hours_summary_text(session, period, settings=settings, now=now))

    if name == "briefing_request":
        briefing = generate_briefing(
            session,
            args.get("body", ""),
            settings=settings,
            request_source="sms",
            topic=args.get("topic"),
            include_sensitive=False,
            created_from_sms_id=sms_message_id,
            now=now,
        )
        if briefing.report is None:
            return ProcessResult(briefing.message, {"briefing_request": [briefing.request.id]})
        return ProcessResult(
            sms_safe_briefing_reply(briefing.report, settings),
            {
                "briefing_request": [briefing.request.id],
                "briefing_report": [briefing.report.id],
            },
        )

    if name == "fix_work_location":
        when = _parse_time_today(args.get("time", ""), now=now, settings=settings)
        if when is None:
            return ProcessResult("I could not parse that work location time.")
        event_type = "arrived" if args.get("event") == "arrived" else "left"
        event = upsert_manual_location_event_for_day(
            session,
            place_name="Work",
            event_type=event_type,
            occurred_at=when,
            settings=settings,
        )
        generate_work_day_summary(session, when.date(), settings=settings)
        return ProcessResult(f"Fixed {event_type} work at {human_dt(event.occurred_at, settings)}.", {"location_event": [event.id]})

    if name == "capture":
        item = add_inbox_item(
            session,
            args.get("body", ""),
            settings=settings,
            sms_message_id=sms_message_id,
            interpreted_type="Capture",
            suggested_next_action="Review and decide whether this is a todo, reminder, note, or saved item.",
            confidence=0.45,
        )
        return ProcessResult(f"Captured inbox item #{item.id} for review.", {"inbox": [item.id]})

    if name == "capture_note":
        body = args.get("body", "")
        note = create_project_note(
            session,
            body,
            settings=settings,
            note_type=_note_type(body),
            title=body[:80],
            body=body,
            source_sms_id=sms_message_id,
            now=now,
        )
        return ProcessResult(f"Captured note #{note.id}.", {"project_note": [note.id]})

    if name == "capture_idea":
        body = args.get("body", "")
        note = create_project_note(
            session,
            body,
            settings=settings,
            note_type="idea",
            title=body[:80],
            body=body,
            source_sms_id=sms_message_id,
            now=now,
        )
        return ProcessResult(f"Captured idea #{note.id} for weekly review.", {"project_note": [note.id]})

    if name == "capture_followup":
        body = args.get("body", "")
        note = create_project_note(
            session,
            body,
            settings=settings,
            note_type="follow_up",
            title=body[:80],
            body=body,
            source_sms_id=sms_message_id,
            now=now,
        )
        note.needs_followup = True
        note.next_review_at = default_next_review_at(
            body,
            category_primary="Work" if note.project_name else "Unknown",
            interpreted_type="follow_up",
            settings=settings,
            now=now,
        )
        return ProcessResult(f"Captured follow-up #{note.id} for {human_dt(note.next_review_at, settings)}.", {"project_note": [note.id]})

    if name == "review_inbox":
        return ProcessResult(start_inbox_review(session))

    if name == "review_captured":
        lines = _circle_back_lines(session, settings, now)
        return ProcessResult(_format_items("Nothing needs review right now.", lines))

    if name == "dismiss":
        item = _get_inbox_item(session, args.get("id", ""))
        if item:
            dismiss_inbox_item(session, item)
            return ProcessResult(f"Dismissed inbox item #{item.id}.")
        if args.get("id", "").isdigit():
            note = session.get(ProjectNote, int(args["id"]))
            if note:
                note.capture_status = "dismissed"
                note.needs_followup = False
                note.last_reviewed_at = now
                return ProcessResult(f"Dismissed note #{note.id}.")
        return ProcessResult("I could not find that captured item.")

    if name == "make_todo":
        item = _get_inbox_item(session, args.get("id", ""))
        if item:
            todo = _convert_inbox_to_todo(session, item, settings=settings, now=now)
            return ProcessResult(f"Made todo #{todo.id}: {todo.title}.", {"todo": [todo.id]})
        if args.get("id", "").isdigit():
            note = session.get(ProjectNote, int(args["id"]))
            if note:
                todo = create_todo_from_text(
                    session,
                    note.body,
                    settings=settings,
                    source_sms_id=note.created_from_sms_id,
                    now=now,
                )
                note.linked_todo_id = todo.id
                note.capture_status = "converted_to_todo"
                note.needs_followup = False
                return ProcessResult(f"Made todo #{todo.id} from note #{note.id}.", {"todo": [todo.id]})
        return ProcessResult("I could not find that captured item.")

    if name == "remind_about":
        item = _get_inbox_item(session, args.get("id", ""))
        if item:
            reminder = _convert_inbox_to_reminder(
                session,
                item,
                when_text=args.get("when", "tomorrow"),
                settings=settings,
                now=now,
            )
            return ProcessResult(f"Scheduled reminder #{reminder.id}.", {"reminder": [reminder.id]})
        if args.get("id", "").isdigit():
            note = session.get(ProjectNote, int(args["id"]))
            if note:
                reminder, todo = create_reminder_from_text(
                    session,
                    f"remind me {args.get('when', 'tomorrow')} to {note.title}",
                    settings=settings,
                    source_sms_id=note.created_from_sms_id,
                    now=now,
                )
                note.linked_reminder_id = reminder.id
                note.capture_status = "converted_to_reminder"
                note.needs_followup = False
                return ProcessResult(f"Scheduled reminder #{reminder.id} from note #{note.id}.", {"reminder": [reminder.id]})
        return ProcessResult("I could not find that captured item.")

    if name == "assign_capture":
        item = _get_inbox_item(session, args.get("id", ""))
        if item:
            note = _assign_inbox_to_project(
                session,
                item,
                project_name=args.get("project", ""),
                settings=settings,
                now=now,
            )
            return ProcessResult(f"Assigned item #{item.id} to {note.project_name}.")
        if args.get("id", "").isdigit():
            note = session.get(ProjectNote, int(args["id"]))
            if note:
                project = add_project(session, args.get("project", ""), aliases=[], category="Unknown")
                note.project_id = project.id
                note.project_name = project.name
                note.capture_status = "reviewed"
                return ProcessResult(f"Assigned note #{note.id} to {project.name}.")
        return ProcessResult("I could not find that captured item.")

    if name == "capture_today":
        return ProcessResult(_capture_summary(session, settings, now))

    if name == "circle_back_list":
        lines = _circle_back_lines(session, settings, now)
        return ProcessResult(_format_items("Nothing needs circle-back right now.", lines))

    if name == "notes_need_action":
        notes = session.scalars(
            select(ProjectNote)
            .where(
                ProjectNote.needs_followup.is_(True),
                ProjectNote.capture_status.in_(["captured", "reviewed"]),
                ProjectNote.linked_todo_id.is_(None),
                ProjectNote.linked_reminder_id.is_(None),
            )
            .order_by(ProjectNote.next_review_at.asc())
            .limit(10)
        ).all()
        return ProcessResult(_format_items("No notes need action.", [f"#{note.id} {note.title}" for note in notes]))

    if name == "show_unassigned":
        inbox = session.scalars(
            select(SecretaryInboxItem)
            .where(SecretaryInboxItem.status == "open", SecretaryInboxItem.suggested_project_name.is_(None))
            .order_by(SecretaryInboxItem.created_at.asc())
            .limit(5)
        ).all()
        notes = session.scalars(
            select(ProjectNote)
            .where(ProjectNote.project_name.is_(None), ProjectNote.capture_status.in_(["captured", "reviewed"]))
            .order_by(ProjectNote.created_at.asc())
            .limit(5)
        ).all()
        lines = [f"Inbox #{item.id}: {item.suggested_title or item.raw_text}" for item in inbox]
        lines.extend(f"Note #{note.id}: {note.title}" for note in notes)
        return ProcessResult(_format_items("No unassigned captured items.", lines))

    if name == "show_stale":
        start, end = _today_bounds(settings, now)
        context = get_circle_back_context(session, start=start, end=end, settings=settings, now=now)
        lines = [f"Todo #{todo.id}: {todo.title}" for todo in context.stale_todos]
        lines.extend(f"Note #{note.id}: {note.title}" for note in context.stale_notes)
        lines.extend(f"Inbox #{item.id}: {item.suggested_title or item.raw_text}" for item in context.stale_inbox_items)
        return ProcessResult(_format_items("No stale items.", lines))

    if name == "show_snoozed":
        reminders = session.scalars(
            select(Reminder)
            .where((Reminder.status == "snoozed") | (Reminder.snooze_count > 0))
            .order_by(Reminder.snooze_count.desc(), Reminder.remind_at.asc())
            .limit(10)
        ).all()
        return ProcessResult(_format_items("No snoozed reminders.", [reminder_line(reminder, settings) for reminder in reminders]))

    if name == "pause_for":
        duration = parse_duration(args.get("duration", ""))
        if duration is None:
            until = parse_natural_datetime(args.get("duration", ""), now=now, settings=settings)
        else:
            until = now + duration
        if until is None:
            return ProcessResult("I could not parse that pause duration.")
        set_state(session, PAUSE_KEY, until.isoformat())
        return ProcessResult("Paused prompts until " + human_dt(until, settings) + ".")

    if name == "pause_until":
        until = parse_natural_datetime(args.get("when", ""), now=now, settings=settings)
        if until is None:
            return ProcessResult("I could not parse that pause time.")
        set_state(session, PAUSE_KEY, until.isoformat())
        return ProcessResult("Paused prompts until " + human_dt(until, settings) + ".")

    if name == "resume":
        clear_state(session, PAUSE_KEY)
        return ProcessResult("Prompts resumed.")

    if name == "status":
        open_todo_count = session.scalar(
            select(func.count())
            .select_from(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]))
        )
        return ProcessResult(f"{pause_status_text(session, settings)} Open todos: {open_todo_count or 0}.")

    if name == "skip":
        prompt = _latest_open_prompt(session, now, settings)
        if prompt is None:
            return ProcessResult("No open prompt to skip.")
        prompt.status = "skipped"
        return ProcessResult("Skipped the latest prompt.")

    if name == "report":
        result = generate_report(session, args.get("type", "daily"), settings=settings, now=now)
        suffix = f" Saved to {result.path}." if result.path else ""
        return ProcessResult(result.summary_text + suffix)

    if name == "project_add":
        project = add_project(session, args.get("name", ""), aliases=[], category="Unknown")
        return ProcessResult(f"Added project {project.name}.")

    if name == "project_alias":
        aliases = [part.strip() for part in args.get("aliases", "").split(",") if part.strip()]
        project = add_project_aliases(session, args.get("project", ""), aliases)
        if project is None:
            return ProcessResult("I could not find that project.")
        return ProcessResult(f"Added aliases to {project.name}: {', '.join(aliases)}.")

    if name == "fix_last_category":
        entry = session.scalar(select(TimeEntry).order_by(TimeEntry.created_at.desc()))
        if entry is None:
            return ProcessResult("No time entry to correct.")
        entry.category_primary = args["category"]
        if entry.category_primary == "Work" and entry.category_secondary == "unknown":
            entry.category_secondary = "other_work"
        elif entry.category_primary == "Home" and entry.category_secondary == "unknown":
            entry.category_secondary = "other_home"
        _learn_from_last_entry(session, entry)
        return ProcessResult(f"Updated last entry to {entry.category_primary}.")

    if name == "fix_last_project":
        entry = session.scalar(select(TimeEntry).order_by(TimeEntry.created_at.desc()))
        if entry is None:
            return ProcessResult("No time entry to correct.")
        project = add_project(session, args["project"], aliases=[], category=entry.category_primary)
        entry.project_name = project.name
        entry.project_confidence = 1.0
        _learn_from_last_entry(session, entry)
        return ProcessResult(f"Updated last entry project to {project.name}.")

    if name == "todo_add":
        todo = create_todo_from_text(session, args.get("body", ""), settings=settings, source_sms_id=sms_message_id, now=now)
        return ProcessResult(f"Added todo #{todo.id}: {todo.title}.", {"todo": [todo.id]})

    if name == "done":
        query = args.get("query", "")
        if not query:
            reminder = complete_reminder(session)
            if reminder:
                return ProcessResult(f"Marked reminder #{reminder.id} done.")
        todo = complete_todo(session, query)
        if todo:
            return ProcessResult(f"Marked todo #{todo.id} done.")
        return ProcessResult("I could not find a matching todo or reminder.")

    if name == "cancel" or name == "cancel_reminder":
        reminder_id = int(args["id"]) if args.get("id", "").isdigit() else None
        reminder = cancel_reminder(session, reminder_id)
        if reminder:
            return ProcessResult(f"Canceled reminder #{reminder.id}.")
        return ProcessResult("I could not find a reminder to cancel.")

    if name == "snooze" or name == "snooze_reminder":
        reminder_id = int(args["id"]) if args.get("id", "").isdigit() else None
        reminder = snooze_reminder(
            session,
            settings=settings,
            reminder_id=reminder_id,
            duration_text=args.get("duration"),
            until_text=args.get("until"),
            now=now,
        )
        if reminder:
            return ProcessResult(f"Snoozed reminder #{reminder.id} until {human_dt(reminder.remind_at, settings)}.")
        return ProcessResult("I could not find a reminder to snooze.")

    if name == "list_todos":
        todos = list_open_todos(session, category=args.get("category"), limit=10)
        return ProcessResult(_format_collection("No open todos.", [todo_line(todo, settings) for todo in todos]))

    if name == "list_project":
        todos = list_open_todos(session, project_name=args.get("project"), limit=10)
        return ProcessResult(_format_collection("No open items for that project.", [todo_line(todo, settings) for todo in todos]))

    if name == "notes":
        project = find_project(session, args.get("project", ""))
        query = select(ProjectNote).order_by(ProjectNote.created_at.desc()).limit(5)
        if project:
            query = select(ProjectNote).where(ProjectNote.project_name == project.name).order_by(ProjectNote.created_at.desc()).limit(5)
        notes = session.scalars(query).all()
        lines = [f"#{note.id} {note.note_type}: {note.title}" for note in notes]
        return ProcessResult(_format_collection("No notes found.", lines))

    if name == "list_reminders":
        reminders = session.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed", "sent"]))
            .order_by(Reminder.remind_at.asc())
            .limit(10)
        ).all()
        return ProcessResult(_format_collection("No active reminders.", [reminder_line(reminder, settings) for reminder in reminders]))

    if name == "agenda":
        return ProcessResult(_agenda(session, args.get("day", "today"), settings, now))

    if name == "recall_project":
        project = find_project(session, args.get("project", ""))
        if project is None:
            return ProcessResult("I could not find that project.")
        notes = session.scalars(
            select(ProjectNote)
            .where(ProjectNote.project_name == project.name)
            .order_by(ProjectNote.created_at.desc())
            .limit(5)
        ).all()
        todos = list_open_todos(session, project_name=project.name, limit=5)
        lines = [f"Notes for {project.name}:"]
        lines.extend(f"#{note.id} {note.note_type}: {note.title}" for note in notes)
        lines.extend("Todo " + todo_line(todo, settings) for todo in todos)
        return ProcessResult(_format_collection(f"No stored context for {project.name}.", lines))

    if name == "project_status":
        project = find_project(session, args.get("project", ""))
        if project is None:
            return ProcessResult("I could not find that project.")
        return ProcessResult(summarize_project(session, project, settings, now=now))

    if name == "help_secretary":
        return ProcessResult(_help_text(secretary_only=True))

    if name == "help":
        return ProcessResult(_help_text())

    return ProcessResult("I did not recognize that command.")


def _process_note_like(
    session: Session,
    text: str,
    *,
    sms_message_id: int | None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> tuple[ProjectNote, str]:
    note_type = _note_type(text)
    body = _clean_note_body(text)
    note = create_project_note(
        session,
        text,
        settings=settings,
        note_type=note_type,
        title=body[:80],
        body=body,
        source_sms_id=sms_message_id,
        now=now,
    )
    project = f" for {note.project_name}" if note.project_name else ""
    return note, f"Captured {note_type}{project}."


def _process_deadline_like(
    session: Session,
    text: str,
    *,
    settings: Settings,
    sms_message_id: int | None,
    now: datetime,
) -> tuple[TodoItem, str]:
    body = re.sub(r"^deadline\s*:?\s*", "", text.strip(), flags=re.I)
    todo = create_todo_from_text(
        session,
        body,
        settings=settings,
        source_sms_id=sms_message_id,
        now=now,
        default_priority="high",
    )
    return todo, f"Added deadline todo #{todo.id}: {todo.title}."


def _intent_body(intent: SmsIntent) -> str:
    return (intent.body or intent.title).strip()


def _known_project_from_intent(session: Session, intent: SmsIntent):
    if not intent.project_name:
        return None
    return find_project(session, intent.project_name)


def _apply_intent_project_and_category(session: Session, item, intent: SmsIntent) -> None:
    project = _known_project_from_intent(session, intent)
    if project is not None:
        if hasattr(item, "project_id"):
            item.project_id = project.id
        if hasattr(item, "related_project_id"):
            item.related_project_id = project.id
        if hasattr(item, "project_name"):
            item.project_name = project.name
    if hasattr(item, "category_primary") and intent.category_primary in {"Work", "Home", "Unknown"}:
        item.category_primary = intent.category_primary


def _todo_text_for_intent(intent: SmsIntent) -> str:
    text = _intent_body(intent) or intent.title
    if intent.due_at_text and intent.due_at_text.lower() not in text.lower():
        text = f"{text} by {intent.due_at_text}"
    if intent.priority != "normal":
        text = f"{intent.priority} {text}"
    return text.strip()


def _reminder_text_for_intent(intent: SmsIntent) -> str:
    body = _intent_body(intent) or intent.title
    when = intent.remind_at_text or intent.due_at_text or intent.next_review_at_text or "in 1 hour"
    return f"remind me {when} to {body}".strip()


def _note_type_for_intent(intent: SmsIntent) -> str:
    return {
        "project_update": "update",
        "decision": "decision",
        "idea": "idea",
        "follow_up": "follow_up",
        "meeting_action_item": "action_item",
        "question_to_revisit": "question",
    }.get(intent.type, "note")


def _merge_process_result(target: ProcessResult, source: ProcessResult) -> None:
    for key, values in source.created.items():
        for value in values:
            target.add(key, value)


def _process_llm_intent(
    session: Session,
    intent: SmsIntent,
    *,
    settings: Settings,
    sms_message_id: int | None,
    now: datetime,
) -> ProcessResult | None:
    body = _intent_body(intent) or intent.title
    if not body:
        return None

    result = ProcessResult(reply="")

    if intent.type == "time_entry":
        entry = create_time_entry_from_text(session, body, settings=settings, now=now, source="sms")
        _apply_intent_project_and_category(session, entry, intent)
        result.add("time_entry", entry.id)
        project = f" ({entry.project_name})" if entry.project_name else ""
        result.reply = f"Logged {entry.category_primary}/{entry.category_secondary}{project}."
        return result

    if intent.type in {"todo", "meeting_action_item"}:
        todo = create_todo_from_text(
            session,
            _todo_text_for_intent(intent),
            settings=settings,
            source_sms_id=sms_message_id,
            now=now,
        )
        _apply_intent_project_and_category(session, todo, intent)
        result.add("todo", todo.id)
        if intent.type == "meeting_action_item":
            note = create_project_note(
                session,
                body,
                settings=settings,
                note_type="action_item",
                title=intent.title or body[:80],
                body=body,
                source_sms_id=sms_message_id,
                now=now,
            )
            _apply_intent_project_and_category(session, note, intent)
            note.linked_todo_id = todo.id
            note.capture_status = "converted_to_todo"
            note.needs_followup = False
            result.add("project_note", note.id)
            result.reply = f"Added action todo #{todo.id}. Captured action item #{note.id}."
        else:
            result.reply = f"Added todo #{todo.id}: {todo.title}."
        return result

    if intent.type == "reminder":
        reminder, todo = create_reminder_from_text(
            session,
            _reminder_text_for_intent(intent),
            settings=settings,
            source_sms_id=sms_message_id,
            now=now,
        )
        _apply_intent_project_and_category(session, reminder, intent)
        if todo is not None:
            _apply_intent_project_and_category(session, todo, intent)
            result.add("todo", todo.id)
        result.add("reminder", reminder.id)
        result.reply = f"Scheduled reminder #{reminder.id} for {human_dt(reminder.remind_at, settings)}."
        return result

    if intent.type in {"project_note", "project_update", "decision", "idea", "follow_up", "question_to_revisit"}:
        if intent.type == "question_to_revisit":
            inbox = add_inbox_item(
                session,
                body,
                settings=settings,
                sms_message_id=sms_message_id,
                interpreted_type="Question",
                suggested_category=intent.category_primary,
                suggested_next_action="Revisit this question and decide whether it needs a todo, note, or answer.",
                confidence=intent.confidence,
            )
            result.add("inbox", inbox.id)
            result.reply = f"Captured question #{inbox.id} for review."
            return result

        note = create_project_note(
            session,
            body,
            settings=settings,
            note_type=_note_type_for_intent(intent),
            title=intent.title or body[:80],
            body=body,
            source_sms_id=sms_message_id,
            now=now,
        )
        _apply_intent_project_and_category(session, note, intent)
        if intent.type == "follow_up":
            note.needs_followup = True
            note.next_review_at = (
                parse_natural_datetime(intent.next_review_at_text or "", now=now, settings=settings)
                or default_next_review_at(
                    body,
                    category_primary=intent.category_primary,
                    interpreted_type="follow_up",
                    settings=settings,
                    now=now,
                )
            )
        result.add("project_note", note.id)
        project = f" for {note.project_name}" if note.project_name else ""
        result.reply = f"Captured {note.note_type}{project}."
        return result

    return None


def _try_llm_natural_text(
    session: Session,
    raw: str,
    *,
    settings: Settings,
    sms_message_id: int | None,
    now: datetime,
    deterministic_confidence: float,
) -> ProcessResult | None:
    if not settings.llm_enabled:
        return None
    if settings.llm_use_for_low_confidence_only and deterministic_confidence >= 0.55:
        return None

    parse_result = llm_service.parse_sms_with_llm(session, raw, settings=settings, now=now)
    intents = llm_service.accepted_intents(parse_result)
    if not intents:
        return None

    aggregate = ProcessResult(reply="")
    replies: list[str] = []
    for intent in intents[:4]:
        intent_result = _process_llm_intent(
            session,
            intent,
            settings=settings,
            sms_message_id=sms_message_id,
            now=now,
        )
        if intent_result is None:
            continue
        _merge_process_result(aggregate, intent_result)
        if intent_result.reply:
            replies.append(intent_result.reply)

    if not replies:
        return None
    aggregate.reply = " ".join(replies)
    return aggregate


def process_natural_text(
    session: Session,
    text: str,
    *,
    settings: Settings,
    sms_message_id: int | None = None,
    now: datetime | None = None,
) -> ProcessResult:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    raw = text.strip()
    low = raw.lower()
    result = ProcessResult(reply="")
    replies: list[str] = []

    if is_briefing_request_text(raw):
        briefing = generate_briefing(
            session,
            raw,
            settings=settings,
            request_source="sms",
            include_sensitive=False,
            created_from_sms_id=sms_message_id,
            now=now,
        )
        if briefing.report is None:
            return ProcessResult(briefing.message, {"briefing_request": [briefing.request.id]})
        return ProcessResult(
            sms_safe_briefing_reply(briefing.report, settings),
            {
                "briefing_request": [briefing.request.id],
                "briefing_report": [briefing.report.id],
            },
        )

    if settings.llm_enabled and not settings.llm_use_for_low_confidence_only:
        llm_result = _try_llm_natural_text(
            session,
            raw,
            settings=settings,
            sms_message_id=sms_message_id,
            now=now,
            deterministic_confidence=0.0,
        )
        if llm_result is not None:
            return llm_result

    reminder_match = re.search(r"\b(remind me|ask me)\b", raw, flags=re.I)
    if reminder_match:
        prefix = raw[: reminder_match.start()].strip(" ,;")
        reminder_text = raw[reminder_match.start() :].strip(" ,;")
        if prefix and _looks_like_activity(session, prefix):
            entry = create_time_entry_from_text(session, prefix, settings=settings, now=now)
            result.add("time_entry", entry.id)
            replies.append(f"Logged time #{entry.id}.")
        reminder, todo = create_reminder_from_text(
            session,
            reminder_text,
            settings=settings,
            source_sms_id=sms_message_id,
            now=now,
        )
        result.add("reminder", reminder.id)
        if todo:
            result.add("todo", todo.id)
        replies.append(f"Scheduled reminder #{reminder.id} for {human_dt(reminder.remind_at, settings)}.")
        result.reply = " ".join(replies)
        return result

    if low.startswith("todo") or low.startswith("i need to") or low.startswith("need to"):
        todo = create_todo_from_text(session, raw, settings=settings, source_sms_id=sms_message_id, now=now)
        result.add("todo", todo.id)
        result.reply = f"Added todo #{todo.id}: {todo.title}."
        return result

    if low.startswith("follow up") or low.startswith("follow-up") or low.startswith("circle back") or " follow up " in low or " circle back " in low:
        note, reply = _process_note_like(
            session,
            raw,
            sms_message_id=sms_message_id,
            now=now,
            settings=settings,
        )
        note.note_type = "follow_up"
        note.needs_followup = True
        note.next_review_at = default_next_review_at(
            raw,
            category_primary="Work" if note.project_name else "Unknown",
            interpreted_type="follow_up",
            settings=settings,
            now=now,
        )
        result.add("project_note", note.id)
        result.reply = reply.replace("Captured note", "Captured follow-up")
        return result

    if low.startswith("deadline"):
        todo, reply = _process_deadline_like(session, raw, settings=settings, sms_message_id=sms_message_id, now=now)
        result.add("todo", todo.id)
        result.reply = reply
        return result

    if "meeting action item" in low or low.startswith("action item"):
        todo = create_todo_from_text(session, raw, settings=settings, source_sms_id=sms_message_id, now=now)
        note, note_reply = _process_note_like(session, raw, sms_message_id=sms_message_id, now=now, settings=settings)
        note.linked_todo_id = todo.id
        note.capture_status = "converted_to_todo"
        note.needs_followup = False
        result.add("todo", todo.id)
        result.add("project_note", note.id)
        result.reply = f"Added action todo #{todo.id}. {note_reply}"
        return result

    if re.match(r"^(run\s+\S+:.*changed|local change|program change|process change|change)\b", raw, flags=re.I):
        change = create_process_change(
            session,
            raw,
            settings=settings,
            source_sms_id=sms_message_id,
            now=now,
        )
        result.add("process_change", change.id)
        result.reply = f"Captured process change #{change.id}."
        return result

    if re.match(r"^(observation|result|process result)\b", raw, flags=re.I):
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|ppm|g|kg|mg|c|min|hr|hours?)\b", raw, flags=re.I):
            metric = create_run_metric(
                session,
                raw,
                settings=settings,
                source="sms",
                now=now,
            )
            result.add("run_metric", metric.id)
            result.reply = f"Captured run metric #{metric.id}."
        else:
            observation = create_process_observation(
                session,
                raw,
                settings=settings,
                source_sms_id=sms_message_id,
                now=now,
            )
            result.add("process_observation", observation.id)
            result.reply = f"Captured observation #{observation.id}."
        return result

    if (
        low.startswith("note")
        or re.search(r"\bnote\s*:", low)
        or low.startswith("project update")
        or low.startswith("decision")
        or low.startswith("idea")
        or low.startswith("remember")
        or "project update:" in low
    ):
        note, reply = _process_note_like(session, raw, sms_message_id=sms_message_id, now=now, settings=settings)
        result.add("project_note", note.id)
        result.reply = reply
        if note.project_name is None:
            inbox = add_inbox_item(
                session,
                raw,
                settings=settings,
                sms_message_id=sms_message_id,
                interpreted_type="ProjectNote",
                confidence=0.45,
            )
            result.add("inbox", inbox.id)
            result.reply += " I also put it in the inbox because no project matched."
        return result

    if raw.endswith("?") or re.match(r"^(what|how|why|when|where|should|could|can)\b", low):
        llm_result = _try_llm_natural_text(
            session,
            raw,
            settings=settings,
            sms_message_id=sms_message_id,
            now=now,
            deterministic_confidence=0.35,
        )
        if llm_result is not None:
            return llm_result
        inbox = add_inbox_item(
            session,
            raw,
            settings=settings,
            sms_message_id=sms_message_id,
            interpreted_type="Question",
            suggested_next_action="Revisit this question and decide whether it needs a todo, note, or answer.",
            confidence=0.35,
        )
        result.add("inbox", inbox.id)
        result.reply = f"Captured question #{inbox.id} for review."
        return result

    if has_action_language(raw) and not _looks_like_activity(session, raw):
        llm_result = _try_llm_natural_text(
            session,
            raw,
            settings=settings,
            sms_message_id=sms_message_id,
            now=now,
            deterministic_confidence=0.5,
        )
        if llm_result is not None:
            return llm_result
        inbox = add_inbox_item(
            session,
            raw,
            settings=settings,
            sms_message_id=sms_message_id,
            interpreted_type="TodoCandidate",
            suggested_next_action="Decide whether to convert this to a todo or reminder.",
            confidence=0.5,
        )
        result.add("inbox", inbox.id)
        result.reply = f"Captured item #{inbox.id} for circle-back."
        return result

    if _looks_like_activity(session, raw):
        entry = create_time_entry_from_text(session, raw, settings=settings, now=now)
        result.add("time_entry", entry.id)
        project = f" ({entry.project_name})" if entry.project_name else ""
        result.reply = f"Logged {entry.category_primary}/{entry.category_secondary}{project}."
        return result

    llm_result = _try_llm_natural_text(
        session,
        raw,
        settings=settings,
        sms_message_id=sms_message_id,
        now=now,
        deterministic_confidence=0.2,
    )
    if llm_result is not None:
        return llm_result

    inbox = add_inbox_item(
        session,
        raw,
        settings=settings,
        sms_message_id=sms_message_id,
        interpreted_type="Unknown",
        confidence=0.2,
    )
    result.add("inbox", inbox.id)
    result.reply = "I saved that to the secretary inbox for review."
    return result


def process_inbound_text(
    session: Session,
    text: str,
    *,
    settings: Settings,
    sms_message_id: int | None = None,
    now: datetime | None = None,
) -> ProcessResult:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    review_result = _handle_review_mode_reply(session, text, settings=settings, now=now)
    if review_result is not None:
        session.flush()
        return review_result
    command = parse_command(text)
    if command:
        result = _execute_command(session, command, settings=settings, now=now, sms_message_id=sms_message_id)
    else:
        result = process_natural_text(
            session,
            text,
            settings=settings,
            sms_message_id=sms_message_id,
            now=now,
        )
    session.flush()
    return result
