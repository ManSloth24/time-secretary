from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.I).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return None


def parse_json_with_repair(text: str) -> tuple[dict[str, Any] | None, str | None]:
    candidate = extract_json_object(text)
    if candidate is None:
        return None, "No JSON object found"
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as first_error:
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError:
            return None, str(first_error)
    if not isinstance(value, dict):
        return None, "JSON root is not an object"
    return value, None
