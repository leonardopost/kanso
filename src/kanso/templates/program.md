# program.md — research protocol for hypothesis `{{hyp_id}}`

You are an autonomous researcher. Your job is to improve `strategy.py` against the objective in `hypothesis.yaml`, one experiment at a time, forever. kanso evaluates every experiment identically whether you or the kanso driver proposed it.

## Setup (once per run)
1. Choose a run tag `<yyyymmdd>-<n>` (e.g. `{{today}}-1`).
2. `kanso research begin {{hyp_id}} --tag <tag>` — creates the lane directory `runs/op/{{hyp_id}}/` with copies of `hypothesis.yaml` and `program.md` and a `strategy.py` taken from the best previous snapshot if one exists (else from `hypotheses/{{hyp_id}}/strategy.py`), pins `hypothesis.yaml` and `program.md` (sha256), the data snapshot and the criteria version, runs the **baseline card**, and prints the lane directory path. **Work only inside that lane directory.** kanso never runs git; on every keep it writes the kept `strategy.py` back to `hypotheses/{{hyp_id}}/strategy.py`, so the workspace copy is always the best-so-far and committing it is the operator's business.
3. Read, in this order: `hypothesis.yaml` (thesis, universe, horizon, resolution, objective, gates), `strategy.py`, `hypotheses/{{hyp_id}}/results.tsv` in the workspace (rendered by kanso from state; not copied into the lane directory). `kanso research show {{hyp_id}} --sha <sha7>` prints any earlier card's `strategy.py`; `--diff <sha7>` shows the change between two. Nothing else is in scope.
4. Note the baseline row. If it is `keep`, every later keep must beat `best` by more than `max(min_delta, k_se × metric_se)` (both are params of the objective in `hypothesis.yaml`). If it is `discard` (e.g. too few trades), the first card that passes all card gates becomes the first keep.

## Scope
- You may modify **only** `strategy.py`. Anything else (`hypothesis.yaml`, `program.md`, catalog, windows, the runner) is fixed and out of reach; if the lane copies of `hypothesis.yaml` or `program.md` no longer equal the run's pins, or any other file appears in the lane directory, the card is `discard` without a backtest and the copies are restored (`strategy_integrity`).
- Allowed imports: `nautilus_trader.model`, `nautilus_trader.trading`, `nautilus_trader.indicators`, `nautilus_trader.core`, `kanso.nautilus.strategy`, `numpy`, `math`, `statistics`, `collections`, `dataclasses`, `typing`, `decimal`, `datetime`. No file, network, or process access. No new dependencies.
- Fair game inside `strategy.py`: signal logic, thresholds, filters, entry/exit rules, sizing within `risk_limits`, order types, timing within `resolution`. Stay inside the thesis: same `universe`, same `mechanism`, same `horizon`. Drift is detected every `align_every` cards and reverted.

## Loop (repeat indefinitely)
1. The lane directory's `strategy.py` is the last keep (or the baseline): kanso restored it after any discard or crash. Start from it.
2. Change `strategy.py` with **one** idea.
3. `kanso research card {{hyp_id}} --desc "<≤120 chars, what and why>"` — kanso stores the lane directory's `strategy.py` under its sha256 (`strategy_sha`) in `state.db`, runs the backtest under the run's wall-clock budget, evaluates the objective and card gates, records the card, and either makes that sha `best` and writes the file back to `hypotheses/{{hyp_id}}/strategy.py` (keep) or restores the lane directory's `strategy.py` from the `best` snapshot, else the run's base (discard/crash). Read its single output line: `keep|discard|crash · metric · Δ · reason`.
4. `crash`: the command prints the traceback tail. Fix an obvious error (typo, import) in your next change; if the idea is fundamentally broken, move on.
5. Prefer simpler code. A keep that adds more than `max_lines_per_keep` lines must clear twice the noise margin.
6. Do not write notes, summaries, or learnings anywhere. The cards are the log.
7. Go to 1.

## Never stop
Do not pause to ask whether to continue. Do not ask "should I keep going?". The operator may be asleep; the loop runs until they interrupt it (`kanso research end {{hyp_id}}`). After a stall (`stall_k` consecutive non-keeps) kanso ends the run and certifies the best snapshot (`best_sha`) if one exists and it differs from the latest certificate's. That is not a signal to stop: unless the hypothesis is `failed` or `retired`, start the next run with `kanso research begin {{hyp_id}} --tag <new tag>` and continue from Setup step 3.
