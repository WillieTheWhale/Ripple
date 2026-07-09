"""COVERS edge derivation for RIPPLE ingestion."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from axon.core.graph.graph import KnowledgeGraph
from axon.core.graph.model import RelType

from ripple.ingest.mapping import MappedGraph


def add_static_covers(
    axon_graph: KnowledgeGraph,
    mapped_graph: MappedGraph,
    max_depth: int = 3,
) -> int:
    """Add static ``COVERS`` edges by following test CALLS up to *max_depth*."""
    test_pairs = [
        (axon_id, uid)
        for axon_id, uid in mapped_graph.axon_to_uid.items()
        if uid in mapped_graph.nodes and "Test" in mapped_graph.nodes[uid].labels
    ]
    before = len(mapped_graph.relationships_of_type("COVERS"))

    for test_axon_id, test_uid in test_pairs:
        seen_depth: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(test_axon_id, 0)])

        while queue:
            current_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            for rel in axon_graph.get_outgoing(current_id, RelType.CALLS):
                next_depth = depth + 1
                if seen_depth.get(rel.target, max_depth + 1) <= next_depth:
                    continue
                seen_depth[rel.target] = next_depth
                queue.append((rel.target, next_depth))

                target_uid = mapped_graph.axon_to_uid.get(rel.target)
                if not target_uid:
                    continue
                target_node = mapped_graph.nodes.get(target_uid)
                if not target_node or "Function" not in target_node.labels or "Test" in target_node.labels:
                    continue
                mapped_graph.add_relationship(
                    "COVERS",
                    test_uid,
                    target_uid,
                    "Test",
                    "Function",
                    {"depth": next_depth},
                )

    after = len(mapped_graph.relationships_of_type("COVERS"))
    return after - before


def add_dynamic_covers(
    repo_path: Path,
    mapped_graph: MappedGraph,
) -> int:
    """Dynamic COVERS placeholder for Phase 4."""
    raise NotImplementedError(
        "Dynamic COVERS is planned for Phase 4: it will run "
        "`coverage run -m pytest` with dynamic_context inside a sandbox."
    )

