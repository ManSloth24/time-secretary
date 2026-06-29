from __future__ import annotations

import json
from typing import Any


PROMPT_VERSION = "report-v1"

REPORT_JSON_SCHEMA = {
    "summary": "string, 2-5 sentences grounded only in the fact pack",
    "priorities": ["short action or priority strings"],
    "risks": ["risk or unresolved issue strings"],
    "questions": ["meeting question strings"],
    "next_actions": ["next action strings"],
    "narrative_markdown": "markdown section with concise headings and bullets",
}


def _compact_json(value: dict[str, Any], max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80] + '...,"truncated":true}'


def build_report_prompt(
    fact_pack: dict[str, Any],
    *,
    max_input_chars: int,
    structured_output: bool,
) -> str:
    fact_pack_json = _compact_json(fact_pack, max_input_chars)
    output_contract = (
        "Return one valid JSON object matching this schema: "
        + json.dumps(REPORT_JSON_SCHEMA, ensure_ascii=True)
        if structured_output
        else "Return concise Markdown only."
    )
    return f"""You are the local-only report writer inside Time Secretary.

Rules:
- Use only facts present in FACT_PACK.
- Do not invent projects, people, dates, counts, totals, metrics, risks, or action items.
- If evidence is missing, say what is missing instead of guessing.
- Keep sensitive details out unless FACT_PACK.sensitive_policy.contains_sensitive_text is true.
- Never include phone numbers, tokens, URLs containing secrets, raw webhook payloads, or authentication material.
- Prefer short, operational language for a working secretary briefing.
- The output may synthesize and prioritize, but every claim must be traceable to FACT_PACK.

{output_contract}

FACT_PACK:
{fact_pack_json}
"""


def render_structured_report(value: dict[str, Any]) -> str:
    parts: list[str] = []
    summary = str(value.get("summary") or "").strip()
    if summary:
        parts.extend(["### Summary", "", summary, ""])
    for key, title in [
        ("priorities", "Priorities"),
        ("risks", "Risks"),
        ("questions", "Questions"),
        ("next_actions", "Next Actions"),
    ]:
        items = value.get(key) or []
        if isinstance(items, list) and items:
            parts.extend([f"### {title}", ""])
            parts.extend(f"- {str(item).strip()}" for item in items if str(item).strip())
            parts.append("")
    narrative = str(value.get("narrative_markdown") or "").strip()
    if narrative:
        parts.extend(["### Narrative", "", narrative, ""])
    return "\n".join(parts).strip()
