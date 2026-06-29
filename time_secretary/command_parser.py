from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: dict[str, str] = field(default_factory=dict)


def parse_command(text: str) -> ParsedCommand | None:
    raw = text.strip()
    low = raw.lower().strip()
    if not low:
        return None

    if low.startswith("pause until "):
        return ParsedCommand("pause_until", {"when": raw[12:].strip()})
    if low.startswith("pause "):
        return ParsedCommand("pause_for", {"duration": raw[6:].strip()})
    if low == "resume":
        return ParsedCommand("resume")
    if low == "status":
        return ParsedCommand("status")
    if low in {"skip", "missed"}:
        return ParsedCommand("skip")
    if low in {"help", "help secretary"}:
        return ParsedCommand(low.replace(" ", "_"))

    if low.startswith("capture "):
        return ParsedCommand("capture", {"body": raw[8:].strip()})
    if low.startswith("note "):
        return ParsedCommand("capture_note", {"body": raw[5:].strip()})
    if low.startswith("idea "):
        return ParsedCommand("capture_idea", {"body": raw[5:].strip()})
    if low.startswith("circle back "):
        return ParsedCommand("capture_followup", {"body": raw[12:].strip()})
    if low.startswith("follow up "):
        return ParsedCommand("capture_followup", {"body": raw[10:].strip()})
    if low in {"review inbox", "review captured"}:
        return ParsedCommand(low.replace(" ", "_"))
    if low.startswith("dismiss "):
        return ParsedCommand("dismiss", {"id": raw[8:].strip()})
    if low.startswith("make todo "):
        return ParsedCommand("make_todo", {"id": raw[10:].strip()})
    match = re.fullmatch(r"remind\s+me\s+about\s+(\d+)\s+(.+)", raw, flags=re.I)
    if match:
        return ParsedCommand("remind_about", {"id": match.group(1), "when": match.group(2).strip()})
    match = re.fullmatch(r"assign\s+(\d+)\s+to\s+project\s+(.+)", raw, flags=re.I)
    if match:
        return ParsedCommand("assign_capture", {"id": match.group(1), "project": match.group(2).strip()})
    if low == "what did i capture today?":
        return ParsedCommand("capture_today")
    if low == "what do i need to circle back on?":
        return ParsedCommand("circle_back_list")
    if low == "what notes need action?":
        return ParsedCommand("notes_need_action")
    if low == "show unassigned":
        return ParsedCommand("show_unassigned")
    if low == "show stale items":
        return ParsedCommand("show_stale")
    if low == "show snoozed":
        return ParsedCommand("show_snoozed")

    if low in {"arrived work", "arrive work", "arrived at work", "left work", "leave work", "at work", "arrived home", "arrive home", "arrived at home", "left home", "leave home", "at home", "where am i", "where am i?", "location status", "list places"}:
        return ParsedCommand("location", {"body": raw})
    if low.startswith("add place "):
        return ParsedCommand("location", {"body": raw})

    match = re.fullmatch(r"work\s+hours\s+(today|week|month|year|ytd)", low)
    if match:
        return ParsedCommand("work_hours", {"period": match.group(1)})
    if low == "work summary":
        return ParsedCommand("work_hours", {"period": "week"})

    briefing_patterns = [
        r"^brief me on (.+)",
        r"^send me notes on (.+)",
        r"^meeting prep (.+)",
        r"^prep me for (.+)",
        r"^what do i need to know about (.+)",
        r"^open items for (.+)",
        r"^what changed on (.+)",
        r"^risks for (.+)",
        r"^questions for (.+) meeting$",
    ]
    for pattern in briefing_patterns:
        match = re.fullmatch(pattern, raw, flags=re.I)
        if match:
            return ParsedCommand("briefing_request", {"body": raw, "topic": match.group(1).strip()})

    match = re.fullmatch(r"fix\s+(arrived|left)\s+work\s+(.+)", raw, flags=re.I)
    if match:
        return ParsedCommand("fix_work_location", {"event": match.group(1).lower(), "time": match.group(2).strip()})

    match = re.fullmatch(r"report\s+(today|day|week|month|year|ytd)", low)
    if match:
        report_type = {"today": "daily", "day": "daily", "week": "weekly", "month": "monthly", "year": "yearly", "ytd": "yearly"}[match.group(1)]
        return ParsedCommand("report", {"type": report_type})

    if low.startswith("project add "):
        return ParsedCommand("project_add", {"name": raw[12:].strip()})

    if low.startswith("alias ") and "=" in raw:
        left, right = raw[6:].split("=", 1)
        return ParsedCommand(
            "project_alias",
            {"project": left.strip(), "aliases": right.strip()},
        )

    match = re.fullmatch(r"fix\s+last\s+category\s*=\s*(work|home|unknown)", low)
    if match:
        return ParsedCommand("fix_last_category", {"category": match.group(1).title()})

    match = re.fullmatch(r"fix\s+last\s+project\s*=\s*(.+)", raw, flags=re.I)
    if match:
        return ParsedCommand("fix_last_project", {"project": match.group(1).strip()})

    if low.startswith("todo"):
        body = re.sub(r"^todo\s*:?\s*", "", raw, flags=re.I).strip()
        return ParsedCommand("todo_add", {"body": body})

    if low.startswith("done"):
        body = re.sub(r"^done\s*", "", raw, flags=re.I).strip()
        return ParsedCommand("done", {"query": body})

    if low == "cancel":
        return ParsedCommand("cancel", {})

    match = re.fullmatch(r"cancel\s+reminder\s+(\d+)", low)
    if match:
        return ParsedCommand("cancel_reminder", {"id": match.group(1)})

    if low.startswith("snooze reminder "):
        match = re.match(r"snooze\s+reminder\s+(\d+)\s+until\s+(.+)", raw, flags=re.I)
        if match:
            return ParsedCommand(
                "snooze_reminder",
                {"id": match.group(1), "until": match.group(2).strip()},
            )
    if low.startswith("snooze "):
        return ParsedCommand("snooze", {"duration": raw[8:].strip()})

    if low in {"list todos", "todos"}:
        return ParsedCommand("list_todos", {})
    if low == "list work todos":
        return ParsedCommand("list_todos", {"category": "Work"})
    if low == "list home todos":
        return ParsedCommand("list_todos", {"category": "Home"})
    if low.startswith("list project "):
        return ParsedCommand("list_project", {"project": raw[13:].strip()})
    if low.startswith("notes "):
        return ParsedCommand("notes", {"project": raw[6:].strip()})
    if low == "reminders":
        return ParsedCommand("list_reminders", {})
    if low in {"agenda today", "what do i need to do today?"}:
        return ParsedCommand("agenda", {"day": "today"})
    if low == "agenda tomorrow":
        return ParsedCommand("agenda", {"day": "tomorrow"})
    if low.startswith("what did i say about "):
        return ParsedCommand("recall_project", {"project": raw[21:].strip(" ?")})
    if low.startswith("project status "):
        return ParsedCommand("project_status", {"project": raw[15:].strip()})

    return None
