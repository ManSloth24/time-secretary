from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .llm.json_repair import parse_json_with_repair
from .llm.report_prompts import PROMPT_VERSION, build_report_prompt, render_structured_report
from .llm_report_validator import validate_llm_report
from .models import LLMCall, LLMReportCache
from .utils import utcnow


@dataclass(frozen=True)
class LLMReportResult:
    mode: str
    final_markdown: str
    narrative_markdown: str | None
    fact_pack: dict[str, Any]
    cache_key: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    validation_warnings: list[str] | None = None
    success: bool = False
    error_message: str | None = None
    from_cache: bool = False


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def fact_pack_hash(fact_pack: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(fact_pack).encode("utf-8")).hexdigest()


def build_cache_key(
    fact_pack: dict[str, Any],
    *,
    model: str,
    task_type: str,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    source = "|".join([task_type, model, prompt_version, fact_pack_hash(fact_pack)])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _deterministic_result(
    deterministic_markdown: str,
    fact_pack: dict[str, Any],
    *,
    error_message: str | None = None,
    cache_key: str | None = None,
    warnings: list[str] | None = None,
) -> LLMReportResult:
    return LLMReportResult(
        mode="deterministic",
        final_markdown=deterministic_markdown,
        narrative_markdown=None,
        fact_pack=fact_pack,
        cache_key=cache_key,
        validation_warnings=warnings or [],
        success=False,
        error_message=error_message,
    )


def _compose_final_markdown(deterministic_markdown: str, narrative_markdown: str) -> str:
    narrative_block = "\n".join(
        [
            "## Local LLM Narrative",
            "",
            narrative_markdown.strip(),
            "",
        ]
    )
    lines = deterministic_markdown.rstrip().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## Executive Summary"):
            return "\n".join([*lines[:index], narrative_block, *lines[index:]]).rstrip() + "\n"
    return deterministic_markdown.rstrip() + "\n\n" + narrative_block


def _ollama_tags_url(settings: Settings) -> str:
    return settings.llm_base_url.rstrip("/") + "/api/tags"


def _ollama_generate_url(settings: Settings) -> str:
    return settings.llm_base_url.rstrip("/") + "/api/generate"


def ollama_report_available(settings: Settings) -> bool:
    try:
        with urllib.request.urlopen(
            _ollama_tags_url(settings),
            timeout=min(settings.llm_report_timeout_seconds, 5),
        ) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def installed_ollama_models(settings: Settings) -> list[str]:
    try:
        with urllib.request.urlopen(
            _ollama_tags_url(settings),
            timeout=min(settings.llm_report_timeout_seconds, 5),
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return []
    return [str(item.get("name") or "") for item in body.get("models", []) if item.get("name")]


def _call_ollama_report(settings: Settings, prompt: str) -> tuple[str, int, int | None]:
    if not settings.llm_report_model:
        raise RuntimeError("LLM_REPORT_MODEL is not configured")
    payload = {
        "model": settings.llm_report_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": settings.llm_report_temperature,
            "num_predict": settings.llm_report_max_output_tokens,
        },
    }
    if settings.llm_report_use_structured_output:
        payload["format"] = "json"
    request = urllib.request.Request(
        _ollama_generate_url(settings),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=settings.llm_report_timeout_seconds) as response:
        body = response.read().decode("utf-8")
    duration_ms = int((time.perf_counter() - started) * 1000)
    outer = json.loads(body)
    return str(outer.get("response", "")), duration_ms, outer.get("eval_count")


def _record_llm_report_call(
    session: Session,
    *,
    settings: Settings,
    task_type: str,
    prompt: str,
    raw_response: str | None,
    parsed_json: str | None,
    success: bool,
    error_message: str | None,
    duration_ms: int | None,
) -> LLMCall:
    call = LLMCall(
        provider=settings.llm_report_provider,
        model=settings.llm_report_model or None,
        task_type=f"llm_report:{task_type}",
        input_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_excerpt=prompt[:500],
        raw_response=raw_response if settings.llm_save_raw_responses else None,
        parsed_json=parsed_json if success else None,
        success=success,
        error_message=error_message,
        duration_ms=duration_ms,
    )
    session.add(call)
    session.flush()
    return call


def _upsert_cache(
    session: Session,
    *,
    cache_key: str,
    task_type: str,
    settings: Settings,
    fact_pack: dict[str, Any],
    prompt: str,
    narrative: str | None,
    structured_json: str | None,
    validation_status: str,
    validation_warnings: list[str],
    duration_ms: int | None,
    token_count: int | None,
    success: bool,
    error_message: str | None,
) -> LLMReportCache:
    row = session.scalar(select(LLMReportCache).where(LLMReportCache.cache_key == cache_key))
    now = utcnow()
    if row is None:
        row = LLMReportCache(cache_key=cache_key)
        session.add(row)
        row.created_at = now
    row.task_type = task_type
    row.provider = settings.llm_report_provider
    row.model = settings.llm_report_model or None
    row.prompt_version = PROMPT_VERSION
    row.fact_pack_hash = fact_pack_hash(fact_pack)
    row.fact_pack_json = _canonical_json(fact_pack)
    row.prompt_excerpt = prompt[:500]
    row.narrative_text = narrative
    row.structured_json = structured_json
    row.validation_status = validation_status
    row.validation_warnings_json = json.dumps(validation_warnings)
    row.duration_ms = duration_ms
    row.token_count = token_count
    row.success = success
    row.error_message = error_message
    row.updated_at = now
    session.flush()
    return row


def _sensitive_content_allowed(settings: Settings, fact_pack: dict[str, Any]) -> bool:
    policy = fact_pack.get("sensitive_policy") or {}
    if not policy.get("contains_sensitive_text"):
        return True
    return bool(
        settings.llm_enabled
        and settings.llm_allow_work_notes
        and settings.secure_capture_allow_llm
        and settings.include_sensitive_local_reports
    )


def generate_llm_report(
    session: Session,
    *,
    fact_pack: dict[str, Any],
    deterministic_markdown: str,
    settings: Settings,
    task_type: str = "briefing",
    sms_safe: bool = False,
) -> LLMReportResult:
    model = settings.llm_report_model or ""
    cache_key = build_cache_key(fact_pack, model=model, task_type=task_type)

    if not settings.llm_reports_enabled:
        return _deterministic_result(
            deterministic_markdown,
            fact_pack,
            cache_key=cache_key,
            error_message="LLM reports are disabled.",
        )

    if settings.llm_report_provider not in {"ollama", "none", "no_llm"}:
        return _deterministic_result(
            deterministic_markdown,
            fact_pack,
            cache_key=cache_key,
            error_message="Only local Ollama report generation is allowed.",
        )

    if settings.llm_report_provider in {"none", "no_llm"}:
        return _deterministic_result(
            deterministic_markdown,
            fact_pack,
            cache_key=cache_key,
            error_message="LLM report provider is disabled.",
        )

    if not _sensitive_content_allowed(settings, fact_pack):
        warnings = ["Sensitive fact pack text is blocked from local LLM by current safety gates."]
        return _deterministic_result(
            deterministic_markdown,
            fact_pack,
            cache_key=cache_key,
            error_message=warnings[0],
            warnings=warnings,
        )

    if settings.llm_report_cache_enabled:
        cached = session.scalar(
            select(LLMReportCache).where(
                LLMReportCache.cache_key == cache_key,
                LLMReportCache.success.is_(True),
            )
        )
        if cached and cached.narrative_text:
            cached.last_used_at = utcnow()
            final = _compose_final_markdown(deterministic_markdown, cached.narrative_text)
            warnings = []
            if cached.validation_warnings_json:
                try:
                    warnings = list(json.loads(cached.validation_warnings_json))
                except json.JSONDecodeError:
                    warnings = []
            return LLMReportResult(
                mode="llm_assisted",
                final_markdown=final,
                narrative_markdown=cached.narrative_text,
                fact_pack=fact_pack,
                cache_key=cache_key,
                model=cached.model,
                duration_ms=cached.duration_ms,
                validation_warnings=warnings,
                success=True,
                from_cache=True,
            )

    prompt = build_report_prompt(
        fact_pack,
        max_input_chars=settings.llm_report_max_input_chars,
        structured_output=settings.llm_report_use_structured_output,
    )

    if not ollama_report_available(settings):
        error = "Ollama is unavailable for local report generation."
        _record_llm_report_call(
            session,
            settings=settings,
            task_type=task_type,
            prompt=prompt,
            raw_response=None,
            parsed_json=None,
            success=False,
            error_message=error,
            duration_ms=None,
        )
        return _deterministic_result(
            deterministic_markdown,
            fact_pack,
            cache_key=cache_key,
            error_message=error,
        )

    raw_response: str | None = None
    structured_json: str | None = None
    duration_ms: int | None = None
    token_count: int | None = None
    try:
        raw_response, duration_ms, token_count = _call_ollama_report(settings, prompt)
        narrative = raw_response.strip()
        if settings.llm_report_use_structured_output:
            parsed, parse_error = parse_json_with_repair(raw_response)
            if parse_error or not isinstance(parsed, dict):
                raise RuntimeError(parse_error or "LLM returned invalid structured output")
            structured_json = json.dumps(parsed, ensure_ascii=True, sort_keys=True)
            narrative = render_structured_report(parsed)
        validation = (
            validate_llm_report(narrative, fact_pack, sms_safe=sms_safe)
            if settings.llm_report_validate_claims
            else None
        )
        warnings = validation.warnings if validation else []
        accepted_text = validation.sanitized_text if validation else narrative
        if validation and not validation.ok:
            error = "LLM report failed claim validation."
            _record_llm_report_call(
                session,
                settings=settings,
                task_type=task_type,
                prompt=prompt,
                raw_response=raw_response,
                parsed_json=structured_json,
                success=False,
                error_message=error + " " + "; ".join(warnings),
                duration_ms=duration_ms,
            )
            if settings.llm_report_cache_enabled:
                _upsert_cache(
                    session,
                    cache_key=cache_key,
                    task_type=task_type,
                    settings=settings,
                    fact_pack=fact_pack,
                    prompt=prompt,
                    narrative=accepted_text,
                    structured_json=structured_json,
                    validation_status="failed",
                    validation_warnings=warnings,
                    duration_ms=duration_ms,
                    token_count=token_count,
                    success=False,
                    error_message=error,
                )
            return _deterministic_result(
                deterministic_markdown,
                fact_pack,
                cache_key=cache_key,
                error_message=error,
                warnings=warnings,
            )

        final = _compose_final_markdown(deterministic_markdown, accepted_text)
        _record_llm_report_call(
            session,
            settings=settings,
            task_type=task_type,
            prompt=prompt,
            raw_response=raw_response,
            parsed_json=structured_json,
            success=True,
            error_message=None,
            duration_ms=duration_ms,
        )
        if settings.llm_report_cache_enabled:
            _upsert_cache(
                session,
                cache_key=cache_key,
                task_type=task_type,
                settings=settings,
                fact_pack=fact_pack,
                prompt=prompt,
                narrative=accepted_text,
                structured_json=structured_json,
                validation_status="passed" if settings.llm_report_validate_claims else "skipped",
                validation_warnings=warnings,
                duration_ms=duration_ms,
                token_count=token_count,
                success=True,
                error_message=None,
            )
        return LLMReportResult(
            mode="llm_assisted",
            final_markdown=final,
            narrative_markdown=accepted_text,
            fact_pack=fact_pack,
            cache_key=cache_key,
            model=model,
            duration_ms=duration_ms,
            validation_warnings=warnings,
            success=True,
        )
    except Exception as exc:
        error = str(exc)
        _record_llm_report_call(
            session,
            settings=settings,
            task_type=task_type,
            prompt=prompt,
            raw_response=raw_response,
            parsed_json=structured_json,
            success=False,
            error_message=error,
            duration_ms=duration_ms,
        )
        if settings.llm_report_cache_enabled:
            _upsert_cache(
                session,
                cache_key=cache_key,
                task_type=task_type,
                settings=settings,
                fact_pack=fact_pack,
                prompt=prompt,
                narrative=None,
                structured_json=structured_json,
                validation_status="error",
                validation_warnings=[],
                duration_ms=duration_ms,
                token_count=token_count,
                success=False,
                error_message=error,
            )
        return _deterministic_result(
            deterministic_markdown,
            fact_pack,
            cache_key=cache_key,
            error_message=error,
        )


def llm_report_status(session: Session, settings: Settings) -> dict[str, Any]:
    last_call = session.scalar(
        select(LLMCall)
        .where(LLMCall.task_type.like("llm_report:%"))
        .order_by(LLMCall.created_at.desc())
        .limit(1)
    )
    avg_duration = session.scalar(
        select(func.avg(LLMCall.duration_ms)).where(
            LLMCall.task_type.like("llm_report:%"),
            LLMCall.success.is_(True),
            LLMCall.duration_ms.is_not(None),
        )
    )
    return {
        "enabled": settings.llm_reports_enabled,
        "provider": settings.llm_report_provider,
        "model": settings.llm_report_model,
        "ollama_available": ollama_report_available(settings),
        "installed_models": installed_ollama_models(settings),
        "last_generation_at": last_call.created_at if last_call else None,
        "last_generation_success": last_call.success if last_call else None,
        "avg_generation_ms": int(avg_duration) if avg_duration is not None else None,
        "last_failure_reason": last_call.error_message if last_call and not last_call.success else None,
    }


def run_llm_report_test(session: Session, settings: Settings) -> LLMReportResult:
    now = datetime.now(settings.timezone)
    fact_pack = {
        "schema_version": "fact-pack-v1",
        "report_kind": "llm_report_test",
        "generated_at": now.isoformat(),
        "scope": {
            "briefing_type": "test",
            "topic": "Local report engine",
            "window_start": now.isoformat(),
            "window_end": now.isoformat(),
            "source_request": "test llm report model",
        },
        "sensitive_policy": {
            "requested_include_sensitive": False,
            "contains_sensitive_sources": False,
            "contains_sensitive_text": False,
            "sensitive_text_withheld": False,
        },
        "record_counts": {"todos": 1, "notes": 1, "time_entries": 1},
        "time_totals": {"total_hours": 0.25, "by_project_hours": {"Local report engine": 0.25}},
        "todos": [{"id": 1, "title": "Confirm local model can summarize facts", "status": "open"}],
        "recent_notes": [{"id": 1, "note_type": "note", "title": "Test", "body": "Local model should mention only this test fact."}],
        "missing_data_warnings": [],
    }
    deterministic = (
        "# Briefing - Local report engine\n\n"
        "## Executive Summary\n\n"
        "- Local records found: 1 project note, 1 open action, 0 changes.\n"
    )
    return generate_llm_report(
        session,
        fact_pack=fact_pack,
        deterministic_markdown=deterministic,
        settings=settings,
        task_type="test",
        sms_safe=False,
    )
