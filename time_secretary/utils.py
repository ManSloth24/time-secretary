from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from .config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_local(settings: Settings) -> datetime:
    return datetime.now(settings.timezone)


def ensure_timezone(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=settings.timezone)
    return value.astimezone(settings.timezone)


def combine_local(date_value, clock: time, settings: Settings) -> datetime:
    return datetime.combine(date_value, clock, tzinfo=settings.timezone)


def floor_to_interval(value: datetime, minutes: int) -> datetime:
    value = value.replace(second=0, microsecond=0)
    floored_minute = (value.minute // minutes) * minutes
    return value.replace(minute=floored_minute)


def interval_for_now(value: datetime, minutes: int) -> tuple[datetime, datetime]:
    end = floor_to_interval(value, minutes)
    return end - timedelta(minutes=minutes), end


def duration_minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def mask_phone_number(value: str | None) -> str:
    if not value:
        return "unknown"
    digits = re.sub(r"\D+", "", value)
    if len(digits) <= 4:
        return "***" + digits
    return f"***-***-{digits[-4:]}"


def parse_duration(value: str) -> timedelta | None:
    match = re.fullmatch(r"\s*(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*", value, re.I)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("h"):
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def human_dt(value: datetime | None, settings: Settings) -> str:
    if value is None:
        return ""
    return ensure_timezone(value, settings).strftime("%Y-%m-%d %H:%M")


def simple_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "report"
