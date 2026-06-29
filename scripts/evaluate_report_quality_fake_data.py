from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy.orm import sessionmaker

from time_secretary.briefing_service import generate_briefing
from time_secretary.classification_service import add_project, seed_default_data
from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.project_memory_service import create_project_note
from time_secretary.todo_service import create_todo_from_text
from time_secretary.work_intelligence_service import create_process_change, create_process_observation, create_run_metric


def _output_dir(settings: Settings) -> Path:
    path = Path(settings.reports_dir)
    if not path.is_absolute():
        path = ROOT_DIR / path
    path = path / "llm_evaluations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate report quality against a fake local dataset.")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    parser.add_argument("--llm", action="store_true", help="Enable local Ollama report pass for the fake dataset.")
    args = parser.parse_args()

    base_settings = Settings.from_env(args.env_file)
    output_dir = _output_dir(base_settings)
    now = datetime(2026, 6, 29, 9, 0, tzinfo=base_settings.timezone)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        settings = replace(
            base_settings,
            database_url=f"sqlite:///{temp_path / 'fake-eval.db'}",
            reports_dir=str(temp_path / "reports"),
            briefing_reports_dir=str(temp_path / "reports" / "briefings"),
            llm_reports_enabled=args.llm,
            include_sensitive_local_reports=False,
        )
        engine = create_engine_from_url(settings.database_url)
        init_db(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        try:
            with SessionLocal() as session:
                seed_default_data(session)
                project = add_project(session, "Example Project Run", aliases=["EPR"], category="Work")
                create_project_note(
                    session,
                    "EPR decision: use the lower setting after item A changed.",
                    settings=settings,
                    note_type="decision",
                    title="Lower setting",
                    body="Use the lower setting after item A changed.",
                    now=now,
                )
                create_todo_from_text(
                    session,
                    "todo confirm EPR review checklist before the next run",
                    settings=settings,
                    now=now,
                )
                create_process_change(
                    session,
                    "Run EPR-001: changed setting to setting 5",
                    settings=settings,
                    project_name=project.name,
                    run_name="EPR-001",
                    now=now,
                )
                create_process_observation(
                    session,
                    "Observation: item A changed at the top edge",
                    settings=settings,
                    project_name=project.name,
                    run_name="EPR-001",
                    now=now,
                )
                create_run_metric(
                    session,
                    "Quality score 2.1",
                    settings=settings,
                    project_name=project.name,
                    run_name="EPR-001",
                    now=now,
                )
                result = generate_briefing(
                    session,
                    "meeting prep EPR",
                    settings=settings,
                    request_source="evaluation",
                    include_sensitive=False,
                    now=now,
                )
                session.commit()
                report = result.report
                if report is None:
                    raise RuntimeError(result.message)
                final_text = report.full_text or ""
                score = {
                    "mentions_decision": "lower setting" in final_text,
                    "mentions_todo": "review checklist" in final_text,
                    "mentions_change": "setting 5" in final_text,
                    "mentions_observation": "changed" in final_text,
                    "mentions_metric": "Quality score" in final_text or "2.1" in final_text,
                    "generation_mode": report.generation_mode,
                }
        finally:
            engine.dispose()

    passed = sum(1 for key, value in score.items() if key.startswith("mentions_") and value)
    payload = {
        "generated_at": datetime.now(base_settings.timezone).isoformat(),
        "passed_checks": passed,
        "total_checks": 5,
        "score": score,
    }
    stem = datetime.now(base_settings.timezone).strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"{stem}-fake-report-eval.json"
    md_path = output_dir / f"{stem}-fake-report-eval.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(
        "\n".join(
            [
                "# Fake Report Quality Evaluation",
                "",
                f"- Passed checks: {passed}/5",
                f"- Generation mode: {score['generation_mode']}",
                f"- Decision: {score['mentions_decision']}",
                f"- Todo: {score['mentions_todo']}",
                f"- Change: {score['mentions_change']}",
                f"- Observation: {score['mentions_observation']}",
                f"- Metric: {score['mentions_metric']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json_path)
    print(md_path)
    return 0 if passed >= 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
