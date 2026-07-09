"""Shared Neo4j configuration and read helpers for RIPPLE."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase


@dataclass(frozen=True)
class Neo4jConfig:
    """Neo4j connection settings sourced from ``NEO4J_*`` environment variables."""

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "ripplepass"
    database: str = "neo4j"

    @classmethod
    def from_env(cls) -> "Neo4jConfig":
        """Build a config from environment variables using local-dev defaults."""
        return cls(
            uri=os.environ.get("NEO4J_URI", cls.uri),
            user=os.environ.get("NEO4J_USER", cls.user),
            password=os.environ.get("NEO4J_PASSWORD", cls.password),
            database=os.environ.get("NEO4J_DATABASE", cls.database),
        )


def create_driver(config: Neo4jConfig | None = None):
    """Create a Neo4j driver using *config* or the environment-backed defaults."""
    resolved = config or Neo4jConfig.from_env()
    return GraphDatabase.driver(resolved.uri, auth=(resolved.user, resolved.password))


def read_records(
    session_or_driver: Any,
    cypher: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run a read query against a Neo4j session or driver and return plain dictionaries."""
    resolved_params = params or {}
    if hasattr(session_or_driver, "run"):
        return _records_to_dicts(session_or_driver.run(cypher, resolved_params))

    config = Neo4jConfig.from_env()
    session_factory = session_or_driver.session
    try:
        context = session_factory(database=config.database)
    except TypeError:
        context = session_factory()

    with context as session:
        return _records_to_dicts(session.run(cypher, resolved_params))


def _records_to_dicts(records: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if hasattr(record, "data"):
            rows.append(record.data())
        else:
            rows.append(dict(record))
    return rows
