"""Concurrency coverage for Daytona snapshot creation."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ripple.sandbox.daytona_provider import DaytonaProvider, _SNAPSHOT_IDS, _SNAPSHOT_LOCKS


class _NotFoundError(Exception):
    status_code = 404


class _ConflictError(Exception):
    status_code = 409


class _Params:
    def __init__(self, **values: object) -> None:
        self.values = values


class _Image:
    @staticmethod
    def from_dockerfile(path: Path) -> Path:
        return path


class _SnapshotService:
    def __init__(self, *, conflict_once: bool = False, hidden_gets_after_conflict: int = 0) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, SimpleNamespace] = {}
        self.conflict_once = conflict_once
        self.hidden_gets_after_conflict = hidden_gets_after_conflict
        self.create_calls = 0
        self.get_calls = 0

    def get(self, name: str) -> SimpleNamespace:
        with self._lock:
            self.get_calls += 1
            snapshot = self._snapshots.get(name)
            if snapshot is not None and self.hidden_gets_after_conflict:
                self.hidden_gets_after_conflict -= 1
                snapshot = None
        if snapshot is None:
            raise _NotFoundError
        return snapshot

    def create(self, params: _Params, timeout: int) -> SimpleNamespace:
        assert timeout == 900
        name = str(params.values["name"])
        with self._lock:
            self.create_calls += 1
            should_conflict = self.conflict_once
            self.conflict_once = False
            if should_conflict:
                self._snapshots[name] = SimpleNamespace(name=name)
                raise _ConflictError

        # Without the provider lock, simultaneous lookups can all reach create.
        time.sleep(0.05)
        snapshot = SimpleNamespace(name=name)
        with self._lock:
            self._snapshots.setdefault(name, snapshot)
            return self._snapshots[name]


@pytest.fixture(autouse=True)
def _clear_snapshot_state() -> None:
    _SNAPSHOT_IDS.clear()
    _SNAPSHOT_LOCKS.clear()
    yield
    _SNAPSHOT_IDS.clear()
    _SNAPSHOT_LOCKS.clear()


def _provider(service: _SnapshotService) -> DaytonaProvider:
    provider = object.__new__(DaytonaProvider)
    provider._client = SimpleNamespace(snapshot=service)
    provider._image_cls = _Image
    provider._create_snapshot_params = _Params
    provider._snapshot_params = _Params
    return provider


def test_concurrent_creation_for_one_snapshot_builds_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_hash = "a" * 64
    monkeypatch.setattr("ripple.sandbox.daytona_provider._source_hash", lambda _: source_hash)
    monkeypatch.setattr(
        "ripple.sandbox.daytona_provider._materialize_repo",
        lambda _source, destination: destination.mkdir(),
    )
    service = _SnapshotService()
    providers = [_provider(service) for _ in range(8)]
    barrier = threading.Barrier(len(providers))

    def ensure(provider: DaytonaProvider) -> str:
        barrier.wait()
        return provider.ensure_snapshot("repo", str(tmp_path))

    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        names = list(executor.map(ensure, providers))

    assert names == ["ripple-sbx-repo-aaaaaaaaaaaa"] * len(providers)
    assert service.create_calls == 1


def test_conflicting_snapshot_create_refetches_and_reuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_hash = "b" * 64
    monkeypatch.setattr("ripple.sandbox.daytona_provider._source_hash", lambda _: source_hash)
    monkeypatch.setattr(
        "ripple.sandbox.daytona_provider._materialize_repo",
        lambda _source, destination: destination.mkdir(),
    )
    service = _SnapshotService(conflict_once=True)

    snapshot_name = _provider(service).ensure_snapshot("repo", str(tmp_path))

    assert snapshot_name == "ripple-sbx-repo-bbbbbbbbbbbb"
    assert service.create_calls == 1
    assert service.get_calls == 2


def test_conflicting_snapshot_create_retries_until_visible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_hash = "c" * 64
    monkeypatch.setattr("ripple.sandbox.daytona_provider._source_hash", lambda _: source_hash)
    monkeypatch.setattr(
        "ripple.sandbox.daytona_provider._materialize_repo",
        lambda _source, destination: destination.mkdir(),
    )
    monkeypatch.setattr("ripple.sandbox.daytona_provider.time.sleep", lambda _delay: None)
    service = _SnapshotService(conflict_once=True, hidden_gets_after_conflict=2)

    snapshot_name = _provider(service).ensure_snapshot("repo", str(tmp_path))

    assert snapshot_name == "ripple-sbx-repo-cccccccccccc"
    assert service.create_calls == 1
    assert service.get_calls == 4
