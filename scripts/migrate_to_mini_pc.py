from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from time_secretary.config import Settings


class RestoreError(RuntimeError):
    pass


def _resolve_under_root(root: Path, path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _backup_existing_file(path: Path, now: datetime) -> Path | None:
    if not path.exists():
        return None
    backup_path = path.with_name(path.name + f".pre_restore_{now.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup_path)
    return backup_path


def restore_backup(
    archive_path: Path,
    *,
    settings: Settings | None = None,
    target_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Path | None]:
    target_root = (target_root or ROOT_DIR).resolve()
    settings = settings or Settings.from_env(target_root / ".env")
    now = now or datetime.now(settings.timezone)
    archive_path = archive_path.resolve()
    if not archive_path.exists():
        raise RestoreError(f"Backup archive does not exist: {archive_path}")

    database_path = settings.database_path
    if database_path is None:
        raise RestoreError("Only SQLite DATABASE_URL values can be restored by this script.")
    database_path = _resolve_under_root(target_root, str(database_path))
    reports_dir = _resolve_under_root(target_root, settings.reports_dir)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        with ZipFile(archive_path, "r") as archive:
            archive.extractall(temp_path)

        manifest_path = temp_path / "backup_manifest.json"
        manifest = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        source_database_name = manifest.get("layout", {}).get("database", "")
        source_database = temp_path / source_database_name if source_database_name else None
        if source_database is None or not source_database.exists():
            data_files = sorted((temp_path / "data").glob("*.db"))
            source_database = data_files[0] if data_files else None
        if source_database is None or not source_database.exists():
            raise RestoreError("Backup archive does not contain a SQLite database under data/.")

        database_path.parent.mkdir(parents=True, exist_ok=True)
        old_database_backup = _backup_existing_file(database_path, now)
        shutil.copy2(source_database, database_path)

        source_reports = temp_path / "reports"
        if source_reports.exists():
            reports_dir.mkdir(parents=True, exist_ok=True)
            for source_report in sorted(path for path in source_reports.rglob("*") if path.is_file()):
                relative = source_report.relative_to(source_reports)
                destination = reports_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_report, destination)

    return {
        "database": database_path,
        "old_database_backup": old_database_backup,
        "reports_dir": reports_dir,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a Time Secretary backup on the mini PC.")
    parser.add_argument("archive")
    parser.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    parser.add_argument("--target-root", default=str(ROOT_DIR))
    args = parser.parse_args()

    target_root = Path(args.target_root).resolve()
    settings = Settings.from_env(args.env_file)
    try:
        result = restore_backup(
            Path(args.archive),
            settings=settings,
            target_root=target_root,
        )
    except RestoreError as exc:
        print(f"Restore failed: {exc}")
        return 1
    for key, path in result.items():
        if path is not None:
            print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
