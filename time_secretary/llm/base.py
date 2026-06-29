from __future__ import annotations

from abc import ABC, abstractmethod

from .schemas import ProjectSummaryContext, ReportSummaryContext, SmsParseContext, SmsParseResult


class LLMProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse_sms_to_intents(self, context: SmsParseContext) -> SmsParseResult:
        raise NotImplementedError

    def summarize_day(self, context: ReportSummaryContext) -> str:
        return ""

    def summarize_week(self, context: ReportSummaryContext) -> str:
        return ""

    def summarize_month(self, context: ReportSummaryContext) -> str:
        return ""

    def summarize_project(self, context: ProjectSummaryContext) -> str:
        return ""
