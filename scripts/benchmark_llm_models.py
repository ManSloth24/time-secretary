from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy.orm import sessionmaker

from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.llm.model_presets import REPORT_MODEL_PRESETS
from time_secretary.llm_report_service import installed_ollama_models, ollama_report_available, run_llm_report_test


def _output_dir(settings: Settings) -> Path:
    path = Path(settings.reports_dir)
    if not path.is_absolute():
        path = ROOT_DIR / path
    path = path / "llm_benchmarks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark safe fake report prompts across local Ollama models.")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    parser.add_argument("--models", nargs="*", default=None, help="Ollama model names to test.")
    args = parser.parse_args()

    settings = Settings.from_env(args.env_file)
    output_dir = _output_dir(settings)
    started_at = datetime.now(settings.timezone)
    models = args.models or [
        preset.model for preset in REPORT_MODEL_PRESETS.values() if preset.model
    ]
    installed = set(installed_ollama_models(settings))
    results: list[dict[str, object]] = []

    if not ollama_report_available(settings):
        results.append({"status": "skipped", "reason": "Ollama unavailable"})
    else:
        engine = create_engine_from_url(settings.database_url)
        init_db(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        try:
            for model in models:
                if installed and model not in installed:
                    results.append({"model": model, "status": "skipped", "reason": "model not installed"})
                    continue
                test_settings = replace(
                    settings,
                    llm_reports_enabled=True,
                    llm_report_provider="ollama",
                    llm_report_model=model,
                    llm_report_cache_enabled=False,
                )
                with SessionLocal() as session:
                    result = run_llm_report_test(session, test_settings)
                    session.commit()
                results.append(
                    {
                        "model": model,
                        "status": "passed" if result.success else "fallback",
                        "duration_ms": result.duration_ms,
                        "error": result.error_message,
                        "warnings": result.validation_warnings or [],
                    }
                )
        finally:
            engine.dispose()

    payload = {
        "started_at": started_at.isoformat(),
        "models_requested": models,
        "installed_models": sorted(installed),
        "results": results,
    }
    stem = started_at.strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"{stem}-llm-benchmark.json"
    md_path = output_dir / f"{stem}-llm-benchmark.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# LLM Report Model Benchmark", ""]
    for item in results:
        label = item.get("model") or "all"
        lines.append(f"- {label}: {item.get('status')}")
        if item.get("duration_ms") is not None:
            lines[-1] += f" ({item['duration_ms']} ms)"
        if item.get("reason") or item.get("error"):
            lines[-1] += f" - {item.get('reason') or item.get('error')}"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
