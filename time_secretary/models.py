from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .utils import utcnow


class CheckinPrompt(Base):
    __tablename__ = "checkin_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduled_for_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_for_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    sms_message_sid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    entries: Mapped[list["TimeEntry"]] = relationship(back_populates="prompt")


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("checkin_prompts.id"), nullable=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    category_primary: Mapped[str] = mapped_column(String(32), default="Unknown", index=True)
    category_secondary: Mapped[str] = mapped_column(String(64), default="unknown")
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    project_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    classification_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="sms")
    work_focus_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    value_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delegation_candidate: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    delegation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    staffing_signal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    staffing_signal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_change_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_metric_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    prompt: Mapped[CheckinPrompt | None] = relationship(back_populates="entries")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    aliases_json: Mapped[str] = mapped_column("aliases", Text, default="[]")
    category_default: Mapped[str] = mapped_column(String(32), default="Unknown")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def aliases(self) -> list[str]:
        try:
            value = json.loads(self.aliases_json or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value]

    @aliases.setter
    def aliases(self, value: list[str]) -> None:
        cleaned = []
        for item in value:
            alias = str(item).strip()
            if alias and alias.lower() not in {x.lower() for x in cleaned}:
                cleaned.append(alias)
        self.aliases_json = json.dumps(cleaned)


class ClassificationRule(Base):
    __tablename__ = "classification_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    pattern: Mapped[str] = mapped_column(Text)
    category_primary: Mapped[str] = mapped_column(String(32), default="Unknown")
    category_secondary: Mapped[str] = mapped_column(String(64), default="unknown")
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class SmsMessage(Base):
    __tablename__ = "sms_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    from_number_masked: Mapped[str] = mapped_column(String(32), default="unknown")
    to_number_masked: Mapped[str] = mapped_column(String(32), default="unknown")
    body: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReportRun(Base):
    __tablename__ = "report_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(32), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary_text: Mapped[str] = mapped_column(Text)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_by_sms: Mapped[bool] = mapped_column(Boolean, default=False)


class BriefingRequest(Base):
    __tablename__ = "briefing_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_text: Mapped[str] = mapped_column(Text)
    request_source: Mapped[str] = mapped_column(String(32), default="sms", index=True)
    topic: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    run_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    time_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    include_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_safe: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="requested", index=True)
    generated_briefing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    created_from_secure_capture_id: Mapped[int | None] = mapped_column(ForeignKey("secure_captures.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BriefingReport(Base):
    __tablename__ = "briefing_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opaque_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    briefing_type: Mapped[str] = mapped_column(String(64), default="custom", index=True)
    topic: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    run_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sms_summary: Mapped[str] = mapped_column(Text)
    markdown_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    includes_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    generation_mode: Mapped[str] = mapped_column(String(32), default="deterministic", index=True)
    fact_pack_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_narrative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_markdown_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_cache_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    llm_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    llm_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_validation_warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_dashboard_path: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    requested_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    requested_from_secure_capture_id: Mapped[int | None] = mapped_column(ForeignKey("secure_captures.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TodoItem(Base):
    __tablename__ = "todo_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    category_primary: Mapped[str] = mapped_column(String(32), default="Unknown", index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    snooze_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_followup: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    capture_status: Mapped[str] = mapped_column(String(32), default="captured", index=True)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    created_from_time_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("time_entries.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240), index=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recurrence_rule: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    snooze_count: Mapped[int] = mapped_column(Integer, default=0)
    capture_status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    related_todo_id: Mapped[int | None] = mapped_column(ForeignKey("todo_items.id"), nullable=True)
    related_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProjectNote(Base):
    __tablename__ = "project_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    note_type: Mapped[str] = mapped_column(String(32), default="note", index=True)
    title: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_followup: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    capture_status: Mapped[str] = mapped_column(String(32), default="captured", index=True)
    linked_todo_id: Mapped[int | None] = mapped_column(ForeignKey("todo_items.id"), nullable=True)
    linked_reminder_id: Mapped[int | None] = mapped_column(ForeignKey("reminders.id"), nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProjectStatusSnapshot(Base):
    __tablename__ = "project_status_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    status_summary: Mapped[str] = mapped_column(Text)
    open_todos_count: Mapped[int] = mapped_column(Integer, default=0)
    overdue_todos_count: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecretaryInboxItem(Base):
    __tablename__ = "secretary_inbox_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_text: Mapped[str] = mapped_column(Text)
    interpreted_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suggested_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    suggested_project_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    suggested_title: Mapped[str | None] = mapped_column(String(240), nullable=True)
    suggested_next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    suggested_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    sms_message_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_to_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    converted_to_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="normal", index=True)


class LocationPlace(Base):
    __tablename__ = "location_places"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(32), default="other", index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LocationEvent(Base):
    __tablename__ = "location_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="manual_sms", index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    place_id: Mapped[int | None] = mapped_column(ForeignKey("location_places.id"), nullable=True)
    place_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CurrentLocationState(Base):
    __tablename__ = "current_location_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_place_id: Mapped[int | None] = mapped_column(ForeignKey("location_places.id"), nullable=True)
    current_place_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    current_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_event_id: Mapped[int | None] = mapped_column(ForeignKey("location_events.id"), nullable=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkDaySummary(Base):
    __tablename__ = "work_day_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[datetime] = mapped_column(Date, index=True, unique=True)
    first_work_location_event_id: Mapped[int | None] = mapped_column(ForeignKey("location_events.id"), nullable=True)
    arrived_work_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_work_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_work_entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_work_entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worksite_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logged_work_minutes: Mapped[int] = mapped_column(Integer, default=0)
    home_minutes: Mapped[int] = mapped_column(Integer, default=0)
    unknown_minutes: Mapped[int] = mapped_column(Integer, default=0)
    lunch_break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    commute_to_work_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commute_from_work_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_arrival_event: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_leave_event: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[str] = mapped_column(String(32), default="low", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RunRecord(Base):
    __tablename__ = "run_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    run_name: Mapped[str] = mapped_column(String(160), index=True)
    run_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    equipment: Mapped[str | None] = mapped_column(String(160), nullable=True)
    material: Mapped[str | None] = mapped_column(String(160), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProcessChange(Base):
    __tablename__ = "process_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run_records.id"), nullable=True)
    change_type: Mapped[str] = mapped_column(String(64), default="other", index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    created_from_secure_capture_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProcessObservation(Base):
    __tablename__ = "process_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run_records.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    observation_text: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(32), default="info", index=True)
    created_from_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"), nullable=True)
    created_from_secure_capture_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunMetric(Base):
    __tablename__ = "run_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run_records.id"), nullable=True)
    metric_name: Mapped[str] = mapped_column(String(160), index=True)
    metric_value_text: Mapped[str] = mapped_column(String(240))
    metric_value_numeric: Mapped[float | None] = mapped_column(Float, nullable=True)
    metric_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkInsight(Base):
    __tablename__ = "work_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_type: Mapped[str] = mapped_column(String(32), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    insight_type: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text)
    supporting_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supporting_entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecureCapture(Base):
    __tablename__ = "secure_captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    capture_type: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    run_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(64), default="iphone_shortcut", index=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="sensitive", index=True)
    processed_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_project_note_id: Mapped[int | None] = mapped_column(ForeignKey("project_notes.id"), nullable=True)
    linked_run_id: Mapped[int | None] = mapped_column(ForeignKey("run_records.id"), nullable=True)
    linked_change_id: Mapped[int | None] = mapped_column(ForeignKey("process_changes.id"), nullable=True)
    linked_observation_id: Mapped[int | None] = mapped_column(ForeignKey("process_observations.id"), nullable=True)
    linked_metric_id: Mapped[int | None] = mapped_column(ForeignKey("run_metrics.id"), nullable=True)
    linked_todo_id: Mapped[int | None] = mapped_column(ForeignKey("todo_items.id"), nullable=True)
    linked_reminder_id: Mapped[int | None] = mapped_column(ForeignKey("reminders.id"), nullable=True)


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    input_hash: Mapped[str] = mapped_column(String(128), index=True)
    prompt_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMReportCache(Base):
    __tablename__ = "llm_report_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="ollama", index=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    prompt_version: Mapped[str] = mapped_column(String(64), default="report-v1", index=True)
    fact_pack_hash: Mapped[str] = mapped_column(String(128), index=True)
    fact_pack_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="not_run", index=True)
    validation_warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
