"""Axon extraction wrapper."""

from __future__ import annotations

from pathlib import Path

from axon.core.graph.graph import KnowledgeGraph
from axon.core.ingestion.pipeline import run_pipeline


def extract(repo_path: Path) -> KnowledgeGraph:
    """Extract an Axon knowledge graph for *repo_path* without persistence or embeddings."""
    graph, _ = run_pipeline(repo_path, storage=None, embeddings=False)
    return graph

