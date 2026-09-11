"""The agent entrypoint.

Three things happen per invocation, in this order:

1. **Read the traveler's bearer token** from the request headers. Runtime already
   validated it against Cognito; we forward it to the Gateway unchanged so the request
   interceptor can verify it again at the tool boundary and inject tenant context. The
   agent never asserts who is asking to a *tool* — the one place it reads identity itself
   is memory, which must name an actor; see `memory.py`.
2. **Build the agent** with a tenant-invariant system prompt and the Gateway's tools.
3. **Stream typed events**, not bare text — so tool status reaches the UI and token usage
   reaches the ledger. The scaffold yielded `event["data"]` and discarded both.

**The agent is built per invocation, not cached in a module global.** The scaffold cached
one; here the MCP client carries *this traveler's* credential, so a shared instance would
mean acting for whoever warmed the container.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from opentelemetry import trace as trace_api
from strands import Agent

import budget as budget_config
import handoff
import metrics
import stream as ev
import tool_choice
import unclaimed
from ledger import Trajectory
from mcp_client.client import get_gateway_client, list_tools
from memory import conversation_memory, identity, session_context
from model.load import guardrail_id, load_model, model_id
from pricing import price
from prompts.manager import prompt_version, system_blocks

app = BedrockAgentCoreApp()
log = app.logger
# Only used for the code-fired handoff below. Everything else in the turn is instrumented by Strands
# and the AWS distro, and a second tracer over the same work would double-count it.
tracer = trace_api.get_tracer(__name__)

# Exported beside the tool schemas and copied into the bundle at deploy time.
ev.load_labels(os.path.join(os.path.dirname(__file__), "tool-labels.json"))

NO_TOOLS_MESSAGE = (
    "I can't reach your travel information right now, so I'd rather not guess. "
    "Please try again in a moment."
)


async def _traced_escalation(
    agent: Any, trajectory: Any, reason: str, *, escalate: Any, separate: bool = False
) -> Any:
    """A code-fired handoff, wrapped in a span parented to the agent's own.

    **Why this needs a span at all.** The break-out fires the handoff from code, after the agent
    stream has ended, so nothing about it lands in the agent's trace: a session-level judge asked
    *when* the agent escalated sees a session that never escalated, and rates the timing on a trace
    that is missing the event being judged. The tool's own gateway span exists but hangs off no
    trace of ours.

    **Named and attributed as the tool execution it is.** A span of our own invention was still
    invisible: the judge's view of a session is rendered from the GenAI-shaped records, so a
    correctly parented span called `escalate_from_code` was read as no escalation at all. This is a
    real invocation of a real tool, so it carries the same `gen_ai.tool.*` attributes Strands puts on
    the ones the model fires. Nothing here is invented for the judge's benefit; the only thing that
    differs from a model-fired call is who decided to make it, which is what `handoff.trigger` says.

    Never fatal, and that is the point of the fallback: a tracer that cannot start a span must not
    cost a traveler their handoff.
    """
    # **Only the span is guarded, and the escalation runs exactly once.** Wrapping the iteration in
    # the same `try` would re-run it from the fallback after a partial yield, sending the traveler a
    # second card because a tracer misbehaved.
    span: Any = contextlib.nullcontext()
    try:
        parent = getattr(agent, "trace_span", None)
        span = tracer.start_as_current_span(
            f"execute_tool {handoff.ESCALATION_TOOL}",
            context=trace_api.set_span_in_context(parent) if parent is not None else None,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": handoff.ESCALATION_TOOL,
                "gen_ai.tool.call.id": f"{trajectory.handoff_trigger}-{trajectory.session_id}",
                "handoff.trigger": trajectory.handoff_trigger or "unknown",
                "handoff.reason": reason,
                "session.id": trajectory.session_id,
                "tenant.id": trajectory.tenant_id,
            },
        )
    except Exception:  # noqa: BLE001 - a trace is not worth a handoff
        log.warning("could not trace the code-fired escalation", exc_info=True)

    prepared = False
    with span:
        async for event in escalate:
            prepared = prepared or bool(event.get("cards"))
            # **A paragraph break, because the model's last sentence ends without one.** Streamed
            # prose carries no trailing whitespace, so the handoff message butted straight onto it:
            # "Let me try again.I couldn't reach the system that holds that answer". Two messages
            # from two authors need to look like two messages.
            if separate and isinstance(event.get("text"), str):
                event = {**event, "text": f"\n\n{event['text'].lstrip()}"}
                separate = False
            yield event
        # **The outcome on the span, because a refused handoff is the case worth finding.** No card
        # means the tool declined, usually a tenant with no queue, and a trace that recorded only the
        # attempt would read as a completed handoff.
        with contextlib.suppress(Exception):
            current = trace_api.get_current_span()
            current.set_attribute("gen_ai.tool.status", "success" if prepared else "error")
            current.add_event(
                "gen_ai.tool.message",
                attributes={"role": "tool", "content": reason, "handoff.prepared": prepared},
            )


def _bearer(context: Any) -> str | None:
    """The traveler's access token, from the allowlisted Authorization header.

    Absent means the runtime's `requestHeaderAllowlist` omits `Authorization` — a
    configuration fault, not a user error, and one that silently removes every tool.
    """
    headers = getattr(context, "request_headers", None) or {}
    if hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).lower() == "authorization":
                token = str(value)
                return token[7:].strip() if token.lower().startswith("bearer ") else token
    return None


def _session_id(context: Any) -> str | None:
    for attr in ("session_id", "runtime_session_id"):
        if value := getattr(context, attr, None):
            return str(value)
    return None


def _today_block() -> str:
    """Today's date, as a post-cache-breakpoint system block.

    **Why the model needs telling at all:** it defaults to its training-era year whenever it writes a
    date it was not given. Measured — asked to prepare a booking from a December 2026 search, it sent
    `2024-12-05`, the backend regenerated a different option set, and the hold was refused. A travel
    assistant that cannot place "next Tuesday" is wrong about the one thing it is for.

    **A system block after the cache breakpoint, not a prefix on the user message** — and that move is
    the point. It joins the session-identity block in the uncached suffix (see
    `prompts.manager.system_blocks`): the stable prefix is still read from cache, while this is re-sent
    fresh every turn, so it can never go stale. Unlike the old per-turn user prefix, it also never
    enters the transcript stored in Memory, so a reopened conversation shows what the traveler typed
    rather than an internal `[Today is …]` annotation.

    UTC rather than a traveler's local zone: the date is a reference point for resolving relative
    phrases, and the tools take explicit `YYYY-MM-DD` values anyway. A per-traveler timezone would be
    a second source of truth for "now" with no question that needs it.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        f"<today>Today's date is {today} (UTC). "
        'Use it to resolve relative dates such as "next Tuesday" or "10 November".</today>'
    )


def _post_cache_context(bearer: str | None) -> str:
    """The uncached system suffix: today's date, plus who the turn is for when the claims are present.

    Both pieces sit after the cache breakpoint, so the stable prefix still cache-hits. Joined here so
    the date is always supplied even on a turn with no verified identity (where `session_context`
    returns `None`).
    """
    return "\n".join(block for block in (_today_block(), session_context(bearer)) if block)


def _tool_result_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool results carried on a message event.

    Strands formats completed tool calls into a conversation message, and *that* is what
    reaches `stream_async` — `ToolResultEvent` itself is marked `is_callback_event = False`
    and never arrives. Content blocks look like
    `{"toolResult": {"toolUseId": ..., "status": ...}}`.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    return [
        block["toolResult"]
        for block in message.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("toolResult"), dict)
    ]


@app.entrypoint
async def invoke(payload: dict[str, Any], context: Any):
    """Handle one turn, yielding typed stream events."""
    prompt = (payload or {}).get("prompt") or ""
    # **A tool the BFF says this turn must call, or `None`.** Only a write-path *click* sets it: the
    # traveler has already chosen, so which tool answers is not a judgement the model needs to make.
    # See `tool_choice.py` for how it is applied and why persuasion was not enough.
    force_tool = (payload or {}).get("force_tool") or None
    session_id = _session_id(context)
    token = _bearer(context)

    model = load_model()
    # **Stamped here after all, reversing the earlier choice, and the reason is specific.** This
    # used to be left absent on the argument that the interceptor's record is the copy an auditor
    # can check without trusting this process — which is still true, and still where an *audit*
    # should read it from. But a CloudWatch metric takes its dimensions at publication time, so
    # "which tenant is expensive?" as a dimension on a graph or an alarm cannot be recovered by
    # joining logs afterwards. The values come from the same runtime-verified token `memory.py`
    # already reads, on a path the model cannot reach, so this is not the model naming a tenant.
    # The interceptor's copy remains authoritative for audit, and if the two ever disagree that is
    # itself a signal worth having rather than a dimension worth losing.
    tenant_id, traveler_id = identity(token)
    trajectory = Trajectory(
        tenant_id=tenant_id,
        traveler_id=traveler_id,
        session_id=session_id,
        model_id=model_id(model),
        prompt_version=prompt_version(),
        guardrail_id=guardrail_id(),
        # Supplied here rather than defaulted inside the ledger, so the ledger stays a record of
        # facts and this is the one line that decides a trajectory gets a dollar figure at all.
        pricer=price,
    )

    # The session id travels to the tools so a DynamoDB row read lands in CloudTrail tagged with
    # the conversation that caused it — the same value the ledger records, so cost and audit join
    # on one dimension instead of needing a mapping.
    client = get_gateway_client(token, session_id)
    if client is None:
        yield ev.text(NO_TOOLS_MESSAGE)
        # **Recorded as an outcome, not dropped.** A turn that never reached a tool still consumed
        # a traveler's question, and leaving it out of the ledger would remove it from the
        # denominator of the per-task cost figures — flattering the success rate by hiding the
        # failures rather than by fixing anything.
        trajectory.outcome = "failed_no_gateway"
        metrics.publish_trajectory(log, trajectory.emit(log))
        yield ev.done()
        return

    # A context manager: the MCP session lives for this turn only, because it carries this
    # traveler's credential.
    with client:
        try:
            tools = list_tools(client)
        except Exception:
            log.exception("could not list gateway tools")
            yield ev.text(NO_TOOLS_MESSAGE)
            trajectory.outcome = "failed_no_tools"
            metrics.publish_trajectory(log, trajectory.emit(log))
            yield ev.done()
            return

        log.info(
            "agent ready: %d tool(s), prompt %s, session %s",
            len(tools),
            trajectory.prompt_version,
            session_id,
        )

        # **AgentCore Memory: this conversation's history, and this traveler's preferences.**
        # Without it a booking cannot complete — "book the first one" only means something if the
        # previous turn's search is still in context, and the write path is three turns by design.
        #
        # The token is passed because memory is the one place the agent must name *who* it is acting
        # for; see `memory.py` for why reading a runtime-verified claim is not the model choosing a
        # tenant.
        # **The system prompt is two blocks with a cache breakpoint between them**, so this session's
        # identity costs its own tokens and nothing more: the stable prefix is still read from cache.
        # Measured — three travelers across two tenants each read the same 1042 cached tokens.
        #
        # The turn's date rides in that same uncached suffix (`_post_cache_context`), not on the user
        # message: the post-breakpoint block is re-sent every request, so the date stays current, and
        # it never lands in the stored transcript the way a message prefix would.
        agent = Agent(
            model=model,
            system_prompt=system_blocks(_post_cache_context(token)),
            tools=tools,
            session_manager=conversation_memory(session_id, token),
        )

        # **Force the tool when the click already decided it.** Only the BFF's write-path clicks set
        # this, so a typed turn is untouched and ordinary conversation is unchanged. Registered per
        # invocation because the agent is too — see `tool_choice.py` for why phrasing alone was not
        # enough and what forcing deliberately does *not* decide.
        if force_tool and tool_choice.force(agent, force_tool, tools):
            log.info("forcing %s on this turn", force_tool)

        open_tools: dict[str, str] = {}
        # Cached per container, so this is a dict lookup rather than an SSM call per turn.
        caps = budget_config.budget()
        breach: str | None = None
        # **Refused confirmations, counted here because no other layer sees the pair.** The tool
        # knows its own call refused; only the turn knows it is the third. A `booking_confirmed`
        # card is the only proof a write landed, so an attempt with no such card is a refusal.
        confirm_attempts = 0
        confirmed = False
        trajectory.start_step()

        # **Refuses to relay a booking the agent did not make.** Measured: asked "yes, confirm it" after
        # a prepared booking, the model answered *"your flight is confirmed… charged to your Visa"* with
        # no tool call at all, in 4 of 6 runs. The prompt already forbids exactly that, and four attempts
        # to strengthen the wording moved the number around without fixing it — one made it worse. So the
        # claim is checked rather than requested. See `unclaimed.py`.
        guard = unclaimed.ClaimGuard()

        # **The same defense for "I'm connecting you now."** Found in the same browser sweep that
        # redeployed the SigV4 fix: asked for a human, the agent claimed a handoff with
        # `escalate_to_human` never invoked — confirmed against CloudWatch, which showed no log
        # stream for the tool. See `unclaimed.py` for why this is a card-gated guard rather than a
        # third `ClaimKind` on the one above.
        handoff_guard = unclaimed.HumanHandoffGuard()

        # Set once any prose has reached the traveler, so a code-fired handoff can start a new
        # paragraph rather than run onto the end of the model's last sentence.
        spoke = False
        # Set once a cap has fired, so the cancellation and its log line happen exactly once while
        # the stream drains. See the breach branch below for why draining matters.
        breach: str | None = None
        # The same, for the dependency-outage handoff. A separate name because the two reach the
        # traveler as different sentences and the ledger as different triggers.
        failure: str | None = None
        # **Consecutive** failures per tool, so a tool that fails once and then succeeds resets. A
        # running total would hand off on two unrelated blips in a long, otherwise healthy turn.
        tool_failures: dict[str, int] = {}

        async for event in agent.stream_async(prompt):
            # **Nothing the model says after a cap fires reaches the traveler.** Cancellation stops
            # the next model round, not the tokens already in flight, so the tail of an abandoned
            # sentence used to be delivered and then have the handoff message appended to it: "I'll
            # I've spent longer on this than I should". The code-fired handoff is now the last word
            # on the turn, which is also the only honest ending once the run has been stopped.
            if (breach or failure) and "data" in event:
                continue
            if isinstance(event.get("data"), str):
                # May return "" while a possible claim is buffered; `flush` below releases or rewrites it.
                if visible := guard.text(event["data"]):
                    if visible := handoff_guard.text(visible):
                        spoke = True
                        yield ev.text(visible)

            # `current_tool_use` repeats while the model streams the tool's arguments, so
            # emit `tool_start` only the first time an id appears.
            if use := event.get("current_tool_use"):
                use_id = use.get("toolUseId")
                raw_name = use.get("name") or ""
                if use_id and raw_name and use_id not in open_tools:
                    name = ev.strip_target_prefix(raw_name)
                    open_tools[use_id] = name
                    trajectory.record_tool(name)
                    if name == "confirm_booking":
                        confirm_attempts += 1
                    yield ev.tool_start(name, use_id)

            # Completion comes from the **message** event, not `ToolResultEvent`: that one
            # sets `is_callback_event = False`, so it never reaches `stream_async` at all.
            # Watching for it produced a pill that started and never cleared.
            for block in _tool_result_blocks(event):
                use_id = block.get("toolUseId")
                if name := open_tools.pop(use_id, None):
                    ok = block.get("status") != "error"
                    payloads = list(ev.payloads_in(block))
                    # **The transport status is not the signal here.** A tool whose dependency is
                    # down returns a refusal, and a refusal is shape-identical to an answer by
                    # design, so `status` stays `success` and only the marker the shared handler
                    # stamps into `provenance` tells the two apart.
                    unavailable = any(ev.upstream_unavailable(payload) for payload in payloads)
                    if unavailable:
                        tool_failures[name] = tool_failures.get(name, 0) + 1
                    else:
                        tool_failures.pop(name, None)
                    for payload in payloads:
                        guard.record_result(name, payload, ok=ok)
                    # A tool that refuses still returns cleanly, so `ok` reflects transport
                    # success — the refusal text is the model's to relay.
                    yield ev.tool_end(name, use_id, ok=ok)
                    # The model resumes with a fresh sentence and no leading space. Told to the
                    # guard rather than handled downstream: short pre-tool narration never reaches
                    # the client as its own chunk, so this boundary is invisible from there.
                    guard.tool_boundary()
                # **Cards must be forwarded here or they reach nothing.** The tool response
                # terminates at the model, so a card the frontend never receives is a tile that
                # cannot be drawn — the UI would render prose where an option list belongs.
                # Only the `cards` array travels; the rest of the envelope stays the model's.
                if built := ev.cards_in(block):
                    yield ev.cards(built)
                    handoff_guard.note_card(built)
                    if any(c.get("card_type") == "booking_confirmed" for c in built):
                        confirmed = True
                    # **A handoff the model asked for is a handoff too.** Only the budget path set
                    # this before, so a traveler saying "get me a person" was recorded as an
                    # ordinary completed turn, and no cost metric could tell a clean handoff from an
                    # answer. Found by the eval suite's first real run, not by reading this file.
                    #
                    # `handoff_prepared`, not `escalated`: the tool assembled a package and logged
                    # it, and nothing delivered it. See `ledger.py` for why that distinction is now
                    # in the vocabulary rather than in a comment.
                    #
                    # Keyed on the card rather than on the tool call, because the escalation tool
                    # refuses a tenant with no support queue by returning a message and *no* card.
                    # That refusal is not a handoff, and counting it as one would report a handoff
                    # to a queue that does not exist.
                    if any(c.get("card_type") == "escalation" for c in built):
                        trajectory.outcome = "handoff_prepared"
                        trajectory.handoff_trigger = "model_request"

            # **Checked after the tool results of this event, and before the next model round.** A
            # tool the traveler's question depends on has now failed twice running, so the next
            # round would produce the offer this exists to prevent: "would you like me to try again,
            # or is there something else I can help with?" That is not a handoff, and a traveler who
            # asked an answerable question is left holding it. Cancelled the same way as a budget
            # breach, for the same reason.
            if breach is None and failure is None:
                for name, count in tool_failures.items():
                    if failure := caps.tool_failure_breach(tool=name, failures=count):
                        trajectory.outcome = "handoff_prepared"
                        trajectory.handoff_trigger = "tool_failure"
                        log.error("tool failures, stopping the turn: %s", failure)
                        agent.cancel()
                        break

            metadata = (event.get("event") or {}).get("metadata") or {}

            # The guardrail's own verdict, on the same metadata event as usage. Recorded
            # because a blocked turn otherwise looks exactly like the model choosing to
            # decline — and a control whose firing leaves no trace cannot be shown to work,
            # nor shown to stop working.
            if guardrail := (metadata.get("trace") or {}).get("guardrail"):
                before = list(trajectory.guardrail_blocked)
                trajectory.record_guardrail(guardrail)
                if fired := [c for c in trajectory.guardrail_blocked if c not in before]:
                    log.warning("guardrail intervened: %s", ", ".join(fired))
                    yield ev.guardrail(fired)

            # Cache counters come straight from the SDK, so the hit rate is observed
            # rather than inferred.
            if usage := metadata.get("usage"):
                trajectory.record_usage(usage)

                # **Checked here, between steps, because anywhere else is too late.** A check after
                # the loop would report a runaway that had already finished paying for itself. This
                # is the first moment the new totals exist, and breaking out of `stream_async` is
                # what actually stops the next model call from being made.
                if breach is None and (
                    breach := caps.breach(
                        steps=trajectory.steps_taken,
                        usd=trajectory.cost().get("usd"),
                        failed_writes=0 if confirmed else confirm_attempts,
                    )
                ):
                    trajectory.outcome = "handoff_prepared"
                    trajectory.handoff_trigger = "budget_breach"
                    log.error("budget breach, stopping the turn: %s", breach)
                    # **Cancelled rather than broken out of, and the difference is a whole trace.**
                    # `cancel()` stops the agent at its next checkpoint, which is inside model
                    # streaming, so no further model call is made — the same saving a `break` gets.
                    # What a `break` also does is abandon the generator, and Strands ends the
                    # `invoke_agent` span only on its success path or in an `except Exception`.
                    # `GeneratorExit` is a `BaseException` and there is no `finally`, so an abandoned
                    # stream leaves the root span unexited and unexported: the breach turn was the
                    # only one of 58 in a judged run with no root span, unscoreable by the judge and
                    # rootless in traces. Cancelling returns `stop_reason="cancelled"` through the
                    # success path instead, so the span closes. The loop keeps draining events until
                    # the generator finishes, which is what lets that happen.
                    agent.cancel()
                    continue

                trajectory.start_step()

        # **Flushed here, before any handoff, because order is the whole problem.** The guards hold a
        # sentence back while it might be an unverified claim, so this is where the model's last
        # completed words are released. Left until after the escalation they landed *behind* the
        # runtime's handoff message and read as the assistant carrying on past its own stop; dropped
        # entirely, the turn lost the one explanation the traveler needed, and G2's `answer_content`
        # row went red for never saying the hold had expired. Released first, then the handoff has
        # the last word.
        if tail := guard.flush():
            if guard.rewrote:
                log.error(
                    "replaced a fabricated %s completion claim; no successful matching tool result",
                    guard.rewritten_kind,
                )
            # Chained through the second guard rather than yielded directly: a booking claim that
            # survived the first guard could still be, in the same sentence, an unverified handoff
            # claim — the two defenses compose the same way their sources do.
            if tail := handoff_guard.text(tail):
                spoke = True
                yield ev.text(tail)
        if tail := handoff_guard.flush():
            spoke = True
            yield ev.text(tail)

        # **The handoff, fired by code rather than asked of the model.** The model is what just
        # overran, so asking it to escalate would be asking it to notice its own loop — and it has
        # already demonstrated it did not. Calling the real tool rather than writing a message keeps
        # one escalation path: the same tenant queue lookup, the same refusal when no queue exists,
        # the same card the traveler sees when they ask for a person themselves.
        # **Traced under the agent's own span, or it is invisible to anything reading traces.** Both
        # of these run after `stream_async` has finished, so they are outside the agent's span tree
        # by default: a session-level judge asked *when* the agent escalated reads the trace and
        # concludes it never did, because from the trace's point of view it did not.
        if trajectory.handoff_trigger == "budget_breach":
            async for event in _traced_escalation(
                agent,
                trajectory,
                breach,
                escalate=handoff.escalate_over_budget(client, trajectory, breach),
                separate=spoke,
            ):
                yield event
        elif trajectory.handoff_trigger == "tool_failure":
            async for event in _traced_escalation(
                agent,
                trajectory,
                failure,
                escalate=handoff.escalate_after_tool_failures(client, trajectory, failure),
                separate=spoke,
            ):
                yield event

        summary = trajectory.emit(log)
        # From the line just written, so the graph and the log cannot disagree.
        metrics.publish_trajectory(log, summary)

        # **Before `done`, because the client settles the turn on it.** Text arriving after `done` is
        # text the transcript has already finished rendering.
        yield ev.done(
            {
                "input": summary["input_tokens"],
                "output": summary["output_tokens"],
                "cache_read": summary["cache_read_tokens"],
                "cache_write": summary["cache_write_tokens"],
            },
            steps=summary["steps"],
            outcome=summary["outcome"],
            reflection_steps=summary["reflection_steps"],
            handoff_trigger=summary["handoff_trigger"],
        )


if __name__ == "__main__":
    app.run()
