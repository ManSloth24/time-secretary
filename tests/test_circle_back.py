from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url
from time_secretary.main import create_app
from time_secretary.models import Project, ProjectNote, SecretaryInboxItem, TimeEntry, TodoItem
from time_secretary.report_service import generate_report
from time_secretary.secretary_service import process_inbound_text


def test_unprompted_action_text_becomes_secretary_item_not_time_log(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 11, 0, tzinfo=settings.timezone)

    result = process_inbound_text(
        session,
        "Need to look into better review thresholding for review item",
        settings=settings,
        now=now,
    )

    assert "Added todo" in result.reply
    assert session.scalar(select(TimeEntry)) is None
    todo = session.scalar(select(TodoItem).order_by(TodoItem.id.desc()))
    assert todo.project_name == "Project Gamma"
    assert todo.next_review_at is not None


def test_circle_back_creates_followup_with_review_date(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)

    result = process_inbound_text(
        session,
        "Circle back on follow-up options next week",
        settings=settings,
        now=now,
    )
    note = session.scalar(select(ProjectNote).order_by(ProjectNote.id.desc()))

    assert "follow-up" in result.reply
    assert note.note_type == "follow_up"
    assert note.project_name == "Project Delta"
    assert note.needs_followup is True
    assert note.next_review_at is not None


def test_vague_note_goes_to_secretary_inbox(db_session):
    session, settings = db_session

    result = process_inbound_text(session, "blue threshold maybe", settings=settings)
    item = session.scalar(select(SecretaryInboxItem).order_by(SecretaryInboxItem.id.desc()))

    assert "inbox" in result.reply.lower()
    assert item.status == "open"
    assert item.next_review_at is not None


def test_action_language_note_needs_followup(db_session):
    session, settings = db_session

    process_inbound_text(
        session,
        "Project Alpha note: need references on follow-up item follow-up",
        settings=settings,
    )
    note = session.scalar(select(ProjectNote).order_by(ProjectNote.id.desc()))

    assert note.project_name == "Project Alpha"
    assert note.needs_followup is True
    assert note.next_review_at is not None


def test_reports_surface_captured_unprocessed_items(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 10, 0, tzinfo=settings.timezone)
    process_inbound_text(session, "blue threshold maybe", settings=settings, now=now)
    process_inbound_text(session, "Project Alpha note: need references on follow-up item follow-up", settings=settings, now=now)

    daily = generate_report(session, "daily", settings=settings, now=now)
    weekly = generate_report(session, "weekly", settings=settings, now=now)

    assert "Things To Circle Back On" in daily.markdown
    assert "Inbox items needing review" in daily.markdown
    assert "Things mentioned but not acted on" in weekly.markdown


def test_project_page_flags_notes_without_next_action(tmp_path):
    settings = Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        reports_dir=str(tmp_path / "reports"),
    )
    engine = create_engine_from_url(settings.database_url)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    app = create_app(settings=settings, engine=engine, session_factory=TestingSession)

    with TestClient(app) as client:
        client.post(
            "/sms/inbound",
            data={"From": "+15551234567", "To": "+15557654321", "Body": "Project Alpha note: need references on follow-up item follow-up", "MessageSid": "SM2"},
        )
        with TestingSession() as session:
            project = session.scalar(select(Project).where(Project.name == "Project Alpha"))
            project_id = project.id

        response = client.get(f"/dashboard/projects/{project_id}")
        assert response.status_code == 200
        assert "This project has notes but no next action." in response.text

    engine.dispose()


def test_sms_review_flow_converts_current_inbox_item_to_todo(db_session):
    session, settings = db_session

    process_inbound_text(session, "blue threshold maybe", settings=settings)
    review = process_inbound_text(session, "review inbox", settings=settings)
    converted = process_inbound_text(session, "todo", settings=settings)

    item = session.scalar(select(SecretaryInboxItem).order_by(SecretaryInboxItem.id.desc()))
    todo = session.scalar(select(TodoItem).order_by(TodoItem.id.desc()))

    assert "Inbox item" in review.reply
    assert "Made todo" in converted.reply
    assert item.status == "converted"
    assert todo.title
