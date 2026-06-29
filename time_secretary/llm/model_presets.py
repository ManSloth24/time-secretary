from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMReportModelPreset:
    key: str
    label: str
    model: str
    role: str
    expected_fit: str
    temperature: float = 0.2
    max_output_tokens: int = 1200


REPORT_MODEL_PRESETS: dict[str, LLMReportModelPreset] = {
    "none": LLMReportModelPreset(
        key="none",
        label="No LLM",
        model="",
        role="deterministic",
        expected_fit="Uses only deterministic fact packs and Markdown.",
    ),
    "fast": LLMReportModelPreset(
        key="fast",
        label="Fast local",
        model="qwen3:1.7b",
        role="quick narrative",
        expected_fit="Good for short summaries on small CPUs.",
        max_output_tokens=900,
    ),
    "balanced": LLMReportModelPreset(
        key="balanced",
        label="Balanced local",
        model="llama3.2:3b",
        role="default narrative",
        expected_fit="Default balance for an Intel N100 / 16 GB mini PC.",
    ),
    "reasoning": LLMReportModelPreset(
        key="reasoning",
        label="Reasoning local",
        model="phi4-mini",
        role="careful narrative",
        expected_fit="Useful for slower but more careful meeting-prep synthesis.",
        max_output_tokens=1400,
    ),
}


def preset_for_model(model: str) -> LLMReportModelPreset | None:
    model = (model or "").strip().lower()
    for preset in REPORT_MODEL_PRESETS.values():
        if preset.model.lower() == model:
            return preset
    return None
