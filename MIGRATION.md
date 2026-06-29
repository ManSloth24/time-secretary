# Migration Guide

This guide covers moving Time Secretary data between the laptop and mini PC.

## What Moves

Backup archives include:

- SQLite database snapshot.
- Markdown reports.
- Markdown briefing reports under `reports/briefings/`.
- `.env.example`.
- `README.md`.
- `backup_manifest.json`.

Backup archives do not include:

- `.env`.
- Twilio credentials.
- Real phone numbers outside the database's masked SMS records.
- Python environments or Anaconda packages.

## Create A Backup On The Source Machine

From the project folder:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\backup_database.py
```

The archive is written under `BACKUPS_DIR`, usually:

```text
backups\time-secretary-backup-YYYYMMDD-HHMMSS.zip
```

## Restore On The Target Machine

Copy the archive to the target machine.

From the target project folder:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\migrate_to_mini_pc.py "C:\Path\To\time-secretary-backup-YYYYMMDD-HHMMSS.zip" --target-root "C:\Time Secretary"
```

The script restores the database to the path configured by `DATABASE_URL` and restores reports to `REPORTS_DIR`.

If a database already exists, it is copied first:

```text
time_secretary.db.pre_restore_YYYYMMDD-HHMMSS
```

## After Restore

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m pytest
& 'C:\ProgramData\anaconda3\python.exe' -m uvicorn time_secretary.main:app --host 127.0.0.1 --port 8000
```

Check:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/dashboard
http://127.0.0.1:8000/dashboard/settings
```

## Rollback

Stop the app or scheduled task, then copy the preserved pre-restore database back over the active database.

Example:

```powershell
Stop-ScheduledTask -TaskName 'Time Secretary'
Copy-Item ".\data\time_secretary.db.pre_restore_YYYYMMDD-HHMMSS" ".\data\time_secretary.db" -Force
Start-ScheduledTask -TaskName 'Time Secretary'
```

## Schema Safety

Startup runs SQLAlchemy `create_all()` and safe additive SQLite migrations. The migration layer only adds missing columns that earlier MVP databases may lack. It does not drop tables, delete rows, rename columns, or rewrite user data.

New missing tables, including `llm_calls`, are created on startup.

## CSV Export

For spreadsheet review:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\export_data.py --output-dir ".\backups\exports"
```

CSV exports include time entries, todos, reminders, project notes, secretary inbox items, projects, and LLM call logs.

New phase exports also include location events, work day summaries, project time, work focus, process changes, observations, run metrics, delegation candidates, staffing signals, secure capture metadata, briefing request metadata, and briefing report metadata.

By default:

```dotenv
EXPORT_INCLUDE_SENSITIVE=false
```

Secure capture text and sensitive briefing full text are redacted in CSV exports. Set `EXPORT_INCLUDE_SENSITIVE=true` only for a deliberate local export, because it includes sensitive process/work-note and briefing text.

## LLM Migration Notes

LLM configuration is environment-only. It is not stored in the backup archive.

The deterministic app remains fully functional with:

```dotenv
LLM_ENABLED=false
LLM_PROVIDER=none
```

If enabling Ollama on the target machine, configure it locally in `.env` and test from `/dashboard/settings`.
