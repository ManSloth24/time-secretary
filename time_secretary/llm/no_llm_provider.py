from __future__ import annotations

from .base import LLMProvider
from .schemas import SmsParseContext, SmsParseResult


class NoLLMProvider(LLMProvider):
    provider_name = "none"

    def is_available(self) -> bool:
        return True

    def parse_sms_to_intents(self, context: SmsParseContext) -> SmsParseResult:
        return SmsParseResult(
            intents=[],
            overall_confidence=0.0,
            success=False,
            error_message="LLM disabled",
        )
