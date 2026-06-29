from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import LocationEvent, TimeEntry, WorkDaySummary
from .utils import duration_minutes, ensure_timezone, human_dt, utcnow


@dataclass(frozen=True)
class WorkPeriodTotals:
    start: datetime
    end: datetime
    summaries: list[WorkDaySummary]
    logged_work_minutes: int
    worksite_duration_minutes: int
    missing_event_count: int

    @property
    def logged_work_hours(self) -> float:
        return self.logged_work_minutes / 60

    @property
    def worksite_hours(self) -> float:
        return self.worksite_duration_minutes / 60


def day_bounds(day: date, settings: Settings) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=settings.timezone)
    return start, start + timedelta(days=1)


def _events_for_day(session: Session, day: date, settings: Settings) -> list[LocationEvent]:
    start, end = day_bounds(day, settings)
    return list(
        session.scalars(
            select(LocationEvent)
            .where(LocationEvent.occurred_at >= start, LocationEvent.occurred_at < end)
            .order_by(LocationEvent.occurred_at.asc())
        )
    )


def _entries_for_day(session: Session, day: date, settings: Settings) -> list[TimeEntry]:
    start, end = day_bounds(day, settings)
    return list(
        session.scalars(
            select(TimeEntry)
            .where(TimeEntry.interval_start >= start, TimeEntry.interval_start < end)
            .order_by(TimeEntry.interval_start.asc())
        )
    )


def _first_event(events: list[LocationEvent], *, place: str, event_type: str) -> LocationEvent | None:
    for event in events:
        if (event.place_name or "").lower() == place.lower() and event.event_type == event_type:
            return event
    return None


def _last_event(events: list[LocationEvent], *, place: str, event_type: str) -> LocationEvent | None:
    for event in reversed(events):
        if (event.place_name or "").lower() == place.lower() and event.event_type == event_type:
            return event
    return None


def _minutes_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end <= start:
        return None
    return duration_minutes(start, end)


def generate_work_day_summary(
    session: Session,
    day: date,
    *,
    settings: Settings,
) -> WorkDaySummary:
    events = _events_for_day(session, day, settings)
    entries = _entries_for_day(session, day, settings)
    for event in events:
        event.occurred_at = ensure_timezone(event.occurred_at, settings)
    for entry in entries:
        entry.interval_start = ensure_timezone(entry.interval_start, settings)
        entry.interval_end = ensure_timezone(entry.interval_end, settings)
    work_entries = [entry for entry in entries if entry.category_primary == "Work"]

    arrived_event = _first_event(events, place="Work", event_type="arrived")
    left_event = _last_event(events, place="Work", event_type="left")
    left_home = _last_event([event for event in events if event.occurred_at <= (arrived_event.occurred_at if arrived_event else datetime.max.replace(tzinfo=settings.timezone))], place="Home", event_type="left")
    arrived_home = _first_event([event for event in events if left_event and event.occurred_at >= left_event.occurred_at], place="Home", event_type="arrived")

    first_work_entry = work_entries[0] if work_entries else None
    last_work_entry = work_entries[-1] if work_entries else None

    arrived_at = arrived_event.occurred_at if arrived_event else (first_work_entry.interval_start if first_work_entry else None)
    left_at = left_event.occurred_at if left_event else (last_work_entry.interval_end if last_work_entry else None)
    missing_arrival = arrived_event is None and first_work_entry is not None
    missing_leave = left_event is None and last_work_entry is not None
    worksite_minutes = _minutes_between(arrived_at, left_at)

    lunch_categories = {"lunch", "lunch_at_work", "meal"}
    logged_work_minutes = sum(
        duration_minutes(entry.interval_start, entry.interval_end)
        for entry in work_entries
        if entry.category_secondary not in lunch_categories
    )
    home_minutes = sum(duration_minutes(entry.interval_start, entry.interval_end) for entry in entries if entry.category_primary == "Home")
    unknown_minutes = sum(duration_minutes(entry.interval_start, entry.interval_end) for entry in entries if entry.category_primary == "Unknown")
    lunch_break_minutes = sum(
        duration_minutes(entry.interval_start, entry.interval_end)
        for entry in entries
        if entry.category_secondary in lunch_categories
    )

    commute_to = _minutes_between(left_home.occurred_at if left_home and arrived_event else None, arrived_event.occurred_at if arrived_event else None)
    commute_from = _minutes_between(left_event.occurred_at if left_event else None, arrived_home.occurred_at if arrived_home else None)

    confidence = "low"
    if arrived_event and left_event:
        confidence = "high"
    elif work_entries and (arrived_event or left_event):
        confidence = "medium"

    summary = session.scalar(select(WorkDaySummary).where(WorkDaySummary.date == day))
    if summary is None:
        summary = WorkDaySummary(date=day)
        session.add(summary)

    summary.first_work_location_event_id = arrived_event.id if arrived_event else left_event.id if left_event else None
    summary.arrived_work_at = arrived_at
    summary.left_work_at = left_at
    summary.first_work_entry_at = first_work_entry.interval_start if first_work_entry else None
    summary.last_work_entry_at = last_work_entry.interval_end if last_work_entry else None
    summary.worksite_duration_minutes = worksite_minutes
    summary.logged_work_minutes = logged_work_minutes
    summary.home_minutes = home_minutes
    summary.unknown_minutes = unknown_minutes
    summary.lunch_break_minutes = lunch_break_minutes
    summary.commute_to_work_minutes = commute_to
    summary.commute_from_work_minutes = commute_from
    summary.missing_arrival_event = missing_arrival
    summary.missing_leave_event = missing_leave
    summary.confidence = confidence
    summary.notes = None
    summary.generated_at = utcnow()
    summary.updated_at = utcnow()
    session.flush()
    return summary


def generate_work_summaries(
    session: Session,
    start_day: date,
    end_day: date,
    *,
    settings: Settings,
) -> list[WorkDaySummary]:
    summaries: list[WorkDaySummary] = []
    current = start_day
    while current <= end_day:
        summaries.append(generate_work_day_summary(session, current, settings=settings))
        current += timedelta(days=1)
    return summaries


def period_bounds(period: str, now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    now = ensure_timezone(now, settings)
    start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period in {"today", "daily"}:
        return start_day, start_day + timedelta(days=1)
    if period in {"week", "weekly"}:
        start = start_day - timedelta(days=start_day.weekday())
        return start, start + timedelta(days=7)
    if period in {"month", "monthly"}:
        start = start_day.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start, end
    if period in {"year", "ytd", "yearly"}:
        start = start_day.replace(month=1, day=1)
        return start, start_day + timedelta(days=1)
    raise ValueError(f"Unknown work-hours period: {period}")


def totals_for_period(
    session: Session,
    period: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> WorkPeriodTotals:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    start, end = period_bounds(period, now, settings)
    generate_work_summaries(session, start.date(), (end - timedelta(days=1)).date(), settings=settings)
    summaries = list(
        session.scalars(
            select(WorkDaySummary)
            .where(WorkDaySummary.date >= start.date(), WorkDaySummary.date < end.date())
            .order_by(WorkDaySummary.date.asc())
        )
    )
    return WorkPeriodTotals(
        start=start,
        end=end,
        summaries=summaries,
        logged_work_minutes=sum(summary.logged_work_minutes or 0 for summary in summaries),
        worksite_duration_minutes=sum(summary.worksite_duration_minutes or 0 for summary in summaries),
        missing_event_count=sum(
            1 for summary in summaries if summary.missing_arrival_event or summary.missing_leave_event
        ),
    )


def work_hours_summary_text(
    session: Session,
    period: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    totals = totals_for_period(session, period, settings=settings, now=now)
    label = {"today": "today", "week": "this week", "month": "this month", "year": "year to date", "ytd": "year to date"}.get(period, period)
    return (
        f"Work hours {label}: logged {totals.logged_work_hours:.1f}h, "
        f"worksite {totals.worksite_hours:.1f}h, "
        f"missing events {totals.missing_event_count}."
    )


def average_event_time(summaries: list[WorkDaySummary], attr: str) -> str:
    values = [getattr(summary, attr) for summary in summaries if getattr(summary, attr)]
    if not values:
        return ""
    total_minutes = sum(value.hour * 60 + value.minute for value in values)
    avg = total_minutes // len(values)
    return f"{avg // 60:02d}:{avg % 60:02d}"


def month_project_totals(session: Session, start: datetime, end: datetime) -> dict[str, int]:
    entries = session.scalars(
        select(TimeEntry)
        .where(TimeEntry.interval_start >= start, TimeEntry.interval_start < end, TimeEntry.category_primary == "Work")
    ).all()
    totals: dict[str, int] = defaultdict(int)
    for entry in entries:
        totals[entry.project_name or "Unassigned Work"] += duration_minutes(entry.interval_start, entry.interval_end)
    return dict(totals)


def export_work_day_summaries(session: Session, path: Path) -> Path:
    summaries = session.scalars(select(WorkDaySummary).order_by(WorkDaySummary.date.asc())).all()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "date",
                "arrived_work_at",
                "left_work_at",
                "worksite_duration_minutes",
                "logged_work_minutes",
                "lunch_break_minutes",
                "missing_arrival_event",
                "missing_leave_event",
                "confidence",
            ]
        )
        for summary in summaries:
            writer.writerow(
                [
                    summary.date.isoformat(),
                    human_dt(summary.arrived_work_at, Settings()) if summary.arrived_work_at else "",
                    human_dt(summary.left_work_at, Settings()) if summary.left_work_at else "",
                    summary.worksite_duration_minutes or "",
                    summary.logged_work_minutes,
                    summary.lunch_break_minutes,
                    summary.missing_arrival_event,
                    summary.missing_leave_event,
                    summary.confidence,
                ]
            )
    return path
