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
| `kanso doctor [--report] [--check-adapters]` | diagnose the workspace, the install, the engine, the credentials, the data adapters, the execution clients and the lanes. Exits 2 when a check fails. `--report` redacts paths for pasting upstream. Makes no network call unless `--check-adapters`, which probes what each configured adapter reaches — a dataset a plan excludes is reported and not graded down; a credential that does not authenticate fails |
| `kanso ext show` | every extension this workspace carries: whether it imported, and per id its `PROVIDES` declares, what the registry for that kind did with it — `registered` (it hands the id out), `shadowed` (it hands out the packaged one instead) or `absent`, with the reason. Exits 0 whatever it finds, because a broken extension degrades a workspace rather than stopping it and `doctor` is where that is graded. Opens nothing and resolves no credential — `docs/extensions.md` |
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
| `kanso data adapters [--check]` | what is registered here: id, kind (`data`, `reference`, `exec`), the credential names each needs and where each resolves from (never a value), its capabilities, its loader ids and its quota. Without `--check` it performs no network I/O. `--check` probes each **configured** adapter for what its key actually reaches: entitlement per dataset at the grain the source gates on, and the measured history floor of each entitled price series — `docs/adapters.md` |

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
| `kanso research run ID [--cards N]` | the same loop with the model in the agent's seat: begin a run if there is none, then propose → apply → evaluate until `N` cards or until the run stalls. `--cards` counts what **this** invocation proposed, so the baseline and everything a previous invocation left behind are not in it. A proposal is a unified diff over `strategy.py`, applied in-package; one that does not fit, names another file, or changes nothing is a wrong answer and takes the retry ladder rather than becoming a card. A run that stalls on a keep nothing has certified certifies it before returning, and the command reports that certificate |
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

## Certification

| command | what it does |
|---|---|
| `kanso cert plan ID [--replan]` | decide what would count as proof for this hypothesis — the cert, paper and live gates, each with parameters chosen inside the toolbox's ranges and a rationale — in one call to the best model on the register, and pin it at `certificates/<id>/plan.yaml`. The planner is shown the hypothesis, its construct, the toolbox, what data the workspace holds and the trial count, and never a card metric, a certificate or the strategy source. Reading a pinned plan costs nothing; `--replan` re-runs the planner on the same closed inputs and mints the next `plan_version`. There is no default plan: with no model configured the step exits 2 |
| `kanso cert run ID [--sha S]` | run the plan's cert gates for the hypothesis's best card (or the one `--sha` names, as any unique prefix) over the embargoed certification window, on the data snapshot the run that produced that card pinned, and write `certificates/<id>/<sha7>-<n_trials>-p<plan>-e<engine>.yaml` with the certified `strategy.py` beside it as `<sha7>.py`. Plans first if there is no plan. A certificate is immutable: certifying the same bytes again under the same plan **and** the same engine is refused (exit 2), so re-certifying an unchanged commit after an engine upgrade is a plain `cert run` |
| `kanso cert show ID` | the newest certificate: the verdict, then each gate with its evidence or the reason it judged nothing |

**A failing verdict is not an error.** `cert run` exits 0 and says `fail`, because the
certificate is what the command produces and a fail is evidence: it counts toward the
`[certify] n_fail` run, its failing gates are fed back into the next proposal, and the run
that exhausts the allowance turns the hypothesis `failed` and writes an inbox entry. A
snapshot holding a dataset of unknown publication, or a vendor-adjusted one, is a recorded
fail for the same reason — it reaches the operator the way every other failure does.

A run that stalls with a keep nothing has certified certifies it there and then, so the
autonomous loop reaches a certificate without an operator. Either verdict returns the
hypothesis to the queue at priority −1; only `failed` and `retired` leave it.

**A plan that names `parity_replay` makes `cert run` replay.** That gate is the comparison
of the two code paths over the certification window, so the runner replays the subject on
the node path and on the engine path and hands the gate what the comparison found; the two
sessions it wrote stay in `sessions/` to be read. A replay that cannot be set up at all
leaves the gate without its evidence, and the certificate records that nothing compared the
paths rather than claiming that they agreed.

**A passing verdict composes and deploys by itself.** The construct's version is made and
the paper stage is offered it, because both acts follow from the certificate with no
decision left in them and a loop that runs indefinitely cannot stop at every certificate to
ask for a command with only one possible form. A stage that cannot take the version — it is
halted, the engine has moved, the catalog has no forward data, the limits leave no capital —
escalates `deploy_blocked` and the certificate still stands. What is never automatic is the
next step: paper to live needs `promote --live --as NAME`.

## Strategies

A strategy is composed, never written. A passing certificate composes the version it
implies and offers it to the paper stage on its own, so these commands are the hand-driven
form of an automatic act; running `strat compose` on a subject already composed returns the
version that exists rather than a second copy of it.

`STRATEGY[@V]` is the notation every command below shares: a strategy id, optionally a
version. Leaving the version out means the one the command's own rule picks — the latest
for a read, the version on the stage for a move.

| command | what it does |
|---|---|
| `kanso strat compose ID` | turn this hypothesis's newest passing certificate into a version: a new strategy at version 1 for a sleeve, the host's version n+1 for a construct attached to one. Writes `strategies/<id>/strategy.yaml` and generates `strategies/<id>/impl/<version>/` — a verbatim copy of every certified source plus a manifest naming the classes — which is the one directory a backtest, a replay and a live node all load. Runs that implementation over the sleeve's certification window to measure the version's `expectation`: the objective, a ninety-percent interval and the ninety-fifth-percentile drawdown |
| `kanso strat show [STRATEGY[@V]]` | with no argument, every composed strategy and the state of each version; with a strategy, its versions and their bands; with a version, what it is made of, what is expected of it and what it was certified under |
| `kanso strat retire STRATEGY[@V]` | end a version: take it off whatever stages hold it and mark it retired, then restart the stages whose kill switch is off. A stage a kill switch has halted is named and left halted |

## Portfolio

| command | what it does |
|---|---|
| `kanso portfolio show` | both stages: how each is configured — including whose money its execution client trades and which clock it runs on — whether its node has consumed everything the catalog holds, what each deployed version holds and what it has realised over the windows its stage has closed. Writes nothing |
| `kanso portfolio clients` | every execution client a stage may name: what each declares (`capital`, `clock`), which adapter provides it, which stages it may be configured on, and per credential the variable name and where it resolves from — never a value. Then, per stage, what `deploy` would refuse its configuration for, or `ok`. Opens nothing and reaches nothing |
| `kanso portfolio deploy --stage paper\|live` | admit what composition produced, apply the capital rule, validate what the stage's execution client declares, render the node configuration and (re)start the node. A node flattens before every stop, so a stage always restarts flat and each redeploy realises its window into the record the paper and live gates read |
| `kanso promote STRATEGY[@V] --live --as NAME` | move a `promotable` version onto the live stage under a named operator's recorded approval, retiring whatever was live, then redeploy both stages |
| `kanso demote STRATEGY[@V]` | take a live version off the live stage — back to paper, or retired when a newer version is already there — then redeploy the stages that are not halted |

**`deploy` refuses six things with exit 2** and two with exit 4.

With exit 2: a stage whose `kill_switch` is on, because the switch is the operator's and a
deployment that cleared it by starting a node would make it advisory; an execution client id
nothing in the workspace provides; a version whose `pins.nautilus_version` differs from the
installed engine, because running it under another engine is running something that was
never measured — the way out is a plain `cert run` on the same commit; a stage whose catalog
holds nothing at or after the forward window's start, because that stage has nothing to
trade and nothing to be judged on; a `clock: wall` execution client paired with replay data
or with any speed but one, because a broker matches against current prices; and a
`clock: wall` client at all, because in this version a stage node cannot run one — see
below.

With exit 4: a `capital: real` client configured anywhere but the live stage, and a version
on a `capital: real` client with no approval on record. Both are a missing act rather than a
broken precondition, which is why they carry their own code.

**Execution clients, and what each declares.** A stage names one in `portfolio.yaml`, and
the id is all the file carries; what matters is the pair of declarations behind it. `capital`
is `simulated`, `broker_paper` or `real`, and `clock` is `replay` or `wall`. Those two are
what forbid real money off the live stage, what forbid replayed history feeding a broker,
and what make a promotion the only way a version reaches real capital. `kanso portfolio
clients` prints them; `kanso doctor` grades them.

**A `clock: wall` stage needs a live data client and `speed: 1`.** A wall-clock client fills
against the price the market is showing now, so pairing it with `data: replay` would fill
orders at prices unrelated to the data that triggered them, and any speed but real time
would compress a market that is not compressible. `data` therefore names a live data client
an adapter provides — for a broker's own feed, usually the client id of the same account —
and `speed` is 1.

**In this version `deploy` refuses a `clock: wall` client outright (exit 2).** A stage node
here is a bounded run: it releases whatever the catalog holds that the stage has not
replayed, into kanso's own simulated venue, flattens and returns. That is exactly what a
`clock: replay` client declares it is executed by. A wall-clock client needs a node that
outlives the command that started it, and running one through this node would fill every
order in simulation while the stage record — and the paper and live gates reading it —
called the money the broker's. The declarations, the refusals and the promotion path are all
live; the long-running node is the piece that is not, and it is tracked in the backlog.

**`promote` is the only command in kanso that can put money at risk, and the only one that
requires a person.** `--as NAME` is the whole of the approval: there is no environment
fallback and no default, the approval is recorded against that exact version before
anything moves, and without `--as` the command exits 4 having changed nothing. Editing
`portfolio.yaml` by hand can therefore never move real money — the file says what is
deployed and the record says what was allowed. Agents pass `--as` only on an explicit
operator instruction; acknowledging an inbox entry is not one.

## Replay

| command | what it does |
|---|---|
| `kanso replay run (--strategy STRATEGY[@V] \| --hyp ID [--sha S]) [--from D] [--to D] [--speed N] [--mode node\|engine]` | replay one target over the catalog and write `sessions/<id>/`: the record, the points released and the order intents that came back. `node` is the live code path — a trading node, kanso's replay data client, a simulated execution client — and `engine` is the research one. The range defaults to the target's forward window through the last day the catalog serves |
| `kanso replay parity (…)` | replay on both code paths over the same days and compare the order intents element by element — instant, instrument, side, quantity, order type and, for an order that names one, price — reporting the first divergence with its index and its field, or that the two agreed. `--ts-ns` is the instant tolerance in nanoseconds, and it exists to be set to zero |
| `kanso replay show [SESSION]` | one session, or every session this workspace holds |

**Replay always executes against kanso's own simulated venue**, whatever a stage is
configured with: a replay feeds history and a broker fills against current prices, so the
pairing would fill orders at prices unrelated to the data that triggered them. It is the
same venue a stage node attaches — one piece of code, so the exchange a version is judged
against and the exchange it is deployed onto cannot drift apart. Replay is evaluation
only — it writes no card, moves no `best` and certifies nothing — and the window it runs is
the one nothing may backtest.

## Monitoring

| command | what it does |
|---|---|
| `kanso monitor run` | one pass of the watch every deployed version lives under. The daemon runs it on `[monitor] interval`; this runs it once |

A pass judges each deployed version against the paper or live gates of its **sleeve
hypothesis's** plan, with the bands from the version's own `expectation`, and acts on the
verdicts: a paper version whose gates all pass becomes `promotable` and reaches the inbox;
a live version that fails one is demoted; a live version that fails the daily loss halts its
stage instead, since halting is the stronger act and demoting into a halted stage would
change nothing about the money. The pass also sums gross and net exposure per stage — the
two limits only a whole-stage view can see — and halts a stage on a breach.

Every action is taken once, on the transition, so the command is safe on a timer and safe
to run twice. It exits 0 whatever the verdicts are: a failing gate is a fact about a
deployment, not a failure of the pass that found it.

## Models

| command | what it does |
|---|---|
| `kanso models check` | print the register as the router reads it — which model serves which tier, which task class routes where, at what thinking effort and output cap — then make one minimal call to every configured model. A tier with no model behind it is refused before any call is paid for. A model that does not answer is reported rather than raised; the command exits 2 when any failed and 0 when they all answered |

Every call is ledgered, including the failed attempts of a retry, because a rejected answer
was still generated and billed.

## Operating

| command | what it does |
|---|---|
| `kanso inbox` | the escalations nobody has acknowledged, oldest first, each with the commands its kind offers over its subject |
| `kanso inbox ack ID` | mark one entry read. **Never an approval**: it writes one timestamp and stands for no decision, so the actions the entry offers are still yours to take. Acknowledging twice is acknowledging once. `escalations/inbox.md` is append-only and is never rewritten, so the file keeps every line and the rows are what say which are unread |
| `kanso status` | the one screen: what the lanes are doing, cards per hour over the trailing hour, the best metric per hypothesis, today's spend broken out by lane, unread escalations, and any hypothesis whose baseline would not run. Writes nothing, so it is safe against a workspace a daemon is working in and safe to run in a loop |
