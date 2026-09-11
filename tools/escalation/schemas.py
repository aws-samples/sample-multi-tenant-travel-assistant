"""Tool schema for the escalation family — the single source of truth.
One free-text input, and it is the **only** free-text field in the whole tool set that reaches a
*human*. That makes it the one place a gateway-side content guardrail would earn its keep — see the
handler for why that is currently an accepted gap rather than an oversight.
"""

from typing import Any

ESCALATE_TO_HUMAN = "escalate_to_human"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": ESCALATE_TO_HUMAN,
        # **Not "Connecting you to a human agent".** This label is shown to the traveler as the
        # tool runs, and it promised a connection the sample does not make: the handler assembles a
        # context package and logs it, and delivery is a marked extension point. A label is a claim
        # like any other.
        "label": "Preparing a handoff for your travel desk",
        "description": (
            "Hand the conversation to a human agent, with everything discussed so far "
            "attached. Use it when the traveler asks for a person, when a booking needs an "
            "exception you cannot grant, or when you have tried and cannot resolve what they need. "
            "Do not use it as a way to avoid answering a question you can answer. This prepares "
            "a handoff and does NOT connect anyone: tell the traveler their travel desk will "
            "receive the conversation, never that someone is joining now or that they are being "
            "transferred."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Why a human is needed, in one sentence and in your own words — the "
                        "human agent reads this first. Describe the problem, not the traveler: "
                        "'needs a business-class exception for a medical reason' rather than "
                        "'difficult request'."
                    ),
                },
                "trip_id": {
                    "type": "string",
                    "description": (
                        "The trip this is about, if there is one — from the trips tool. It "
                        "lets the "
                        "human agent open the right record before they say hello."
                    ),
                },
            },
            "required": ["reason"],
        },
    }
]
