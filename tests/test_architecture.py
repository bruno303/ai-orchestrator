"""Regression checks for provider boundaries."""

from pathlib import Path


def test_generic_layers_do_not_reference_github_reactions_or_client():
    root = Path(__file__).parents[1] / "src" / "orchestrator"
    paths = [
        root / "application.py",
        root / "review.py",
        root / "graph.py",
        root / "git_workspace.py",
        root / "runtime",
    ]
    forbidden = (
        "from orchestrator import github", "github.", '"eyes"', '"rocket"',
        "ai-reviewed", "comment_id",
    )
    for path in paths:
        files = [path] if path.is_file() else sorted(path.glob("*.py"))
        text = "\n".join(file.read_text() for file in files)
        assert not any(value in text for value in forbidden), path
