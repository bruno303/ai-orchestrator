"""Graph executes entirely in memory."""

from orchestrator.graph import build_graph


def test_graph_compiles_without_a_checkpointer():
    assert build_graph(runtime=object()) is not None
