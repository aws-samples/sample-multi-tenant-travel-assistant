"""LLM-as-judge scoring, advisory and never gating.

**Why this is deliberately outside the gate.** The seven code-based evaluators are chosen so that
the same trace scores the same way every time, which is what makes a red row attributable to the
commit rather than to the scorer. A judge gives that up in two ways: it can score one trace two
ways, and it costs money on every run. So the judge output prints beside the gate and contributes
**no row and no exit code**. `gate.yaml` says an unmeasured threshold must not pass; that rule is
about thresholds, and this deliberately has none.

What it is for is the question code cannot answer: did the prose assert only what the tools
returned? `agentcore.json` declares `GroundedNarration` for exactly that, and this is the wiring
that finally calls it.

**The collection step, which is the part that was missing.** `Evaluate` takes a session's spans
inline; it does not read them from CloudWatch itself. The runtime already emits Strands GenAI spans
in OpenTelemetry format, into the `spans` stream of its own log group under unified telemetry — not
the shared `aws/spans` group, which is why looking there suggests no instrumentation at all.

**Two context rules the API enforces and the parameter reference does not mention**, both learned by
being refused:

  * a `spanId` in the reference context is rejected: "Span-level reference inputs are not currently
    supported";
  * with a `traceId` present the context is TRACE-level, and then `assertions` is rejected too —
    "Valid fields: {'expectedResponse'}".

So `assertions` need a session-level context and `expectedResponse` needs a trace-level one.

**A custom judge is driven by its prompt, not by reference inputs.** `assertions` and
`expectedResponse` are consumed by built-in evaluators; a custom `llmAsAJudge` receives only what
its instructions interpolate, and reports anything else as `ignoredReferenceInputFields`. So its
criteria belong in the evaluator's prompt in `agentcore.json`, not in a fixture mapped onto a call.

Which placeholders exist depends on the evaluator's level. A trace-level judge gets `{context}` —
previous turns plus the current turn's user prompt and tool calls — and `{assistant_turn}`, the
model's reply. A prompt that interpolates only `{context}` never sees the prose, and a grounding
judge asked to audit prose it was not given has nothing to fault. Session-level judges get
`{context}` and `{available_tools}`, and there `{context}` already includes assistant responses.

**And the trap this module exists to make loud.** On the session-level path a built-in evaluator
returned `1.0` / "Perfectly Correct" while reporting `ignoredReferenceInputFields: ["assertions"]`.
It had discarded the ground truth and scored the trace anyway. A judge that silently ignores the
fixture is a green light measuring nothing, so any ignored field is printed as a warning rather than
buried in the response.
"""

from __future__ import annotations

import json
from typing import Any

# Under unified telemetry the runtime writes two streams into its own log group, and both are
# needed. `spans` carries the trace structure and the tool calls; `otel-rt-logs` carries the
# conversation as OpenTelemetry *log records*: the user's message, the model's replies, tool
# results.
SPAN_STREAM = "spans"
LOG_STREAM = "otel-rt-logs"

# An evaluator scores one turn or a whole session, and the call shape differs: only a TRACE-level
# evaluator accepts `traceIds`, and only a TRACE-level judge reads a single turn's closing reply.
TRACE_LEVEL = "TRACE"
SESSION_LEVEL = "SESSION"

# Enough to cover a long trajectory without paging a whole day of traffic.
READ_LIMIT = 4000

# The event names that carry conversation content. `gen_ai.choice` is the model's own output.
NARRATION_EVENTS = ("gen_ai.choice", "gen_ai.assistant.message")


def resolve_evaluator(control: Any, wanted: str) -> tuple[str, str]:
    """A deployed evaluator's id and level, from its stable name prefix.

    **Read from the deployment rather than pasted from one.** A custom evaluator's id carries a
    random suffix (`MultiTenantTravel_GroundedNarration-sGlb119Xvp`), so a hardcoded value survives
    exactly until the next redeploy and then fails as "evaluator not found" on a judge that is
    perfectly healthy. Built-in and third-party ids are stable and pass straight through.

    **The level comes back with the id because the call shape depends on it**, and getting that
    wrong makes a healthy evaluator look broken: `Evaluate` refuses `traceIds` for a SESSION-level
    evaluator with "Only TRACE level evaluators are allowed with traceIds". Asking the deployment
    beats hardcoding a level next to a name, for the same reason as the id.
    """
    if wanted.startswith(("Builtin.", "ThirdParty.")):
        return wanted, TRACE_LEVEL
    listed = control.list_evaluators().get("evaluators") or []
    matches = [e for e in listed if e["evaluatorId"].startswith(wanted)]
    if not matches:
        raise LookupError(f"no deployed evaluator whose id starts with {wanted!r}")
    if len(matches) > 1:
        raise LookupError(
            f"{wanted!r} matches more than one evaluator: {[e['evaluatorId'] for e in matches]}"
        )
    return matches[0]["evaluatorId"], matches[0].get("level") or TRACE_LEVEL


def log_group(runtime_arn: str) -> str:
    """`/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`, derived from the deployed ARN."""
    return f"/aws/bedrock-agentcore/runtimes/{runtime_arn.rsplit('/', 1)[-1]}-DEFAULT"


def read_stream(
    logs: Any,
    group: str,
    stream: str,
    limit: int = READ_LIMIT,
    *,
    start_time: int | None = None,
) -> list[dict]:
    """Newest records first, parsed. A malformed line is skipped rather than fatal.

    A single unparseable record must not hide a readable session: this is diagnostic tooling, and
    failing the whole read on one bad line is how it gets abandoned for the wrong reason.

    `start_time` is epoch milliseconds and bounds the read to a run's own window. Without it the
    read is bounded only by `limit`, so a long run loses its earliest sessions off the far end.
    """
    records: list[dict] = []
    token: str | None = None
    while len(records) < limit:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": False,
            "limit": 1000,
        }
        if start_time is not None:
            kwargs["startTime"] = start_time
        if token:
            kwargs["nextToken"] = token
        page = logs.get_log_events(**kwargs)
        events = page.get("events") or []
        if not events:
            break
        for event in events:
            try:
                records.append(json.loads(event["message"]))
            except json.JSONDecodeError:
                continue
        nxt = page.get("nextBackwardToken")
        if not nxt or nxt == token:
            break
        token = nxt
    return records


def trace_ids(session_spans: list[dict]) -> set[str]:
    """Every trace in this session, which is how the conversation records are found.

    **The log records carry no `session.id`.** They have a trace id and a span id and nothing else
    to tie them to a conversation, so the session filter has to run on the spans first and the trace
    ids it yields are the key for the second read. Filtering the log stream by session id directly
    returns nothing, which reads as an empty conversation rather than as the wrong join.
    """
    return {span["traceId"] for span in session_spans if span.get("traceId")}


def spans_for(spans: list[dict], session_id: str) -> list[dict]:
    """The spans belonging to one conversation, by the `session.id` the runtime stamps on each."""
    return [s for s in spans if ((s.get("attributes") or {}).get("session.id")) == session_id]


def conversation_records(records: list[dict], traces: set[str], session_id: str) -> list[dict]:
    """The log records belonging to these traces that carry conversation content.

    Kept on the same test the AWS sample applies to its own runtime logs: a record is relevant if it
    has `gen_ai` attributes, or if its body carries the conversation. Body shapes differ between
    telemetry modes — `content` for a message, `message` for a choice, `input`/`output` in the split
    arrangement — so all of them count.

    **Each record is given span start and end times, and without that the judge sees nothing.**
    `Evaluate` parses every entry in `sessionSpans` as a span and rejects one that has no
    `startTimeUnixNano`/`endTimeUnixNano`:

        Failed to parse span data for entry at index [73] ... Field 'start_time' is required

    A log record carries `timeUnixNano` instead, being an instant rather than an interval. Left
    alone the whole call fails; dropped, the conversation goes with it and the grounding judge
    reports that no prose was in the trace while returning **Pass**. So the record's own timestamp
    is copied into both fields. Nothing about the content is invented — this is the instant restated
    as a zero-length interval, which is what a span schema can hold.

    **And the session id is stamped on, because the API requires one session per call.** A log
    record carries only a trace id, so an unstamped record reads as belonging to no session and the
    call is refused with "EvaluationInput has spans from more than one session". The value is not a
    guess: these records were selected by trace ids taken from that session's own spans.
    """
    relevant: list[dict] = []
    for record in records:
        if record.get("traceId") not in traces:
            continue
        keep = any(str(key).startswith("gen_ai") for key in (record.get("attributes") or {}))
        body = record.get("body")
        if not keep and isinstance(body, dict):
            keep = any(key in body for key in ("content", "message", "input", "output"))
        if not keep:
            continue

        stamped = dict(record)
        instant = record.get("timeUnixNano") or record.get("observedTimeUnixNano")
        if instant is not None:
            stamped.setdefault("startTimeUnixNano", instant)
            stamped.setdefault("endTimeUnixNano", instant)
        # A log record has no span name, and an unnamed entry is harder to read in a judge's
        # explanation than one labelled with the event it came from.
        stamped.setdefault("name", record.get("eventName") or "log")
        attributes = dict(stamped.get("attributes") or {})
        attributes.setdefault("session.id", session_id)
        stamped["attributes"] = attributes
        relevant.append(stamped)
    return relevant


def agent_span_present(payload: list[dict]) -> bool:
    """Does this payload carry the span that wraps the agent invocation?

    **The refusal this predicts, rather than pays for.** `Evaluate` rejects a payload with no
    agent-invocation span as "Provided input has no spans to evaluate", however many tool and HTTP
    spans it contains. That happened to exactly one session in a 58-turn run, and the cause was in
    the agent rather than in the judge: a break-out that abandoned the stream left Strands'
    `invoke_agent` span unexited, so it was never exported. Checked here so the report names the
    missing span instead of relaying an error that sounds like an empty session.
    """
    return any(str(record.get("name", "")).startswith("invoke_agent") for record in payload)


def _anchor_trace(payload: list[dict]) -> str:
    """The trace carrying the agent invocation, which is the turn a judge should score."""
    for record in payload:
        if str(record.get("name", "")).startswith("invoke_agent"):
            return record.get("traceId", "")
    for record in payload:
        if record.get("kind") == "SERVER":
            return record.get("traceId", "")
    return payload[0].get("traceId", "") if payload else ""


def _texts(content: Any) -> list[str]:
    """The text parts of a message content block, ignoring tool-use parts."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, dict):
        value = content.get("text")
        return [value] if isinstance(value, str) and value.strip() else []
    if isinstance(content, list):
        found: list[str] = []
        for part in content:
            found.extend(_texts(part))
        return found
    return []


# A choice that stops to call a tool has not spoken yet. Any other finish reason ends the turn.
TOOL_USE_FINISH = "tool_use"


def _choice_bodies(payload: list[dict]) -> list[dict]:
    """The `gen_ai.choice` bodies, which are where a finish reason lives."""
    bodies = []
    for record in payload:
        if record.get("eventName") == "gen_ai.choice" and isinstance(record.get("body"), dict):
            bodies.append(record["body"])
    return bodies


def final_narration_present(payload: list[dict]) -> bool:
    """Did the turn end with the model saying something?

    **Stricter than `narration_present`, and the difference is a real verdict.** A grounding judge
    audits the turn's *final* assistant reply, so intermediate narration is not what it reads. A
    cancelled turn has plenty of intermediate prose and no closing reply: every `gen_ai.choice` is
    `tool_use`, the reply the judge interpolates is empty, and the judge dutifully reports **Pass**
    with the explanation "the prose is empty, so there is nothing to audit". A pass on nothing.

    That is exactly what a budget-breach turn looks like: it is stopped mid-chain, and the
    escalation the traveler reads is assembled by `handoff.py` rather than spoken by the model, so
    it is not in the trace at all and never was the model's claim to audit.

    A payload whose choices carry no finish reason at all is a telemetry shape this does not know,
    so it falls back to the looser check rather than reporting every session unscoreable.
    """
    saw_finish_reason = False
    for body in _choice_bodies(payload):
        finish = body.get("finish_reason")
        if finish:
            saw_finish_reason = True
        if finish and finish != TOOL_USE_FINISH:
            message = body.get("message")
            if isinstance(message, dict) and _texts(message.get("content")):
                return True
            if _texts(body.get("content")):
                return True
    if saw_finish_reason:
        return False
    return narration_present(payload)


def narration_present(payload: list[dict]) -> bool:
    """Does this payload carry the model's own words anywhere?

    **The check that stops a vacuous pass.** A grounding judge audits whether the narration
    asserted only what the tools returned. Given tool calls but no assistant text it finds no
    violations and reports **Pass**: a green verdict on evidence it never received, which is the
    failure this whole suite exists to catch. So an unscoreable session is reported as such.

    **Text, not merely a message.** A `gen_ai.choice` whose `finish_reason` is `tool_use` carries a
    `message` with `tool_calls` and no prose at all, so testing for the presence of a message passes
    on a turn that never spoke. Only the text parts count, which in practice means the `end_turn`
    choice: `body.message.content = [{"text": "Your hotel nightly cap is ..."}]`.

    **This checks the payload, not the judge's view of it.** It confirms the prose was sent; whether
    the evaluator reads it depends on the placeholders its prompt interpolates. So this prevents
    paying for an obviously empty audit rather than guaranteeing a meaningful one.
    """
    for record in payload:
        if record.get("eventName") in NARRATION_EVENTS:
            body = record.get("body") or {}
            if isinstance(body, dict):
                message = body.get("message")
                if isinstance(message, dict) and _texts(message.get("content")):
                    return True
                if _texts(body.get("content")):
                    return True
        for event in record.get("events") or []:
            if event.get("name") in NARRATION_EVENTS and _texts(
                (event.get("attributes") or {}).get("content")
            ):
                return True
    return False


def score(
    agentcore: Any,
    *,
    evaluator_id: str,
    session_id: str,
    payload: list[dict],
    level: str = TRACE_LEVEL,
    expected_response: str | None = None,
    assertions: list[str] | None = None,
) -> dict[str, Any]:
    """One `Evaluate` call. Returns a small summary rather than the raw response.

    The reference input is shaped by which ground truth is available, because the API ties the
    permitted fields to the context level — see the module docstring.
    """
    if not agent_span_present(payload):
        return {
            "error": (
                "no invoke_agent span in this session, so Evaluate has nothing to score - "
                "the turn ended without closing the agent stream"
            )
        }
    # **Only a trace-level judge needs a closing reply.** It reads one turn's reply, so an empty one
    # makes its verdict vacuous. A session-level judge reads the whole session, and a turn that was
    # stopped mid-chain is exactly the session it should have an opinion about, so refusing it here
    # would hide the case the judge exists for.
    if level == TRACE_LEVEL and not final_narration_present(payload):
        return {
            "error": (
                "the turn never reached a closing reply, so the prose a grounding judge reads is "
                "empty and its verdict would be vacuous"
            )
        }

    request: dict[str, Any] = {
        "evaluatorId": evaluator_id,
        "evaluationInput": {"sessionSpans": payload},
    }
    # **`traceIds` only for a trace-level evaluator.** `Evaluate` refuses the pair outright: "Only
    # TRACE level evaluators are allowed with traceIds". Sending it unconditionally made the
    # session-level judge fail on every session, which reads as a broken judge rather than a
    # malformed call.
    trace_id = _anchor_trace(payload)
    if trace_id and level == TRACE_LEVEL:
        request["evaluationTarget"] = {"traceIds": [trace_id]}

    if expected_response and level == TRACE_LEVEL:
        request["evaluationReferenceInputs"] = [
            {
                "context": {"spanContext": {"sessionId": session_id, "traceId": trace_id}},
                "expectedResponse": {"text": expected_response},
            }
        ]
    elif assertions:
        request["evaluationReferenceInputs"] = [
            {
                "context": {"spanContext": {"sessionId": session_id}},
                "assertions": [{"text": text} for text in assertions],
            }
        ]

    try:
        response = agentcore.evaluate(**request)
    except Exception as error:  # noqa: BLE001 - advisory output must not end a gate run
        return {"error": str(error)}

    results = response.get("evaluationResults") or []
    if not results:
        return {"error": "no evaluationResults in the response"}
    first = results[0]
    usage = first.get("tokenUsage") or {}
    return {
        "value": first.get("value"),
        "label": first.get("label"),
        "explanation": first.get("explanation") or "",
        "tokens": usage.get("totalTokens"),
        # Printed loudly by the caller. A judge that ignored the ground truth scored something else.
        "ignored": first.get("ignoredReferenceInputFields") or [],
    }


def report(scores: list[dict[str, Any]]) -> str:
    """Advisory block. Deliberately carries no PASS/FAIL and no threshold."""
    lines = [
        "advisory — LLM-as-judge, no gate row and no effect on the exit code",
        "",
    ]
    for entry in scores:
        task = entry.get("task_id", "?")
        if entry.get("error"):
            lines.append(f"  {task:14} could not be scored: {entry['error'][:120]}")
            continue
        # **A custom judge returns a label and no numeric value**, where a built-in returns both.
        # Printing `None (Pass)` reads like a bug in the runner rather than a difference between
        # evaluators, so the score is whichever of the two the evaluator actually supplied.
        value, label = entry.get("value"), entry.get("label")
        score_text = f"{value} ({label})" if value is not None else str(label)
        lines.append(f"  {task:14} {score_text}, {entry.get('tokens')} judge tokens")
        if entry.get("ignored"):
            lines.append(
                f"  {'':14} WARNING: the judge ignored {entry['ignored']} — that score was "
                "not measured against the fixture"
            )
        first_line = (entry.get("explanation") or "").strip().splitlines()
        if first_line:
            lines.append(f"  {'':14} {first_line[0][:150]}")
    total = sum(e.get("tokens") or 0 for e in scores if not e.get("error"))
    lines += ["", f"  {len(scores)} session(s) scored, {total} judge tokens"]
    return "\n".join(lines)
