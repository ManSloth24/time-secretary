from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import AppState, CheckinPrompt, Reminder
from .report_service import generate_report
from .reminder_service import due_reminders, mark_reminder_sent
from .sms_service import send_sms
from .utils import ensure_timezone, floor_to_interval, human_dt, interval_for_now, utcnow
from scripts.backup_database import BackupError, create_backup


PAUSE_KEY = "prompts_paused_until"


def set_state(session: Session, key: str, value: str) -> None:
    state = session.get(AppState, key)
    if state is None:
        state = AppState(key=key, value=value)
        session.add(state)
    else:
        state.value = value
    session.flush()


def get_state(session: Session, key: str) -> str | None:
    state = session.get(AppState, key)
    return state.value if state else None


def clear_state(session: Session, key: str) -> None:
    state = session.get(AppState, key)
    if state:
        session.delete(state)
        session.flush()


def prompts_paused_until(session: Session, settings: Settings) -> datetime | None:
    value = get_state(session, PAUSE_KEY)
    if not value:
        return None
    try:
        return ensure_timezone(datetime.fromisoformat(value), settings)
    except ValueError:
        return None


def is_active_time(now: datetime, settings: Settings) -> bool:
    local = ensure_timezone(now, settings)
    return settings.active_start <= local.time() <= settings.active_end


def mark_missed_prompts(session: Session, settings: Settings, now: datetime) -> int:
    cutoff = now - timedelta(minutes=settings.checkin_grace_minutes)
    prompts = session.scalars(
        select(CheckinPrompt)
        .where(CheckinPrompt.status.in_(["pending", "sent"]), CheckinPrompt.scheduled_for_end < cutoff)
    ).all()
    for prompt in prompts:
        prompt.status = "missed"
    session.flush()
    return len(prompts)


def send_checkin_prompt_job(session: Session, settings: Settings, now: datetime | None = None) -> CheckinPrompt | None:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    mark_missed_prompts(session, settings, now)
    paused_until = prompts_paused_until(session, settings)
    if paused_until and paused_until > now:
        return None
    if settings.quiet_mode:
        return None
    if not is_active_time(now, settings):
        return None

    interval_start, interval_end = interval_for_now(now, settings.checkin_interval_minutes)
    if interval_end == now.replace(second=0, microsecond=0):
        prompt_start, prompt_end = interval_start, interval_end
    else:
        prompt_end = floor_to_interval(now, settings.checkin_interval_minutes)
        prompt_start = prompt_end - timedelta(minutes=settings.checkin_interval_minutes)

    existing = session.scalar(
        select(CheckinPrompt).where(
            CheckinPrompt.scheduled_for_start == prompt_start,
            CheckinPrompt.scheduled_for_end == prompt_end,
        )
    )
    if existing and existing.status in {"sent", "answered", "skipped"}:
        return existing
    if existing is None:
        existing = CheckinPrompt(
            scheduled_for_start=prompt_start,
            scheduled_for_end=prompt_end,
            status="pending",
        )
        session.add(existing)
        session.flush()

    prompt_text = (
        "Time check: what did you do from "
        f"{prompt_start.strftime('%H:%M')}-{prompt_end.strftime('%H:%M')}? "
        "Reply naturally."
    )
    outbound = send_sms(session, prompt_text, settings=settings)
    existing.sent_at = utcnow()
    existing.status = "sent"
    existing.sms_message_sid = outbound.provider_message_id
    session.flush()
    return existing


def send_due_reminders_job(session: Session, settings: Settings, now: datetime | None = None) -> list[Reminder]:
    now = ensure_timezone(now or datetime.now(settings.timezone), settings)
    sent: list[Reminder] = []
    for reminder in due_reminders(session, now):
        body = f"Reminder: {reminder.title}. Reply done, snooze 30m, or cancel."
        send_sms(session, body, settings=settings)
        mark_reminder_sent(session, reminder, sent_at=now)
        sent.append(reminder)
    session.flush()
    return sent


def generate_daily_report_job(session: Session, settings: Settings, now: datetime | None = None) -> None:
    report = generate_report(session, "daily", settings=settings, now=now)
    if settings.send_reports_by_sms:
        send_sms(session, report.summary_text, settings=settings)
    session.flush()


def backup_database_job(session: Session, settings: Settings, now: datetime | None = None) -> None:
    if not settings.backup_enabled:
        return
    try:
        create_backup(settings=settings, now=ensure_timezone(now or datetime.now(settings.timezone), settings))
    except BackupError:
        return
    session.flush()


def start_scheduler(session_factory: sessionmaker[Session], settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.app_timezone)

    def with_session(func):
        def runner():
            with session_factory() as session:
                func(session, settings)
                session.commit()

        return runner

    scheduler.add_job(
        with_session(send_checkin_prompt_job),
        "interval",
        minutes=1,
        id="checkin_prompt",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        with_session(send_due_reminders_job),
        "interval",
        minutes=1,
        id="due_reminders",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        with_session(generate_daily_report_job),
        "cron",
        hour=settings.daily_report_clock.hour,
        minute=settings.daily_report_clock.minute,
        id="daily_report",
        replace_existing=True,
    )
    if settings.backup_enabled:
        scheduler.add_job(
            with_session(backup_database_job),
            "cron",
            hour=settings.backup_clock.hour,
            minute=settings.backup_clock.minute,
            id="database_backup",
            replace_existing=True,
        )
    scheduler.start()
    return scheduler


def pause_status_text(session: Session, settings: Settings) -> str:
    if settings.quiet_mode:
        return "Quiet mode is on."
    paused_until = prompts_paused_until(session, settings)
    if paused_until and paused_until > datetime.now(settings.timezone):
        return "Paused until " + human_dt(paused_until, settings)
    return "Prompts are active."
