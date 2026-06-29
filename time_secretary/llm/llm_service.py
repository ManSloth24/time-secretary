from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import LLMProvider
from .evaluation import should_accept_llm_result
from .llama_cpp_provider import LlamaCppProvider
from .no_llm_provider import NoLLMProvider
from .ollama_provider import OllamaProvider
from .prompts import build_sms_parse_prompt
from .schemas import ProjectAliasContext, SmsIntent, SmsParseContext, SmsParseResult
from ..config import Settings
from ..models import LLMCall, Project
from ..utils import mask_phone_number


PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def redact_for_llm(text: str, settings: Settings) -> str:
    if not settings.llm_redact_phone_numbers:
        return text
    return PHONE_RE.sub(lambda match: mask_phone_number(match.group(0)), text)


def get_llm_provider(settings: Settings) -> LLMProvider:
    if not settings.llm_enabled or settings.llm_provider == "none":
        return NoLLMProvider()
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings)
    if settings.llm_provider == "llama_cpp":
        return LlamaCppProvider()
    return NoLLMProvider()


def build_sms_context(
    session: Session,
    raw_text: str,
    *,
    settings: Settings,
    now: datetime,
    recent_prompt: str | None = None,
    recent_reminders: list[str] | None = None,
) -> SmsParseContext:
    projects = session.scalars(select(Project).where(Project.active.is_(True)).order_by(Project.name.asc())).all()
    return SmsParseContext(
        raw_text=redact_for_llm(raw_text, settings),
        now=now,
        projects=[ProjectAliasContext(name=project.name, aliases=project.aliases) for project in projects],
        recent_prompt=recent_prompt,
        recent_reminders=recent_reminders or [],
    )


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_llm_call(
    session: Session,
    *,
    settings: Settings,
    provider: LLMProvider,
    task_type: str,
    prompt: str,
    result: SmsParseResult,
    duration_ms: int | None = None,
    raw_response: str | None = None,
) -> LLMCall:
    call = LLMCall(
        provider=provider.provider_name,
        model=settings.llm_model or None,
        task_type=task_type,
        input_hash=_input_hash(prompt),
        prompt_excerpt=prompt[:500],
        raw_response=raw_response if settings.llm_save_raw_responses else None,
        parsed_json=result.model_dump_json() if result.success else None,
        success=result.success,
        error_message=result.error_message,
        duration_ms=duration_ms,
    )
    session.add(call)
    session.flush()
    return call


def parse_sms_with_llm(
    session: Session,
    raw_text: str,
    *,
    settings: Settings,
    now: datetime,
    recent_prompt: str | None = None,
    recent_reminders: list[str] | None = None,
) -> SmsParseResult:
    provider = get_llm_provider(settings)
    if isinstance(provider, NoLLMProvider):
        return provider.parse_sms_to_intents(
            build_sms_context(
                session,
                raw_text,
                settings=settings,
                now=now,
                recent_prompt=recent_prompt,
                recent_reminders=recent_reminders,
            )
        )
    context = build_sms_context(
        session,
        raw_text,
        settings=settings,
        now=now,
        recent_prompt=recent_prompt,
        recent_reminders=recent_reminders,
    )
    prompt = build_sms_parse_prompt(context, settings.llm_max_input_chars)
    result = provider.parse_sms_to_intents(context)
    record_llm_call(
        session,
        settings=settings,
        provider=provider,
        task_type="parse_sms_to_intents",
        prompt=prompt,
        result=result,
        duration_ms=getattr(provider, "last_duration_ms", None),
        raw_response=getattr(provider, "last_raw_response", None),
    )
    return result


def accepted_intents(result: SmsParseResult, min_confidence: float = 0.55) -> list[SmsIntent]:
    if not should_accept_llm_result(result, min_confidence=min_confidence):
        return []
    return result.intents
