# Generated AgentCore CDK application

The AgentCore CLI owns this CDK application and reads the project specification from the parent
`agentcore/` directory.

The committed specification contains account and resource placeholders, so do not run `cdk deploy`
or `agentcore deploy` directly from this directory. The repository root's `deploy.sh` renders the
live specification and performs the required infrastructure, policy, and Gateway wiring in order.

The only standalone maintenance commands expected here are:

```bash
npm ci
npx --no-install tsc --noEmit
```

Both are included in the root `test.sh`.
