from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from scripts.backup_database import create_backup
from scripts.export_data import export_data
from time_secretary.briefing_service import generate_briefing
from time_secretary.classification_service import add_project, seed_default_data
from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.models import BriefingReport, BriefingRequest, LLMCall, Project, SecureCapture
from time_secretary.project_memory_service import create_project_note
from time_secretary.secretary_service import process_inbound_text
from time_secretary.todo_service import create_todo_from_text
from time_secretary.work_intelligence_service import create_process_change, create_process_observation, create_run_metric


def _settings(tmp_path, *, token: str = "secret") -> Settings:
    return Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'briefings.db'}",
        reports_dir=str(tmp_path / "reports"),
        backups_dir=str(tmp_path / "backups"),
        briefing_reports_dir=str(tmp_path / "reports" / "briefings"),
        briefing_tailscale_base_url="http://tail.test:8002",
        secure_capture_token=token,
        export_include_sensitive=False,
        require_twilio_signature_validation=True,
    )


def _app_for(settings: Settings):
    engine = create_engine_from_url(settings.database_url)
    init_db(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with TestingSession() as session:
        seed_default_data(session)
    app = create_app(settings=settings, engine=engine, session_factory=TestingSession)
    return app, engine, TestingSession


from time_secretary.main import create_app


def test_sms_briefing_request_creates_report_and_returns_opaque_link_only(tmp_path):
    settings = _settings(tmp_path)
    app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        create_project_note(
            session,
            "Project Alpha: private project detail",
            settings=settings,
            title="Review detail",
            body="private project detail",
            now=datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone),
        )
        session.commit()

    with TestClient(app) as client:
        response = client.post(
            "/sms/inbound",
            data={"From": "+15551234567", "To": "+15557654321", "Body": "brief me on Project Alpha", "MessageSid": "SMB1"},
        )

    assert response.status_code == 200
    assert "Briefing ready:" in response.text
    assert "/dashboard/briefings/" in response.text
    assert "Project Alpha" not in response.text
    assert "private project detail" not in response.text
    with SessionLocal() as session:
        report = session.scalar(select(BriefingReport))
        request = session.scalar(select(BriefingRequest))
        assert report is not None
        assert request is not None
        assert report.opaque_id not in {"Project Alpha", "Project-Alpha"}
        assert "private project detail" in (report.full_text or "")
        assert report.markdown_path is not None
    engine.dispose()


def test_secure_capture_briefing_request_returns_local_url(tmp_path):
    settings = _settings(tmp_path)
    app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        create_todo_from_text(
            session,
            "todo finish Project Alpha meeting slides",
            settings=settings,
            now=datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone),
        )
        session.commit()

    with TestClient(app) as client:
        response = client.post(
            "/secure-capture",
            json={
                "secret": "secret",
                "capture_type": "briefing_request",
                "text": "Generate a meeting prep report on Project Alpha",
                "source": "iphone_shortcut",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Briefing ready."
    assert data["local_url"].startswith("http://tail.test:8002/dashboard/briefings/")
    with SessionLocal() as session:
        assert session.scalar(select(BriefingReport)) is not None
        assert session.scalar(select(SecureCapture)).processed_status == "processed"
    engine.dispose()


def test_briefing_includes_local_project_context(tmp_path):
    settings = _settings(tmp_path)
    app, engine, SessionLocal = _app_for(settings)
    now = datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone)
    with SessionLocal() as session:
        create_project_note(session, "Project Alpha decision: use lower setting", settings=settings, note_type="decision", body="use lower setting", now=now)
        create_todo_from_text(session, "todo review Project Alpha follow-up risk", settings=settings, now=now)
        create_process_change(session, "Run A-001: changed Project Alpha setting to setting 5", settings=settings, project_name="Project Alpha", run_name="A-001", now=now)
        create_process_observation(session, "Observation: Project Alpha item inconsistent", settings=settings, project_name="Project Alpha", run_name="A-001", now=now)
        create_run_metric(session, "Quality score 2.4", settings=settings, project_name="Project Alpha", run_name="A-001", now=now)
        result = generate_briefing(session, "meeting prep Project Alpha", settings=settings, request_source="dashboard", include_sensitive=False, now=now)
        assert result.report is not None
        text = result.report.full_text or ""
        session.commit()

    assert "use lower setting" in text
    assert "review Project Alpha follow-up risk" in text
    assert "changed Project Alpha setting" in text
    assert "item inconsistent" in text
    assert "Quality score" in text
    engine.dispose()


def test_sensitive_secure_capture_text_requires_global_local_report_gate(tmp_path):
    settings = _settings(tmp_path)
    _app, engine, SessionLocal = _app_for(settings)
    now = datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone)
    with SessionLocal() as session:
        session.add(
            SecureCapture(
                capture_type="work_note",
                text="private configuration placeholder detail",
                source="iphone_shortcut",
                sensitivity="sensitive",
                processed_status="processed",
                created_at=now,
                received_at=now,
            )
        )
        safe = generate_briefing(session, "brief me on private configuration", settings=settings, request_source="sms", include_sensitive=False, now=now)
        blocked = generate_briefing(session, "brief me on private configuration", settings=settings, request_source="dashboard", include_sensitive=True, now=now)
        allowed_settings = replace(settings, include_sensitive_local_reports=True)
        sensitive = generate_briefing(session, "brief me on private configuration", settings=allowed_settings, request_source="dashboard", include_sensitive=True, now=now)
        assert safe.report is not None
        assert blocked.report is not None
        assert sensitive.report is not None
        safe_text = safe.report.full_text or ""
        blocked_text = blocked.report.full_text or ""
        sensitive_text = sensitive.report.full_text or ""
        session.commit()

    assert "private configuration placeholder detail" not in safe_text
    assert "private configuration placeholder detail" not in blocked_text
    assert "private configuration placeholder detail" in sensitive_text
    engine.dispose()


def test_multiple_project_matches_need_clarification(tmp_path):
    settings = _settings(tmp_path)
    _app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        alpha = Project(name="Alpha", category_default="Work", active=True)
        alpha.aliases = ["cell"]
        beta = Project(name="Beta", category_default="Work", active=True)
        beta.aliases = ["cell"]
        session.add_all([alpha, beta])
        session.flush()
        result = generate_briefing(session, "brief me on cell", settings=settings, request_source="sms")

    assert result.report is None
    assert result.request.status == "needs_clarification"
    assert "more specific" in result.message
    engine.dispose()


def test_briefing_dashboard_routes_render(tmp_path):
    settings = _settings(tmp_path)
    app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        result = generate_briefing(session, "brief me on Project Alpha", settings=settings, request_source="dashboard")
        session.commit()
        opaque_id = result.report.opaque_id

    with TestClient(app) as client:
        list_response = client.get("/dashboard/briefings")
        detail_response = client.get(f"/dashboard/briefings/{opaque_id}")

    assert list_response.status_code == 200
    assert "Briefings" in list_response.text
    assert detail_response.status_code == 200
    assert "Full Local Briefing" in detail_response.text
    engine.dispose()


def test_briefing_generation_does_not_call_llm(tmp_path):
    settings = _settings(tmp_path)
    settings = replace(settings, llm_enabled=True, llm_provider="ollama")
    _app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        generate_briefing(session, "brief me on Project Alpha", settings=settings, request_source="sms")
        assert session.scalar(select(LLMCall)) is None
    engine.dispose()


def test_briefing_export_redacts_sensitive_full_text(tmp_path):
    settings = replace(_settings(tmp_path), include_sensitive_local_reports=True)
    _app, engine, SessionLocal = _app_for(settings)
    now = datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone)
    with SessionLocal() as session:
        session.add(
            SecureCapture(
                capture_type="work_note",
                text="private project placeholder detail",
                source="iphone_shortcut",
                sensitivity="sensitive",
                processed_status="processed",
                created_at=now,
                received_at=now,
            )
        )
        generate_briefing(session, "brief me on private project", settings=settings, request_source="dashboard", include_sensitive=True, now=now)
        session.commit()
    engine.dispose()

    paths = export_data(settings=settings, output_dir=tmp_path / "exports")
    briefing_csv = next(path for path in paths if path.name == "briefing_reports.csv")
    contents = briefing_csv.read_text(encoding="utf-8")
    assert "[redacted sensitive briefing]" in contents
    assert "private project placeholder detail" not in contents


def test_backup_includes_briefing_markdown(tmp_path):
    settings = _settings(tmp_path)
    _app, engine, SessionLocal = _app_for(settings)
    with SessionLocal() as session:
        generate_briefing(session, "brief me on Project Alpha", settings=settings, request_source="dashboard")
        session.commit()
    engine.dispose()

    backup = create_backup(settings=settings, backup_dir=tmp_path / "backups", now=datetime(2026, 6, 29, 12, 0, tzinfo=settings.timezone))
    with ZipFile(backup) as archive:
        names = archive.namelist()

    assert any(name.startswith("reports/briefings/") and name.endswith(".md") for name in names)
