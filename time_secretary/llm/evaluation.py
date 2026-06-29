from __future__ import annotations

from .schemas import SmsParseResult


def should_accept_llm_result(result: SmsParseResult, min_confidence: float = 0.55) -> bool:
    if not result.success or not result.intents:
        return False
    if result.overall_confidence < min_confidence:
        return False
    return all(intent.confidence >= min_confidence for intent in result.intents)
