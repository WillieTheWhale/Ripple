from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ripple.chassis_db import ChassisDB, add_memory_note, initialize_chassis_db, list_memory_notes


def test_initialize_creates_memory_notes_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "ripple_local.db"

    initialize_chassis_db(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = connection.execute("PRAGMA table_info(memory_notes)").fetchall()

    assert db_path.exists()
    assert [column[1] for column in columns] == [
        "id",
        "repo_id",
        "summary",
        "cognee_ref",
        "created_at",
    ]


def test_memory_notes_are_parameterized_scoped_and_ordered(tmp_path: Path) -> None:
    db_path = tmp_path / "ripple_local.db"
    first = add_memory_note("repo-a", "first note", "cognee:first", db_path=db_path)
    second = add_memory_note(
        "repo-a",
        "second note with an apostrophe: it's safe",
        db_path=db_path,
    )
    add_memory_note("repo-b", "other repository", db_path=db_path)

    notes = list_memory_notes("repo-a", db_path=db_path)

    assert [note.id for note in notes] == [second.id, first.id]
    assert [note.summary for note in notes] == ["second note with an apostrophe: it's safe", "first note"]
    assert notes[0].cognee_ref is None
    assert notes[1].cognee_ref == "cognee:first"
    assert all(note.created_at.tzinfo is not None for note in notes)


def test_concurrent_connections_persist_every_memory_note(tmp_path: Path) -> None:
    db_path = tmp_path / "ripple_local.db"
    database = ChassisDB(db_path)
    database.initialize()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: database.add_memory_note("repo-a", f"note {index}"),
                range(24),
            )
        )

    notes = database.list_memory_notes("repo-a")

    assert len(notes) == 24
    assert {note.summary for note in notes} == {f"note {index}" for index in range(24)}
