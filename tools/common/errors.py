"""Tool-layer failures.

Distinct types because the *caller* handles them differently, not for taxonomy's
sake: a refusal is something the model should tell the user about, while a
configuration error is something an operator must fix. Collapsing them would mean
the model apologizing for a missing environment variable.
"""


class ToolError(Exception):
    """Base for anything a tool can fail with."""


class MissingIdentityError(ToolError):
    """No verified tenant on the request.

    Never surfaced to the model as a normal answer. It means the interceptor did not
    run or its headers were not forwarded — an infrastructure fault, and one that
    must fail loudly rather than degrade into an unscoped read.
    """


class BackendError(ToolError):
    """The backend refused or failed.

    Carries the status so the handler can distinguish "not found" (an answer the
    model can convey) from "500" (a failure it should admit to rather than paper
    over).

    `detail` is the endpoint's own `detail` string, kept separate from `message`
    because `message` is for logs and may name internals. **Nothing relays it by
    default.** A status code is too coarse to decide what is safe to repeat: a
    `400` can be a caller-correctable request on one endpoint and an internal
    contract defect on another. So the tool that knows its endpoint's error
    vocabulary opts in, per endpoint, and the rest keep the generic refusal.
    """

    def __init__(
        self, message: str, *, status: int | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class UpstreamUnavailable(ToolError):
    """A dependency this tool needs could not be reached, so there is no answer to give.

    **Separate from `ToolError` because the two need different endings.** An ordinary `ToolError` is
    a refusal that *is* the answer: the cabin was not named, the check is not one this tool decides,
    the offer id does not exist. The traveler can act on it. This one means the question was
    answerable and the system could not reach the answer, which is what a human is for.

    Refusals are shape-identical by design, so nothing outside the model can tell the two apart
    without a marker. The shared handler stamps `provenance.error` on this one, and the agent loop
    counts consecutive occurrences per tool and hands off rather than offering another retry.
    """
