"""A circuit breaker for one conversation's spend.

**Not the same thing as the eval gate, and the distinction is the whole design.** The gate in
`evaluation/gate.yaml` judges a *commit* on aggregate quality and cost, offline, before it ships.
This judges a single *trajectory*, at runtime, while a traveler waits. A gate breach means "do
not merge"; a budget breach means "stop spending this traveler's money and get a human".

So the caps here sit deliberately **above** the gate's thresholds. The gate wants p95 spend under
`$0.60` and p95 steps under 10; if a cap fired at those values it would fire on turns the gate
considers healthy, and a circuit breaker that trips in normal operation gets raised until it is
meaningless. A measured warm turn on this deployment costs about `$0.0086` over 2 steps, so the
defaults below are roughly a hundred times a typical turn — they exist to catch a loop, not to
enforce efficiency.

**Both caps, not one.** Spend is the
honest unit and steps are the one a person perceives as "it's stuck", and they fail differently: a
reflection loop burns steps with small token counts, while one enormous context burns dollars in a
single step. Either alone leaves a real runaway uncaught.

**Three limits on what this actually bounds, all of them worth knowing before trusting it.**

1. **Deployment-wide, not per-tenant.** `budget()` reads one parameter and takes no tenant id, so
   every tenant shares these caps. The comment on `DEFAULT_MAX_USD` says the right number depends on
   a tenant's tolerance, and that is an argument for per-tenant caps rather than a description of
   this code. Making it per-tenant means keying the parameter by tenant and threading the id from
   `context` to here, at which point the cache below has to be keyed too.
2. **Model tokens only.** `usd` comes from the ledger, which prices input, output and cache tokens.
   A trajectory that calls Amazon Location forty times, or drives a knowledge-base retrieval per
   step, spends real money this cap cannot see. It bounds *model* spend per conversation, which is
   the dominant variable cost here and not the whole of it.
3. **It stops the next step, so it can overshoot by one.** The check runs after a step's usage is
   recorded, which is the first moment the new totals exist, so a single invocation that alone
   exceeds the remaining headroom is paid for before anything trips. A cap of $1.00 therefore means
   "no further work after $1.00", not "never more than $1.00". Predicting the next step's cost to
   avoid that would mean guessing, and a guess that stops a turn early is worse than a known
   one-step overshoot.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ~100x a measured typical turn, and above every gate threshold, so this only ever fires on a
# genuine runaway. For scale, the dearest single turn measured on this deployment is $0.268115, a
# booking chain that searched, prepared and confirmed four times over 14 steps and then prepared a
# handoff, so `$1.00` is about 3.7x that.
#
# Overridable because the right number depends on a tenant's tolerance — but note limit 1 in the
# module docstring: the override is deployment-wide, so today "a tenant's tolerance" means whichever
# tenant's tolerance the operator picked.
DEFAULT_MAX_USD = 1.00
DEFAULT_MAX_STEPS = 15

# **A write that keeps being refused is the one loop the step cap is too generous for.** Suite G's
# `hold_expired` produced this: search, hold, confirm, refused because the hold was dead on arrival —
# three times over, eleven steps and $0.163, about four times the next dearest turn in that run,
# before the model escalated on its own. The outcome was right and every quality row passed; what
# failed was `p95_steps`, a cost row catching a behavioural problem the correctness rows could not
# see.
#
# Two, because one refusal is worth retrying — a hold can lapse for a reason the next attempt fixes —
# and a third has never once succeeded here. Counted on *refused* confirmations rather than on holds,
# so a legitimate two-item booking that holds a flight and a hotel is untouched.
DEFAULT_MAX_FAILED_WRITES = 2

# **A dependency that is down is a different situation from a budget that is spent**, and it needs
# its own ceiling because it costs almost nothing to hit: two failed reads of a policy the traveler
# is entitled to, and the turn has spent pennies while helping nobody. Left alone the model offers to
# try again later, which reads like service and leaves the traveler with nothing. A human can answer
# a policy question that the tool cannot currently reach.
#
# Two for the same reason as the write cap: one failure is worth retrying, since a timeout can clear.
# Counted per tool and *consecutively*, so a tool that fails once and then succeeds resets it, and a
# turn that legitimately touches several tools is not penalized for one flaky one.
DEFAULT_MAX_TOOL_FAILURES = 2

BUDGET_PARAM = "/multi-tenant-travel/budget/trajectory"
BUDGET_VAR = "TRAVEL_BUDGET_JSON"

_cached: Budget | None = None


@dataclass(frozen=True)
class Budget:
    max_usd: float = DEFAULT_MAX_USD
    max_steps: int = DEFAULT_MAX_STEPS
    max_failed_writes: int = DEFAULT_MAX_FAILED_WRITES
    max_tool_failures: int = DEFAULT_MAX_TOOL_FAILURES

    def tool_failure_breach(self, *, tool: str, failures: int) -> str | None:
        """The reason to hand off after one tool keeps failing, or `None` to carry on.

        **Separate from `breach` because it is not a budget event**, and reporting it as one would
        put "the assistant stopped itself" against a trigger of `budget_breach` in the ledger when
        nothing was overspent. The ledger's own vocabulary is the reason this is a second method
        rather than a fourth branch: a handoff caused by a dependency outage and a handoff caused by
        a runaway need to be told apart by whoever reads the line afterwards.
        """
        if failures >= self.max_tool_failures:
            return (
                f"{tool} failed {failures} times in a row, limit {self.max_tool_failures} - the "
                "traveler's question is answerable but the system could not reach it"
            )
        return None

    def breach(self, *, steps: int, usd: float | None, failed_writes: int = 0) -> str | None:
        """The reason this trajectory must stop, or `None` to carry on.

        Returns prose rather than a boolean because the string reaches a human agent as the
        escalation reason, and "the assistant stopped" without a number is not something a travel
        desk can act on.

        **An unpriced trajectory does not disable the spend cap silently.** `usd` is `None` when
        the model has no rate card, and treating that as "under budget" would remove the money
        guard on exactly the deployment whose spend nobody is tracking. The step cap still applies
        and the gap is logged, so the failure is visible rather than convenient.
        """
        if failed_writes >= self.max_failed_writes:
            return (
                f"{failed_writes} booking confirmations were refused, limit "
                f"{self.max_failed_writes} — the hold could not be completed"
            )
        if steps >= self.max_steps:
            return f"step budget reached: {steps} steps, limit {self.max_steps}"
        if usd is None:
            log.warning(
                "trajectory is unpriced, so the $%.2f spend cap cannot be enforced on it — the "
                "%d-step cap is still in force. Publish rates to restore the spend guard.",
                self.max_usd,
                self.max_steps,
            )
            return None
        if usd >= self.max_usd:
            return f"spend budget reached: ${usd:.4f}, limit ${self.max_usd:.2f}"
        return None


def _parse(raw: str, source: str) -> Budget | None:
    try:
        published = json.loads(raw)
        return Budget(
            max_usd=float(published.get("max_usd", DEFAULT_MAX_USD)),
            max_steps=int(published.get("max_steps", DEFAULT_MAX_STEPS)),
            max_failed_writes=int(published.get("max_failed_writes", DEFAULT_MAX_FAILED_WRITES)),
            max_tool_failures=int(published.get("max_tool_failures", DEFAULT_MAX_TOOL_FAILURES)),
        )
    except Exception as error:  # noqa: BLE001 - a bad override must be loud, not silent
        log.warning(
            "budget at %s is unusable (%s) — falling back to $%.2f / %d steps, so any limit "
            "published there is NOT in force",
            source,
            type(error).__name__,
            DEFAULT_MAX_USD,
            DEFAULT_MAX_STEPS,
        )
        return None


def budget() -> Budget:
    """The caps in force, from SSM if published, else the defaults above.

    Cached per container for the same reason the model id and rates are: it changes on the order
    of months, and this is consulted after every model step.
    """
    global _cached
    if _cached is not None:
        return _cached

    raw = os.environ.get(BUDGET_VAR)
    source = BUDGET_VAR
    if not raw:
        try:
            import boto3

            ssm = boto3.client("ssm")
            raw = ssm.get_parameter(Name=BUDGET_PARAM)["Parameter"]["Value"]
            source = BUDGET_PARAM
        except Exception as error:  # noqa: BLE001 - see below: absent is fine, everything else is not
            # **An absent parameter is normal; every other failure to read one is not.**
            #
            # The runtime's role was granted SSM only under `/multi-tenant-travel/guardrails/*` and
            # `/model/*` — never `/budget/*`. So every attempt to read a published budget failed with
            # `AccessDenied`, this `except` swallowed it as "not published", and the defaults applied.
            # The documented override mechanism could not work, and nothing anywhere said so. It was
            # found by trying to lower the cap to prove the break-out fires and watching the cap not
            # change.
            #
            # **The first fix for that was itself too narrow, and this is the second.** It special
            # cased `AccessDenied` and stayed silent on everything else, which left throttling,
            # timeouts, expired credentials, a wrong region and a malformed parameter name all
            # indistinguishable from "not published" — the identical defect, one exception class
            # further out. The rule is inverted now: **only `ParameterNotFound` is quiet, because it
            # is the only error that actually means "nothing is published here".**
            #
            # Matched on the name rather than the class so this module still needs no botocore import
            # at module scope. `ClientError` carries the API code in its string, and matching the
            # narrow case conservatively means an unrecognized error is loud rather than swallowed.
            absent = "ParameterNotFound" in type(error).__name__ or "ParameterNotFound" in str(
                error
            )
            if not absent:
                log.error(
                    "cannot read the budget at %s (%s: %s) — any limit published there is NOT in "
                    "force, and the defaults of $%.2f / %d steps apply. If this is a permissions "
                    "error, grant ssm:GetParameter on that prefix; see policies/budget-iam.json",
                    BUDGET_PARAM,
                    type(error).__name__,
                    error,
                    DEFAULT_MAX_USD,
                    DEFAULT_MAX_STEPS,
                )
            raw = None

    _cached = (_parse(raw, source) if raw else None) or Budget()
    return _cached


def reason_for_handoff(breach: str, *, steps: int, usd: float | None, tools: list[str]) -> str:
    """The escalation reason a human agent reads first.

    **Assembled from the ledger's own facts rather than asked of the model.** The model is the
    thing that just misbehaved, so asking it to summarize why would be asking the unreliable
    narrator for the incident report. What was tried, how many steps, and what it cost are all
    recorded, so they are stated.
    """
    spend = "unpriced" if usd is None else f"${usd:.4f}"
    tried = ", ".join(dict.fromkeys(tools)) or "no tools"
    return (
        f"The assistant stopped itself before finishing: {breach}. "
        f"It ran {steps} step(s) costing {spend} and used: {tried}. "
        "The traveler has not been helped yet and needs a person to pick this up."
    )
