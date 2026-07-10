"""Focused unit coverage for local Docker orphan reaping."""

from __future__ import annotations

import json
import subprocess

import pytest

from ripple.sandbox.local_docker import reap_orphaned_local_docker_sandboxes


def _completed(args: list[str], *, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def _container(container_id: str, name: str, created: str, *, managed: bool = True) -> str:
    labels = {"ripple.sandbox": "1"} if managed else {"other.label": "1"}
    return json.dumps(
        [
            {
                "Id": container_id,
                "Name": f"/{name}",
                "Created": created,
                "Config": {"Labels": labels},
                "State": {"Running": False},
            }
        ]
    )


def test_reaper_removes_only_old_managed_containers_not_in_live_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_id = "a" * 64
    active_id = "b" * 64
    recent_id = "c" * 64
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        if args[2:4] == ["ls", "-aq"]:
            return _completed(args, stdout=f"{old_id}\n{active_id}\n{recent_id}\n")
        if args[2] == "inspect":
            containers = {
                old_id: _container(old_id, "ripple-sbx-run-old", "2020-01-01T00:00:00Z"),
                active_id: _container(active_id, "ripple-sbx-run-active", "2020-01-01T00:00:00Z"),
                recent_id: _container(recent_id, "ripple-sbx-run-recent", "2035-01-01T00:00:00Z"),
            }
            return _completed(args, stdout=containers[args[-1]])
        if args[1:3] == ["rm", "-f"]:
            return _completed(args)
        raise AssertionError(f"Unexpected Docker invocation: {args}")

    monkeypatch.setattr("ripple.sandbox.local_docker._run", fake_run)
    monkeypatch.setattr("ripple.sandbox.local_docker.time.time", lambda: 1_900_000_000)

    result = reap_orphaned_local_docker_sandboxes({"ripple-sbx-run-active"}, min_age_seconds=60)

    assert result.removed == ("ripple-sbx-run-old",)
    assert result.skipped_active == ("ripple-sbx-run-active",)
    assert result.skipped_recent == ("ripple-sbx-run-recent",)
    assert result.failed == ()
    assert [call for call in calls if call[1:3] == ["rm", "-f"]] == [
        ["docker", "rm", "-f", old_id]
    ]


def test_reaper_treats_full_and_short_container_ids_as_active(monkeypatch: pytest.MonkeyPatch) -> None:
    container_id = "d" * 64
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        if args[2:4] == ["ls", "-aq"]:
            return _completed(args, stdout=f"{container_id}\n")
        if args[2] == "inspect":
            return _completed(args, stdout=_container(container_id, "ripple-sbx-run-live", "2020-01-01T00:00:00Z"))
        raise AssertionError(f"Unexpected Docker invocation: {args}")

    monkeypatch.setattr("ripple.sandbox.local_docker._run", fake_run)
    monkeypatch.setattr("ripple.sandbox.local_docker.time.time", lambda: 1_900_000_000)

    result = reap_orphaned_local_docker_sandboxes({container_id[:12]}, min_age_seconds=60)

    assert result.removed == ()
    assert result.skipped_active == ("ripple-sbx-run-live",)
    assert all(call[1] != "rm" for call in calls)


def test_reaper_requires_the_exact_management_label_and_a_positive_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "e" * 64

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[2:4] == ["ls", "-aq"]:
            return _completed(args, stdout=f"{container_id}\n")
        if args[2] == "inspect":
            return _completed(args, stdout=_container(container_id, "not-ripple", "2020-01-01T00:00:00Z", managed=False))
        raise AssertionError(f"Unexpected Docker invocation: {args}")

    monkeypatch.setattr("ripple.sandbox.local_docker._run", fake_run)

    assert reap_orphaned_local_docker_sandboxes(set(), min_age_seconds=60).removed == ()
    with pytest.raises(ValueError, match="min_age_seconds must be positive"):
        reap_orphaned_local_docker_sandboxes(set(), min_age_seconds=0)


def test_reaper_reports_docker_remove_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    container_id = "f" * 64

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[2:4] == ["ls", "-aq"]:
            return _completed(args, stdout=f"{container_id}\n")
        if args[2] == "inspect":
            return _completed(args, stdout=_container(container_id, "ripple-sbx-run-stale", "2020-01-01T00:00:00Z"))
        if args[1:3] == ["rm", "-f"]:
            return _completed(args, stdout="busy", returncode=1)
        raise AssertionError(f"Unexpected Docker invocation: {args}")

    monkeypatch.setattr("ripple.sandbox.local_docker._run", fake_run)
    monkeypatch.setattr("ripple.sandbox.local_docker.time.time", lambda: 1_900_000_000)

    result = reap_orphaned_local_docker_sandboxes(set(), min_age_seconds=60)

    assert result.removed == ()
    assert result.failed == ("ripple-sbx-run-stale",)


def test_reaper_preserves_container_owned_by_a_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "1" * 64
    inspected = json.loads(
        _container(container_id, "ripple-sbx-run-owned", "2020-01-01T00:00:00Z")
    )
    inspected[0]["Config"]["Labels"]["ripple.owner_pid"] = "4242"
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        if args[2:4] == ["ls", "-aq"]:
            return _completed(args, stdout=f"{container_id}\n")
        if args[2] == "inspect":
            return _completed(args, stdout=json.dumps(inspected))
        raise AssertionError(f"Unexpected Docker invocation: {args}")

    monkeypatch.setattr("ripple.sandbox.local_docker._run", fake_run)
    monkeypatch.setattr("ripple.sandbox.local_docker._pid_is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr("ripple.sandbox.local_docker.time.time", lambda: 1_900_000_000)

    result = reap_orphaned_local_docker_sandboxes(set(), min_age_seconds=60)

    assert result.skipped_active == ("ripple-sbx-run-owned",)
    assert all(call[1] != "rm" for call in calls)


def test_reaper_preserves_running_legacy_container_without_owner_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "2" * 64
    inspected = json.loads(
        _container(container_id, "ripple-sbx-run-legacy", "2020-01-01T00:00:00Z")
    )
    inspected[0]["State"]["Running"] = True

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[2:4] == ["ls", "-aq"]:
            return _completed(args, stdout=f"{container_id}\n")
        if args[2] == "inspect":
            return _completed(args, stdout=json.dumps(inspected))
        raise AssertionError(f"Unexpected Docker invocation: {args}")

    monkeypatch.setattr("ripple.sandbox.local_docker._run", fake_run)
    monkeypatch.setattr("ripple.sandbox.local_docker.time.time", lambda: 1_900_000_000)

    result = reap_orphaned_local_docker_sandboxes(set(), min_age_seconds=60)

    assert result.skipped_active == ("ripple-sbx-run-legacy",)
