from __future__ import annotations

from pathlib import Path

import pytest

from ripple.ingest.covers import add_static_covers
from ripple.ingest.extract import extract
from ripple.ingest.mapping import MappedGraph, MappedNode, map_graph
from ripple.ingest.risk import apply_risk

FIXTURE = Path(__file__).parent / "fixtures" / "miniproj"


@pytest.fixture(scope="module")
def mapped_pair() -> tuple[object, MappedGraph]:
    axon_graph = extract(FIXTURE)
    mapped = map_graph(axon_graph, FIXTURE, "mini", commits=200)
    return axon_graph, mapped


def test_mapping_counts_flags_and_fqns(mapped_pair: tuple[object, MappedGraph]) -> None:
    _, mapped = mapped_pair

    assert len(mapped.nodes_with_label("File")) == 5
    assert len(mapped.nodes_with_label("Function")) == 12
    assert len(mapped.nodes_with_label("Class")) == 3
    assert len(mapped.nodes_with_label("Test")) == 2

    normalize_uid = mapped.function_by_fqn["src/miniproj/math_ops.py:normalize"]
    normalize = mapped.nodes[normalize_uid]
    assert normalize.properties["name"] == "normalize"
    assert normalize.properties["doc"] == "Return a normalized display value."
    assert normalize.properties["is_test"] is False

    method_uid = mapped.function_by_fqn["src/miniproj/service.py:Processor.run"]
    assert mapped.nodes[method_uid].properties["name"] == "run"

    test_uid = mapped.function_by_fqn["tests/test_mini.py:test_compute_value"]
    assert "Test" in mapped.nodes[test_uid].labels
    assert mapped.nodes[test_uid].properties["is_test"] is True


def test_static_covers_edges(mapped_pair: tuple[object, MappedGraph]) -> None:
    axon_graph, mapped = mapped_pair

    covers_added = add_static_covers(axon_graph, mapped)
    covered_fqns = {
        mapped.nodes[rel.target_uid].properties["fqn"]
        for rel in mapped.relationships_of_type("COVERS")
    }

    assert covers_added >= 4
    assert "src/miniproj/service.py:compute_value" in covered_fqns
    assert "src/miniproj/service.py:compute_label" in covered_fqns
    assert "src/miniproj/math_ops.py:normalize" in covered_fqns
    assert all(rel.properties["depth"] <= 3 for rel in mapped.relationships_of_type("COVERS"))


def test_risk_scores_most_called_function_max(mapped_pair: tuple[object, MappedGraph]) -> None:
    _, mapped = mapped_pair

    scores, communities = apply_risk(mapped)
    normalize_fqn = "src/miniproj/math_ops.py:normalize"

    assert scores[normalize_fqn] == max(scores.values())
    assert mapped.nodes[mapped.function_by_fqn[normalize_fqn]].properties["blast_score"] == max(
        scores.values()
    )
    assert all(0.0 <= score <= 1.0 for score in scores.values())
    assert set(communities) == set(scores)
    assert all("community" in node.properties for node in mapped.nodes_with_label("Function"))


def test_risk_pagerank_excludes_test_functions() -> None:
    mapped = MappedGraph(repo_id="mini")
    for fqn, name, is_test in (
        ("src/app.py:prod_caller", "prod_caller", False),
        ("src/app.py:prod_target", "prod_target", False),
        ("src/app.py:test_only_target", "test_only_target", False),
        ("tests/test_app.py:test_target", "test_target", True),
    ):
        uid = f"mini#{fqn}"
        labels = ("Function", "Test") if is_test else ("Function",)
        mapped.add_node(
            MappedNode(
                uid=uid,
                labels=labels,
                properties={
                    "uid": uid,
                    "fqn": fqn,
                    "name": name,
                    "file_path": fqn.split(":", maxsplit=1)[0],
                    "is_test": is_test,
                    "repo_id": "mini",
                },
            )
        )
        mapped.function_by_fqn[fqn] = uid

    mapped.add_relationship(
        "CALLS",
        mapped.function_by_fqn["src/app.py:prod_caller"],
        mapped.function_by_fqn["src/app.py:prod_target"],
        "Function",
        "Function",
    )
    mapped.add_relationship(
        "CALLS",
        mapped.function_by_fqn["tests/test_app.py:test_target"],
        mapped.function_by_fqn["src/app.py:test_only_target"],
        "Function",
        "Function",
        {"count": 100},
    )

    scores, communities = apply_risk(mapped)

    assert scores["src/app.py:prod_target"] == 1.0
    assert scores["src/app.py:test_only_target"] == 0.0
    assert scores["tests/test_app.py:test_target"] == 0.0
    assert communities["src/app.py:test_only_target"] != -1
    assert communities["tests/test_app.py:test_target"] != -1
