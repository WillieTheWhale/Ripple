"""Risk analytics for RIPPLE ingestion."""

from __future__ import annotations

import logging

import igraph as ig
import leidenalg

from ripple.ingest.mapping import MappedGraph, MappedRelationship

logger = logging.getLogger(__name__)


def compute_risk(mapped_graph: MappedGraph) -> tuple[dict[str, float], dict[str, int]]:
    """Compute ``blast_score`` and ``community`` dictionaries keyed by Function fqn."""
    function_uids = {
        uid
        for uid, node in mapped_graph.nodes.items()
        if "Function" in node.labels
    }
    production_uids = {
        uid
        for uid in function_uids
        if not bool(mapped_graph.nodes[uid].properties.get("is_test", False))
    }
    fqn_by_uid = {
        uid: str(mapped_graph.nodes[uid].properties["fqn"])
        for uid in function_uids
    }
    scores = {fqn: 0.0 for fqn in fqn_by_uid.values()}
    communities = {fqn: -1 for fqn in fqn_by_uid.values()}

    calls = [
        rel
        for rel in mapped_graph.relationships
        if rel.rel_type == "CALLS"
        and rel.source_uid in function_uids
        and rel.target_uid in function_uids
    ]

    production_calls = [
        rel
        for rel in calls
        if rel.source_uid in production_uids and rel.target_uid in production_uids
    ]
    production_endpoint_uids = sorted(
        {rel.source_uid for rel in production_calls} | {rel.target_uid for rel in production_calls}
    )
    if production_endpoint_uids and production_calls:
        uid_to_index = {uid: idx for idx, uid in enumerate(production_endpoint_uids)}
        ig_graph, weights = _build_call_graph(production_calls, uid_to_index)

        ranks = ig_graph.pagerank(directed=True, damping=0.85, weights=weights)
        min_rank = min(ranks)
        max_rank = max(ranks)
        for uid, idx in uid_to_index.items():
            fqn = fqn_by_uid[uid]
            if max_rank == min_rank:
                scores[fqn] = 1.0
            else:
                scores[fqn] = round((ranks[idx] - min_rank) / (max_rank - min_rank), 4)

    endpoint_uids = sorted({rel.source_uid for rel in calls} | {rel.target_uid for rel in calls})
    if not endpoint_uids or not calls:
        return scores, communities

    uid_to_index = {uid: idx for idx, uid in enumerate(endpoint_uids)}
    ig_graph, weights = _build_call_graph(calls, uid_to_index)

    if ig_graph.ecount() > 0 and ig_graph.vcount() >= 2:
        undirected = ig_graph.as_undirected(combine_edges={"weight": "sum"})
        community_weights = (
            undirected.es["weight"]
            if undirected.ecount() > 0 and "weight" in undirected.es.attributes()
            else None
        )
        try:
            partition = leidenalg.find_partition(
                undirected,
                leidenalg.ModularityVertexPartition,
                weights=community_weights,
            )
        except Exception:
            logger.warning("Leiden community detection failed", exc_info=True)
        else:
            for idx, community in enumerate(partition.membership):
                communities[fqn_by_uid[endpoint_uids[idx]]] = int(community)

    return scores, communities


def apply_risk(mapped_graph: MappedGraph) -> tuple[dict[str, float], dict[str, int]]:
    """Compute risk analytics and write them onto mapped Function nodes."""
    scores, communities = compute_risk(mapped_graph)
    for node in mapped_graph.nodes.values():
        if "Function" not in node.labels:
            continue
        fqn = str(node.properties["fqn"])
        node.properties["blast_score"] = scores.get(fqn, 0.0)
        node.properties["community"] = communities.get(fqn, -1)
    return scores, communities


def _build_call_graph(
    calls: list[MappedRelationship],
    uid_to_index: dict[str, int],
) -> tuple[ig.Graph, list[float]]:
    graph = ig.Graph(directed=True)
    graph.add_vertices(len(uid_to_index))

    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for rel in calls:
        # Axon/RIPPLE CALLS edges point caller -> callee. PageRank rewards
        # incoming references, which is the intended blast-score signal.
        edges.append((uid_to_index[rel.source_uid], uid_to_index[rel.target_uid]))
        weights.append(float(rel.properties.get("count", 1) or 1))

    graph.add_edges(edges)
    if weights:
        graph.es["weight"] = weights
    return graph, weights
