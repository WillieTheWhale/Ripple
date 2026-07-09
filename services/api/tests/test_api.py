from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from ripple.query.blast import BlastResult, ImpactedNode

api_app = importlib.import_module("ripple_api.app")


class FakeDriver:
    def verify_connectivity(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_health_reports_neo4j_up() -> None:
    app = api_app.create_app(driver=FakeDriver())

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "neo4j": "up"}


def test_repos_includes_stats(monkeypatch) -> None:
    def fake_read_records(driver, cypher, params=None):
        return [
            {
                "repo_id": "mini",
                "name": "Mini",
                "default_branch": "main",
                "ingested_at": "2026-07-09T00:00:00+00:00",
            }
        ]

    def fake_repo_stats(driver, repo_id):
        assert repo_id == "mini"
        return {"functions": 3, "classes": 1, "files": 2}

    monkeypatch.setattr(api_app, "read_records", fake_read_records)
    monkeypatch.setattr(api_app, "repo_stats", fake_repo_stats)
    app = api_app.create_app(driver=FakeDriver())

    with TestClient(app) as client:
        assert client.get("/repos").json() == [
            {
                "repo_id": "mini",
                "name": "Mini",
                "default_branch": "main",
                "ingested_at": "2026-07-09T00:00:00+00:00",
                "stats": {"functions": 3, "classes": 1, "files": 2},
            }
        ]


def test_blast_endpoint_returns_dataclass_payload(monkeypatch) -> None:
    def fake_blast_radius(driver, repo_id, fqn, max_hops, limit):
        assert (repo_id, fqn, max_hops, limit) == ("mini", "src/app.py:target", 4, 50)
        return BlastResult(
            target_fqn=fqn,
            impacted=[
                ImpactedNode(
                    fqn="src/app.py:caller",
                    name="caller",
                    file_path="src/app.py",
                    hops=1,
                    centrality=0.9,
                    tests=0,
                    risk=1.35,
                    community=2,
                )
            ],
            cypher="MATCH ...",
            params={"repo_id": repo_id, "fqn": fqn, "limit": limit},
            uncovered_count=1,
            total=1,
        )

    monkeypatch.setattr(api_app, "blast_radius", fake_blast_radius)
    app = api_app.create_app(driver=FakeDriver())

    with TestClient(app) as client:
        payload = client.get("/repos/mini/blast", params={"fqn": "src/app.py:target"}).json()

    assert payload["target_fqn"] == "src/app.py:target"
    assert payload["impacted"][0]["fqn"] == "src/app.py:caller"
    assert payload["cypher"] == "MATCH ..."
    assert payload["uncovered_count"] == 1


def test_graph_animation_and_resolve_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(
        api_app,
        "graph_snapshot",
        lambda driver, repo_id, max_nodes: {"nodes": [{"uid": "mini#a"}], "edges": []},
    )
    monkeypatch.setattr(
        api_app,
        "ripple_paths",
        lambda driver, repo_id, fqn, max_hops: [
            {"source_uid": "mini#a", "target_uid": "mini#b", "hop": 1}
        ],
    )
    monkeypatch.setattr(
        api_app,
        "resolve_fqn",
        lambda driver, repo_id, q: ["src/app.py:target"],
    )
    app = api_app.create_app(driver=FakeDriver())

    with TestClient(app) as client:
        assert client.get("/repos/mini/graph", params={"max_nodes": 10}).json()["nodes"]
        assert client.get(
            "/repos/mini/blast/animation", params={"fqn": "src/app.py:target"}
        ).json() == [{"source_uid": "mini#a", "target_uid": "mini#b", "hop": 1}]
        assert client.get("/repos/mini/resolve", params={"q": "target"}).json() == [
            "src/app.py:target"
        ]
