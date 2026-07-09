# RIPPLE RocketRide Pipelines

Local development uses `llm_gemini` because `.env` contains a Gemini key for the self-hosted
engine. The committed `.pipe` files keep `${VAR}` placeholders; `ripple_gateway up` loads
`.env` and substitutes them before calling `RocketRideClient.use()`.

For the RocketRide Cloud swap described in the system design, replace the Gemini LLM nodes
with an Anthropic LLM control node shaped like:

```json
{
  "id": "agent_llm",
  "provider": "llm_anthropic",
  "config": {
    "profile": "claude-sonnet-4",
    "claude-sonnet-4": {
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
