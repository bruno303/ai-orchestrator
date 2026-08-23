"""Shared triage prompt and runtime helpers."""

TRIAGE_PROMPT = """Assess whether this GitHub issue contains enough context for another agent to implement it safely.

Issue ID: {task_id}
Repository: {repository}
Title: {title}

Description:
{description}

Return ONLY valid JSON with exactly this shape:
{{"enough_context":true,"confidence":"high|medium|low","summary":"...","missing_context":["..."]}}

Use confidence high only when the requested work and its expected behavior are clear enough to execute.
Set enough_context to true only when the issue is actionable without asking the author for more information.
When either condition is false, list the missing information in missing_context. Do not add labels or modify files.
"""
