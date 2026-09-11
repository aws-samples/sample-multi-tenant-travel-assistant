"""Per-tenant scoped DynamoDB credentials.

The backend holds one role that can reach every tenant's rows — it serves every tenant, so
it must. This module narrows that per *request*: it assumes a role tagged with the caller's
tenant, and the role's policy pins `dynamodb:LeadingKeys` to `TENANT#${aws:PrincipalTag/tenant}`.
A query for another tenant's partition is then refused by IAM, whatever the code intended.

**What this defends against, precisely.** Not prompt injection — the model has no channel to
name a tenant (it is injected from a verified JWT into `client_context`, which the model cannot
reach) and Cedar gates the action before the tool runs. This defends against *our own code
being wrong*: a query built with the wrong prefix by a bug, a refactor, or an exploit.

**And this half is not an agentic problem.** Pooled multi-tenant SaaS has isolated rows this way
for years. The pattern here is conventional on purpose — what is new in this sample is the
Cedar/interceptor layer above it, not this.

**One role, not one per tenant.** `LeadingKeys` accepts policy variables, so the tenant arrives
as a session tag at assume time. Onboarding a customer therefore stays a *data* operation;
role-per-tenant would make every new tenant an IAM deploy, which does not reach thousands of
tenants.

**The honest caveat:** a fully compromised backend can assume this role with any tenant tag,
because it serves all tenants. That is the pooled-tenancy trust boundary — identical in any
non-agentic SaaS, and the reason compliance-driven customers buy the silo model instead. IAM
narrows the blast radius of a *bug*; it does not make a pooled service unbreakable.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import boto3

from .observability import count, log_decision, log_refusal

# **Alarmed on, so the name is a contract.** `infra/lib/tenant-isolation.ts` builds a
# CloudWatch alarm from this exact metric name in the `MultiTenantTravel` namespace.
# Renaming it here without renaming it there leaves an alarm watching a metric nobody
# emits — worse than no alarm, because it reads as green. `test_tenant_credentials.py`
# asserts the two agree.
#
# A dedicated counter rather than reusing the generic `Refusals` metric: that one is
# dimensioned by the refusal's prose, so alarming on it would mean matching a sentence.
SCOPING_UNCONFIGURED_METRIC = "TenantScopingUnconfigured"

# Written by CDK. Absent means row-scoping is not configured, which is a legitimate local
# state (tests, `uvicorn`) and a misconfiguration in a deployed one — hence the explicit
# `is_enabled()` rather than a silent fallback.
ROLE_ARN_VAR = "TENANT_DATA_ROLE_ARN"

# Sessions last an hour; refresh early so a long request never straddles expiry.
SESSION_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 300

# Cached per (tenant, session, actor, subject) per container — the full tag set,
# not just the tenant.
# An `AssumeRole` on every request would add a control-plane call to the hot path for credentials
# valid for an hour; keying on tenant alone would misattribute later conversations to the first
# one's session tags. See `_credentials`.
_cache: dict[tuple[str, str, str, str], tuple[dict[str, Any], float]] = {}


def is_enabled() -> bool:
    return bool(os.environ.get(ROLE_ARN_VAR))


def hashed_user(traveler_id: str) -> str:
    """A stable, non-reversible stand-in for a traveler, for the audit trail.

    **Not obfuscation — purpose limitation.** `trv_31d81fa59772` is already opaque to an
    outsider, so hashing hides nothing from an attacker. The point is that CloudTrail is
    retained for years, read by auditors and shipped to SIEMs: a per-person identifier
    accumulating there becomes a person-tracking dataset by accident. A stable hash keeps the
    property an audit needs — the same person yields the same value, so a chain reconstructs —
    and drops the one it does not: joining back to a named human without our own data.

    16 hex characters, because session-tag values are length-limited and 64 bits of SHA-256 is
    far beyond collision risk for any traveler population.
    """
    return hashlib.sha256(traveler_id.encode()).hexdigest()[:16]


def _assume(
    tenant_id: str,
    session_id: str | None = None,
    actor_id: str | None = None,
    subject_id: str | None = None,
) -> dict[str, Any]:
    """Assume the data role, tagged for this tenant, conversation, actor and subject.

    The `tenant` tag is what the role's `LeadingKeys` condition interpolates, so **that tag
    value is the security boundary**. It comes from the request's verified `X-Tenant-Id` header,
    never from anything a caller or a model supplied as data.

    The other tags are **audit dimensions, not authorization inputs** — no policy keys off them.
    `user` is the verified actor and `subject` is the traveler whose data the operation concerns,
    so an arranger remains visible as acting for someone else rather than impersonating them.
    """
    tags = [{"Key": "tenant", "Value": tenant_id}]
    if session_id:
        tags.append({"Key": "session_id", "Value": session_id[:256]})
    if actor_id:
        tags.append({"Key": "user", "Value": hashed_user(actor_id)})
    if subject_id:
        tags.append({"Key": "subject", "Value": hashed_user(subject_id)})

    sts = boto3.client("sts")
    response = sts.assume_role(
        RoleArn=os.environ[ROLE_ARN_VAR],
        # Appears in CloudTrail as the session identity, so a data access is attributable to a
        # tenant without joining anything. Same dimension the cost ledger uses.
        RoleSessionName=f"multi-tenant-travel-{tenant_id}"[:64],
        DurationSeconds=SESSION_SECONDS,
        Tags=tags,
        # **Only `tenant` is transitive, and the asymmetry is deliberate.** A transitive tag is
        # fixed for the remainder of the role chain: exactly right for the isolation boundary,
        # since nothing downstream can re-tag itself into another tenant and satisfy the same
        # policy. Exactly wrong for a conversation id — a later, unrelated chain would inherit a
        # stale `session_id` and the audit trail would begin to lie.
        TransitiveTagKeys=["tenant"],
    )
    return response["Credentials"]


def _credentials(
    tenant_id: str,
    session_id: str | None = None,
    actor_id: str | None = None,
    subject_id: str | None = None,
) -> dict[str, Any]:
    """Credentials for this tenant, cached per **tag set**.

    The cache key includes the audit dimensions, not just the tenant. Keying on tenant alone
    would be a subtle correctness bug rather than a mere inefficiency: the second conversation
    for a tenant would reuse the first conversation's credentials, and every row it read would be
    attributed in CloudTrail to the *earlier* `session_id`, actor and subject. The audit trail
    would look complete and be wrong, which is worse than missing.

    The trade is more `AssumeRole` calls — one per conversation per container rather than one per
    tenant. Acceptable: it is a control-plane call once per hour per distinct session, and the
    alternative is an audit trail that cannot be trusted.
    """
    key = (tenant_id, session_id or "", actor_id or "", subject_id or "")
    cached = _cache.get(key)
    now = time.time()
    if cached and cached[1] - REFRESH_MARGIN_SECONDS > now:
        return cached[0]

    credentials = _assume(tenant_id, session_id, actor_id, subject_id)
    expiry = credentials["Expiration"].timestamp()
    _cache[key] = (credentials, expiry)

    log_decision(
        "assumed tenant-scoped data role",
        tenant_id=tenant_id,
        # The audit dimensions, logged so an application log line and a CloudTrail event can be
        # joined on the same values. The user is logged **hashed**, matching the tag — a raw
        # traveler id here would defeat the point of hashing it there.
        session_id=session_id,
        user=hashed_user(actor_id) if actor_id else None,
        subject=hashed_user(subject_id) if subject_id else None,
        # Not the credentials, obviously. The expiry is the useful diagnostic when a
        # long-running container starts failing reads.
        expires_in_seconds=int(expiry - now),
        cached_sessions=len(_cache),
    )
    return credentials


def scoped_dynamodb(
    tenant_id: str,
    session_id: str | None = None,
    actor_id: str | None = None,
    subject_id: str | None = None,
) -> Any:
    """A DynamoDB resource whose credentials can only reach this tenant's partitions.

    Returns the default resource when row-scoping is unconfigured, so local runs and tests work
    untouched — but says so loudly, because silently serving unscoped credentials in a deployed
    environment is the failure this module exists to prevent.
    """
    if not is_enabled():
        log_refusal(
            "tenant row-scoping is not configured — using the backend's own broad role",
            tenant_id=tenant_id,
        )
        # **A log line is not detection, and this is the one condition where that mattered.**
        # Losing `TENANT_DATA_ROLE_ARN` removes the row-level isolation control while every request
        # keeps succeeding, so nothing pages, nothing fails, and the service looks healthy. The log
        # above says so on every request, which only helps someone already reading logs.
        #
        # Emitting a dedicated metric makes the condition alarmable, and
        # `tenant-isolation.ts` alarms on it. Deliberately undimensioned beyond the
        # service: this is a deployment-wide state rather than a per-tenant event, and
        # adding `tenant_id` would make the alarm's Sum depend on how many tenants
        # happened to be active.
        count(SCOPING_UNCONFIGURED_METRIC)
        return boto3.resource("dynamodb")

    credentials = _credentials(tenant_id, session_id, actor_id, subject_id)
    return boto3.resource(
        "dynamodb",
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def clear_cache() -> None:
    """Drop cached credentials. For tests, and for the control-removal demo."""
    _cache.clear()
