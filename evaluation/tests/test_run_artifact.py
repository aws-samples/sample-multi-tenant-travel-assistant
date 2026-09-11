"""The run artifact's time fields, and what reads them.

**Why these are worth a test.** A timestamp is the one field in the record that cannot be
reconstructed from anything else after the run, and the failure is silent both ways: a naive local
timestamp looks joinable against CloudWatch and is not, and a span read bounded by a record count
rather than by the run's window drops its earliest sessions and reports them as unscoreable for a
reason that has nothing to do with them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from runner.judge import read_stream
from runner.run import _utc_now, epoch_millis, run_window


def _record(task_id: str, started: str, finished: str) -> dict[str, str]:
    """One executed record, reduced to the fields these tests care about."""
    return {
        "task_id": task_id,
        "started_at": f"2026-08-31T{started}Z",
        "finished_at": f"2026-08-31T{finished}Z",
    }


def test_timestamp_is_utc_to_the_second() -> None:
    """Z-suffixed, no microseconds, and parseable back into an aware datetime."""
    stamp = _utc_now()
    assert stamp.endswith("Z"), "a stamp with no zone reads as local and joins against nothing"
    parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.microsecond == 0


def test_epoch_millis_round_trips() -> None:
    assert epoch_millis("2026-08-31T11:54:01Z") == 1788177241000
    assert epoch_millis(_utc_now()) > 0


def test_run_window_spans_first_start_to_last_finish() -> None:
    runs = [
        _record("A1/priya", "11:54:01", "11:54:27"),
        _record("A2/sam", "11:55:02", "11:55:19"),
    ]
    assert run_window(runs) == ("2026-08-31T11:54:01Z", "2026-08-31T11:55:19Z")


def test_run_window_ignores_skipped_records() -> None:
    """A skipped task carries no timestamps, and must not turn the window into `None`."""
    runs = [
        {"task_id": "E4", "skipped": "operator action rather than a prompt"},
        _record("A1/priya", "11:54:01", "11:54:27"),
    ]
    assert run_window(runs) == ("2026-08-31T11:54:01Z", "2026-08-31T11:54:27Z")


def test_run_window_of_nothing_is_none() -> None:
    assert run_window([{"task_id": "E4", "skipped": "why"}]) == (None, None)


class _FakeLogs:
    """One page of log events, recording the kwargs it was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_log_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"events": [{"message": '{"name": "invoke"}'}], "nextBackwardToken": None}


def test_read_stream_bounds_the_read_by_the_run_window() -> None:
    logs = _FakeLogs()
    read_stream(logs, "group", "spans", start_time=1788177241000)
    assert logs.calls[0]["startTime"] == 1788177241000


def test_read_stream_without_a_window_sends_no_start_time() -> None:
    """Absent is absent: sending `startTime=0` would ask CloudWatch for the epoch."""
    logs = _FakeLogs()
    read_stream(logs, "group", "spans")
    assert "startTime" not in logs.calls[0]


def _span(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "traceId": "6a956f98710e80e9", **extra}


def test_a_session_without_its_root_span_is_refused_locally() -> None:
    """**The refusal is predicted rather than paid for.**

    `Evaluate` rejects a payload carrying no agent-invocation span as "Provided input has no
    spans to evaluate", however many tool and HTTP spans came with it. One session in a 58-turn
    run hit this,
    and the cause was in the agent: a break-out that abandoned the stream left `invoke_agent`
    unexported. The judge should name the missing span rather than relay an error that reads as an
    empty session.
    """
    from runner.judge import agent_span_present

    tools_only = [
        _span("POST /invocations", kind="SERVER"),
        _span("execute_tool booking___confirm_booking"),
        _span("mcp tools/call escalation___escalate_to_human"),
    ]
    assert not agent_span_present(tools_only)
    assert agent_span_present([*tools_only, _span("invoke_agent Strands Agents")])


def test_score_refuses_before_calling_evaluate() -> None:
    """No AWS call at all when the root span is missing: an advisory pass must not cost money."""
    from runner.judge import score

    class _NeverCalled:
        def evaluate(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("Evaluate must not be called on a payload with no root span")

    result = score(
        _NeverCalled(),
        evaluator_id="MultiTenantTravel_GroundedNarration",
        session_id="a25e4f85",
        payload=[_span("execute_tool booking___confirm_booking")],
    )
    assert "invoke_agent" in result["error"]


def _choice(finish: str, text: str | None = None) -> dict[str, Any]:
    content = [{"text": text}] if text is not None else [{"toolUse": {"name": "search_hotels"}}]
    return {
        "eventName": "gen_ai.choice",
        "traceId": "6a956f98710e80e9",
        "body": {"finish_reason": finish, "message": {"content": content}},
    }


def _assistant(text: str) -> dict[str, Any]:
    return {
        "eventName": "gen_ai.assistant.message",
        "traceId": "6a956f98710e80e9",
        "body": {"message": {"content": [{"text": text}]}},
    }


def test_a_turn_that_never_spoke_is_not_scoreable() -> None:
    """**A cancelled turn is the case that mattered.**

    Every choice is `tool_use`, so the reply a grounding judge interpolates is empty and it returns
    Pass with "the prose is empty, so there is nothing to audit". That is a pass on nothing. The
    intermediate narration is real prose but it is not the turn's closing claim.
    """
    from runner.judge import final_narration_present

    cancelled = [
        _assistant("I'll search for hotels in Amsterdam and hold the cheapest."),
        _choice("tool_use"),
        _assistant("The hold expired while I was confirming. Let me search again."),
        _choice("tool_use"),
    ]
    assert not final_narration_present(cancelled)
    assert final_narration_present(
        [*cancelled, _choice("end_turn", "Your nightly cap is 250 USD.")]
    )


def test_an_end_turn_with_no_text_does_not_count() -> None:
    from runner.judge import final_narration_present

    assert not final_narration_present([_choice("end_turn", "   ")])


def test_an_unknown_telemetry_shape_falls_back_rather_than_refusing() -> None:
    """No finish reason anywhere is a shape this does not know; refusing every session is worse."""
    from runner.judge import final_narration_present

    assert final_narration_present([_assistant("Your nightly cap is 250 USD.")])


def test_score_refuses_a_turn_that_never_spoke() -> None:
    from runner.judge import score

    class _NeverCalled:
        def evaluate(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("Evaluate must not be called when the closing reply is empty")

    result = score(
        _NeverCalled(),
        evaluator_id="MultiTenantTravel_GroundedNarration",
        session_id="13497086",
        payload=[_span("invoke_agent Strands Agents"), _choice("tool_use")],
    )
    assert "closing reply" in result["error"]


class _Control:
    def list_evaluators(self) -> dict[str, Any]:
        return {
            "evaluators": [
                {"evaluatorId": "MultiTenantTravel_GroundedNarration-abc", "level": "TRACE"},
                {"evaluatorId": "MultiTenantTravel_EscalationWarrant-xyz", "level": "SESSION"},
            ]
        }


class _Recorder:
    def __init__(self) -> None:
        self.request: dict[str, Any] = {}

    def evaluate(self, **request: Any) -> dict[str, Any]:
        self.request = request
        return {"evaluationResults": [{"label": "Bad", "explanation": "never escalated"}]}


def test_the_level_is_resolved_with_the_id() -> None:
    """Hardcoding a level beside a name is the same defect as hardcoding the id."""
    from runner.judge import resolve_evaluator

    assert resolve_evaluator(_Control(), "MultiTenantTravel_EscalationWarrant") == (
        "MultiTenantTravel_EscalationWarrant-xyz",
        "SESSION",
    )
    assert resolve_evaluator(_Control(), "Builtin.Correctness") == ("Builtin.Correctness", "TRACE")


def test_a_session_level_judge_is_not_sent_trace_ids() -> None:
    """**`Evaluate` refuses the pair.** "Only TRACE level evaluators are allowed with traceIds", so
    sending it unconditionally failed every session and read as a broken judge.
    """
    from runner.judge import score

    recorder = _Recorder()
    result = score(
        recorder,
        evaluator_id="MultiTenantTravel_EscalationWarrant-xyz",
        session_id="1349",
        payload=[_span("invoke_agent Strands Agents"), _choice("tool_use")],
        level="SESSION",
    )
    assert "evaluationTarget" not in recorder.request
    assert result["label"] == "Bad"


def test_a_trace_level_judge_still_gets_trace_ids() -> None:
    from runner.judge import score

    recorder = _Recorder()
    score(
        recorder,
        evaluator_id="MultiTenantTravel_GroundedNarration-abc",
        session_id="1349",
        payload=[_span("invoke_agent Strands Agents"), _choice("end_turn", "Your cap is 250 USD.")],
        level="TRACE",
    )
    assert recorder.request["evaluationTarget"] == {"traceIds": ["6a956f98710e80e9"]}


def test_a_session_judge_scores_a_turn_that_never_spoke() -> None:
    """The closing-reply guard is a grounding concern. A timing judge should see a stopped turn:
    that session is exactly the one it exists to have an opinion about.
    """
    from runner.judge import score

    recorder = _Recorder()
    result = score(
        recorder,
        evaluator_id="MultiTenantTravel_EscalationWarrant-xyz",
        session_id="1349",
        payload=[_span("invoke_agent Strands Agents"), _choice("tool_use")],
        level="SESSION",
    )
    assert "error" not in result
