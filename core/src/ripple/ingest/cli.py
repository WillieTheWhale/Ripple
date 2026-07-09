"""Command-line interface for RIPPLE ingestion."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import NoReturn

from ripple.ingest.covers import add_static_covers
from ripple.ingest.extract import extract
from ripple.ingest.mapping import MappedGraph, map_graph
from ripple.ingest.risk import apply_risk
from ripple.ingest.writer import IngestWriter, WriterConfig

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run a RIPPLE ingestion from the command line."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    started = time.monotonic()

    def emit(stage: str, pct: float, detail: str) -> None:
        if args.emit_progress:
            print(json.dumps({"stage": stage, "pct": pct, "detail": detail}), flush=True)

    try:
        emit("clone", 0.0, "resolving repository")
        repo_path = _prepare_repo(args.repo, args.repo_id)
        emit("clone", 1.0, str(repo_path))

        emit("extract", 0.0, "running axon pipeline")
        axon_graph = extract(repo_path)
        emit("extract", 1.0, f"{axon_graph.node_count} nodes")

        emit("map", 0.0, "mapping ripple schema")
        mapped = map_graph(axon_graph, repo_path, args.repo_id, commits=args.commits)
        emit("map", 1.0, f"{len(mapped.nodes)} nodes")

        emit("covers", 0.0, args.covers)
        if args.covers == "static":
            covers = add_static_covers(axon_graph, mapped)
        else:
            covers = 0
        emit("covers", 1.0, f"{covers} covers")

        emit("risk", 0.0, "computing analytics")
        apply_risk(mapped)
        emit("risk", 1.0, "analytics complete")

        emit("write", 0.0, "writing neo4j")
        writer = IngestWriter(WriterConfig.from_env())
        try:
            writer.write(mapped, wipe=args.wipe)
        finally:
            writer.close()
        emit("write", 1.0, "write complete")

        summary = _summary(mapped, started)
        print(json.dumps(summary), flush=True)
        return 0
    except Exception as exc:
        logger.exception("Ingestion failed")
        _die(str(exc))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ripple.ingest")
    parser.add_argument("--repo", required=True, help="Repository path or git URL")
    parser.add_argument("--repo-id", required=True, help="Stable RIPPLE repository id")
    parser.add_argument("--covers", choices=("static", "off"), default="static")
    parser.add_argument("--commits", type=int, default=200)
    parser.add_argument("--wipe", action="store_true")
    parser.add_argument("--emit-progress", action="store_true")
    return parser


def _prepare_repo(repo: str, repo_id: str) -> Path:
    if _is_git_url(repo):
        work_root = Path(".ripple_work")
        work_root.mkdir(exist_ok=True)
        clone_dir = work_root / f"{_slug(repo_id)}-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["git", "clone", "--depth", "1", repo, str(clone_dir)],
            check=True,
        )
        return clone_dir.resolve()

    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo}")
    return repo_path


def _is_git_url(value: str) -> bool:
    if "://" in value or value.startswith("git@"):
        return True
    return value.endswith(".git") and not Path(value).exists()


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "repo"


def _summary(mapped: MappedGraph, started: float) -> dict[str, int | float | str]:
    return {
        "stage": "done",
        "nodes": len(mapped.nodes),
        "edges": len(mapped.relationships),
        "functions": len(mapped.nodes_with_label("Function")),
        "tests": len(mapped.nodes_with_label("Test")),
        "covers": len(mapped.relationships_of_type("COVERS")),
        "duration_s": round(time.monotonic() - started, 3),
    }


def _die(message: str) -> NoReturn:
    raise SystemExit(message)


if __name__ == "__main__":
    raise SystemExit(main())
