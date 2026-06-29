from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from scripts.export_data import export_data
from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.main import create_app
from time_secretary.models import LLMCall, ProcessChange, ProcessObservation, ProjectNote, RunMetric, SecureCapture, TimeEntry
from time_secretary.report_service import generate_report
from time_secretary.secretary_service import create_time_entry_from_text
from time_secretary.secure_capture_service import process_secure_capture


def _secure_settings(tmp_path, *, enabled: bool = True, token: str = "secret") -> Settings:
    return Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'secure.db'}",
        reports_dir=str(tmp_path / "reports"),
        backups_dir=str(tmp_path / "backups"),
        secure_capture_enabled=enabled,
        secure_capture_token=token,
        export_include_sensitive=False,
    )


def _app_for(settings: Settings):
    engine = create_engine_from_url(settings.database_url)
    init_db(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    app = create_app(settings=settings, engine=engine, session_factory=TestingSession)
    return app, engine, TestingSession


def test_secure_capture_rejects_when_disabled(tmp_path):
    settings = _secure_settings(tmp_path, enabled=False)
    app, engine, _session_factory = _app_for(settings)

    with TestClient(app) as client:
        response = client.post("/secure-capture", json={"secret": "secret", "text": "private note"})
        assert response.status_code == 403

    engine.dispose()


def test_secure_capture_rejects_missing_and_wrong_secret(tmp_path):
    settings = _secure_settings(tmp_path)
    app, engine, _session_factory = _app_for(settings)

    with TestClient(app) as client:
        missing = client.post("/secure-capture", json={"text": "private note"})
        wrong = client.post("/secure-capture", json={"secret": "wrong", "text": "private note"})

    assert missing.status_code == 403
    assert wrong.status_code == 403
    engine.dispose()


def test_valid_secure_work_note_creates_capture_and_project_note(tmp_path):
    settings = _secure_settings(tmp_path)
    app, engine, SessionLocal = _app_for(settings)

    with TestClient(app) as client:
        response = client.post(
            "/secure-capture",
            json={
                "secret": "secret",
                "capture_type": "work_note",
                "text": "Project Alpha private note: item looked inconsistent",
                "project": "Project Alpha",
                "source": "iphone_shortcut",
            },
        )
        assert response.status_code == 200

    with SessionLocal() as session:
        capture = session.scalar(select(SecureCapture))
        note = session.scalar(select(ProjectNote))
        assert capture.sensitivity == "sensitive"
        assert capture.processed_status == "processed"
        assert note.sensitivity == "sensitive"
        assert note.project_name == "Project Alpha"
        assert "item looked inconsistent" not in (capture.raw_payload_json or "")

    engine.dispose()


def test_secure_run_change_observation_and_metric_create_structured_records(db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        secure_capture_token="secret",
    )

    process_secure_capture(
        session,
        {"secret": "secret", "capture_type": "run_change", "text": "Run A-001: changed setting to setting 5", "project": "Project Alpha", "run_name": "A-001"},
        settings=settings,
    )
    process_secure_capture(
        session,
        {"secret": "secret", "capture_type": "observation", "text": "Observation: item inconsistent after review", "project": "Project Alpha", "run_name": "A-001"},
        settings=settings,
    )
    process_secure_capture(
        session,
        {"secret": "secret", "capture_type": "run_metric", "text": "Quality score 2.4", "project": "Project Alpha", "run_name": "A-001"},
        settings=settings,
    )

    assert session.scalar(select(ProcessChange)).change_type == "program_change"
    assert "inconsistent" in session.scalar(select(ProcessObservation)).observation_text
    assert session.scalar(select(RunMetric)).metric_value_numeric == 2.4


def test_secure_capture_does_not_use_llm_or_twilio(db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        secure_capture_token="secret",
        llm_enabled=True,
        llm_provider="ollama",
        llm_allow_work_notes=False,
        secure_capture_allow_llm=False,
    )

    process_secure_capture(
        session,
        {"secret": "secret", "capture_type": "work_note", "text": "sensitive configuration detail"},
        settings=settings,
    )

    assert session.scalar(select(LLMCall)) is None


def test_work_focus_classification_for_admin_and_strategic_tasks(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 15, tzinfo=settings.timezone)

    admin = create_time_entry_from_text(session, "answered emails", settings=settings, now=now)
    strategic = create_time_entry_from_text(session, "decided next project path for Project Alpha", settings=settings, now=now.replace(hour=10, minute=15))

    assert admin.work_focus_type == "admin_reactive"
    assert admin.value_level == "low"
    assert strategic.work_focus_type == "strategic_contribution"
    assert strategic.value_level == "high"


def test_repeated_routine_task_creates_delegation_and_staffing_signal(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 15, tzinfo=settings.timezone)

    entries = [
        create_time_entry_from_text(session, "manually formatted report again", settings=settings, now=now.replace(hour=9 + index, minute=15))
        for index in range(3)
    ]

    assert all(entry.delegation_candidate for entry in entries)
    assert entries[-1].staffing_signal is True


def test_work_intelligence_dashboard_route_renders(tmp_path):
    settings = _secure_settings(tmp_path)
    app, engine, _session_factory = _app_for(settings)

    with TestClient(app) as client:
        response = client.get("/dashboard/work-intelligence")
        assert response.status_code == 200
        assert "Work Intelligence" in response.text
        assert "Delegation Candidates" in response.text

    engine.dispose()


def test_secure_captures_dashboard_displays_received_time_in_local_timezone(tmp_path):
    settings = _secure_settings(tmp_path)
    app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        session.add(
            SecureCapture(
                capture_type="work_note",
                text="timezone display test",
                source="iphone_shortcut",
                sensitivity="sensitive",
                processed_status="processed",
                created_at=datetime(2026, 6, 27, 23, 28, tzinfo=settings.timezone),
                received_at=datetime(2026, 6, 28, 3, 28),
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get("/dashboard/secure-captures")

    assert response.status_code == 200
    assert "received 2026-06-27 23:28 EDT" in response.text
    assert "received 2026-06-28 03:28" not in response.text
    engine.dispose()


def test_reports_include_delegation_staffing_and_yearly_ytd(db_session):
    session, settings = db_session
    now = datetime(2026, 6, 22, 9, 15, tzinfo=settings.timezone)
    for index in range(3):
        create_time_entry_from_text(session, "manually formatted report again", settings=settings, now=now.replace(hour=9 + index, minute=15))

    weekly = generate_report(session, "weekly", settings=settings, now=now)
    monthly = generate_report(session, "monthly", settings=settings, now=now)
    yearly = generate_report(session, "yearly", settings=settings, now=now)

    assert "Delegation Candidates" in weekly.markdown
    assert "Staffing Signals" in monthly.markdown
    assert "Project Allocation" in yearly.markdown
    assert "Work Focus Trends" in yearly.markdown


def test_secure_capture_details_are_not_in_outbound_sms_summary(db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        secure_capture_token="secret",
    )
    process_secure_capture(
        session,
        {
            "secret": "secret",
            "capture_type": "work_note",
            "text": "secret project detail",
            "project": "Project Alpha",
            "created_at": "2026-06-22T09:15:00-04:00",
        },
        settings=settings,
    )

    report = generate_report(session, "daily", settings=settings, now=datetime(2026, 6, 22, 9, 15, tzinfo=settings.timezone))

    assert "secure note" in report.summary_text
    assert "secret project detail" not in report.summary_text


def test_secure_capture_export_redacts_sensitive_text(tmp_path):
    settings = _secure_settings(tmp_path)
    _app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        process_secure_capture(
            session,
            {"secret": "secret", "capture_type": "work_note", "text": "private project detail"},
            settings=settings,
        )
        session.commit()
    engine.dispose()

    paths = export_data(settings=settings, output_dir=tmp_path / "exports")
    secure_csv = next(path for path in paths if path.name == "secure_captures.csv")
    contents = secure_csv.read_text(encoding="utf-8")

    assert "[redacted sensitive text]" in contents
    assert "private project detail" not in contents
