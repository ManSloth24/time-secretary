from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import ProcessChange, ProcessObservation, Project, RunMetric, RunRecord, TimeEntry
from .project_memory_service import find_project
from .utils import duration_minutes, ensure_timezone, utcnow


@dataclass(frozen=True)
class WorkIntelligenceSummary:
    project_minutes: dict[str, int]
    focus_minutes: dict[str, int]
    high_value_minutes: int
    low_value_minutes: int
    delegation_candidates: list[TimeEntry]
    staffing_signals: list[TimeEntry]
    process_changes: list[ProcessChange]
    observations: list[ProcessObservation]
    metrics: list[RunMetric]


def _project_from_text_or_name(session: Session, text: str, project_name: str | None = None) -> Project | None:
    if project_name:
        project = find_project(session, project_name)
        if project:
            return project
    return find_project(session, text)


def infer_change_type(text: str) -> str:
    low = text.lower()
    if "configuration" in low:
        return "configuration_change"
    if "program" in low or "setting" in low:
        return "program_change"
    if "parameter" in low or "flow" in low or "temperature" in low or "pressure" in low:
        return "parameter_change"
    if "material" in low:
        return "material_change"
    if "equipment" in low:
        return "equipment_change"
    if "procedure" in low or "sop" in low:
        return "procedure_change"
    return "other"


def extract_run_name(text: str, fallback: str | None = None) -> str | None:
    match = re.search(r"\brun\s+([A-Za-z0-9_.-]+)", text, flags=re.I)
    if match:
        return match.group(1)
    return fallback


def get_or_create_run(
    session: Session,
    *,
    run_name: str | None,
    project: Project | None = None,
    settings: Settings,
    now: datetime | None = None,
) -> RunRecord | None:
    if not run_name:
        return None
    run = session.scalar(select(RunRecord).where(RunRecord.run_name == run_name))
    if run is None:
        timestamp = ensure_timezone(now or datetime.now(settings.timezone), settings)
        run = RunRecord(
            run_name=run_name,
            project_id=project.id if project else None,
            project_name=project.name if project else None,
            run_date=timestamp,
            status="planned",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(run)
        session.flush()
    elif project and not run.project_id:
        run.project_id = project.id
        run.project_name = project.name
    return run


def create_process_change(
    session: Session,
    text: str,
    *,
    settings: Settings,
    project_name: str | None = None,
    run_name: str | None = None,
    source_sms_id: int | None = None,
    secure_capture_id: int | None = None,
    now: datetime | None = None,
) -> ProcessChange:
    timestamp = ensure_timezone(now or datetime.now(settings.timezone), settings)
    project = _project_from_text_or_name(session, text, project_name)
    run = get_or_create_run(
        session,
        run_name=extract_run_name(text, run_name),
        project=project,
        settings=settings,
        now=timestamp,
    )
    title = re.sub(r"^(run\s+\S+:|local change|program change|change)\s*:?\s*", "", text.strip(), flags=re.I).strip()
    title = title[:120] or text.strip()[:120]
    change = ProcessChange(
        project_id=project.id if project else None,
        run_id=run.id if run else None,
        change_type=infer_change_type(text),
        title=title,
        description=text.strip(),
        created_from_sms_id=source_sms_id,
        created_from_secure_capture_id=secure_capture_id,
        occurred_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(change)
    session.flush()
    return change


def create_process_observation(
    session: Session,
    text: str,
    *,
    settings: Settings,
    project_name: str | None = None,
    run_name: str | None = None,
    source_sms_id: int | None = None,
    secure_capture_id: int | None = None,
    now: datetime | None = None,
) -> ProcessObservation:
    timestamp = ensure_timezone(now or datetime.now(settings.timezone), settings)
    project = _project_from_text_or_name(session, text, project_name)
    run = get_or_create_run(
        session,
        run_name=extract_run_name(text, run_name),
        project=project,
        settings=settings,
        now=timestamp,
    )
    body = re.sub(r"^(observation|result|process result)\s*:?\s*", "", text.strip(), flags=re.I).strip() or text.strip()
    severity = "info"
    if re.search(r"\b(critical|failed|failure|unsafe)\b", text, flags=re.I):
        severity = "critical"
    elif re.search(r"\b(major|severe|bad)\b", text, flags=re.I):
        severity = "major"
    elif re.search(r"\b(minor|slight)\b", text, flags=re.I):
        severity = "minor"
    observation = ProcessObservation(
        project_id=project.id if project else None,
        run_id=run.id if run else None,
        title=body[:120],
        observation_text=body,
        severity=severity,
        created_from_sms_id=source_sms_id,
        created_from_secure_capture_id=secure_capture_id,
        observed_at=timestamp,
        created_at=timestamp,
    )
    session.add(observation)
    session.flush()
    return observation


def create_run_metric(
    session: Session,
    text: str,
    *,
    settings: Settings,
    project_name: str | None = None,
    run_name: str | None = None,
    source: str = "manual",
    secure_capture_id: int | None = None,
    now: datetime | None = None,
) -> RunMetric:
    timestamp = ensure_timezone(now or datetime.now(settings.timezone), settings)
    project = _project_from_text_or_name(session, text, project_name)
    run = get_or_create_run(
        session,
        run_name=extract_run_name(text, run_name),
        project=project,
        settings=settings,
        now=timestamp,
    )
    body = re.sub(r"^(metric|run metric|result|process result)\s*:?\s*", "", text.strip(), flags=re.I).strip() or text.strip()
    metric_name = body[:80]
    metric_value_text = body
    numeric = None
    unit = None
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([A-Za-z%/]+)?", body)
    if match:
        numeric = float(match.group(1))
        unit = match.group(2)
        metric_value_text = match.group(0)
        metric_name = body[: match.start()].strip(" :-") or body[:80]
    metric = RunMetric(
        project_id=project.id if project else None,
        run_id=run.id if run else None,
        metric_name=metric_name[:160],
        metric_value_text=metric_value_text[:240],
        metric_value_numeric=numeric,
        metric_unit=unit,
        source=source,
        measured_at=timestamp,
        created_at=timestamp,
    )
    session.add(metric)
    session.flush()
    return metric


def summarize_work_intelligence(
    session: Session,
    *,
    settings: Settings,
    start: datetime | None = None,
    end: datetime | None = None,
) -> WorkIntelligenceSummary:
    end = ensure_timezone(end or datetime.now(settings.timezone), settings)
    start = ensure_timezone(start or (end - timedelta(days=30)), settings)
    entries = list(
        session.scalars(
            select(TimeEntry)
            .where(TimeEntry.interval_start >= start, TimeEntry.interval_start < end, TimeEntry.category_primary == "Work")
            .order_by(TimeEntry.interval_start.asc())
        )
    )
    project_minutes: dict[str, int] = defaultdict(int)
    focus_minutes: dict[str, int] = defaultdict(int)
    high_value = 0
    low_value = 0
    for entry in entries:
        minutes = duration_minutes(entry.interval_start, entry.interval_end)
        project_minutes[entry.project_name or "Unassigned Work"] += minutes
        focus_minutes[entry.work_focus_type or "unclassified"] += minutes
        if entry.value_level in {"high", "critical"}:
            high_value += minutes
        if entry.value_level == "low":
            low_value += minutes

    changes = list(session.scalars(select(ProcessChange).where(ProcessChange.occurred_at >= start, ProcessChange.occurred_at < end).order_by(ProcessChange.occurred_at.desc()).limit(50)))
    observations = list(session.scalars(select(ProcessObservation).where(ProcessObservation.observed_at >= start, ProcessObservation.observed_at < end).order_by(ProcessObservation.observed_at.desc()).limit(50)))
    metrics = list(session.scalars(select(RunMetric).where(RunMetric.created_at >= start, RunMetric.created_at < end).order_by(RunMetric.created_at.desc()).limit(50)))
    delegation_candidates = [entry for entry in entries if entry.delegation_candidate]
    staffing_signals = [entry for entry in entries if entry.staffing_signal]
    return WorkIntelligenceSummary(
        project_minutes=dict(sorted(project_minutes.items(), key=lambda item: item[1], reverse=True)),
        focus_minutes=dict(sorted(focus_minutes.items(), key=lambda item: item[1], reverse=True)),
        high_value_minutes=high_value,
        low_value_minutes=low_value,
        delegation_candidates=delegation_candidates,
        staffing_signals=staffing_signals,
        process_changes=changes,
        observations=observations,
        metrics=metrics,
    )
