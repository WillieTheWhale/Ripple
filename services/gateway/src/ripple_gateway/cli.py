"""Local gateway for starting and calling RIPPLE RocketRide pipelines."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[4]
TASKS_FILE = ROOT / ".ripple_tasks.json"
PIPELINES = {
    "p1": ROOT / "pipelines" / "ripple_ingest.pipe",
    "p2": ROOT / "pipelines" / "ripple_ask.pipe",
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
        return _ask(env, args.repo_id, args.question, args.intent, args.timeout)
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


def _ask(env: dict[str, str], repo_id: str, question: str, intent: str | None, timeout: float) -> int:
    task = {
        "repo_id": repo_id,
        "mode": "ask",
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
    response = httpx.post(url, params={"token": token}, headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    answer = _extract_answer(payload)
    return _parse_answer(answer)


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
        for key in ("answer", "text", "content"):
            value = answer.get(key)
            if isinstance(value, str):
                return _parse_answer(value)
        return answer
    if not isinstance(answer, str):
        return answer
    stripped = _strip_code_fence(answer.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"answer": answer}


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

    ingest = subparsers.add_parser("ingest", help="Trigger the P1 ingest agent")
    ingest.add_argument("--repo-url", required=True)
    ingest.add_argument("--repo-id", required=True)
    ingest.add_argument("--timeout", type=float, default=180.0)
    ingest.add_argument("--no-wipe", action="store_false", dest="wipe")
    ingest.set_defaults(wipe=True)

    return parser


def _die(message: str) -> NoReturn:
    raise SystemExit(message)
