from __future__ import annotations

from datetime import datetime, timedelta

from time_secretary.classification_service import seed_default_data
from time_secretary.config import get_settings
from time_secretary.database import SessionLocal, init_db
from time_secretary.project_memory_service import create_project_note
from time_secretary.reminder_service import create_reminder_from_text
from time_secretary.secretary_service import create_time_entry_from_text
from time_secretary.todo_service import create_todo_from_text


DEMO_ENTRIES = [
    ("drove to work", 7, 45),
    ("answered work emails", 8, 0),
    ("worked on Project Alpha report", 8, 15),
    ("meeting about Project Alpha", 9, 0),
    ("Project Gamma for Project Gamma", 10, 30),
    ("lunch", 12, 0),
    ("worked on project beta writeup", 13, 15),
    ("left work", 16, 45),
    ("picked up kids", 17, 15),
    ("dinner", 18, 30),
    ("worked on Personal Project UI", 20, 0),
]


def main() -> None:
    settings = get_settings()
    init_db()
    with SessionLocal() as session:
        seed_default_data(session)
        today = datetime.now(settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        for text, hour, minute in DEMO_ENTRIES:
            create_time_entry_from_text(
                session,
                text,
                settings=settings,
                now=today.replace(hour=hour, minute=minute) + timedelta(minutes=15),
                source="import",
            )
        create_todo_from_text(
            session,
            "todo high finish project beta writeup by Friday",
            settings=settings,
            now=today.replace(hour=11),
        )
        create_reminder_from_text(
            session,
            "remind me tomorrow to check the project update",
            settings=settings,
            now=today.replace(hour=15),
        )
        create_project_note(
            session,
            "note for Project Alpha project: follow-up question needs references",
            settings=settings,
            note_type="risk",
        )
        session.commit()
        print("Seeded one realistic demo day.")


if __name__ == "__main__":
    main()
