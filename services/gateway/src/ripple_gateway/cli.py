"""Local gateway for starting and calling RIPPLE RocketRide pipelines."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[4]
TASKS_FILE = ROOT / ".ripple_tasks.json"
PIPELINES = {
    "p1": ROOT / "pipelines" / "ripple_ingest.pipe",
    "p2": ROOT / "pipelines" / "ripple_ask.pipe",
    "p3": ROOT / "pipelines" / "ripple_draft.pipe",
}

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def main(argv: list[str] | None = None) -> int:
    """Run the gateway CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    env = _load_env(ROOT / ".env")

    if args.command == "up":
        return asyncio.run(_up(env))
    if args.command == "ask":
        return _ask(env, args.repo_id, args.question, args.intent, args.timeout, mode="ask")
    if args.command == "fix":
        return _ask(env, args.repo_id, args.question, args.intent, args.timeout, mode="fix")
    if args.command == "ingest":
        return _ingest(env, args.repo_url, args.repo_id, args.wipe, args.timeout)
    if args.command == "down":
        return asyncio.run(_down(env))

    parser.print_help()
    return 2


async def _up(env: dict[str, str]) -> int:
    _ensure_rocketride_path()
    from rocketride import RocketRideClient

    client = RocketRideClient(
        uri=env.get("ROCKETRIDE_URI", "ws://localhost:5565"),
        auth=env.get("ROCKETRIDE_APIKEY", "ripple-local-dev-key"),
        env=env,
    )
    await client.connect()
    try:
        tasks: dict[str, dict[str, Any]] = {}
        for key, path in PIPELINES.items():
            pipeline = _load_pipeline(path, env)
            token = await _running_token(client, str(pipeline["project_id"]), str(pipeline["source"]))
            if token is None:
                result = await client.use(
                    pipeline=pipeline,
                    name=path.stem,
                    use_existing=True,
                    env=env,
                    ttl=0,
                )
            else:
                result = {
                    "token": token,
                    "projectId": pipeline["project_id"],
                    "source": pipeline["source"],
                    "reused": True,
                }
            tasks[key] = {
                "token": result["token"],
                "project_id": pipeline["project_id"],
                "source": pipeline["source"],
                "name": path.stem,
                "reused": bool(result.get("reused", False)),
            }
        _write_tasks(tasks)
        print(json.dumps(tasks, indent=2, sort_keys=True))
        return 0
    finally:
        await client.disconnect()


def _ask(
    env: dict[str, str],
    repo_id: str,
    question: str,
    intent: str | None,
    timeout: float,
    *,
    mode: str,
) -> int:
    request_id = uuid.uuid4().hex
    task = {
        "request_id": request_id,
        "repo_id": repo_id,
        "mode": mode,
        "question": question,
        "intent": intent or "",
    }
    answer = _call_pipeline(env, "p2", task, timeout=timeout)
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 0


def _ingest(env: dict[str, str], repo_url: str, repo_id: str, wipe: bool, timeout: float) -> int:
    task = {
        "repo_url": repo_url,
        "repo_id": repo_id,
        "wipe": wipe,
    }
    answer = _call_pipeline(env, "p1", task, timeout=timeout)
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 0


async def _down(env: dict[str, str]) -> int:
    _ensure_rocketride_path()
    from rocketride import RocketRideClient

    client = RocketRideClient(
        uri=env.get("ROCKETRIDE_URI", "ws://localhost:5565"),
        auth=env.get("ROCKETRIDE_APIKEY", "ripple-local-dev-key"),
        env=env,
    )
    await client.connect()
    terminated: dict[str, str] = {}
    try:
        tasks = _read_tasks()
        for key, path in PIPELINES.items():
            token = tasks.get(key, {}).get("token")
            if not token:
                pipeline = _load_pipeline(path, env)
                token = await _running_token(client, str(pipeline["project_id"]), str(pipeline["source"]))
            if not token:
                terminated[key] = "not-running"
                continue
            try:
                await client.terminate(token)
            except Exception as exc:
                terminated[key] = f"error: {exc}"
            else:
                terminated[key] = "terminated"
        if TASKS_FILE.exists():
            TASKS_FILE.unlink()
        print(json.dumps(terminated, indent=2, sort_keys=True))
        return 0
    finally:
        await client.disconnect()


async def _running_token(client: Any, project_id: str, source: str) -> str | None:
    try:
        token = await client.get_task_token(project_id, source)
    except Exception:
        return None
    if not token:
        return None
    try:
        status = await client.get_task_status(token)
    except Exception:
        return None
    if bool(status.get("completed")):
        return None
    return str(token)


def _call_pipeline(
    env: dict[str, str],
    key: str,
    task: dict[str, Any],
    *,
    timeout: float,
) -> Any:
    tasks = _read_tasks()
    token = tasks.get(key, {}).get("token")
    if not token:
        _die(f"No {key} token found. Run `python -m ripple_gateway up` first.")

    url = f"{_http_base(env)}/webhook"
    headers = {
        "Authorization": f"Bearer {env.get('ROCKETRIDE_APIKEY', 'ripple-local-dev-key')}",
        "Content-Type": "lane/questions",
    }
    body = {"questions": [{"text": json.dumps(task, separators=(",", ":"))}]}
    try:
        response = httpx.post(
            url,
            params={"token": token},
            headers=headers,
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException:
        if key != "p2" or task.get("mode") != "fix" or not task.get("request_id"):
            raise
        return _recover_finalized_fix_result(
            env,
            str(task["request_id"]),
            timeout=75.0,
        )
    response.raise_for_status()
    payload = response.json()
    answer = _extract_answer(payload)
    parsed = _parse_answer(answer)
    if key == "p2" and task.get("mode") == "fix" and _is_unverified_fix_draft(parsed):
        return _complete_fix_draft(env, task, parsed, timeout=180.0)
    if key == "p2" and task.get("mode") == "fix" and _is_empty_agent_answer(parsed):
        return _draft_and_verify_fallback(env, task, timeout=180.0)
    return parsed


def _is_empty_agent_answer(answer: Any) -> bool:
    if not isinstance(answer, dict) or any(
        key in answer for key in ("mode", "diff", "verification")
    ):
        return False
    value = str(answer.get("answer") or "").strip()
    return not value or value.startswith("LLM error:")


def _is_unverified_fix_draft(answer: Any) -> bool:
    return (
        isinstance(answer, dict)
        and answer.get("mode") == "fix"
        and isinstance(answer.get("diff_text"), str)
        and bool(answer["diff_text"].strip())
        and isinstance(answer.get("changed_fqns"), list)
        and "verification" not in answer
    )


def _draft_and_verify_fallback(
    env: dict[str, str],
    task: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    return asyncio.run(_draft_and_verify_fallback_async(env, task, timeout=timeout))


async def _draft_and_verify_fallback_async(
    env: dict[str, str],
    task: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    question = str(task.get("question") or "")
    match = re.search(r"[A-Za-z0-9_./-]+\.py:[A-Za-z_][A-Za-z0-9_.]*", question)
    if match is None:
        raise ValueError("RocketRide draft fallback requires a target Python fqn in the question")
    fqn = match.group(0)
    repo_id = str(task.get("repo_id") or "")
    request_id = str(task.get("request_id") or "")
    source = await _mcp_call(
        env,
        "read_function_source",
        {"repo_id": repo_id, "fqn": fqn},
        timeout=15.0,
    )
    prompt = (
        "You are the constrained draft stage inside a RocketRide coding pipeline. "
        "Return one JSON object and no markdown with the key replacement_source. "
        "replacement_source must contain the complete replacement for the target function, "
        "including its signature and body, with no ellipses or surrounding markdown. "
        "Preserve all behavior except the requested minimal change. "
        f"Repository id: {repo_id}. Target fqn: {fqn}. Request: {question}. "
        f"Exact source record: {json.dumps(source, separators=(',', ':'))}"
    )
    draft_response = _call_pipeline(env, "p3", {"prompt": prompt}, timeout=60.0)
    if not isinstance(draft_response, dict):
        raise ValueError("RocketRide draft pipeline returned no JSON object")
    replacement_source = draft_response.get("replacement_source")
    if not isinstance(replacement_source, str) or not replacement_source.strip():
        raise ValueError("RocketRide draft pipeline returned no replacement_source")
    draft = {
        "mode": "fix",
        "repo_id": repo_id,
        "request_id": request_id,
        "diff_text": _build_function_diff(source, replacement_source),
        "changed_fqns": [fqn],
    }
    return await _verify_fix_draft(
        env,
        repo_id,
        request_id,
        str(draft["diff_text"] or ""),
        [str(value) for value in draft["changed_fqns"]],
        timeout=timeout,
    )


def _build_function_diff(source: dict[str, Any], replacement_source: str) -> str:
    file_path = str(source.get("file_path") or "").strip()
    if not file_path or file_path.startswith("/") or ".." in Path(file_path).parts:
        raise ValueError("Function source record contained an invalid file_path")
    if "\n" in file_path or "\r" in file_path:
        raise ValueError("Function source record contained an invalid file_path")

    original_source = source.get("source")
    if not isinstance(original_source, str) or not original_source:
        raise ValueError("Function source record contained no source")
    replacement = replacement_source.strip("\r\n")
    if not replacement or replacement.lstrip().startswith("```"):
        raise ValueError("RocketRide replacement_source was empty or markdown-wrapped")
    if replacement == original_source.strip("\r\n"):
        raise ValueError("RocketRide replacement_source did not change the function")

    start_line = int(source.get("start_line") or 0)
    if start_line < 1:
        raise ValueError("Function source record contained an invalid start_line")
    diff_lines = list(
        difflib.unified_diff(
            original_source.splitlines(),
            replacement.splitlines(),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
    )
    if len(diff_lines) < 3:
        raise ValueError("RocketRide replacement_source produced no patch")

    offset = start_line - 1
    for index, line in enumerate(diff_lines):
        match = re.fullmatch(
            r"@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)",
            line,
        )
        if match is None:
            continue
        old_start = int(match.group(1)) + offset
        new_start = int(match.group(3)) + offset
        diff_lines[index] = (
            f"@@ -{old_start}{match.group(2) or ''} "
            f"+{new_start}{match.group(4) or ''} @@{match.group(5)}"
        )
    return "\n".join(diff_lines) + "\n"


def _complete_fix_draft(
    env: dict[str, str],
    task: dict[str, Any],
    draft: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    request_id = str(task.get("request_id") or "")
    repo_id = str(task.get("repo_id") or "")
    if draft.get("request_id") != request_id or draft.get("repo_id") != repo_id:
        raise ValueError("RocketRide fix draft did not match the active request")
    fqns = [str(fqn) for fqn in draft["changed_fqns"] if str(fqn).strip()]
    if not fqns:
        raise ValueError("RocketRide fix draft did not contain changed_fqns")
    diff_text = str(draft["diff_text"])
    if len(diff_text.encode("utf-8")) > 100_000:
        raise ValueError("RocketRide fix draft exceeded the 100KB verification limit")
    return asyncio.run(
        _verify_fix_draft(
            env,
            repo_id,
            request_id,
            diff_text,
            fqns,
            timeout=timeout,
        )
    )


async def _verify_fix_draft(
    env: dict[str, str],
    repo_id: str,
    request_id: str,
    diff_text: str,
    fqns: list[str],
    *,
    timeout: float,
) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    endpoint = env.get("RIPPLE_MCP_ENDPOINT", "http://127.0.0.1:8790/mcp")
    deadline = asyncio.get_running_loop().time() + timeout
    async with streamable_http_client(endpoint) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            queued = _mcp_result(
                await session.call_tool(
                    "verify_fix",
                    {
                        "repo_id": repo_id,
                        "request_id": request_id,
                        "diff_text": diff_text,
                        "fqns": fqns,
                    },
                )
            )
            job_id = str(queued.get("job_id") or "")
            if not job_id:
                raise RuntimeError(f"Composite verification returned no job_id: {queued}")
            while asyncio.get_running_loop().time() < deadline:
                record = _mcp_result(
                    await session.call_tool("job_status", {"job_id": job_id})
                )
                if record.get("status") == "done":
                    result = record.get("result")
                    if not isinstance(result, dict) or not result.get("passed"):
                        raise RuntimeError(f"RocketRide draft did not pass verification: {result}")
                    return _mcp_result(
                        await session.call_tool(
                            "get_finalized_fix_result",
                            {"request_id": request_id, "wait_seconds": 10},
                        )
                    )
                if record.get("status") == "failed":
                    raise RuntimeError(f"Composite verification failed: {record.get('error')}")
                await asyncio.sleep(0.25)
    raise TimeoutError(f"Composite verification timed out for request_id: {request_id}")


def _mcp_result(response: Any) -> dict[str, Any]:
    if response.isError:
        raise RuntimeError(f"MCP tool failed: {response.content}")
    if isinstance(response.structuredContent, dict):
        return response.structuredContent
    for item in response.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("MCP tool returned no structured result")


async def _mcp_call(
    env: dict[str, str],
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    endpoint = env.get("RIPPLE_MCP_ENDPOINT", "http://127.0.0.1:8790/mcp")

    async def call() -> dict[str, Any]:
        async with streamable_http_client(endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return _mcp_result(await session.call_tool(name, arguments))

    return await asyncio.wait_for(call(), timeout=timeout)


def _recover_finalized_fix_result(
    env: dict[str, str],
    request_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    return asyncio.run(_fetch_finalized_fix_result(env, request_id, timeout=timeout))


async def _fetch_finalized_fix_result(
    env: dict[str, str],
    request_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    endpoint = env.get("RIPPLE_MCP_ENDPOINT", "http://127.0.0.1:8790/mcp")

    async def fetch() -> dict[str, Any]:
        async with streamable_http_client(endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool(
                    "get_finalized_fix_result",
                    {
                        "request_id": request_id,
                        "wait_seconds": max(0.0, min(timeout - 10.0, 60.0)),
                    },
                )
                if response.isError:
                    raise RuntimeError(f"Finalized proof recovery failed: {response.content}")
                if isinstance(response.structuredContent, dict):
                    return response.structuredContent
                for item in response.content:
                    text = getattr(item, "text", None)
                    if isinstance(text, str):
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                raise RuntimeError("Finalized proof recovery returned no structured result")

    return await asyncio.wait_for(fetch(), timeout=timeout)


def _extract_answer(payload: dict[str, Any]) -> Any:
    try:
        answers = payload["data"]["objects"]["body"]["answers"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Webhook response did not contain answers: {payload}") from exc
    if not answers:
        raise ValueError(f"Webhook response answers were empty: {payload}")
    return answers[0]


def _parse_answer(answer: Any) -> Any:
    if isinstance(answer, dict):
        structured = answer.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = answer.get("content")
        if isinstance(content, list):
            for item in content:
                parsed = _parse_answer(item)
                if parsed is not item:
                    return parsed
        for key in ("answer", "text", "content"):
            value = answer.get(key)
            if isinstance(value, str):
                return _parse_answer(value)
        return answer
    if not isinstance(answer, str):
        return answer
    stripped = _strip_code_fence(_strip_text_content_envelope(answer.strip()))
    try:
        return _parse_answer(json.loads(stripped))
    except json.JSONDecodeError:
        return {"answer": answer}


def _strip_text_content_envelope(value: str) -> str:
    # Agent nodes sometimes emit an MCP TextContent rendered as "type: text\ntext: <payload>".
    match = re.match(r"^type:\s*text\s*\ntext:\s*", value)
    if match:
        return value[match.end():].strip()
    return value


def _strip_code_fence(value: str) -> str:
    if not value.startswith("```"):
        return value
    lines = value.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return value


def _load_pipeline(path: Path, env: dict[str, str]) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        pipeline = json.load(handle)
    return _substitute_env(pipeline, env)


def _substitute_env(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {str(k): _substitute_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(item, env) for item in value]
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(lambda match: _env_value(env, match.group(1)), value)
    return value


def _env_value(env: dict[str, str], name: str) -> str:
    if name in env:
        return env[name]
    raise KeyError(f"Missing required environment variable {name}")


def _load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            env.setdefault(key, value)

    defaults = {
        "ROCKETRIDE_URI": "ws://localhost:5565",
        "ROCKETRIDE_APIKEY": "ripple-local-dev-key",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "ripplepass",
        "NEO4J_DATABASE": "neo4j",
        "RIPPLE_MCP_ENDPOINT": "http://127.0.0.1:8790/mcp",
    }
    for key, value in defaults.items():
        env.setdefault(key, value)
    os.environ.update(env)
    return env


def _http_base(env: dict[str, str]) -> str:
    uri = env.get("ROCKETRIDE_URI", "ws://localhost:5565")
    parsed = urlsplit(uri)
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    return urlunsplit((scheme, parsed.netloc, "", "", "")).rstrip("/")


def _write_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    TASKS_FILE.write_text(json.dumps(tasks, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_tasks() -> dict[str, dict[str, Any]]:
    if not TASKS_FILE.exists():
        return {}
    with TASKS_FILE.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _ensure_rocketride_path() -> None:
    rocketride_path = ROOT / "infra" / "rocketride"
    if str(rocketride_path) not in sys.path:
        sys.path.insert(0, str(rocketride_path))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ripple_gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("up", help="Start or reuse both RocketRide pipelines")
    subparsers.add_parser("down", help="Terminate both RocketRide pipelines")

    ask = subparsers.add_parser("ask", help="Ask the P2 RIPPLE agent")
    ask.add_argument("--repo-id", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--intent", default="")
    ask.add_argument("--timeout", type=float, default=180.0)

    fix = subparsers.add_parser("fix", help="Ask the P2 RIPPLE agent to verify a fix")
    fix.add_argument("--repo-id", required=True)
    fix.add_argument("--question", required=True)
    fix.add_argument("--intent", default="")
    fix.add_argument("--timeout", type=float, default=240.0)

    ingest = subparsers.add_parser("ingest", help="Trigger the P1 ingest agent")
    ingest.add_argument("--repo-url", required=True)
    ingest.add_argument("--repo-id", required=True)
    ingest.add_argument("--timeout", type=float, default=180.0)
    ingest.add_argument("--no-wipe", action="store_false", dest="wipe")
    ingest.set_defaults(wipe=True)

    return parser


def _die(message: str) -> NoReturn:
    raise SystemExit(message)
