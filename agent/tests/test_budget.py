"""Budget caps, and the ways a circuit breaker fails to be one.

Three failure modes worth asserting, none of which raises anything:

  * **a cap set at the gate's thresholds**, which trips on healthy turns until somebody raises it
    to a number that never trips at all;
  * **an unpriced trajectory reading as under budget**, which removes the money guard on precisely
    the deployment whose spend nobody is tracking; and
  * **a breach that stops nothing**, because it is noticed after the loop that did the spending.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "MultiTenantTravel" / "app" / "MultiTenantTravel"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import budget as budget_module  # noqa: E402
import pricing  # noqa: E402
from budget import Budget  # noqa: E402
from ledger import Trajectory  # noqa: E402

SONNET = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

# The thresholds `evaluation/gate.yaml` sets for the eval gate. The caps must sit above these, or
# the breaker trips on turns the gate calls healthy.
GATE_P95_USD = 0.60
GATE_P95_STEPS = 10

# A measured warm turn from the deployment, for scale.
MEASURED_TURN_USD = 0.008581
MEASURED_TURN_STEPS = 2


def test_the_caps_sit_above_the_gate_thresholds():
    """A runtime breaker and an offline quality gate are different instruments.

    If the cap equalled the gate's p95, every turn at the edge of acceptable would be escalated to
    a human — and the fix a team reaches for is raising the cap until it stops complaining, which
    ends with a breaker that never fires.
    """
    caps = Budget()
    assert caps.max_usd > GATE_P95_USD
    assert caps.max_steps > GATE_P95_STEPS
    # And far enough above a normal turn to only mean "runaway".
    assert caps.max_usd > MEASURED_TURN_USD * 50
    assert caps.max_steps > MEASURED_TURN_STEPS * 3


def test_a_normal_turn_is_not_a_breach():
    assert Budget().breach(steps=MEASURED_TURN_STEPS, usd=MEASURED_TURN_USD) is None


def test_the_step_cap_catches_a_loop_that_spends_little():
    """A reflection loop burns steps with small token counts, so spend alone would miss it."""
    breach = Budget(max_usd=1.0, max_steps=15).breach(steps=15, usd=0.02)
    assert breach is not None
    assert "step budget" in breach
    assert "15" in breach


def test_the_spend_cap_catches_one_enormous_step():
    """And one huge context burns dollars without burning steps, so steps alone would miss it.

    Both caps exist for this reason — it is the question phase-7 DESIGN left open.
    """
    breach = Budget(max_usd=1.0, max_steps=15).breach(steps=2, usd=1.4)
    assert breach is not None
    assert "spend budget" in breach
    assert "1.40" in breach


def test_an_unpriced_trajectory_keeps_the_step_cap_and_says_the_spend_cap_is_off(caplog):
    """`usd is None` means no rate card — it must not read as "under budget".

    Silently passing would strip the money guard from the one deployment whose spend is already
    unmeasured, which is the opposite of what a missing rate card should cost.
    """
    caps = Budget(max_usd=1.0, max_steps=15)
    with caplog.at_level("WARNING"):
        assert caps.breach(steps=3, usd=None) is None
    assert any("cannot be enforced" in r.getMessage() for r in caplog.records)
    # The step cap is untouched by the gap.
    assert caps.breach(steps=15, usd=None) is not None


def test_the_breach_reason_is_prose_a_travel_desk_can_act_on():
    breach = Budget(max_usd=1.0, max_steps=15).breach(steps=15, usd=0.4)
    reason = budget_module.reason_for_handoff(
        breach, steps=15, usd=0.4, tools=["get_travel_policy", "search_flights"]
    )
    assert "15 step" in reason
    assert "$0.4000" in reason
    assert "get_travel_policy" in reason and "search_flights" in reason
    assert "has not been helped" in reason, "the human needs to know nothing was resolved"


def test_the_handoff_reason_reports_unpriced_spend_honestly():
    reason = budget_module.reason_for_handoff("step budget reached", steps=15, usd=None, tools=[])
    assert "unpriced" in reason
    assert "None" not in reason, "a human agent should never read a Python None"


def test_a_malformed_published_budget_falls_back_loudly(monkeypatch, caplog):
    monkeypatch.setattr(budget_module, "_cached", None)
    monkeypatch.setenv(budget_module.BUDGET_VAR, "{nope")
    with caplog.at_level("WARNING"):
        caps = budget_module.budget()
    assert caps == Budget()
    assert any("unusable" in r.getMessage() for r in caplog.records)


def test_a_published_budget_takes_effect(monkeypatch):
    monkeypatch.setattr(budget_module, "_cached", None)
    monkeypatch.setenv(budget_module.BUDGET_VAR, '{"max_usd": 0.05, "max_steps": 4}')
    caps = budget_module.budget()
    assert caps.max_usd == 0.05
    assert caps.max_steps == 4
    assert caps.breach(steps=2, usd=0.06) is not None


# --- reading the parameter, and the two rounds of getting this wrong ----------------------------
#
# Round one: a bare `except` treated every failure as "not published", so `AccessDenied` from a role
# with no grant on `/budget/*` read as "no override configured" and the documented mechanism could
# not work for the life of the file.
#
# Round two: the fix special-cased `AccessDenied` and stayed silent on everything else, which is the
# same defect one exception class further out. Throttling, a timeout, expired credentials and a
# malformed name all still read as "not published".
#
# So the rule is inverted: only `ParameterNotFound` is quiet. These tests pin both halves, because a
# silent branch is exactly the thing that cannot be spotted by reading the file.


class _Boom(Exception):
    """Stands in for a botocore ClientError, which carries its API code in the message."""


def _ssm_raising(monkeypatch, error: Exception):
    """Point `budget()` at an SSM client that fails, without importing botocore."""
    import sys
    import types

    client = types.SimpleNamespace(get_parameter=lambda **_: (_ for _ in ()).throw(error))
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *_a, **_k: client
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(budget_module, "_cached", None)
    monkeypatch.delenv(budget_module.BUDGET_VAR, raising=False)


def test_an_absent_parameter_is_quiet_because_absent_is_normal(monkeypatch, caplog):
    _ssm_raising(monkeypatch, _Boom("ParameterNotFound: no such parameter"))
    with caplog.at_level("WARNING"):
        caps = budget_module.budget()
    assert caps == Budget()
    assert not caplog.records, "publishing no override is the normal case and must not log"


def test_a_denied_read_is_loud(monkeypatch, caplog):
    """The original defect. The limit is not in force and the log has to say so."""
    _ssm_raising(
        monkeypatch, _Boom("AccessDeniedException: not authorized to perform ssm:GetParameter")
    )
    with caplog.at_level("ERROR"):
        caps = budget_module.budget()
    assert caps == Budget()
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "NOT in force" in message
    assert budget_module.BUDGET_PARAM in message


@pytest.mark.parametrize(
    "error",
    [
        _Boom("ThrottlingException: rate exceeded"),
        _Boom("ExpiredTokenException: the security token included in the request is expired"),
        _Boom("EndpointConnectionError: could not connect to the endpoint URL"),
        _Boom("ValidationException: invalid parameter name"),
        TimeoutError("read timed out"),
    ],
)
def test_every_other_read_failure_is_loud_too(monkeypatch, caplog, error):
    """**The second round of this bug.** Any of these used to be indistinguishable from "absent".

    None of them means "nothing is published". Each means "there may be a limit in force that this
    process could not read", and a circuit breaker running on defaults it did not choose is exactly
    the state that must not be silent.
    """
    _ssm_raising(monkeypatch, error)
    with caplog.at_level("ERROR"):
        caps = budget_module.budget()
    assert caps == Budget()
    assert any("NOT in force" in r.getMessage() for r in caplog.records), (
        f"{type(error).__name__} was swallowed silently"
    )


# --- what the ledger contributes to a handoff -------------------------------------------------


def _turn_with(tool_names: list[str]) -> Trajectory:
    trajectory = Trajectory(
        tenant_id="globex",
        traveler_id="trv_1",
        session_id="sess_1",
        model_id=SONNET,
        prompt_version="abc123",
        pricer=pricing.price,
    )
    for name in tool_names:
        trajectory.record_tool(name)
    trajectory.record_usage({"inputTokens": 400, "outputTokens": 60})
    return trajectory


def test_the_ledger_supplies_what_was_tried_rather_than_the_model():
    """ "What has already been tried?" is a human agent's first question.

    Taken from the recorded tool calls, because the model is the thing that just overran and would
    be an unreliable narrator of its own loop.
    """
    trajectory = _turn_with(["get_travel_policy", "search_flights", "get_travel_policy"])
    assert trajectory.tools_tried == ["get_travel_policy", "search_flights"], "deduped, in order"
    assert trajectory.steps_taken == 1


def test_a_handoff_turn_is_recorded_as_prepared_rather_than_resolved():
    """**A clean handoff succeeded and resolved nobody, and the ledger now says both.**

    It used to record `escalated`, which every cost view counted as a resolved task.

    Otherwise the metric rewards an agent that flails on over one that gives up well.
    """
    trajectory = _turn_with(["get_travel_policy"])
    assert trajectory.as_dict()["outcome"] == "completed"
    trajectory.outcome = "handoff_prepared"
    trajectory.handoff_trigger = "budget_breach"
    line = trajectory.as_dict()
    assert line["outcome"] == "handoff_prepared"
    assert line["handoff_trigger"] == "budget_breach"
    # The spend that triggered it stays on the same line, so the handoff is joinable to its cost.
    assert line["usd"] is not None
    assert line["session_id"] == "sess_1"


class TestTheRefusedWriteCap:
    """**A write that keeps being refused is the loop the step cap is too generous for.**

    Suite G's `hold_expired` scenario produced the case this exists for: search, hold, confirm,
    refused because the hold was dead on arrival, three times over. Eleven steps and $0.163,
    roughly four times the next dearest turn in that run, and the model escalated on its own in
    the end. Every quality row passed, because the outcome was right: nothing was booked and a
    human was brought in. The only row that failed was `p95_steps` at 11 against a ceiling of 10,
    a cost row catching a behavioral problem the correctness rows could not see.
    """

    def test_one_refusal_is_not_a_breach(self):
        """A hold can lapse for a reason the next attempt fixes, so the first retry is allowed."""
        assert Budget().breach(steps=4, usd=0.05, failed_writes=1) is None

    def test_the_second_refusal_stops_the_turn(self):
        reason = Budget().breach(steps=6, usd=0.09, failed_writes=2)
        assert reason is not None
        assert "refused" in reason

    def test_a_confirmed_booking_is_never_a_breach_however_many_attempts_it_took(self):
        """The caller passes 0 once a `booking_confirmed` card has been seen.

        Asserted at this boundary because the distinction lives in `main.py`: the tool knows its
        own call was refused, and only the turn knows whether any attempt produced the card that
        proves a write landed.
        """
        assert Budget().breach(steps=8, usd=0.12, failed_writes=0) is None

    def test_the_reason_names_the_limit_a_travel_desk_can_act_on(self):
        reason = Budget().breach(steps=6, usd=0.09, failed_writes=3) or ""
        assert "3" in reason and "limit 2" in reason

    def test_the_cap_is_absent_by_default_for_callers_that_do_not_pass_it(self):
        """`failed_writes` defaults to 0, so an existing caller cannot be broken by the addition."""
        assert Budget().breach(steps=2, usd=0.01) is None

    def test_a_published_override_takes_effect(self, monkeypatch):
        monkeypatch.setenv(budget_module.BUDGET_VAR, '{"max_failed_writes": 1}')
        budget_module._cached = None
        try:
            assert budget_module.budget().max_failed_writes == 1
        finally:
            budget_module._cached = None


class TestToolFailureCap:
    """**The cap for a dependency that is down rather than a budget that is spent.**

    Left to the model, a tool failing twice ends the turn with an offer: "would you like me to try
    again, or is there something else I can help with?" That is not a handoff, and a traveler who
    asked an answerable question is left holding it. The session-level timing judge rated exactly
    that shape Bad while every gate row stayed green.
    """

    def test_one_failure_is_worth_retrying(self):
        assert (
            budget_module.Budget().tool_failure_breach(tool="get_travel_policy", failures=1) is None
        )

    def test_two_consecutive_failures_hand_off(self):
        reason = budget_module.Budget().tool_failure_breach(tool="get_travel_policy", failures=2)
        assert reason is not None
        assert "get_travel_policy" in reason, "a travel desk needs to know which system was down"
        assert "2" in reason

    def test_it_is_not_reported_as_a_budget_breach(self):
        """The ledger tells the two apart, so nothing overspent must not read as a spend event."""
        caps = budget_module.Budget()
        assert caps.breach(steps=1, usd=0.001, failed_writes=0) is None
        assert caps.tool_failure_breach(tool="get_travel_policy", failures=2) is not None

    def test_the_cap_is_tunable_like_the_others(self, monkeypatch):
        monkeypatch.setenv(budget_module.BUDGET_VAR, '{"max_tool_failures": 5}')
        budget_module._cached = None
        assert budget_module.budget().max_tool_failures == 5

    def test_an_absent_override_keeps_the_documented_default(self, monkeypatch):
        monkeypatch.setenv(budget_module.BUDGET_VAR, '{"max_usd": 2.0}')
        budget_module._cached = None
        assert budget_module.budget().max_tool_failures == budget_module.DEFAULT_MAX_TOOL_FAILURES
