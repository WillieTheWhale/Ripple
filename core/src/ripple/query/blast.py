"""Blast-radius read queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ripple.db import read_records


@dataclass(frozen=True)
class ImpactedNode:
    """A function that transitively depends on the blast-radius target."""

    fqn: str
    name: str
    file_path: str
    hops: int
    centrality: float
    tests: int
    risk: float
    community: int


@dataclass(frozen=True)
class BlastResult:
    """Blast-radius query result plus transparency metadata."""

    target_fqn: str
    impacted: list[ImpactedNode]
    cypher: str
    params: dict[str, Any]
    uncovered_count: int
    total: int


def blast_radius(
    session_or_driver: Any,
    repo_id: str,
    fqn: str,
    max_hops: int = 4,
    limit: int = 50,
) -> BlastResult:
    """Return production-impact dependents that transitively call *fqn*."""
    normalized_hops = _validate_positive_int(max_hops, "max_hops")
    normalized_limit = _validate_positive_int(limit, "limit")
    cypher = build_blast_cypher(normalized_hops)
    params = {"repo_id": repo_id, "fqn": fqn, "limit": normalized_limit}
    rows = read_records(session_or_driver, cypher, params)
    impacted = [
        ImpactedNode(
            fqn=str(row["fqn"]),
            name=str(row.get("name") or ""),
            file_path=str(row.get("file_path") or ""),
            hops=int(row["hops"]),
            centrality=float(row.get("centrality") or 0.0),
            tests=int(row.get("tests") or 0),
            risk=float(row.get("risk") or 0.0),
            community=int(row.get("community") if row.get("community") is not None else -1),
        )
        for row in rows
    ]
    return BlastResult(
        target_fqn=fqn,
        impacted=impacted,
        cypher=cypher,
        params=params,
        uncovered_count=sum(1 for node in impacted if node.tests == 0),
        total=len(impacted),
    )


def resolve_fqn(driver: Any, repo_id: str, query: str) -> list[str]:
    """Resolve a user query to up to 10 candidate Function FQNs."""
    if not query:
        return []
    rows = read_records(
        driver,
        """
        MATCH (f:Function {repo_id: $repo_id})
        WHERE f.fqn = $query OR toLower(f.name) CONTAINS toLower($query)
        WITH f, CASE WHEN f.fqn = $query THEN 0 ELSE 1 END AS match_rank
        RETURN f.fqn AS fqn
        ORDER BY match_rank ASC, coalesce(f.blast_score, 0.0) DESC, f.fqn ASC
        LIMIT 10
        """,
        {"repo_id": repo_id, "query": query},
    )
    return [str(row["fqn"]) for row in rows]


def calculate_risk(blast_score: float, hops: int, tests: int) -> float:
    """Compute the Phase 2 risk score for unit tests and API parity."""
    if hops <= 0:
        raise ValueError("hops must be positive")
    multiplier = 1.5 if tests == 0 else 1.0
    return round(float(blast_score) * (1.0 / hops) * multiplier, 3)


def build_blast_cypher(max_hops: int = 4) -> str:
    """Build the exact blast-radius Cypher text used for a validated hop limit."""
    normalized_hops = _validate_positive_int(max_hops, "max_hops")
    return f"""
        MATCH (target:Function {{repo_id: $repo_id, fqn: $fqn}})
        CALL (target) {{
          MATCH p = (dependent:Function {{repo_id: $repo_id}})-[:CALLS*1..{normalized_hops}]->(target)
          WHERE dependent <> target AND coalesce(dependent.is_test, false) = false
          RETURN dependent, min(length(p)) AS hops
        }}
        OPTIONAL MATCH (t:Test {{repo_id: $repo_id}})-[:COVERS]->(dependent)
        WITH dependent, hops, count(DISTINCT t) AS tests
        RETURN dependent.fqn AS fqn,
               dependent.name AS name,
               dependent.file_path AS file_path,
               hops,
               coalesce(dependent.blast_score, 0.0) AS centrality,
               tests,
               round(coalesce(dependent.blast_score, 0.0) * (1.0 / hops) *
                     CASE WHEN tests = 0 THEN 1.5 ELSE 1.0 END, 3) AS risk,
               coalesce(dependent.community, -1) AS community
        ORDER BY risk DESC
        LIMIT $limit
        """.strip()


def _validate_positive_int(value: int, name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return normalized
