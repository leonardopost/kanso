---
name: kanso-hypothesis
description: Turn a market thesis stated in chat (any instrument, mechanism, or horizon) into a validated kanso hypothesis.yaml and register it. Use when the operator proposes, refines, or asks to record a trading idea, thesis, or hypothesis in a kanso workspace.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-hypothesis

Deterministic procedure. The CLI validates; you only fill the fields.

## Steps
1. Pick an id: `^[a-z0-9_]{3,40}$` (short, descriptive). Run `kanso hyp new <id>` → creates `hypotheses/<id>/{hypothesis.yaml, program.md, strategy.py}` at the workspace root.
2. Fill `hypothesis.yaml` from the operator's words. Ask at most one question if a required field cannot be inferred; otherwise choose and state the assumption.
   - `thesis`: ≤2 sentences, falsifiable.
   - `mechanism`: one of `mean_reversion | momentum | microstructure | stat_arb | event | carry | vol | other`.
   - `universe`: ids that exist in `instruments.yaml` (`kanso hyp validate` checks them; add instruments first if missing — skill `kanso-data`).
   - `horizon` (holding period) and `resolution` (bar/tick spec) as durations `30m`, `1d`, or `tick|quote|trade`.
   - `data_requirements`: only what the thesis needs. `risk_limits`: keep defaults unless the operator specified.
   - `costs`: keep the venue's defaults when the thesis requires `quote` data. Without `quote` there are no quotes to take a spread from, so set `costs: {spread: fixed_bps, fixed_bps: <width>}` (or rely on `venues.<MIC>.costs.fixed_bps` in `portfolio.yaml`); otherwise step 3 exits 3 naming `costs.fixed_bps`. State the width you chose.
   - `windows`: research → embargo → certification → forward. Embargo ≥ `max(5 × horizon, 1d)`. Certification strictly after research. `forward.start` is where paper/live replay begins; no end.
   - `warmup: {sessions: N}` when the thesis needs indicators full from the first session: the last N sessions (days any universe name printed at the sleeve's grain — the host's, for an attached construct) before each window are fed to the strategy first with every order dropped, so it trades from the open. The catalog must hold them and the snapshot must cover them, or `research begin` exits 2 naming what to load. It is scope: adding or changing it clears `best`. Leave it out for a thesis that carries no state.
   - `book: {reset: monthly|none, financing_rate_bps: N, maintenance_pct: N}` when the operator names how the book behaves between fills: a monthly reset to `capital` (surplus to a cushion, deficit restored from it, never borrowed), a yearly rate charged once per return period on what the book holds above its equity (shorts included), and a floor for the `maintenance_margin` gate — the period-end holdings at the period's worst prices over their gross. `hyp validate` exits 3 on a floor above `100 / max_leverage`, on a reset or a carry over a `cash` account, and on an attached construct whose `book` is not its host's. Scope as a whole: any change clears `best`. Leave it out unless the thesis says so.
   - Leave `construct`, `objective`, `constraints` empty — `kanso classify` fills them; certification gates are planned later at runtime.
3. `kanso hyp validate hypotheses/<id>/hypothesis.yaml` → fix every reported error (exit 3) until clean.
4. `kanso hyp add hypotheses/<id>/hypothesis.yaml` → status `draft`; kanso pins the file by the sha256 of its bytes (`hypothesis_sha`) and never runs git — the files are the operator's to commit if and when they choose.
5. Tell the operator the id and run `kanso classify <id>` next (skill `kanso-classify`), unless they said otherwise.

## A hypothesis kanso wrote
An inbox entry of kind `explored` names a draft kanso wrote under `hypotheses/<id>/` from a researched hypothesis that stopped learning (`kanso hyp explore <parent>`, or a daemon lane after `[research] explore_after_stalls` stalls). It is not registered. Read the three files, tell the operator the idea and the rationale, then run steps 3–5 on it only if they want it researched; otherwise leave the directory for them to delete.

## Rules
- Never edit `strategy.py` here; that is the research loop's surface.
- Never invent instruments, dates, or data availability: `kanso data show` shows what the catalog holds and over which spans.
- One hypothesis per thesis. Variants are new hypotheses, not edits to a researched one. Editing `hypothesis.yaml` after a run began requires a new run (`kanso research end`, edit, `kanso hyp add`, then `begin`).
