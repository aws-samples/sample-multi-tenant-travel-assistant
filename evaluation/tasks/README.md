# Task fixtures

One YAML file per suite. Each file declares what "resolved" means for that suite, because the
denominator of cost-per-resolved-task is only meaningful if "resolved" is written down per suite
rather than assumed. The thresholds those numbers are judged against live in `../gate.yaml`.

## Shape

```yaml
suite: B
title: Eligibility verdicts
resolved_when: verdict exact-match, and the narration agrees with it
tasks:
  - id: B1
    title: A hotel exactly at the cap is inside policy
    prompt: Is a hotel at 250 dollars a night within my policy?
    personas: [priya] # runs once per persona listed
    expect:
      tools:
        required_any: [check_policy_eligibility]
        forbidden: [confirm_booking]
      verdict: # asserted by VerdictExactMatch
        eligible: true
        reason_code: hotel_in_policy
      cards:
        required_types: [policy_verdict]
      by_persona: # where the correct answer differs per tenant
        priya:
          must_mention: ["250"]
          must_not_mention: ["150", "initech"]
```

Every block maps to exactly one code-based evaluator, so a failure names the property that broke
rather than "the task failed":

| Block                  | Evaluator            |
| ---------------------- | -------------------- |
| `tools.*`              | `ToolSequence`       |
| `verdict`              | `VerdictExactMatch`  |
| `cards.required_types` | `CardSchemaValid`    |
| `must_mention`         | `AnswerContent`      |
| `must_mention_one_of`  | `AnswerContent`      |
| `must_not_mention`     | `TenantIsolation`    |
| `writes`               | `ConfirmBeforeWrite` |
| `handoff.requires`     | `EscalationPackage`  |
| `handoff.trigger`      | `EscalationTrigger`  |
| `handoff.after_tool_calls` | `EscalationTrigger` |

`must_mention` and `must_not_mention` used to share `TenantIsolation`. They are separate rows now,
because one score could not say whether a value had crossed a tenant boundary or an answer had
simply failed to state a fact, and `AnswerContent` is the row that catches a dependency failing
_gracefully_.

Four tool operators, and the difference between them is the difference between a control and a
comment:

| Operator         | Means                     | Use for                                               |
| ---------------- | ------------------------- | ----------------------------------------------------- |
| `required_any`   | at least one of these ran | several chains are legitimately correct               |
| `required_all`   | every one of these ran    | a control needing two tools rather than either of two |
| `required_order` | these ran in this order   | a sequence with meaning, such as prepare then confirm |
| `forbidden`      | none of these ran         | the only operator that constrains what _else_ ran     |

Neither requiring operator is an exact set. **A positive control needs exactly one acceptable tool
named**, whichever operator names it: A6 spent months accepting either `search_policy_knowledge` or
`get_travel_policy`, so the vector index could have been empty for the life of the suite without the
task noticing. `tests/test_fixtures.py` rejects an operator outside this table, because
`required_al` parses as valid YAML, is never read, and passes on any trajectory at all.

## Two rules that make the numbers mean anything

**Personas, not tenants.** A task lists the travelers it runs as, and the runner resolves the
tenant from the persona — so `priya` and `sam` asking the identical question is one task run twice,
which is what makes tenant contrast a property of the fixture rather than a suite of its own.

| Persona  | Tenant  | Role     | Policy in force                                         |
| -------- | ------- | -------- | ------------------------------------------------------- |
| `priya`  | globex  | traveler | hotel ≤ $250 / 4★, business every 4th internationaltrip |
| `adaeze` | globex  | arranger | as globex, and may act for other globex travelers      |
| `sam`    | initech | traveler | hotel ≤ €150 / 3★, economy only, 7-day advance          |

**`FIXTURE` mode, or the assertions are theatre.** Exact-match verdicts and cost baselines only
mean something against seeded generation, where the same query returns the same options. `LIVE`
mode seeds on a time bucket and drifts between sessions, which is right for a demo and useless for
a gate.

## Expected values are computed, not guessed

The verdicts in suite B were produced by calling `backend/app/service/policy_check.py` directly and
recording what it returned, including the exact boundaries — `$250` exactly at
the cap and departure exactly 7 days out are both **inside** policy. A fixture whose expectation was
written from reading the policy prose would encode the reader's arithmetic instead of the code's.

## Tasks whose support does not exist yet

A task may declare `blocked_on:`. The runner reports those as skipped **with the reason**, and never
as passed — a suite that silently drops the tasks it cannot run would report a clean sheet for
exactly the behavior nobody has built.
