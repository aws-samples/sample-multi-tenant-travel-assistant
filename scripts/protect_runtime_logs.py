"""Mask PII in the agent runtime's log group, and stop it retaining conversations forever.

    cd backend && AWS_REGION=us-east-1 uv run python ../scripts/protect_runtime_logs.py
    ...add --dry-run to print what would change without applying it

**The gap this closes.** Every log group `infra/` creates carries `piiMaskingPolicy()`, and both API
Gateway stages have `dataTraceEnabled: false` so no request body reaches CloudWatch. None of that
touches the group that holds the most sensitive content in the system. Under unified telemetry the
AgentCore Runtime writes to its own group, `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`,
whose
`otel-rt-logs` stream carries the conversation itself: the traveler's messages, the model's replies
and
the tool results. So `logs:GetLogEvents` on one group yielded every tenant's conversations, with no
tenant scoping available and no masking, from a permission that is routinely granted broadly for
troubleshooting.

**A post-deploy script for the same reason as its neighbours: ordering, not optionality.** The group
is
created by the runtime the first time it emits, and the runtime belongs to the AgentCore CLI's
stack,
which deploys after `infra/`. There is no construct to hang a `dataProtectionPolicy` on at synth
time,
and `LogGroup.fromLogGroupName` returns an interface that cannot set one. The same sequencing
problem
`restrict_agentcore_endpoints.py` and `constrain_memory_extraction.py` already solve the same way.

**Masking rather than dropping, which is the more useful half.** The value is still stored; reading
it
needs `logs:Unmask`, a separate permission. So an operator with full log-read access cannot see a
passport number, while an incident responder can be granted it deliberately and audibly. Dropping
the
field would make the trace unreadable and buy less.

**What this deliberately does not claim.** Managed data identifiers are keyword-sensitive, measured
against a live group and recorded at length in `infra/lib/log-masking.ts`: a passport number nested
under a generic JSON key is not reliably masked. This is a backstop over free prose, and prose is
exactly what this group holds, so it fits better here than anywhere else in the system. It is still
not
a guarantee, and the honest claim is "conversation content is masked at ingestion on a best-effort
basis and is behind a second permission", not "PII cannot be read from this group".

**Retention is the part with no caveat.** The group has no retention by default, meaning
conversations
accumulate indefinitely. Fourteen days is set here: comfortably longer than the evaluation suite's
read
window, which is the one legitimate consumer, and short enough that the store is not an archive.

**Idempotent**, and it prints what it changed. Re-run after any `agentcore deploy` that recreates
the
runtime, because the group name embeds the runtime id.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3

# Same precedence and default as `deploy.sh` and the sibling scripts: an explicit `TRAVEL_REGION`
# wins over an ambient `AWS_REGION` that may be set for unrelated work.
REGION = os.environ.get("TRAVEL_REGION") or os.environ.get("AWS_REGION") or "us-east-1"

AGENT_STACK = "AgentCore-MultiTenantTravel-default"
# Matched by prefix because CDK appends a hash that shifts when the stack is regenerated. Same
# reasoning as `publish_agent_refs.py` and `restrict_agentcore_endpoints.py`.
RUNTIME_ARN_OUTPUT = "ApplicationAgentAssistantRuntimeArnOutput"

# Long enough for the evaluation runner, which reads this group back to score trajectories, and
# short
# enough that the group is not a conversation archive. Matches the retention on the BFF and tool
# groups, which is deliberate: one number to reason about rather than four.
RETENTION_DAYS = 14

POLICY_NAME = "multi-tenant-travel-runtime-pii"

# Value-shaped identifiers first, then the keyword-sensitive ones. `Name` and `EmailAddress` match
# on
# the value's own shape and so work in free prose, which is what this group holds. The passport and
# card identifiers are included because they cost nothing and do catch loose prose, while the module
# docstring is explicit that they cannot be relied on for structured payloads.
MANAGED_IDENTIFIERS = [
    "Name",
    "EmailAddress",
    "PhoneNumber-US",
    "PassportNumber-US",
    "CreditCardNumber",
    "DriversLicense-US",
    "Address",
]


def log_group_name(runtime_arn: str) -> str:
    """`/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`.

    Duplicated from `evaluation/runner/judge.py:log_group` rather than imported: `evaluation` is a
    separate uv project with its own lock, and importing across them to share one f-string would
    couple the deploy path to the eval path for no benefit. If the platform ever changes this shape
    both places break together and loudly, which is the acceptable failure.
    """
    return f"/aws/bedrock-agentcore/runtimes/{runtime_arn.rsplit('/', 1)[-1]}-DEFAULT"


def data_protection_policy(group: str) -> dict:
    """Audit-and-mask over the whole group.

    The `Audit` statement must come before `Deidentify`; CloudWatch rejects a policy without both,
    and
    the findings destination is required even when nothing consumes it. Sending findings back into
    the
    same group would be circular, so this uses the group's own name only as the policy description
    and
    keeps the finding destination empty of a second store.
    """
    return {
        "Name": POLICY_NAME,
        "Description": f"Mask PII in {group}; readable only with logs:Unmask",
        "Version": "2021-06-01",
        "Statement": [
            {
                "Sid": "audit",
                "DataIdentifier": [
                    f"arn:aws:dataprotection::aws:data-identifier/{i}" for i in MANAGED_IDENTIFIERS
                ],
                "Operation": {"Audit": {"FindingsDestination": {}}},
            },
            {
                "Sid": "mask",
                "DataIdentifier": [
                    f"arn:aws:dataprotection::aws:data-identifier/{i}" for i in MANAGED_IDENTIFIERS
                ],
                "Operation": {"Deidentify": {"MaskConfig": {}}},
            },
        ],
    }


def runtime_arn(cfn) -> str:
    try:
        stacks = cfn.describe_stacks(StackName=AGENT_STACK)["Stacks"]
    except cfn.exceptions.ClientError as error:
        raise SystemExit(
            f"cannot read {AGENT_STACK}: {error}\nrun `agentcore deploy` first"
        ) from None
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    for key, value in outputs.items():
        if key.startswith(RUNTIME_ARN_OUTPUT):
            return value
    raise SystemExit(
        f"no output starting with {RUNTIME_ARN_OUTPUT} on {AGENT_STACK}; found {sorted(outputs)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without applying")
    args = parser.parse_args()

    session = boto3.Session(region_name=REGION)
    group = log_group_name(runtime_arn(session.client("cloudformation")))
    logs = session.client("logs")

    print(f"runtime log group {group}")

    # **The group may legitimately not exist yet.** It is created on first emit, so a freshly
    # deployed
    # runtime that has served no turn has no group. Refusing loudly is wrong here — the deploy is
    # fine
    # and the fix is to run one turn — so this reports and exits non-zero without a traceback.
    existing = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
    if not any(g["logGroupName"] == group for g in existing):
        print(
            "  the group does not exist yet, which means the runtime has not emitted.\n"
            "  send one turn through the assistant, then re-run this script.",
            file=sys.stderr,
        )
        return 1

    current = existing[0]
    policy = data_protection_policy(group)

    if args.dry_run:
        print(
            f"  would set retention to {RETENTION_DAYS} days "
            f"(currently {current.get('retentionInDays') or 'never expires'})"
        )
        print(f"  would put data protection policy {POLICY_NAME}:")
        print(json.dumps(policy, indent=2))
        return 0

    if current.get("retentionInDays") == RETENTION_DAYS:
        print(f"  retention already {RETENTION_DAYS} days")
    else:
        logs.put_retention_policy(logGroupName=group, retentionInDays=RETENTION_DAYS)
        print(
            f"  retention {current.get('retentionInDays') or 'never expires'} "
            f"-> {RETENTION_DAYS} days"
        )

    logs.put_data_protection_policy(logGroupIdentifier=group, policyDocument=json.dumps(policy))
    print(
        f"  data protection policy {POLICY_NAME} applied ({len(MANAGED_IDENTIFIERS)} identifiers)"
    )
    print("  reading a masked value now requires logs:Unmask in addition to logs:GetLogEvents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
