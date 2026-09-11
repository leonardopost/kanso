# Concepts

kanso turns a sentence about a market into a strategy running on a stage. The words below
are the joints of that path. Each names a **guarantee** rather than a class: what the thing
promises, what it refuses, and what would have gone wrong had it promised less.

Two ideas run through all of them and are worth stating first.

**Everything that decides anything is content-addressed.** A hypothesis is pinned by the
sha256 of its bytes, a card is identified by the sha256 of the `strategy.py` that produced
it, a snapshot by the checksums of the data it froze. Nothing in kanso says "the current
version of"; it says "these bytes". A result that cannot be reproduced from recorded inputs
is a defect and not a limitation.

**The framework evaluates and refuses; agents decide.** Which construct, which thresholds,
which gates, which parameters — all chosen at runtime by a model from catalogues that
declare ranges, never by a default this package ships. kanso holds no numeric opinion about
research at all. What it holds instead is a list of things it will not do, and most of this
page is that list.

`docs/constructs.md` is the catalogue of what a hypothesis may be, `docs/workspace.md` the
files, `docs/cli.md` every command and exit code, `docs/extensions.md` how to add to any of
it.

*Every transcript below is real output from a workspace made by `kanso init --demo`. Its
`demo_mr` is the hypothesis that workspace ships with; `demo_filter` is a filter attached to
`demo_mr`, written for these examples. The only edit is that a long absolute path is elided
as `/…/`.*

## Hypothesis

The unit of research: one falsifiable thesis, plus everything needed to test it — the
universe, the holding period, the bar resolution, the data types it requires, its risk
limits, and its three windows. It is a file the operator writes,
`hypotheses/<id>/hypothesis.yaml`, and `kanso hyp add` registers it.

```
$ kanso hyp add hypotheses/demo_mr/hypothesis.yaml
registered demo_mr · hypotheses/demo_mr/hypothesis.yaml
universe   DEMO.SIM
grain      1m · bar
           research      2024-01-02..2024-12-31
           certification 2025-01-06..2025-05-30
           forward       2025-06-02..
status     classified
sha        e880fcafc8a60240f7b0bf24a807f22d72464e139974e1a1d222244a76942dec
```

That `sha` is the whole of the registration. **The file holds the idea and nothing about how
it is going.** Status, the pinned snapshot, the run history and the best card so far live in
`state.db`, never in the file, because a file that recorded its own progress would change
after every card and could not be content-addressed at all. What follows from that is the
useful part: a run is pinned to the bytes it began with, so the idea being tested cannot
drift out from under the test.

Re-registering while a run is active is refused (exit 2):

```
$ kanso hyp add hypotheses/demo_filter/hypothesis.yaml
error: demo_filter has an active run (f922aba53c5d48f3a3f40b45f850fb72), so it cannot re-pin
remedy: end the run with `kanso research end demo_filter` first
```

A hypothesis moves through `draft → classified → researching → candidate → certified`, and
`retired` is the one end. **Only an operator ends a line of research**, with `kanso hyp
retire`, and `kanso hyp resume` takes it back. A certificate, pass or fail, is a milestone
in a hypothesis's life and not its end, so a certified hypothesis returns to the queue at
lowered priority and keeps being researched, and so does one whose certificate failed. Every
`n_fail`-th consecutive failure escalates instead, because how many times an idea has failed
to certify is worth saying out loud and is not kanso's to act on.

`failed` survives as a status on hypotheses ended by a version of kanso that ended them:
`n_fail` consecutive failing certificates wrote it, the queue treated it as over, and no
command could bring one back — the remedy printed was to register the idea again under a new
id, which resets the trial count `deflated_sharpe` prices the search by. Nothing writes it
now, `kanso research queue add` takes such a hypothesis back, and `kanso hyp resume` clears
the failure count with it.

## Construct

What a hypothesis **is** in portfolio-construction terms — a whole strategy, a filter on
one, an overlay over one, an exit rule, a return forecast, an execution tactic, an
allocation rule. `kanso classify` assigns one, and the assignment decides three things
nothing later can renegotiate: what the hypothesis attaches to, whether its objective is
measured on its own or against its host, and which class the lane's `strategy.py` must
define.

The catalogue is **the domain, not a menu of what this version implements.** Three of the
seven constructs cannot be run in 0.1.0, and they are in the catalogue anyway, because a
taxonomy that names only what is built teaches an operator to misclassify their own idea.
Classification accepts one of those honestly and `research begin` refuses it, naming the
seam that would make it runnable — late on purpose. `docs/constructs.md` is the catalogue.

## Objective, and why a constraint is never one

A run optimises **exactly one scalar**. Which one is not a free choice: it follows from the
construct's objective mode and the hypothesis's horizon, by the one deterministic domain
rule in the system. A sub-daily sleeve is scored on net edge per trade; a daily-or-longer
sleeve on a walk-forward net Sharpe; an attached construct on the marginal version of
whichever of those applies.

`absolute` objectives score the construct alone. `relative` objectives score its **marginal
effect on its host**: the host is run by itself once per run, and every card's number is the
difference. A neutral modifier therefore scores exactly zero, which is what the baseline of
an attached construct should be. Here is one, on a filter attached to the demo sleeve:

```
$ kanso research begin demo_filter
lane dir   /…/runs/op/demo_filter
run        f922aba53c5d48f3a3f40b45f850fb72 · 20260906-1 · lane op
snapshot   4592f8c0dbed3f78ec2f9278f239c5ca080abf029a69553e9c2a8212c394a062
baseline   keep · metric 0.000000 · 4.2s · budget 60s
next       edit /…/runs/op/demo_filter/strategy.py, then `kanso research card demo_filter`
```

Everything else a hypothesis cares about — a minimum trade count, a drawdown ceiling, the
integrity rules — is a **gate**, evaluated pass or fail, never folded into the number being
maximised. A constraint blended into an objective can be bought: a strategy trades its way
out of a drawdown limit by earning enough elsewhere, and the limit stops meaning anything.
As a gate it cannot be bought at any price.

## Run, lane and the envelope

A **run** is one research session on one hypothesis: its own directory, its own pinned
inputs, its own budget. One active run per hypothesis, ever.

A **lane** is one concurrent research worker with a directory of its own,
`runs/<lane>/<hyp>/`. The daemon's lanes are `l1..lN`; the interactive lane is `op`, and it
never blocks the daemon's. Lanes share no files, which is the whole of the concurrency
design.

`N` is not configured by hand. `kanso env detect` measures the host and derives the lane
plan into `envelope.yaml`: two cores and 4 GB per lane, less a reservation of one core and
4 GB — two and 8 GB when a live stage is colocated — and at least one lane whatever the
formula says, so a host too small to satisfy it still researches, one card at a time. The
memory figure per lane starts at that 4 GB floor and is recalibrated to 1.5× the largest
baseline card actually recorded, so the plan tightens once the machine has seen real work.
`[env]` in `kanso.toml` overrides the reservations and the cores per lane; an override is
clamped to what the formula can use, so an implausible number yields a small plan rather
than a crash.

A lane directory holds **exactly three files** — `hypothesis.yaml`, `program.md`,
`strategy.py` — and only `strategy.py` may change. That is not a convention: it is checked
before every card, and the first two are compared against the blobs the run pinned.

A lane writes no log of its own. What a run did — every card, its metric, its verdict and
each change of status — is recorded in `state.db`, as the card rows and the `events` table
every state change appends to, and read back with `kanso research show` (a card's source,
or the diff between two), `kanso research status` and `results.tsv`. The daemon's
`runs/daemon.log` is one shared stream for whatever the supervisor and its lanes print, not
a per-run record; `research end` removes the lane directory and loses nothing, because
nothing in it was the record.

## Snapshot

An immutable, content-addressed set of catalog datasets, plus the checksum of the resolved
instrument definitions. `research begin` pins the newest snapshot that covers the
hypothesis's universe and data requirements over its research **and** certification windows
and whose instrument checksum is the store's own. It refuses to start when none covers, and
refuses by name — the snapshot, what it pins, what the store holds — when the definitions
have moved since the newest covering snapshot was taken.

```
$ kanso data instruments resolve --as-of 2024-01-02
as of      2024-01-02
           DEMO.SIM → DEMO.SIM
resolved   1 instrument(s)
$ kanso data snapshot
snapshot   4592f8c0dbed3f78ec2f9278f239c5ca080abf029a69553e9c2a8212c394a062
datasets   1 · reproducible
instrument a2b290ce0b0542b34d8cd512bd338c72615c35201b4e0f5e13d7d8e9fac9b9b1
```

A dataset a snapshot pins is immutable: an overlapping write is refused, and `data backfill`
and `data sync` write successor datasets rather than editing one. The instrument checksum
is in the snapshot for the same reason as the data — a tick size reassigned next year must
not silently rewrite a card that was measured under the old one — and it is read back where
a run is pinned. The store is resolved before it is frozen: a snapshot over instrument data
is refused while the store holds no definition, since the checksum of nothing pins nothing a
run could use.

## Card

One experiment: store the lane's `strategy.py` as a blob under its sha256, run the backtest,
evaluate, record. Three outcomes, and each does something different to the lane.

| status | what it means | what happens to the lane |
|---|---|---|
| `keep` | every constraint passed and the keep rule cleared | this becomes `best`; the blob is written to `hypotheses/<id>/strategy.py` |
| `discard` | a constraint failed, or the improvement did not clear its noise floor | `strategy.py` is restored from `best`, else from the run's base |
| `crash` | the backtest raised, or exceeded its time or memory budget | the same restore, with the traceback tail recorded |

`results.tsv` is rendered from state rather than appended to, so the history survives every
restore. Three proposals into the demo:

```
sha7	metric	metric_se	n_trials	n_trades	wall_s	peak_mem_gb	status	desc
93510e2	0.000000	0.000000	1	0	2.643	0.307	discard	baseline
f729a53	9.986730	1.064759	2	1003	4.240	0.321	keep	fade a 2-sigma deviation from a 60-bar rolling mean
67ef6fd	3.153159	0.409428	3	2445	7.102	0.330	discard	narrow the entry threshold to 1 sigma
020aef2	0.000000	0.000000	4	0	1.203	0.307	crash	scale by a rolling sigma helper that does not exist (intentional crash)
```

**A card runs in a child process with no path to any catalog.** The parent reads the
research window out of the catalog and hands the points to the child, which starts in a new
session under an environment allow-list. A card therefore has no route to data outside its
window even if its code went looking for one. The parent supervises wall time and resident
memory and kills the process group on breach.

`n_trials` counts every card of every run of the hypothesis, baselines and crashes included.
It is recorded on each card and on every certificate, because it is the size of the search
that found the result, and no card may be dropped from a number that is part of a filename.

One certification gate deflates the result by that search, and it counts a narrower set: a
**trial** is a card that ran to a result and traded. A crash produced no metric to compare
and a card that placed no order did not trade the hypothesis, so neither is a candidate the
selection could have kept. The gate's count and the spread it deflates by are the same set —
it reports it as `trials`, which is at or below the certificate's `n_trials`.

## What a card must satisfy

Card-stage gates, and they are the only judgement that reaches a strategy while it is being
researched: everything else in the toolbox runs at certification or later, when the search is
already over. There are five.

| gate | what it refuses |
|---|---|
| `strategy_integrity` | a file that reads what the embargo hides, or imports outside the allow-list |
| `min_trades` | a metric earned on too few trades, or on one fold alone |
| `max_drawdown` | a run that fell further than the hypothesis permits |
| `position_size` | a position worth more, **or less**, than the hypothesis says it should be |
| `max_hold` | a position held longer than the hypothesis allows: `days` in calendar days, `trading_days` in the sessions it was held across — the period ends of a daily return period, so a weekend or a holiday inside a hold adds nothing. A closed position is timed from its entry fill to its exit fill, one still open when the window closes to that close; an attached construct on what it added to its host at period ends, a floor on the hold rather than a ceiling |
| `sizing` | an order the harness refused at the boundary under a `sizing` rule: the rule, the instrument, the instant and the book held. Recorded by the runner, chosen by no one |

**The ceilings are read on what the book can fund.** `max_position_pct` and `max_leverage`
are shares of the smaller of the hypothesis's `capital` and the balance the sleeve has left:
the capital, less what its fills paid and were charged, plus its positions marked at the last
print — the equity curve's own number, computed as the run goes. A strategy that has lost money
therefore cannot keep entering at its original size on borrowed money, and one that has made
money does not grow past its capital. `submit_entry` and an overlay's hedge legs are cut to that
room; an order an author builds by hand is not, and under a `sizing` rule the budget is funded
by definition (`docs/backlog.md`, rows 76 and 77). `self.balance` reads the number.

The fourth of those is the only one that carries a floor. `risk_limits` are three ceilings — a
position may not exceed `max_position_pct`, the book may not exceed `max_leverage` — so a
strategy holding a tenth of what its operator asked for satisfies all of them, and nothing in
the package could say otherwise. `position_size` is measured on `run.held`: what each
instrument was worth at each period end, marked at that period's price. Neither notional a run
already carried says that. A fill's is traded value struck at one price, so a strategy that
tops up in three orders looks like three small positions; a trade's is `peak_qty x avg_open`,
an opening cost basis, which is biased upward by the strategy that rebalances toward a target
as the price falls and blind to the drift of one entered once and left alone. A gate built on
either would refuse the compliant strategy and pass the drifting one.

Every held period is judged rather than an average of them, because a size instruction is
broken by one period that breaks it. For a construct attached to a host, the host's quantity is
subtracted first and the remainder re-marked, so what is judged is what the modifier added.

**Under a `sizing` rule the floor is not a gate at all.** `sizing: {mode: full_book, budget: N}`
in `hypothesis.yaml` moves the size of every order from the strategy to the harness: an entry
is the whole budget in one instrument at market — `budget / ((1 + 2 × cost) × (price + one
increment))`, floored to whole lots — at most one instrument is held at a time, every exit is
the whole position, and `submit_entry(id, side)` and `submit_exit(id)` take no size. A
proposal that names one is discarded by `strategy_integrity` before any backtest, with the
line and the reason; a shape the scan cannot see — an entry into a second instrument while
one is held with no exit in flight, the other side of a held name, a hand-built order — is
refused inside the handler that asked for it, the run stops there, and the card is a
`discard` carrying a `sizing` gate with the rule, the instrument, the instant and what was
held, which the proposer sees with its next cards. Nothing is spent grinding on a size that
could never validate. `position_size` stays as the backstop, and under the rule it judges
**entry fills** as a share of the budget — gathered per order, because the venue fills a
market order past a quarter of the bar's volume as two events — rather than period-end
marks, which a leveraged leg drifts through between two closes. A flip is `submit_exit(old)`
then `submit_entry(new, side)` in the same handler: the exit in flight nets the old leg to
nothing before the new one sizes, so one leg at a time needs no leverage. A sized overlay
(`docs/constructs.md`) names `Clip(instrument, side)` and the harness sizes it to the
overlay's own budget; the host's own share and the overlay's clips are told apart by a
ledger over the clip orders, so `self.held(id)` and `ctx.book` are the host's and
`ctx.clips` the overlay's, even in one name. A refused card places no order and is not a
trial. The rule is scope: a `best` earned under one sizing is not compared with a card run
under another, so adding or changing it clears the best — as does changing the objective,
whose units the best is a number in.

**Who chooses them.** `constraints` is the classifier's list, rewritten on every
classification. `required_constraints` is yours, and classification does not read or write it.
Both are evaluated, yours first, and a gate you require is not reprised by a model that names
it too. See `docs/workspace.md`.

## The keep rule

When a card's number is an improvement rather than an accident. Three clauses, and together
they are the loop's whole defence against fitting noise.

**A metric arrives with its own noise floor.** The objective is computed on each of the
research window's contiguous folds; the metric is their mean and `metric_se` their standard
error. An improvement counts only when it clears `max(min_delta, k_se × metric_se)` — the
operator's smallest interesting difference and the spread of the folds that produced the
number. Both parameters are fixed at classification, before any result is seen.

Here is the rule refusing a real improvement. The hypothesis carries `min_delta: 0.0` and
`k_se: 1.0`; the neutral baseline above scored `0.000000`:

```
$ kanso research card demo_filter --desc "withhold entries in the first ten minutes of each hour"
card       45cc54c · discard · withhold entries in the first ten minutes of each hour
metric     0.273688 ± 0.437542 · 844 trade(s)
cost       3.9s · 0.32 GB · trial 2
best       f72bc11
           strategy_integrity: pass — n_problems=0, problems=[]
           max_drawdown: pass — limit_pct=15.0, max_drawdown_pct=0.3480137950000208
```

Every constraint passed and the metric went up. It was discarded anyway: `0.273688 − 0.0` is
less than `max(0.0, 1.0 × 0.437542)`. The folds disagree with each other by more than the
improvement is worth, so the improvement is not yet distinguishable from the disagreement.

**The comparison is strict.** Equal is not better. A loop that kept ties drifts across a
plateau of indistinguishable strategies and calls the drift progress.

**Complexity pays double.** A keep that grows `strategy.py` past `[research]
max_lines_per_keep` lines must clear twice `k_se`. Added lines are added parameters however
they are spelled, and the cheapest way to move a metric is to add enough of them. Growth is
measured against the file the run is climbing from — its `best` — not against the previous
card, which may have been thrown away.

The first keep has nothing to beat: with no `best`, passing every constraint is the whole
rule.

## The embargo

The certification window is the data that judges a strategy, so the loop that writes the
strategy may not read it. That is `max(5 × horizon, 1d)` of separation between the end of
research and the start of certification, rounded up to whole days because the windows are
dated, and it is arithmetic checked at validation rather than advice in a prompt.

Enforcement has two layers, and neither is a convention anyone has to remember.

**The runner will not load it.** A card request may name only a window the hypothesis
declares, and the card path accepts only the research window; a certification-window request
is a refusal in code. The child process re-checks the points it was handed against the
window it was asked for, so the refusal survives the trip across the process boundary.

**Code that could reach around it never executes.** The `strategy_integrity` gate is a
syntax-tree check run *before* the backtest, not after. Imports are matched by full dotted
path against an allow-list of exact leaves; every path that reaches the data catalog is
denied by name, as are the builtins and modules that would let a strategy open a file or
read a wall clock.

```
$ kanso research card demo_filter --desc "read the catalog directly"
card       f128b63 · discard · read the catalog directly
metric     0.000000 ± 0.000000 · 0 trade(s)
cost       0.0s · 0.00 GB · trial 3
best       f72bc11
           strategy_integrity: fail — n_problems=1, problems=["line 10: import of 'nautilus_trader.persistence.catalog.ParquetDataCatalog' is denied — the persistence layer is the data catalog"]
```

`cost 0.0s` is the point of the design: nothing ran. This is a guardrail against the loop's
own proposer reward-hacking its way to a better number, layered with the data isolation of
the card subprocess. It is not a sandbox against a hostile actor and does not claim to be
one.

Two further denials are about corporate actions rather than about capability, both are
listed in the same gate's output, and both name their reason there so a proposer can act on
it. **`.cache`** is denied because it is the one route by which a `strategy.py` can hold an
instrument, and an instrument carries `info.splits` — every split of its life, including
ones after the window this card is judged on. (The word `info` is not itself denied:
`self.log.info(...)` is the engine's own logging call.) **Anything the engine derives from a
position's opening basis** — the account, `avg_px_open`, `peak_qty`, `realized_pnl`,
`equity` and the portfolio's P&L views — is denied because the engine leaves that basis in
the share count the position opened in, so after a split it is a price per share that no
longer exists. kanso's own extraction reads none of them, and `program.md` lists them for
the author.

## Delivery

The engine delivers by `ts_init` — availability — and, at a tie, keeps the order the
series were loaded in. A sleeve that trades several instruments therefore used to handle
the first name against a book the later names had not yet moved, and a market submitted
into the second filled at its previous close.

kanso batches a coincident grain — every bar at one `ts_init`, then every quote, then
every trade — into the venue before any author handler of that grain runs, and then runs
those handlers one at a time so each fill is visible to the next. `on_bar` of the first
instrument sees every instrument's last close for that instant; a fill against another
instrument of the instant is at that close. A limit placed in that handler sees only the
close, not the rest of the bar's range. An instant a name does not print is incomplete:
the silent leg still trades at the last price that was public, which is not lookahead.

A live data client that polls several series independently must emit the same
per-`(ts_init, kind)` markers. A single marker at the end of a poll that covered more
than one instant would flush the first instant after later books had already moved.

**Two grains in one run.** An overlay researched at a finer grain than its host loads both —
for the combined run and the host-alone run alike, so the difference between them is the
overlay. Every grain loaded reaches the venue whether or not the sleeve subscribes it, and
the exchange matches against the finest bar type it has seen for an instrument. So inside
such a run the host's market orders fill at the last print of the finer grain, not at the
close its own bar just delivered — on the soxpair catalog, a daily bar stamped 04:00 UTC
fills at the previous session's last after-hours print, four hours earlier at the median —
and a coarser bar on a day the finer grain is silent does not move the book at all. The
host's own certificate and its combined baseline are therefore different numbers by
construction, and a composed version that carries such an overlay is measured and deployed
under both grains, so a stage sees what composition measured.

## Corporate actions

A split is a bookkeeping change: a thousand shares at four dollars become a hundred at
forty, and nothing is bought, sold or earned. kanso **applies** the action rather than
trading through it, because the alternative is reading a one-for-ten reverse split as a
905% return — and on a leveraged ETF that teaches the loop to buy an inverse fund the week
before one.

You declare the splits an equity has been through in its own definition, as
`override.info.splits` in `instruments.yaml` (`docs/workspace.md`). The **venue** applies
them: on the first market point whose reference time reaches an ex-date, and one call
before that point is matched against anything, the simulated exchange cancels every resting
order in that instrument, rescales every open position, and resyncs the portfolio index
behind the change.

It is the venue and not the strategy because a strategy is too late. A sleeve handles a
point only after the exchange has already matched against it, so a take-profit resting
above the market is filled at the restated price on the ex-date bar before any strategy
code runs — measured, 1,005 shares sold at fifty into a one-for-ten reverse split, a 40%
return on a bookkeeping change. Cancelling standing orders across a corporate action is
what a broker does anyway, which is why a deployment against a real broker loads none of
this. Both of kanso's simulated venues load the same module, so a backtest, a replay and a
paper stage apply a split at the same instant, and `kanso replay parity` compares the two
code paths across one at a tolerance of zero.

The account is not repaired, and cannot be. `Position.avg_px_open` is read-only in the
engine and no adjustment rescales it, so from the closing fill onwards the engine's own
realised P&L — and every balance credited from it — is wrong by the ratio. kanso reads none
of those numbers, `strategy_integrity` denies them to a researched strategy, and the card's
numbers come from the fills and the adjustments instead.

The card's own numbers come out of that: the equity curve folds the quantity change in at
the ex-date, and a trade is measured in the shares it opened with — its size and `avg_open`
are what was actually bought, `avg_close` is what came back per opening share, and the only
profit or loss a split itself produces is the fractional residue a reverse split truncates
away. A position too small to survive one — under a lot after the ratio — is refused rather
than deleted, since kanso holds no cash to pay it out in lieu.

**A window holding a split you have not declared is refused.** When the run's data carries a
`corporate_action` point of kind `split` whose ex-date falls inside the window, and the
instrument's definition schedules none — or schedules a different ratio — the run stops in
the parent process, before a card's child is spawned:

```
$ kanso research begin demo_mr --tag 20240101-1
error: DEMO.SIM: the window holds a split effective 2024-02-01 at a ratio of 0.1, and its definition schedules none
remedy: add the split to `info.splits` in this instrument's `override` in instruments.yaml, then re-resolve and re-snapshot
$ echo $?
2
```

How far that reaches depends on where a workspace's corporate actions came from. A dataset
whose points carry the instant each action was **announced** is snapshot-covered like any
other, and its splits reach this check. A source that serves only effective dates cannot
say when a split became knowable, so such a dataset declares no publication instant at all
and no snapshot will rely on it — which is the same refusal one step earlier, and is why
the shipped `massive_corporate_actions` loader marks any spec including splits `unknown`.
And a workspace that loads no corporate actions has told kanso nothing about its
instruments' history: kanso invents none, and the schedule on the definition is what makes
a window spanning one researchable at all.

## Certification, the plan and the certificate

Research produces a candidate. Certification decides whether it survives data it has never
seen.

**The plan comes first, and no default exists.** `kanso cert plan` asks the best model on
the register what would count as proof for *this* hypothesis: which gates, at which stage —
`cert`, `paper` or `live` — with which parameters chosen inside the ranges the toolbox
declares, and why. The planner is shown the hypothesis, its construct, the toolbox, what
data the workspace holds and the trial count. It is **never** shown a card metric, a
certificate or the strategy source, so a plan cannot be tuned to the result it is about to
judge. kanso validates the plan against structural invariants — every required gate present,
every parameter inside its range, at least one gate at each of the three stages — and sends
the complaints back to the same model, then to the next tier up; a plan still invalid after
that fails the step. With no model configured there is nothing to fall back on and the step
exits 2.

The plan is pinned. It changes only through `cert plan --replan`, which re-runs the planner
on the same closed inputs and mints the next `plan_version`.

**A paper window is chosen against the certification window, and `cert plan` says so when it
is not.** The paper gate compares the objective a stage realises against the interval
composition measured over the *certification* window: two estimates of the same quantity, and
the shorter one scatters more widely around a band that does not widen with it. So a plan
that judges a fortnight against a year is asking a sound version to look drifted, and the
command warns — with the two spans and how much noisier the shorter one is — while pinning
the plan anyway. The plan is the planner's and the windows are yours; the warning is all kanso
has to say about the pair, and a short paper window you meant is a short paper window you get.

```
$ kanso cert plan demo_mr                 # the gates, the exclusions and the last two lines are elided
warning    paper window 5d against a 145d certification window · the paper objective is about 5x noisier than the band it is judged against, so a version behaving as certified can read as drifted · raise min_duration or horizon_mult, or narrow windows.certification
```

**A certificate is immutable, and that is what makes it evidence.** It records one
evaluation of one `strategy_sha`, on one data snapshot, under one plan version, on one
engine version — every gate with the numbers it decided on, or the reason it judged nothing.

```
$ kanso cert run demo_mr
verdict    demo_mr · f729a53 · pass
gates      5 judged · 5 pass · 0 fail · 0 skipped
           pass  embargoed_window      certification=10.149870877220001, min_fraction=0.5, objective=net_edge_bps, research=9.98672990339711
           pass  publication_lag       n_datasets=1, published_too_early=[], tolerance_s=0.0, unknown=[]
           pass  parity_replay         compared=850, divergence=None, engine=20260906T010234Z-engine-6ca3f78, engine_intents=850, identical=True, max_ts_delta_ns=0, node=20260906T010230Z-node-6ca3f78, node_intents=850, ts_ns=0
           pass  cost_stress           metric_a=5.146082462396991, metric_b=0.14229404757397998, mult_a=2.0, mult_b=3.0, objective=net_edge_bps
           pass  bootstrap             limit_pct=15.0, mdd_p95=0.38450757237500577, n=1000, objective=net_edge_bps, objective_ci90=[7.823304135204389, 12.394500908889786]
objective  net_edge_bps 10.149871 ± 0.603055
pins       engine 1.231.0 · plan 1 · snapshot 4592f8c0dbed3f78ec2f9278f239c5ca080abf029a69553e9c2a8212c394a062 · trial 4
written    /…/certificates/demo_mr/f729a53-4-p1-e1.231.0.yaml
source     /…/certificates/demo_mr/f729a53.py
next       kanso cert show demo_mr
```

Certifying the same bytes again under the same plan **and** the same engine is refused:

```
$ kanso cert run demo_mr --json
{
  "error": "demo_mr already certified f729a53 under plan version 1 and nautilus_trader 1.231.0 on 2026-09-06T01:02:36.258656+00:00; a certificate is immutable",
  "code": 2,
  "remedy": "research a better strategy, replan, or upgrade the engine"
}
```

The engine version is in that condition on purpose. A certificate is a claim about a
strategy *under an engine*, so an engine upgrade invalidates it — and re-certifying the
unchanged bytes is then a plain `cert run`, with no replan and no frontier planner call.

**A failing verdict is not an error, and it ends nothing.** `cert run` exits 0 and says
`fail`, because the certificate is what the command produces and a fail is evidence: it
counts toward the `[certify] n_fail` run, the **ids** of its failing gates are fed back into
the next proposal, and every `n_fail`-th consecutive failure writes an inbox entry. The
hypothesis returns to research either way; only `kanso hyp retire` ends one.

The ids alone, and never the evidence. A certification gate measures the certification
window and records what it measured — `embargoed_window` and `walk_forward_consistency` both
put the objective's value over that window in theirs — while the proposer that reads this
feedback writes the next `strategy.py`. Handing it those numbers is a route from the
embargoed window into research, arriving as feedback rather than as a backtest request, and
the embargo has no exception for feedback.

The certified bytes are written beside the certificate as `<sha7>.py`, so a certified
subject travels with the files even where `state.db` does not.

## The strategy version

What a passing certificate composes: a **sleeve** becomes version 1 of a new strategy, or
version n+1 of its own when it is certified again with different bytes or under another
engine; an attached construct becomes its host's version n+1 with itself appended. A version is not a
pointer to source that might change — it is a closed record of four things.

- **What it is made of**: the sleeve's `strategy_sha` and each attached construct's, by sha.
- **`impl/<version>/`**: a verbatim copy of every certified source plus a manifest naming
  the classes and the configuration they are constructed with. This is the one directory a
  backtest, a replay and a live node all load — one class everywhere, so the thing that was
  measured and the thing that trades cannot drift apart.
- **`pins`**: what it was certified under — the kanso version, the engine version, the
  criteria version, the plan version, the data snapshot and the resolved venue model.
- **`expectation`**: what composition measured by running that implementation over the
  sleeve's certification window — the objective, a ninety-percent interval and the
  ninety-fifth-percentile drawdown. The paper and live gates judge the deployment against
  this band, so it is measured rather than declared.

The identity really is the bytes. In a workspace that has just certified and composed:

```
$ shasum -a 256 hypotheses/demo_mr/strategy.py certificates/demo_mr/f729a53.py \
      strategies/demo_mr/impl/1/*.py
f729a538831e3ea8f80c46b68c5993ed4662c168bd9c56541cdf570619b6f6e9  hypotheses/demo_mr/strategy.py
f729a538831e3ea8f80c46b68c5993ed4662c168bd9c56541cdf570619b6f6e9  certificates/demo_mr/f729a53.py
f729a538831e3ea8f80c46b68c5993ed4662c168bd9c56541cdf570619b6f6e9  strategies/demo_mr/impl/1/kanso_impl_sleeve_demo_mr_f729a538831e.py
```

kanso checks that last line itself, every time it loads the directory: a source that no
longer hashes to what the manifest records is refused by name and nothing runs, so a version
is the bytes it was certified with or it is nothing.

A version's life is `composed → paper → promotable → live → retired`. At most one version of
a strategy per stage; a replaced version is retired. A sleeve certified again composes its
next version and the paper stage replaces the one it holds, so an overlay composed onto the
old version leaves the stage with it until it certifies against the new one. The same bytes
certified again under the same engine return the version they already have, whatever its
position or state; under a new engine they compose a new one, since a version is deployed
only on the engine it was measured on.

## Stages

Two: `paper` and `live`. A stage is a capital allocation, a set of limits, an execution
client, a data client and a kill switch. `portfolio.yaml` names them and `kanso portfolio
deploy` renders the node.

```
$ kanso portfolio show
paper      up · exec sandbox (simulated) · data replay · speed 1 · capital 100,000
           clock 2025-09-01 · catalog to 2025-09-01 · allocated 40,000 · pnl +2,192.07
           demo_mr@1               40,000  pnl +2,192.07 over 1 window(s)
live       down · exec sandbox (simulated) · data replay · speed 1 · capital 0
           clock never run · catalog to nothing · allocated 0 · pnl +0.00
limits     gross 100% · net 100% · per strategy 40% · daily loss 3%
```

The stage file carries only the **id** of an execution client. What matters is the pair of
declarations behind that id: `capital` is `simulated`, `broker_paper` or `real`, and `clock`
is `replay` or `wall`. Those two declarations, and not any string in a configuration file,
are what forbid real money off the live stage and what forbid replayed history feeding a
broker. `kanso portfolio clients` prints them and `kanso doctor` grades them; `docs/cli.md`
lists every refusal `deploy` makes and its code.

**A passing certificate composes and deploys to paper on its own.** Both acts follow from
the certificate with no decision left in them, and a loop that runs indefinitely cannot stop
at every certificate to ask for a command with only one possible form. In the transcript
above nothing was deployed by hand: `cert run` passed, and paper had the version.

## Promotion and demotion

**Promotion is the one thing kanso will not do by itself.** Everything else on this page
happens without a person: classification, research, certification, composition, paper
deployment, demotion, halting a stage. Moving a version onto real capital does not.

What moves a paper version to `promotable` is one gate, `paper_forward`, and it is strict
both ways: the version must have been on the stage for the longer of the plan's minimum
duration and its horizon multiple — a shorter window is a `fail`, not a skip — and the
objective it realised must fall **inside** the ninety-percent interval composition
measured, above the band as much a fail as below it, because a stage that out-performs its
certification is not reproducing what was certified. `docs/cli.md` has the pass.

`--as NAME` is the whole of the approval. There is no environment fallback, no default and
no way to configure one. The approval is recorded against that exact version before anything
moves, and without `--as` the command changes nothing and **exits 4**:

```
$ kanso promote demo_mr@1 --live
error: promote: moving demo_mr onto the live stage is a named operator act
remedy: kanso promote demo_mr --live --as NAME
```

Exit 4 is its own code because a missing approval is a missing *act*, not a broken
precondition. Everything else in the way is exit 2 — a version the monitor has not yet found
promotable, and then, once it has, a live stage with no capital left to fund it. The two
below are the same command before and after a `kanso monitor run`, which is what moves a
paper version to `promotable`:

```
$ kanso promote demo_mr@1 --live --as leo
error: demo_mr@1 is paper, not promotable
remedy: only a promotable version makes this move

$ kanso promote demo_mr@1 --live --as leo
error: stages.live has no capital left to fund demo_mr@1
remedy: raise stages.live.capital, or demote what holds it
```

With the version promotable and the stage funded, the approval is recorded and both stages
are redeployed:

```
$ kanso promote demo_mr@1 --live --as leo
promoted   demo_mr@1 · live
approved   leo at 2026-09-06T01:02:49.802043+00:00
live       1 version(s) · 20,000 · 20260906T010252Z-live-3e932eb
paper      0 version(s) · 0 · no node ran
```

Because the approval is recorded against the version rather than read from the file,
**editing `portfolio.yaml` by hand can never move real money**: the file says what is
deployed, and the record says what was allowed. An execution client declaring
`capital: real` holding a version with no approval on record is refused at deploy, under the
same exit 4.

**Demotion is automatic and needs no one.** A live version that fails any live-stage gate is
demoted by the monitor; `kanso demote` does the same by hand. It is symmetric with
promotion — back to paper, or retired when a newer version already holds the stage — and the
stages that are not halted are redeployed after:

```
$ kanso demote demo_mr@1
demoted    demo_mr@1 · now paper
paper      1 version(s) · 40,000 · 20260906T010253Z-paper-54f7c28
live       0 version(s) · 0 · no node ran
```

The asymmetry is deliberate. Taking risk off needs no permission; putting it on does.

There is one exception to demotion, and it is the stronger act rather than a weaker one: a
live version that breaches the stage's daily loss limit **halts the stage** instead of being
demoted, since demoting into a halted stage would change nothing about the money. A halted
stage stays halted until the operator clears the switch — a redeploy that cleared it by
starting a node would make the switch advisory.

## Escalation

kanso escalates five things and nothing else: `misaligned`, `cert_failed`, `promotable`,
`demoted`, `deploy_blocked`. Each entry names its subject and the commands that kind offers
over it.

```
$ kanso inbox
unread     1 escalation(s)
e33c9656 promotable    demo_mr@1           demo_mr@1 passed every paper gate (paper_forward) and is ready for real capital
         kanso strat show demo_mr@1 · kanso promote demo_mr@1 --live --as <your name>
file       /…/escalations/inbox.md
```

`escalations/inbox.md` is append-only and never rewritten; read state lives in `state.db`,
which is what says which entries are unread.

**Acknowledging is never approving.** `kanso inbox ack ID` writes one timestamp and stands
for no decision: the actions the entry offers are still there to take, and an agent that
acknowledged a `promotable` entry has been told about a possibility, not given permission to
act on it.
