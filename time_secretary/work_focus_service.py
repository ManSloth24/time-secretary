from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import TimeEntry


@dataclass(frozen=True)
class WorkFocusResult:
    work_focus_type: str
    value_level: str
    delegation_candidate: bool = False
    delegation_reason: str | None = None
    staffing_signal: bool = False
    staffing_signal_reason: str | None = None


def classify_work_focus(text: str) -> WorkFocusResult:
    low = text.lower()
    if re.search(r"\b(email|emails|inbox|slack|teams|admin|paperwork)\b", low):
        return WorkFocusResult("admin_reactive", "low")
    if re.search(r"\b(manually|manual|formatted|formatting|cleaned|cleaning|again|repeated|routine)\b", low):
        return WorkFocusResult(
            "routine_execution",
            "low",
            delegation_candidate=True,
            delegation_reason="Repeated or manual execution work may be delegable.",
        )
    if re.search(r"\b(cleaned|processed|analyzed|analysis|data|plot|plots)\b", low):
        return WorkFocusResult("data_analysis", "medium")
    if re.search(r"\b(planned|planning|project task design|designed project task|doe)\b", low):
        return WorkFocusResult("project taskal_planning", "high")
    if re.search(r"\b(decided|strategy|strategic|next project path|roadmap|prioritized)\b", low):
        return WorkFocusResult("strategic_contribution", "high")
    if re.search(r"\b(fixed|debugged|troubleshoot|troubleshooting|issue|failure|repair)\b", low):
        return WorkFocusResult("troubleshooting", "high")
    if re.search(r"\b(sop|procedure|documentation|documented|wrote report|writeup)\b", low):
        return WorkFocusResult("documentation", "medium")
    if re.search(r"\b(improved|improvement|automated|automation|reduced|optimized)\b", low):
        return WorkFocusResult("process_improvement", "high")
    if re.search(r"\b(meeting|sync|coordinated|coordinate|managed|management)\b", low):
        return WorkFocusResult("management_coordination", "medium")
    return WorkFocusResult("technical_contribution", "medium")


def apply_work_focus(session: Session, entry: TimeEntry) -> None:
    if entry.category_primary != "Work":
        entry.work_focus_type = None
        entry.value_level = None
        entry.delegation_candidate = False
        entry.delegation_reason = None
        entry.staffing_signal = False
        entry.staffing_signal_reason = None
        return

    focus = classify_work_focus(entry.raw_text)
    entry.work_focus_type = focus.work_focus_type
    entry.value_level = focus.value_level
    entry.delegation_candidate = focus.delegation_candidate
    entry.delegation_reason = focus.delegation_reason
    entry.staffing_signal = focus.staffing_signal
    entry.staffing_signal_reason = focus.staffing_signal_reason

    if entry.delegation_candidate:
        repeats = session.scalar(
            select(func.count())
            .select_from(TimeEntry)
            .where(
                TimeEntry.category_primary == "Work",
                TimeEntry.work_focus_type == entry.work_focus_type,
                TimeEntry.delegation_candidate.is_(True),
            )
        ) or 0
        if repeats >= 2:
            entry.staffing_signal = True
            entry.staffing_signal_reason = "Repeated delegable work suggests possible staffing support."


def high_value_minutes(entries: list[TimeEntry]) -> int:
    from .utils import duration_minutes

    return sum(
        duration_minutes(entry.interval_start, entry.interval_end)
        for entry in entries
        if entry.value_level in {"high", "critical"}
    )


def low_value_minutes(entries: list[TimeEntry]) -> int:
    from .utils import duration_minutes

    return sum(
        duration_minutes(entry.interval_start, entry.interval_end)
        for entry in entries
        if entry.value_level in {"low"}
    )
