from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url
from time_secretary.main import create_app
from time_secretary.models import CurrentLocationState, LocationEvent, TimeEntry, WorkDaySummary
from time_secretary.secretary_service import create_time_entry_from_text, process_inbound_text
from time_secretary.work_hours_service import generate_work_day_summary, totals_for_period


def test_arrived_work_creates_location_event_not_time_entry(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 7, 35, tzinfo=settings.timezone)

    result = process_inbound_text(session, "arrived work", settings=settings, now=now)

    event = session.scalar(select(LocationEvent).order_by(LocationEvent.id.desc()))
    assert "Arrived at Work" in result.reply
    assert event.place_name == "Work"
    assert event.event_type == "arrived"
    assert session.scalar(select(TimeEntry)) is None


def test_left_work_updates_current_location_state(db_session):
    session, settings = db_session
    process_inbound_text(session, "arrived work", settings=settings, now=datetime(2026, 6, 22, 7, 35, tzinfo=settings.timezone))
    process_inbound_text(session, "left work", settings=settings, now=datetime(2026, 6, 22, 17, 20, tzinfo=settings.timezone))

    state = session.get(CurrentLocationState, 1)

    assert state.current_place_name is None
    assert state.last_event_id is not None


def test_arrived_and_left_work_creates_worksite_duration(db_session):
    session, settings = db_session
    day = datetime(2026, 6, 22, 7, 35, tzinfo=settings.timezone)
    process_inbound_text(session, "arrived work", settings=settings, now=day)
    process_inbound_text(session, "left work", settings=settings, now=day.replace(hour=17, minute=20))

    summary = generate_work_day_summary(session, day.date(), settings=settings)

    assert summary.worksite_duration_minutes == 585
    assert summary.confidence == "high"


def test_missing_left_work_falls_back_to_last_work_entry(db_session):
    session, settings = db_session
    day = datetime(2026, 6, 22, 8, 0, tzinfo=settings.timezone)
    process_inbound_text(session, "arrived work", settings=settings, now=day)
    create_time_entry_from_text(session, "worked on Project Alpha report", settings=settings, now=day.replace(hour=10, minute=15))

    summary = generate_work_day_summary(session, day.date(), settings=settings)

    assert summary.missing_leave_event is True
    assert summary.left_work_at.hour == 10
    assert summary.left_work_at.minute == 15
    assert summary.worksite_duration_minutes == 135


def test_missing_arrived_work_falls_back_to_first_work_entry(db_session):
    session, settings = db_session
    day = datetime(2026, 6, 22, 9, 15, tzinfo=settings.timezone)
    create_time_entry_from_text(session, "worked on Project Alpha report", settings=settings, now=day)
    process_inbound_text(session, "left work", settings=settings, now=day.replace(hour=17, minute=20))

    summary = generate_work_day_summary(session, day.date(), settings=settings)

    assert summary.missing_arrival_event is True
    assert summary.arrived_work_at.hour == 9
    assert summary.arrived_work_at.minute == 0
    assert summary.worksite_duration_minutes == 500


def test_lunch_at_work_is_separate_from_logged_work_activity(db_session):
    session, settings = db_session
    day = datetime(2026, 6, 22, 8, 0, tzinfo=settings.timezone)
    process_inbound_text(session, "arrived work", settings=settings, now=day)
    process_inbound_text(session, "lunch", settings=settings, now=day.replace(hour=12, minute=15))
    process_inbound_text(session, "left work", settings=settings, now=day.replace(hour=17, minute=0))

    entry = session.scalar(select(TimeEntry).order_by(TimeEntry.id.desc()))
    summary = generate_work_day_summary(session, day.date(), settings=settings)

    assert entry.category_primary == "Work"
    assert entry.category_secondary == "lunch_at_work"
    assert summary.worksite_duration_minutes == 540
    assert summary.lunch_break_minutes == 15
    assert summary.logged_work_minutes == 0


def test_year_to_date_work_hours_calculate_correctly(db_session):
    session, settings = db_session
    day1 = datetime(2026, 1, 2, 8, 0, tzinfo=settings.timezone)
    day2 = datetime(2026, 6, 22, 8, 0, tzinfo=settings.timezone)
    for day in (day1, day2):
        process_inbound_text(session, "arrived work", settings=settings, now=day)
        create_time_entry_from_text(session, "worked on Project Alpha report", settings=settings, now=day.replace(hour=9, minute=15))
        process_inbound_text(session, "left work", settings=settings, now=day.replace(hour=17, minute=0))

    totals = totals_for_period(session, "ytd", settings=settings, now=day2)

    assert totals.worksite_duration_minutes == 1080
    assert totals.logged_work_minutes == 30


def test_work_hours_week_sms_command_returns_summary(db_session):
    session, settings = db_session
    result = process_inbound_text(
        session,
        "work hours week",
        settings=settings,
        now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
    )

    assert "Work hours this week" in result.reply


def test_fix_arrived_and_left_work_commands_recalculate_summary(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 12, 0, tzinfo=settings.timezone)

    process_inbound_text(session, "fix arrived work 7:35am", settings=settings, now=now)
    process_inbound_text(session, "fix left work 5:20pm", settings=settings, now=now)
    summary = session.scalar(select(WorkDaySummary).where(WorkDaySummary.date == now.date()))

    assert summary.arrived_work_at.hour == 7
    assert summary.arrived_work_at.minute == 35
    assert summary.left_work_at.hour == 17
    assert summary.left_work_at.minute == 20
    assert summary.worksite_duration_minutes == 585


def test_work_hours_dashboard_route_renders(tmp_path):
    settings = Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        reports_dir=str(tmp_path / "reports"),
    )
    engine = create_engine_from_url(settings.database_url)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    app = create_app(settings=settings, engine=engine, session_factory=TestingSession)

    with TestClient(app) as client:
        response = client.get("/dashboard/work-hours")
        assert response.status_code == 200
        assert "Work Hours" in response.text
        assert "Daily Summaries" in response.text

    engine.dispose()
