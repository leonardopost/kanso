---
name: kanso-certify
description: Plan and run kanso certification for a hypothesis's best research snapshot (`strategy_sha`) and interpret the plan and certificate. Use when the operator asks to certify, validate, prove, or stress-test a hypothesis, asks which tests will be applied and why, or asks why certification failed.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-certify

## Plan first (once per hypothesis)
1. `kanso cert plan <id> --json`. The `certify_plan` task class reads the hypothesis, the gate toolbox (each gate's `meaningful_when`, params and ranges — the toolbox has no defaults and no thresholds of its own), data availability, the construct and the trial count — never research results — and returns the cert/paper/live gates, each with parameters chosen inside its ranges and a rationale, plus the gates it considered and excluded, with reasons. kanso validates the plan against the invariants (embargoed-window evaluation, `parity_replay`, ≥1 paper gate, ≥1 live gate, params inside ranges), retries once with the errors, and pins it at `certificates/<id>/plan.yaml`. Without a configured model the step exits 2: there is no default plan by design.
2. Show the operator the plan as a two-column list: gate (with its parameters) → rationale, then the exclusions. The plan is the agent's answer to "what would convince a reasonable person about *this* thesis" — it should look different for a 30-minute and a 3-month horizon.
3. To change it: `kanso cert plan <id> --replan` re-runs the planner on the same closed inputs and produces a new `plan_version`. Never edit `plan.yaml` by hand; the planner takes no free-text input by design.

## Run
4. `kanso cert run <id> [--sha S] --json` (default = `best`; `S` is any unique prefix of a card's `strategy_sha`; plans automatically if no plan exists). Runs the plan's cert gates on the embargoed certification window and the pinned data snapshot. Writes `certificates/<id>/<sha7>-<n_trials>-p<plan_version>-e<engine version>.yaml` with the certified `strategy.py` beside it as `<sha7>.py`. Exit 2 only if this `strategy_sha` was already certified under the same plan version **and** the same engine version — so after an engine upgrade, re-certifying an unchanged commit is a plain `cert run` with no replan.
5. `kanso cert show <id>`: verdict, then per gate `pass`, `evidence`, `skipped` (nothing to evaluate, e.g. no deployed strategies for `book_correlation`).
6. Report: verdict, failing gate ids with one number each from `evidence`, and `n_trials`. Three lines maximum.
7. Pass → kanso auto-composes the strategy and deploys it to paper (skill `kanso-promote`); the plan's paper/live gates then govern promotion and demotion. Fail → status returns to `researching`; failing gates are fed back to the loop. `n_fail` consecutive fails → `failed` + inbox entry.

## Rules
- Never re-plan to make a certificate pass.
- Paper-stage gates run during paper via `kanso monitor run` (the daemon runs it too), not in `cert run`.
