from __future__ import annotations

from datetime import datetime, timedelta

from time_secretary.command_parser import parse_command
from time_secretary.natural_date_parser import parse_natural_datetime


def test_sms_command_parsing():
    assert parse_command("pause 1h").name == "pause_for"
    assert parse_command("report week").args["type"] == "weekly"
    assert parse_command("todo high finish project beta writeup by Friday").name == "todo_add"
    assert parse_command("done 12").args["query"] == "12"
    assert parse_command("agenda tomorrow").args["day"] == "tomorrow"
    assert parse_command("what did I say about Project Alpha?").name == "recall_project"


def test_natural_date_parser_handles_common_relative_dates(db_session):
    _session, settings = db_session
    now = datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone)

    tomorrow = parse_natural_datetime("tomorrow", now=now, settings=settings)
    assert tomorrow.date().isoformat() == "2026-06-23"
    assert tomorrow.hour == 8

    tonight = parse_natural_datetime("tonight", now=now, settings=settings)
    assert tonight.date().isoformat() == "2026-06-22"
    assert tonight.hour == 19

    friday = parse_natural_datetime("by Friday", now=now, settings=settings)
    assert friday.date().isoformat() == "2026-06-26"
    assert friday.hour == 21

    in_two_hours = parse_natural_datetime("in 2 hours", now=now, settings=settings)
    assert in_two_hours == now + timedelta(hours=2)
