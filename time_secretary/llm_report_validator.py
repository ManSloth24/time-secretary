from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


TOKEN_PATTERNS = [
    re.compile(r"\b(?:SECURE_CAPTURE_TOKEN|TWILIO_AUTH_TOKEN|AUTH_TOKEN|API_KEY)\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9_.\-]{12,}\b", re.I),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
]
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
HOURS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", re.I)
PROJECT_LIKE_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.-]*(?:Project|Process|Program)\b")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    warnings: list[str]
    sanitized_text: str


def _walk_values(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _known_project_names(fact_pack: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    scope = fact_pack.get("scope") or {}
    project = scope.get("project") or {}
    if isinstance(project, dict):
        if project.get("name"):
            names.add(str(project["name"]))
        for alias in project.get("aliases") or []:
            names.add(str(alias))
    for key, value in _walk_values(fact_pack):
        if key in {"project_name", "topic", "run_name"} and value:
            names.add(str(value))
        elif key in {"by_project_hours"} and isinstance(value, dict):
            names.update(str(item) for item in value.keys())
    return {name.lower() for name in names if name and name != "Unassigned"}


def _known_hour_values(fact_pack: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for key, value in _walk_values(fact_pack):
        if "hour" in str(key).lower():
            if isinstance(value, (int, float)):
                values.append(float(value))
            elif isinstance(value, dict):
                values.extend(float(item) for item in value.values() if isinstance(item, (int, float)))
    return values


def _has_matching_hour_claim(claim: float, known_values: list[float]) -> bool:
    if not known_values:
        return True
    return any(abs(claim - value) <= 0.15 for value in known_values)


def validate_llm_report(
    output_text: str,
    fact_pack: dict[str, Any],
    *,
    sms_safe: bool = False,
) -> ValidationResult:
    warnings: list[str] = []
    sanitized = output_text.strip()

    if not sanitized:
        warnings.append("LLM output was empty.")

    if PHONE_RE.search(sanitized):
        warnings.append("LLM output contained a phone-number-like value.")
        sanitized = PHONE_RE.sub("[redacted phone]", sanitized)

    for pattern in TOKEN_PATTERNS:
        if pattern.search(sanitized):
            warnings.append("LLM output contained token-like or secret-like text.")
            sanitized = pattern.sub("[redacted secret]", sanitized)

    known_projects = _known_project_names(fact_pack)
    for candidate in PROJECT_LIKE_RE.findall(sanitized):
        if candidate.lower() not in known_projects:
            warnings.append(f"LLM output referenced unknown project-like name: {candidate}.")

    known_hours = _known_hour_values(fact_pack)
    for match in HOURS_RE.finditer(sanitized):
        claimed = float(match.group(1))
        if not _has_matching_hour_claim(claimed, known_hours):
            warnings.append(f"LLM output made an unsupported hour claim: {claimed:g}h.")

    policy = fact_pack.get("sensitive_policy") or {}
    if sms_safe and policy.get("contains_sensitive_text"):
        warnings.append("SMS-safe report cannot include sensitive text.")

    if not policy.get("contains_sensitive_text"):
        if re.search(r"\b(secret|token|private configuration|private)\b", sanitized, re.I):
            warnings.append("LLM output may contain sensitive language while sensitive text is disabled.")

    return ValidationResult(ok=not warnings, warnings=warnings, sanitized_text=sanitized)
