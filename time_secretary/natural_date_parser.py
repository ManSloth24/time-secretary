from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import Settings
from .utils import combine_local, ensure_timezone, parse_duration

try:
    import dateparser
except ImportError:  # pragma: no cover - dependency is in requirements
    dateparser = None


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _default_clock(text: str, settings: Settings):
    low = text.lower()
    if "afternoon" in low:
        return settings.default_afternoon_clock
    if "evening" in low or "tonight" in low:
        return settings.default_evening_clock
    if "eod" in low or "end of day" in low:
        return settings.daily_report_clock
    return settings.default_morning_clock


def _next_weekday(now: datetime, weekday: int, clock, include_today: bool = True) -> datetime:
    days = (weekday - now.weekday()) % 7
    candidate = datetime.combine(now.date() + timedelta(days=days), clock, tzinfo=now.tzinfo)
    if days == 0 and (not include_today or candidate <= now):
        candidate = candidate + timedelta(days=7)
    return candidate


def parse_natural_datetime(
    text: str,
    *,
    now: datetime | None = None,
    settings: Settings,
) -> datetime | None:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    low = text.lower()

    duration_match = re.search(
        r"\bin\s+(\d+\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours))\b",
        low,
    )
    if duration_match:
        duration = parse_duration(duration_match.group(1))
        if duration:
            return now + duration

    if "tomorrow" in low:
        return combine_local(now.date() + timedelta(days=1), _default_clock(low, settings), settings)

    if "tonight" in low:
        candidate = combine_local(now.date(), settings.default_evening_clock, settings)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        return candidate

    if "eod" in low or "end of day" in low:
        candidate = combine_local(now.date(), settings.daily_report_clock, settings)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        return candidate

    if "next week" in low:
        return combine_local(now.date() + timedelta(days=7), settings.default_morning_clock, settings)

    for name, weekday in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            clock = settings.daily_report_clock if re.search(r"\b(by|deadline)\b", low) else _default_clock(low, settings)
            return _next_weekday(now, weekday, clock, include_today=True)

    until_match = re.search(r"\buntil\s+(.+)$", low)
    if until_match:
        return parse_natural_datetime(until_match.group(1), now=now, settings=settings)

    if dateparser is not None:
        parsed = dateparser.parse(
            text,
            settings={
                "RELATIVE_BASE": now,
                "TIMEZONE": settings.app_timezone,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
            },
        )
        if parsed:
            return ensure_timezone(parsed, settings)

    return None


DATE_PHRASE_RE = re.compile(
    r"\b(?:by\s+)?(?:tomorrow(?:\s+(?:morning|afternoon|evening|night))?|tonight|eod|end of day|next week|"
    r"in\s+\d+\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    flags=re.I,
)


def strip_date_phrases(text: str) -> str:
    cleaned = DATE_PHRASE_RE.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" :-,")
