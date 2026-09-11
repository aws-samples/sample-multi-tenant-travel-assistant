# AgentCore project

This directory contains the AgentCore-managed half of the sample: the runtime application, Gateway
targets, memory, evaluators, and Cedar policies.

## Layout

- `app/MultiTenantTravel/` — Strands runtime application, prompts, memory integration, and cost ledger
- `policies/` — Cedar authorization policies synchronized to the deployed Gateway
- `agentcore/agentcore.json` — declarative AgentCore project specification
- `agentcore/aws-targets.json` — placeholder deployment target rendered for the active account
- `agentcore/.llm-context/` — generated schema types used to validate the specification
- `agentcore/cdk/` — generated CDK application driven by the AgentCore CLI

The committed specification deliberately contains `000000000000`, `Pending00`, and
`pending-first-deploy` placeholders. The root deployment script renders account-specific values
immediately before invoking the AgentCore CLI.

## Development

Run the repository's offline checks from the root:

```bash
./test.sh
```

The runtime's Python tests can also be run directly:

```bash
cd app/MultiTenantTravel
uv run --frozen --group dev python -m pytest ../../../tests
```

## Deployment

Do not run `agentcore deploy` or the generated CDK application directly against the committed
placeholder configuration. Use the root workflow:

```bash
cd ../..
./deploy.sh --seed
```

`deploy.sh` renders the specification, deploys the supporting infrastructure, synchronizes Cedar
policies, configures the Gateway interceptor, and verifies that no placeholder reached the live
deployment.
