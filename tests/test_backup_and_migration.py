from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker

from scripts.backup_database import create_backup
from scripts.export_data import export_data
from scripts.migrate_to_mini_pc import restore_backup
from time_secretary.classification_service import seed_default_data
from time_secretary.config import Settings
from time_secretary.database import create_engine_from_url, init_db
from time_secretary.migration_service import run_startup_migrations
from time_secretary.models import TimeEntry
from time_secretary.scheduler_service import backup_database_job
from time_secretary.secretary_service import process_inbound_text


def _settings_for(tmp_path: Path, name: str) -> Settings:
    return Settings(
        dev_mode=True,
        database_url=f"sqlite:///{tmp_path / name / 'data' / 'time_secretary.db'}",
        reports_dir=str(tmp_path / name / "reports"),
        backups_dir=str(tmp_path / name / "backups"),
    )


def _create_seeded_database(settings: Settings) -> None:
    engine = create_engine_from_url(settings.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    init_db(bind=engine)
    with SessionLocal() as session:
        seed_default_data(session)
        process_inbound_text(
            session,
            "worked on Project Alpha report",
            settings=settings,
            now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
        )
        session.commit()
    engine.dispose()


def test_backup_archive_contains_database_reports_and_no_env(tmp_path):
    settings = _settings_for(tmp_path, "source")
    _create_seeded_database(settings)
    reports_dir = Path(settings.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "daily.md").write_text("# Daily", encoding="utf-8")

    archive_path = create_backup(
        settings=settings,
        now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
    )

    assert archive_path.exists()
    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "data/time_secretary.db" in names
    assert "reports/daily.md" in names
    assert "backup_manifest.json" in names
    assert ".env" not in names


def test_scheduler_backup_job_uses_backup_settings(tmp_path):
    settings = _settings_for(tmp_path, "source")
    _create_seeded_database(settings)
    engine = create_engine_from_url(settings.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with SessionLocal() as session:
        backup_database_job(
            session,
            settings,
            now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
        )

    backups = list(Path(settings.backups_dir).glob("time-secretary-backup-*.zip"))
    assert len(backups) == 1
    engine.dispose()


def test_export_data_writes_csv_files(tmp_path):
    settings = _settings_for(tmp_path, "source")
    _create_seeded_database(settings)

    paths = export_data(settings=settings, output_dir=tmp_path / "exports")

    exported = {path.name for path in paths}
    assert "time_entries.csv" in exported
    assert "projects.csv" in exported
    time_entries = (tmp_path / "exports" / "time_entries.csv").read_text(encoding="utf-8")
    assert "worked on Project Alpha report" in time_entries


def test_restore_backup_preserves_existing_database_and_reports(tmp_path):
    source_settings = _settings_for(tmp_path, "source")
    _create_seeded_database(source_settings)
    source_reports = Path(source_settings.reports_dir)
    source_reports.mkdir(parents=True, exist_ok=True)
    (source_reports / "daily.md").write_text("# Daily", encoding="utf-8")
    archive_path = create_backup(
        settings=source_settings,
        now=datetime(2026, 6, 22, 9, 0, tzinfo=source_settings.timezone),
    )

    target_settings = _settings_for(tmp_path, "target")
    target_db = target_settings.database_path
    assert target_db is not None
    target_db.parent.mkdir(parents=True, exist_ok=True)
    target_db.write_text("old database", encoding="utf-8")

    result = restore_backup(
        archive_path,
        settings=target_settings,
        target_root=tmp_path / "target",
        now=datetime(2026, 6, 22, 10, 0, tzinfo=target_settings.timezone),
    )

    assert result["database"] == target_db.resolve()
    assert result["old_database_backup"] is not None
    assert result["old_database_backup"].exists()
    assert (Path(target_settings.reports_dir) / "daily.md").exists()

    engine = create_engine_from_url(target_settings.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        entry = session.scalar(select(TimeEntry).order_by(TimeEntry.id.desc()))
        assert entry.raw_text == "worked on Project Alpha report"
    engine.dispose()


def test_startup_migrations_add_missing_columns_only(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE todo_items (id INTEGER PRIMARY KEY, title VARCHAR(240))"))

    actions = run_startup_migrations(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("todo_items")}

    assert "Added todo_items.next_review_at" in actions
    assert "next_review_at" in columns
    assert "capture_status" in columns
    engine.dispose()


def test_deployment_mode_defaults_and_twilio_mode():
    laptop = Settings(deployment_mode="laptop", dev_mode=True, sms_provider="dev", simulate_sms=True)
    assert laptop.effective_dev_sms is True

    mini_pc = Settings(
        deployment_mode="mini_pc",
        dev_mode=False,
        simulate_sms=False,
        sms_provider="twilio",
        twilio_account_sid="AC123",
        twilio_auth_token="token",
        twilio_from_number="+15557654321",
        start_scheduler=True,
    )
    assert mini_pc.effective_dev_sms is False
    assert mini_pc.start_scheduler is True
