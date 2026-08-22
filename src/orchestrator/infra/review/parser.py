"""Validate the structured review response shared by agent providers."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.domain import Context, ReviewCheck, ReviewFinding, ReviewOutcome


def extract_review_json(output: str) -> dict[str, Any]:
    """Extract the last review JSON object from mixed agent output."""
    decoder = json.JSONDecoder()
    value: dict[str, Any] | None = None
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "verdict" in candidate:
            value = candidate
    if value is None:
        raise ValueError("review output does not contain a JSON object")
    return value


def parse_review_output(output: str, context: Context) -> ReviewOutcome:
    """Parse and validate an agent's structured review response."""
    try:
        value = extract_review_json(output)
        verdict = str(value.get("verdict", "")).lower()
        if verdict not in {"approve", "request_changes", "comment"}:
            raise ValueError("review verdict is invalid")
        findings = value.get("findings", [])
        checks = value.get("checks", [])
        if not isinstance(findings, list) or not isinstance(checks, list):
            raise ValueError("findings and checks must be arrays")
        if not isinstance(value.get("summary", ""), str):
            raise ValueError("summary must be a string")
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("message"), str):
                raise ValueError("each finding must have a message")
            if "path" in finding and not isinstance(finding["path"], str):
                raise ValueError("finding path must be a string")
            if "line" in finding and (not isinstance(finding["line"], int) or isinstance(finding["line"], bool)):
                raise ValueError("finding line must be an integer")
            if "start_line" in finding and (not isinstance(finding["start_line"], int) or isinstance(finding["start_line"], bool)):
                raise ValueError("finding start_line must be an integer")
            if finding.get("side", "RIGHT") not in {"LEFT", "RIGHT"}:
                raise ValueError("finding side is invalid")
            if finding.get("start_side", finding.get("side", "RIGHT")) not in {"LEFT", "RIGHT"}:
                raise ValueError("finding start_side is invalid")
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("name"), str) or check.get("status") not in {"pass", "fail", "skip"}:
                raise ValueError("each check must have a name and valid status")
        typed_findings = tuple(ReviewFinding(**finding) for finding in findings)
        typed_checks = tuple(ReviewCheck(**check) for check in checks)
        return ReviewOutcome(
            True,
            verdict=verdict,
            summary=str(value.get("summary", "")),
            findings=typed_findings,
            checks=typed_checks,
            context=context,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return ReviewOutcome(False, summary=f"invalid structured review output: {exc}", context=context)
