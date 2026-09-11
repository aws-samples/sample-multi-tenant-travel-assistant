"""The fold from stream to trace, which nothing tested until it dropped a measurement.

`Trace.from_events` is the only path by which a gate row learns anything about a turn, and it had
no test at all. That is how `reflection_steps` went missing: the `done` event did not carry it, the
fold had no reason to look for it, and the runner filled the gap with a hardcoded `0` that scored a
perfect `reflection_step_rate` on every run.

So these tests assert the shape of the fold rather than any particular verdict, and every field that
feeds a gate row is named here on purpose. A field that no test names is a field that can quietly
stop arriving.
"""

from __future__ import annotations

from evaluators import Trace

DONE = {
    "type": "done",
    "usage": {"input": 1085, "cache_read": 11802, "output": 119},
    "steps": 2,
    "outcome": "answered",
    "reflection_steps": 0,
}


def fold(events: list[dict]) -> Trace:
    return Trace.from_events(
        task_id="A1",
        persona="priya",
        tenant_id="globex",
        prompt="does not matter",
        events=events,
    )


def test_every_accounting_field_on_the_done_event_reaches_the_trace():
    trace = fold([{"type": "text", "text": "the cap is 250"}, DONE])
    assert trace.text == "the cap is 250"
    assert trace.usage == DONE["usage"]
    assert trace.steps == 2
    assert trace.outcome == "answered"
    assert trace.reflection_steps == 0, "the field the runner used to hardcode"


def test_a_reflection_count_of_zero_is_not_the_same_as_no_reflection_count():
    """The distinction the hardcoded `0` destroyed.

    A turn that genuinely wasted no steps and a turn that never reported have to be
    distinguishable, or the gate cannot tell a clean run from an unmeasured one.
    """
    measured = fold([{**DONE, "reflection_steps": 0}])
    assert measured.reflection_steps == 0

    silent = fold([{k: v for k, v in DONE.items() if k != "reflection_steps"}])
    assert silent.reflection_steps is None


def test_a_stream_that_never_settled_reports_nothing_rather_than_zero():
    trace = fold([{"type": "text", "text": "half an answer"}, {"type": "error", "message": "boom"}])
    assert trace.steps is None
    assert trace.reflection_steps is None
    assert trace.error == "boom"


def test_tool_names_lose_the_gateway_prefix_and_keep_their_order():
    trace = fold(
        [
            {"type": "tool_start", "tool": "search_hotels"},
            {"type": "tool_start", "tool": "prepare_booking"},
            DONE,
        ]
    )
    assert trace.tools_called == ["search_hotels", "prepare_booking"]


def test_handoff_trigger_and_interleaved_sequence_reach_the_trace():
    trace = fold(
        [
            {"type": "tool_start", "tool": "get_travel_policy"},
            {"type": "tool_start", "tool": "get_travel_policy"},
            {"type": "cards", "cards": [{"card_type": "escalation", "data": {}}]},
            {
                **DONE,
                "outcome": "handoff_prepared",
                "handoff_trigger": "tool_failure",
            },
        ]
    )

    assert trace.handoff_trigger == "tool_failure"
    assert trace.sequence == [
        ("tool", "get_travel_policy"),
        ("tool", "get_travel_policy"),
        ("card", "escalation"),
    ]


def test_an_unknown_event_type_is_ignored_rather_than_fatal():
    """The envelope gains events over time, and a harness that raised on a new one would fail on the
    day the agent got a capability rather than the day it got worse."""
    trace = fold([{"type": "something_from_next_year", "payload": 1}, DONE])
    assert trace.steps == 2
