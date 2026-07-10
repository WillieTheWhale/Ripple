from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ripple_gateway import cli
from ripple_gateway.cli import _parse_answer


def test_parse_answer_unwraps_json_encoded_mcp_structured_content() -> None:
    structured = {
        "mode": "fix",
        "diff": "diff --git a/a.py b/a.py\n",
        "verification": {"passed": True},
    }
    wrapped = {
        "content": [{"type": "text", "text": json.dumps(structured)}],
        "isError": False,
        "structuredContent": structured,
    }

    assert _parse_answer(json.dumps(wrapped)) == structured


def test_parse_answer_falls_back_to_text_content_list() -> None:
    structured = {"mode": "fix", "verification": {"passed": True}}
    wrapped = {"content": [{"type": "text", "text": json.dumps(structured)}], "isError": False}

    assert _parse_answer(wrapped) == structured


def test_fix_pipeline_uses_rocketride_draft_fallback_for_empty_agent_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = {"mode": "fix", "diff": "diff\n", "verification": {"passed": True}}
    monkeypatch.setattr(cli, "_read_tasks", lambda: {"p2": {"token": "task-token"}})
    monkeypatch.setattr(cli, "_http_base", lambda _env: "http://rocketride.test")
    monkeypatch.setattr(
        cli.httpx,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"objects": {"body": {"answers": [""]}}}},
        ),
    )
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "_draft_and_verify_fallback",
        lambda _env, task, **_kwargs: observed.append(task) or proof,
    )
    task = {"mode": "fix", "request_id": "request_1234567890"}
    result = cli._call_pipeline({}, "p2", task, timeout=30)

    assert result == proof
    assert observed == [task]


def test_fix_pipeline_recovers_request_bound_proof_after_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = {"mode": "fix", "diff": "diff\n", "verification": {"passed": True}}
    monkeypatch.setattr(cli, "_read_tasks", lambda: {"p2": {"token": "task-token"}})
    monkeypatch.setattr(cli, "_http_base", lambda _env: "http://rocketride.test")

    def time_out(*_args: object, **_kwargs: object) -> None:
        raise cli.httpx.ReadTimeout("agent response stalled")

    monkeypatch.setattr(cli.httpx, "post", time_out)
    observed: list[str] = []
    monkeypatch.setattr(
        cli,
        "_recover_finalized_fix_result",
        lambda _env, request_id, **_kwargs: observed.append(request_id) or proof,
    )

    result = cli._call_pipeline(
        {},
        "p2",
        {"mode": "fix", "request_id": "request_1234567890"},
        timeout=30,
    )

    assert result == proof
    assert observed == ["request_1234567890"]


def test_fix_pipeline_completes_a_request_matched_structured_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = {"mode": "fix", "diff": "diff\n", "verification": {"passed": True}}
    draft = {
        "mode": "fix",
        "repo_id": "repo-a",
        "request_id": "request_1234567890",
        "diff_text": "diff --git a/a.py b/a.py\n",
        "changed_fqns": ["a.py:f"],
    }
    monkeypatch.setattr(cli, "_read_tasks", lambda: {"p2": {"token": "task-token"}})
    monkeypatch.setattr(cli, "_http_base", lambda _env: "http://rocketride.test")
    monkeypatch.setattr(
        cli.httpx,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"objects": {"body": {"answers": [draft]}}}},
        ),
    )
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "_complete_fix_draft",
        lambda _env, task, answer, **_kwargs: observed.extend([task, answer]) or proof,
    )
    task = {
        "mode": "fix",
        "repo_id": "repo-a",
        "request_id": "request_1234567890",
    }

    assert cli._call_pipeline({}, "p2", task, timeout=30) == proof
    assert observed == [task, draft]


def test_draft_fallback_builds_diff_from_replacement_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {
        "repo_id": "repo-a",
        "fqn": "src/a.py:compute_label",
        "file_path": "src/a.py",
        "start_line": 20,
        "end_line": 22,
        "source": (
            "def compute_label(value: int) -> str:\n"
            '    """Process and format a value."""\n'
            "    return format_value(value)"
        ),
    }

    async def mcp_call(*_args: object, **_kwargs: object) -> dict[str, object]:
        return source

    observed: dict[str, object] = {}

    async def verify(
        _env: dict[str, str],
        _repo_id: str,
        _request_id: str,
        diff_text: str,
        fqns: list[str],
        **_kwargs: object,
    ) -> dict[str, object]:
        observed.update(diff_text=diff_text, fqns=fqns)
        return {"verification": {"passed": True}}

    monkeypatch.setattr(cli, "_mcp_call", mcp_call)
    monkeypatch.setattr(
        cli,
        "_call_pipeline",
        lambda *_args, **_kwargs: {
            "replacement_source": (
                "def compute_label(value: int) -> str:\n"
                '    """Format a processed value."""\n'
                "    return format_value(value)"
            )
        },
    )
    monkeypatch.setattr(cli, "_verify_fix_draft", verify)

    result = asyncio.run(
        cli._draft_and_verify_fallback_async(
            {},
            {
                "repo_id": "repo-a",
                "request_id": "request_1234567890",
                "question": "Change src/a.py:compute_label docstring.",
            },
            timeout=30,
        )
    )

    assert result == {"verification": {"passed": True}}
    assert observed["fqns"] == ["src/a.py:compute_label"]
    assert observed["diff_text"] == (
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -20,3 +20,3 @@\n"
        " def compute_label(value: int) -> str:\n"
        '-    """Process and format a value."""\n'
        '+    """Format a processed value."""\n'
        "     return format_value(value)\n"
    )
