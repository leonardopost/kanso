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
- Lags and horizons come from the mechanism: the route's latency as the operator states it for that venue, the follower's print rate, the hold the thesis claims. Every cell widens the family max-T corrects over: twenty considered cells beat two hundred speculative ones.
- A response's latency is its measure's `latency_ms`; a `latency_ms` under `costs` is ignored by the screen, and `screen draft` carries the measure's into the hypothesis.
- Every parameter has a range its measure declares (`docs/workspace.md`, `screens/<id>/`); `validate` refuses one outside it.
- `costs` (free screens, per venue) is what the hurdle is struck under; a follower read as bars or prints needs a spread stated (`spread: fixed_bps`, `fixed_bps`).

## Declare the verdict before you run
- Write `verdict: {alpha, min_margin_bp, min_events_per_day, min_sessions}` before `kanso screen run`, from the campaign's own target. `ceiling_bp_day` is `margin_bp` × `events_per_day`, so choose the two floors whose product is the ceiling the target needs. With no stated target, choose them, and say in your report that you did and why.
- Sessions are the unit: S sessions can give no p below 2^(1−S), and a verdict asking for less is refused. Screen at least seven sessions for an alpha of 0.05, more for a stricter one.
- Never edit thresholds after reading numbers. New bytes are a new result; the old one stays, and `kanso screen show <id>` lists both.

## Get the data; missing data is never a reason to stop
- `kanso screen validate <path>` prints each series `held`, `fetchable` (with the adapter, its loader and whether it is configured), `unresolved` or `unserved`, and the venue model each hurdle is struck under. It fetches nothing.
- `kanso screen run <path>` fetches every fetchable series itself, resolves undefined instruments, and freezes a snapshot when what it holds has not been frozen.
- `unresolved` means no definition of the instrument resolves yet, which says nothing about whether an adapter serves it: resolve it first, then validate again; `validate` prints the command. An equity resolves through `[data] reference` under its symbol (`kanso data instruments resolve MSTR` for `MSTR.XNAS`, which also tells you its venue). An id qualified with a venue an adapter declares (`ETH-USDT-SWAP.OKX`) resolves through that adapter whatever the reference is. Otherwise give it a manual entry in `instruments.yaml`. Never build an adapter for an unresolved instrument.
- `unserved` means it resolves and no adapter serves that venue or type: build one. Write it as a workspace extension in `kanso_ext/` against the data-adapter protocol (`docs/extensions.md`), declaring `timestamps`, `serves` and `spec_for`; drive it against the live source and record the measured responses as its fixtures; run the screen again; move it upstream with skill `kanso-upstream`.
- Choose the finest data the mechanism needs (prints or the book for sub-second leads), even when bars are already held.
- Prints and quotes are large: a liquid name's day is millions of rows and minutes to fetch, and `screen run` prints nothing until it finishes. Size names × sessions before you run. They are fetched one day per chunk, so a run that stops resumes by the day: run it again.

## Read the result
- `kanso screen show <id>` lists every cell of the newest result with why it was judged so; `--cell <key>` shows one in full; `--json` carries every field (`docs/workspace.md` names them).
- For a response, `ceiling_bp_day` against the target first: the search can approach it, never exceed it. Then `margin_bp` per event, `events_per_day`, `folds` and `folds_same_sign` (net bp a day, fold by fold) and `unfilled`. Its `mean`, `t` and `p` test something else: the follower's drift-adjusted move at its mid, whether it follows at all. A pass needs both; a significant follow with a negative margin is a real effect a taker cannot earn.
- For a lead, `mean` (the correlation) and `t`, and the lag with the largest mean; a negative lag is the reverse direction, so read a forward lead against its mirror. On a `grid`, each leg's `staleness`: a stale follower overstates a lead. Hayashi–Yoshida and response cells have none.
- `in_sample_fit: true`: the spread was fitted on the data it was judged on — discount it. `clock_bound: true`: a sub-second lead between clocks that mean different things — do not trust it.
- Every result is scoped to its grain, data types and latency. A null on bars is not a null on books: say so, and screen again on the finer data.

## Decide, and say who decides
- Recommend; the operator decides. A screen never blocks a lane the operator wants running. Use it to order the queue and to set the lane's expectations.
- For a passing response cell of a free screen: `kanso screen draft <screen> --cell <key> --as <new_id> --certify A..B`, then `kanso hyp validate`, `kanso hyp add`, `kanso classify`. Compare the baseline card with the cell's ceiling and margin; report a large gap as a finding.

## Rules
- kanso never runs git. Write no notes or summaries into the workspace: the result files and `state.db` are the record.
- Never point a free screen at a window you intend to certify on.
