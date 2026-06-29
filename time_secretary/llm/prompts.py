from __future__ import annotations

from .schemas import SmsParseContext


ALLOWED_INTENTS = [
    "time_entry",
    "todo",
    "reminder",
    "project_note",
    "project_update",
    "decision",
    "idea",
    "follow_up",
    "question_to_revisit",
    "meeting_action_item",
    "unknown",
]


def build_sms_parse_prompt(context: SmsParseContext, max_chars: int) -> str:
    project_lines = []
    for project in context.projects[:30]:
        aliases = ", ".join(project.aliases[:8])
        project_lines.append(f"- {project.name}: {aliases}")
    projects = "\n".join(project_lines) or "- None"
    reminders = "\n".join(f"- {item}" for item in context.recent_reminders[:5]) or "- None"
    recent_prompt = context.recent_prompt or "None"
    raw_text = context.raw_text[:max_chars]

    return f"""You parse one SMS for a local time-tracking secretary app.
Return JSON only. Do not use markdown. Do not add commentary.

Current local time: {context.now.isoformat()}
Raw SMS: {raw_text}

Known projects and aliases:
{projects}

Recent prompt context: {recent_prompt}
Recent active reminders:
{reminders}

Allowed intent types: {", ".join(ALLOWED_INTENTS)}
Allowed primary categories: Work, Home, Unknown
Allowed priorities: low, normal, high, urgent

Rules:
- Split mixed messages into multiple intents.
- Use null when uncertain.
- Do not invent project names. Use a known project name or null.
- Keep titles short.
- Preserve the raw meaning in body.
- Confidence must be 0.0 to 1.0.
- Do not create exact datetimes. Use due_at_text/remind_at_text/next_review_at_text for relative text.
- If vague, return unknown with low confidence.
- If actionable, classify as todo, follow_up, project_note, reminder, or meeting_action_item.
- If it says worked on, drove, meeting, did, answered, picked up, etc., it may be a time_entry.

Examples:
{{"intents":[{{"type":"time_entry","title":"Worked on Project Alpha report","body":"worked on Project Alpha report","category_primary":"Work","category_secondary":"active_project_work","project_name":"Project Alpha","due_at_text":null,"remind_at_text":null,"next_review_at_text":null,"priority":"normal","confidence":0.86}}],"overall_confidence":0.86,"notes":null}}
{{"intents":[{{"type":"follow_up","title":"Follow-up options","body":"Circle back on follow-up options next week","category_primary":"Work","category_secondary":null,"project_name":"Project Delta","due_at_text":null,"remind_at_text":null,"next_review_at_text":"next week","priority":"normal","confidence":0.82}}],"overall_confidence":0.82,"notes":null}}
"""
