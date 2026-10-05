---
name: kanso-screen
description: Measure, without tokens, whether the relationship a hypothesis rests on is in real data and clears its hurdle, before spending autoresearch on it; fetch or build whatever data that takes. Use for lead-lag across names, universes, venues or adapters (crypto against crypto equities, an ETF against its constituents, one asset on several exchanges), for pairs and spreads, when deciding whether to queue, re-queue or end research, or when the operator asks whether an idea is worth a lane.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-screen

A screen measures declared relationships between declared series over a declared lattice. It runs no strategy and calls no model. Every command takes `--json`.

## Choose the form
- **Bound** (`kanso screen new <id> --hyp <hyp>`): a registered hypothesis's research window and costs; every leg must be an instrument of its universe, at a type and grain it requires. Use it before `kanso research queue add`, and after a stall before re-queuing.
- **Free** (`kanso screen new <id>`): legs and a `window` you state, for discovery. `screen run` refuses a window that meets a registered hypothesis's certification span on a shared instrument, and `hyp validate` later refuses certifying on data a screen read. Screen where you do not mean to certify.

## Write the lattice from the mechanism, and keep it small
- `legs`: `{instrument, type: bar|trade|quote|book, resolution}` (resolution for bars only). `derived`: `basket` (weights), `spread` (`long`, `short`, `hedge: fixed` + `beta`, or `hedge: ols` + `fit: window|first_fold`), `gap` (`a`, `b`: one asset on two venues). `groups` name sets of legs.
- `lead_lag`: `from`, `to`, `estimator: grid` (needs `clock.grid`; every lag a whole number of steps) or `hy` (prints against prints, no grid), `lags` — never zero; positive means `from` leads.
- `response`: `trigger` (`move_bp` + `within`, or `z` + `lookback`), `followers` (never `on`: YAML reads it as true), `side: with|against`, `horizons`, `latency_ms`.
- Lags and horizons come from the mechanism: the route's latency (the operator's standing figure is a colocated host, about 5 ms each way), the follower's print rate, the hold the thesis claims. Every cell widens the family max-T corrects over: twenty considered cells beat two hundred speculative ones.
- `costs` (free screens, per venue) is what the hurdle is struck under; a follower read as bars or prints needs a spread stated (`spread: fixed_bps`, `fixed_bps`).

## Declare the verdict before you run
- Write `verdict: {alpha, min_margin_bp, min_events_per_day, min_sessions}` before `kanso screen run`, from the campaign's own target: what `ceiling_bp_day` would make the mechanism worth a lane.
- Sessions are the unit: S sessions can give no p below 2^(1−S), and a verdict asking for less is refused. Screen at least seven sessions for an alpha of 0.05, more for a stricter one.
- Never edit thresholds after reading numbers. New bytes are a new result; the old one stays, and `kanso screen show <id>` lists both.

## Get the data; missing data is never a reason to stop
- `kanso screen validate <path>` prints each series `held`, `fetchable` (with the adapter, its loader and whether it is configured) or `unserved`, and the venue model each hurdle is struck under. It fetches nothing.
- `kanso screen run <path>` fetches every fetchable series itself, resolves undefined instruments, and freezes a snapshot when what it holds has not been frozen.
- `unserved` means no adapter serves that venue or type: build one. Write it as a workspace extension in `kanso_ext/` against the data-adapter protocol (`docs/extensions.md`), declaring `timestamps`, `serves` and `spec_for`; drive it against the live source and record the measured responses as its fixtures; run the screen again; move it upstream with skill `kanso-upstream`.
- Choose the finest data the mechanism needs (prints or the book for sub-second leads), even when bars are already held.

## Read the result
- `kanso screen show <id>`, and `--cell <key>` for one cell in full.
- For a response, `ceiling_bp_day` against the target first: the search can approach it, never exceed it. Then `margin_bp` per event, `events_per_day`, `folds_same_sign` and `unfilled`.
- For a lead, `mean` (the correlation) and `t`, the lag with the largest mean, and each leg's `staleness`: a stale follower overstates a lead.
- `in_sample_fit: true`: the spread was fitted on the data it was judged on — discount it. `clock_bound: true`: a sub-second lead between clocks that mean different things — do not trust it.
- Every result is scoped to its grain, data types and latency. A null on bars is not a null on books: say so, and screen again on the finer data.

## Decide, and say who decides
- Recommend; the operator decides. A screen never blocks a lane the operator wants running. Use it to order the queue and to set the lane's expectations.
- For a passing response cell of a free screen: `kanso screen draft <screen> --cell <key> --as <new_id> --certify A..B`, then `kanso hyp validate`, `kanso hyp add`, `kanso classify`. Compare the baseline card with the cell's ceiling and margin; report a large gap as a finding.

## Rules
- kanso never runs git. Write no notes or summaries into the workspace: the result files and `state.db` are the record.
- Never point a free screen at a window you intend to certify on.
