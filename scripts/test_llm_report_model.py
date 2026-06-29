from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy.orm import sessionmaker

from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.llm_report_service import run_llm_report_test


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a safe local LLM report test prompt.")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    args = parser.parse_args()

    settings = Settings.from_env(args.env_file)
    engine = create_engine_from_url(settings.database_url)
    init_db(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    try:
        with SessionLocal() as session:
            result = run_llm_report_test(session, settings)
            session.commit()
        if result.success:
            source = "cache" if result.from_cache else "model"
            print(f"PASS: local report model responded via {source}: {result.model or settings.llm_report_model}")
            if result.duration_ms is not None:
                print(f"duration_ms={result.duration_ms}")
        else:
            print(f"SKIP/FALLBACK: {result.error_message or 'LLM reports are not enabled'}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
