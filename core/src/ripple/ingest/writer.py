"""Neo4j Bolt writer for RIPPLE mapped graphs."""

from __future__ import annotations

import logging
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from neo4j import GraphDatabase
from neo4j import Session

from ripple.ingest.mapping import MappedGraph, MappedNode, MappedRelationship

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BATCH_SIZE = 1000


@dataclass(frozen=True)
class WriterConfig:
    """Neo4j connection settings for ingestion writes."""

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "ripplepass"
    database: str = "neo4j"

    @classmethod
    def from_env(cls) -> "WriterConfig":
        """Build a writer config from ``NEO4J_*`` environment variables."""
        return cls(
            uri=os.environ.get("NEO4J_URI", cls.uri),
            user=os.environ.get("NEO4J_USER", cls.user),
            password=os.environ.get("NEO4J_PASSWORD", cls.password),
            database=os.environ.get("NEO4J_DATABASE", cls.database),
        )


class IngestWriter:
    """Write mapped RIPPLE graphs to Neo4j using batched Cypher MERGE statements."""

    def __init__(self, config: WriterConfig) -> None:
        self.config = config
        self._driver = GraphDatabase.driver(
            config.uri,
            auth=(config.user, config.password),
        )

    def close(self) -> None:
        """Close the underlying Neo4j driver."""
        self._driver.close()

    def write(self, mapped_graph: MappedGraph, wipe: bool = False) -> None:
        """Ensure schema and write *mapped_graph* to Neo4j."""
        with self._driver.session(database=self.config.database) as session:
            self._ensure_schema(session)
            if wipe:
                self._wipe(session, mapped_graph.repo_id)
            self._write_nodes(session, mapped_graph.nodes.values())
            self._write_relationships(session, mapped_graph.relationships)

    def _ensure_schema(self, session: Session) -> None:
        labels = ("Repo", "File", "Function", "Class", "Test", "Commit")
        for label in labels:
            _validate_ident(label)
            session.run(
                f"CREATE CONSTRAINT ripple_{label.lower()}_uid IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.uid IS UNIQUE"
            ).consume()

        session.run(
            "CREATE INDEX ripple_function_repo_id IF NOT EXISTS "
            "FOR (f:Function) ON (f.repo_id)"
        ).consume()
        session.run(
            "CREATE INDEX ripple_function_fqn IF NOT EXISTS "
            "FOR (f:Function) ON (f.fqn)"
        ).consume()

    def _wipe(self, session: Session, repo_id: str) -> None:
        result = session.run(
            """
            MATCH (n {repo_id: $repo_id})
            CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS
            """,
            repo_id=repo_id,
        )
        result.consume()
        logger.info("Wiped existing nodes for repo_id=%s", repo_id)

    def _write_nodes(self, session: Session, nodes: Iterable[MappedNode]) -> None:
        grouped: dict[tuple[str, ...], list[MappedNode]] = defaultdict(list)
        label_counts: Counter[str] = Counter()
        for node in nodes:
            grouped[node.labels].append(node)
            label_counts.update(node.labels)

        for labels, group_nodes in grouped.items():
            query = _node_query(labels)
            for batch in _chunks(group_nodes, _BATCH_SIZE):
                rows = [{"uid": node.uid, "props": _clean_props(node.properties)} for node in batch]
                session.execute_write(_run_write, query, rows)

        for label, count in sorted(label_counts.items()):
            logger.info("Wrote %d %s nodes", count, label)

    def _write_relationships(
        self,
        session: Session,
        relationships: Iterable[MappedRelationship],
    ) -> None:
        grouped: dict[tuple[str, str, str], list[MappedRelationship]] = defaultdict(list)
        rel_counts: Counter[str] = Counter()
        for rel in relationships:
            grouped[(rel.rel_type, rel.source_label, rel.target_label)].append(rel)
            rel_counts[rel.rel_type] += 1

        for (rel_type, source_label, target_label), group_rels in grouped.items():
            query = _relationship_query(rel_type, source_label, target_label)
            for batch in _chunks(group_rels, _BATCH_SIZE):
                rows = [
                    {
                        "source_uid": rel.source_uid,
                        "target_uid": rel.target_uid,
                        "props": _clean_props(rel.properties),
                    }
                    for rel in batch
                ]
                session.execute_write(_run_write, query, rows)

        for rel_type, count in sorted(rel_counts.items()):
            logger.info("Wrote %d %s relationships", count, rel_type)


def _run_write(tx, query: str, rows: list[dict[str, object]]) -> None:
    tx.run(query, rows=rows).consume()


def _node_query(labels: tuple[str, ...]) -> str:
    if not labels:
        raise ValueError("Node labels cannot be empty")
    for label in labels:
        _validate_ident(label)
    primary = labels[0]
    extra_sets = "\n".join(f"SET n:{label}" for label in labels[1:])
    return f"""
    UNWIND $rows AS row
    MERGE (n:{primary} {{uid: row.uid}})
    {extra_sets}
    SET n += row.props
    """


def _relationship_query(rel_type: str, source_label: str, target_label: str) -> str:
    for ident in (rel_type, source_label, target_label):
        _validate_ident(ident)
    return f"""
    UNWIND $rows AS row
    MATCH (source:{source_label} {{uid: row.source_uid}})
    MATCH (target:{target_label} {{uid: row.target_uid}})
    MERGE (source)-[rel:{rel_type}]->(target)
    SET rel += row.props
    """


def _chunks[T](items: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _validate_ident(value: str) -> None:
    if not _IDENT_RE.match(value):
        raise ValueError(f"Unsafe Cypher identifier: {value!r}")


def _clean_props(props: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in props.items() if value is not None}
