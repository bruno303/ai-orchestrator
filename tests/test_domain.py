"""Canonical provider-neutral domain contract tests."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from orchestrator.domain import Context, ReviewTarget, WorkItem, ensure_task_id
from orchestrator import workspace


def test_context_merges_namespaces_without_erasing_unrelated_data():
    original = Context({"github": {"issue_number": 3}, "git": {"branch": "task"}})
    updated = original.merge_namespace("git", {"workspace": "/tmp/task"}).merged(
        Context({"opencode": {"session_id": "s1"}})
    )

    assert updated.to_dict() == {
        "github": {"issue_number": 3},
        "git": {"branch": "task", "workspace": "/tmp/task"},
        "opencode": {"session_id": "s1"},
    }
    assert original.to_dict() == {"github": {"issue_number": 3}, "git": {"branch": "task"}}


def test_context_is_serializable_and_namespace_views_are_read_only():
    context = Context({"source": {"ticket": "ABC-42"}})
    assert json.loads(json.dumps(context.to_dict())) == {"source": {"ticket": "ABC-42"}}
    with pytest.raises(TypeError):
        context.namespace("source")["ticket"] = "changed"
    with pytest.raises(TypeError, match="not JSON-serializable"):
        Context({"source": {"invalid": object()}})


@pytest.mark.parametrize("value", [None, ""])
def test_missing_task_id_is_generated_once_at_input_boundary(value):
    generated = ensure_task_id(value)
    assert str(UUID(generated)) == generated
    assert WorkItem(generated, "company/backend", "Task").id == generated


@pytest.mark.parametrize("value", ["ABC-42", "123"])
def test_provider_task_id_remains_an_opaque_string(value):
    assert ensure_task_id(f" {value} ") == value
    assert WorkItem(value, "company/backend", "Task").id == value


def test_review_target_accepts_non_github_identity():
    target = ReviewTarget("MR-abc", "company/backend", context=Context({"gitlab": {"iid": "abc"}}))
    assert target.id == "MR-abc"


def test_workspace_tokens_are_safe_deterministic_and_collision_resistant():
    assert workspace.safe_task_token("owner/repo#123") == "owner-repo-123"
    assert workspace.safe_task_token("ABC-42") == "ABC-42"
    assert workspace.safe_task_token("foo/bar") == workspace.safe_task_token("foo/bar")
    assert workspace.safe_task_token("foo/bar") != workspace.safe_task_token("foo-bar")
