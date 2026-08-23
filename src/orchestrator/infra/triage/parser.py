"""Validate the structured triage response shared by agent providers."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.domain import Context, TriageOutcome


def extract_triage_json(output: str) -> dict[str, Any]:
    """Extract the last triage JSON object from mixed agent output."""
    decoder = json.JSONDecoder()
    value: dict[str, Any] | None = None
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "enough_context" in candidate:
            value = candidate
    if value is None:
        raise ValueError("triage output does not contain a JSON object")
    return value


def parse_triage_output(output: str, context: Context) -> TriageOutcome:
    try:
        value = extract_triage_json(output)
        enough_context = value.get("enough_context")
        confidence = value.get("confidence")
        summary = value.get("summary")
        missing_context = value.get("missing_context")
        if not isinstance(enough_context, bool):
            raise ValueError("enough_context must be a boolean")
        if not isinstance(confidence, str) or confidence.lower() not in {"low", "medium", "high"}:
            raise ValueError("confidence must be low, medium, or high")
        if not isinstance(summary, str):
            raise ValueError("summary must be a string")
        if not isinstance(missing_context, list) or any(not isinstance(item, str) for item in missing_context):
            raise ValueError("missing_context must be an array of strings")
        return TriageOutcome(True, enough_context, confidence.lower(), summary, tuple(missing_context), context)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return TriageOutcome(False, summary=f"invalid structured triage output: {exc}", context=context)
