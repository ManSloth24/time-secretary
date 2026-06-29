from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url
from time_secretary.main import create_app
from time_secretary.models import TimeEntry
from time_secretary.report_service import generate_report
from time_secretary.secretary_service import process_inbound_text
from time_secretary.sms_service import mask_phone_number, validate_twilio_webhook


def test_daily_report_contains_secretary_sections(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)
    process_inbound_text(session, "worked on Project Alpha report", settings=settings, now=now)
    process_inbound_text(session, "todo finish project beta writeup by Friday", settings=settings, now=now)
    process_inbound_text(session, "remind me tomorrow to check the project update", settings=settings, now=now)
    process_inbound_text(session, "decision Project Alpha: use lower review threshold", settings=settings, now=now)

    report = generate_report(session, "daily", settings=settings, now=now)

    assert "## Secretary" in report.markdown
    assert "Open And Overdue Todos" in report.markdown
    assert "use lower review threshold" in report.markdown
    assert report.path is not None


def test_masking_phone_numbers():
    assert mask_phone_number("+15551234567") == "***-***-4567"
    assert mask_phone_number(None) == "unknown"


def test_webhook_validation_dev_vs_production():
    dev = Settings(dev_mode=True, require_twilio_signature_validation=True)
    assert validate_twilio_webhook(settings=dev, url="https://example.test/sms", form_data={}, signature=None)

    prod = Settings(dev_mode=False, require_twilio_signature_validation=True, twilio_auth_token="token")
    assert not validate_twilio_webhook(settings=prod, url="https://example.test/sms", form_data={}, signature=None)

    relaxed = Settings(dev_mode=False, require_twilio_signature_validation=False)
    assert validate_twilio_webhook(settings=relaxed, url="https://example.test/sms", form_data={}, signature=None)


def test_dashboard_correction_flow(tmp_path):
    settings = Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        reports_dir=str(tmp_path / "reports"),
    )
    engine = create_engine_from_url(settings.database_url)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    app = create_app(settings=settings, engine=engine, session_factory=TestingSession)

    with TestClient(app) as client:
        response = client.post(
            "/sms/inbound",
            data={"From": "+15551234567", "To": "+15557654321", "Body": "lunch", "MessageSid": "SM1"},
        )
        assert response.status_code == 200
        with TestingSession() as session:
            entry = session.scalar(select(TimeEntry).order_by(TimeEntry.id.desc()))
            assert entry.category_primary == "Unknown"
            entry_id = entry.id

        response = client.post(
            f"/entries/{entry_id}/correct",
            data={"category_primary": "Home", "category_secondary": "meal", "project_name": ""},
        )
        assert response.status_code == 200

        with TestingSession() as session:
            entry = session.get(TimeEntry, entry_id)
            assert entry.category_primary == "Home"
            assert entry.category_secondary == "meal"

    engine.dispose()


def test_settings_dashboard_renders_and_tests_disabled_llm(tmp_path):
    settings = Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'settings.db'}",
        reports_dir=str(tmp_path / "reports"),
        backups_dir=str(tmp_path / "backups"),
    )
    engine = create_engine_from_url(settings.database_url)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    app = create_app(settings=settings, engine=engine, session_factory=TestingSession)

    with TestClient(app) as client:
        response = client.get("/dashboard/settings")
        assert response.status_code == 200
        assert "Settings" in response.text
        assert "LLM" in response.text

        response = client.post("/dashboard/settings/action", data={"action": "test_llm"}, follow_redirects=False)
        assert response.status_code == 303
        assert "LLM+none%3A+available" in response.headers["location"]

    engine.dispose()
