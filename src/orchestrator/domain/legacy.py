"""Read-only adapters for checkpoints created before canonical domain contracts."""

from __future__ import annotations

from typing import Any, Mapping

from orchestrator.domain.context import Context
from orchestrator.domain.work import WorkItem


def work_item_from_legacy_state(state: Mapping[str, Any]) -> WorkItem:
    value = state.get("input") or {}
    data = value.get("data") or {}
    repository = data.get("repository", state.get("repository", ""))
    number = data.get("number", data.get("issue_number", state.get("issue_number")))
    task_id = data.get("id") or data.get("work_item_id") or state.get("task_id")
    if not task_id and number is not None:
        task_id = f"{repository}#{number}"
    if not repository and task_id and "#" in str(task_id):
        repository = str(task_id).rsplit("#", 1)[0]
    provider_state = value.get("provider_state") or state.get("provider_state") or {}
    context_data: dict[str, dict[str, Any]] = {}
    if number is not None:
        context_data["github"] = {"issue_number": number}
    if provider_state:
        context_data["git"] = {
            key: provider_state[key]
            for key in ("repository_url", "base_branch", "branch", "workspace")
            if provider_state.get(key) is not None
        }
    root_git = {
        "repository_url": state.get("repository_url"),
        "base_branch": state.get("base_branch"),
        "branch": state.get("branch"),
        "workspace": state.get("workspace_path") or (
            state.get("workspace") if isinstance(state.get("workspace"), str) else None
        ),
        "repository": repository,
    }
    if any(value is not None for value in root_git.values()):
        context_data["git"] = {
            **context_data.get("git", {}),
            **{key: value for key, value in root_git.items() if value is not None},
        }
        context_data["github"] = {
            **context_data.get("github", {}),
            **{
                key: provider_state[key]
                for key in ("pr_number", "comment_id", "author_login", "changed_lines")
                if provider_state.get(key) is not None
            },
        }
    return WorkItem(
        str(task_id), repository,
        data.get("title", data.get("issue_title", state.get("issue_title", ""))),
        data.get("body", data.get("issue_body", state.get("issue_body", ""))),
        tuple(data.get("extra_context", state.get("extra_context", []))),
        value.get("provider", "github"), Context(context_data),
    )
