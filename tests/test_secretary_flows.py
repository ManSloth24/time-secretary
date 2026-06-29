from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from time_secretary.models import CheckinPrompt, ProjectNote, Reminder, TimeEntry, TodoItem
from time_secretary.reminder_service import complete_reminder, snooze_reminder
from time_secretary.secretary_service import process_inbound_text


def test_late_reply_matches_open_prompt(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 10, 20, tzinfo=settings.timezone)
    prompt = CheckinPrompt(
        scheduled_for_start=datetime(2026, 6, 22, 10, 0, tzinfo=settings.timezone),
        scheduled_for_end=datetime(2026, 6, 22, 10, 15, tzinfo=settings.timezone),
        status="sent",
    )
    session.add(prompt)
    session.flush()

    result = process_inbound_text(session, "meeting about Project Alpha", settings=settings, now=now)
    entry = session.scalar(select(TimeEntry).order_by(TimeEntry.id.desc()))

    assert "Logged" in result.reply
    assert entry.prompt_id == prompt.id
    assert prompt.status == "answered"


def test_creates_todo_from_sms_and_assigns_project(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)

    result = process_inbound_text(session, "todo high finish project beta writeup by Friday", settings=settings, now=now)
    todo = session.scalar(select(TodoItem).order_by(TodoItem.id.desc()))

    assert "Added todo" in result.reply
    assert todo.priority == "high"
    assert todo.status == "open"
    assert todo.project_name == "Project Beta"
    assert todo.due_at.date().isoformat() == "2026-06-26"


def test_creates_reminder_and_related_todo_from_sms(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)

    result = process_inbound_text(session, "remind me tomorrow to check the project update", settings=settings, now=now)
    reminder = session.scalar(select(Reminder).order_by(Reminder.id.desc()))
    todo = session.get(TodoItem, reminder.related_todo_id)

    assert "Scheduled reminder" in result.reply
    assert reminder.remind_at.date().isoformat() == "2026-06-23"
    assert todo is not None
    assert "check the project update" in todo.title


def test_snooze_and_complete_reminder(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)
    process_inbound_text(session, "remind me tomorrow to check the project update", settings=settings, now=now)
    reminder = session.scalar(select(Reminder).order_by(Reminder.id.desc()))
    reminder.status = "sent"
    reminder.sent_at = now
    session.flush()

    snoozed = snooze_reminder(session, settings=settings, duration_text="30m", now=now)
    assert snoozed.id == reminder.id
    assert snoozed.status == "snoozed"
    assert snoozed.remind_at == now + timedelta(minutes=30)

    done = complete_reminder(session)
    assert done.id == reminder.id
    assert done.status == "done"


def test_project_note_capture(db_session):
    session, settings = db_session

    result = process_inbound_text(
        session,
        "note for Project Alpha project: risk item needs references",
        settings=settings,
    )
    note = session.scalar(select(ProjectNote).order_by(ProjectNote.id.desc()))

    assert "Captured" in result.reply
    assert note.project_name == "Project Alpha"
    assert note.note_type == "risk"


def test_one_sms_can_create_time_entry_and_reminder(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)

    result = process_inbound_text(
        session,
        "worked on Project Alpha report, remind me tomorrow to add references",
        settings=settings,
        now=now,
    )

    assert result.created["time_entry"]
    assert result.created["reminder"]
    entry = session.get(TimeEntry, result.created["time_entry"][0])
    reminder = session.get(Reminder, result.created["reminder"][0])
    assert entry.project_name == "Project Alpha"
    assert reminder.title == "add references"
