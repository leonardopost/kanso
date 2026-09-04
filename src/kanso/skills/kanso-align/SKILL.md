---
name: kanso-align
description: Check whether the current strategy.py of a kanso hypothesis still implements the stated thesis (universe, mechanism, horizon, resolution), and explain a misalignment escalation. Use when the operator asks if research drifted, when an inbox entry of kind `misaligned` appears, or before certification.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-align

## Steps
1. `kanso align check <id> --json` → `{aligned, reason}`. Deterministic checks (universe, resolution, data types) run first; the model is consulted only if they pass.
2. Aligned → say so in one line. Drifted → report `reason` and what kanso did: restored the lane directory's `strategy.py` from the last aligned keep's snapshot, re-pointed `best_sha` to it (and rewrote `hypotheses/<id>/strategy.py`), reset `best_metric`, flagged the drifted cards, wrote an inbox entry, and continued researching.
3. If the operator wants the drifted direction, it is a **new hypothesis** (skill `kanso-hypothesis`), never an edit to this one. Then `kanso inbox ack <entry-id>`.

## Rules
- The loop runs this check every `align_every` cards (`kanso.toml`).
- Do not "fix" alignment by editing `hypothesis.yaml` mid-run; the run's pin would no longer match (end the run first).
