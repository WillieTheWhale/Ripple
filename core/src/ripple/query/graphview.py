"""Graph-view read queries for the frontend canvas."""

from __future__ import annotations

from typing import Any

from ripple.db import read_records
from ripple.query.blast import _validate_positive_int

_EDGE_LIMIT = 8000


def graph_snapshot(driver: Any, repo_id: str, max_nodes: int = 2000) -> dict[str, list[dict[str, Any]]]:
    """Return a capped graph snapshot for the frontend canvas."""
    normalized_max_nodes = _validate_positive_int(max_nodes, "max_nodes")
    node_rows = read_records(
        driver,
        """
        MATCH (n {repo_id: $repo_id})
        WHERE n:Function OR n:Class OR n:File
        WITH n,
             CASE WHEN n:Function THEN 0 WHEN n:Class THEN 1 ELSE 2 END AS label_rank,
             coalesce(n.blast_score, 0.0) AS score
        ORDER BY label_rank ASC, score DESC, n.uid ASC
        LIMIT $max_nodes
        OPTIONAL MATCH (:Test {repo_id: $repo_id})-[:COVERS]->(n)
        WITH n, count(*) > 0 AS covered
        RETURN n.uid AS uid,
               coalesce(n.fqn, n.path, n.uid) AS fqn,
               coalesce(n.name, n.path, n.uid) AS name,
               CASE WHEN n:Function THEN 'Function'
                    WHEN n:Class THEN 'Class'
                    ELSE 'File'
               END AS label,
               coalesce(n.file_path, n.path, '') AS file_path,
               coalesce(n.blast_score, 0.0) AS blast_score,
               coalesce(n.community, -1) AS community,
               coalesce(n.is_test, false) AS is_test,
               covered
        ORDER BY label ASC, blast_score DESC, uid ASC
        """,
        {"repo_id": repo_id, "max_nodes": normalized_max_nodes},
    )
    included_uids = [str(row["uid"]) for row in node_rows]
    edge_rows: list[dict[str, Any]] = []
    if included_uids:
        edge_rows = read_records(
            driver,
            """
            MATCH (source {repo_id: $repo_id})-[rel:CALLS|IMPORTS|DEFINES|INHERITS]->(target {repo_id: $repo_id})
            WHERE source.uid IN $uids AND target.uid IN $uids
            WITH source, target, type(rel) AS rel_type,
                 CASE type(rel)
                   WHEN 'CALLS' THEN 0
                   WHEN 'DEFINES' THEN 1
                   WHEN 'IMPORTS' THEN 2
                   ELSE 3
                 END AS edge_rank
            ORDER BY edge_rank ASC, source.uid ASC, target.uid ASC
            LIMIT $edge_limit
            RETURN source.uid AS source_uid,
                   target.uid AS target_uid,
                   rel_type AS type
            """,
            {"repo_id": repo_id, "uids": included_uids, "edge_limit": _EDGE_LIMIT},
        )

    return {
        "nodes": [_normalize_node(row) for row in node_rows],
        "edges": [
            {
                "source_uid": str(row["source_uid"]),
                "target_uid": str(row["target_uid"]),
                "type": str(row["type"]),
            }
            for row in edge_rows
        ],
    }


def repo_stats(driver: Any, repo_id: str) -> dict[str, int | float]:
    """Return aggregate counts for a repository graph."""
    rows = read_records(
        driver,
        """
        MATCH (repo:Repo {repo_id: $repo_id})
        CALL () {
          MATCH (f:Function {repo_id: $repo_id})
          RETURN count(f) AS functions,
                 coalesce(avg(coalesce(f.blast_score, 0.0)), 0.0) AS avg_blast_score,
                 count(DISTINCT CASE
                   WHEN coalesce(f.community, -1) >= 0 THEN f.community
                 END) AS communities
        }
        CALL () {
          MATCH (c:Class {repo_id: $repo_id})
          RETURN count(c) AS classes
        }
        CALL () {
          MATCH (file:File {repo_id: $repo_id})
          RETURN count(file) AS files
        }
        CALL () {
          MATCH (t:Test {repo_id: $repo_id})
          RETURN count(t) AS tests
        }
        CALL () {
          MATCH (:Function {repo_id: $repo_id})-[call:CALLS]->(:Function {repo_id: $repo_id})
          RETURN count(call) AS calls
        }
        CALL () {
          MATCH (:Test {repo_id: $repo_id})-[cover:COVERS]->(:Function {repo_id: $repo_id})
          RETURN count(cover) AS covers
        }
        CALL () {
          MATCH (f:Function {repo_id: $repo_id})
          WHERE coalesce(f.is_test, false) = false
            AND NOT (:Test {repo_id: $repo_id})-[:COVERS]->(f)
          RETURN count(f) AS uncovered_functions
        }
        RETURN functions, classes, files, tests, calls, covers, uncovered_functions,
               avg_blast_score, communities
        """,
        {"repo_id": repo_id},
    )
    if not rows:
        return {
            "functions": 0,
            "classes": 0,
            "files": 0,
            "tests": 0,
            "calls": 0,
            "covers": 0,
            "uncovered_functions": 0,
            "avg_blast_score": 0.0,
            "communities": 0,
        }
    row = rows[0]
    return {
        "functions": int(row.get("functions") or 0),
        "classes": int(row.get("classes") or 0),
        "files": int(row.get("files") or 0),
        "tests": int(row.get("tests") or 0),
        "calls": int(row.get("calls") or 0),
        "covers": int(row.get("covers") or 0),
        "uncovered_functions": int(row.get("uncovered_functions") or 0),
        "avg_blast_score": float(row.get("avg_blast_score") or 0.0),
        "communities": int(row.get("communities") or 0),
    }


def ripple_paths(
    driver: Any,
    repo_id: str,
    fqn: str,
    max_hops: int = 4,
) -> list[dict[str, int | str]]:
    """Return CALLS edges that lie on shortest dependent-to-target paths."""
    normalized_hops = _validate_positive_int(max_hops, "max_hops")
    rows = read_records(
        driver,
        _build_ripple_paths_cypher(normalized_hops),
        {"repo_id": repo_id, "fqn": fqn},
    )
    return [
        {
            "source_uid": str(row["source_uid"]),
            "target_uid": str(row["target_uid"]),
            "hop": int(row["hop"]),
        }
        for row in rows
    ]


def _build_ripple_paths_cypher(max_hops: int) -> str:
    return f"""
        MATCH (target:Function {{repo_id: $repo_id, fqn: $fqn}})
        MATCH p = (dependent:Function {{repo_id: $repo_id}})-[:CALLS*1..{max_hops}]->(target)
        WHERE dependent <> target AND coalesce(dependent.is_test, false) = false
        WITH target, dependent, min(length(p)) AS shortest_hops
        MATCH p = (dependent)-[:CALLS*1..{max_hops}]->(target)
        WHERE length(p) = shortest_hops
        UNWIND range(0, length(p) - 1) AS idx
        WITH nodes(p)[idx] AS source,
             nodes(p)[idx + 1] AS target_node,
             length(p) - idx AS hop
        RETURN DISTINCT source.uid AS source_uid,
               target_node.uid AS target_uid,
               hop
        ORDER BY hop DESC, source_uid ASC, target_uid ASC
        """.strip()


def _normalize_node(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": str(row["uid"]),
        "fqn": str(row["fqn"]),
        "name": str(row["name"]),
        "label": str(row["label"]),
        "file_path": str(row["file_path"]),
        "blast_score": float(row.get("blast_score") or 0.0),
        "community": int(row.get("community") if row.get("community") is not None else -1),
        "is_test": bool(row.get("is_test", False)),
        "covered": bool(row.get("covered", False)),
    }
