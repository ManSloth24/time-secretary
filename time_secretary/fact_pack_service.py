from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    ProcessChange,
    ProcessObservation,
    Project,
    ProjectNote,
    Reminder,
    ReportRun,
    RunMetric,
    RunRecord,
    SecureCapture,
    SecretaryInboxItem,
    TimeEntry,
    TodoItem,
    WorkDaySummary,
    WorkInsight,
)
from .utils import duration_minutes, ensure_timezone
from .work_hours_service import period_bounds, totals_for_period
from .work_intelligence_service import summarize_work_intelligence


def _clean_text(value: str | None, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _dt(value: datetime | None, settings: Settings) -> str | None:
    if value is None:
        return None
    return ensure_timezone(value, settings).isoformat()


def _date(value: Any) -> str | None:
    return value.isoformat() if value else None


def _sensitive_allowed(settings: Settings, include_sensitive: bool) -> bool:
    return bool(settings.include_sensitive_local_reports and include_sensitive)


def _project_item(project: Project | None) -> dict[str, Any] | None:
    if project is None:
        return None
    return {
        "id": project.id,
        "name": project.name,
        "aliases": project.aliases,
        "category_default": project.category_default,
    }


def _entry_item(entry: TimeEntry, settings: Settings) -> dict[str, Any]:
    minutes = duration_minutes(entry.interval_start, entry.interval_end)
    return {
        "id": entry.id,
        "start": _dt(entry.interval_start, settings),
        "end": _dt(entry.interval_end, settings),
        "minutes": minutes,
        "hours": round(minutes / 60, 2),
        "category_primary": entry.category_primary,
        "category_secondary": entry.category_secondary,
        "project_name": entry.project_name,
        "work_focus_type": entry.work_focus_type,
        "value_level": entry.value_level,
        "delegation_candidate": entry.delegation_candidate,
        "delegation_reason": _clean_text(entry.delegation_reason),
        "staffing_signal": entry.staffing_signal,
        "staffing_signal_reason": _clean_text(entry.staffing_signal_reason),
        "summary": _clean_text(entry.raw_text),
    }


def _todo_item(todo: TodoItem, settings: Settings) -> dict[str, Any]:
    return {
        "id": todo.id,
        "title": _clean_text(todo.title),
        "status": todo.status,
        "priority": todo.priority,
        "project_name": todo.project_name,
        "category_primary": todo.category_primary,
        "due_at": _dt(todo.due_at, settings),
        "remind_at": _dt(todo.remind_at, settings),
        "needs_followup": todo.needs_followup,
        "created_at": _dt(todo.created_at, settings),
    }


def _reminder_item(reminder: Reminder, settings: Settings) -> dict[str, Any]:
    return {
        "id": reminder.id,
        "title": _clean_text(reminder.title),
        "status": reminder.status,
        "remind_at": _dt(reminder.remind_at, settings),
        "related_project_id": reminder.related_project_id,
        "created_at": _dt(reminder.created_at, settings),
    }


def _note_item(note: ProjectNote, settings: Settings, allow_sensitive: bool) -> dict[str, Any]:
    sensitive = note.sensitivity == "sensitive"
    include_text = allow_sensitive or not sensitive
    item = {
        "id": note.id,
        "note_type": note.note_type,
        "title": _clean_text(note.title),
        "project_name": note.project_name,
        "needs_followup": note.needs_followup,
        "capture_status": note.capture_status,
        "sensitivity": note.sensitivity,
        "created_at": _dt(note.created_at, settings),
        "text_withheld": sensitive and not allow_sensitive,
    }
    if include_text:
        item["body"] = _clean_text(note.body)
    return item


def _secure_capture_item(
    capture: SecureCapture,
    settings: Settings,
    allow_sensitive: bool,
) -> dict[str, Any]:
    item = {
        "id": capture.id,
        "capture_type": capture.capture_type,
        "project_name": capture.project_name,
        "run_name": capture.run_name,
        "source": capture.source,
        "sensitivity": capture.sensitivity,
        "processed_status": capture.processed_status,
        "created_at": _dt(capture.created_at, settings),
        "received_at": _dt(capture.received_at, settings),
        "text_withheld": not allow_sensitive,
    }
    if allow_sensitive:
        item["text"] = _clean_text(capture.text)
    return item


def _inbox_item(item: SecretaryInboxItem, settings: Settings) -> dict[str, Any]:
    return {
        "id": item.id,
        "interpreted_type": item.interpreted_type,
        "suggested_category": item.suggested_category,
        "suggested_project_name": item.suggested_project_name,
        "suggested_title": _clean_text(item.suggested_title),
        "suggested_next_action": _clean_text(item.suggested_next_action),
        "status": item.status,
        "created_at": _dt(item.created_at, settings),
    }


def _change_item(change: ProcessChange, settings: Settings) -> dict[str, Any]:
    return {
        "id": change.id,
        "project_id": change.project_id,
        "run_id": change.run_id,
        "change_type": change.change_type,
        "title": _clean_text(change.title),
        "description": _clean_text(change.description),
        "reason": _clean_text(change.reason),
        "expected_effect": _clean_text(change.expected_effect),
        "occurred_at": _dt(change.occurred_at, settings),
    }


def _observation_item(observation: ProcessObservation, settings: Settings) -> dict[str, Any]:
    return {
        "id": observation.id,
        "project_id": observation.project_id,
        "run_id": observation.run_id,
        "title": _clean_text(observation.title),
        "observation_text": _clean_text(observation.observation_text),
        "severity": observation.severity,
        "observed_at": _dt(observation.observed_at, settings),
    }


def _metric_item(metric: RunMetric, settings: Settings) -> dict[str, Any]:
    return {
        "id": metric.id,
        "project_id": metric.project_id,
        "run_id": metric.run_id,
        "metric_name": _clean_text(metric.metric_name, 120),
        "metric_value_text": _clean_text(metric.metric_value_text, 120),
        "metric_value_numeric": metric.metric_value_numeric,
        "metric_unit": metric.metric_unit,
        "source": metric.source,
        "measured_at": _dt(metric.measured_at, settings),
        "created_at": _dt(metric.created_at, settings),
    }


def _run_item(run: RunRecord, settings: Settings) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "project_name": run.project_name,
        "run_name": run.run_name,
        "run_date": _dt(run.run_date, settings),
        "equipment": run.equipment,
        "material": run.material,
        "operator": run.operator,
        "status": run.status,
        "notes": _clean_text(run.notes),
        "created_at": _dt(run.created_at, settings),
    }


def _summary_item(summary: WorkDaySummary, settings: Settings) -> dict[str, Any]:
    return {
        "id": summary.id,
        "date": _date(summary.date),
        "arrived_work_at": _dt(summary.arrived_work_at, settings),
        "left_work_at": _dt(summary.left_work_at, settings),
        "worksite_duration_minutes": summary.worksite_duration_minutes or 0,
        "worksite_hours": round((summary.worksite_duration_minutes or 0) / 60, 2),
        "logged_work_minutes": summary.logged_work_minutes or 0,
        "logged_work_hours": round((summary.logged_work_minutes or 0) / 60, 2),
        "home_minutes": summary.home_minutes or 0,
        "missing_arrival_event": summary.missing_arrival_event,
        "missing_leave_event": summary.missing_leave_event,
        "confidence": summary.confidence,
    }


def _insight_item(insight: WorkInsight, settings: Settings) -> dict[str, Any]:
    return {
        "id": insight.id,
        "period_type": insight.period_type,
        "insight_type": insight.insight_type,
        "project_id": insight.project_id,
        "title": _clean_text(insight.title),
        "body": _clean_text(insight.body),
        "supporting_minutes": insight.supporting_minutes,
        "supporting_entry_count": insight.supporting_entry_count,
        "confidence": insight.confidence,
        "created_at": _dt(insight.created_at, settings),
    }


def _report_run_item(report_run: ReportRun, settings: Settings) -> dict[str, Any]:
    return {
        "id": report_run.id,
        "report_type": report_run.report_type,
        "period_start": _dt(report_run.period_start, settings),
        "period_end": _dt(report_run.period_end, settings),
        "generated_at": _dt(report_run.generated_at, settings),
        "summary_text": _clean_text(report_run.summary_text),
    }


def _time_totals(entries: list[TimeEntry]) -> dict[str, Any]:
    by_primary: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    by_focus: Counter[str] = Counter()
    high_value = 0
    low_value = 0
    for entry in entries:
        minutes = duration_minutes(entry.interval_start, entry.interval_end)
        by_primary[entry.category_primary or "Unknown"] += minutes
        by_project[entry.project_name or "Unassigned"] += minutes
        by_focus[entry.work_focus_type or "unclassified"] += minutes
        if entry.value_level in {"high", "critical"}:
            high_value += minutes
        elif entry.value_level == "low":
            low_value += minutes
    total = sum(by_primary.values())
    return {
        "entry_count": len(entries),
        "total_minutes": total,
        "total_hours": round(total / 60, 2),
        "by_primary_hours": {key: round(value / 60, 2) for key, value in by_primary.items()},
        "by_project_hours": {key: round(value / 60, 2) for key, value in by_project.items()},
        "by_focus_hours": {key: round(value / 60, 2) for key, value in by_focus.items()},
        "high_value_hours": round(high_value / 60, 2),
        "low_value_hours": round(low_value / 60, 2),
        "delegation_signal_count": sum(1 for entry in entries if entry.delegation_candidate),
        "staffing_signal_count": sum(1 for entry in entries if entry.staffing_signal),
    }


def _worksite_totals(summaries: list[WorkDaySummary]) -> dict[str, Any]:
    worksite_minutes = sum(summary.worksite_duration_minutes or 0 for summary in summaries)
    logged_minutes = sum(summary.logged_work_minutes or 0 for summary in summaries)
    missing_events = sum(
        1 for summary in summaries if summary.missing_arrival_event or summary.missing_leave_event
    )
    return {
        "summary_count": len(summaries),
        "worksite_minutes": worksite_minutes,
        "worksite_hours": round(worksite_minutes / 60, 2),
        "logged_work_minutes": logged_minutes,
        "logged_work_hours": round(logged_minutes / 60, 2),
        "missing_event_count": missing_events,
    }


def build_briefing_fact_pack_from_records(
    *,
    request_text: str,
    briefing_type: str,
    topic: str,
    project: Project | None,
    run_name: str | None,
    start: datetime,
    end: datetime,
    include_sensitive: bool,
    settings: Settings,
    notes: list[ProjectNote],
    secure_captures: list[SecureCapture],
    todos: list[TodoItem],
    reminders: list[Reminder],
    inbox_items: list[SecretaryInboxItem],
    entries: list[TimeEntry],
    work_summaries: list[WorkDaySummary],
    insights: list[WorkInsight],
    runs: list[RunRecord],
    changes: list[ProcessChange],
    observations: list[ProcessObservation],
    metrics: list[RunMetric],
    report_runs: list[ReportRun],
) -> dict[str, Any]:
    allow_sensitive = _sensitive_allowed(settings, include_sensitive)
    sensitive_notes = [note for note in notes if note.sensitivity == "sensitive"]
    sensitive_captures = list(secure_captures)
    overdue_todos = [
        todo for todo in todos if todo.due_at and ensure_timezone(todo.due_at, settings) < end
    ]
    decisions = [note for note in notes if note.note_type == "decision"]
    risks = [
        note for note in notes if note.note_type in {"risk", "question", "follow_up"}
    ]
    delegation_entries = [entry for entry in entries if entry.delegation_candidate]
    staffing_entries = [entry for entry in entries if entry.staffing_signal]
    fact_pack = {
        "schema_version": "fact-pack-v1",
        "report_kind": "briefing",
        "generated_at": _dt(end, settings),
        "scope": {
            "briefing_type": briefing_type,
            "topic": topic,
            "project": _project_item(project),
            "run_name": run_name,
            "window_start": _dt(start, settings),
            "window_end": _dt(end, settings),
            "source_request": _clean_text(request_text, 500),
        },
        "sensitive_policy": {
            "requested_include_sensitive": include_sensitive,
            "local_sensitive_reports_enabled": settings.include_sensitive_local_reports,
            "contains_sensitive_sources": bool(sensitive_notes or sensitive_captures),
            "contains_sensitive_text": bool(allow_sensitive and (sensitive_notes or sensitive_captures)),
            "sensitive_text_withheld": bool((sensitive_notes or sensitive_captures) and not allow_sensitive),
        },
        "record_counts": {
            "notes": len(notes),
            "secure_captures": len(secure_captures),
            "todos": len(todos),
            "overdue_todos": len(overdue_todos),
            "reminders": len(reminders),
            "inbox_items": len(inbox_items),
            "time_entries": len(entries),
            "work_summaries": len(work_summaries),
            "insights": len(insights),
            "runs": len(runs),
            "changes": len(changes),
            "observations": len(observations),
            "metrics": len(metrics),
        },
        "time_totals": _time_totals(entries),
        "worksite_totals": _worksite_totals(work_summaries),
        "todos": [_todo_item(todo, settings) for todo in todos[:40]],
        "overdue_todos": [_todo_item(todo, settings) for todo in overdue_todos[:20]],
        "reminders": [_reminder_item(reminder, settings) for reminder in reminders[:30]],
        "recent_time_entries": [_entry_item(entry, settings) for entry in entries[:40]],
        "recent_notes": [_note_item(note, settings, allow_sensitive) for note in notes[:40]],
        "secure_captures": [
            _secure_capture_item(capture, settings, allow_sensitive)
            for capture in secure_captures[:30]
        ],
        "decisions": [_note_item(note, settings, allow_sensitive) for note in decisions[:20]],
        "risks_and_questions": [_note_item(note, settings, allow_sensitive) for note in risks[:20]],
        "inbox_items": [_inbox_item(item, settings) for item in inbox_items[:20]],
        "runs": [_run_item(run, settings) for run in runs[:20]],
        "process_changes": [_change_item(change, settings) for change in changes[:30]],
        "process_observations": [
            _observation_item(observation, settings) for observation in observations[:30]
        ],
        "run_metrics": [_metric_item(metric, settings) for metric in metrics[:30]],
        "work_summaries": [_summary_item(summary, settings) for summary in work_summaries[:30]],
        "work_insights": [_insight_item(insight, settings) for insight in insights[:30]],
        "supporting_reports": [_report_run_item(report, settings) for report in report_runs[:20]],
        "delegation_staffing": {
            "delegation_candidates": [_entry_item(entry, settings) for entry in delegation_entries[:20]],
            "staffing_signals": [_entry_item(entry, settings) for entry in staffing_entries[:20]],
            "insights": [
                _insight_item(insight, settings)
                for insight in insights
                if insight.insight_type in {"delegation", "staffing"}
            ][:20],
        },
        "missing_data_warnings": _missing_data_warnings(entries, notes, todos, changes, observations, metrics),
    }
    return fact_pack


def _missing_data_warnings(
    entries: list[TimeEntry],
    notes: list[ProjectNote],
    todos: list[TodoItem],
    changes: list[ProcessChange],
    observations: list[ProcessObservation],
    metrics: list[RunMetric],
) -> list[str]:
    warnings: list[str] = []
    if not entries:
        warnings.append("No matching time entries were found for this window.")
    if not notes:
        warnings.append("No matching project notes were found for this window.")
    if not todos:
        warnings.append("No open matching todos were found.")
    if changes and not metrics:
        warnings.append("Process changes exist, but no matching metrics were found.")
    if observations and not changes:
        warnings.append("Observations exist without matching process change records.")
    return warnings


def _text_filter(model, topic: str, fields: list[str]):
    clauses = []
    pattern = f"%{topic}%"
    for field in fields:
        clauses.append(getattr(model, field).ilike(pattern))
    return or_(*clauses) if clauses else None


def _project_or_topic_filter(
    model,
    topic: str,
    project: Project | None,
    text_fields: list[str],
):
    clauses = []
    if project is not None:
        if hasattr(model, "project_name"):
            clauses.append(getattr(model, "project_name") == project.name)
        if hasattr(model, "related_project_id"):
            clauses.append(getattr(model, "related_project_id") == project.id)
        if hasattr(model, "project_id"):
            clauses.append(getattr(model, "project_id") == project.id)
    if topic:
        text_clause = _text_filter(model, topic, text_fields)
        if text_clause is not None:
            clauses.append(text_clause)
    return or_(*clauses) if clauses else None


def _query_recent(
    session: Session,
    model,
    date_field: str,
    start: datetime,
    end: datetime,
    filters,
    limit: int = 40,
):
    query = select(model).where(getattr(model, date_field) >= start, getattr(model, date_field) <= end)
    if filters is not None:
        query = query.where(filters)
    return list(session.scalars(query.order_by(getattr(model, date_field).desc()).limit(limit)))


def build_project_briefing_fact_pack(
    session: Session,
    *,
    request_text: str,
    topic: str,
    project: Project | None = None,
    run_name: str | None = None,
    start: datetime,
    end: datetime,
    include_sensitive: bool = False,
    settings: Settings,
    briefing_type: str = "project",
) -> dict[str, Any]:
    filt = lambda model, fields: _project_or_topic_filter(model, topic, project, fields)
    notes = _query_recent(session, ProjectNote, "created_at", start, end, filt(ProjectNote, ["title", "body", "raw_text"]), 40)
    if not include_sensitive:
        notes = [note for note in notes if note.sensitivity != "sensitive"]
    secure_captures = []
    if include_sensitive:
        secure_captures = _query_recent(session, SecureCapture, "created_at", start, end, filt(SecureCapture, ["text", "capture_type", "run_name"]), 30)
    todos = list(
        session.scalars(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]))
            .where(filt(TodoItem, ["title", "description"]) if filt(TodoItem, ["title", "description"]) is not None else True)
            .order_by(TodoItem.created_at.desc())
            .limit(40)
        )
    )
    reminders = list(
        session.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed", "sent"]))
            .where(filt(Reminder, ["title", "body"]) if filt(Reminder, ["title", "body"]) is not None else True)
            .order_by(Reminder.remind_at.asc())
            .limit(30)
        )
    )
    entries = _query_recent(session, TimeEntry, "interval_start", start, end, filt(TimeEntry, ["raw_text", "project_name"]), 40)
    work_summaries = list(session.scalars(select(WorkDaySummary).where(WorkDaySummary.date >= start.date(), WorkDaySummary.date <= end.date()).order_by(WorkDaySummary.date.desc()).limit(30)))
    return build_briefing_fact_pack_from_records(
        request_text=request_text,
        briefing_type=briefing_type,
        topic=topic,
        project=project,
        run_name=run_name,
        start=start,
        end=end,
        include_sensitive=include_sensitive,
        settings=settings,
        notes=notes,
        secure_captures=secure_captures,
        todos=todos,
        reminders=reminders,
        inbox_items=_query_recent(session, SecretaryInboxItem, "created_at", start, end, filt(SecretaryInboxItem, ["raw_text", "suggested_title", "suggested_next_action"]), 20),
        entries=entries,
        work_summaries=work_summaries,
        insights=_query_recent(session, WorkInsight, "created_at", start, end, filt(WorkInsight, ["title", "body"]), 20),
        runs=list(session.scalars(select(RunRecord).where(filt(RunRecord, ["run_name", "notes", "equipment", "material"]) if filt(RunRecord, ["run_name", "notes", "equipment", "material"]) is not None else True).order_by(RunRecord.created_at.desc()).limit(20))),
        changes=_query_recent(session, ProcessChange, "occurred_at", start, end, filt(ProcessChange, ["title", "description", "reason", "expected_effect"]), 30),
        observations=_query_recent(session, ProcessObservation, "observed_at", start, end, filt(ProcessObservation, ["title", "observation_text"]), 30),
        metrics=_query_recent(session, RunMetric, "created_at", start, end, filt(RunMetric, ["metric_name", "metric_value_text", "source"]), 30),
        report_runs=_query_recent(session, ReportRun, "generated_at", start, end, _text_filter(ReportRun, topic, ["summary_text", "report_type"]) if topic else None, 10),
    )


def build_run_briefing_fact_pack(
    session: Session,
    *,
    request_text: str,
    topic: str,
    run_name: str,
    start: datetime,
    end: datetime,
    include_sensitive: bool = False,
    settings: Settings,
) -> dict[str, Any]:
    return build_project_briefing_fact_pack(
        session,
        request_text=request_text,
        topic=topic or run_name,
        run_name=run_name,
        start=start,
        end=end,
        include_sensitive=include_sensitive,
        settings=settings,
        briefing_type="run",
    )


def build_work_hours_fact_pack(
    session: Session,
    period: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    totals = totals_for_period(session, period, settings=settings, now=now)
    return {
        "schema_version": "fact-pack-v1",
        "report_kind": "work_hours",
        "generated_at": _dt(now, settings),
        "scope": {
            "period": period,
            "window_start": _dt(totals.start, settings),
            "window_end": _dt(totals.end, settings),
        },
        "worksite_totals": {
            "logged_work_minutes": totals.logged_work_minutes,
            "logged_work_hours": round(totals.logged_work_hours, 2),
            "worksite_duration_minutes": totals.worksite_duration_minutes,
            "worksite_hours": round(totals.worksite_hours, 2),
            "missing_event_count": totals.missing_event_count,
        },
        "work_summaries": [_summary_item(summary, settings) for summary in totals.summaries],
        "sensitive_policy": {
            "requested_include_sensitive": False,
            "contains_sensitive_sources": False,
            "contains_sensitive_text": False,
            "sensitive_text_withheld": False,
        },
    }


def build_work_intelligence_fact_pack(
    session: Session,
    *,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    summary = summarize_work_intelligence(session, settings=settings, start=start, end=end)
    return {
        "schema_version": "fact-pack-v1",
        "report_kind": "work_intelligence",
        "generated_at": _dt(end, settings),
        "scope": {
            "window_start": _dt(start, settings),
            "window_end": _dt(end, settings),
        },
        "time_totals": {
            "by_project_hours": {key: round(value / 60, 2) for key, value in summary.project_minutes.items()},
            "by_focus_hours": {key: round(value / 60, 2) for key, value in summary.focus_minutes.items()},
            "high_value_hours": round(summary.high_value_minutes / 60, 2),
            "low_value_hours": round(summary.low_value_minutes / 60, 2),
        },
        "delegation_staffing": {
            "delegation_candidates": [_entry_item(entry, settings) for entry in summary.delegation_candidates[:30]],
            "staffing_signals": [_entry_item(entry, settings) for entry in summary.staffing_signals[:30]],
        },
        "process_changes": [_change_item(change, settings) for change in summary.process_changes],
        "process_observations": [_observation_item(observation, settings) for observation in summary.observations],
        "run_metrics": [_metric_item(metric, settings) for metric in summary.metrics],
        "sensitive_policy": {
            "requested_include_sensitive": False,
            "contains_sensitive_sources": False,
            "contains_sensitive_text": False,
            "sensitive_text_withheld": False,
        },
    }


def build_periodic_report_fact_pack(
    session: Session,
    report_type: str,
    *,
    settings: Settings,
    now: datetime | None = None,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    start, end = period_bounds(report_type, now, settings)
    project_pack = build_project_briefing_fact_pack(
        session,
        request_text=f"{report_type} report",
        topic="",
        start=start,
        end=end,
        include_sensitive=include_sensitive,
        settings=settings,
        briefing_type=report_type,
    )
    project_pack["report_kind"] = report_type
    project_pack["scope"]["period"] = report_type
    return project_pack


def build_daily_report_fact_pack(session: Session, *, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    return build_periodic_report_fact_pack(session, "daily", settings=settings, now=now)


def build_weekly_report_fact_pack(session: Session, *, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    return build_periodic_report_fact_pack(session, "weekly", settings=settings, now=now)


def build_monthly_report_fact_pack(session: Session, *, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    return build_periodic_report_fact_pack(session, "monthly", settings=settings, now=now)


def build_yearly_report_fact_pack(session: Session, *, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    return build_periodic_report_fact_pack(session, "yearly", settings=settings, now=now)
