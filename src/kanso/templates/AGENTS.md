# kanso workspace — operator agent instructions

This directory is a kanso workspace. kanso is the CLI; you are the operator agent. Skills in `.claude/skills/kanso-*` (or your tool's equivalent path) are thin procedures over CLI commands; use them by name.

## Session start
1. `kanso doctor` — stop and report if not green.
2. `kanso inbox` — escalations needing the operator: `misaligned`, `cert_failed`, `promotable`, `demoted`, `deploy_blocked`.
3. `kanso status` — lanes, cards/hour, best metric per hypothesis, spend today.

## Flow
`kanso-hypothesis` → `kanso-classify` → `kanso-research` → (automatic: stall → certification → paper) → `kanso-promote` for live approval. Data and instruments: `kanso-data`. Models: `kanso-models`. Machine: `kanso-env`. Drift: `kanso-align`. Certification plans and certificates: `kanso-certify`. What-would-it-have-done and parity: `kanso-replay`. Sending a workspace extension or fix to the framework: `kanso-upstream`.

## Rules
- Research happens in plain lane directories `runs/<lane>/<hyp>/`; kanso never runs git (no commits, branches, tags or worktrees). `hypotheses/<id>/strategy.py` is always the best-so-far; committing workspace files is the operator's choice, never yours to do on their behalf unless asked.
- Research edits only `strategy.py`, and only through the loop (`kanso research card|run|start`). Do not edit `strategy.py` by hand outside a run.
- Do not stop, pause, or throttle research on your own initiative. Escalations arrive through the inbox; act on those.
- Live capital moves only on an explicit operator instruction in the conversation. Then, and only then, run `kanso promote <strategy> --live --as <the operator's name>`. Never pass `--as` otherwise. Acknowledging an inbox entry is not an approval.
- Never write notes, learnings, summaries, or reports into this workspace. State lives in `state.db` (including every card's `strategy.py`, content-addressed by `strategy_sha`), certificates and sessions.
- If the operator commits the workspace, `.gitignore` already keeps `.env`, `state.db`, `runs/` and `results.tsv` out; never force-add them.
- Prefer `--json` outputs when you need to reason over results; quote numbers from them, not from memory.
