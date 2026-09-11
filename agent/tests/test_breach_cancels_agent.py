"""The break-out cancels the agent rather than abandoning its stream, so the trace keeps its root.

**Why this is a source check rather than a behavioral one.** `main.py` imports the AgentCore runtime
and the Strands agent at module scope, so it is not importable in this suite and no test here drives
the entrypoint. The invariant still needs a guard, because the defect it protects against is silent.

**What the silence cost.** Ending the turn with a bare `break` out of `stream_async` stops the next
model call and also abandons the generator. Strands ends the `invoke_agent` span on its success path
or in an `except Exception`; `GeneratorExit` is a `BaseException` and there is no `finally`, so an
abandoned stream leaves the root span unexited and never exported. In a 58-turn judged run the
single budget-breach turn was the only one whose spans carried no `invoke_agent`, and `Evaluate`
refused it
with "Provided input has no spans to evaluate": the one turn in the run anybody would want to read.
Nothing else notices, because the handoff still fires, the ledger still emits and the gate still
passes.

`agent.cancel()` stops the agent at its next checkpoint, inside model streaming, and returns
`stop_reason="cancelled"` through the success path, which ends the span. So the loop must cancel and
then keep draining rather than break, and this asserts that shape.
"""

from __future__ import annotations

import ast
import pathlib

MAIN = (
    pathlib.Path(__file__).resolve().parents[1]
    / "MultiTenantTravel"
    / "app"
    / "MultiTenantTravel"
    / "main.py"
)

TREE = ast.parse(MAIN.read_text())


def _breach_branch() -> ast.If:
    """The branch guarded by the `breach :=` walrus, wherever it sits in the test expression."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.If) and any(
            isinstance(inner, ast.NamedExpr)
            and isinstance(inner.target, ast.Name)
            and inner.target.id == "breach"
            for inner in ast.walk(node.test)
        ):
            return node
    raise AssertionError("no `breach := ...` branch in main.py; this test needs rewriting")


def _calls(node: ast.AST, attr: str) -> bool:
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == attr
        for inner in ast.walk(node)
    )


def test_the_breach_cancels_the_agent() -> None:
    branch = _breach_branch()
    assert _calls(branch, "cancel"), (
        "the budget break-out must cancel the agent, or the turn's next model call is not stopped"
    )


def test_the_breach_does_not_abandon_the_stream() -> None:
    """A `break` here strands the generator and the root span goes with it."""
    branch = _breach_branch()
    assert not any(isinstance(node, ast.Break) for node in ast.walk(branch)), (
        "breaking out abandons the generator, so Strands never exits the invoke_agent span; "
        "cancel and keep draining instead"
    )
    assert any(isinstance(node, ast.Continue) for node in ast.walk(branch)), (
        "the loop must keep draining after cancelling, so the stream can finish and close its span"
    )


def test_the_cancellation_happens_once() -> None:
    """The cap is re-evaluated on every usage event, and cancelling on each would spam the log."""
    branch = _breach_branch()
    guards = [
        node
        for node in ast.walk(branch.test)
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Is)
    ]
    assert guards, (
        "the breach branch must be guarded on `breach is None`, or it fires on every usage event"
    )


def test_no_model_text_reaches_the_traveler_after_a_cap_fires() -> None:
    """**Cancellation stops the next round, not the tokens already in flight.**

    Observed on a live run: the traveler was sent "I'll " and then the handoff message, rendering as
    "I'll I've spent longer on this than I should". The turn's last word has to be the runtime's,
    because once the run is stopped the model's half-finished sentence is not an ending.
    """
    source = MAIN.read_text()
    assert 'if (breach or failure) and "data" in event:' in source, (
        "in-flight model text must be suppressed once a cap has fired"
    )
    tree = ast.parse(source)
    flush_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "flush"
    )
    escalate_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "escalate_over_budget"
    )
    assert flush_line < escalate_line, (
        "the guards must flush before the handoff: after it the model's last words land behind the "
        "runtime's, and dropping them loses the explanation the traveler needs"
    )


def test_the_code_fired_escalation_is_named_as_a_tool_execution() -> None:
    """A span of our own invention was invisible to the judge, which reads GenAI-shaped records."""
    source = MAIN.read_text()
    assert '"gen_ai.operation.name": "execute_tool"' in source
    assert '"gen_ai.tool.name": handoff.ESCALATION_TOOL' in source
    assert 'f"execute_tool {handoff.ESCALATION_TOOL}"' in source, (
        "the span name carries the tool, the same way Strands names the ones the model fires"
    )


def test_the_handoff_starts_a_new_paragraph_when_the_model_already_spoke() -> None:
    """**Two authors, two messages, and streamed prose has no trailing whitespace.**

    Observed live: "Let me try again.I couldn't reach the system that holds that answer". The break
    is conditional on the model having said something, so a turn stopped before it spoke does not
    open with blank lines.
    """
    source = MAIN.read_text()
    assert "separate=spoke" in source, "the handoff must know whether the traveler was already told"
    assert "f\"\\n\\n{event['text'].lstrip()}\"" in source, (
        "the first handoff message must start a new paragraph"
    )
