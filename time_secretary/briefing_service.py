from __future__ import annotations

import re
import secrets
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .classification_service import find_project_by_alias
from .config import Settings
from .fact_pack_service import build_briefing_fact_pack_from_records
from .llm_report_service import generate_llm_report
from .models import (
    BriefingReport,
    BriefingRequest,
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
from .utils import duration_minutes, ensure_timezone, simple_slug, utcnow


BASE_DIR = Path(__file__).resolve().parent.parent
BRIEFING_CAPTURE_TYPES = {"briefing_request", "report_request", "meeting_prep_request"}


@dataclass(frozen=True)
class BriefingResult:
    request: BriefingRequest
    report: BriefingReport | None
    message: str
    local_url: str | None


def is_briefing_request_text(text: str) -> bool:
    low = text.strip().lower()
    patterns = [
        r"^brief me on .+",
        r"^send me notes on .+",
        r"^meeting prep .+",
        r"^prep me for .+",
        r"^what do i need to know about .+",
        r"^open items for .+",
        r"^what changed on .+",
        r"^risks for .+",
        r"^questions for .+ meeting$",
        r"^generate .*briefing",
        r"^generate .*report",
    ]
    return any(re.search(pattern, low) for pattern in patterns)


def briefing_type_for_text(text: str, default: str = "custom") -> str:
    low = text.lower()
    if "meeting prep" in low or "prep me" in low or "questions for" in low:
        return "meeting_prep"
    if "run " in low:
        return "run"
    if "open items" in low:
        return "project"
    if "daily prep" in low:
        return "daily_prep"
    if "report" in low:
        return "topic"
    return default


def extract_briefing_topic(text: str) -> str:
    body = text.strip()
    body = re.sub(r"^(brief me on|send me notes on|meeting prep|prep me for|open items for|risks for)\s+", "", body, flags=re.I)
    body = re.sub(r"^what do i need to know about\s+", "", body, flags=re.I)
    body = re.sub(r"^what changed on\s+", "", body, flags=re.I)
    body = re.sub(r"^questions for\s+", "", body, flags=re.I)
    body = re.sub(r"\s+meeting$", "", body, flags=re.I)
    body = re.sub(r"\b(this week|today|this month|ytd)\b", "", body, flags=re.I)
    body = re.sub(r"^generate\s+(a\s+)?(meeting prep|briefing|report)\s+(on|about|for)\s+", "", body, flags=re.I)
    return re.sub(r"\s+", " ", body).strip(" .,:;") or text.strip()


def _resolve_reports_dir(settings: Settings) -> Path:
    path = Path(settings.briefing_reports_dir)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _opaque_id(session: Session) -> str:
    while True:
        value = secrets.token_hex(4)
        exists = session.scalar(select(BriefingReport).where(BriefingReport.opaque_id == value))
        if exists is None:
            return value


def _link_base(settings: Settings) -> str:
    return (
        settings.briefing_tailscale_base_url
        or settings.briefing_public_base_url
        or settings.public_base_url
    ).rstrip("/")


def briefing_local_url(report: BriefingReport, settings: Settings) -> str:
    base = _link_base(settings)
    return f"{base}{report.local_dashboard_path}" if base else report.local_dashboard_path


def sms_safe_briefing_reply(report: BriefingReport | None, settings: Settings) -> str:
    if report is None:
        return "Briefing ready. Open Time Secretary dashboard -> Briefings -> Latest."
    link = briefing_local_url(report, settings)
    if not link:
        return "Briefing ready. Open Time Secretary dashboard -> Briefings -> Latest."
    return f"Briefing ready: {link}"


def _project_matches(session: Session, topic: str) -> list[Project]:
    if not topic:
        return []
    exact = session.scalar(select(Project).where(Project.name.ilike(topic.strip()), Project.active.is_(True)))
    if exact is not None:
        return [exact]
    low = topic.lower()
    matches: list[Project] = []
    for project in session.scalars(select(Project).where(Project.active.is_(True))).all():
        aliases = [project.name, *project.aliases]
        if any(alias and alias.lower() in low for alias in aliases):
            matches.append(project)
    project, score = find_project_by_alias(session, topic)
    if project is not None and score >= 0.65 and project not in matches:
        matches.append(project)
    return matches


def _extract_run_name(text: str) -> str | None:
    match = re.search(r"\brun\s+([A-Za-z0-9][A-Za-z0-9_.-]*)", text, flags=re.I)
    return match.group(1) if match else None


def _window(text: str, settings: Settings, now: datetime) -> tuple[datetime, datetime, int]:
    low = text.lower()
    if "this week" in low:
        days = 7
    elif "today" in low:
        days = 1
    elif "this month" in low:
        days = 31
    else:
        days = settings.briefing_default_window_days
    end = ensure_timezone(now, settings)
    return end - timedelta(days=days), end, days


def _text_filter(model, topic: str, fields: list[str]):
    clauses = []
    pattern = f"%{topic}%"
    for field in fields:
        clauses.append(getattr(model, field).ilike(pattern))
    return or_(*clauses) if clauses else None


def _query_recent(session: Session, model, date_field: str, start: datetime, end: datetime, filters, limit: int = 20):
    query = select(model).where(getattr(model, date_field) >= start, getattr(model, date_field) <= end)
    if filters is not None:
        query = query.where(filters)
    return list(session.scalars(query.order_by(getattr(model, date_field).desc()).limit(limit)))


def _project_or_topic_filter(model, topic: str, project: Project | None, text_fields: list[str]):
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


def _line(value: str, limit: int = 180) -> str:
    clean = re.sub(r"\s+", " ", value or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _local_dt(value: datetime | None, settings: Settings) -> str:
    if value is None:
        return ""
    return ensure_timezone(value, settings).strftime("%Y-%m-%d %H:%M")


def _append_items(lines: list[str], empty: str, values: list[str]) -> None:
    if values:
        lines.extend(f"- {value}" for value in values)
    else:
        lines.append(f"- {empty}")


def _build_markdown(
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
) -> tuple[str, bool]:
    title_topic = project.name if project else topic
    lines = [
        f"# Briefing - {title_topic or 'General'}",
        "",
        f"- Type: {briefing_type}",
        f"- Generated: {_local_dt(end, settings)}",
        f"- Window: {_local_dt(start, settings)} to {_local_dt(end, settings)}",
        f"- Source request: {_line(request_text, 240)}",
    ]
    if project:
        lines.append(f"- Project: {project.name}")
    if run_name:
        lines.append(f"- Run: {run_name}")
    lines.append("")

    sensitive_sources = [
        *(note for note in notes if note.sensitivity == "sensitive"),
        *secure_captures,
    ]
    includes_sensitive = include_sensitive and bool(sensitive_sources)

    lines.extend(["## Executive Summary", ""])
    summary_bits = [
        f"{len(notes)} project note(s)",
        f"{len(todos)} open action(s)",
        f"{len(changes)} recent change(s)",
        f"{len(observations)} observation(s)",
        f"{len(metrics)} metric(s)",
    ]
    lines.append("- Local records found: " + ", ".join(summary_bits) + ".")
    if not include_sensitive and sensitive_sources:
        lines.append("- Sensitive capture details are withheld in this briefing by current settings.")
    lines.append("")

    lines.extend(["## Recent Activity", ""])
    activity = [
        f"{_local_dt(entry.interval_start, settings)} - {_line(entry.raw_text)}"
        for entry in entries[:12]
    ]
    _append_items(lines, "No matching recent time entries.", activity)
    if notes:
        lines.append("")
        lines.append("Recent notes:")
        _append_items(
            lines,
            "No matching project notes.",
            [f"{_local_dt(note.created_at, settings)} - {note.note_type}: {_line(note.body)}" for note in notes[:12] if include_sensitive or note.sensitivity != "sensitive"],
        )
    if include_sensitive and secure_captures:
        lines.append("")
        lines.append("Secure captures:")
        _append_items(
            lines,
            "No matching secure captures.",
            [f"{_local_dt(capture.created_at, settings)} - {capture.capture_type}: {_line(capture.text)}" for capture in secure_captures[:10]],
        )
    lines.append("")

    lines.extend(["## Open Actions", ""])
    _append_items(
        lines,
        "No matching open todos.",
        [f"#{todo.id} {todo.priority} {todo.status}: {_line(todo.title)}" for todo in todos[:12]],
    )
    if reminders:
        lines.append("")
        lines.append("Upcoming reminders:")
        _append_items(
            lines,
            "No matching reminders.",
            [f"#{reminder.id} {_local_dt(reminder.remind_at, settings)}: {_line(reminder.title)}" for reminder in reminders[:8]],
        )
    lines.append("")

    lines.extend(["## Recent Changes", ""])
    _append_items(
        lines,
        "No matching process changes.",
        [f"#{change.id} {_local_dt(change.occurred_at, settings)} {change.change_type}: {_line(change.description)}" for change in changes[:12]],
    )
    lines.append("")

    lines.extend(["## Run/Performance Context", ""])
    run_lines = [f"Run {run.run_name}: {run.status}{' - ' + _line(run.notes) if run.notes else ''}" for run in runs[:8]]
    metric_lines = [f"{metric.metric_name}: {metric.metric_value_text}" for metric in metrics[:12]]
    _append_items(lines, "No matching run records.", run_lines)
    if metric_lines:
        lines.append("")
        lines.append("Metrics:")
        _append_items(lines, "No matching metrics.", metric_lines)
    lines.append("")

    lines.extend(["## Risks And Unresolved Issues", ""])
    risks = [
        f"{note.note_type}: {_line(note.body)}"
        for note in notes
        if note.note_type in {"risk", "question", "follow_up"} and (include_sensitive or note.sensitivity != "sensitive")
    ]
    risks.extend(f"{item.interpreted_type or 'Inbox'}: {_line(item.raw_text)}" for item in inbox_items[:8])
    risks.extend(f"{obs.severity}: {_line(obs.observation_text)}" for obs in observations if obs.severity in {"warn", "risk", "high"} or "risk" in obs.observation_text.lower())
    _append_items(lines, "No matching risks or unresolved issues.", risks[:12])
    lines.append("")

    lines.extend(["## Decisions", ""])
    decisions = [
        f"{_local_dt(note.created_at, settings)} - {_line(note.body)}"
        for note in notes
        if note.note_type == "decision" and (include_sensitive or note.sensitivity != "sensitive")
    ]
    _append_items(lines, "No matching decisions.", decisions[:8])
    lines.append("")

    lines.extend(["## Delegation/Staffing Signals", ""])
    signal_lines = [
        f"{insight.title}: {_line(insight.body)}"
        for insight in insights
        if insight.insight_type in {"delegation", "staffing"}
    ]
    signal_lines.extend(
        f"{entry.work_focus_type}: {entry.delegation_reason or entry.staffing_signal_reason}"
        for entry in entries
        if entry.delegation_candidate or entry.staffing_signal
    )
    _append_items(lines, "No matching delegation or staffing signals.", signal_lines[:10])
    lines.append("")

    lines.extend(["## Suggested Questions For Meeting", ""])
    questions = [
        f"What is the next action for {todo.title}?"
        for todo in todos[:4]
    ]
    questions.extend(f"What changed after {change.title}?" for change in changes[:4])
    questions.extend(f"What should we do about {_line(obs.title, 80)}?" for obs in observations[:4])
    _append_items(lines, "No specific meeting questions found.", questions[:10])
    lines.append("")

    lines.extend(["## Suggested Next Actions", ""])
    next_actions = [todo.title for todo in todos[:6]]
    next_actions.extend(note.title for note in notes if note.needs_followup and (include_sensitive or note.sensitivity != "sensitive"))
    _append_items(lines, "No explicit next actions found.", [_line(item) for item in next_actions[:10]])
    lines.append("")

    lines.extend(["## Supporting Local Reports", ""])
    _append_items(
        lines,
        "No matching report runs.",
        [f"{run.report_type}: {_line(run.summary_text)}" for run in report_runs[:8]],
    )
    if work_summaries:
        lines.append("")
        lines.append("Workday summaries:")
        _append_items(
            lines,
            "No workday summaries.",
            [f"{summary.date}: {summary.logged_work_minutes} logged work min, {summary.worksite_duration_minutes or 0} worksite min" for summary in work_summaries[:8]],
        )
    lines.append("")

    lines.extend(["## Sensitive Data Notice", ""])
    if include_sensitive:
        lines.append("- This local briefing may include sensitive project and secure-capture details. Do not send it by SMS.")
    else:
        lines.append("- Sensitive secure-capture text and sensitive notes were excluded. Generate with include_sensitive=true only for local review.")
    lines.append("")

    return "\n".join(lines), includes_sensitive


def generate_briefing(
    session: Session,
    request_text: str,
    *,
    settings: Settings,
    request_source: str = "sms",
    briefing_type: str | None = None,
    topic: str | None = None,
    include_sensitive: bool | None = None,
    created_from_sms_id: int | None = None,
    created_from_secure_capture_id: int | None = None,
    now: datetime | None = None,
) -> BriefingResult:
    timestamp = ensure_timezone(now or datetime.now(settings.timezone), settings)
    if not settings.briefings_enabled:
        request = BriefingRequest(
            request_text=request_text,
            request_source=request_source,
            topic=topic or extract_briefing_topic(request_text),
            requested_at=timestamp,
            status="failed",
            created_from_sms_id=created_from_sms_id,
            created_from_secure_capture_id=created_from_secure_capture_id,
        )
        session.add(request)
        session.flush()
        return BriefingResult(request, None, "Briefings are disabled.", None)

    clean_topic = topic or extract_briefing_topic(request_text)
    clean_type = briefing_type or briefing_type_for_text(request_text)
    requested_include = settings.briefing_include_sensitive_default if include_sensitive is None else include_sensitive
    include = bool(requested_include and settings.include_sensitive_local_reports)
    start, end, days = _window(request_text, settings, timestamp)
    run_name = _extract_run_name(request_text)
    matches = _project_matches(session, clean_topic)
    project = matches[0] if len(matches) == 1 else None

    request = BriefingRequest(
        request_text=request_text,
        request_source=request_source,
        topic=clean_topic,
        project_id=project.id if project else None,
        project_name=project.name if project else None,
        run_name=run_name,
        requested_at=timestamp,
        time_window_days=days,
        include_sensitive=bool(requested_include),
        sms_safe=True,
        status="requested",
        created_from_sms_id=created_from_sms_id,
        created_from_secure_capture_id=created_from_secure_capture_id,
        created_at=timestamp,
    )
    session.add(request)
    session.flush()

    if len(matches) > 1:
        request.status = "needs_clarification"
        session.flush()
        return BriefingResult(
            request,
            None,
            "I found multiple matching projects. Please send a more specific briefing request.",
            None,
        )

    filters_by_project = lambda model, fields: _project_or_topic_filter(model, clean_topic, project, fields)

    notes = _query_recent(session, ProjectNote, "created_at", start, end, filters_by_project(ProjectNote, ["title", "body", "raw_text"]), 40)
    if not include:
        notes = [note for note in notes if note.sensitivity != "sensitive"]
    secure_captures = []
    if include:
        secure_captures = _query_recent(session, SecureCapture, "created_at", start, end, filters_by_project(SecureCapture, ["text", "capture_type", "run_name"]), 30)
    todos = list(
        session.scalars(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]))
            .where(filters_by_project(TodoItem, ["title", "description"]) if filters_by_project(TodoItem, ["title", "description"]) is not None else True)
            .order_by(TodoItem.created_at.desc())
            .limit(40)
        )
    )
    reminders = list(
        session.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed", "sent"]))
            .where(filters_by_project(Reminder, ["title", "body"]) if filters_by_project(Reminder, ["title", "body"]) is not None else True)
            .order_by(Reminder.remind_at.asc())
            .limit(20)
        )
    )
    inbox_items = _query_recent(session, SecretaryInboxItem, "created_at", start, end, filters_by_project(SecretaryInboxItem, ["raw_text", "suggested_title", "suggested_next_action"]), 20)
    entries = _query_recent(session, TimeEntry, "interval_start", start, end, filters_by_project(TimeEntry, ["raw_text", "project_name"]), 40)
    work_summaries = list(session.scalars(select(WorkDaySummary).where(WorkDaySummary.date >= start.date(), WorkDaySummary.date <= end.date()).order_by(WorkDaySummary.date.desc()).limit(20)))
    insights = _query_recent(session, WorkInsight, "created_at", start, end, filters_by_project(WorkInsight, ["title", "body"]), 20)
    runs_filter = filters_by_project(RunRecord, ["run_name", "notes", "equipment", "material"])
    if run_name:
        runs_filter = or_(runs_filter, RunRecord.run_name.ilike(f"%{run_name}%")) if runs_filter is not None else RunRecord.run_name.ilike(f"%{run_name}%")
    runs = list(session.scalars(select(RunRecord).where(runs_filter if runs_filter is not None else True).order_by(RunRecord.created_at.desc()).limit(20)))
    change_filter = filters_by_project(ProcessChange, ["title", "description", "reason", "expected_effect"])
    changes = _query_recent(session, ProcessChange, "occurred_at", start, end, change_filter, 30)
    obs_filter = filters_by_project(ProcessObservation, ["title", "observation_text"])
    observations = _query_recent(session, ProcessObservation, "observed_at", start, end, obs_filter, 30)
    metric_filter = filters_by_project(RunMetric, ["metric_name", "metric_value_text", "source"])
    metrics = _query_recent(session, RunMetric, "created_at", start, end, metric_filter, 30)
    report_runs = _query_recent(session, ReportRun, "generated_at", start, end, _text_filter(ReportRun, clean_topic, ["summary_text", "report_type"]) if clean_topic else None, 10)

    markdown, includes_sensitive = _build_markdown(
        request_text=request_text,
        briefing_type=clean_type,
        topic=clean_topic,
        project=project,
        run_name=run_name,
        start=start,
        end=end,
        include_sensitive=include,
        settings=settings,
        notes=notes,
        secure_captures=secure_captures,
        todos=todos,
        reminders=reminders,
        inbox_items=inbox_items,
        entries=entries,
        work_summaries=work_summaries,
        insights=insights,
        runs=runs,
        changes=changes,
        observations=observations,
        metrics=metrics,
        report_runs=report_runs,
    )
    fact_pack = build_briefing_fact_pack_from_records(
        request_text=request_text,
        briefing_type=clean_type,
        topic=clean_topic,
        project=project,
        run_name=run_name,
        start=start,
        end=end,
        include_sensitive=include,
        settings=settings,
        notes=notes,
        secure_captures=secure_captures,
        todos=todos,
        reminders=reminders,
        inbox_items=inbox_items,
        entries=entries,
        work_summaries=work_summaries,
        insights=insights,
        runs=runs,
        changes=changes,
        observations=observations,
        metrics=metrics,
        report_runs=report_runs,
    )
    llm_result = generate_llm_report(
        session,
        fact_pack=fact_pack,
        deterministic_markdown=markdown,
        settings=settings,
        task_type=f"briefing:{clean_type}",
        sms_safe=False,
    )
    final_markdown = llm_result.final_markdown

    opaque = _opaque_id(session)
    dashboard_path = f"/dashboard/briefings/{opaque}"
    reports_dir = _resolve_reports_dir(settings)
    artifact_dir = reports_dir / f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{opaque}-{simple_slug(clean_type)}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fact_pack_path = artifact_dir / "deterministic_fact_pack.json"
    deterministic_path = artifact_dir / "deterministic_briefing.md"
    final_path = artifact_dir / "final_briefing.md"
    llm_narrative_path = artifact_dir / "llm_narrative.md"
    fact_pack_path.write_text(json.dumps(fact_pack, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    deterministic_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    if llm_result.narrative_markdown:
        llm_narrative_path.write_text(llm_result.narrative_markdown.rstrip() + "\n", encoding="utf-8")
        llm_narrative_path_text = str(llm_narrative_path)
    else:
        llm_narrative_path_text = None
    final_path.write_text(final_markdown.rstrip() + "\n", encoding="utf-8")
    report = BriefingReport(
        opaque_id=opaque,
        briefing_type=clean_type,
        topic=clean_topic,
        project_id=project.id if project else None,
        project_name=project.name if project else None,
        run_name=run_name,
        period_start=start,
        period_end=end,
        sms_summary="Briefing ready.",
        markdown_path=str(final_path),
        full_text=final_markdown,
        includes_sensitive=includes_sensitive,
        generation_mode=llm_result.mode,
        fact_pack_path=str(fact_pack_path),
        llm_narrative_path=llm_narrative_path_text,
        final_markdown_path=str(final_path),
        llm_cache_key=llm_result.cache_key,
        llm_model=llm_result.model,
        llm_duration_ms=llm_result.duration_ms,
        llm_validation_warnings_json=json.dumps(llm_result.validation_warnings or []),
        local_dashboard_path=dashboard_path,
        generated_at=timestamp,
        requested_from_sms_id=created_from_sms_id,
        requested_from_secure_capture_id=created_from_secure_capture_id,
        created_at=timestamp,
    )
    session.add(report)
    session.flush()
    request.generated_briefing_id = report.id
    request.status = "generated"
    session.flush()
    return BriefingResult(request, report, "Briefing ready.", briefing_local_url(report, settings))
