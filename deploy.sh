#!/usr/bin/env bash
#
# Deploy the sample end to end: infrastructure, agent, and the wiring between them.
#
#     ./deploy.sh                 # public arrangement (no VPC, no idle cost)
#     ./deploy.sh --private       # capability + data layers in a private VPC (~$161/mo idle)
#     ./deploy.sh --waf           # WAF web ACL in front of the distribution (~$10/mo idle)
#     ./deploy.sh --seed          # also load fixtures and create demo users (first deploy)
#     ./deploy.sh --skip-agent    # infrastructure only
#
# **This exists because the correct order was previously only in a human's head.** Two CDK apps plus
# three post-deploy scripts have to run in sequence, and the sequence is not guessable: each of those
# scripts carries a value across a boundary CloudFormation cannot express. A deploy reconstructed
# from a runbook is one nobody performs the same way twice,
# and the failure mode is silent — a skipped step leaves a working-looking deployment with a detached
# Cedar policy or a BFF pointing at nothing.
#
# **Why two CDK apps at all, since that is the obvious question.** The AgentCore CLI generates and owns
# its own app for the runtime, memory, gateway and policy engine (`agentcore.json` is `managedBy: CDK`).
# It regenerates that app, so hand-editing it is out of bounds. `infra/` covers everything the CLI does
# not own. The split is structural, not a choice.
#
# **Why the post-deploy scripts cannot become CDK.** Each was checked rather than assumed:
#
#   * `configure_gateway.py`   — the CLI schema has no interceptor field on a gateway, and no
#                                `metadataConfiguration` on a target. Verified against the published
#                                schema: neither exists in its vocabulary.
#   * `publish_agent_refs.py`  — carries the runtime ARN and memory id from the *agent* stack, which
#                                deploys after `infra/`. No CloudFormation reference can span that.
#   * `restrict_agentcore_endpoints.py` — same ordering problem, for VPC endpoint policies.
#   * `constrain_memory_extraction.py` — two independent blocks. The CLI validates memory strategies
#                                against a strict enum of the four built-in types, so there is no
#                                CUSTOM type and nowhere to put an extraction override; and
#                                `UpdateMemory` rejects configuration changes to a built-in strategy
#                                outright ("not allowed for memory strategy type USER_PREFERENCE"),
#                                so the override can only arrive with a newly created strategy.
#   * `purge_orphaned_preferences.py` — deleting a strategy leaves its records behind, and they are
#                                still returned by semantic retrieval. Measured, not assumed.
#   * `sync_knowledge_base.py`  — CloudFormation creates the data source but never ingests through
#                                it, and an un-ingested knowledge base answers every query with
#                                nothing while looking healthy.
#
# `sync_policies.py` is a *source* step rather than a deploy step: `agentcore.json` needs each Cedar
# rule inline as `statement`, and the construct ignores `sourceFile` (checked). So it runs before the
# agent deploy, not after.
#
# Idempotent: every step is safe to re-run, and each prints what it changed.

set -euo pipefail

# --- configuration ------------------------------------------------------------------------------

# **Not `AWS_REGION`.** A shell often carries a region for unrelated work, and CDK would then build a
# *second parallel stack* in that region — two Cognito pools, two sets of tables, no error anywhere.
# One explicit default, overridable.
export TRAVEL_REGION="${TRAVEL_REGION:-us-east-1}"
export AWS_REGION="$TRAVEL_REGION"

# The generated AgentCore CDK app and this repository's deployment workarounds are tested as one
# release unit with this CLI version. An unversioned global install silently moves that boundary.
REQUIRED_AGENTCORE_VERSION="0.24.0"

# One flag, read by both halves: `infra/` builds the VPC, `render_agent_spec.py` puts the runtime
# in it. Two sources would let them disagree.
#
# **`:-` rather than a bare assignment, so a preset environment survives.** Neither of these is stored
# state — they are synth-time context, and a deploy without them synthesizes the *other* topology,
# which CloudFormation then converges to by deleting VPC endpoints or a web ACL. Nothing errors,
# because that is what was asked for. Forcing `false` here meant the only way to keep a private
# deployment private was to remember the flag every single time; now `export TRAVEL_PRIVATE=true` once
# holds for the session, matching how `TRAVEL_REGION` already behaves directly above.
export TRAVEL_PRIVATE="${TRAVEL_PRIVATE:-false}"

# Off by default so an idle deployment carries no *avoidable* standing cost. The web ACL is ~$10/month
# and, until this switch existed, was the largest thing here billing while nobody used it. (Not the
# only thing: the session-token KMS key is ~$1/month and is not optional — see the Cost section of
# README.md.) Recommended before sharing the demo URL broadly.
export TRAVEL_WAF="${TRAVEL_WAF:-false}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Seeded from the exported values so a preset environment shows up in the preflight summary and in
# `render_agent_spec.py`'s branch below, not just in the synth.
PRIVATE="$TRAVEL_PRIVATE"
WAF="$TRAVEL_WAF"
SEED=false
SKIP_AGENT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --private) PRIVATE=true; TRAVEL_PRIVATE=true ;;
    --waf) WAF=true; TRAVEL_WAF=true ;;
    --seed) SEED=true ;;
    --skip-agent) SKIP_AGENT=true ;;
    -h|--help) sed -n '3,9p' "$0" | sed 's/^#[[:space:]]\{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
note() { printf '    %s\n' "$1"; }

# --- preflight ----------------------------------------------------------------------------------
#
# Checked up front because each of these fails *late* and confusingly otherwise: missing credentials
# surface as a CDK bootstrap error, and a missing `uv` surfaces as
# `uv install failed on platform aarch64-manylinux2014` — which reads like a platform
# incompatibility rather than a missing binary.

step "Preflight"
for tool in node npm uv docker python3 aws; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done
if ! $SKIP_AGENT; then
  command -v agentcore >/dev/null 2>&1 || {
    echo "missing required tool: agentcore" >&2
    echo "install it with: npm install -g @aws/agentcore@$REQUIRED_AGENTCORE_VERSION" >&2
    exit 1
  }
  AGENTCORE_VERSION="$(agentcore --version 2>/dev/null || true)"
  AGENTCORE_VERSION="${AGENTCORE_VERSION//$'\r'/}"
  AGENTCORE_VERSION="${AGENTCORE_VERSION//$'\n'/}"
  if [[ "$AGENTCORE_VERSION" != "$REQUIRED_AGENTCORE_VERSION" ]]; then
    echo "AgentCore CLI $REQUIRED_AGENTCORE_VERSION is required; found '${AGENTCORE_VERSION:-unknown}'." >&2
    echo "install the tested version with:" >&2
    echo "  npm install -g @aws/agentcore@$REQUIRED_AGENTCORE_VERSION" >&2
    exit 1
  fi
  note "AgentCore CLI: $AGENTCORE_VERSION"
fi
python3 scripts/check_agent_spec_placeholders.py
# Docker has to be *running*, not merely installed — CDK bundles the Python Lambdas in a container.
docker info >/dev/null 2>&1 || { echo "docker is installed but not running" >&2; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" || {
  echo "no usable AWS credentials — refresh them and retry" >&2; exit 1; }
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
note "account $ACCOUNT, region $TRAVEL_REGION"

# **The account has to be CDK-bootstrapped, and finding that out late costs ten minutes.** An
# unbootstrapped account does not fail at synth: CDK bundles all eleven Python Lambdas in Docker
# first, prints `current credentials could not be used to assume
# 'arn:aws:iam::<account>:role/cdk-hnb659fds-file-publishing-role-...', but are for the right
# account. Proceeding anyway` eight times — which reads like a permissions problem and is not — and
# only then stops on `SSM parameter /cdk-bootstrap/hnb659fds/version not found`. Measured on a fresh
# account: seven minutes to reach a one-line prerequisite.
#
# Detected rather than done. `cdk bootstrap` creates account-level resources — a staging bucket, an
# ECR repository and five roles — that outlive this sample and that `cleanup.sh` deliberately does
# not remove, because other stacks in the account may depend on them. Creating those silently on
# someone's behalf is not a deploy script's decision to make.
aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --region "$TRAVEL_REGION" \
  >/dev/null 2>&1 || {
  echo "    account $ACCOUNT is not CDK-bootstrapped in $TRAVEL_REGION." >&2
  echo >&2
  echo "      cd infra && npx cdk bootstrap aws://$ACCOUNT/$TRAVEL_REGION" >&2
  echo >&2
  echo "    One-time per account and region. It creates a staging bucket, an ECR repository and" >&2
  echo "    five roles, which are shared with every other CDK app in the account — so cleanup.sh" >&2
  echo "    leaves them alone." >&2
  exit 1
}
note "cdk bootstrap: present"
$PRIVATE && note "private VPC: ON (~\$161/month idle for the endpoints — see the Cost section of README.md)" \
         || note "private VPC: off (pass --private to enable)"
$WAF && note "WAF web ACL: ON (~\$10/month idle — recommended when the demo URL is shared broadly)" \
     || note "WAF web ACL: off (pass --waf to enable; API Gateway throttling is on either way)"

# The AgentCore schema requires concrete deployment identifiers, so two scripts below must render
# five tracked source files in place. Preserve the portable originals before the first mutation and
# restore them on every exit, including an interrupted or failed deploy. A deploy may consume live
# values; a commit must never contain them.
DEPLOY_RENDERED_FILES=(
  "agent/MultiTenantTravel/agentcore/agentcore.json"
  "agent/MultiTenantTravel/agentcore/aws-targets.json"
  "agent/MultiTenantTravel/app/MultiTenantTravel/policies/budget-iam.json"
  "agent/MultiTenantTravel/app/MultiTenantTravel/policies/guardrail-iam.json"
  "agent/MultiTenantTravel/app/MultiTenantTravel/policies/model-iam.json"
)
DEPLOY_SOURCE_BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/multi-tenant-travel-source.XXXXXX")"
for file in "${DEPLOY_RENDERED_FILES[@]}"; do
  mkdir -p "$DEPLOY_SOURCE_BACKUP/$(dirname "$file")"
  cp -p "$file" "$DEPLOY_SOURCE_BACKUP/$file"
done

restore_deploy_rendered_files() {
  local original_status=$?
  local restore_status=0
  trap - EXIT INT TERM
  for file in "${DEPLOY_RENDERED_FILES[@]}"; do
    cp -p "$DEPLOY_SOURCE_BACKUP/$file" "$file" || restore_status=1
  done
  rm -rf "$DEPLOY_SOURCE_BACKUP"
  if [[ "$restore_status" -ne 0 ]]; then
    echo "ERROR: failed to restore portable AgentCore source files" >&2
    exit 1
  fi
  note "restored portable AgentCore source placeholders"
  exit "$original_status"
}
trap restore_deploy_rendered_files EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 1. derived files ---------------------------------------------------------------------------
#
# Before anything is deployed, because both are inputs to it. Cheap, and skipping them ships a
# frontend that cannot render a card type the tools emit — which fails silently as a missing tile.

step "Regenerating derived files"
python3 scripts/generate_card_types.py
note "shared/generated/cards.ts"

# **Exit 10 is not a failure**: the policy does not yet name a live gateway, which on a first
# deploy is unavoidable. Step 4b narrows it.
sync_policies() {
  set +e
  (cd backend && uv run python ../scripts/sync_policies.py)
  SYNC_RC=$?
  set -e
  [[ "$SYNC_RC" == "0" || "$SYNC_RC" == "10" ]] || exit "$SYNC_RC"
}

sync_policies
note "Cedar policies -> agentcore.json"

# --- 2. infrastructure --------------------------------------------------------------------------
#
# cdk-nag runs as part of this synth, so a security regression stops the deploy here rather than
# being discovered in review.

step "Deploying infrastructure (cdk)"
(
  cd infra
  # **`ci`, not `install`.** `install` may resolve something the lockfile does not name and then
  # rewrite the lock as a side effect of deploying, so the artifact deployed is not the artifact the
  # gate tested. `ci` installs exactly `package-lock.json` and fails if the lock and the manifest
  # disagree, which is the property a deploy wants.
  npm ci --silent
  npm run deploy 2>&1 | tee /tmp/multi-tenant-travel-cdk-deploy.log
  # `tee` puts the pipeline's exit status in ${PIPESTATUS[0]}, not $?.
  [[ "${PIPESTATUS[0]}" -eq 0 ]] || exit 1
)

# **A zero exit from `cdk deploy` does not prove anything was applied.** With `--no-execute` in
# effect — a wrapper, a CI guard, a sandbox that intercepts writes — the CLI creates a changeset,
# prints success and exits 0 having deployed nothing, and every step below would then run against
# stale infrastructure while the script reported success.
#
# **Asserted against the stack, not against the log.** An earlier version grepped the output for
# "waiting in review for manual execution" and stopped a deploy that had entirely succeeded: CDK
# prints that line during an ordinary two-phase deploy (create changeset, then execute it) and
# proceeds to apply it. The phrase describes a step, not an outcome. What distinguishes the two is
# the stack itself — an unexecuted changeset leaves a new stack in `REVIEW_IN_PROGRESS` and an
# existing one untouched.
STACK_STATE="$(aws cloudformation describe-stacks --stack-name multi-tenant-travel \
  --query 'Stacks[0].[StackStatus,LastUpdatedTime,CreationTime]' --output text 2>/dev/null || echo "MISSING")"
STACK_STATUS="${STACK_STATE%%$'\t'*}"
case "$STACK_STATUS" in
  CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_COMPLETE_CLEANUP_IN_PROGRESS)
    note "stack multi-tenant-travel: $STACK_STATUS" ;;
  *)
    echo "" >&2
    echo "ERROR: cdk exited 0 but stack multi-tenant-travel is '$STACK_STATUS', not a completed state." >&2
    echo "       REVIEW_IN_PROGRESS means a changeset was created and never executed" >&2
    echo "       (--no-execute). Nothing below this point would be deploying against what" >&2
    echo "       you think, so the script is stopping instead." >&2
    exit 1 ;;
esac

# --- 2b. render the agent spec against what was just deployed -----------------------------------
#
# Here because `agentcore.json` needs concrete values that are outputs of the stack above and
# cannot be expressed as references. A stale spec deploys perfectly and fails at invoke time.
render_agent_spec() {
  set +e
  (cd backend && uv run python ../scripts/render_agent_spec.py)
  RENDER_RC=$?
  set -e
  [[ "$RENDER_RC" == "0" || "$RENDER_RC" == "10" ]] || exit "$RENDER_RC"
}

step "Rendering the agent spec from the deployed stack"
render_agent_spec

# --- 3. seed data (optional) --------------------------------------------------------------------
#
# Only on request. Re-running refreshes mutable attributes and reuses the stored demo password.
# Immutable tenant and traveler claims are compared rather than updated; a mismatch stops with
# instructions to delete that user or recreate the pool.

if $SEED; then
  step "Seeding fixtures and demo users"
  POOL_ID="$(aws ssm get-parameter --name /multi-tenant-travel/identity/user-pool-id \
    --query Parameter.Value --output text)"
  DOCS_BUCKET="$(aws cloudformation describe-stacks --stack-name multi-tenant-travel \
    --query "Stacks[0].Outputs[?contains(OutputKey,'PolicyDocsBucketName')].OutputValue" \
    --output text)"
  (cd backend && uv run python -m seed.load --table-prefix multi-tenant-travel --bucket "$DOCS_BUCKET")
  (cd backend && uv run python -m seed.users --user-pool-id "$POOL_ID")
fi

# --- 4. agent -----------------------------------------------------------------------------------

if ! $SKIP_AGENT; then
  step "Deploying agent (agentcore)"
  # **`agentcore deploy` can exit non-zero after a successful deploy** — it validated its own
  # `deployed-state.json` write-back and failed on empty `gatewayArn` fields (fixed in CLI 0.24.0).
  # Checked against the service afterwards rather than trusted, because the exit code has lied before.
  (cd agent/MultiTenantTravel && agentcore deploy --yes) || {
    note "agentcore deploy returned non-zero — verifying against the service before failing"
  }
  RUNTIME_STATUS="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$(aws ssm get-parameter --name /multi-tenant-travel/agent/runtime-arn \
      --query Parameter.Value --output text 2>/dev/null | sed 's|.*/||')" \
    --query status --output text 2>/dev/null || echo UNKNOWN)"
  note "runtime status: $RUNTIME_STATUS"

  # --- 4b. point the spec at the gateway that now exists ----------------------------------------
  #
  # The one unavoidably two-pass step: the Cedar `resource` ARN and `GATEWAY_MCP_URL` both derive
  # from the gateway's id, which does not exist until the gateway does. A first deploy therefore
  # writes a policy naming a gateway that is not there — deny-all, the right direction — and this
  # pass replaces both. Unconditional, so a re-run after a partial failure cannot skip it.
  step "Pointing Cedar and GATEWAY_MCP_URL at the deployed gateway"
  sync_policies
  render_agent_spec
  if [[ "$SYNC_RC" == "10" || "$RENDER_RC" == "10" ]]; then
    note "spec did not name the live gateway — re-deploying the agent"
    (cd agent/MultiTenantTravel && agentcore deploy --yes) || {
      note "agentcore deploy returned non-zero — verifying below"
    }
  else
    note "spec already names the live gateway"
  fi

  # **Cedar must be attached before this script claims success.** The first pass deliberately
  # deploys the gateway with no policy engine, because the policy resource refuses to reference a
  # gateway that does not exist yet. If the second pass did not attach it, the gateway is authorizing
  # nothing — which is the one outcome that must never be reported as a completed deploy.
  # **Asked of the live gateway, not of the spec file on disk.** This used to grep `agentcore.json`
  # for the two keys — and `sync_policies.py` writes them, so the check passed by construction. It
  # passed on a deploy whose second pass had rolled back: the spec named a policy engine, the gateway
  # had none, and the script printed a successful summary over a sample whose central control was off.
  # The only thing worth asserting here is what the gateway itself reports.
  if ! (cd backend && uv run python - <<'GUARD'
import sys

sys.path.insert(0, "../scripts")
import boto3
from deployed_refs import REGION, refs

gateway_id = refs.gateway_arn.rsplit("/", 1)[-1]
control = boto3.client("bedrock-agentcore-control", region_name=REGION)
engine = control.get_gateway(gatewayIdentifier=gateway_id).get("policyEngineConfiguration")
if not engine or not engine.get("arn"):
    print(f"  gateway {gateway_id} reports no policy engine", file=sys.stderr)
    raise SystemExit(1)
print(f"  Cedar attached: {engine['arn'].rsplit('/', 1)[-1]} (mode: {engine.get('mode')})")
GUARD
  ); then
    echo "" >&2
    echo "ERROR: the gateway has no Cedar policy engine attached, so it is authorizing nothing." >&2
    echo "       The second pass attaches it — check the agent deploy above for a rollback, then" >&2
    echo "       re-run. A policy name already in use by an older deployment is one cause: policy" >&2
    echo "       names are account-scoped, so a superseded engine has to be deleted first." >&2
    exit 1
  fi

  # --- 5. the wiring CloudFormation cannot express ----------------------------------------------
  #
  # All three carry a value across the boundary between the two CDK apps. Order matters only in that
  # they all follow the agent deploy.

  step "Wiring agent references"
  (cd backend && uv run python ../scripts/publish_agent_refs.py)

  step "Attaching gateway interceptor and header allowlist"
  # **Confirm this prints "preserved policy engine (ENFORCE)".** A warning there means Cedar got
  # detached and the gateway is enforcing nothing — a silent loss of the authorization layer.
  (cd backend && uv run python ../scripts/configure_gateway.py)

  # **Two steps, because neither alone is sufficient.** `agentcore.json` cannot express a memory
  # extraction override — the CLI's strategy schema is a strict enum of the four built-in types — and
  # `UpdateMemory` refuses to add one to a built-in strategy, so the first script *replaces* the
  # strategy with a CUSTOM one. Deleting a strategy does not delete its records, and orphaned records
  # are still returned by semantic retrieval, so the second removes them. Without both, tenant policy
  # keeps being stored as a traveler preference and keeps being injected on policy turns, which lets
  # the agent answer from memory instead of calling `get_travel_policy`.
  step "Constraining memory extraction and purging orphaned preferences"
  (cd backend && uv run python ../scripts/constrain_memory_extraction.py)
  (cd backend && uv run python ../scripts/purge_orphaned_preferences.py)

  # **Without this the knowledge base is an empty index that fails silently.** Creating the KB and
  # uploading the documents does not index them, and nothing else here calls `StartIngestionJob` —
  # `ListIngestionJobs` reported that none had ever run, so `search_policy_knowledge` returned zero
  # results on every deploy since the first. The agent handles an empty result *gracefully* ("the
  # policy documents don't mention it — shall I connect you with your travel team?"), so the broken
  # retrieval layer reads as a careful answer. Incremental, so a re-run on unchanged documents is
  # cheap, and an edited policy document reaches the index here rather than sitting in S3.
  step "Indexing policy documents into the knowledge base"
  (cd backend && uv run python ../scripts/sync_knowledge_base.py)

  # **The one log group `infra/` cannot reach, and it holds the most sensitive content here.** Under
  # unified telemetry the runtime writes the conversation itself — traveler messages, model replies,
  # tool results — into its own group, which the AgentCore stack creates on first emit. So there is no
  # construct to hang a data-protection policy on at synth time, and `logs:GetLogEvents` on that one
  # group otherwise yields every tenant's conversations unmasked. Also sets a retention, because the
  # group defaults to keeping them forever.
  #
  # **Non-fatal on a first deploy.** The group does not exist until the runtime has served a turn, so
  # this reports and continues rather than failing the deploy; re-run it after the first conversation.
  step "Masking PII and setting retention on the runtime log group"
  if ! (cd backend && uv run python ../scripts/protect_runtime_logs.py); then
    note "not protected yet: the group appears on the runtime's first emit."
    note "send one turn through the assistant, then re-run:"
    note "  (cd backend && uv run python ../scripts/protect_runtime_logs.py)"
  fi

  if $PRIVATE; then
    step "Narrowing AgentCore VPC endpoint policies"
    (cd backend && uv run python ../scripts/restrict_agentcore_endpoints.py)
  fi
fi

# --- 6. frontend --------------------------------------------------------------------------------

step "Building and publishing the frontend"
./scripts/deploy_frontend.sh

# --- done --------------------------------------------------------------------------------------

SITE="$(aws ssm get-parameter --name /multi-tenant-travel/frontend/origin \
  --query Parameter.Value --output text 2>/dev/null || echo '(not published)')"

step "Deployed"
note "site: $SITE"
note ""
note "Verify (the demo password is read from Parameter Store):"
note "  cd backend && uv run python ../scripts/verify_conversation_api.py"
note "  cd backend && uv run python ../scripts/verify_isolation.py"
$PRIVATE && note "  cd backend && uv run python ../scripts/verify_network.py"
note ""
note "Cost allocation tags need a separate run ~24h after a first deploy, because AWS will not"
note "activate a key until it appears in billing data:"
note "  cd backend && uv run python ../scripts/activate_cost_tags.py"
