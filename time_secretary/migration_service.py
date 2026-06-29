from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


SQLITE_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "time_entries": {
        "work_focus_type": "VARCHAR(64)",
        "value_level": "VARCHAR(32)",
        "delegation_candidate": "BOOLEAN DEFAULT 0",
        "delegation_reason": "TEXT",
        "staffing_signal": "BOOLEAN DEFAULT 0",
        "staffing_signal_reason": "TEXT",
        "linked_run_id": "INTEGER",
        "linked_change_id": "INTEGER",
        "linked_metric_id": "INTEGER",
        "sensitivity": "VARCHAR(32) DEFAULT 'normal'",
    },
    "todo_items": {
        "next_review_at": "DATETIME",
        "last_reviewed_at": "DATETIME",
        "review_count": "INTEGER DEFAULT 0",
        "snooze_count": "INTEGER DEFAULT 0",
        "needs_followup": "BOOLEAN DEFAULT 1",
        "capture_status": "VARCHAR(32) DEFAULT 'captured'",
    },
    "reminders": {
        "next_review_at": "DATETIME",
        "snooze_count": "INTEGER DEFAULT 0",
        "capture_status": "VARCHAR(32) DEFAULT 'scheduled'",
    },
    "project_notes": {
        "next_review_at": "DATETIME",
        "last_reviewed_at": "DATETIME",
        "review_count": "INTEGER DEFAULT 0",
        "needs_followup": "BOOLEAN DEFAULT 0",
        "capture_status": "VARCHAR(32) DEFAULT 'captured'",
        "linked_todo_id": "INTEGER",
        "linked_reminder_id": "INTEGER",
        "sensitivity": "VARCHAR(32) DEFAULT 'normal'",
    },
    "secretary_inbox_items": {
        "interpreted_type": "VARCHAR(64)",
        "suggested_category": "VARCHAR(32)",
        "suggested_project_id": "INTEGER",
        "suggested_project_name": "VARCHAR(160)",
        "suggested_title": "VARCHAR(240)",
        "suggested_next_action": "TEXT",
        "next_review_at": "DATETIME",
        "created_from_sms_id": "INTEGER",
        "updated_at": "DATETIME",
        "reviewed_at": "DATETIME",
        "converted_to_type": "VARCHAR(64)",
        "converted_to_id": "INTEGER",
        "sensitivity": "VARCHAR(32) DEFAULT 'normal'",
    },
    "briefing_reports": {
        "generation_mode": "VARCHAR(32) DEFAULT 'deterministic'",
        "fact_pack_path": "TEXT",
        "llm_narrative_path": "TEXT",
        "final_markdown_path": "TEXT",
        "llm_cache_key": "VARCHAR(128)",
        "llm_model": "VARCHAR(160)",
        "llm_duration_ms": "INTEGER",
        "llm_validation_warnings_json": "TEXT",
    },
}


def run_startup_migrations(engine: Engine) -> list[str]:
    """Apply safe additive SQLite migrations for MVP schema drift."""
    if engine.dialect.name != "sqlite":
        return []

    actions: list[str] = []
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table_name, columns in SQLITE_COLUMN_MIGRATIONS.items():
            if table_name not in table_names:
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl in columns.items():
                if column_name in existing:
                    continue
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))
                actions.append(f"Added {table_name}.{column_name}")

    return actions
