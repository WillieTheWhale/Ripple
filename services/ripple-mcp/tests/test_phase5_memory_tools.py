from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ripple_mcp import server


class FakeCogneeClient:
    async def __aenter__(self) -> FakeCogneeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def recall(self, repo_id: str, query: str) -> list[dict[str, object]]:
        assert repo_id == "repo-a"
        assert query == "token errors"
        return [{"text": "Token errors return 401.", "score": 0.9}]

    async def remember(self, repo_id: str, note: str) -> SimpleNamespace:
        assert repo_id == "repo-a"
        assert note == "Decision: token errors return 401."
        return SimpleNamespace(response={"dataset_id": "dataset-123"})


def test_memory_tools_recall_and_mirror_after_cognee_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RIPPLE_CHASSIS_DB_PATH", str(tmp_path / "ripple.db"))
    monkeypatch.setattr(server.CogneeClient, "from_env", lambda: FakeCogneeClient())

    recalled = server.memory_recall("repo-a", "token errors")
    remembered = server.memory_remember("repo-a", "Decision: token errors return 401.")
    mirrored = server.memory_list("repo-a")

    assert recalled == [{"text": "Token errors return 401.", "score": 0.9}]
    assert remembered["cognee_ref"] == "dataset-123"
    assert mirrored[0]["summary"] == "Decision: token errors return 401."
    assert mirrored[0]["cognee_ref"] == "dataset-123"
