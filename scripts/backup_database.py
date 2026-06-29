from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from time_secretary.config import Settings


class BackupError(RuntimeError):
    pass


def _resolve_from_root(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


def _snapshot_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source)
    dest_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()


def _prune_old_backups(backups_dir: Path, retention_days: int, now: datetime) -> None:
    if retention_days <= 0:
        return
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    for backup in backups_dir.glob("time-secretary-backup-*.zip"):
        if backup.stat().st_mtime < cutoff:
            backup.unlink()


def create_backup(
    *,
    settings: Settings | None = None,
    backup_dir: Path | None = None,
    include_reports: bool = True,
    now: datetime | None = None,
) -> Path:
    settings = settings or Settings.from_env(ROOT_DIR / ".env")
    now = now or datetime.now(settings.timezone)
    database_path = settings.database_path
    if database_path is None:
        raise BackupError("Only SQLite DATABASE_URL values can be backed up by this script.")
    database_path = _resolve_from_root(str(database_path))
    if not database_path.exists():
        raise BackupError(f"Database file does not exist: {database_path}")

    backups_dir = backup_dir or _resolve_from_root(settings.backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    filename = f"time-secretary-backup-{now.strftime('%Y%m%d-%H%M%S')}.zip"
    archive_path = backups_dir / filename

    manifest = {
        "created_at": now.isoformat(),
        "deployment_mode": settings.deployment_mode,
        "database_url": settings.database_url,
        "reports_dir": settings.reports_dir,
        "app_timezone": settings.app_timezone,
        "contains_env_file": False,
        "layout": {
            "database": f"data/{database_path.name}",
            "reports": "reports/",
        },
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        snapshot_path = Path(temp_dir) / database_path.name
        _snapshot_sqlite_database(database_path, snapshot_path)
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.write(snapshot_path, f"data/{database_path.name}")
            reports_dir = _resolve_from_root(settings.reports_dir)
            if include_reports and reports_dir.exists():
                for report in sorted(path for path in reports_dir.rglob("*") if path.is_file()):
                    archive.write(report, "reports/" + report.relative_to(reports_dir).as_posix())
            briefing_reports_dir = _resolve_from_root(settings.briefing_reports_dir)
            if (
                include_reports
                and briefing_reports_dir.exists()
                and briefing_reports_dir != reports_dir
                and reports_dir not in briefing_reports_dir.parents
            ):
                for report in sorted(path for path in briefing_reports_dir.rglob("*") if path.is_file()):
                    archive.write(
                        report,
                        "reports/briefings/" + report.relative_to(briefing_reports_dir).as_posix(),
                    )
            env_example = ROOT_DIR / ".env.example"
            if env_example.exists():
                archive.write(env_example, ".env.example")
            readme = ROOT_DIR / "README.md"
            if readme.exists():
                archive.write(readme, "README.md")
            archive.writestr("backup_manifest.json", json.dumps(manifest, indent=2))

    _prune_old_backups(backups_dir, settings.backup_retention_days, now)
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Time Secretary backup archive.")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--skip-reports", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env(args.env_file)
    try:
        path = create_backup(
            settings=settings,
            backup_dir=Path(args.backup_dir).resolve() if args.backup_dir else None,
            include_reports=not args.skip_reports,
        )
    except BackupError as exc:
        print(f"Backup failed: {exc}")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
