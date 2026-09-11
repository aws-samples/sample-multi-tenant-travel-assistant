# Agent runtime

The Strands application deployed to Amazon Bedrock AgentCore Runtime.

## Main components

- `main.py` — streaming entry point and request lifecycle
- `model/` — model construction and guardrail configuration
- `prompts/` — system prompt assembled from focused partials
- `mcp_client/` — Gateway client used to discover and invoke tools
- `memory.py` — short- and long-term memory integration
- `ledger.py`, `pricing.py`, `metrics.py` — per-turn usage, cost, and operational metrics
- `stream.py` — event stream contract consumed by the conversation API

## Local checks

From this directory:

```bash
uv run --frozen --group dev python -m pytest ../../../tests
```

The complete repository suite is `../../../../test.sh`.

## Deployment

The committed AgentCore specification contains placeholders and is not directly deployable. Run
`../../../../deploy.sh` from the repository root so supporting infrastructure, rendered references,
Gateway policies, and the runtime are deployed in the required order.
