from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .classification_service import add_project, add_project_aliases, seed_default_data
from .briefing_service import generate_briefing, briefing_local_url
from .circle_back_service import get_circle_back_context
from .config import Settings, get_settings
from .database import SessionLocal, create_engine_from_url, engine as default_engine, init_db
from .llm.llm_service import get_llm_provider
from .llm_report_service import llm_report_status, run_llm_report_test
from .inbox_service import dismiss_inbox_item, resolve_inbox_item
from .models import (
    CheckinPrompt,
    BriefingReport,
    BriefingRequest,
    ClassificationRule,
    LLMCall,
    ProcessChange,
    ProcessObservation,
    Project,
    ProjectNote,
    Reminder,
    RunMetric,
    SecureCapture,
    SecretaryInboxItem,
    TimeEntry,
    TodoItem,
    WorkDaySummary,
)
from .project_memory_service import create_project_note, summarize_project
from .reminder_service import create_reminder_from_text
from .report_service import generate_report
from .scheduler_service import start_scheduler
from .secure_capture_service import SecureCaptureError, process_secure_capture
from .secretary_service import create_time_entry_from_text, process_inbound_text
from .sms_service import record_inbound_sms, twiml_response, validate_twilio_webhook
from .todo_service import create_todo_from_text
from .utils import duration_minutes, ensure_timezone, mask_phone_number
from .work_hours_service import average_event_time, totals_for_period
from .work_intelligence_service import summarize_work_intelligence
from scripts.backup_database import BackupError, create_backup


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
SECURE_CAPTURE_DEBUG_LOG = BASE_DIR / "data" / "secure_capture_attempts.jsonl"


def _log_secure_capture_attempt(request: Request, status: int, detail: str) -> None:
    SECURE_CAPTURE_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "remote": request.client.host if request.client else "",
        "status": status,
        "detail": detail,
        "content_type": request.headers.get("content-type", ""),
        "content_length": request.headers.get("content-length", ""),
        "user_agent": request.headers.get("user-agent", ""),
    }
    with SECURE_CAPTURE_DEBUG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _recent_secure_capture_attempts(limit: int = 20) -> list[dict[str, Any]]:
    if not SECURE_CAPTURE_DEBUG_LOG.exists():
        return []
    lines = SECURE_CAPTURE_DEBUG_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _utc_to_local(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(settings.timezone)


def _session_dependency(session_factory: sessionmaker[Session]):
    def get_session():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    return get_session


async def _request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return dict(await request.json())
    form = await request.form()
    return dict(form)


def _entry_dict(entry: TimeEntry, settings: Settings) -> dict[str, Any]:
    return {
        "id": entry.id,
        "interval_start": ensure_timezone(entry.interval_start, settings).isoformat(),
        "interval_end": ensure_timezone(entry.interval_end, settings).isoformat(),
        "raw_text": entry.raw_text,
        "category_primary": entry.category_primary,
        "category_secondary": entry.category_secondary,
        "project_name": entry.project_name,
        "classification_confidence": entry.classification_confidence,
        "source": entry.source,
    }


def _resolved_local_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    value = Path(path)
    if not value.is_absolute():
        value = BASE_DIR / value
    return value.resolve()


def _masked_secret(value: str) -> str:
    if not value:
        return "not set"
    return "***" + value[-4:] if len(value) > 4 else "***"


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def create_app(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        if engine is None and settings.database_url == get_settings().database_url:
            session_factory = SessionLocal
            engine = default_engine
        else:
            engine = engine or create_engine_from_url(settings.database_url)
            session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    elif engine is None:
        engine = session_factory.kw.get("bind")

    get_session = _session_dependency(session_factory)
    app = FastAPI(title="Time Secretary")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.scheduler = None

    @app.on_event("startup")
    def startup() -> None:
        init_db(bind=engine)
        with session_factory() as session:
            seed_default_data(session)
            session.commit()
        if settings.start_scheduler and app.state.scheduler is None:
            app.state.scheduler = start_scheduler(session_factory, settings)

    @app.on_event("shutdown")
    def shutdown() -> None:
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)
            app.state.scheduler = None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "dev_mode": settings.dev_mode,
            "effective_dev_sms": settings.effective_dev_sms,
            "scheduler": bool(app.state.scheduler),
        }

    @app.post("/sms/inbound")
    async def inbound_sms(request: Request, db: Session = Depends(get_session)) -> Response:
        form = await request.form()
        form_dict = dict(form)
        url = str(request.url)
        if settings.public_base_url:
            url = settings.public_base_url.rstrip("/") + request.url.path
        signature = request.headers.get("X-Twilio-Signature")
        if not validate_twilio_webhook(
            settings=settings,
            url=url,
            form_data=form_dict,
            signature=signature,
        ):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        body = str(form_dict.get("Body", "")).strip()
        sms = record_inbound_sms(
            db,
            from_number=str(form_dict.get("From", "")),
            to_number=str(form_dict.get("To", "")),
            body=body,
            provider_message_id=str(form_dict.get("MessageSid", "")),
            raw_payload=form_dict,
        )
        result = process_inbound_text(db, body, settings=settings, sms_message_id=sms.id)
        db.commit()
        return Response(content=twiml_response(result.reply), media_type="application/xml")

    @app.post("/secure-capture")
    async def secure_capture(request: Request, db: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            payload = dict(await request.json())
        except Exception as exc:
            _log_secure_capture_attempt(request, 400, "Invalid JSON")
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        try:
            result = process_secure_capture(db, payload, settings=settings)
        except SecureCaptureError as exc:
            db.rollback()
            message = str(exc)
            status = 403 if "secret" in message.lower() or "token" in message.lower() else 400
            if "disabled" in message.lower():
                status = 403
            _log_secure_capture_attempt(request, status, message)
            raise HTTPException(status_code=status, detail=message) from exc
        db.commit()
        _log_secure_capture_attempt(request, 200, result.message)
        response_payload = {
            "ok": True,
            "id": result.capture.id,
            "status": result.capture.processed_status,
            "message": result.message,
        }
        if result.local_url:
            response_payload["local_url"] = result.local_url
        return response_payload

    @app.get("/debug/secure-capture-attempts")
    def secure_capture_attempts() -> dict[str, Any]:
        return {"attempts": _recent_secure_capture_attempts()}

    @app.get("/entries")
    def entries(db: Session = Depends(get_session)) -> list[dict[str, Any]]:
        rows = db.scalars(select(TimeEntry).order_by(TimeEntry.interval_start.desc()).limit(200)).all()
        return [_entry_dict(row, settings) for row in rows]

    @app.post("/entries/{entry_id}/correct")
    async def correct_entry(entry_id: int, request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        entry = db.get(TimeEntry, entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        if data.get("category_primary"):
            entry.category_primary = str(data["category_primary"])
        if data.get("category_secondary"):
            entry.category_secondary = str(data["category_secondary"])
        if "project_name" in data:
            project_name = str(data.get("project_name") or "").strip()
            entry.project_name = project_name or None
            if project_name:
                add_project(db, project_name, aliases=[], category=entry.category_primary)
        if entry.raw_text:
            pattern = re.escape(entry.raw_text.strip())
            existing_rule = db.scalar(select(ClassificationRule).where(ClassificationRule.pattern == pattern))
            if existing_rule is None:
                db.add(
                    ClassificationRule(
                        name=f"dashboard-correction-{entry.id}",
                        pattern=pattern,
                        category_primary=entry.category_primary,
                        category_secondary=entry.category_secondary,
                        project_name=entry.project_name,
                        priority=115,
                        active=True,
                    )
                )
            else:
                existing_rule.category_primary = entry.category_primary
                existing_rule.category_secondary = entry.category_secondary
                existing_rule.project_name = entry.project_name
        db.commit()
        if data.get("return_to"):
            return RedirectResponse(str(data["return_to"]), status_code=303)
        return _entry_dict(entry, settings)

    @app.get("/reports/daily/today", response_class=PlainTextResponse)
    def daily_today(db: Session = Depends(get_session)) -> str:
        report = generate_report(db, "daily", settings=settings)
        db.commit()
        return report.markdown

    @app.post("/reports/generate")
    async def reports_generate(request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        report_type = str(data.get("report_type", "daily"))
        if report_type in {"year", "ytd"}:
            report_type = "yearly"
        if report_type not in {"daily", "weekly", "monthly", "yearly"}:
            raise HTTPException(status_code=400, detail="Unknown report type")
        report = generate_report(db, report_type, settings=settings)
        db.commit()
        if data.get("return_to"):
            return RedirectResponse(str(data["return_to"]), status_code=303)
        return {"summary": report.summary_text, "path": report.path, "markdown": report.markdown}

    @app.post("/briefings/generate")
    async def briefings_generate(request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        project = None
        project_id_raw = str(data.get("project_id", "") or "").strip()
        if project_id_raw.isdigit():
            project = db.get(Project, int(project_id_raw))
        secure_capture = None
        secure_capture_id_raw = str(data.get("secure_capture_id", "") or "").strip()
        if secure_capture_id_raw.isdigit():
            secure_capture = db.get(SecureCapture, int(secure_capture_id_raw))
        topic = str(
            data.get("topic")
            or data.get("project_name")
            or (secure_capture.project_name if secure_capture else "")
            or (project.name if project else "")
        ).strip()
        request_text = str(data.get("request_text") or (secure_capture.text if secure_capture else "") or topic or "Generate briefing").strip()
        if not topic and request_text:
            topic = request_text
        result = generate_briefing(
            db,
            request_text,
            settings=settings,
            request_source=str(data.get("source") or "dashboard"),
            briefing_type=str(data.get("briefing_type") or "custom"),
            topic=topic,
            include_sensitive=_truthy(data.get("include_sensitive"), settings.briefing_include_sensitive_default),
            created_from_secure_capture_id=secure_capture.id if secure_capture else None,
            now=datetime.now(settings.timezone),
        )
        db.commit()
        if data.get("return_to") or "text/html" in request.headers.get("accept", ""):
            if result.report is not None:
                return RedirectResponse(result.report.local_dashboard_path, status_code=303)
            return RedirectResponse("/dashboard/briefings", status_code=303)
        return {
            "ok": result.report is not None,
            "message": result.message,
            "local_url": result.local_url,
            "briefing_id": result.report.id if result.report else None,
            "request_id": result.request.id,
        }

    @app.get("/projects")
    def projects(db: Session = Depends(get_session)) -> list[dict[str, Any]]:
        rows = db.scalars(select(Project).order_by(Project.name.asc())).all()
        return [
            {
                "id": project.id,
                "name": project.name,
                "aliases": project.aliases,
                "category_default": project.category_default,
                "active": project.active,
            }
            for project in rows
        ]

    @app.post("/projects")
    async def projects_create(request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        aliases = [part.strip() for part in str(data.get("aliases", "")).split(",") if part.strip()]
        project = add_project(
            db,
            str(data.get("name", "")).strip(),
            aliases=aliases,
            category=str(data.get("category_default", "Unknown")),
        )
        db.commit()
        if data.get("return_to"):
            return RedirectResponse(str(data["return_to"]), status_code=303)
        return {"id": project.id, "name": project.name, "aliases": project.aliases}

    @app.post("/projects/{project_id}/aliases")
    async def project_aliases(project_id: int, request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        aliases = [part.strip() for part in str(data.get("aliases", "")).split(",") if part.strip()]
        add_project_aliases(db, project.name, aliases)
        db.commit()
        if data.get("return_to"):
            return RedirectResponse(str(data["return_to"]), status_code=303)
        return {"id": project.id, "aliases": project.aliases}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        now = datetime.now(settings.timezone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        entries_today = db.scalars(
            select(TimeEntry)
            .where(TimeEntry.interval_start >= start, TimeEntry.interval_start < end)
            .order_by(TimeEntry.interval_start.asc())
        ).all()
        missed = db.scalars(
            select(CheckinPrompt)
            .where(CheckinPrompt.status == "missed", CheckinPrompt.scheduled_for_start >= start)
            .order_by(CheckinPrompt.scheduled_for_start.desc())
        ).all()
        todos = db.scalars(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]))
            .order_by(TodoItem.due_at.asc(), TodoItem.created_at.desc())
            .limit(30)
        ).all()
        reminders = db.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed", "sent"]))
            .order_by(Reminder.remind_at.asc())
            .limit(30)
        ).all()
        notes = db.scalars(select(ProjectNote).order_by(ProjectNote.created_at.desc()).limit(20)).all()
        inbox = db.scalars(
            select(SecretaryInboxItem)
            .where(SecretaryInboxItem.status == "open")
            .order_by(SecretaryInboxItem.created_at.desc())
            .limit(20)
        ).all()
        projects = db.scalars(select(Project).where(Project.active.is_(True)).order_by(Project.name.asc())).all()
        report_paths = sorted(Path(settings.reports_dir).glob("*.md")) if Path(settings.reports_dir).exists() else []
        work_minutes = sum(
            duration_minutes(entry.interval_start, entry.interval_end)
            for entry in entries_today
            if entry.category_primary == "Work"
        )
        home_minutes = sum(
            duration_minutes(entry.interval_start, entry.interval_end)
            for entry in entries_today
            if entry.category_primary == "Home"
        )
        project_minutes: dict[str, int] = {}
        for entry in entries_today:
            if entry.project_name:
                project_minutes[entry.project_name] = project_minutes.get(entry.project_name, 0) + duration_minutes(entry.interval_start, entry.interval_end)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "settings": settings,
                "entries": entries_today,
                "missed": missed,
                "todos": todos,
                "reminders": reminders,
                "notes": notes,
                "inbox": inbox,
                "projects": projects,
                "report_paths": report_paths,
                "work_hours": work_minutes / 60,
                "home_hours": home_minutes / 60,
                "project_hours": {name: minutes / 60 for name, minutes in project_minutes.items()},
                "now": now,
            },
        )

    @app.get("/dashboard/settings", response_class=HTMLResponse)
    def dashboard_settings(request: Request, notice: str = "", db: Session = Depends(get_session)) -> HTMLResponse:
        now = datetime.now(settings.timezone)
        database_path = _resolved_local_path(settings.database_path)
        reports_path = _resolved_local_path(settings.reports_dir)
        backups_path = _resolved_local_path(settings.backups_dir)
        recent_llm_calls = db.scalars(
            select(LLMCall).order_by(LLMCall.created_at.desc()).limit(10)
        ).all()
        report_llm_status = llm_report_status(db, settings)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "settings": settings,
                "now": now,
                "notice": notice,
                "database_path": database_path,
                "database_exists": bool(database_path and database_path.exists()),
                "reports_path": reports_path,
                "reports_exists": bool(reports_path and reports_path.exists()),
                "backups_path": backups_path,
                "backups_exists": bool(backups_path and backups_path.exists()),
                "masked_user_phone": mask_phone_number(settings.user_phone_number),
                "masked_twilio_sid": _masked_secret(settings.twilio_account_sid),
                "masked_twilio_from": mask_phone_number(settings.twilio_from_number),
                "recent_llm_calls": recent_llm_calls,
                "report_llm_status": report_llm_status,
            },
        )

    @app.post("/dashboard/settings/action")
    async def dashboard_settings_action(request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        action = str(data.get("action", ""))
        notice = "No action selected."

        if action == "backup":
            try:
                backup_path = create_backup(settings=settings)
                notice = f"Backup created: {backup_path.name}"
            except BackupError as exc:
                notice = f"Backup failed: {exc}"
        elif action == "daily_report":
            report = generate_report(db, "daily", settings=settings)
            db.commit()
            notice = f"Daily report generated: {Path(report.path).name if report.path else report.summary_text}"
        elif action == "test_llm":
            provider = get_llm_provider(settings)
            available = provider.is_available()
            notice = f"LLM {provider.provider_name}: {'available' if available else 'unavailable'}"
        elif action == "test_llm_report_model":
            result = run_llm_report_test(db, settings)
            db.commit()
            if result.success:
                source = "cache" if result.from_cache else "model"
                notice = f"LLM report test passed via {source}: {result.model or settings.llm_report_model}"
            else:
                notice = f"LLM report test used deterministic fallback: {result.error_message or 'not enabled'}"
        elif action == "benchmark_llm_reports":
            notice = "Benchmark helper ready: run scripts/benchmark_llm_models.py from the Anaconda prompt."

        return RedirectResponse(f"/dashboard/settings?notice={quote_plus(notice)}", status_code=303)

    @app.get("/dashboard/work-hours", response_class=HTMLResponse)
    def dashboard_work_hours(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        now = datetime.now(settings.timezone)
        today = totals_for_period(db, "today", settings=settings, now=now)
        week = totals_for_period(db, "week", settings=settings, now=now)
        month = totals_for_period(db, "month", settings=settings, now=now)
        year = totals_for_period(db, "year", settings=settings, now=now)
        summaries = db.scalars(select(WorkDaySummary).order_by(WorkDaySummary.date.desc()).limit(45)).all()
        db.commit()
        return templates.TemplateResponse(
            request,
            "work_hours.html",
            {
                "request": request,
                "settings": settings,
                "now": now,
                "today": today,
                "week": week,
                "month": month,
                "year": year,
                "summaries": summaries,
                "average_arrival": average_event_time(list(summaries), "arrived_work_at"),
                "average_leave": average_event_time(list(summaries), "left_work_at"),
            },
        )

    @app.get("/dashboard/work-hours/export.csv")
    def dashboard_work_hours_export(db: Session = Depends(get_session)) -> StreamingResponse:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["date", "arrived_work_at", "left_work_at", "worksite_minutes", "logged_work_minutes", "lunch_break_minutes", "missing_arrival", "missing_leave", "confidence"])
        rows = db.scalars(select(WorkDaySummary).order_by(WorkDaySummary.date.asc())).all()
        for row in rows:
            writer.writerow(
                [
                    row.date.isoformat(),
                    ensure_timezone(row.arrived_work_at, settings).isoformat() if row.arrived_work_at else "",
                    ensure_timezone(row.left_work_at, settings).isoformat() if row.left_work_at else "",
                    row.worksite_duration_minutes or "",
                    row.logged_work_minutes,
                    row.lunch_break_minutes,
                    row.missing_arrival_event,
                    row.missing_leave_event,
                    row.confidence,
                ]
            )
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=work_day_summaries.csv"},
        )

    @app.get("/dashboard/work-intelligence", response_class=HTMLResponse)
    def dashboard_work_intelligence(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        now = datetime.now(settings.timezone)
        start = now - timedelta(days=30)
        summary = summarize_work_intelligence(db, settings=settings, start=start, end=now)
        return templates.TemplateResponse(
            request,
            "work_intelligence.html",
            {
                "request": request,
                "settings": settings,
                "now": now,
                "summary": summary,
            },
        )

    @app.get("/dashboard/secure-captures", response_class=HTMLResponse)
    def dashboard_secure_captures(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        captures = db.scalars(select(SecureCapture).order_by(SecureCapture.received_at.desc()).limit(100)).all()
        return templates.TemplateResponse(
            request,
            "secure_captures.html",
            {
                "request": request,
                "settings": settings,
                "captures": captures,
                "received_local": lambda value: _utc_to_local(value, settings),
                "now": datetime.now(settings.timezone),
            },
        )

    @app.get("/dashboard/briefings", response_class=HTMLResponse)
    def dashboard_briefings(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        reports = db.scalars(select(BriefingReport).order_by(BriefingReport.generated_at.desc()).limit(100)).all()
        requests = db.scalars(select(BriefingRequest).order_by(BriefingRequest.requested_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request,
            "briefings.html",
            {
                "request": request,
                "settings": settings,
                "reports": reports,
                "requests": requests,
                "now": datetime.now(settings.timezone),
                "local_url": lambda report: briefing_local_url(report, settings),
                "local_dt": lambda value: ensure_timezone(value, settings).strftime("%Y-%m-%d %H:%M") if value else "",
            },
        )

    @app.get("/dashboard/briefings/{opaque_id}", response_class=HTMLResponse)
    def dashboard_briefing_detail(opaque_id: str, request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        report = db.scalar(select(BriefingReport).where(BriefingReport.opaque_id == opaque_id))
        if report is None:
            raise HTTPException(status_code=404, detail="Briefing not found")
        source_request = db.scalar(select(BriefingRequest).where(BriefingRequest.generated_briefing_id == report.id))
        todos = db.scalars(select(TodoItem).where(TodoItem.project_name == report.project_name).order_by(TodoItem.created_at.desc()).limit(20)).all() if report.project_name else []
        reminders = db.scalars(select(Reminder).where(Reminder.related_project_id == report.project_id).order_by(Reminder.remind_at.asc()).limit(20)).all() if report.project_id else []
        changes = db.scalars(select(ProcessChange).where(ProcessChange.project_id == report.project_id).order_by(ProcessChange.occurred_at.desc()).limit(20)).all() if report.project_id else []
        observations = db.scalars(select(ProcessObservation).where(ProcessObservation.project_id == report.project_id).order_by(ProcessObservation.observed_at.desc()).limit(20)).all() if report.project_id else []
        metrics = db.scalars(select(RunMetric).where(RunMetric.project_id == report.project_id).order_by(RunMetric.created_at.desc()).limit(20)).all() if report.project_id else []
        return templates.TemplateResponse(
            request,
            "briefing_detail.html",
            {
                "request": request,
                "settings": settings,
                "report": report,
                "source_request": source_request,
                "todos": todos,
                "reminders": reminders,
                "changes": changes,
                "observations": observations,
                "metrics": metrics,
                "now": datetime.now(settings.timezone),
                "local_url": briefing_local_url(report, settings),
                "local_dt": lambda value: ensure_timezone(value, settings).strftime("%Y-%m-%d %H:%M") if value else "",
            },
        )

    @app.get("/dashboard/briefings/{opaque_id}/export.md", response_class=PlainTextResponse)
    def dashboard_briefing_export(opaque_id: str, db: Session = Depends(get_session)) -> PlainTextResponse:
        report = db.scalar(select(BriefingReport).where(BriefingReport.opaque_id == opaque_id))
        if report is None:
            raise HTTPException(status_code=404, detail="Briefing not found")
        text = report.full_text or ""
        response = PlainTextResponse(text, media_type="text/markdown")
        response.headers["Content-Disposition"] = f'attachment; filename="briefing-{report.opaque_id}.md"'
        return response

    @app.get("/dashboard/export.csv")
    def dashboard_export(db: Session = Depends(get_session)) -> StreamingResponse:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "interval_start", "interval_end", "raw_text", "category_primary", "category_secondary", "project_name", "source"])
        rows = db.scalars(select(TimeEntry).order_by(TimeEntry.interval_start.asc())).all()
        for entry in rows:
            writer.writerow(
                [
                    entry.id,
                    ensure_timezone(entry.interval_start, settings).isoformat(),
                    ensure_timezone(entry.interval_end, settings).isoformat(),
                    entry.raw_text,
                    entry.category_primary,
                    entry.category_secondary,
                    entry.project_name or "",
                    entry.source,
                ]
            )
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=time_entries.csv"},
        )

    @app.post("/dashboard/inbox/{item_id}/convert")
    async def convert_inbox(item_id: int, request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        item = db.get(SecretaryInboxItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Inbox item not found")
        target = str(data.get("target", "note"))
        now = datetime.now(settings.timezone)
        if target == "todo":
            create_todo_from_text(db, item.raw_text, settings=settings, now=now)
        elif target == "reminder":
            create_reminder_from_text(db, item.raw_text, settings=settings, now=now)
        elif target == "time":
            create_time_entry_from_text(db, item.raw_text, settings=settings, now=now, source="correction")
        else:
            create_project_note(db, item.raw_text, settings=settings, note_type="note", body=item.raw_text, now=now)
        resolve_inbox_item(db, item)
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/dashboard/circle-back", response_class=HTMLResponse)
    def dashboard_circle_back(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        now = datetime.now(settings.timezone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        context = get_circle_back_context(db, start=start, end=end, settings=settings, now=now)
        todos_without_due = db.scalars(
            select(TodoItem)
            .where(TodoItem.status.in_(["open", "in_progress", "waiting"]), TodoItem.due_at.is_(None))
            .order_by(TodoItem.next_review_at.asc(), TodoItem.created_at.asc())
            .limit(50)
        ).all()
        notes_without_action = db.scalars(
            select(ProjectNote)
            .where(
                ProjectNote.capture_status.in_(["captured", "reviewed"]),
                ProjectNote.linked_todo_id.is_(None),
                ProjectNote.linked_reminder_id.is_(None),
            )
            .order_by(ProjectNote.next_review_at.asc(), ProjectNote.created_at.asc())
            .limit(50)
        ).all()
        return templates.TemplateResponse(
            request,
            "circle_back.html",
            {
                "request": request,
                "settings": settings,
                "context": context,
                "todos_without_due": todos_without_due,
                "notes_without_action": notes_without_action,
                "now": now,
            },
        )

    @app.post("/dashboard/circle-back/action")
    async def dashboard_circle_back_action(request: Request, db: Session = Depends(get_session)):
        data = await _request_data(request)
        item_type = str(data.get("item_type", ""))
        action = str(data.get("action", ""))
        item_id = int(str(data.get("id", "0")) or "0")
        project_name = str(data.get("project_name", "")).strip()
        now = datetime.now(settings.timezone)

        if item_type == "inbox":
            item = db.get(SecretaryInboxItem, item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Inbox item not found")
            if action == "todo":
                todo = create_todo_from_text(db, item.suggested_title or item.raw_text, settings=settings, now=now)
                item.converted_to_type = "TodoItem"
                item.converted_to_id = todo.id
                resolve_inbox_item(db, item)
            elif action == "reminder":
                reminder, _todo = create_reminder_from_text(
                    db,
                    f"remind me tomorrow to {item.suggested_title or item.raw_text}",
                    settings=settings,
                    now=now,
                )
                item.converted_to_type = "Reminder"
                item.converted_to_id = reminder.id
                resolve_inbox_item(db, item)
            elif action == "assign" and project_name:
                project = add_project(db, project_name, aliases=[], category=item.suggested_category or "Unknown")
                item.suggested_project_id = project.id
                item.suggested_project_name = project.name
                item.status = "reviewed"
                item.reviewed_at = now
            elif action == "dismiss":
                dismiss_inbox_item(db, item)

        elif item_type == "note":
            note = db.get(ProjectNote, item_id)
            if note is None:
                raise HTTPException(status_code=404, detail="Note not found")
            if action == "todo":
                todo = create_todo_from_text(db, note.body, settings=settings, now=now)
                note.linked_todo_id = todo.id
                note.capture_status = "converted_to_todo"
                note.needs_followup = False
            elif action == "reminder":
                reminder, _todo = create_reminder_from_text(
                    db,
                    f"remind me tomorrow to {note.title}",
                    settings=settings,
                    now=now,
                )
                note.linked_reminder_id = reminder.id
                note.capture_status = "converted_to_reminder"
                note.needs_followup = False
            elif action == "assign" and project_name:
                project = add_project(db, project_name, aliases=[], category="Unknown")
                note.project_id = project.id
                note.project_name = project.name
                note.capture_status = "reviewed"
            elif action == "dismiss":
                note.capture_status = "dismissed"
                note.needs_followup = False
                note.last_reviewed_at = now

        elif item_type == "todo":
            todo = db.get(TodoItem, item_id)
            if todo is None:
                raise HTTPException(status_code=404, detail="Todo not found")
            if action == "done":
                todo.status = "done"
                todo.capture_status = "completed"
                todo.needs_followup = False
                todo.completed_at = now
            elif action == "dismiss":
                todo.status = "canceled"
                todo.capture_status = "dismissed"
                todo.needs_followup = False

        elif item_type == "reminder":
            reminder = db.get(Reminder, item_id)
            if reminder is None:
                raise HTTPException(status_code=404, detail="Reminder not found")
            if action == "done":
                reminder.status = "done"
                reminder.capture_status = "completed"
            elif action == "dismiss":
                reminder.status = "canceled"
                reminder.capture_status = "dismissed"

        db.commit()
        return RedirectResponse("/dashboard/circle-back", status_code=303)

    @app.get("/dashboard/projects/{project_id}", response_class=HTMLResponse)
    def dashboard_project(project_id: int, request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        summary = summarize_project(db, project, settings)
        todos = db.scalars(
            select(TodoItem)
            .where(TodoItem.project_name == project.name)
            .order_by(TodoItem.created_at.desc())
            .limit(50)
        ).all()
        notes = db.scalars(
            select(ProjectNote)
            .where(ProjectNote.project_name == project.name)
            .order_by(ProjectNote.created_at.desc())
            .limit(50)
        ).all()
        entries = db.scalars(
            select(TimeEntry)
            .where(TimeEntry.project_name == project.name)
            .order_by(TimeEntry.interval_start.desc())
            .limit(50)
        ).all()
        reminders = db.scalars(
            select(Reminder)
            .where(Reminder.related_project_id == project.id)
            .order_by(Reminder.remind_at.asc())
            .limit(50)
        ).all()
        recent_cutoff = datetime.now(settings.timezone) - timedelta(days=14)
        notes_need_action = [
            note
            for note in notes
            if note.needs_followup and not note.linked_todo_id and not note.linked_reminder_id
        ]
        has_open_next_action = any(todo.status in {"open", "in_progress", "waiting"} for todo in todos) or any(
            reminder.status in {"scheduled", "snoozed", "sent"} for reminder in reminders
        )
        has_recent_time = any(ensure_timezone(entry.interval_start, settings) >= recent_cutoff for entry in entries)
        db.commit()
        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "request": request,
                "project": project,
                "summary": summary,
                "todos": todos,
                "notes": notes,
                "entries": entries,
                "reminders": reminders,
                "notes_need_action": notes_need_action,
                "has_open_next_action": has_open_next_action,
                "has_recent_time": has_recent_time,
                "settings": settings,
            },
        )

    return app


app = create_app()
