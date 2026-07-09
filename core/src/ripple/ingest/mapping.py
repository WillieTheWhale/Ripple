"""Map Axon graphs into the RIPPLE phase-1 schema.

RIPPLE keeps only Function-to-Function ``CALLS`` relationships. Axon may emit
constructor calls to Class nodes; those are dropped here so downstream risk,
coverage, and writer code can treat every ``CALLS`` endpoint as a Function.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from axon.core.graph.graph import KnowledgeGraph
from axon.core.graph.model import GraphNode, NodeLabel, RelType

logger = logging.getLogger(__name__)

PropertyValue = str | int | float | bool | None

_FUNCTION_LABELS = {NodeLabel.FUNCTION, NodeLabel.METHOD}
_CLASS_LABELS = {NodeLabel.CLASS, NodeLabel.INTERFACE, NodeLabel.ENUM}


@dataclass
class MappedNode:
    """A RIPPLE node ready to be written to Neo4j."""

    uid: str
    labels: tuple[str, ...]
    properties: dict[str, PropertyValue]


@dataclass
class MappedRelationship:
    """A RIPPLE relationship ready to be written to Neo4j."""

    rel_type: str
    source_uid: str
    target_uid: str
    source_label: str
    target_label: str
    properties: dict[str, PropertyValue] = field(default_factory=dict)


@dataclass
class MappedGraph:
    """Mapped RIPPLE graph plus lookup indexes needed by later ingest stages."""

    repo_id: str
    nodes: dict[str, MappedNode] = field(default_factory=dict)
    relationships: list[MappedRelationship] = field(default_factory=list)
    axon_to_uid: dict[str, str] = field(default_factory=dict)
    file_by_path: dict[str, str] = field(default_factory=dict)
    function_by_fqn: dict[str, str] = field(default_factory=dict)
    class_by_fqn: dict[str, str] = field(default_factory=dict)
    skipped_calls: int = 0
    _relationship_index: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def add_node(self, node: MappedNode) -> bool:
        """Add *node* if its uid has not been seen, returning whether it was inserted."""
        if node.uid in self.nodes:
            return False
        self.nodes[node.uid] = node
        return True

    def add_relationship(
        self,
        rel_type: str,
        source_uid: str,
        target_uid: str,
        source_label: str,
        target_label: str,
        properties: dict[str, PropertyValue] | None = None,
    ) -> bool:
        """Add or merge a relationship, returning whether a new edge was inserted."""
        props = dict(properties or {})
        key = (rel_type, source_uid, target_uid)
        existing_idx = self._relationship_index.get(key)
        if existing_idx is not None:
            existing = self.relationships[existing_idx]
            if rel_type == "CALLS":
                old_count = int(existing.properties.get("count", 1) or 1)
                new_count = int(props.get("count", 1) or 1)
                existing.properties["count"] = old_count + new_count
            elif rel_type == "COVERS" and "depth" in props:
                old_depth = int(existing.properties.get("depth", props["depth"]) or props["depth"])
                new_depth = int(props["depth"] or old_depth)
                existing.properties["depth"] = min(old_depth, new_depth)
            else:
                existing.properties.update(props)
            return False

        self._relationship_index[key] = len(self.relationships)
        self.relationships.append(
            MappedRelationship(
                rel_type=rel_type,
                source_uid=source_uid,
                target_uid=target_uid,
                source_label=source_label,
                target_label=target_label,
                properties=props,
            )
        )
        return True

    def nodes_with_label(self, label: str) -> list[MappedNode]:
        """Return all mapped nodes carrying *label*."""
        return [node for node in self.nodes.values() if label in node.labels]

    def relationships_of_type(self, rel_type: str) -> list[MappedRelationship]:
        """Return all mapped relationships of *rel_type*."""
        return [rel for rel in self.relationships if rel.rel_type == rel_type]


@dataclass(frozen=True)
class GitCommit:
    """A git commit and the repository paths it touched."""

    sha: str
    author: str
    ts: str
    message: str
    paths: tuple[str, ...]


def map_graph(
    axon_graph: KnowledgeGraph,
    repo_path: Path,
    repo_id: str,
    commits: int = 200,
    ingested_at: datetime | None = None,
) -> MappedGraph:
    """Map an Axon graph and recent git history into the RIPPLE schema."""
    repo_path = repo_path.resolve()
    timestamp = (ingested_at or datetime.now(UTC)).isoformat(timespec="seconds")
    mapped = MappedGraph(repo_id=repo_id)

    repo_uid = _uid(repo_id, "repo")
    mapped.add_node(
        MappedNode(
            uid=repo_uid,
            labels=("Repo",),
            properties={
                "uid": repo_uid,
                "id": repo_id,
                "name": repo_path.name,
                "default_branch": _default_branch(repo_path),
                "ingested_at": timestamp,
                "repo_id": repo_id,
            },
        )
    )

    _map_files(axon_graph, mapped, repo_id)
    _map_symbols(axon_graph, mapped, repo_id)
    _map_relationships(axon_graph, mapped)
    _map_commits(repo_path, mapped, repo_id, commits)
    return mapped


def _map_files(axon_graph: KnowledgeGraph, mapped: MappedGraph, repo_id: str) -> None:
    for node in sorted(axon_graph.get_nodes_by_label(NodeLabel.FILE), key=lambda item: item.file_path):
        path = _norm_path(node.file_path)
        uid = _uid(repo_id, path)
        mapped.add_node(
            MappedNode(
                uid=uid,
                labels=("File",),
                properties={
                    "uid": uid,
                    "path": path,
                    "loc": _line_count(node),
                    "lang": node.language,
                    "repo_id": repo_id,
                },
            )
        )
        mapped.axon_to_uid[node.id] = uid
        mapped.file_by_path[path] = uid
        mapped.add_relationship("CONTAINS", _uid(repo_id, "repo"), uid, "Repo", "File")


def _map_symbols(axon_graph: KnowledgeGraph, mapped: MappedGraph, repo_id: str) -> None:
    symbol_nodes = sorted(
        (
            node
            for node in axon_graph.iter_nodes()
            if node.label in _FUNCTION_LABELS or node.label in _CLASS_LABELS
        ),
        key=lambda item: (item.file_path, item.start_line, item.label.value, item.name),
    )

    for node in symbol_nodes:
        if node.label in _FUNCTION_LABELS:
            _map_function(node, mapped, repo_id)
        elif node.label in _CLASS_LABELS:
            _map_class(node, mapped, repo_id)


def _map_function(node: GraphNode, mapped: MappedGraph, repo_id: str) -> None:
    file_path = _norm_path(node.file_path)
    qualified_name = _qualified_function_name(node)
    fqn = f"{file_path}:{qualified_name}"
    uid = _uid(repo_id, fqn)

    if fqn in mapped.function_by_fqn or uid in mapped.nodes:
        logger.warning("Duplicate function fqn %s from Axon node %s; keeping first", fqn, node.id)
        return

    is_test = _is_test_function(file_path, node.name)
    labels = ("Function", "Test") if is_test else ("Function",)
    mapped.add_node(
        MappedNode(
            uid=uid,
            labels=labels,
            properties={
                "uid": uid,
                "fqn": fqn,
                "name": node.name,
                "file_path": file_path,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "loc": _line_count(node),
                "is_test": is_test,
                "doc": _doc_first_line(node.content),
                "repo_id": repo_id,
            },
        )
    )
    mapped.axon_to_uid[node.id] = uid
    mapped.function_by_fqn[fqn] = uid

    file_uid = mapped.file_by_path.get(file_path)
    if file_uid is not None:
        mapped.add_relationship("DEFINES", file_uid, uid, "File", "Function")


def _map_class(node: GraphNode, mapped: MappedGraph, repo_id: str) -> None:
    file_path = _norm_path(node.file_path)
    fqn = f"{file_path}:{node.name}"
    uid = _uid(repo_id, fqn)

    if fqn in mapped.class_by_fqn or uid in mapped.nodes:
        logger.warning("Duplicate class fqn %s from Axon node %s; keeping first", fqn, node.id)
        return

    mapped.add_node(
        MappedNode(
            uid=uid,
            labels=("Class",),
            properties={
                "uid": uid,
                "fqn": fqn,
                "name": node.name,
                "file_path": file_path,
                "repo_id": repo_id,
            },
        )
    )
    mapped.axon_to_uid[node.id] = uid
    mapped.class_by_fqn[fqn] = uid

    file_uid = mapped.file_by_path.get(file_path)
    if file_uid is not None:
        mapped.add_relationship("DEFINES", file_uid, uid, "File", "Class")


def _map_relationships(axon_graph: KnowledgeGraph, mapped: MappedGraph) -> None:
    for rel in axon_graph.get_relationships_by_type(RelType.CALLS):
        source_uid = mapped.axon_to_uid.get(rel.source)
        target_uid = mapped.axon_to_uid.get(rel.target)
        if not source_uid or not target_uid:
            mapped.skipped_calls += 1
            continue
        if "Function" not in mapped.nodes[source_uid].labels or "Function" not in mapped.nodes[target_uid].labels:
            mapped.skipped_calls += 1
            continue
        mapped.add_relationship(
            "CALLS",
            source_uid,
            target_uid,
            "Function",
            "Function",
            {"count": _rel_count(rel.properties)},
        )

    if mapped.skipped_calls:
        logger.info("Skipped %d CALLS edges with unmapped or non-Function endpoints", mapped.skipped_calls)

    for rel in axon_graph.get_relationships_by_type(RelType.IMPORTS):
        source_uid = mapped.axon_to_uid.get(rel.source)
        target_uid = mapped.axon_to_uid.get(rel.target)
        if source_uid and target_uid:
            mapped.add_relationship("IMPORTS", source_uid, target_uid, "File", "File")

    for rel_type in (RelType.EXTENDS, RelType.IMPLEMENTS):
        for rel in axon_graph.get_relationships_by_type(rel_type):
            source_uid = mapped.axon_to_uid.get(rel.source)
            target_uid = mapped.axon_to_uid.get(rel.target)
            if (
                source_uid
                and target_uid
                and "Class" in mapped.nodes[source_uid].labels
                and "Class" in mapped.nodes[target_uid].labels
            ):
                mapped.add_relationship("INHERITS", source_uid, target_uid, "Class", "Class")


def _map_commits(repo_path: Path, mapped: MappedGraph, repo_id: str, limit: int) -> None:
    if limit <= 0:
        return

    known_files = set(mapped.file_by_path)
    for commit in _read_commits(repo_path, limit):
        uid = _uid(repo_id, commit.sha)
        mapped.add_node(
            MappedNode(
                uid=uid,
                labels=("Commit",),
                properties={
                    "uid": uid,
                    "sha": commit.sha,
                    "author": commit.author,
                    "ts": commit.ts,
                    "message": commit.message,
                    "repo_id": repo_id,
                },
            )
        )
        for path in commit.paths:
            norm_path = _norm_path(path)
            if norm_path not in known_files:
                continue
            mapped.add_relationship("TOUCHED", uid, mapped.file_by_path[norm_path], "Commit", "File")


def _read_commits(repo_path: Path, limit: int) -> list[GitCommit]:
    raw = _git_output(
        repo_path,
        [
            "log",
            "-n",
            str(limit),
            "--date=iso-strict",
            "--pretty=format:%x1e%H%x1f%an%x1f%aI%x1f%s",
            "--name-only",
        ],
    )
    if not raw:
        return []

    commits: list[GitCommit] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        lines = [line for line in record.splitlines() if line.strip()]
        if not lines:
            continue
        parts = lines[0].split("\x1f", maxsplit=3)
        if len(parts) != 4:
            logger.warning("Skipping unparsable git log record header: %r", lines[0])
            continue
        paths = tuple(_norm_path(line.strip()) for line in lines[1:] if line.strip())
        commits.append(
            GitCommit(
                sha=parts[0],
                author=parts[1],
                ts=parts[2],
                message=parts[3],
                paths=paths,
            )
        )
    return commits


def _default_branch(repo_path: Path) -> str:
    branch = _git_output(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch and branch != "HEAD":
        return branch.strip()
    symbolic = _git_output(repo_path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if symbolic:
        return symbolic.removeprefix("origin/").strip()
    return ""


def _git_output(repo_path: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _uid(repo_id: str, key: str) -> str:
    return f"{repo_id}#{key}"


def _norm_path(path: str) -> str:
    return path.replace("\\", "/")


def _qualified_function_name(node: GraphNode) -> str:
    if node.label == NodeLabel.METHOD and node.class_name:
        return f"{node.class_name}.{node.name}"
    return node.name


def _line_count(node: GraphNode) -> int:
    if node.start_line > 0 and node.end_line >= node.start_line:
        return node.end_line - node.start_line + 1
    if node.end_line > 0:
        return node.end_line
    if node.content:
        return len(node.content.splitlines())
    return 0


def _is_test_function(file_path: str, name: str) -> bool:
    pure = PurePosixPath(file_path)
    basename = pure.name
    in_tests_dir = "tests" in pure.parts
    return (
        fnmatch.fnmatch(basename, "test_*.py")
        or fnmatch.fnmatch(basename, "*_test.py")
        or in_tests_dir
        or name.startswith("test_")
    )


def _doc_first_line(content: str) -> str:
    if not content:
        return ""
    try:
        module = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return ""
    if not module.body:
        return ""
    doc = ast.get_docstring(module.body[0])
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _rel_count(properties: dict[str, Any]) -> int:
    raw = properties.get("count", 1)
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(count, 1)

