# RIPPLE RocketRide Pipelines

Local development runs the Wave agent in RocketRide and uses RocketRide's
`llm_openai_api` node for authenticated GitHub Models inference. The committed `.pipe`
files keep `${VAR}` placeholders; `ripple_gateway up` loads `.env` and substitutes them
before calling `RocketRideClient.use()`.

The local model is `openai/gpt-4.1-nano` at `https://models.github.ai/inference`, authenticated
with `ROCKETRIDE_GITHUB_TOKEN`. GitHub Models is only the LLM endpoint: RocketRide remains
the orchestration runtime, Wave planner, memory owner, tool router, and response pipeline.
`ripple_draft.pipe` uses the separately metered `openai/gpt-4o-mini` GitHub
Models profile as a bounded RocketRide fallback. When the Wave planner returns neither proof
nor a structured draft, the gateway reads exact source through RIPPLE MCP, asks this
single-purpose RocketRide pipeline for replacement function source, constructs the unified
diff deterministically, and submits it to the same composite Daytona proof contract.
P2 uses RIPPLE's read-only, parameterized MCP graph tools rather than the generic
`db_neo4j` NL-to-Cypher surface so the complete Wave planning prompt stays below GitHub
Models' free 8,000-token request cap. Those tools still query Neo4j and return the exact
Cypher for transparency.

Fix-mode completion uses RocketRide keyed memory: after `finalize_fix_result`, the Wave agent
returns that tool result through a `memory.ref` JSON formatter. This keeps the proof payload
verbatim and prevents a final model rewrite from dropping verification fields. The gateway
also has a request-bound recovery path for a passed verification if a hosted model returns
an empty final message or its response transport times out.

For the RocketRide Cloud swap described in the system design, replace the GitHub Models nodes
with an Anthropic LLM control node shaped like:

```json
{
  "id": "agent_llm",
  "provider": "llm_anthropic",
  "config": {
    "profile": "claude-sonnet-4-6",
    "claude-sonnet-4-6": {
      "apikey": "${ROCKETRIDE_ANTHROPIC_KEY}"
    },
    "parameters": {}
  },
  "control": [
    {
      "classType": "llm",
      "from": "agent"
    }
  ]
}
```
