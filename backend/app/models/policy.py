"""Travel policy — a hybrid model, deliberately not a rigid schema.

Corporate travel policy is heterogeneous: caps by city tier, cabin by duration
*or* seniority *or* route, advance-purchase windows, approval thresholds,
per diems, supplier preferences, exception prose. Every customer's differs —
which is why real systems store policy as documents in a CMS rather than a
fixed table. A rigid schema here would be unable to represent a real customer.

So the rule is: **type the facts code decides on, type the envelope it routes
on, and leave a customer's specific rule meaning as prose.**

- `PolicyCore` — the few fields `check_policy_eligibility` does arithmetic on.
  Typed because the eval gate demands exact-match verdicts.
- `PolicyRule` — a typed envelope around untyped meaning. `code` and
  `applies_to` are typed and `rules_for()` filters on `applies_to`; only
  `description` is prose, and only `description` is narrated rather than used.
- The knowledge base carries the third tier: exceptions, approval process, the
  "spirit of the policy" that no schema captures.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from .common import CabinClass, Money, PolicyStatus, TenantId


class CabinRuleType(StrEnum):
    """How a tenant decides when a better cabin is allowed.

    The enum turns variation along a supported axis into data rather than code:
    a customer on duration who wants 8 hours instead of 12 is a field change.

    **It does not make every cabin policy a data change**, and the limit is
    worth stating because the four members read like a complete list and are
    not one. A rule keyed on seniority, or on route, is a real corporate policy
    and neither has an axis here. Supporting one means a new member and the code
    path behind it. What the enum buys is that the *common* axes stop being
    per-tenant branches, not that the schema anticipates every customer.
    """

    NEVER = "never"  # economy always
    DURATION = "duration"  # above N hours of flight time
    TRIP_COUNT = "trip_count"  # every Nth international trip in a period
    ALWAYS = "always"


class EntitlementPeriod(StrEnum):
    """The window a trip-count entitlement resets over.

    A real difference between programs, so it is data rather than a hardcoded
    assumption: "every 4th trip this year" and "every 4th in a rolling 12 months"
    give different verdicts in December.
    """

    CALENDAR_YEAR = "calendar_year"
    ROLLING_12_MONTHS = "rolling_12_months"


class CabinRule(BaseModel):
    """Typed because eligibility compares against it in code."""

    type: CabinRuleType
    cabin: CabinClass = CabinClass.ECONOMY

    # DURATION
    threshold_hours: int | None = None

    # TRIP_COUNT
    every_nth_trip: int | None = None
    period: EntitlementPeriod = EntitlementPeriod.CALENDAR_YEAR
    count_upcoming: bool = True
    """Whether a booked-but-not-taken trip consumes the entitlement.

    True is the stricter, more realistic reading: booking your fourth while a
    fifth is already reserved should not grant the benefit twice.
    """

    def permitted_cabin(
        self,
        flight_hours: float | None = None,
        prior_international_trips: int | None = None,
    ) -> CabinClass:
        """The best cabin allowed, given flight length and travel history.

        Deterministic on purpose: the model narrates the verdict, it never
        derives it. `prior_international_trips` is only consulted by TRIP_COUNT
        rules, and a missing count there is treated as *not* yet entitled —
        absent evidence must not grant a benefit.

        `flight_hours` is only consulted by DURATION rules, and is optional for
        the same reason it is optional at the endpoint: a rule that never reads
        the duration must not require the caller to invent one. A missing
        duration on a DURATION rule follows the same fail-closed treatment as a
        missing trip count.
        """
        match self.type:
            case CabinRuleType.ALWAYS:
                return self.cabin
            case CabinRuleType.DURATION:
                if flight_hours is None:
                    return CabinClass.ECONOMY
                threshold = self.threshold_hours or 0
                return self.cabin if flight_hours > threshold else CabinClass.ECONOMY
            case CabinRuleType.TRIP_COUNT:
                nth = self.every_nth_trip or 0
                if nth <= 0 or prior_international_trips is None:
                    return CabinClass.ECONOMY
                # The trip being considered is the (prior + 1)th.
                this_trip = prior_international_trips + 1
                return self.cabin if this_trip % nth == 0 else CabinClass.ECONOMY
            case _:
                return CabinClass.ECONOMY

    def trips_until_entitled(self, prior_international_trips: int) -> int | None:
        """How many further trips before the benefit applies.

        Lets the agent say "two more international trips" instead of a bare no.
        `None` when the rule isn't count-based.
        """
        if self.type is not CabinRuleType.TRIP_COUNT or not self.every_nth_trip:
            return None
        nth = self.every_nth_trip
        return (nth - (prior_international_trips + 1) % nth) % nth


class PolicyRule(BaseModel):
    """A rule whose *meaning* is prose, wrapped in a typed envelope.

    `code` is stable so evals and citations can refer to a rule; `description`
    is what the model reads aloud, and nothing computes on it.

    **The envelope is typed on purpose, and `applies_to` is computed on** —
    `rules_for()` filters by it, so a hotel question does not drag in the car
    rules. So the split here is finer than "typed" versus "untyped": the fields
    the platform routes on are typed, and the field carrying a customer's
    specific intent is left as text. Add a rule shape nobody anticipated and
    `description` absorbs it; add a new category to route on and `applies_to`
    is where that lands.
    """

    code: str
    applies_to: str  # air | hotel | car | expense | general
    description: str


class PolicyCore(BaseModel):
    """The computed-on subset. Everything here has a code path behind it."""

    hotel_nightly_cap: Money | None = None
    max_hotel_star_rating: int | None = Field(default=None, ge=1, le=5)
    cabin_rule: CabinRule = Field(default_factory=lambda: CabinRule(type=CabinRuleType.NEVER))
    advance_purchase_days: int | None = None
    refundable_allowed: bool = True


class TravelPolicy(BaseModel):
    """A tenant's policy for one topic (air, hotel, general)."""

    tenant_id: TenantId
    topic: str
    version: str
    core: PolicyCore = Field(default_factory=PolicyCore)
    rules: list[PolicyRule] = Field(default_factory=list)
    # Free-text sections mirror what the KB holds in full; kept short here.
    sections: dict[str, str] = Field(default_factory=dict)

    def rules_for(self, applies_to: str) -> list[PolicyRule]:
        return [r for r in self.rules if r.applies_to in (applies_to, "general")]

    def hotel_status(self, nightly_rate: Money, star_rating: int | None) -> PolicyStatus:
        """Annotate a hotel option. The backend decides; the agent passes through.

        Mirrors how a real OBT returns policy-annotated search results rather
        than expecting the consumer to apply the rules.
        """
        cap = self.core.hotel_nightly_cap
        if cap is not None and nightly_rate.currency != cap.currency:
            # Cross-currency comparison needs FX we deliberately don't fake.
            return PolicyStatus.REQUIRES_APPROVAL
        if cap is not None and nightly_rate.amount > cap.amount:
            return PolicyStatus.OUT_OF_POLICY
        if (
            self.core.max_hotel_star_rating is not None
            and star_rating is not None
            and star_rating > self.core.max_hotel_star_rating
        ):
            return PolicyStatus.OUT_OF_POLICY
        return PolicyStatus.IN_POLICY

    def air_status(
        self,
        cabin: CabinClass,
        flight_hours: float,
        prior_international_trips: int | None = None,
    ) -> PolicyStatus:
        permitted = self.core.cabin_rule.permitted_cabin(flight_hours, prior_international_trips)
        if cabin == permitted:
            return PolicyStatus.IN_POLICY
        _ORDER = [
            CabinClass.ECONOMY,
            CabinClass.PREMIUM_ECONOMY,
            CabinClass.BUSINESS,
            CabinClass.FIRST,
        ]
        return (
            PolicyStatus.IN_POLICY
            if _ORDER.index(cabin) < _ORDER.index(permitted)
            else PolicyStatus.OUT_OF_POLICY
        )
