from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from time_secretary.classification_service import seed_default_data
from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db


@pytest.fixture()
def db_session(tmp_path) -> Generator[tuple[Session, Settings], None, None]:
    settings = Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        reports_dir=str(tmp_path / "reports"),
        save_reports_to_disk=True,
        require_twilio_signature_validation=True,
    )
    engine = create_engine_from_url(settings.database_url)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    init_db(bind=engine)
    with TestingSession() as session:
        seed_default_data(session)
        yield session, settings
        session.rollback()
    engine.dispose()
