# The command line

Every command takes `--json` and prints exactly one object under it — the result on
success, `{"error", "code", "remedy"?}` on failure — so a caller parses one document and
branches on the exit code. Without `--json` the same result is a few terse lines. Both
`kanso --json <command>` and `kanso <command> --json` work.

`--workspace PATH` (`-w`) names the workspace a command acts on; the default is the
current directory, from which discovery walks up to the nearest `kanso.toml`.

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | an unexpected fault |
| 2 | a precondition failed: workspace or engine state forbids the action |
| 3 | validation failed: operator-authored input is malformed or wrong |
| 4 | a named operator approval is required and absent |

## Workspace

| command | what it does |
|---|---|
| `kanso init [DIR] [--demo]` | scaffold a workspace, link the skills, detect the envelope, apply the migrations. `--demo` renders the mock-only register, the synthetic loader spec, the `DEMO.SIM` instrument and `hypotheses/demo_mr/`. kanso never invokes git; a `.gitignore` is written or appended |
| `kanso doctor [--report] [--check-adapters]` | diagnose the workspace, the install, the engine, the credentials and the lanes. Exits 2 when a check fails. `--report` redacts paths for pasting upstream. Makes no network call unless `--check-adapters` |
| `kanso migrate` | apply the pending state migrations. Every other command refuses a database behind the schema rather than migrating it behind your back |
| `kanso skills sync` | link the packaged skills into every `[skills] targets` entry |
| `kanso env detect` | detect the host, derive the lane plan, write `envelope.yaml` |

## Data

| command | what it does |
|---|---|
| `kanso data load --loader ID --spec FILE [--replace]` | run a loader over the range its spec names. An overlapping write into unpinned data is refused (exit 2) and needs `--replace`; one into a dataset a snapshot pins is refused outright |
| `kanso data show` | every series the store holds: its datasets, the spans they **served**, the gaps between them and the row counts |
| `kanso data snapshot` | freeze what is held — the dataset checksums and the checksum of the resolved instruments — into `catalog/snapshots/<snapshot_id>.yaml`. A run is pinned to one of these |
| `kanso data backfill --loader ID --spec FILE [--from DATE] [--to DATE] [--dry-run]` | fill history from the source's floor (or `--from`, clamped up to the floor and reported) to the earliest day already held (or `--to`), and close the gaps inside what is held. Chunked; the manifest each chunk writes is its checkpoint, so an interrupt resumes and a repeat fetches nothing. `--dry-run` prints the chunks, the request count and the estimated bytes and fetches nothing |
| `kanso data sync [--loader ID] [--dataset D] [--to DATE]` | extend each held dataset from its served end towards `--to` (default today) into a successor dataset recording `supersedes`, so a dataset a pinned snapshot references is never mutated |
| `kanso data instruments resolve [ID…] [--as-of DATE] [--refresh]` | resolve ids (default: every id `instruments.yaml` names) into the catalog's instrument store and the cache. `--refresh` resolves again rather than answering from the cache, and is refused (exit 2) while a run is active or while a deployed version depends on a snapshot that pins one of these instruments |
| `kanso data instruments show [ID]` | one resolved definition's canonical fields, or the ids the catalog holds |
| `kanso data adapters [--check]` | what is registered here: id, kind (`data`, `reference`, `exec`), whether its credentials resolve, its capabilities and its quota. Without `--check` it performs no network I/O |

Dates are written `YYYY-MM-DD`; anything else is a validation failure (exit 3).

## Hypotheses

| command | what it does |
|---|---|
| `kanso hyp new ID` | scaffold `hypotheses/<id>/` with `hypothesis.yaml`, `program.md` and a `strategy.py` stub |
| `kanso hyp validate PATH` | say whether the file is admissible — windows, embargo, universe resolution, construct, objective and constraints — and change nothing either way |
| `kanso hyp add PATH` | register it, or re-pin an already registered one, under the sha256 of its bytes. Refused while a run is active (exit 2), because a run is pinned to the bytes it began with |
| `kanso hyp show [ID]` | one registration — status, pin, construct, objective, best — or all of them |
| `kanso hyp retire ID` | end a hypothesis. Its cards, blobs and certificates stay in state |
| `kanso classify ID` | decide what the hypothesis **is** — construct, host, the keep rule's two parameters and the card-stage constraints — in one call to the best model on the register, and write the three keys into `hypothesis.yaml`, re-pinning it. The objective is not asked for: it follows from the hypothesis and the construct. A construct this build cannot run is recorded honestly and refused at `research begin`. `strategy.py` is replaced by the construct's stub only while the file is still one kanso wrote |

Editing `construct`, `objective` and `constraints` by hand and running `kanso hyp add` is
the override path, and needs no model at all.

## Research

| command | what it does |
|---|---|
| `kanso research begin ID [--tag T] [--from-workspace]` | start a run and **print the lane directory**, which is where an agent works. Copies the three scoped files there, pins them, and runs the baseline card. `--from-workspace` starts from the workspace `strategy.py` and clears the hypothesis's best |
| `kanso research card ID --desc TEXT` | evaluate the lane directory's `strategy.py` as one card: the static integrity rules first, then the backtest on the research window in a child process under the run's time and memory budgets, then the constraints and the keep rule |
| `kanso research run ID [--cards N]` | the same loop with the model in the agent's seat: begin a run if there is none, then propose → apply → evaluate until `N` cards or until the run stalls. `--cards` counts what **this** invocation proposed, so the baseline and everything a previous invocation left behind are not in it. A proposal is a unified diff over `strategy.py`, applied in-package; one that does not fit, names another file, or changes nothing is a wrong answer and takes the retry ladder rather than becoming a card |
| `kanso research end ID` | end the run and remove the lane directory, and nothing else: the cards, the blobs and the best stay in state |
| `kanso research show ID [--sha S] [--diff S2]` | print a card's stored `strategy.py` (default: the best), or the unified diff between two of them. A sha is any unique prefix of one belonging to this hypothesis; a foreign or ambiguous prefix is refused (exit 3) |

A card is `keep`, `discard` or `crash`. On a keep the hypothesis's best moves and the blob
is written to `hypotheses/<id>/strategy.py`; on a discard or a crash the lane's
`strategy.py` is restored from the best, else from the run's base. `results.tsv` is
rendered from state, so the history survives every restore.

Every `align_every` cards a run is asked whether it still tests its own idea; `stall_k`
consecutive non-keeps end it. Both are `[research]` keys of `kanso.toml`.

## The daemon and the queue

| command | what it does |
|---|---|
| `kanso research start` | detach a supervisor and start one worker per lane the envelope allows, plus the monitor. Prints the pid, the lanes and the log. A second `start` in the same workspace is refused (exit 2): the pid file is also the lock |
| `kanso research stop` | signal the daemon and wait for it to go. **Nothing is ended and nothing is cleaned up** — active runs and their lane directories stay exactly where they are, and the next `start` resumes them before it takes anything new — so stopping is a cheap act |
| `kanso research status` | the daemon, its lanes, every active run with its `lane_sha`, `best_sha` and `base_sha`, and the queue |
| `kanso research queue add ID [--priority P]` | put a hypothesis in the queue, or raise the priority of one already in it. Served by priority descending, then by arrival |
| `kanso align check ID` | run the alignment check now: the deterministic syntax-tree checks first, the model only when they pass. Drift is not an error and does not exit like one — a check that finds the run has wandered has already rewound the lane to the last aligned keep, re-pointed `best`, marked the cards since the last check and written an escalation, and reports that with exit 0 |

## Models

| command | what it does |
|---|---|
| `kanso models check` | print the register as the router reads it — which model serves which tier, which task class routes where, at what thinking effort and output cap — then make one minimal call to every configured model. A tier with no model behind it is refused before any call is paid for. A model that does not answer is reported rather than raised; the command exits 2 when any failed and 0 when they all answered |

Every call is ledgered, including the failed attempts of a retry, because a rejected answer
was still generated and billed.

## Operating

| command | what it does |
|---|---|
| `kanso status` | the one screen: what the lanes are doing, cards per hour over the trailing hour, the best metric per hypothesis, today's spend broken out by lane, unread escalations, and any hypothesis whose baseline would not run. Writes nothing, so it is safe against a workspace a daemon is working in and safe to run in a loop |
