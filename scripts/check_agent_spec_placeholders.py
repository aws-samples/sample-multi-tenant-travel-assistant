"""Reject deployment-specific values in the tracked AgentCore source files.

`render_agent_spec.py` must materialize live AWS identifiers because the AgentCore
schema has no reference syntax. Those rendered files are deploy inputs, not source:
the repository must retain portable placeholders so a clone never points at another
account's resources.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "agent/MultiTenantTravel/agentcore/agentcore.json"
TARGETS_PATH = REPO_ROOT / "agent/MultiTenantTravel/agentcore/aws-targets.json"
POLICY_DIR = REPO_ROOT / "agent/MultiTenantTravel/app/MultiTenantTravel/policies"
POLICY_PATHS = [
    POLICY_DIR / "budget-iam.json",
    POLICY_DIR / "guardrail-iam.json",
    POLICY_DIR / "model-iam.json",
]
TRACKED_PATHS = [SPEC_PATH, TARGETS_PATH, *POLICY_PATHS]

PLACEHOLDER_ACCOUNT = "000000000000"
PLACEHOLDER_POOL = "us-east-1_Pending00"
PLACEHOLDER_DISCOVERY = (
    f"https://cognito-idp.us-east-1.amazonaws.com/{PLACEHOLDER_POOL}"
    "/.well-known/openid-configuration"
)
PLACEHOLDER_CLIENTS = ["pendingfirstdeploy-web-cli", "pendingfirstdeploy-spa-cl"]
PLACEHOLDER_GATEWAY_ID = "pending-first-deploy"
PLACEHOLDER_GATEWAY_URL = (
    f"https://{PLACEHOLDER_GATEWAY_ID}.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
)

ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def _first(items: list[dict[str, Any]], label: str, errors: list[str]) -> dict[str, Any]:
    if items:
        return items[0]
    errors.append(f"agentcore.json has no {label}")
    return {}


def validation_errors() -> list[str]:
    errors: list[str] = []
    spec = json.loads(SPEC_PATH.read_text())
    runtime = _first(spec.get("runtimes") or [], "runtime", errors)
    gateway = _first(spec.get("agentCoreGateways") or [], "gateway", errors)

    env = {item.get("name"): item.get("value") for item in runtime.get("envVars") or []}
    if env.get("GATEWAY_MCP_URL") != PLACEHOLDER_GATEWAY_URL:
        errors.append("runtime GATEWAY_MCP_URL is not the pending-first-deploy placeholder")

    if runtime.get("networkMode") != "PUBLIC" or "networkConfig" in runtime:
        errors.append("runtime network configuration contains deploy-rendered VPC values")

    for label, holder in (("runtime", runtime), ("gateway", gateway)):
        authorizer = (holder.get("authorizerConfiguration") or {}).get("customJwtAuthorizer") or {}
        if authorizer.get("discoveryUrl") != PLACEHOLDER_DISCOVERY:
            errors.append(f"{label} discoveryUrl is not the placeholder Cognito pool")
        if authorizer.get("allowedClients") != PLACEHOLDER_CLIENTS:
            errors.append(f"{label} allowedClients are not the placeholder client ids")

    gateway_targets = gateway.get("targets") or []
    if not gateway_targets:
        errors.append("agentcore.json has no Gateway targets")
    for target in gateway_targets:
        arn = ((target.get("lambdaFunctionArn") or {}).get("lambdaArn")) or ""
        parts = arn.split(":")
        if len(parts) < 5 or parts[4] != PLACEHOLDER_ACCOUNT:
            errors.append(f"gateway target {target.get('name', '<unnamed>')} has a live Lambda ARN")

    statements = [
        policy.get("statement", "")
        for engine in spec.get("policyEngines") or []
        for policy in engine.get("policies") or []
    ]
    expected_gateway = (
        f"arn:aws:bedrock-agentcore:us-east-1:{PLACEHOLDER_ACCOUNT}:"
        f"gateway/{PLACEHOLDER_GATEWAY_ID}"
    )
    if not statements:
        errors.append("agentcore.json has no Cedar policy statements")
    elif any(expected_gateway not in statement for statement in statements):
        errors.append("a Cedar statement does not use the placeholder gateway ARN")

    targets = json.loads(TARGETS_PATH.read_text())
    if not targets:
        errors.append("aws-targets.json has no deployment target")
    for target in targets:
        name = target.get("name", "<unnamed>")
        if target.get("account") != PLACEHOLDER_ACCOUNT:
            errors.append(f"aws-targets.json target {name} contains a live account id")

    budget = (POLICY_DIR / "budget-iam.json").read_text()
    if "{{REGION}}" not in budget or "{{ACCOUNT}}" not in budget:
        errors.append("budget-iam.json does not retain its region/account placeholders")

    for path in TRACKED_PATHS:
        for account in sorted(set(ACCOUNT_ID.findall(path.read_text()))):
            if account != PLACEHOLDER_ACCOUNT:
                errors.append(f"{path.relative_to(REPO_ROOT)} contains live account id {account}")

    return errors


def main() -> int:
    errors = validation_errors()
    if errors:
        print("AgentCore source files contain deployment-rendered values:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("AgentCore source files contain portable placeholders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
