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
   - `data_requirements`: only what the thesis needs. `costs`, `risk_limits`: keep defaults unless the operator specified.
   - `windows`: research → embargo → certification → forward. Embargo ≥ `max(5 × horizon, 1d)`. Certification strictly after research. `forward.start` is where paper/live replay begins; no end.
   - Leave `construct`, `objective`, `constraints` empty — `kanso classify` fills them; certification gates are planned later at runtime.
3. `kanso hyp validate hypotheses/<id>/hypothesis.yaml` → fix every reported error (exit 3) until clean.
4. `kanso hyp add hypotheses/<id>/hypothesis.yaml` → status `draft`; kanso pins the file by the sha256 of its bytes (`hypothesis_sha`) and never runs git — the files are the operator's to commit if and when they choose.
5. Tell the operator the id and run `kanso classify <id>` next (skill `kanso-classify`), unless they said otherwise.

## Rules
- Never edit `strategy.py` here; that is the research loop's surface.
- Never invent instruments, dates, or data availability: `kanso data show` shows what the catalog holds and over which spans.
- One hypothesis per thesis. Variants are new hypotheses, not edits to a researched one. Editing `hypothesis.yaml` after a run began requires a new run (`kanso research end`, edit, `kanso hyp add`, then `begin`).
