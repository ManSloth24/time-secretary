from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from time_secretary.briefing_service import generate_briefing
from time_secretary.classification_service import seed_default_data
from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.llm_report_validator import validate_llm_report
from time_secretary.models import BriefingReport, LLMCall, LLMReportCache, SecureCapture
from time_secretary.project_memory_service import create_project_note
from time_secretary.todo_service import create_todo_from_text


def _settings(tmp_path) -> Settings:
    return Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'llm-reports.db'}",
        reports_dir=str(tmp_path / "reports"),
        backups_dir=str(tmp_path / "backups"),
        briefing_reports_dir=str(tmp_path / "reports" / "briefings"),
        require_twilio_signature_validation=True,
    )


def _db(settings: Settings):
    engine = create_engine_from_url(settings.database_url)
    init_db(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with TestingSession() as session:
        seed_default_data(session)
    return engine, TestingSession


def test_fact_pack_excludes_sensitive_text_until_global_gate_enabled(tmp_path):
    settings = _settings(tmp_path)
    engine, SessionLocal = _db(settings)
    now = datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone)
    with SessionLocal() as session:
        session.add(
            SecureCapture(
                capture_type="work_note",
                text="private project note formula",
                source="iphone_shortcut",
                sensitivity="sensitive",
                processed_status="processed",
                created_at=now,
                received_at=now,
            )
        )
        blocked = generate_briefing(session, "brief me on project note", settings=settings, include_sensitive=True, now=now)
        allowed = generate_briefing(
            session,
            "brief me on project note",
            settings=replace(settings, include_sensitive_local_reports=True),
            include_sensitive=True,
            now=now,
        )
        session.commit()
        blocked_fact_pack_path = blocked.report.fact_pack_path
        allowed_fact_pack_path = allowed.report.fact_pack_path

    with open(blocked_fact_pack_path, encoding="utf-8") as handle:
        blocked_pack = json.load(handle)
    with open(allowed_fact_pack_path, encoding="utf-8") as handle:
        allowed_pack = json.load(handle)
    assert "private project note formula" not in json.dumps(blocked_pack)
    assert "private project note formula" in json.dumps(allowed_pack)
    engine.dispose()


def test_llm_reports_disabled_keeps_deterministic_and_no_llm_call(tmp_path):
    settings = _settings(tmp_path)
    engine, SessionLocal = _db(settings)
    with SessionLocal() as session:
        result = generate_briefing(session, "brief me on Project Alpha", settings=settings)
        session.commit()
        report = result.report
        assert report is not None
        assert report.generation_mode == "deterministic"
        assert "Local LLM Narrative" not in (report.full_text or "")
        assert session.scalar(select(LLMCall)) is None
    engine.dispose()


def test_ollama_unavailable_falls_back_and_logs_failure(tmp_path, monkeypatch):
    settings = replace(_settings(tmp_path), llm_reports_enabled=True)
    engine, SessionLocal = _db(settings)
    monkeypatch.setattr("time_secretary.llm_report_service.ollama_report_available", lambda _settings: False)
    with SessionLocal() as session:
        result = generate_briefing(session, "brief me on Project Alpha", settings=settings)
        session.commit()
        assert result.report is not None
        assert result.report.generation_mode == "deterministic"
        call = session.scalar(select(LLMCall))
        assert call is not None
        assert call.success is False
        assert "Ollama is unavailable" in (call.error_message or "")
    engine.dispose()


def test_llm_assisted_briefing_and_cache_hit(tmp_path, monkeypatch):
    settings = replace(
        _settings(tmp_path),
        llm_reports_enabled=True,
        llm_report_model="fake-report-model",
        llm_report_cache_enabled=True,
    )
    engine, SessionLocal = _db(settings)
    now = datetime(2026, 6, 29, 9, 0, tzinfo=settings.timezone)
    calls = {"count": 0}

    def fake_call(_settings, _prompt):
        calls["count"] += 1
        return (
            json.dumps(
                {
                    "summary": "Found one open action for Project Alpha.",
                    "priorities": ["Review Project Alpha follow-up risk."],
                    "risks": ["follow-up risk needs review."],
                    "questions": ["Who owns the follow-up risk review?"],
                    "next_actions": ["Review Project Alpha follow-up risk."],
                    "narrative_markdown": "Use the documented todo as the next action.",
                }
            ),
            25,
            50,
        )

    monkeypatch.setattr("time_secretary.llm_report_service.ollama_report_available", lambda _settings: True)
    monkeypatch.setattr("time_secretary.llm_report_service._call_ollama_report", fake_call)
    with SessionLocal() as session:
        create_project_note(session, "Project Alpha decision: use lower setting", settings=settings, note_type="decision", body="use lower setting", now=now)
        create_todo_from_text(session, "todo review Project Alpha follow-up risk", settings=settings, now=now)
        first = generate_briefing(session, "meeting prep Project Alpha", settings=settings, request_source="dashboard", now=now)
        second = generate_briefing(session, "meeting prep Project Alpha", settings=settings, request_source="dashboard", now=now)
        session.commit()
        assert first.report is not None
        assert second.report is not None
        assert first.report.generation_mode == "llm_assisted"
        assert second.report.generation_mode == "llm_assisted"
        assert "Local LLM Narrative" in (first.report.full_text or "")
        assert first.report.llm_narrative_path is not None
        assert session.scalar(select(LLMReportCache)) is not None
    assert calls["count"] == 1
    engine.dispose()


def test_validator_flags_unknown_project_and_hour_mismatch():
    fact_pack = {
        "scope": {"project": {"name": "KnownProject", "aliases": []}},
        "time_totals": {"total_hours": 2.0},
        "sensitive_policy": {"contains_sensitive_text": False},
    }
    result = validate_llm_report("OtherProject took 7 hours.", fact_pack)
    assert result.ok is False
    assert any("unknown project-like" in warning for warning in result.warnings)
    assert any("unsupported hour claim" in warning for warning in result.warnings)


def test_benchmark_script_skips_missing_ollama(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"DATABASE_URL=sqlite:///{tmp_path / 'bench.db'}",
                f"REPORTS_DIR={tmp_path / 'reports'}",
                "LLM_REPORTS_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    import scripts.benchmark_llm_models as benchmark

    monkeypatch.setattr(benchmark, "ollama_report_available", lambda _settings: False)
    monkeypatch.setattr(benchmark, "installed_ollama_models", lambda _settings: [])
    monkeypatch.setattr(sys, "argv", ["benchmark_llm_models.py", "--env-file", str(env_file)])
    assert benchmark.main() == 0
    assert list((tmp_path / "reports" / "llm_benchmarks").glob("*.json"))


def test_fake_data_evaluation_script_runs(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"REPORTS_DIR={tmp_path / 'reports'}",
                "LLM_REPORTS_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    import scripts.evaluate_report_quality_fake_data as evaluator

    monkeypatch.setattr(sys, "argv", ["evaluate_report_quality_fake_data.py", "--env-file", str(env_file)])
    assert evaluator.main() == 0
    assert list((tmp_path / "reports" / "llm_evaluations").glob("*.json"))
