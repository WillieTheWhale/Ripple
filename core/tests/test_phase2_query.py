from __future__ import annotations

import pytest

from ripple.query.blast import blast_radius, build_blast_cypher, calculate_risk


class FakeSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.cypher = ""
        self.params: dict[str, object] = {}

    def run(
        self,
        cypher: str,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        self.cypher = cypher
        self.params = params or {}
        return self.rows


def test_calculate_risk_uses_decay_and_uncovered_multiplier() -> None:
    assert calculate_risk(0.8, hops=2, tests=0) == 0.6
    assert calculate_risk(0.8, hops=2, tests=3) == 0.4
    assert calculate_risk(0.3333, hops=3, tests=0) == 0.167


def test_calculate_risk_rejects_zero_hops() -> None:
    with pytest.raises(ValueError, match="hops"):
        calculate_risk(1.0, hops=0, tests=0)


def test_blast_radius_returns_transparent_cypher_and_params() -> None:
    session = FakeSession(
        [
            {
                "fqn": "src/app.py:caller",
                "name": "caller",
                "file_path": "src/app.py",
                "hops": 2,
                "centrality": 0.75,
                "tests": 0,
                "risk": 0.563,
                "community": 4,
            }
        ]
    )

    result = blast_radius(session, "repo", "src/app.py:target", max_hops=3, limit=7)

    assert result.target_fqn == "src/app.py:target"
    assert result.total == 1
    assert result.uncovered_count == 1
    assert result.impacted[0].risk == 0.563
    assert result.cypher == session.cypher
    assert result.params == {"repo_id": "repo", "fqn": "src/app.py:target", "limit": 7}
    assert "CALLS*1..3" in result.cypher
    assert "repo_id: $repo_id" in result.cypher
    assert "fqn: $fqn" in result.cypher
    assert "count(DISTINCT t) AS tests" in result.cypher
    assert "CASE WHEN tests = 0 THEN 1.5 ELSE 1.0 END" in result.cypher
    assert "coalesce(dependent.is_test, false) = false" in result.cypher


def test_build_blast_cypher_rejects_invalid_hops() -> None:
    with pytest.raises(ValueError, match="max_hops"):
        build_blast_cypher(0)
