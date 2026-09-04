# Operator backlog

Blockers and deferred decisions found during the build. Each entry is something the build
worked around rather than stopped for. Nothing here blocks a later milestone; all of it is
worth an operator's eye.

| # | milestone | item | what the build did | what it needs |
|---|---|---|---|---|
| 1 | M0 | ~~macOS 26 runner label unverified~~ **closed**: `macos-26` exists and both Python jobs passed on it in the first CI run. | — | Nothing. |
| 2 | M1 | A hypothesis cannot reach `classified` without `classify`, which is M2's. | The M1 tests and the milestone acceptance move the status through `kanso.hyp.set_status`, exactly as `classify` will, after writing the classification into `hypothesis.yaml` by hand — the operator override path of the research protocol. | Nothing: M2's `classify` closes it. Until then the interactive path is edit → `hyp validate` → `hyp add` → set the status. |
| 3 | M1 | The demo hypothesis named its universe by the short instruments.yaml key (`DEMO`) while the catalog files datasets under the qualified id (`DEMO.SIM`), so no snapshot covered the demo's universe. | The demo now names the qualified id in both places, and `SPEC.md` §6.1's example was amended to match. Snapshot coverage, the runner's instrument lookup and the strategy's subscriptions all key on the qualified id. | An operator's own `hypothesis.yaml` must name universe ids as `<SYMBOL>.<VENUE>`. |
| 2 | M1 | The full suite now takes about four minutes, dominated by real backtests over a year of one-minute synthetic bars. | Left as is: the runs are what make the determinism and embargo tests meaningful. | Watch it. If it reaches the point of discouraging a full run before a commit, split the slow backtests behind a marker that CI still runs. |
| 3 | M1 | `parity_replay` and `param_plateau` ship as declared catalogue items whose implementations are stubs, because both need machinery that arrives later (replay sessions in M4, and perturbed re-runs that want the certification runner of M3). | Declared them in the toolbox so a plan can name them, and left the implementations to their milestones. | Nothing now. M3 and M4 fill them; the plan validator already rejects a plan that names a gate with no implementation. |

