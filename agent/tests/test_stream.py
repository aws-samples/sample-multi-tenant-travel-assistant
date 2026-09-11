"""Structured values extracted from Strands tool-result content blocks."""

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "MultiTenantTravel" / "app" / "MultiTenantTravel"
sys.path.insert(0, str(AGENT_DIR))

import stream  # noqa: E402


def test_payloads_in_returns_the_tool_envelope():
    result = {
        "content": [
            {
                "text": (
                    '{"cards":[{"card_type":"booking_confirmed"}],'
                    '"facts":{"booked":true},"provenance":{"source":"booking_confirm"}}'
                )
            }
        ]
    }
    assert stream.payloads_in(result) == [
        {
            "cards": [{"card_type": "booking_confirmed"}],
            "facts": {"booked": True},
            "provenance": {"source": "booking_confirm"},
        }
    ]


def test_payloads_in_ignores_unparseable_content():
    result = {"content": [{"text": "not json"}, {"json": {"facts": {"booked": True}}}, None]}
    assert stream.payloads_in(result) == []


def test_cards_in_reuses_the_same_envelope_parser():
    result = {
        "content": [
            {"text": '{"facts":{"booked":true}}'},
            {
                "text": (
                    '{"cards":[{"card_type":"booking_confirmed"},null,"bad"],'
                    '"facts":{"booked":true}}'
                )
            },
        ]
    }
    assert stream.cards_in(result) == [{"card_type": "booking_confirmed"}]


# --- the `done` event, which the eval gate reads instead of CloudWatch --------------------------
#
# **Every accounting field here is a gate row upstream.** `reflection_steps` was absent for the
# life of the suite, so `reflection_step_rate` scored a perfect `0.0` against its ceiling on every
# run and could not fail. Nothing was wrong with the ledger; the number simply never left the
# process. These tests name each field so a silent omission fails rather than reads as a green row.


def test_done_carries_every_accounting_field_the_gate_scores():
    settled = stream.done(
        {"input": 1085, "cache_read": 11802},
        steps=2,
        outcome="answered",
        reflection_steps=1,
    )
    assert settled == {
        "type": "done",
        "usage": {"input": 1085, "cache_read": 11802},
        "steps": 2,
        "outcome": "answered",
        "reflection_steps": 1,
    }


def test_a_zero_reflection_count_is_still_sent():
    """`0` is a measurement and has to travel, or the row it feeds cannot tell a clean turn from an
    unreported one. A truthiness check here would drop exactly the common case."""
    assert stream.done(steps=2, reflection_steps=0)["reflection_steps"] == 0


def test_an_omitted_field_is_absent_rather_than_zero():
    """A turn that died mid-stream reports nothing. The runner must see the gap, so the key is left
    out entirely instead of defaulting."""
    settled = stream.done()
    assert "steps" not in settled
    assert "outcome" not in settled
    assert "reflection_steps" not in settled


def test_cost_never_travels_on_the_done_event():
    """A client may be told how much work its own turn took; a dollar figure is the tenant's
    commercial data, and the runner prices these counts itself."""
    settled = stream.done({"input": 10}, steps=1, outcome="answered", reflection_steps=0)
    assert not [k for k in settled if "usd" in k or "cost" in k]


class TestTheUpstreamUnavailableMarker:
    """**The one signal that separates an outage from an answer.**

    Refusals are shape-identical to answers on purpose, so the model treats them as answers, and the
    transport status stays `success`. That leaves nothing outside the model able to tell "the tool
    decided no" from "the dependency is down" — and those need different endings, one a result and
    one a handoff. The marker rides in `provenance`.
    """

    def test_a_marked_refusal_is_recognized(self):
        assert stream.upstream_unavailable({"provenance": {"error": "upstream_unavailable"}})

    def test_an_ordinary_refusal_is_not(self):
        """ "I need to know which cabin" is an answer the traveler can act on."""
        assert not stream.upstream_unavailable(
            {"message": "I need to know which cabin to check.", "provenance": {"source": "policy"}}
        )

    def test_a_normal_answer_is_not(self):
        assert not stream.upstream_unavailable({"facts": {"hotel_nightly_cap": 250}})

    def test_a_missing_or_malformed_provenance_is_not(self):
        assert not stream.upstream_unavailable({})
        assert not stream.upstream_unavailable({"provenance": "policy"})

    def test_the_constant_matches_the_tool_side_spelling(self):
        """Both sides name it, and a rename that touches one is a marker that stops matching."""
        tools_response = (
            Path(__file__).resolve().parents[2] / "tools" / "common" / "response.py"
        ).read_text()
        assert f'UPSTREAM_UNAVAILABLE = "{stream.UPSTREAM_UNAVAILABLE}"' in tools_response
