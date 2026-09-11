#!/usr/bin/env bash
#
# The eval gate: quality and cost in one decision.
#
#   ./run-evals.sh --dry-run              what would run, and roughly what it would cost
#   ./run-evals.sh                        the seven code-based evaluators
#   ./run-evals.sh --suite B --sample 3   three tasks of one suite, while iterating
#   ./run-evals.sh --judge                the same run, plus advisory LLM-judge scores
#
# **This spends real money and needs a deployment.** Every task runs a live turn: roughly $0.0086
# for a warm policy answer and $0.04 for a search-and-booking chain, both measured. 58 turns is
# about $0.96 at the latest observed mix. `--judge` adds an LLM-as-judge pass over the spans each turn
# already emitted, which costs judge tokens on top and **cannot change the exit code** — it prints
# after the verdict and holds no gate row, because a judge can score one trace two ways.
#
# The ceiling matters more than the per-run cost. A published incident describes a $3,900 overnight
# bill from a suite with no ceiling: a dependency bot opened 41 pull requests and the merge queue
# re-ran everything on every push and rebase — around 270 unattended runs at a per-run cost nobody
# would have queried. The runner prices each trajectory as it finishes and stops at
# `limits.max_usd_per_run` in `evaluation/gate.yaml`, and a stopped run gives no verdict at all,
# because an aborted run has proven nothing about quality.
#
# Exit code is the merge decision: 0 to ship, 1 for a breach.
set -euo pipefail
# Exported before the `cd` so a relative `--json` path remains relative to the caller's directory,
# not to `evaluation/`.
export RUN_EVALS_PWD="$PWD"

cd "$(dirname "$0")/evaluation"
# `-u` so progress is visible when the output is redirected to a file. Without it Python
# buffers stdout and a 13-minute run looks like a hung one.
exec uv run python -u -m runner.run "$@"
