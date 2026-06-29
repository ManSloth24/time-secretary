from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .base import LLMProvider
from .json_repair import parse_json_with_repair
from .prompts import build_sms_parse_prompt
from .schemas import ProjectSummaryContext, ReportSummaryContext, SmsParseContext, SmsParseResult
from ..config import Settings


class OllamaProvider(LLMProvider):
    provider_name = "ollama"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_duration_ms: int | None = None
        self.last_raw_response: str | None = None
        self.last_error: str | None = None

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(
                self.settings.llm_base_url.rstrip("/") + "/api/tags",
                timeout=min(self.settings.llm_timeout_seconds, 5),
            ) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _generate(self, prompt: str) -> str:
        if not self.settings.llm_model:
            raise RuntimeError("LLM_MODEL is not configured")
        url = self.settings.llm_base_url.rstrip("/") + "/api/generate"
        payload = json.dumps(
            {
                "model": self.settings.llm_model,
                "prompt": prompt,
                "stream": False,
                "format": "json" if self.settings.llm_require_json else None,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
            body = response.read().decode("utf-8")
        outer = json.loads(body)
        return str(outer.get("response", ""))

    def parse_sms_to_intents(self, context: SmsParseContext) -> SmsParseResult:
        prompt = build_sms_parse_prompt(context, self.settings.llm_max_input_chars)
        started = time.perf_counter()
        self.last_raw_response = None
        self.last_error = None
        try:
            raw = self._generate(prompt)
            self.last_raw_response = raw
            parsed, error = parse_json_with_repair(raw)
            if error or parsed is None:
                self.last_error = error or "Invalid JSON"
                return SmsParseResult(
                    intents=[],
                    overall_confidence=0.0,
                    success=False,
                    error_message=self.last_error,
                )
            return SmsParseResult.model_validate(parsed)
        except Exception as exc:  # Ollama is optional; all failures are structured.
            self.last_error = str(exc)
            return SmsParseResult(
                intents=[],
                overall_confidence=0.0,
                success=False,
                error_message=self.last_error,
            )
        finally:
            self.last_duration_ms = int((time.perf_counter() - started) * 1000)

    def summarize_day(self, context: ReportSummaryContext) -> str:
        return ""

    def summarize_week(self, context: ReportSummaryContext) -> str:
        return ""

    def summarize_month(self, context: ReportSummaryContext) -> str:
        return ""

    def summarize_project(self, context: ProjectSummaryContext) -> str:
        return ""
