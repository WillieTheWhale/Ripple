#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

infra/cognee/run.sh up
set -a
source .env
set +a

PYTHONPATH=core/src .venv/bin/python <<'PY'
import asyncio

from ripple.memory import CogneeClient, CogneeClientError


async def main() -> None:
    dataset = "ripple-phase5-smoke"
    async with CogneeClient.from_env() as client:
        try:
            await client.forget(dataset)
        except CogneeClientError as exc:
            if exc.status_code != 404:
                raise

        await client.remember(
            dataset,
            "Decision: token errors return 401 with WWW-Authenticate; security/oauth2.py is fragile.",
        )
        hits = await client.recall(dataset, "What did we decide about token errors?")
        text = "\n".join(item["text"] for item in hits)
        assert "401" in text and "WWW-Authenticate" in text

        improved = await client.improve(dataset)
        assert improved.supported
        await client.forget(dataset)
        assert await client.recall(dataset, "token errors") == []
        print({"remembered": True, "recall_hits": len(hits), "improved": True, "forgotten": True})


asyncio.run(main())
PY

echo "Phase 5 authenticated Cognee smoke passed"
