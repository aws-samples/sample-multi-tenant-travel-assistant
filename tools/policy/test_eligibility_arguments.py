"""What `check_policy_eligibility` forwards, and what it refuses. No AWS, no network.

**Why this exists.** The tool used to refuse an air check that carried no flight duration, so a
model asking about a tenant whose cabin rule counts trips had to invent a number that played no
part in the answer. Only a `DURATION` rule reads the duration, and the tool cannot know the
tenant's rule type without asking the backend — so the decision belongs to the backend, which
already loads the policy in order to answer at all.

The failure mode is silent in the worst way: with a tool-side refusal in place the request never
leaves the tool, so no backend test can catch it. These assert on the request that crosses the
boundary rather than on the verdict.
"""

from __future__ import annotations

import pytest
from tools.common import BackendError, ToolError
from tools.common.testing import GLOBEX
from tools.policy import handler as policy_handler

VERDICT = {
    "eligible": False,
    "reason_code": "cabin_entitlement_not_yet_earned",
    "request_label": "business cabin",
    "rule_quote": "Business on every 4th international trip.",
    "computation": "1 of every 4 international trips",
}

DURATION_REFUSAL = (
    "this tenant's cabin rule is duration-based, so air eligibility needs "
    "either flight_hours or a trip_id"
)


@pytest.fixture(autouse=True)
def backend_url_is_set(monkeypatch):
    """`backend_url()` refuses to default, so the offline tests supply one.

    Deliberately never reached: every test here replaces `post`. The value exists only so the
    handler gets past the env check that stops a deployed tool pointing at the wrong backend.
    """
    monkeypatch.setenv("BACKEND_API_URL", "https://backend.invalid")


@pytest.fixture
def sent(monkeypatch):
    """Capture the body posted to the backend, answering with a fixed verdict."""
    captured: dict[str, object] = {}

    def fake_post(base_url, path, context, *, body=None, **kwargs):
        captured["path"] = path
        captured["body"] = body
        return VERDICT

    monkeypatch.setattr(policy_handler, "post", fake_post)
    return captured


@pytest.fixture
def raises_backend(monkeypatch):
    """Make the backend call fail with a given status and detail."""

    def install(status: int, detail: str | None):
        def fake_post(*args, **kwargs):
            raise BackendError(f"backend returned {status}", status=status, detail=detail)

        monkeypatch.setattr(policy_handler, "post", fake_post)

    return install


def _check(arguments):
    return policy_handler.check_policy_eligibility(arguments, GLOBEX)


class TestTheDurationIsNotRequiredByTheTool:
    def test_an_air_check_without_a_duration_reaches_the_backend(self, sent):
        """The point of the fix: the question is forwarded rather than refused here."""
        _check({"check": "air", "cabin": "business"})
        assert sent["path"] == "/v1/eligibility"
        assert sent["body"] == {"check": "air", "cabin": "business"}

    def test_a_supplied_duration_is_still_forwarded(self, sent):
        _check({"check": "air", "cabin": "business", "flight_hours": 13.0})
        assert sent["body"]["flight_hours"] == 13.0

    def test_a_trip_id_is_preferred_over_a_duration(self, sent):
        """`trip_id` lets the backend derive the duration from the trip's longest segment."""
        _check({"check": "air", "cabin": "business", "trip_id": "trp_1", "flight_hours": 13.0})
        assert sent["body"]["trip_id"] == "trp_1"
        assert "flight_hours" not in sent["body"]

    def test_a_missing_cabin_is_still_refused_without_a_backend_call(self, sent):
        """The tool still refuses what it alone can decide: no rule makes the cabin optional."""
        with pytest.raises(ToolError, match="cabin"):
            _check({"check": "air"})
        assert sent == {}


class TestTheEndpointsCorrectableRefusalReachesTheModel:
    def test_a_400_detail_becomes_a_usable_refusal(self, raises_backend):
        """A duration-based tenant gets the specific corrective message, not a generic fault."""
        raises_backend(400, DURATION_REFUSAL)
        with pytest.raises(ToolError, match="duration-based") as caught:
            _check({"check": "air", "cabin": "business"})
        assert not isinstance(caught.value, BackendError)

    def test_a_500_is_not_relayed(self, raises_backend):
        """Only this endpoint's 400 is repeated.

        A fault stays a fault, so the dispatcher's generic refusal applies rather than a backend
        sentence that may name internals.
        """
        raises_backend(500, "table multi-tenant-travel-policies not found")
        with pytest.raises(BackendError):
            _check({"check": "air", "cabin": "business"})

    def test_a_400_without_a_detail_is_not_dressed_up(self, raises_backend):
        """Nothing to relay means nothing is invented."""
        raises_backend(400, None)
        with pytest.raises(BackendError):
            _check({"check": "air", "cabin": "business"})
