"""The card contract, and the handoff vocabulary that crosses three languages.

Run by `test.sh`. Needs no AWS account.

**Why the handoff half exists.** "Has this reached a human?" had three answers in three places:
`tools/escalation/handler.py` emitted `prepared`, the agent's narration guard licensed
`{queued, connected, transferred, delivered}`, and the frontend licensed `{delivered, queued}`. Two
of the guard's four statuses were values no tool could produce and the card contract never defined,
so the guard looked stricter than it was while the frontend was looser. `shared/cards.py` now owns
the vocabulary; the frontend receives it generated, and the agent restates it because it is a
separate deployable with its own venv, the same boundary the model id crosses. This asserts all
three agree, because a status added at a future delivery site must not be honest in only one.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.cards import (  # noqa: E402
    ALLOWED_ACTIONS,
    DELIVERS_TO_HUMAN,
    REQUIRED_DATA,
    CardType,
    HandoffStatus,
)


def main() -> int:
    missing = [t for t in CardType if t not in REQUIRED_DATA or t not in ALLOWED_ACTIONS]
    if missing:
        print(f"card types with no contract: {missing}", file=sys.stderr)
        return 1

    # A prepared package has reached nobody. If this ever holds, the guard and the UI will both
    # start telling travelers they are connected when they are not.
    if HandoffStatus.PREPARED in DELIVERS_TO_HUMAN:
        print("'prepared' must not license a claim that a human has the handoff", file=sys.stderr)
        return 1

    expected = {str(s) for s in DELIVERS_TO_HUMAN}

    guard_src = (ROOT / "agent/MultiTenantTravel/app/MultiTenantTravel/unclaimed.py").read_text()
    found = re.search(r"_TRANSFERRED_STATUSES = frozenset\(\{([^}]*)\}\)", guard_src)
    if not found:
        print("could not find _TRANSFERRED_STATUSES in the agent's guard", file=sys.stderr)
        return 1
    guard_set = {s.strip().strip("\"'") for s in found.group(1).split(",") if s.strip()}
    if guard_set != expected:
        print(
            f"the agent guard licenses {sorted(guard_set)} but shared/cards.py says "
            f"{sorted(expected)}. A status meaning 'a human has this' must mean that in both.",
            file=sys.stderr,
        )
        return 1

    ts_src = (ROOT / "shared/generated/cards.ts").read_text()
    if "DELIVERS_TO_HUMAN" not in ts_src:
        print(
            "shared/generated/cards.ts is stale: run scripts/generate_card_types.py",
            file=sys.stderr,
        )
        return 1
    ts_set = set(re.findall(r'"([a-z_]+)"', ts_src.split("DELIVERS_TO_HUMAN")[1]))
    if ts_set != expected:
        print(
            f"the generated contract licenses {sorted(ts_set)}, expected {sorted(expected)}. "
            "Run scripts/generate_card_types.py.",
            file=sys.stderr,
        )
        return 1

    # The escalation card must require its manifest, or `handoff.requires` in the eval fixtures has
    # nothing to check against and the suite silently stops verifying the package.
    if "context_included" not in REQUIRED_DATA[CardType.ESCALATION]:
        print("the escalation card must require context_included", file=sys.stderr)
        return 1

    print(f"{len(list(CardType))} card types, all with required-data and allowed-action entries")
    print(f"handoff vocabulary agrees in 3 places: {sorted(str(s) for s in HandoffStatus)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
