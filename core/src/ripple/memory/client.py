"""Async, repository-scoped client for Cognee's REST API.

The defaults target Cognee 1.2.2. :class:`CogneeRoutes` keeps an image upgrade
or a reverse-proxy route change to configuration rather than application code.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict

import httpx


LOGGER = logging.getLogger(__name__)


class RecallItem(TypedDict):
    """A normalized memory hit returned by :meth:`CogneeClient.recall`."""

    text: str
    score: NotRequired[float]


@dataclass(frozen=True, slots=True)
class CogneeRoutes:
    """REST paths for the current Cognee deployment.

    Set ``improve_path`` to ``None`` for an image with no compatible improve
    endpoint. Environment overrides are deliberately path-only: credentials
    remain separate from route configuration.
    """

    remember_path: str = "/api/v1/remember"
    recall_path: str = "/api/v1/recall"
    forget_path: str = "/api/v1/forget"
    improve_path: str | None = "/api/v1/improve"
    login_path: str = "/api/v1/auth/login"

    @classmethod
    def from_env(cls) -> CogneeRoutes:
        """Load endpoint overrides without embedding deployment details in code."""
        defaults = cls()
        improve_path = os.environ.get("RIPPLE_COGNEE_IMPROVE_PATH", defaults.improve_path)
        return cls(
            remember_path=os.environ.get("RIPPLE_COGNEE_REMEMBER_PATH", defaults.remember_path),
            recall_path=os.environ.get("RIPPLE_COGNEE_RECALL_PATH", defaults.recall_path),
            forget_path=os.environ.get("RIPPLE_COGNEE_FORGET_PATH", defaults.forget_path),
            improve_path=improve_path or None,
            login_path=os.environ.get("RIPPLE_COGNEE_LOGIN_PATH", defaults.login_path),
        )


@dataclass(frozen=True, slots=True)
class CogneeClientConfig:
    """Connection settings for one Cognee API deployment.

    Authentication may use a pre-issued bearer token, an ``X-Api-Key``, or the
    native username/password login endpoint. Secrets are read only from the
    process environment and never included in errors or logs.
    """

    base_url: str = "http://127.0.0.1:8890"
    bearer_token: str | None = None
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    timeout_seconds: float = 30.0
    routes: CogneeRoutes = field(default_factory=CogneeRoutes)

    @classmethod
    def from_env(cls) -> CogneeClientConfig:
        """Build configuration from ``RIPPLE_COGNEE_*`` environment variables."""
        defaults = cls()
        timeout_value = os.environ.get("RIPPLE_COGNEE_TIMEOUT_SECONDS", "30")
        try:
            timeout_seconds = float(timeout_value)
        except ValueError as exc:
            raise ValueError("RIPPLE_COGNEE_TIMEOUT_SECONDS must be a number") from exc
        return cls(
            base_url=os.environ.get("RIPPLE_COGNEE_BASE_URL", defaults.base_url),
            bearer_token=os.environ.get("RIPPLE_COGNEE_BEARER_TOKEN") or None,
            api_key=(
                os.environ.get("RIPPLE_COGNEE_API_KEY")
                or os.environ.get("COGNEE_API_KEY")
                or None
            ),
            username=(
                os.environ.get("RIPPLE_COGNEE_USERNAME")
                or os.environ.get("COGNEE_USER")
                or None
            ),
            password=(
                os.environ.get("RIPPLE_COGNEE_PASSWORD")
                or os.environ.get("COGNEE_PASSWORD")
                or None
            ),
            timeout_seconds=timeout_seconds,
            routes=CogneeRoutes.from_env(),
        )


class CogneeClientError(RuntimeError):
    """A sanitized transport or HTTP failure from Cognee."""

    def __init__(self, operation: str, message: str, *, status_code: int | None = None) -> None:
        self.operation = operation
        self.status_code = status_code
        super().__init__(f"Cognee {operation} failed: {message}")


@dataclass(frozen=True, slots=True)
class RememberResult:
    """Confirmation that a note was stored in one repository dataset."""

    dataset: str
    response: object | None


@dataclass(frozen=True, slots=True)
class ForgetResult:
    """Confirmation that a repository dataset was forgotten."""

    dataset: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class ImproveResult:
    """Outcome of a dataset improve request.

    ``supported=False`` means the configured Cognee image did not expose a
    compatible improve route.
    """

    dataset: str
    supported: bool
    response: object | None = None


class CogneeClient:
    """Thin async adapter around Cognee's dataset-aware REST endpoints."""

    def __init__(
        self,
        config: CogneeClientConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or CogneeClientConfig.from_env()
        self._validate_config(self.config)
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["X-Api-Key"] = self.config.api_key
        self._bearer_token = self.config.bearer_token
        self._login_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=self.config.timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> CogneeClient:
        """Create a client configured entirely through process environment variables."""
        return cls(CogneeClientConfig.from_env())

    async def __aenter__(self) -> CogneeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def remember(
        self,
        repo_id: str,
        text: str,
        meta: dict[str, object] | None = None,
    ) -> RememberResult:
        """Store one text note in the dataset named by ``repo_id``.

        Cognee 1.2.2 accepts uploaded files for ``remember`` rather than a
        JSON metadata field. ``meta`` remains for caller compatibility but is
        not transmitted because the endpoint has no metadata form field.
        """
        dataset = self._dataset(repo_id)
        if not text.strip():
            raise ValueError("text must not be empty")
        if meta is not None:
            LOGGER.debug("Cognee remember metadata is not sent by the native REST endpoint")
        response = await self._request_json(
            "remember",
            "POST",
            self.config.routes.remember_path,
            form_data={"datasetName": dataset, "run_in_background": "false"},
            files={"data": ("memory-note.txt", text.encode("utf-8"), "text/plain")},
        )
        return RememberResult(dataset, response)

    async def recall(self, repo_id: str, query: str) -> list[RecallItem]:
        """Recall from one dataset and normalize Cognee's heterogeneous hits."""
        dataset = self._dataset(repo_id)
        if not query.strip():
            raise ValueError("query must not be empty")
        try:
            response = await self._request_json(
                "recall",
                "POST",
                self.config.routes.recall_path,
                json_payload={
                    "datasets": [dataset],
                    "query": query,
                    "searchType": "GRAPH_COMPLETION",
                    "topK": 10,
                    "includeReferences": True,
                },
            )
        except CogneeClientError as exc:
            if exc.status_code != 404:
                raise
            return []
        return _recall_items(response)

    async def forget(self, repo_id: str) -> ForgetResult:
        """Forget the complete, isolated dataset for ``repo_id``."""
        dataset = self._dataset(repo_id)
        for attempt in range(3):
            try:
                await self._request_json(
                    "forget",
                    "POST",
                    self.config.routes.forget_path,
                    json_payload={"dataset": dataset},
                    allow_empty=True,
                )
                break
            except CogneeClientError as exc:
                if exc.status_code not in {409, 500, 503} or attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        return ForgetResult(dataset=dataset, deleted=True)

    async def improve(self, repo_id: str) -> ImproveResult:
        """Improve a dataset, or report unavailable support without failing a job."""
        dataset = self._dataset(repo_id)
        path = self.config.routes.improve_path
        if path is None:
            return ImproveResult(dataset=dataset, supported=False)
        try:
            response = await self._request_json(
                "improve",
                "POST",
                path,
                json_payload={"datasetName": dataset, "runInBackground": False},
            )
        except CogneeClientError as exc:
            if exc.status_code not in {404, 405, 501}:
                raise
            LOGGER.info("Cognee improve is unavailable for dataset=%s", dataset)
            return ImproveResult(dataset=dataset, supported=False)
        return ImproveResult(dataset=dataset, supported=True, response=response)

    async def login(self) -> None:
        """Exchange configured username/password credentials for a bearer token."""
        if not self.config.username or not self.config.password:
            raise ValueError("username and password are required for login")
        response = await self._request_json(
            "login",
            "POST",
            self.config.routes.login_path,
            form_data={"username": self.config.username, "password": self.config.password},
            authenticate=False,
        )
        if not isinstance(response, dict):
            raise CogneeClientError("login", "response did not contain a bearer token")
        token = response.get("access_token") or response.get("token")
        if not isinstance(token, str) or not token:
            raise CogneeClientError("login", "response did not contain a bearer token")
        self._bearer_token = token

    async def _ensure_authenticated(self) -> None:
        if self._bearer_token or self.config.api_key or not self.config.username:
            return
        async with self._login_lock:
            if not self._bearer_token:
                await self.login()

    @staticmethod
    def _validate_config(config: CogneeClientConfig) -> None:
        if not config.base_url.strip():
            raise ValueError("base_url must not be empty")
        if config.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if bool(config.username) != bool(config.password):
            raise ValueError("username and password must be provided together")

    @staticmethod
    def _dataset(repo_id: str) -> str:
        dataset = repo_id.strip()
        if not dataset:
            raise ValueError("repo_id must not be empty")
        return dataset

    async def _request_json(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_payload: dict[str, object] | None = None,
        form_data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        allow_empty: bool = False,
        authenticate: bool = True,
    ) -> object | None:
        if authenticate:
            await self._ensure_authenticated()
        headers = {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else None
        try:
            response = await self._client.request(
                method,
                path,
                json=json_payload,
                data=form_data,
                files=files,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            LOGGER.warning("Cognee %s request could not be completed", operation)
            raise CogneeClientError(operation, "request could not be completed") from exc
        if response.is_error:
            LOGGER.warning("Cognee %s request failed with status=%s", operation, response.status_code)
            raise CogneeClientError(
                operation,
                f"HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if allow_empty and not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            LOGGER.warning("Cognee %s returned a non-JSON response", operation)
            raise CogneeClientError(operation, "response was not valid JSON") from exc


def _recall_items(response: object | None) -> list[RecallItem]:
    """Normalize graph, context, and question-answer response entries safely."""
    results: list[RecallItem] = []
    for candidate in _search_candidates(response):
        if isinstance(candidate, str):
            results.append({"text": candidate})
            continue
        if not isinstance(candidate, dict):
            continue
        text = next(
            (
                candidate[key]
                for key in ("text", "content", "answer", "context", "result")
                if isinstance(candidate.get(key), str)
            ),
            None,
        )
        if text is None:
            continue
        item: RecallItem = {"text": text}
        score = next(
            (
                candidate[key]
                for key in ("score", "similarity", "relevance_score")
                if isinstance(candidate.get(key), (int, float))
                and not isinstance(candidate.get(key), bool)
            ),
            None,
        )
        if score is not None:
            item["score"] = float(score)
        results.append(item)
    return results


def _search_candidates(response: object | None) -> list[object]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []
    for key in ("results", "data", "items", "result"):
        value = response.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _search_candidates(value)
            if nested:
                return nested
    return []
