"""Hand a conversation to a human after the agent stops itself.

Separate from `main.py` so it can be tested without the runtime: `main.py` imports
`bedrock_agentcore` to declare the entrypoint, which is not installable in the environment the
test suite runs in. Putting the logic here means the budget handoff is exercised directly rather
than through a stub of the whole app — and this is the path that only ever runs when something has
already gone wrong, so it is the last one that should be verified by inspection.
"""

from __future__ import annotations

import logging
from typing import Any

import budget
import stream as ev
from ledger import Trajectory

log = logging.getLogger(__name__)

ESCALATION_TOOL = "escalation___escalate_to_human"

# What the traveler reads when the assistant stops itself. Deliberately does not blame them and
# does not promise a timescale the sample cannot keep.
OVER_BUDGET_MESSAGE = (
    "I've spent longer on this than I should without getting you an answer, so I've prepared a "
    "handoff for your travel desk with everything we've covered. I can't connect you to anyone "
    "directly from here."
)
# Used only when the handoff itself could not be completed — a tenant with no support queue, or an
# unreachable gateway. Saying "I'm connecting you" into a void is the one outcome to avoid.
OVER_BUDGET_NO_HANDOFF = (
    "I've spent longer on this than I should without getting you an answer, and I can't reach your "
    "travel desk from here either. Your internal travel team will be able to help."
)

# **A dependency being down is not the traveler running out of budget**, and telling them it is would
# be a lie about whose fault it is. Same mechanism, different sentence: this one says the system could
# not reach the answer, because that is what happened.
TOOL_FAILURE_MESSAGE = (
    "I couldn't reach the system that holds that answer, and I'd rather not guess at it. I've "
    "prepared a handoff for your travel desk with what you asked and what I tried. I can't connect "
    "you to anyone directly from here."
)
TOOL_FAILURE_NO_HANDOFF = (
    "I couldn't reach the system that holds that answer, and I can't reach your travel desk from "
    "here either. Your internal travel team will be able to help."
)


async def escalate_over_budget(client: Any, trajectory: Trajectory, breach: str) -> Any:
    """Hand off after a budget breach, through the ordinary escalation tool."""
    async for event in _escalate(
        client,
        trajectory,
        breach,
        prefix="budget",
        message=OVER_BUDGET_MESSAGE,
        no_handoff=OVER_BUDGET_NO_HANDOFF,
        label="a budget breach",
    ):
        yield event


async def escalate_after_tool_failures(client: Any, trajectory: Trajectory, breach: str) -> Any:
    """Hand off after one tool has failed repeatedly, through the ordinary escalation tool.

    **Fired by code for the same reason the budget handoff is.** Left to the model, this turn ends
    with an offer: "would you like me to try again, or is there something else I can help with?"
    That reads like service and leaves a traveler who asked an answerable question with nothing. The
    offer is also not a handoff, and the session-level timing judge scores it as one of the worst
    shapes there is: a human was required and the agent stopped at offering one.
    """
    async for event in _escalate(
        client,
        trajectory,
        breach,
        prefix="toolfail",
        message=TOOL_FAILURE_MESSAGE,
        no_handoff=TOOL_FAILURE_NO_HANDOFF,
        label="repeated tool failures",
    ):
        yield event


async def _escalate(
    client: Any,
    trajectory: Trajectory,
    breach: str,
    *,
    prefix: str,
    message: str,
    no_handoff: str,
    label: str,
) -> Any:
    """The shared code-fired handoff.

    **The same tool a traveler's own "get me a human" goes through**, so the tenant's queue lookup,
    its refusal when no queue is configured, and the card the traveler sees are identical on every
    path. A second, code-only handoff would be a second thing to keep correct, and it is the one
    that would rot — nobody demonstrates these paths in a browser.

    The reason is built from the ledger rather than generated, and nothing here raises: a failure to
    escalate must still leave the traveler with something true to act on.
    """
    reason = budget.reason_for_handoff(
        breach,
        steps=trajectory.steps_taken,
        usd=trajectory.cost().get("usd"),
        tools=trajectory.tools_tried,
    )
    try:
        result = await client.call_tool_async(
            tool_use_id=f"{prefix}-{trajectory.session_id or 'session'}",
            name=ESCALATION_TOOL,
            arguments={"reason": reason},
        )
    except Exception:
        log.exception("could not escalate after %s", label)
        yield ev.text(no_handoff)
        return

    cards = ev.cards_in(result) if isinstance(result, dict) else []
    # No card means the tool refused. The commonest cause is a tenant with no support queue, which
    # it reports as a message rather than as an error, so an absent card is the signal — not an
    # exception. Relayed as the honest version instead of a transfer that will not happen.
    yield ev.text(message if cards else no_handoff)
    if cards:
        yield ev.cards(cards)
        log.info("handoff prepared after %s: %s", label, breach)
