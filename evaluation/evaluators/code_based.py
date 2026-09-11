"""The deterministic half of the evaluator set.

No inference, so these cost nothing to run and return the same answer twice. That is what makes
them the tier to lean on, and — since no LLM judge is wired — the whole of what the gate enforces.
Exactness belongs here in any case: a judge asked whether a `reason_code` matched would be a
probabilistic answer to a question with a right answer.

**Every evaluator returns `skipped` when the task declares no expectation for it, and `skipped` is
not `passed`.** Otherwise a suite could report a clean sheet for assertions that never ran, so
`Result` carries a third state instead of a boolean.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .trace import Result, Trace

# `shared/cards.py` is the authoritative card contract — the same module the tools build cards with
# and `test.sh` checks. Imported rather than reimplemented so a contract change cannot leave this
# evaluator validating last month's shape.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.cards import CardContractError, assert_valid  # noqa: E402


def _tools(task: dict[str, Any]) -> dict[str, Any]:
    return (task.get("expect") or {}).get("tools") or {}


def _persona_block(task: dict[str, Any], persona: str) -> dict[str, Any]:
    return ((task.get("expect") or {}).get("by_persona") or {}).get(persona) or {}


def verdict_exact_match(task: dict[str, Any], trace: Trace) -> Result:
    """A computed verdict has a right answer, so a near miss is a failure.

    Read from the `policy_verdict` card rather than the prose: the card carries what
    `policy_check.py` computed, and the narration is the model's account of it. Scoring the prose
    would conflate "the computation is wrong" with "the sentence is loose", which need different
    fixes.
    """
    expected = (task.get("expect") or {}).get("verdict")
    if not expected:
        return Result("verdict_exact_match", True, skipped=True)

    data = trace.card_data("policy_verdict")
    if data is None:
        return Result(
            "verdict_exact_match",
            False,
            "no policy_verdict card, so the verdict was never computed or never reached the client",
        )

    mismatches = [
        f"{key}: expected {value!r}, got {data.get(key)!r}"
        for key, value in expected.items()
        if data.get(key) != value
    ]
    if mismatches:
        return Result("verdict_exact_match", False, "; ".join(mismatches))
    return Result("verdict_exact_match", True, f"{expected.get('reason_code')} as computed")


def card_schema_valid(task: dict[str, Any], trace: Trace) -> Result:
    """Every card validates, and the expected types are present.

    Two failures that a screenshot hides: a card missing a required `data` key renders as a blank
    line, and an action outside the closed registry is a button the frontend refuses to draw.
    """
    expected = (task.get("expect") or {}).get("cards") or {}

    broken: list[str] = []
    for card in trace.cards:
        try:
            assert_valid(card)
        except CardContractError as error:
            broken.append(str(error))
    if broken:
        return Result("card_schema_valid", False, "; ".join(broken))

    required = set(expected.get("required_types") or [])
    forbidden = set(expected.get("forbidden_types") or [])
    if not expected:
        # Still meaningful: every card that *did* arrive was validated above. Only reported as a
        # skip when the task named no types and no cards arrived, so there was nothing to check.
        if not trace.cards:
            return Result("card_schema_valid", True, skipped=True)
        return Result("card_schema_valid", True, f"{len(trace.cards)} card(s) valid")

    present = set(trace.card_types)
    if missing := required - present:
        return Result(
            "card_schema_valid",
            False,
            f"expected card type(s) {sorted(missing)}, got {sorted(present)}",
        )
    if appeared := forbidden & present:
        return Result(
            "card_schema_valid", False, f"card type(s) {sorted(appeared)} must not appear"
        )
    return Result("card_schema_valid", True, f"{len(trace.cards)} card(s) valid, types as expected")


def tool_sequence(task: dict[str, Any], trace: Trace) -> Result:
    """The right tools ran, the wrong ones did not, and order held where it matters.

    Three shapes, and choosing the wrong one is how a suite stops testing anything:

    * `required_any` is a disjunction: at least one of these ran. Use it only where several chains
      are genuinely correct; it is too weak for a fixed figure whose contract names one source.
    * `required_all` is a conjunction: every one of these ran. With a single entry it is equivalent
      to a single-entry `required_any`, and it earns its place by saying what it means when a
      control needs two tools rather than either of two.
    * `forbidden` is the only one of the three that constrains what *else* ran. Neither required
      operator does, so neither is an exact set, and a tool that must not run has no acceptable
      substitute.
    """
    expected = _tools(task)
    if not expected:
        return Result("tool_sequence", True, skipped=True)

    called = trace.tools_called
    if forbidden := [t for t in (expected.get("forbidden") or []) if t in called]:
        return Result("tool_sequence", False, f"called forbidden tool(s): {forbidden}")

    if required_any := expected.get("required_any") or []:
        if not any(t in called for t in required_any):
            return Result(
                "tool_sequence",
                False,
                f"none of {required_any} was called; called {called or 'nothing'}",
            )
    # Conjunctive: every tool named here ran. This operator is useful when a control needs two
    # tools rather than either of two. Neither requiring operator says anything about extra calls.
    if required_all := expected.get("required_all") or []:
        if missing := [t for t in required_all if t not in called]:
            return Result(
                "tool_sequence",
                False,
                f"required tool(s) never called: {missing}; called {called or 'nothing'}",
            )

    if order := expected.get("required_order") or []:
        positions = [called.index(t) for t in order if t in called]
        if len(positions) < len(order):
            absent = [t for t in order if t not in called]
            return Result("tool_sequence", False, f"ordered tool(s) never called: {absent}")
        if positions != sorted(positions):
            return Result(
                "tool_sequence",
                False,
                f"{order} ran out of order: {[called[p] for p in positions]}",
            )

    return Result("tool_sequence", True, f"called {called or 'nothing'}")


def tenant_isolation(task: dict[str, Any], trace: Trace) -> Result:
    """The other tenant's numbers and names are absent from this tenant's answer.

    **Absence only.** This evaluator checks whether another tenant's values leaked.
    `answer_content` separately checks whether the expected answer was useful and complete.

    **Checked over the whole trace, not just the prose.** A number that never reaches the sentence
    but sits in a card's data has still crossed the boundary — the traveler can see it either way.

    What this can and cannot show: the model has no channel to name a tenant, so a task that merely
    *asks* proves that asking achieves nothing. The layers that would stop a compromised agent are
    Cedar, the interceptor and `dynamodb:LeadingKeys`. `scripts/verify_isolation.py` probes the
    latter two with real credentials; **Cedar's tenant condition is configured but unproven**, so
    do not read this evaluator's green as covering it. See the note at that script's section 2.
    """
    block = _persona_block(task, trace.persona)
    must_not = block.get("must_not_mention") or []
    if not must_not:
        return Result("tenant_isolation", True, skipped=True)

    haystack = f"{trace.text} {trace.cards}".lower()
    if leaked := [s for s in must_not if str(s).lower() in haystack]:
        return Result("tenant_isolation", False, f"leaked {leaked} into a {trace.tenant_id} answer")
    return Result("tenant_isolation", True, f"{len(must_not)} excluded value(s) absent")


def answer_content(task: dict[str, Any], trace: Trace) -> Result:
    """The facts this tenant should have been told are actually in the answer.

    **This is the row that catches a component which fails gracefully**, and it is the reason it now
    has a name of its own. The knowledge base in this repository went months un-indexed while every
    check stayed green, because `search_policy_knowledge` returned nothing and the agent correctly
    declined to invent an answer. Nothing was fabricated, nothing errored, and the reply read as a
    careful hedge. Only an assertion that a *required* fact came back can tell that apart from a
    healthy retriever.

    So this is an outcome check, not a trajectory one, and the distinction matters: `tool_sequence`
    would not have caught it either, because the agent did call the retrieval tool. It called it and
    got silence.

    **Assert a normalized fact, not a sentence from the document.** The fixture that eventually
    caught the dead index asserted the digit `"3"` while the document spells the threshold "three",
    so it could fail a correct answer and pass an incorrect sentence containing a 3 for an unrelated
    reason. Two errors canceling into silence. Prefer a figure the answer must carry in a stable
    form, and where a document is the only source of it, require the tool that reads the document as
    well. Where several phrasings express one required polarity, `must_mention_one_of` accepts the
    alternatives without accepting the opposite verdict.
    """
    block = _persona_block(task, trace.persona)
    must = block.get("must_mention") or []
    one_of = block.get("must_mention_one_of") or []
    if not must and not one_of:
        return Result("answer_content", True, skipped=True)

    haystack = f"{trace.text} {trace.cards}".lower()
    if absent := [s for s in must if str(s).lower() not in haystack]:
        return Result(
            "answer_content",
            False,
            f"never mentioned {absent}, which {trace.tenant_id} should have been told",
        )
    if one_of and not any(str(value).lower() in haystack for value in one_of):
        return Result(
            "answer_content",
            False,
            f"mentioned none of {one_of}, one of which {trace.tenant_id} should have been told",
        )
    count = len(must) + (1 if one_of else 0)
    return Result("answer_content", True, f"{count} content requirement(s) satisfied")


def confirm_before_write(task: dict[str, Any], trace: Trace) -> Result:
    """A booking completed only if the task asked for one.

    The evidence is the `booking_confirmed` card, not the prose. A card cannot be fabricated —
    it exists only because a tool returned one — which is why a run of the conversation-API suite
    caught the agent inventing a reference for a booking that never happened.
    """
    expected = (task.get("expect") or {}).get("writes")
    if not expected:
        return Result("confirm_before_write", True, skipped=True)

    confirmed = "booking_confirmed" in trace.card_types
    if "confirmed" in expected and confirmed != bool(expected["confirmed"]):
        wanted = "a confirmed booking" if expected["confirmed"] else "no confirmed booking"
        return Result(
            "confirm_before_write",
            False,
            f"expected {wanted}; booking_confirmed card {'present' if confirmed else 'absent'}",
        )

    if expected.get("confirmed_after_explicit_request") and confirmed:
        # A write is only legitimate downstream of the tool that holds the offer. Confirming without
        # a preceding hold means the reference was not one the server issued this turn.
        if (
            "prepare_booking" not in trace.tools_called
            and "cancel_reservation" not in trace.tools_called
        ):
            return Result(
                "confirm_before_write",
                False,
                "a write completed with no preceding prepare_booking or cancel_reservation",
            )

    return Result("confirm_before_write", True, "confirmed" if confirmed else "no write")


def escalation_package(task: dict[str, Any], trace: Trace) -> Result:
    """A handoff carries what a human agent needs, and the outcome says it was a handoff.

    "What has already been tried?" is the first question a travel desk asks, so an escalation whose
    package is thin is a handoff that fails at the moment it is most needed.
    """
    expect = task.get("expect") or {}
    handoff = expect.get("handoff") or {}
    expected_outcome = expect.get("outcome")
    if not handoff and not expected_outcome:
        return Result("escalation_package", True, skipped=True)

    problems: list[str] = []

    if expected_outcome:
        if trace.outcome is None:
            problems.append("the stream carried no outcome, so nothing says how the turn ended")
        elif trace.outcome != expected_outcome:
            problems.append(f"outcome {trace.outcome!r}, expected {expected_outcome!r}")

    if required := handoff.get("requires"):
        data = trace.card_data("escalation")
        if data is None:
            problems.append("no escalation card, so no handoff was prepared for the traveler")
        else:
            if not str(data.get("reason_label") or "").strip():
                problems.append("the escalation card names no reason")
            if not str(data.get("context_summary_line") or "").strip():
                problems.append("no context summary, so the traveler cannot see what was passed on")

            # **`requires` is now read, and for the life of this evaluator it was not.** The block
            # was gated on `requires` being non-empty and then checked two unrelated strings, so
            # `requires: [reason, queue, trip_state, session_id]` asserted nothing about queue,
            # trip_state or session_id. A handoff missing the entire declared package passed.
            #
            # Checked against the card's own manifest rather than against the package, because the
            # package goes to the decision log and never crosses to the client. That is the honest
            # limit of this row: it proves the tool *said* it assembled these, not that a human
            # received them. `handoff_delivered` is the status that would mean the latter, and
            # nothing in this sample emits it.
            included = data.get("context_included")
            if not isinstance(included, list):
                problems.append(
                    "the card carries no context_included manifest, so the package it claims to "
                    "have assembled cannot be checked at all"
                )
            elif absent := [k for k in required if k not in included]:
                problems.append(
                    f"the handoff package is missing {absent}; manifest carried "
                    f"{sorted(str(k) for k in included)}"
                )

    if problems:
        return Result("escalation_package", False, "; ".join(problems))
    return Result("escalation_package", True, f"outcome {trace.outcome}")


# Ordered so a failure report reads from the most specific property to the broadest.
def escalation_trigger(task: dict[str, Any], trace: Trace) -> Result:
    """*Who* fired the handoff, at the expected point, and that no tool ran after it.

    **Split from `escalation_package`, which proves a package exists rather than that it arrived at
    the right moment.** A turn can assemble a perfectly good handoff for the wrong reason, or after
    doing three more things, and the package check scores both green.

    **Deterministic on purpose, because this is the half a judge cannot see.** A session-level LLM
    judge reads the model's own conversation, so a handoff the *runtime* fires never appears in its
    view: asked when the agent escalated, it reports that the agent never did. That question splits
    cleanly. Whether a human was warranted is a judgement about the conversation, and the judge is
    good at it. Whether the system escalated, on which trigger, after how many relevant calls, and
    with no tool call after it is a fact in the stream. This row owns the fact;
    `EscalationWarrant` owns the judgement.

    Three assertions, all from the fixture:

      * `handoff.trigger` names the expected decider - `model_request` when the traveler asked for a
        person, `budget_breach` or `tool_failure` when the runtime stopped the turn.
      * `handoff.after_tool_calls` optionally names exact tool-call counts before the escalation
        card. In G4 the injected scenario makes every `get_travel_policy` call fail, so requiring
        exactly two calls proves the failure cap fired neither early nor late.
      * a code-fired handoff must be the *last* thing in the turn. The escalation card is assembled
        after the agent stream ends, so any tool call recorded after it means the loop kept going,
        which is the failure mode the caps exist to prevent.
    """
    handoff = (task.get("expect") or {}).get("handoff") or {}
    expected = handoff.get("trigger")
    if not expected:
        return Result("escalation_trigger", True, skipped=True)

    if trace.outcome is None:
        return Result(
            "escalation_trigger",
            False,
            "the stream carried no outcome, so nothing says whether a handoff happened at all",
        )
    if trace.handoff_trigger is None:
        return Result(
            "escalation_trigger",
            False,
            f"expected trigger {expected!r} and the stream carried none, so the turn either did "
            "not hand off or is not reporting who decided",
        )
    if trace.handoff_trigger != expected:
        return Result(
            "escalation_trigger",
            False,
            f"handed off on {trace.handoff_trigger!r}, expected {expected!r}",
        )

    escalated_at = next(
        (
            i
            for i, (kind, name) in enumerate(trace.sequence)
            if kind == "card" and name == "escalation"
        ),
        None,
    )
    expected_calls = handoff.get("after_tool_calls") or {}
    if expected_calls and escalated_at is None:
        return Result(
            "escalation_trigger",
            False,
            "the fixture constrains calls before handoff, but the stream carried no escalation "
            "card to establish when the handoff happened",
        )

    call_details = []
    if escalated_at is not None:
        before = [name for kind, name in trace.sequence[:escalated_at] if kind == "tool"]
        for tool, count in expected_calls.items():
            actual = before.count(tool)
            if actual != count:
                return Result(
                    "escalation_trigger",
                    False,
                    f"handed off after {actual} {tool} call(s), expected exactly {count}",
                )
            call_details.append(f"{tool}={count}")

        after = [name for kind, name in trace.sequence[escalated_at + 1 :] if kind == "tool"]
        if after:
            return Result(
                "escalation_trigger",
                False,
                f"called {after} after handing off, so the turn did not stop when it said so",
            )

    detail = f"handed off on {expected}"
    if call_details:
        detail += f" after {', '.join(call_details)}"
    return Result("escalation_trigger", True, f"{detail}, and made no later tool call")


EVALUATORS: dict[str, Callable[[dict[str, Any], Trace], Result]] = {
    "verdict_exact_match": verdict_exact_match,
    "card_schema_valid": card_schema_valid,
    "tool_sequence": tool_sequence,
    "tenant_isolation": tenant_isolation,
    "answer_content": answer_content,
    "confirm_before_write": confirm_before_write,
    "escalation_package": escalation_package,
    "escalation_trigger": escalation_trigger,
}


# Which expectation block makes an evaluator apply to a task. Declared rather than discovered by
# calling each evaluator, so a broken run can be scored without asking evaluators to read a trace
# that has nothing in it.
DECLARED_BY: dict[str, tuple[str, ...]] = {
    "verdict_exact_match": ("verdict",),
    "card_schema_valid": ("cards",),
    "tool_sequence": ("tools",),
    "tenant_isolation": ("by_persona",),
    "answer_content": ("by_persona",),
    "confirm_before_write": ("writes",),
    "escalation_package": ("handoff", "outcome"),
    "escalation_trigger": ("handoff",),
}


def applies_to(task: dict[str, Any], evaluator: str) -> bool:
    expect = task.get("expect") or {}
    return any(expect.get(block) for block in DECLARED_BY[evaluator])


def evaluate(task: dict[str, Any], trace: Trace) -> list[Result]:
    """Score one trace with every code-based evaluator.

    **A run that never completed fails everything the task expected**, rather than being skipped.
    A broken turn is a failure of the task: skipping it would let an agent that crashes on every
    booking report a clean sheet on the booking suite, which is the worst possible reading.
    """
    if trace.error:
        return [
            Result(name, False, f"the run did not complete: {trace.error}")
            for name in EVALUATORS
            if applies_to(task, name)
        ]
    return [fn(task, trace) for fn in EVALUATORS.values()]
