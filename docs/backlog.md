# Operator backlog

Blockers and deferred decisions found during the build. Each entry is something the build
worked around rather than stopped for. Nothing here blocks a later milestone; all of it is
worth an operator's eye.

| # | milestone | item | what the build did | what it needs |
|---|---|---|---|---|
| 1 | M0 | ~~macOS 26 runner label unverified~~ **closed**: `macos-26` exists and both Python jobs passed on it in the first CI run. | — | Nothing. |
| 2 | M1 | ~~A hypothesis cannot reach `classified` without `classify`~~ **closed**: `kanso classify` landed in M2 and moves the status itself. | — | Nothing. The override path — edit the three keys, `hyp add` — remains and needs no model. |
| 3 | M1 | The demo hypothesis named its universe by the short instruments.yaml key (`DEMO`) while the catalog files datasets under the qualified id (`DEMO.SIM`), so no snapshot covered the demo's universe. | The demo now names the qualified id in both places, and `SPEC.md` §6.1's example was amended to match. Snapshot coverage, the runner's instrument lookup and the strategy's subscriptions all key on the qualified id. | An operator's own `hypothesis.yaml` must name universe ids as `<SYMBOL>.<VENUE>`. |
| 4 | M1 | The full suite now takes about four minutes, dominated by real backtests over a year of one-minute synthetic bars. | Left as is: the runs are what make the determinism and embargo tests meaningful. | Watch it. If it reaches the point of discouraging a full run before a commit, split the slow backtests behind a marker that CI still runs. |
| 5 | M1 | `parity_replay` and `param_plateau` ship as declared catalogue items whose implementations are stubs, because both need machinery that arrives later (replay sessions in M4, and perturbed re-runs that want the certification runner of M3). | Declared them in the toolbox so a plan can name them, and left the implementations to their milestones. | Nothing now. M3 and M4 fill them; the plan validator already rejects a plan that names a gate with no implementation. |
| 6 | M2 | Certification is M3's, so a stalled run cannot be certified where the protocol says it is. | `on_stall` records the intent — status `candidate`, event `certifiable` with the subject sha — and requeues at −1. The single call site the certification run belongs at is marked with a comment in `kanso/research/scheduler.py`. | Nothing now. M3 fills that call site. |
| 7 | M2 | The daemon's monitor loop has nothing to check: there is no deployed version until M4. | The loop is started and keeps its `[monitor] interval` cadence with an empty body, so the process the stage gates will need is already in place and already supervised. | Nothing now. M4 fills the pass. |
| 4 | M2 | `src/kanso/templates/models.yaml` and the demo register both point operators at `docs/workspace.md`, which does not exist yet. | Left the pointer; the docs set is written at M8. | Nothing now. M8 either writes that page or the pointer changes. |
| 5 | M2 | The stall path records the intent to certify and requeues, rather than certifying: the certification runner is M3. One call site is marked for it. | Requeued at the lower priority, so nothing is lost. | Nothing. M3 fills the call site. |
| 6 | M2 | The daemon starts a monitor loop that has nothing to check: the paper and live gates are M4. | Loop runs and idles. | Nothing. M4 fills the body. |

