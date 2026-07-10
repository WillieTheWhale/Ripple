"""Local SQLite mirror used by the RIPPLE product chassis."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

type DatabasePath = str | Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "chassis" / "ripple_local.db"


@dataclass(frozen=True)
class MemoryNote:
    """A Cognee memory note mirrored locally for the UI timeline."""

    id: int
    repo_id: str
    summary: str
    cognee_ref: str | None
    created_at: datetime


class ChassisDB:
    """SQLite access for chassis data, using short-lived connections per operation."""

    def __init__(self, db_path: DatabasePath = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        """Create the local database and its tables when they do not yet exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    cognee_ref TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    def add_memory_note(
        self,
        repo_id: str,
        summary: str,
        cognee_ref: str | None = None,
    ) -> MemoryNote:
        """Persist and return a local mirror of a Cognee memory note."""
        self.initialize()
        created_at = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_notes (repo_id, summary, cognee_ref, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (repo_id, summary, cognee_ref, _serialize_timestamp(created_at)),
            )
            note_id = cursor.lastrowid

        if note_id is None:
            raise RuntimeError("SQLite did not return an id for the memory note")
        return MemoryNote(
            id=note_id,
            repo_id=repo_id,
            summary=summary,
            cognee_ref=cognee_ref,
            created_at=created_at,
        )

    def list_memory_notes(self, repo_id: str) -> list[MemoryNote]:
        """Return a repository's memory notes from newest to oldest."""
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, repo_id, summary, cognee_ref, created_at
                FROM memory_notes
                WHERE repo_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (repo_id,),
            ).fetchall()

        return [_memory_note_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


def initialize_chassis_db(db_path: DatabasePath = DEFAULT_DB_PATH) -> None:
    """Initialize the local chassis database at *db_path*."""
    ChassisDB(db_path).initialize()


def add_memory_note(
    repo_id: str,
    summary: str,
    cognee_ref: str | None = None,
    *,
    db_path: DatabasePath = DEFAULT_DB_PATH,
) -> MemoryNote:
    """Persist and return a local Cognee memory-note mirror."""
    return ChassisDB(db_path).add_memory_note(repo_id, summary, cognee_ref)


def list_memory_notes(
    repo_id: str,
    *,
    db_path: DatabasePath = DEFAULT_DB_PATH,
) -> list[MemoryNote]:
    """Return a repository's mirrored memory notes, newest first."""
    return ChassisDB(db_path).list_memory_notes(repo_id)


def _serialize_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _memory_note_from_row(row: sqlite3.Row) -> MemoryNote:
    return MemoryNote(
        id=row["id"],
        repo_id=row["repo_id"],
        summary=row["summary"],
        cognee_ref=row["cognee_ref"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
