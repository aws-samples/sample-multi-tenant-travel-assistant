"""Request context: who the Gateway says this call is for.

**The single most important file in the tool layer.** Every tenant boundary in the
sample funnels through `tenant_context()`, so a mistake here is a cross-tenant data
leak in thirteen tools at once.

Identity arrives as **headers injected by the request interceptor**, which verified
the traveler's JWT against Cognito's JWKS at the edge. Three properties make that
trustworthy:

1. **The tool Lambda's resource policy admits only the Gateway role.** A request
   that arrives came from the Gateway, and the Gateway forwards only what the
   interceptor produced. That is why no signed assertion is needed — signing would
   defend against a compromised Gateway, which is outside the threat model and
   costs a KMS call per request.
2. **Identity travels in a channel the model cannot reach.** For a Lambda target the
   event contains *only* the model's arguments; propagated headers arrive out of band in
   the Lambda client context. So this is stronger than "we don't put tenant in the
   schema" — there is no field a prompt-injected model could forge, because the argument
   object and the identity channel are physically different objects.
3. **Absent identity is a refusal, not a default.** There is no "current tenant" to
   fall back to, and a tool that guesses is a tool that eventually guesses wrong.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from .errors import MissingIdentityError
from .observability import logger

# Injected by the request interceptor from verified JWT claims. The names match
# `infra/lambda/interceptor/` — one source of truth for the pair would be better,
# but they cross a language boundary, so a test asserts they agree.
TENANT_HEADER = "X-Tenant-Id"
TRAVELER_HEADER = "X-Traveler-Id"
ROLE_HEADER = "X-Traveler-Role"

# Added only on the tool-to-backend hop. `X-Traveler-Id` names the operation's
# subject; this names the verified caller so delegation remains delegation in
# authorization and audit records.
ACTOR_HEADER = "X-Actor-Id"

# The conversation id, forwarded by the **agent** for audit correlation only.
#
# **Deliberately `session_id`, not a new `workflow_id`.** An earlier draft invented a second
# name for this exact value, which would have quietly broken the property the whole audit story
# rests on: the ledger already records `session_id`, so writing `workflow_id` into CloudTrail
# for the same value would force anyone joining cost to audit through a mapping table. One name
# per dimension is the point of "one instrumentation layer, three readouts".
#
# Unlike the three headers above it is *not* injected by the interceptor from a verified claim —
# it originates in the agent and passes through untouched. That is acceptable precisely because
# nothing authorizes on it: it is a label tying a data access to one conversation, and the worst
# a forged value can do is mislabel the forger's own trail. Kept visibly separate from the
# identity headers so a reader never mistakes it for a trust boundary.
SESSION_HEADER = "X-Session-Id"

# Where a **Lambda target** actually receives propagated headers.
#
# Not in the event. The event holds *only* the model's arguments — verified by dumping
# it: a `tools/call` with `{"topic": "hotel"}` arrives as exactly `{"topic": "hotel"}`.
# Headers travel out of band, in the Lambda client context, beside the tool name:
#
#     context.client_context.custom["bedrockAgentCorePropagatedHeaders"]
#         -> {"X-Tenant-Id": "globex", "X-Traveler-Id": "trv_...", ...}
#
# **This example used to show `Authorization` here, and it was accurate at the time.** The
# interceptor now deletes the token before returning, so a tool call carries no bearer
# credential — nothing in this package ever read it, and leaving the channel open invited a
# future tool to start validating JWTs locally. Confirmed against a live deployment on
# 2026-08-29: the canary below stayed silent across tool invocations, so the Gateway forwards
# the interceptor's transformed headers rather than the caller's originals.
#
# This is a *better* boundary than the event would be, and worth stating in the blog:
# identity is not merely absent from the tool schema, it is physically unreachable from
# anything the model can shape. A prompt-injected model cannot forge a field it has no
# channel to.
#
# The same `custom` dict also carries `bedrockAgentCoreToolName`, `…TargetId`,
# `…GatewayId`, `…McpMessageId` and `…AwsRequestId`.
_PROPAGATED_HEADERS_KEY = "bedrockAgentCorePropagatedHeaders"


def _client_custom(context: Any) -> dict[str, Any]:
    """The Gateway's out-of-band metadata for this invocation."""
    custom = getattr(getattr(context, "client_context", None), "custom", None)
    return custom if isinstance(custom, dict) else {}


def _headers(context: Any) -> dict[str, str]:
    """Propagated request headers, keyed lowercase.

    HTTP header names are case-insensitive and the casing that arrives has varied between
    target types, so matching an exact string is a bug waiting for a platform change.
    """
    propagated = _client_custom(context).get(_PROPAGATED_HEADERS_KEY)
    if not isinstance(propagated, dict):
        return {}
    headers = {str(k).lower(): v for k, v in propagated.items()}

    # **A canary, because the interceptor's strip cannot be proven from inside the interceptor.**
    # It deletes `Authorization` before returning, but whether the Gateway forwards the transformed
    # headers or the originals is the Gateway's behavior, not ours. This is the only place in the
    # system that sees what a tool actually received.
    #
    # Names, never values: the whole point is that a token must not reach a log. `log_decision`
    # is not used because this is a platform observation rather than a business decision, and it
    # fires on every tool call in the failure case.
    #
    # **The session id is carried so a checker can correlate.** Without it, "no warning in the
    # last N minutes" is not an answer: log delivery lags, so a reader can see an older
    # invocation's line, conclude the window held a call, and pass before this call's own line
    # has arrived. `verify_isolation.py` matches on this value and on nothing else.
    if "authorization" in headers:
        logger.warning(
            "propagated headers carried a bearer token; the interceptor strip is not reaching "
            "this target",
            extra={
                "propagated_header_names": sorted(headers),
                "session_id": headers.get(SESSION_HEADER.lower()),
            },
        )

    return headers


@dataclass(frozen=True)
class RequestContext:
    """Verified identity for one tool call.

    `traveler_id` is always the verified caller. An arranger may also have an
    authorized target for backend reads and writes, but that target is carried
    separately so logs and authorization never mistake it for the actor.

    Frozen: nothing downstream may adjust the tenant or identity in place. A
    mutable context is one refactor away from a tool "correcting" either value
    mid-request.
    """

    tenant_id: str
    traveler_id: str | None
    role: str | None
    # Audit correlation only, never authorization. See SESSION_HEADER.
    session_id: str | None = None
    # Set only after name resolution and `ensure_can_act_for` authorize the target.
    target_traveler_id: str | None = None
    # False only while resolving the owner of an opaque offer/reservation handle.
    include_backend_subject: bool = True

    @property
    def backend_traveler_id(self) -> str | None:
        """The traveler whose data the backend operation should use."""
        if not self.include_backend_subject:
            return None
        return self.target_traveler_id or self.traveler_id

    def for_traveler(self, traveler_id: str) -> RequestContext:
        """Return a backend context for an already-authorized target.

        The verified actor remains in `traveler_id`; only the outbound backend
        traveler header changes. Callers must authorize the target before using
        this method.
        """
        return replace(
            self,
            target_traveler_id=traveler_id,
            include_backend_subject=True,
        )

    def without_subject(self) -> RequestContext:
        """Identify only the actor while resolving an opaque handle's owner."""
        return replace(self, target_traveler_id=None, include_backend_subject=False)

    @property
    def log_fields(self) -> dict[str, str]:
        """Dimensions every log line in this request should carry.

        Ids only — never a name. These are the same dimensions the audit trail and
        the cost ledger use, and attribution cannot be retrofitted onto historical
        logs.
        """
        fields = {"tenant_id": self.tenant_id}
        if self.traveler_id:
            fields["traveler_id"] = self.traveler_id
        if self.session_id:
            fields["session_id"] = self.session_id
        return fields


def tenant_context(context: Any) -> RequestContext:
    """Read verified identity from the Lambda client context, or refuse.

    Takes the **Lambda context**, not the event: propagated headers arrive out of band
    (see `_PROPAGATED_HEADERS_KEY`), which is what makes identity unreachable from the
    model's arguments.

    Raises `MissingIdentityError` when no tenant is present. That is deliberately not
    recoverable: the only safe behaviors are "act for the verified tenant" and "refuse",
    and anything resembling a default would silently serve one tenant's data under
    another's session.
    """
    headers = _headers(context)

    tenant_id = headers.get(TENANT_HEADER.lower())
    if not tenant_id:
        raise MissingIdentityError(
            "no verified tenant on this request — the interceptor did not run, or the "
            "header is missing from the target's metadataConfiguration.allowedRequestHeaders"
        )

    return RequestContext(
        tenant_id=str(tenant_id),
        traveler_id=_optional(headers.get(TRAVELER_HEADER.lower())),
        role=_optional(headers.get(ROLE_HEADER.lower())),
        # Absent is fine and must stay fine: a missing conversation id degrades the audit
        # trail's correlation, it does not make the call unsafe. Refusing here would trade a
        # working request for a nicer log line.
        session_id=_optional(headers.get(SESSION_HEADER.lower())),
    )


def _optional(value: Any) -> str | None:
    """Treat empty strings as absent — an empty traveler id is not a traveler."""
    text = str(value).strip() if value is not None else ""
    return text or None


# Defensive only. A Lambda target's event *is* the argument object, so there is normally
# nothing to strip — but a caller that manages to smuggle one of these names in must not
# have it read as identity, and the local test harness constructs events by hand.
_TRANSPORT_KEYS = frozenset({"mcp", "headers", "requestContext"})


def tool_arguments(event: dict[str, Any]) -> dict[str, Any]:
    """Just the model-supplied arguments.

    For a Lambda target the event already contains only the model's arguments; identity
    travels in the client context. This stays as a guard rather than a transformation.
    """
    if not isinstance(event, dict):
        return {}
    return {k: v for k, v in event.items() if k not in _TRANSPORT_KEYS}


def tool_name(context: Any) -> str | None:
    """The tool the model actually called.

    The Gateway passes the name out of band, in the Lambda client context, and prefixes it
    with the target name (`policy___get_travel_policy`). One Lambda serves a whole family,
    so this is how a handler dispatches — and the prefix must be stripped or every
    comparison silently fails.
    """
    name = _client_custom(context).get("bedrockAgentCoreToolName")
    if not name:
        return None
    _, _, bare = str(name).rpartition("___")
    return bare or str(name)


def backend_url() -> str:
    """Base URL of the mock TMC, from the environment.

    Written by our CDK into an SSM parameter and passed in as an env var at deploy
    time. Never a default: a tool silently pointing at the wrong backend is worse
    than one that fails at cold start with a clear message.
    """
    url = os.environ.get("BACKEND_API_URL")
    if not url:
        raise RuntimeError("BACKEND_API_URL is not set — the tool cannot reach the backend")
    return url.rstrip("/")
