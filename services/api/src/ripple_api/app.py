"""FastAPI app for RIPPLE read-only graph queries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from ripple.db import create_driver, read_records
from ripple.query.blast import blast_radius, resolve_fqn
from ripple.query.graphview import graph_snapshot, repo_stats, ripple_paths


def create_app(driver: Any | None = None) -> FastAPI:
    """Create the RIPPLE read-only API app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_driver = None
        if driver is None:
            owned_driver = create_driver()
            app.state.driver = owned_driver
        else:
            app.state.driver = driver
        try:
            yield
        finally:
            if owned_driver is not None:
                owned_driver.close()

    app = FastAPI(title="RIPPLE API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:8787",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8787",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            app.state.driver.verify_connectivity()
        except Exception:
            return {"status": "ok", "neo4j": "down"}
        return {"status": "ok", "neo4j": "up"}

    @app.get("/repos")
    def repos() -> list[dict[str, Any]]:
        rows = read_records(
            app.state.driver,
            """
            MATCH (repo:Repo)
            RETURN repo.id AS repo_id,
                   repo.name AS name,
                   repo.default_branch AS default_branch,
                   repo.ingested_at AS ingested_at
            ORDER BY repo.id ASC
            """,
        )
        return [
            {
                "repo_id": str(row["repo_id"]),
                "name": str(row.get("name") or ""),
                "default_branch": str(row.get("default_branch") or ""),
                "ingested_at": str(row.get("ingested_at") or ""),
                "stats": repo_stats(app.state.driver, str(row["repo_id"])),
            }
            for row in rows
        ]

    @app.get("/repos/{repo_id}/graph")
    def repo_graph(
        repo_id: str,
        max_nodes: int = Query(default=2000, ge=1, le=2000),
    ) -> dict[str, list[dict[str, Any]]]:
        return graph_snapshot(app.state.driver, repo_id, max_nodes=max_nodes)

    @app.get("/repos/{repo_id}/blast")
    def repo_blast(
        repo_id: str,
        fqn: str,
        max_hops: int = Query(default=4, ge=1, le=12),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return asdict(blast_radius(app.state.driver, repo_id, fqn, max_hops, limit))

    @app.get("/repos/{repo_id}/blast/animation")
    def repo_blast_animation(
        repo_id: str,
        fqn: str,
        max_hops: int = Query(default=4, ge=1, le=12),
    ) -> list[dict[str, int | str]]:
        return ripple_paths(app.state.driver, repo_id, fqn, max_hops=max_hops)

    @app.get("/repos/{repo_id}/resolve")
    def repo_resolve(repo_id: str, q: str) -> list[str]:
        return resolve_fqn(app.state.driver, repo_id, q)

    return app


app = create_app()
