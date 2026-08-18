"""Helpers for parsing machine-readable review output."""

from __future__ import annotations

import re


def findings(output: str) -> list[str]:
    """Extract bullet findings from the reviewer's FINDINGS section."""
    result: list[str] = []
    in_findings = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("FINDINGS:"):
            in_findings = True
            continue
        if re.match(r"^[A-Z_]+:\s*", line):
            if in_findings:
                break
            continue
        if in_findings and line.startswith("- "):
            result.append(line[2:].strip())
    return result
