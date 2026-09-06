---
name: kanso-research
description: Run, queue, inspect, or stop kanso's infinite autoresearch loop on a classified hypothesis — autonomously via the kanso driver (24/7 daemon or foreground) or interactively by following the hypothesis's program.md. Use when the operator says research, autoresearch, run experiments, start/stop the loop, check research status, or read results.tsv.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-research

## Choose the path
- **Daemon (default, 24/7):** `kanso research queue add <id> [--priority P]` then `kanso research start`. Lanes (`l1..lN`, each with its own lane directory `runs/<lane>/<hyp>/`) come from `envelope.yaml`. `kanso research status` / `kanso status` to inspect; `kanso research stop` halts the daemon (runs stay resumable).
- **Foreground driver:** `kanso research run <id> [--cards N]` — same loop, lane `op`, this terminal. Omit `--cards` for infinite.
- **Interactive (you are the proposer):** `kanso research begin <id> --tag <yyyymmdd-n>` prints the lane directory path (`runs/op/<id>/`); then follow `hypotheses/<id>/program.md` exactly, working only inside that lane directory. Each iteration: edit `strategy.py` → `kanso research card <id> --desc "<≤120 chars>"` → read the one-line result → continue. Never stop to ask whether to continue. `kanso research end <id>` ends the run (cards, snapshots and `best` remain in state; the lane directory is removed).

## Reading results
- `hypotheses/<id>/results.tsv` (rendered from state): `sha7 metric metric_se n_trials n_trades wall_s peak_mem_gb status desc` (`sha7` = the first 7 characters of the card's `strategy_sha`). Best-so-far is `best_sha`; it is unset until the first keep, and `hypotheses/<id>/strategy.py` is rewritten to it on every keep. `kanso research show <id> --sha <sha7>` prints any card's `strategy.py`; commands taking a sha accept any unique prefix.
- `keep` = beat the noise floor and passed card gates; `discard` = equal/worse or a card gate failed (including `strategy_integrity`); `crash` = an exception (the traceback tail is fed to the next proposal) or over budget.
- Stalls (`stall_k` consecutive non-keeps) end the run automatically: certification if there is a keep not yet certified, and in every case — pass, fail or nothing to certify — the hypothesis is requeued at lower priority. Only `failed` and `retired` hypotheses leave the queue; a certificate is a milestone in a hypothesis's life, not its end.

## Rules
- Only `strategy.py` changes, only inside the run's lane directory `runs/<lane>/<id>/`; kanso writes each kept version back to `hypotheses/<id>/strategy.py`. kanso never runs git.
- `hypothesis.yaml` is pinned per run; to change it, `kanso research end <id>`, edit, `kanso hyp validate`, `kanso hyp add` (re-pins; keeps `classified` if construct/objective/constraints still validate, otherwise back to `draft` → `kanso classify`; a change of `universe`, `resolution`, `data_requirements` or `construct.id` — stripping the classification included — clears `best`), then `begin` again. A baseline that fails when the run started from `best` says so and names `kanso research begin <id> --from-workspace`; plain `begin` would take the same blob again.
- Never write notes, learnings, or summaries into the workspace. Cards and the content-addressed `strategy.py` snapshots in `state.db` are the record.
- Preconditions (exit 2): status `classified|candidate|researching|certified` with no active run, a runnable construct (a construct this version cannot run exits 2 and names itself), a snapshot covering the universe and windows whose instrument checksum is the store's own (`kanso data instruments resolve`, then `kanso data snapshot`; a store that moved since the snapshot is refused by name and a new snapshot pins it), envelope detected (`kanso env detect`), every tier has a model (`kanso models check`).
