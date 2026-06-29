from __future__ import annotations

from .base import LLMProvider
from .schemas import SmsParseContext, SmsParseResult


class LlamaCppProvider(LLMProvider):
    provider_name = "llama_cpp"

    def is_available(self) -> bool:
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            return False
        return False

    def parse_sms_to_intents(self, context: SmsParseContext) -> SmsParseResult:
        return SmsParseResult(
            intents=[],
            overall_confidence=0.0,
            success=False,
            error_message="llama.cpp provider is a future project taskal placeholder",
        )
