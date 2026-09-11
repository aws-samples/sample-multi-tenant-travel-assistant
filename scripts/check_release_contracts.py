"""Fail when release-facing instructions drift from the files that actually execute them.

These are deliberately small static contracts. They cover facts that changed without breaking code:
the AgentCore CLI install command, the generated CDK version pair, the deployed verification
inventory, and mutable GitHub Action tags.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _agentcore_pin() -> None:
    deploy = _text("deploy.sh")
    readme = _text("README.md")
    match = re.search(r'^REQUIRED_AGENTCORE_VERSION="([^"]+)"$', deploy, re.MULTILINE)
    _require(match is not None, "deploy.sh must declare REQUIRED_AGENTCORE_VERSION")
    assert match is not None  # narrowed for type checkers after the executable guard above
    version = match.group(1)
    _require(
        f"`npm install -g @aws/agentcore@{version}`" in readme,
        "README AgentCore install command must use deploy.sh's exact version",
    )
    _require(
        '[[ "$AGENTCORE_VERSION" != "$REQUIRED_AGENTCORE_VERSION" ]]' in deploy,
        "deploy.sh must reject an unexpected AgentCore CLI version",
    )


def _cdk_pair() -> None:
    infra = json.loads(_text("infra/package.json"))
    agent = json.loads(_text("agent/MultiTenantTravel/agentcore/cdk/package.json"))
    infra_version = infra["dependencies"]["aws-cdk-lib"]
    agent_version = agent["dependencies"]["aws-cdk-lib"]
    _require(
        infra_version == agent_version,
        f"CDK apps disagree on aws-cdk-lib: infra={infra_version}, agent={agent_version}",
    )


def _verification_inventory() -> None:
    test_script = _text("test.sh")
    verify_scripts = sorted((ROOT / "scripts").glob("verify_*.py"))
    missing = [
        str(path.relative_to(ROOT)) for path in verify_scripts if path.name not in test_script
    ]
    _require(
        not missing,
        f"test.sh's deployed-verification inventory omits: {', '.join(missing)}",
    )


def _action_pins() -> None:
    workflow = _text(".github/workflows/ci.yml")
    mutable = []
    for action in re.findall(r"^\s*-\s+uses:\s+([^\s#]+)", workflow, re.MULTILINE):
        if action.startswith("./"):
            continue
        _, separator, ref = action.rpartition("@")
        if not separator or not re.fullmatch(r"[0-9a-f]{40}", ref):
            mutable.append(action)
    _require(
        not mutable,
        f"GitHub Actions must use full commit SHAs: {', '.join(mutable)}",
    )


def main() -> int:
    _agentcore_pin()
    _cdk_pair()
    _verification_inventory()
    _action_pins()
    print("Release contracts agree: CLI pin, CDK pair, verification inventory, action SHAs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
