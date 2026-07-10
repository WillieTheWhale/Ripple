"""Transport-mocked coverage for the Cognee 1.2.2 REST memory client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import TypeVar

import httpx
import pytest

from ripple.memory import CogneeClient, CogneeClientConfig, CogneeClientError, CogneeRoutes


T = TypeVar("T")


def _run(coroutine: Coroutine[object, object, T]) -> T:
    return asyncio.run(coroutine)


def _json(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)


def test_remember_posts_native_multipart_to_the_repo_dataset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/remember"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        body = request.content.decode("utf-8")
        assert 'name="datasetName"\r\n\r\ngithub.com/acme/api\r\n' in body
        assert 'name="run_in_background"\r\n\r\nfalse\r\n' in body
        assert 'name="data"; filename="memory-note.txt"' in body
        assert "Decision: preserve WWW-Authenticate." in body
        assert "kind" not in body
        return httpx.Response(200, json={"id": "note-7"})

    async def exercise() -> object:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(handler),
        ) as client:
            return await client.remember(
                "github.com/acme/api",
                "Decision: preserve WWW-Authenticate.",
                {"kind": "decision"},
            )

    result = _run(exercise())

    assert result.dataset == "github.com/acme/api"
    assert result.response == {"id": "note-7"}


def test_recall_uses_native_payload_and_normalizes_heterogeneous_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/recall"
        assert _json(request) == {
            "datasets": ["repo-a"],
            "query": "What did we decide about token errors?",
            "searchType": "GRAPH_COMPLETION",
            "topK": 10,
            "includeReferences": True,
        }
        return httpx.Response(
            200,
            json=[
                {"text": "Token errors return 401.", "score": 0.91, "dataset_name": "repo-a"},
                {"content": "security/oauth2.py is fragile"},
                {"answer": "Preserve WWW-Authenticate.", "context": "auth middleware"},
                {"context": "fallback context"},
                {"score": True},
            ],
        )

    async def exercise() -> list[dict[str, object]]:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(handler),
        ) as client:
            return await client.recall("repo-a", "What did we decide about token errors?")

    assert _run(exercise()) == [
        {"text": "Token errors return 401.", "score": 0.91},
        {"text": "security/oauth2.py is fragile"},
        {"text": "Preserve WWW-Authenticate."},
        {"text": "fallback context"},
    ]


def test_forget_and_improve_use_native_json_bodies() -> None:
    def forget_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/forget"
        assert _json(request) == {"dataset": "org/repo"}
        return httpx.Response(204)

    async def forget_exercise() -> object:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(forget_handler),
        ) as client:
            return await client.forget("org/repo")

    forgotten = _run(forget_exercise())
    assert forgotten.dataset == "org/repo"
    assert forgotten.deleted is True

    def improve_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/improve"
        assert _json(request) == {"datasetName": "org/repo", "runInBackground": False}
        return httpx.Response(200, json={"updated": 3})

    async def improve_exercise() -> object:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(improve_handler),
        ) as client:
            return await client.improve("org/repo")

    improved = _run(improve_exercise())
    assert improved.supported is True
    assert improved.response == {"updated": 3}


def test_forget_retries_a_transient_dataset_lock() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500 if attempts == 1 else 200, json={"status": "success"})

    async def exercise() -> object:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(handler),
        ) as client:
            return await client.forget("repo-a")

    assert _run(exercise()).deleted is True
    assert attempts == 2


def test_improve_reports_missing_endpoint_as_unsupported() -> None:
    async def exercise() -> object:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ) as client:
            return await client.improve("repo-a")

    result = _run(exercise())

    assert result.supported is False
    assert result.response is None


def test_bearer_api_key_and_login_authentication_are_supported() -> None:
    async def bearer_exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer bearer-token"
            assert "X-Api-Key" not in request.headers
            return httpx.Response(200, json=[])

        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test", bearer_token="bearer-token"),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.recall("repo-a", "token errors")

    _run(bearer_exercise())

    async def api_key_exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Api-Key"] == "api-key"
            assert "Authorization" not in request.headers
            return httpx.Response(200, json=[])

        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test", api_key="api-key"),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.recall("repo-a", "token errors")

    _run(api_key_exercise())

    requests: list[httpx.Request] = []

    def login_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/login":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert request.content == b"username=user&password=pass"
            return httpx.Response(200, json={"access_token": "login-token"})
        assert request.headers["Authorization"] == "Bearer login-token"
        return httpx.Response(200, json=[])

    async def login_exercise() -> None:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test", username="user", password="pass"),
            transport=httpx.MockTransport(login_handler),
        ) as client:
            await client.recall("repo-a", "token errors")

    _run(login_exercise())
    assert [request.url.path for request in requests] == ["/api/v1/auth/login", "/api/v1/recall"]


def test_http_errors_do_not_expose_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "secret server diagnostic"})

    async def exercise() -> None:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test", bearer_token="test-token"),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.recall("repo-a", "token errors")

    with pytest.raises(CogneeClientError, match="Cognee recall failed: HTTP 401") as exc_info:
        _run(exercise())

    assert exc_info.value.status_code == 401
    assert "secret server diagnostic" not in str(exc_info.value)
    assert "test-token" not in str(exc_info.value)


def test_recall_returns_empty_for_a_missing_or_forgotten_dataset() -> None:
    async def exercise() -> list[dict[str, object]]:
        async with CogneeClient(
            CogneeClientConfig(base_url="http://cognee.test"),
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ) as client:
            return await client.recall("forgotten-repo", "what did we decide?")

    assert _run(exercise()) == []


def test_environment_configuration_allows_native_route_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIPPLE_COGNEE_BASE_URL", "http://memory.internal:8891/")
    monkeypatch.setenv("RIPPLE_COGNEE_REMEMBER_PATH", "/v2/remember")
    monkeypatch.setenv("RIPPLE_COGNEE_IMPROVE_PATH", "")
    monkeypatch.setenv("RIPPLE_COGNEE_TIMEOUT_SECONDS", "12.5")

    config = CogneeClientConfig.from_env()

    assert config.base_url == "http://memory.internal:8891/"
    assert config.routes == CogneeRoutes(remember_path="/v2/remember", improve_path=None)
    assert config.timeout_seconds == 12.5


def test_environment_configuration_accepts_root_service_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNEE_API_KEY", "service-key")
    monkeypatch.setenv("COGNEE_USER", "service@ripple.dev")
    monkeypatch.setenv("COGNEE_PASSWORD", "password")

    config = CogneeClientConfig.from_env()

    assert config.api_key == "service-key"
    assert config.username == "service@ripple.dev"
    assert config.password == "password"
