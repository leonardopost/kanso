---
name: kanso-certify
description: Plan and run kanso certification for a hypothesis's best research snapshot (`strategy_sha`) and interpret the plan and certificate. Use when the operator asks to certify, validate, prove, or stress-test a hypothesis, asks which tests will be applied and why, or asks why certification failed.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-certify

## Plan first (once per hypothesis)
1. `kanso cert plan <id> --json`. The `certify_plan` task class reads the hypothesis, the gate toolbox (each gate's `meaningful_when`, params and ranges — the toolbox has no defaults and no thresholds of its own), data availability, the construct and the trial count — never research results — and returns the cert/paper/live gates, each with parameters chosen inside its ranges and a rationale, plus the gates it considered and excluded, with reasons. kanso validates the plan against the invariants (every required gate this version can run, ≥1 cert gate, ≥1 paper gate, ≥1 live gate, params inside ranges, and — once the hypothesis's `n_trials`, every card it has, reaches two — the deflation gate whose library file `deflates` its objective: `deflated_sharpe`, `deflated_contribution` or `deflated_edge`, with its threshold set at a value the planner chooses), retries once with the errors, and pins it at `certificates/<id>/plan.yaml`. A plan pinned without that gate, or with it and no threshold, is refused by `cert plan` and `cert run` (exit 3, remedy `kanso cert plan <id> --replan`) from the second card. A cert-stage gate this version has no implementation for is neither offered to the planner nor accepted from it, so it is absent from the toolbox and from the plan until its implementation ships. Without a configured model the step exits 2: there is no default plan by design.
2. Show the operator the plan as a two-column list: gate (with its parameters) → rationale, then the exclusions, then the `power` line: the edge the certification window resolves at 80% power and the probability `embargoed_window` passes at the research estimate and at zero, from the error the best card recorded (no line when it is zero). A window that resolves only an edge far above the one researched is worth saying out loud before `cert run` spends it. The plan is the agent's answer to "what would convince a reasonable person about *this* thesis" — it should look different for a 30-minute and a 3-month horizon.
3. To change it: `kanso cert plan <id> --replan` re-runs the planner on the same closed inputs and produces a new `plan_version`. Never edit `plan.yaml` by hand; the planner takes no free-text input by design.

## Run
4. `kanso cert run <id> [--sha S] --json` (default = `best`; `S` is any unique prefix of a card's `strategy_sha`; plans automatically if no plan exists). Runs the plan's cert gates on the embargoed certification window and the pinned data snapshot; under a `warmup` both windows are run warmed on the sessions before them, and the evidence gates read the certification window's own lags and volume. Writes `certificates/<id>/<sha7>-<n_trials>-p<plan_version>-e<engine version>.yaml` with the certified `strategy.py` beside it as `<sha7>.py`. Exit 2 if this `strategy_sha` was already certified under the same plan version **and** the same engine version — so after an engine upgrade, re-certifying an unchanged commit is a plain `cert run` with no replan. Exit 2 too, writing nothing, when the search behind the subject cannot be priced: two or more cards ran and traded but fewer than two were scored under the subject's objective (a hypothesis re-pinned to another objective, its carried strategy certified as the first card under it — research further, then certify), or the subject was scored under an objective whose deflation gate the pinned plan does not carry (`--sha` of a card from before such a re-pin). The deflation's `trials` counts every card that ran and traded, under any objective; `scored_trials` the ones its spread was read from.
5. `kanso cert show <id>`: verdict, then per gate `pass`, `evidence`, `skipped` (nothing to evaluate, e.g. no deployed strategies for `book_correlation`).
6. Report: verdict, failing gate ids with one number each from `evidence`, `n_trials`, and the certificate's `power.resolves`. Three lines maximum.
7. Pass → kanso auto-composes the strategy and deploys it to paper (skill `kanso-promote`); the plan's paper/live gates then govern promotion and demotion. Fail → status returns to `researching`; failing gates are fed back to the loop. `n_fail` consecutive fails → `failed` + inbox entry.

## Rules
- Never re-plan to make a certificate pass.
- Paper-stage gates run during paper via `kanso monitor run` (the daemon runs it too), not in `cert run`.
