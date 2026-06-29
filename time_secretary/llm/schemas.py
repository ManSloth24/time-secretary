from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


IntentType = Literal[
    "time_entry",
    "todo",
    "reminder",
    "project_note",
    "project_update",
    "decision",
    "idea",
    "follow_up",
    "question_to_revisit",
    "meeting_action_item",
    "unknown",
]

PrimaryCategory = Literal["Work", "Home", "Unknown"]
Priority = Literal["low", "normal", "high", "urgent"]


class SmsIntent(BaseModel):
    type: IntentType
    title: str = ""
    body: str = ""
    category_primary: PrimaryCategory = "Unknown"
    category_secondary: str | None = None
    project_name: str | None = None
    due_at_text: str | None = None
    remind_at_text: str | None = None
    next_review_at_text: str | None = None
    priority: Priority = "normal"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("title", "body", mode="before")
    @classmethod
    def coerce_text(cls, value):
        return "" if value is None else str(value)


class SmsParseResult(BaseModel):
    intents: list[SmsIntent] = Field(default_factory=list)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = None
    success: bool = True
    error_message: str | None = None


@dataclass(frozen=True)
class ProjectAliasContext:
    name: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SmsParseContext:
    raw_text: str
    now: datetime
    projects: list[ProjectAliasContext] = field(default_factory=list)
    recent_prompt: str | None = None
    recent_reminders: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportSummaryContext:
    report_type: str
    markdown_excerpt: str
    now: datetime


@dataclass(frozen=True)
class ProjectSummaryContext:
    project_name: str
    source_excerpt: str
    now: datetime
