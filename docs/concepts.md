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
sleeve on a walk-forward net Sharpe — or, when the operator declares a `benchmark`, on
that Sharpe less the Sharpe of holding the universe's first leg over the same folds; an
attached construct on the marginal version of whichever of the first two applies.

`absolute` objectives score the construct alone. `relative` objectives score its **marginal
effect on its host**: the host is run by itself once per run, and every card's number is the
difference. A neutral modifier therefore scores exactly zero, which is what the baseline of
an attached construct should be. A benchmark objective is the same difference against a
hold run by the same runner rather than against a host, so a strategy that only rode its
market scores zero too. Here is a relative one, on a filter attached to the demo sleeve:

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

The daemon keeps every lane of its plan running. A lane that ends while nobody stopped the
daemon — taken by the kernel's OOM killer on a large card, or crashed — is recorded as a
`lane_died` event and started again under its own name, and the lane in its place resumes
the run the dead one left, because a lane finishes its own run before it takes anything
new; what the dead lane held with no run yet goes back in the queue. The monitor is kept
the same way (`monitor_died`). A child that dies again soon after it came back waits longer
each time, up to five minutes, so one that cannot stay up costs a start every five minutes
rather than a start a second, and `kanso research status` lists every child the running
daemon had to start again.

`N` is not configured by hand. `kanso env detect` measures the host and derives the lane
plan into `envelope.yaml`: two cores and 4 GB per lane, less a reservation of one core and
4 GB — two and 8 GB when a live stage is colocated — and at least one lane whatever the
formula says, so a host too small to satisfy it still researches, one card at a time. The
memory figure per lane starts at that 4 GB floor and is recalibrated to 1.5× the largest
baseline card actually recorded, so the plan tightens once the machine has seen real work.
`[env]` in `kanso.toml` overrides the reservations, the cores per lane and that memory
figure; an override is clamped to what the formula can use, so an implausible number yields
a small plan rather than a crash. `mem_per_lane_gb` replaces the derived figure outright,
because the calibration reads the heaviest run the workspace ever recorded and that may be a
heavier hypothesis than the one now researching — an overlay on one-second bars leaves a
peak a daily sleeve will never approach, and every lane is charged for it until you say
otherwise.

**A lane's share bounds what the lane runs in its children.** The lane process itself holds
the engine, its store and, while a card runs, one read of the window and the chunk of it
being written to the child (Card, below), and it makes no run in its own process: every card, the baseline, a host-alone run and the hold
a benchmark objective differences against — once per run, over the research window — are
staged into a child it watches, and what a child cost goes when the child exits. A card's
child is killed once its resident memory passes the lane's share, floored at three times
what the run's baseline needed (`docs/workspace.md`, `envelope.yaml`). A stall's
certification is a child held to the same figure for a card of the run it judges: both
windows, every perturbation a gate runs and `parity_replay`'s node replay are made there,
and the lane only reads back the certificate, or the refusal it raised, and records what the
certification cost on the `stalled` event (`cert_peak_mem_gb`, `cert_wall_s`). Measured on a
16 GB host with six lanes at `mem_per_lane_gb` 2.5 while lanes still certified in their own
process: two idle lanes held 2.9 GB and 4.1 GB after certifying, swap reached 13.3 GB, and
nothing but killing them gave it back. A certification that needs more than the share is
killed and refused — the lane records a `lane_failed` naming what it reached and puts the
hypothesis back — and the answer is yours: a larger `mem_per_lane_gb`, which plans fewer
lanes around what certification actually costs, or `kanso cert run` by hand, which certifies
in your own process with nothing but the host to bound it. No wall time bounds a
certification, since how many engine runs it makes is its plan's choice. The hold moved out
of the lane for the same reason: run in the lane's own process it read the whole window at
once and left the lane holding what the engine allocated for it — 7.6 MB of peak on the
research suite's month of daily bars, measured in a fresh lane, against 0.2 MB now that a
child runs it. The baseline, a host-alone run and the hold have no memory cap, because they
are what the rest are measured against, and the hold has no wall time either; it is the same
run, element by element, as the lane made of it.

**A lane stops at its next safe point, whatever it ran.** Between cards, while it waits for a
claim and while a child certifies, a `SIGTERM` is the lane's own: a trading node takes the
stop signals for the loop it is handed and closing that loop does not give them back, so
every node kanso builds — a replay's, a stage's — hands them back to the process that built
it. Measured before that on an operator's workspace on 2026-10-01: lanes that had replayed a
certification's parity on a node did not exit within a minute of a `SIGTERM`, and only
`SIGKILL` moved them.

A lane directory holds **exactly three files** — `hypothesis.yaml`, `program.md`,
`strategy.py` — and only `strategy.py` may change. That is not a convention: it is checked
before every card, and the first two are compared against the blobs the run pinned. The one
thing kanso writes beside them is `.card/`, where a card's child writes its report and its
output — the window itself travels on the child's standard input and is never on disk —
which exists while the card runs and is emptied by the next card when a killed lane left it
behind.

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
— and, when the hypothesis declares a `warmup`, the sessions before each — and whose
instrument checksum is the store's own. It refuses to start when none covers, and
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
run could use. A run reads its definitions back from the store and nowhere else: `research
begin` and every card build the venue model from what the store holds, so a run asks no
reference adapter about an instrument its snapshot pins, however many lanes begin at once.

**Covers** is counted in whole UTC days, from the spans the datasets **served** — never from
what was asked of the source — on the days the instrument's market opened. A chunked
backfill whose chunk edge falls on a weekend or a holiday is served up to the session before
the edge and from the session after it, so the chunks' spans break although no session is
missing. A day the market was closed is not a hole: the spans either side of it join, and a
window may begin or end on one. The closures are dated facts in `kanso.data.closures`, filed
by asset class and venue as the tick conventions are and read off the definition the store
holds, so the venue's spelling in an instrument id does not matter. One calendar is on file,
US equities: weekends, and every weekday from 2003-09-10 to 2027-09-06 on which no US equity
venue opened — measured to 2026-09-25, and the exchanges' published closures after it. A day
the market opened that no dataset holds is a hole wherever it falls, and nothing a source
says closes one: an empty answer reads the same whether the market shut or the source lost
the day, and only the calendar tells which. A chunk answered empty on days the market opened
stays a gap, which `kanso data show` also lists under `empty` so that why it persists can be
read, and which `data backfill` does not ask for twice. An instrument the store does not
define, a market with no calendar on file and a day outside the span on file are read with
every day open, so what the calendar cannot state costs a refusal and never pins a hole.

## Screen

A measurement of declared relationships between declared series, made before any lane is spent
on the idea that rests on them: no strategy, no venue, no fill and no model call. It answers the
question a lane cannot answer cheaply — is this worth tokens — with a base rate: the loop tunes a
mechanism around an edge and cannot create one, and a screen costs CPU and no tokens.

```
$ kanso screen run screens/ou_rev/screen.yaml
screen     ou_rev · dc2affd · snapshot 524d901
window     2024-01-02..2024-03-29
cells      3
verdict    worth a lane: no · 1 pass · 0 fail · 2 thin
           pass · lead_lag/a>a/1h  mean -0.2189 ± 0.064 · t -3.41 · p 0.0015 · 64 session(s)
cost       0.3s · 0.26 GB
```

*That screen measured the hourly OU path of the test workspace against itself at one, two and
three hours: its returns revert: a pull of half its gap an hour is a lag-one reversion of −0.25 in theory, and
the screen reads −0.22. At six hourly bars
a session, two and three hours hold too few pairs a session to be a correlation, and the cells
say so rather than reading a number.*

**It reads what a card reads.** Every leg is read through the runner's own reader
(`kanso.nautilus.backtest.market_points`), at its grain, the points of one instant in the order a
card gets them, one session at a time, clamped to the window. Every instant is a `ts_init`: a
lead a screen finds is a lead in what was public.

**Two estimators.** On a grid, the realised correlation of returns sampled at the clock's step —
returns are not demeaned, because a session's mean return is noise and taking it out of a few
biases the correlation towards −1/(n−1). Without one (`hy`), the Hayashi–Yoshida covariance of
the two series' own returns, every pair of intervals that overlap once one series is moved back
by the lag: a grid at a fine step mostly samples prices that have not moved and shrinks a
correlation towards zero as the step shrinks, and this has no step to shrink, so it is the one
for prints against prints. A lead shorter than a second between two sources whose timestamps
mean different things — an exchange's own instant and a consolidated tape's, or one nobody
declared — is marked `clock_bound`: it may be the difference between the clocks.

**A response is set against the hurdle a card would pay.** A `response` cell fires on a
trigger — a move of at least `move_bp` within `within`, or a z-score over a trailing `lookback`,
cut at the session's open because the session is the unit — and enters the follower at its first point after the declared latency, exits at its first
point after the horizon, one position at a time. Its gross is what a taker would have made — a
quoted follower buys the ask and sells the bid it shows — and its hurdle is the round trip the
venue model charges, struck by the runner's own `fill_cost`: commission, slippage, the sale's
fees, the per-share commission, and the model's spread on a follower that crossed none. No
spread is charged twice. The null is tested on the signal less the follower's session drift, so
a trending month whose triggers lean one way cannot pass for a reaction; the result reports the
margin per event, the events a day, and `ceiling_bp_day` — what one notional on every event
earned a day, with no capacity limit and no sizing: a bound on what a search of the mechanism
could find, held against a campaign's target in the same units its lanes are scored in.

```
$ kanso screen run screens/ou_fade/screen.yaml
screen     ou_fade · f3d9549 · snapshot 524d901
window     2024-01-02..2024-03-29
cells      6
verdict    worth a lane: yes · 2 pass · 4 fail · 0 thin
           pass · response/a/40bp/1h/a/1h  ceiling +19.34 bp/day · margin +17.68 bp (gross +22.68, hurdle 5) · 1.09 a day · t +5.29 · p 0.0001
           pass · response/a/20bp/1h/a/1h  ceiling +16.83 bp/day · margin +9.617 bp (gross +14.62, hurdle 5) · 1.75 a day · t +4.18 · p 0.0003
```

**Derived legs are functions of legs, and a fitted one says on what it was fitted.** A
`basket` is a weighted sum of its legs' log prices, a `spread` is `log long − beta log short`,
and a `gap` is one asset's price on two venues as a fraction of the second. Each is live only
where all its legs are, moves at the union of their points, and trades leg by leg in its
shares when it is a response's follower. A spread's beta is stated, or fitted by least squares
of one leg's log price on the other's over the window (`fit: window`), in which case every cell
reading it carries `in_sample_fit: true`, or over the window's first fold (`fit: first_fold`),
which then scores no cell that reads it, so the later folds judge a hedge they did not choose.
A spread's increments against their own past is mean reversion measured without a model: a
`lead_lag` of the spread against itself.

**Missing data is fetched, never skipped.** A series the catalog lacks is fetched through the
adapter that declares it serves it, and a snapshot taken; a series no adapter serves is refused,
and the remedy is to build one (`docs/adapters.md`, the three declarations a screen asks).

**The session is the unit of replication, and the family is the lattice.** A cell's evidence is
one value per session — for `lead_lag`, the correlation of one series' returns with another's at
a lag, on a grid — reported as the mean across sessions and the standard error of their spread.
Each measure's cells are judged together by max-T over session sign flips: one shared vector of
signs per draw, `[screen] draws` draws, seeded from the screen's bytes, the snapshot and the
measure's index. So forty lags of one pair are one family, not forty chances, and the same pins
give the same numbers. Every result states the one assumption that rests on: that sessions are
roughly independent of each other. Lag zero is refused: the same instant's co-movement is not a
lead.

**Thresholds are declared, never defaulted.** A screen with no `verdict` measures everything and
judges nothing. One with a verdict judges each cell `pass`, `fail` or `thin` — too few sessions to
judge — and the verdict is part of the file's bytes, so loosening it after reading a number is a
new screen with a new result, beside the old one.

**A result is immutable.** It is keyed by the screen's bytes, the snapshot and the measure
library's version; the same three again return it as it was.

**The embargo holds.** A free screen's window is refused when it meets, for any registered
hypothesis holding one of its instruments, the span from that hypothesis's certification start
less its embargo to its certification end: a screen chooses ideas, and the data that judges an
idea may not have chosen it. A bound screen reads its own hypothesis's research window and
nothing else. The embargo binds the other way as well: `kanso hyp validate`, and so `hyp add`,
refuses a hypothesis whose certification window, with its embargo, meets a window a recorded
screen read for one of its instruments. Whichever arrives first, the data that chose an idea
never judges it.

**A screen never gates a lane.** `research begin` and `queue add` read no screen result.

## Card

One experiment: store the lane's `strategy.py` as a blob under its sha256, run the backtest,
evaluate, record. Four outcomes, and each does something different to the lane.

| status | what it means | what happens to the lane |
|---|---|---|
| `keep` | every constraint passed and the keep rule cleared | this becomes the run's `best`, and the hypothesis's when it beats that or the run already holds it; only then is the blob written to `hypotheses/<id>/strategy.py` |
| `discard` | a constraint failed, or the improvement did not clear its noise floor | `strategy.py` is restored from `best`, else from the run's base. One that held a book already judged under the pins and moved the number past that floor also appends a `same_book` event, naming the book and both numbers |
| `crash` | the backtest raised, or exceeded its time or memory budget | the same restore, with the traceback tail recorded |
| `redundant` | it did not keep, it held the same book as a strategy already judged under the run's pins, and it earned that strategy's number to within the hypothesis's noise floor | the same restore; the card carries the metric it measured, the `redundant` event names the card it repeats and both numbers, and the command that asked for it is refused |

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
research window — and the warmup sessions before it, when the hypothesis declares them —
out of the catalog and hands the points to the child, which starts in a new
session under an environment allow-list. A card therefore has no route to data outside its
window even if its code went looking for one. The parent supervises wall time and resident
memory and kills the process group on breach.

**The window streams to the child.** The parent starts the child first and hands it the
window on its standard input while it runs: it reads the catalog an hour at a time when the
hypothesis requires prints, quotes or a book and a day at a time otherwise, cuts each read
into chunks of at most 250,000 points — always between instants, so every point of an
instant is in one chunk — and reads the next chunk only once the last is wholly written. The
child runs a chunk before it reads the next, so the child holds the chunk it is running and
the parent the read that chunk was cut from — an hour of prints, quotes or a book, a day of
anything else — until every chunk of it is written; nothing of the window is ever on disk.
Where a window is cut changes nothing. Prints, quotes and book changes are read in the order
the catalog's files hold them, because the catalog's own sorted query leaves the points of one
instant in an order that depends on the span asked for: on a day of OKX BTC-USDT-SWAP prints,
79 of 524,932 instants came back in a different order read by the hour than read whole, and a
card trading on twenty minutes of them sent 9,064 orders read by the hour and 9,060 read
whole. Read from the files, the day's prints and its 10.8 million book changes come back in
the same order either way. The suite reads a catalog of prints that share instants unevenly
by the hour and whole, and runs one tick window read by the day, by the hour and by the hour
cut to seven points a chunk — of one name, and of two whose quieter one's book changes
always follow the other's and some of whose chunks hold its prints and none of its changes —
one whose first hour holds book changes and no print, and one daily window cut to a bar a
chunk; each is the card the whole window gives when it is run in one process. Measured on a day of BTC's book and prints on 2026-10-02: a fresh child holds about 0.2 GB of its own and 0.66–0.81 KB per point of the
chunk it runs, about 0.4 GB at the cap, and caps of 10,000, 50,000 and 200,000 points and
none gave the identical card at about 10 ms of CPU an extra chunk. Read and staged a day at
a time as before, the same day of a three-level book was estimated at 4.5 GB in the child and
4.8 GB in the parent, and a 45-day window at 22.8 GB on disk before the card began. A card's
clock starts with its child, before the window is read, so its `wall_s` and its time budget
count whatever part of the read the run did not overlap. A refusal the parent makes while it
streams — a `wanted` check, a stop, an overlay grain the catalog does not hold, a window that
holds nothing but its warmup — kills the child first and is raised as the refusal it is; a
child whose stream stops before its end refuses it, and never reports the part it was
handed as a card.

**A replay streams its window as a card does.** `kanso replay run` and `parity` — and so the
`parity_replay` certification gate — read the range through the reads and chunks a card's
child is streamed, in the replaying process: the research path runs each chunk in the engine
before the next is read, and the live path releases each into the node once the one before
has been released and the node has gone quiet. Between two chunks both do the same things in
the same order — the fills so far are priced at the chunk's quotes, the sleeve is held for the
next chunk's markers, the venue is bound to what it carries — so a range is replayed the same
however it is cut, and the two paths agree at a tolerance of zero over a window of one
name's or two names' book changes and prints cut to seven points a chunk, as over one handed
whole. What a session records of its stream is a count and a digest (`docs/workspace.md`,
`sessions/`), folded in as each chunk is released, so the digest of a range cut into chunks is
the digest of the range run whole and nothing is written until the replay has finished. So a
replay holds one read and one chunk of its range, never the whole, and keeps none of it on
disk. Measured on 2026-10-02 on one day of OKX BTC-USDT-SWAP's
three-level book and prints (5,668,044 points released): `kanso replay parity` gave
`identical` on 2,556 intents in 874 s at 0.88 GB resident, where the acceptance build that
read the day whole, earlier that day, reached a 6.0 GB footprint and was stopped; on two days
of GRAM-USDT-SWAP (863,292 points) it gave `identical` on 1,723 intents in 148 s at 0.47 GB,
against 813 s and 0.98 GB on that build, and
the streamed engine session's intents equal those of the same range run whole in one
process, element by element. Certification's own runs and a stage node still read their
windows whole (`docs/backlog.md`).

**Return periods are cut on the UTC clock.** The window opens at 00:00Z of its first day,
and from there the runner cuts one `[research] return_period` after another — a day by
default — for as long as the window lasts; a period exists only when a point landed in it,
and a point lands in the period its `ts_init` falls in. A bar is stamped at its close, so a
daily bar lands in the period after the day it summarises: on a 24-hour venue the day's bar
closes at 00:00Z and lands in the following UTC period, the same rule under which an equity
daily bar a vendor stamps at 05:00Z is counted in the UTC day of that stamp. On such a
venue trading days are calendar days, so a series that printed every day is annualised at
what `periods_per_year` observes — about 365 periods a year, the count the window held
over its own length in years, with no constant assumed; the warmup sessions the runner
resolves are the calendar days that printed, seven a week; and a `session_scope` point
admitting a name for a session must be stamped in `[00:00Z, first market point)` of that
session, because the session opens at midnight there — where the first market point is
the bar that closes at exactly 00:00Z, which summarises the previous day's last period,
that bar is ordered ahead of a scope point stamped at the same instant and is judged
under the previous session's scope while its return folds into the new period
(`docs/backlog.md` row 100). No calendar decides a session: the sessions are the days the
catalog holds prints on, whichever venue printed them, and the closures
`kanso.data.closures` holds decide only which missing days coverage excuses (Snapshot,
above).

A card proposed by a model carries the proposer's own account of what it was: `tags`, one
or more of the twenty-one strings `kanso.schemas.TAGS` fixes — `signal_*` for what the
change reads, `horizon_*` for how long it holds, `filter_*`, `exit_*`, `sizing_*`, and
`parameter_only` or `refactor` for a change that moves no structure. The vocabulary is the
package's rather than the model's because the tags are read back as a **coverage** table,
keyed by them, that every proposal is shown: for each tag, how many cards under the run's
pins carry it, the best metric among them and its status, and the newest. The recent
cards say what was tried last; the coverage says what has been tried at all, in a size
bounded by the vocabulary rather than by the hypothesis. A card made by hand carries no
tags, and reaches the table under none.

A card that ran and did not keep is then compared by what it **held**: its signature,
which is, for each session of the research window, the instruments and sides it held and
whether each was still open at the session's end. Both of a run's own records of a
position are read — what it held at each period end, and the spans of the positions it
opened and closed — because a period end is a sample, and a hypothesis whose positions
close before the session does is flat at every sample by construction. Signed from the
ends alone, such a hypothesis matched every candidate against every other at 100 percent
and never made an experiment again: measured in a live workspace, 192 of one's 201 stored
signatures held nothing on any of their 834 sampled days. The two are marked apart rather
than merged into "something was held", so the reading is strictly finer than sampling the
ends alone and never coarser: two strategies that differed under the old reading differ
under this one, and carrying a position over the close is not the same book as closing it
before. Two strategies with the same signature on nearly every shared session made the
same bets, however differently they were written — a threshold moved, a helper renamed, a
condition spelt the other way. Whether they also earned the same number is the rest of that
sentence, and it is read rather than assumed: a signature is stored with the metric its card
earned, and a candidate is **redundant** when it matches a strategy judged under the run's
pins on at least `[research] redundant_pct` percent of their shared sessions *and* its own
metric is within that hypothesis's noise floor — `max(min_delta, k_se x se)`, the floor
the keep rule is struck from too — of what that strategy earned. Then it is a card of that
status carrying the metric it measured, the lane restored as any non-keep restores it, a
`redundant` event carrying the card it repeats and both numbers, the command that asked for
it refused, and the proposer shown that card by name on its next turn.

Both clauses, because "its result is already known" is a claim about the result, and the
result is in hand when the refusal is decided. Measured across the 3,092 candidates the
live workspace of 2026-09-18 had turned away, 106 had earned a number further from the
book they repeated than that hypothesis's own floor, so the refusal asserted what their
two numbers denied. Where the daily book does determine the number, nothing changes and
loop memory is untouched: a vol-target sizing hypothesis refused 1,151 candidates, 1,090
of them against one book scoring 0.4674, and their own scores sat a median 0.0039 and at
most 0.0931 from it against a floor of 0.2713 — not one of the 1,151 is admitted, and a
size that moves no result is still not an experiment. What the floor admits is the case
the other way round: an intraday hypothesis measured on a per-trade edge, whose 289
matches of one book scored -18.780 to 9.179 around that book's -4.5452, a median 9.096
against a floor of 10.116, with 74 of its 325 refusals beyond it. The same book read by
session, and not the same result.

A redundant card is a card because the backtest ran and a real number came back — the
trial it counts as, the corner it fills in on the coverage table and the record the next
run reads are all things the search actually did, and dropping them was measured deflating
a certificate by a search more than a hundred times narrower than the one that ran. The
same argument is owed to the 106, and it is paid in the only currency they lack: a card
that matched a measured book and moved the number is an ordinary discard, with nothing in
`cards` to say which book it matched, so it appends a `same_book` event carrying what the
`redundant` event carries. The next proposals are shown both kinds in one list, each
saying which it is — a proposer told to hold a measured book and move its number cannot
apply that rule from the refusals alone, and each admitted candidate stores its own book,
so a hypothesis whose spread is wide against its floor re-treads a book about
`ceil(spread / 2 x floor)` times before matching resumes. What
it may never be is a keep: the keep rule is asked first, so a candidate that beats the
best is a keep whatever it resembles. Nor is there a gap between the two rules for a card
to fall into unjudged — they are struck from one floor, which the keep rule doubles only
when the file grew past its line budget, so a candidate that cleared the floor upward and
not the doubled bar is neither a keep nor a repeat, which is what a discard is. The
baseline is exempt, since it is the last run's best and its signature is already stored;
and signatures are stored for every judged run, redundant ones included, so the third
spelling of an idea is refused against the second as well as the first.

The rule is in the proposer's instruction, with what a signature is and what to do with
the refusals it is shown, for the same reason the phase is: a refusal the proposer was
never told about is a wasted ladder. Measured in a live workspace before it was, 2,822
proposals were refused in a day by a rule whose words — signature, redundant, session,
`pct` — appeared nowhere in the 2,393 characters the proposer was given. Every fact the
proposer is sent is named in that instruction, and a test reads the fact keys out of the
driver's own source to keep it that way. Signatures live under the pins — the hypothesis
file, the snapshot, the criteria — and a run under new pins starts with none; the books
under pins no run can be given again stay in `state.db` until `kanso state prune` deletes
them. The number
beside the book lives under one thing more, because those pins fix the question and the
data and nothing about the arithmetic: `[research] capital`, `folds` and `return_period`,
the venue model `[research] broker` and `portfolio.yaml` resolve, and the host version an
attached construct is differenced against all move a number while every pin stands still.
So do three the workspace declares nowhere: what the sleeve sizes to, which for an attached
construct is its host's budget as the host's `hypothesis.yaml` is registered *now*; the bar
grains the run loads, which is that same file's resolution beside the sleeve's own; and the
warmup sessions a card is fed, which are resolved from the catalog for every card, so a
`kanso data load` that adds a printed day inside the lookback moves them between two cards
of one run.
So a stored number carries a digest of what it was measured under, and an anchor is a row
measured the way the asking card was. Edit one of those keys and the stored numbers stop
being anchors until the books are measured again — which is the same statement as the one
above, that a result already known is a claim about a result. Set the key back and the
anchors are there again: a book judged under a second reading is stored beside the first
and not over it, so nothing an operator can edit and undo costs the loop what it has
already measured.

The search driven by a model has a **phase**, and the phase is a rule rather than a mood.
Misses since the last keep set it: for the first `[research] local_cards` the proposer is
asked for local changes — a parameter, a threshold, a window — and for the next
`structural_cards` a change that moves no structure is refused on the ladder like a
repeat, where structure is the syntax tree of `strategy.py` with every constant blanked.
Then local again, round until a keep or a stall. The rule is in the proposer's instruction
and the phase is a fact of every call, because a refusal the proposer was never told about
is a wasted ladder. Both lengths, like `stall_k` and `redundant_pct`, are framework search
rules: they bound the search and choose nothing within it.

A crash is the one card whose idea was never judged, so the turn after it is a **repair**.
The proposer is given the traceback and the change that produced it, as a diff over the
file it now holds — the lane was restored the moment the card crashed — and asked for the
same idea with the fault fixed rather than for a new experiment. Two repairs, then the
idea is dropped and the next turn asks for something else. Measured in a live workspace
before this existed: five consecutive crashes on one hypothesis were five state-handling
slips on ideas out of that hypothesis's own declared families, each costing a whole
proposal and returning nothing, and not one of the five was ever judged.

A stall is where the memory is read one more time. `[research] reseed_after_stalls`
consecutive stalls on the same best — counted since the last reseed — say the best is a
ridge the climb cannot leave, so the scheduler **re-seeds**: the next run starts from the
highest-scoring other keep under the stalled run's pins that is still aligned — a keep a
drift check marked is not ground to start from — else from that run's own base,
and from the best as before when there is neither. The decision is a `reseed` event and
rides on the `queued` passage, which `put_back` and `recover` keep; a decision written only
at the stall did not survive a live workspace. The best is not cleared. A run's best and
the hypothesis's are two records: a keep always moves the run's, and moves the
hypothesis's only when it beats it or when the hypothesis's best is that run's own — so a
re-seeded run climbs its own ancestry and replaces the best only by bettering it, and a
drift rewind in one run leaves what another run earned standing.

A re-seed moves the climb to another foot of the same hill; **exploring** asks for another
hill. `kanso hyp explore ID` — or a daemon lane, once `[research] explore_after_stalls`
stalls on one best have passed since the last exploration (zero, never, is the template) —
calls the `explore` task class with what the hypothesis's research learned: its pinned
`hypothesis.yaml` and `program.md`, its best `strategy.py`, the coverage of its cards by
tag, its keeps and their scores, its stalls, and each certificate's verdict with the ids of
the gates that failed and nothing they measured. The answer is one new hypothesis, whole,
judged on the ladder: an id nothing holds, a file that parses with that id and no
classification, a strategy the static alignment checks accept against it and whose bytes
the workspace has never stored, and windows that neither research past the end of the
parent's research window nor certify before the start of its certification window. Nor may
the candidate certify inside its own embargo counted from the last day the *parent*
researched — the latest research end of any pin the parent's runs held — rather than from
the end of the research window the candidate declares: the idea was chosen by scores
measured on the parent's research, so that is the last day it saw, and one certified sooner
would be certified on data within the embargo of the data that chose it. It is written as a
draft to `hypotheses/<id>/`, a directory that did not exist, and registered by nothing: an
`explored` escalation offers `hyp validate` and `hyp add`, and whether it gets a lane is
yours. Every attempt, by hand or by a lane, leaves an event under the parent — `explored`
when it wrote a candidate, `explored_failed` with the error and its remedy when it did not —
and the stalls a lane counts are the ones since the newest of them, so a provider that is
down costs one call per spell. A hypothesis not registered or never researched is refused
before any attempt and leaves neither. A lane's exploration that fails is never a failure
of the lane.

`n_trials` counts every card of every run of the hypothesis, baselines and crashes included.
It is recorded on each card and on every certificate, because it is the size of the search
that found the result, and no card may be dropped from a number that is part of a filename.

One certification gate deflates the result by that search, and it counts a narrower set: a
**trial** is a card that ran to a result and traded. A crash produced no metric to compare
and a card that placed no order did not trade the hypothesis, so neither is a candidate the
selection could have kept. A redundant card is one — it ran, it traded, and the keep rule
was asked before the signature, so it would have been kept had it beaten the best. That
another candidate held the same book makes it a correlated trial rather than no trial, and
how much less than one a correlated trial is worth is open on the same terms as the
hill-climbing path it sits on (`docs/backlog.md` row 59). The gate's count and the spread
it deflates by are the same set — it reports it as `trials`, which is at or below the
certificate's `n_trials`.

## What a card must satisfy

Card-stage gates, and they are the only judgement that reaches a strategy while it is being
researched: everything else in the toolbox runs at certification or later, when the search is
already over. There are eight.

| gate | what it refuses |
|---|---|
| `strategy_integrity` | a file that reads what the embargo hides, imports outside the allow-list, or binds a name its base class owns |
| `min_trades` | a metric earned on too few trades, or on one fold alone |
| `max_drawdown` | a run that fell further than the hypothesis permits |
| `maintenance_margin` | a book whose equity over its gross, with each period-end holding valued at that period's adverse extreme — a long at its lowest low, a short at its highest high — fell below the `book.maintenance_pct` the hypothesis declares. Carries no parameter; skipped without a floor, and on a run that held nothing at any period end |
| `position_size` | a position worth more, **or less**, than the hypothesis says it should be |
| `max_hold` | a position held longer than the hypothesis allows: `days` in calendar days, `trading_days` in the sessions it was held across — the period ends of a daily return period, so a weekend or a holiday inside a hold adds nothing. A closed position is timed from its entry fill to its exit fill, one still open when the window closes to that close; an attached construct on what it added to its host at period ends, a floor on the hold rather than a ceiling |
| `leg_edge` | a card whose named leg did not earn its place: in a fold that closed one of that leg's spells, the annualised Sharpe of their returns — `pnl_net / notional`, net of the leg's own fill costs — below `min_sharpe`. A spell belongs to the fold that closed it; one still open at the window's close counts nowhere; a fold whose spells cannot vary — one spell, or spells that returned the same, or the same but for the last bits of the arithmetic — scores zero; a leg that never closed one is skipped, not failed, and every skip says so in its evidence |
| `sizing` | an order the harness refused at the boundary — one a `sizing` rule forbids, or an entry built by hand that the book cannot fund: the rule, the instrument, the instant and the book held. Recorded by the runner, chosen by no one |

`position_size` is the only one that carries a floor on size: `min_trades` floors the trade
count, `maintenance_margin` the book's margin and `leg_edge` a leg's Sharpe, none of them a
position's size. `risk_limits` are three ceilings — a position may not exceed
`max_position_pct`, the book may not exceed `max_leverage` — so a strategy holding a tenth of
what its operator asked for satisfies all of them, and nothing in the package could say
otherwise. `position_size` is measured on `run.held`: what each instrument was worth at each
period end, marked at that period's price. Neither notional a run already carried says that.
A fill's is traded value struck at one price, so a strategy that tops up in three orders looks
like three small positions; a trade's is `peak_qty x avg_open`, an opening cost basis, which
is biased upward by the strategy that rebalances toward a target as the price falls and blind
to the drift of one entered once and left alone. A gate built on either would refuse the
compliant strategy and pass the drifting one. Every notional a run records or sizes — a
fill's, a trade's, a holding's, the room a sleeve sizes an entry to, the budget a `full_book`
rule fills and the book a stage reports — is `qty x price x multiplier`, the instrument's
contract multiplier being one for a share and the contract size for a future or an option.
Each recorded fill and trade carries the multiplier it was struck with, so a cost model
re-applied to the record charges the notional the runner charged. A record written before the
multiplier was kept reads as one, a share's; a run struck on a multiplied instrument before then
is re-run before a cost model is re-applied to it. The volume a certification holds a day's
fills to is the same product. A bar's volume counts the instrument's own unit — shares for a
share, contracts for a perpetual or a future — so `capacity_vs_adv` reads each day's volume as
`volume x close x multiplier` of the resolved definition the window was run with, and the
busiest day's fill notional is held to a share of an average struck in the same unit: contracts
against contracts, never contracts against the coins inside them.

**A perpetual is a linear contract settled in the account's currency.** A crypto perpetual
swap (`instrument_class: swap`, `docs/workspace.md`) is to kanso a contract whose notional is
`qty x px x multiplier` in its quote currency — the same product every other notional above
is — so it is built linear and an inverse one is refused. It settles and is booked in the
account currency of its venue — its `settlement_currency` and its quote currency must both be
that code, which `hyp validate` checks — and it is charged exactly what any other fill is:
the venue model's costs, once, by the runner, with its own maker and taker rates held at
zero.

**A perpetual's funding is booked once, by the runner, beside every other cost.** A
hypothesis holding a perpetual must require the `funding` type, and at each settlement the
extraction takes `qty x mark x multiplier x rate` out of cash: the realised rate of the
period that settled, on the signed quantity held at the settlement instant, marked at the
instrument's last print at or before that instant — of several prints at the instant, the
greatest, exactly as a period's mark is chosen. A long pays a positive rate and a short
receives it; a negative rate reverses both. What is held is every fill stamped before the
instant and no fill stamped at it. That is deliberately not the `<=` rule that books a
period's fills up to and including its end: the rate is public at the settlement — the
sleeve is handed it there — and the engine stamps the fill of an order sent in answer at
that same instant, so under `<=` a position opened because the rate was known would collect
it and one closed because of it would escape it. Which point of an instant an order answered
is recorded nowhere both code paths can read — a stage node stamps its orders by its live
clock, not by the data — so the line is drawn at the instant: a position opened by a fill at
08:00 pays nothing at 08:00, whether its order was sent in answer to the settlement or
rested from 07:00, and one closed by a fill at 08:00 still pays it. What a settlement sees
was decided before its rate was public. The payment is inside the return and the equity of
the period that holds the instant, and the run records each one in `funding` — the instant,
the instrument, the quantity held, the rate and what was paid, negative when it was
received; a settlement at which nothing was held pays and records nothing, and one in the
warmup prefix is never booked. A closed trade carries what it paid over its life in
`funding`: every settlement of its instrument after its opening fill, up to and including
its close. Its `pnl_net` is net of that and its `cost` is not, so a Sharpe, an edge and a
net edge all read one post-funding number, while `cost_stress` and `cost_scenario`, which
re-price the recorded fills, leave funding exactly as it was booked. The sleeve's `balance`
books the same amount when the settlement point is delivered, before `on_data` is handed it,
so a balance read there is the equity the runner strikes at that instant, on the same
holdings: an order placed in answer to the settlement changes nothing it settled, in the
balance as in the card. One thing the runner uses at the instant arrives only after the
point — a print of the instant that follows it, which moves the mark — and the first point
of a later instant settles the difference into the balance before anything else is done with
it. A stage node books funding into its sleeves the same way, because its venue is simulated
and settles none; an account a broker keeps settles its own, and a sleeve on one would not
book it (`docs/backlog.md` row 15). A stage replays the catalog, so a deployed sleeve is
handed and pays the settlements the catalog holds for its window — loaded from a file, or
fetched by the OKX package's `okx_funding` loader, which serves the exchange's settled rates
for its last three months (`docs/adapters.md`) — and a held perpetual pays nothing past the
last settlement loaded (row 107). And
neither margin nor liquidation is simulated: what bounds a perpetual book is the sleeve's
room, `max_leverage` and the `maintenance_margin` gate (`docs/backlog.md`).

Every held period is judged rather than an average of them, because a size instruction is
broken by one period that breaks it. For a construct attached to a host, the host's quantity is
subtracted first and the remainder re-marked, so what is judged is what the modifier added.

**The ceilings are read on what the book can fund.** `max_position_pct` and `max_leverage`
are shares of the smaller of the hypothesis's `capital` and the balance the sleeve has left:
the capital, less what its fills paid and were charged, plus its positions marked at the last
print — the equity curve's own number, computed as the run goes. A strategy that has lost money
therefore cannot keep entering at its original size on borrowed money, and one that has made
money does not grow past its capital. The room counts orders in flight as filled and holds back
what a resting limit or stop entry would add. `submit_entry` is cut to it, and so is each hedge
leg an overlay asks for, with the legs before it counted. An entry a strategy builds by hand is
not rebuilt at another size, and only the funding question is asked of it: one that would take
gross exposure past `max_leverage` of the book is refused inside the handler that placed it as
`unfunded_order`, and the card is a `discard` carrying a `sizing` gate. An order list is judged
whole, what each order closes freeing room for the next, and a bracket's exits are not asked.
A modify is asked nothing: `modify_order` is the engine's own, so an entry placed small and
grown by a modify, or a resting one moved to a price at which it opens more, is held neither
to the room nor to the funding question, and the room reads it at its new size and price from
then on (`docs/backlog.md` row 123). A `sizing` rule denies `modify_order` to a researched
strategy, so the gap is an unsized sleeve's. On a pair's
ex-date the venue restates the held leg at the day's first point, which may be the other leg's;
until the held leg prints again its last price is restated by the split's ratio, for the balance
and for the room. `position_size` still judges a position against the capital
(`docs/backlog.md`), and under a `sizing` rule the budget is funded by definition (row 76).
`self.balance` reads the number, and a read costs what the sleeve's orders gained since the
last one rather than what they hold: each event of each order — a fill, and the two a modify
adds — is folded in once, as the engine hands it to the sleeve, so a sleeve may read it on
every bar while it moves a resting order on every bar. Read whole each time, such an order
cost its whole history on every read and the square of it over a card — measured on a crypto
workspace, a card that re-priced its resting orders on every five-second bar ran 2.4 minutes
per simulated day and was killed. The engine keeps every event of every order for the whole
run, closed or not, so a card's memory still grows with every modify, and re-posting the
order saves none of it: one resting order moved on each of 30,000 one-second bars left the
engine holding 60,001 events, and the same sleeve cancelling it for a fresh one every 2,000
moves left it holding 60,043 across fifteen orders, at the same peak.

**A `book` policy changes the equity a card is measured on** (`docs/workspace.md` has the
keys). The runner applies it once, at each period end of the extraction, in one order: the
carry — the yearly `financing_rate_bps` on gross exposure, shorts counted, above the book's
equity, over the period's span — out of cash and so in the return; the maintenance ratio;
then, at the first period end of a calendar month under `reset: monthly`, a surplus over the
capital moved to a cushion or a deficit restored from it while it lasts. Returns are struck
before the transfer, so the sum of a run's returns is what book and cushion made together,
and the run carries `cushion`, `carry` and `worst_ratio` beside its equity curve. The harness
settles each period from the same functions when the next period's first point arrives, so
`self.balance` reads the book the policy left. Two consequences are deliberate. The equity
curve is the book after each transfer, and on a reset book a drawdown is bounded by the month
it fell in: `max_drawdown` judges each end on the equity struck before its transfer against the
peak so far, then starts the peak again at each month's first end from the higher of the
capital and the book after it — so a surplus swept into the cushion is no loss, a loss the
cushion restored ends with its month, and one it could not restore carries into the next as a
drawdown from the capital. And `cost_stress` multiplies fill costs — dividing a maker's
rebate instead, so no multiple makes a fill pay better — and leaves the carry alone,
because a rate on borrowed notional is not an execution cost; the transfers stand as struck,
so a stressed reset book carries its extra cost across months rather than having a turn
absorb it, and its drawdown is the more conservative for that. The engine enforces no margin
and charges no financing here — kanso's instruments carry no margin rates — so none of this
is delegated to the venue. A sized sleeve gets no special case: a full-book entry that
borrows pays the carry and can breach the floor (row 76).

The policy has edges, each recorded in `docs/backlog.md` row 86. `maintenance_margin` reads
end-of-period holdings, so a position opened and closed inside one period is never judged.
The harness settles a period on a point delivered to the sleeve itself, so a grouped series
the sleeve never subscribes to that holds a period's only point moves the runner's period end
and not the harness's, and `balance` lags one period until the sleeve is handed a point.
`bootstrap` resamples closed trades' net P&L, which holds neither the carry nor a transfer, so
its `mdd_p95` understates the drawdown a levered book recorded. The harness's period close is
out of reach both ways: its attribute reads are denied, and a `strategy.py` that defines a
method of the same name binds a name the base owns, which the same gate refuses, sized or not.

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
whose units the best is a number in, the `warmup`, since a run whose indicators were
fed before the open and one that spent the window's first sessions filling them measured
different things over the same days, the `benchmark`, since a Sharpe over a hold of the
first leg is not a Sharpe, and the `book` policy as a whole, since a reset, a carry and a
maintenance floor each change the equity path a metric is read from.

**One leg on its own.** A pair's number is struck on the book, so a hedge that pays its
spread at every switch and returns nothing of its own is invisible in it. `leg_edge` reads
one named leg's closed spells — the runner's `Trade`s in that instrument — and holds the
Sharpe of their returns to `min_sharpe` in every research fold that closed one, annualised
by the spells the fold held per year, the way `bootstrap` annualises the trades it
resamples. The leg is an `instrument` parameter: a value naming anything outside the
hypothesis's universe is refused at `hyp validate` (exit 3), from `constraints` and from
`required_constraints` alike. For an attached construct the spells the host's own run also
closed — the same instrument, instants, quantity and prices — are subtracted first, by
identity: a spell the candidate altered in any of those is judged whole, and one identical to
the host's is not judged at all. A fold whose spells cannot vary scores zero — one spell,
spells that all returned the same, or spells whose returns differ only in the last bits of
the divisions that struck them, which is not variation and would otherwise divide a mean by
a figure near zero. A skipped `leg_edge` records its reason in its evidence as well as in
its skip, because a card stores the evidence and a pass with an empty one reads as a leg
that was judged and cleared. It is the one gate that records it there: the others' skips
leave the evidence empty, and none of them is read as a named instrument having earned
its place.

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

The folds are `[research] folds` equal spans of the calendar, not of the sessions, and a
return period belongs to the fold its end falls in. So the metric is a mean of folds and not
of periods: every fold weighs the same however many sessions it holds, and a fold that holds
none scores zero and is averaged in like the rest. Over a research window of months the
folds hold nearly as many sessions each and the two means nearly agree; over a window of a
few sessions they need not, and that is the window a certificate measures (below).

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

## Alignment

A run optimises a number, and the cheapest way to move a number is often to stop testing
the hypothesis: trade an instrument outside the universe, read a finer bar than the idea is
about, exploit something the thesis never claimed. None of that breaks `strategy_integrity`
— the code is well-behaved, it answers a different question — so it is checked separately:
every `[research] align_every` cards, and whenever `kanso align check ID` asks.

The syntax tree is read first. An instrument literal outside `universe`, a bar whose step or
aggregation is not `resolution`, and a subscription or handler for a type outside
`data_requirements` are facts about the file, stated rather than judged, and they cost
nothing. Only a file that passes them is shown to a model — the `align_check` task class —
with the thesis, the file and the diff since the last check.

A drift is rewound rather than stopped: research is indefinite, and stopping it on a
judgement call would hand the model a veto. The lane and `best` go back to the newest keep of
the run that no check has marked drifted, else to the run's base; the cards since the last
check are marked not aligned, so no rewind or re-seed starts from them; the reason reaches
the run's next proposals; and a `misaligned` escalation tells you. The run carries on from
ground that was checked.

**The run's base is never judged.** A check judges what the loop proposed, and the bytes a
run was handed — the workspace `strategy.py`, or a keep an earlier run made — are not this
run's proposal. No rewind can go behind them either, so a verdict against them moves
nothing. Asked anyway, once every proposal since the last check had failed to keep and the
lane had been restored to its base, a model judged a deliberately simple reference seed
against a thesis describing what the search was for, and the run was "rewound" onto the
bytes it was already on: the cards since the check marked, the proposer told the run had
been rewound, an escalation raised, and not one byte moved. Observed in a live workspace on
2026-09-24: ten `misaligned` escalations in an hour and three quarters across four lanes,
every one rewound to its own run's base, for reasons such as "Fixed-clock baseline, not
signal-driven". A check that finds the lane on its base asks no model and raises nothing. It
is recorded as an `aligned` event marked `judged: false` — `kanso align check` reports the
same — so the next check differences from there and a reader can tell it from a verdict, and
every card keeps the mark it had: the baseline card among them, which no check has ever
marked.

Nor is the file the last check left the lane on judged again, whether the check passed it or
rewound to it. A keep moves the lane to new bytes, and every card after it that does not
keep restores the lane to that keep, so the loop is back on that file only when nothing has
kept since; its answer is on record, and a drift verdict could only rewind the lane to where
it already stands. An operator can break that by hand — card a file, then write older bytes
back into the lane — so a check that judges nothing never counts a keep whose bytes no check
has answered for: its checkpoint stops short of the keep, and the next check that asks the
model marks it. Nor does a drift ever rewind onto the bytes it found drifted: every card of
the run that carried them is marked, not only the cards since the last check.

A keep an earlier run made was that run's to judge, and a run can end before the check that
would have judged it: ended by hand, or stalled on a spell of misses, which count toward
`stall_k` and not toward `align_every`. Such a keep becomes the next run's base unjudged,
and row 90 of `docs/backlog.md` says what closing that would take.

The syntax tree still reads the base and the file the last check left the lane on. What it
finds is a fact about the bytes rather than an opinion of them, and a check that answered
`aligned` for a file that names an instrument outside the universe would assert what the
tree disproves, whoever wrote the file — so a base that fails it is reported as drift
whenever a check finds the lane on it.

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

A `warmup` widens only the lower bound of that check. The runner resolves the sessions
before the window in the parent, puts them on the request as a span, and the child admits
points from the first of them — never a point at or after the window's close, prefix or
not — while every order the strategy places before the window's first point is dropped
before it is checked or recorded. The prefix is data the strategy may see and may not act
on; the certification window stays data it may not see, and a warmed card refuses it
exactly as a cold one does.

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

The engine's history requests — `request_bars` and every other `request_*` an engine actor
holds, a book's snapshot, deltas and depth among them — are denied with the rest: history
reaches a strategy only as the `warmup` prefix its hypothesis declares, which the runner
resolves and feeds before the window, and a request would be a second route to the catalog
that no window bounds. `request_instrument` and `request_instruments` are denied with the
cache, as the other route to an instrument and its splits. The component clock is denied under
both the names the engine gives it, `clock` and `_clock`: it reads the instant the engine is
at, which under `depth` is every change of the book, where a strategy reads `data_time`. The state the harness keeps
for a `book` policy — the instant it cuts return periods from, the period it is in, the
cushion and the last end it settled — is denied the same way: it is a clock of where the
window opens and a record of what the book made before this month, and a strategy reads
the book the policy left from `balance` alone. The harness's own copy of a book a `depth`
hypothesis holds, and the methods that show the author its view of it, are denied to every
strategy for the same kind of reason: they are the book at every change, which the account
does not see. Under `depth` the message bus, which carries the data engine's own topics and
every change of the book among them, is denied under both the names the engine gives it,
`msgbus` and `_msgbus`, and so are the three subscriptions that would hand the author a book
of its own — `subscribe_order_book_deltas`, `subscribe_order_book_at_interval` and
`subscribe_order_book_depth`; the view reaches `on_order_book_deltas` and level one
`on_quote_tick`, and the refusal says so. The same denials bind an attached construct, which
is handed no view of the book at all: it reads the host's prices from the context it is
asked with, and a construct the gate did not scan under `depth` that subscribes a book anyway
is refused when it asks, which crashes the card with the call named.

A denied name is refused however the scan can see it spelled. The builtins and the
introspection that would reach one by another route are denied with it: `__getattribute__`,
which `object` and every instance carry and which reads an attribute by a name held in a
string; `__getstate__`, `__reduce__` and `__reduce_ex__`, which hand back an instance's
attributes all at once; `__builtins__`, and a builtin function's `__self__`, which is the
builtins module and so `open`; and `__traceback__` with the frame attributes, which walk up
into the harness's own frames. A denied attribute named in a format field —
`"{0.cache}".format(self)` — is refused as the attribute is.

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

**A name the base class owns is the harness's.** `KansoStrategy` keeps its machinery on
names a strategy could as well have chosen — `_close` places the sleeve's exits, `_fund`
and `_refund` book funding, `_last_price` holds the last prices, `size` scales an entry by
the attached overlays — and
Python lets a subclass bind any of them without a word. `self._close = 3` in `on_start`
replaces the exit method with an int, and the card runs until its first exit, where it
crashes inside `submit_exit` with a traceback that names the harness and not the line that
did it. So the gate refuses a class whose instances are a `KansoStrategy` or a
`KansoModifier` — one that names either among its bases, through an alias, a module or a
class of the file that does, and any class of the file such a class names, a mixin
included — when it binds a name that base owns: by `def`, by assignment in the class body,
or by assignment on the instance a method receives. The refusal names the name, the base
and the line. What the base owns is read from the installed classes whenever the gate runs,
never kept as a list: every name `dir()` shows of it, the engine's beneath it included, and
every attribute kanso's own classes set on `self`. What it leaves to the author is
`config_cls`, a modifier's `construct`, `evaluate` and `on_data`, the engine's `on_*`
handlers and the dunders. The rule is the same sized or not; under a sizing rule an
override is also a size knob no attribute scan could see. A binding made through another
object — a module-level function handed the strategy — is not one the scan follows.

```
$ kanso research card demo_mr --desc "keep the close column index on self._close"
card       38fe6bc · discard · keep the close column index on self._close
metric     0.000000 ± 0.000000 · 0 trade(s)
cost       0.0s · 0.00 GB · trial 2
best       none yet
           strategy_integrity: fail — n_problems=1, problems=["line 16: '_close' belongs to KansoStrategy, and binding 'self._close' replaces it, so the harness would reach yours where it expects its own; rename yours — KansoStrategy owns every name it defines or sets, underscored or not"]
```

The same refusal reaches an operator before any run: `kanso hyp validate` refuses a
`hypotheses/<id>/strategy.py` that binds one (exit 3), `research begin` refuses a baseline
that does with the line named, and `kanso doctor`'s `base names` check lists every
hypothesis whose file does.

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

**A book's changes of one instant are one batch.** The changes one instrument's book made at
one `ts_init` reach the venue and the sleeve as one `OrderBookDeltas`: the venue applies all
of them before it matches, and `on_order_book_deltas` is called once per instrument and
instant, with every change of it and the cache's book already past all of them. A change at
a time, as a book used to be delivered, called the author with the book half-moved — after
the old best offer's delete and before the new one's add — and the venue matched against
every state in between, so a resting buy could fill against an offer that existed only
between two changes of one instant (`kanso.nautilus.facts` measures both). A book handler is
never held, so no marker follows a batch, and under a `latency_ms` a command that comes due at
a change lands after the sleeve's handler for that change: the engine hands the batch to the
venue and the sleeve before it settles the commands due then, so the handler sees the order
sent, and the next point's handler sees it on the book — a print at the same instant, held
behind its marker, already sees it there. Both paths alike. Book changes of several instruments at one instant
are still handed over one instrument at a time: a book cohort is not a cross-section
(`docs/backlog.md`). A strategy that counted its book calls counts instants now, and a
`best` struck on a book hypothesis before this is not comparable with a card after it.

**Whether a feed is marked is the hypothesis's.** It used to be read off the points: a stream
was marked when some instant held two points of one kind. A card read its window a day at a
time and a replay read it whole, so the two could decide differently, and a card cut finer
would have depended on where it was cut. A feed is now marked whenever its universe holds
more than one name or it requires prints, quotes or a book, and any other feed when some
instant holds two points of a kind. At zero latency a lone point is dispatched the same
marked or not. Under a `latency_ms`, a command that came due by a lone print or quote now
lands before the author's handler for it, as it always did for a point that shared its
instant: a modify sent on one quote with 20 ms to travel is answered before the handler of a
quote a second later runs. On real tick data this changes nothing — measured on two OKX
swaps over 82 days, no hour went by without a millisecond two prints shared, and 88–89 % of
prints shared theirs.

A live data client that polls several series independently must emit the same
per-`(ts_init, kind)` markers. A single marker at the end of a poll that covered more
than one instant would flush the first instant after later books had already moved.

**The simulated venue is settled where the research path settles it.** The research
engine settles every venue after every point, the markers included, so under a stated
latency (`costs.latency_ms`) a command that came due by a held point lands before the
marker hands that point to the author. The node's venue sees no marker through its own
market subscription and used to land it on the next point it saw, after the flush — so a
sleeve that sold on the first print it handled while long sold a print later on the node
than on the engine, and an operator's `parity_replay` of a level-two book hypothesis under
20 ms failed the same way, on the instant of one intent, with every earlier intent agreed and
the node's instant the later. Both of kanso's simulated venues — a replay's and a stage's — now settle at each
marker, above the sleeve. A change to the book also stamps `data_time`, and so what a
sleeve sends from `on_order_book_deltas`, with its own `ts_event`, where it used to carry
the last print's, which for a sleeve that holds only the book was no instant at all; a
book intent is compared on the change it was decided on, and `kanso replay parity` holds
at a tolerance of zero for book data too.

**A limit that rests is filled by the venue's rule.** A limit order that was not marketable
when it was placed waits on the book, and a later point that reaches its price fills it at
that price, as a maker. Whether *reaching* is enough is the venue model's `limit_fill`
(`docs/workspace.md`): under `touch`, the default and the engine's own rule, a bar whose low
is a buy's price fills it; under `through` the low has to go under it, a sell's high over it,
a print past it. On a bar the venue walks the open, the high, the low and the close as prints
in turn, so a bar that only touched the level at its low fills a resting buy at the limit
under `touch` and leaves it resting under `through`; on a quote the engine asks the rule only
when the order's own side of the book is at the price, so an ask falling to a resting buy
fills it under either. Both code paths build their venue from the same configuration, so a
card and a stage fill the same resting orders, and the rule draws no random number, so they
fill them the same way every time. A print fills the order by its own size and no more, so
the honesty of a fill is the honesty of the print: an exchange's own executions, one per
print, fill a resting order as that exchange would; a consolidated or merged tape fills it
with size the book never showed it (`docs/workspace.md`, `limit_fill`).

A trade print reaches a resting order only from the side that can trade with it: the engine
moves only the ask down for a seller's print and only the bid up for a buyer's, so a
buyer's print below a resting buy never fills it, while a seller's print or one with no
aggressor does. What side a print carries is therefore a fact about the data, and a trade
file that records none is loaded with no aggressor rather than a guessed one
(`csv_parquet`); a buyer's label on those prints used to leave every buy resting under them
unfilled.

**An order that joins a level waits behind what the level showed.** A hypothesis that
requires `book` loads the exchange's level-two changes beside its prints — one `book` point
per change to one level, as a market-by-order or market-by-price file spells it, or as the
OKX package's `okx_book` derives it from the exchange's daily archives — and both
venues keep a book from them with the engine's queue position on: a resting order that
joins a displayed level is filled only after the size shown ahead of it has traded through,
print by print, so joining the touch is as honest as improving it. Measured through the
runner and on both code paths: 500 shown on the bid, an order of 300 joining it, and eight
sellers' prints of 100 a second apart fill the order at the seventh, eighth and ninth prints;
the top-of-book venue a hypothesis without `book` gets fills the same order at the second,
third and fourth, credited with what stood ahead of it — which is why a posting thesis on
that venue rests a level of its own. `kanso doctor` checks both engine facts.

**Under `depth` the strategy sees the book its account would, and the venue every change.**
Without the key a sleeve is handed each change to the book as the venue is. With it
(`docs/workspace.md`, `depth`) the harness keeps its own copy of each book — the cache's is
already past the change being handled when a handler runs — and hands the author two views of
it, from the same code on both paths. The depth view: at the first point published after
each multiple of `every_ms`, the top `levels` of each side as they stood at that instant,
as one `OrderBookDeltas` of the differences from the last view, stamped with the instant and
closed by `F_LAST`, handed only when it moved. Every change the harness holds when it does
so was published at or before the instant, because a change after it is itself a point
published after it and shows the view first. Level one: a `QuoteTick` of the best bid and
offer to `on_quote_tick` for every instant whose changes, all applied, moved the top —
handed at the change on a feed whose every instant is one change, and otherwise at the flush
marker after the instant's changes, so a top the instant passed through on its way is never
shown. Each call runs in the envelope every handler does: held cancels go out before it and
owed exits are asked for after it, and both still run once for every change the author is
not shown. An order sent from either is stamped with the instant shown and reaches the venue
`costs.latency_ms` after the point that carried the call; the data is not delayed again,
since the latency is the whole round trip. The harness keeps the book by the engine's own
level-two rules — an add or an update sets the level, a delete of a price not held does
nothing, a clear empties both sides — which `kanso doctor` checks, and a property test holds
its top levels and its best equal to the engine's `OrderBook` after any sequence of changes.

**A fill that rested can be charged as one, and a sale pays its fees whoever filled it.** Every fill pays commission, slippage and half
the spread, once, in the runner's extraction — unless the venue model states `maker_bps` and
the venue reported the fill as a maker's, in which case it pays exactly that and nothing
else, since a resting limit fills at its own price and the spread is what it earns. A
negative rate is a rebate. Each recorded fill says whether it was a maker's, so the charge
can be struck again from the record, and the harness books the same rate into
`self.balance` as it goes, which keeps the balance a sleeve sizes against equal to the
equity the runner strikes.

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
them: on the first market point stamped after the midnight that opens an ex-date where the
instrument trades (`info.timezone`, or UTC when it names none), and one call before that
point is matched against anything, the simulated exchange cancels every resting
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
are what was actually bought, `avg_close` is what came back per opening share, and a split
itself produces no profit or loss. The shares a split leaves short of a whole lot are paid
out in cash, as an issuer pays them, at the last price in the old count — the close before
the ex-date: the venue writes the amount on the split's own adjustment, the extraction
books it into cash at the split and into the trade the position closes as, and the harness
books the same amount into `balance` when the venue announces the split, so the three read one
number. A position under one new lot is paid out whole and closes at the split.

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
           pass  embargoed_window      certification=10.14987087722001, min_fraction=0.5, objective=net_edge_bps, research=9.986729903397118
           pass  publication_lag       n_datasets=1, published_too_early=[], tolerance_s=0.0, unknown=[]
           pass  parity_replay         compared=850, divergence=None, engine=20261002T225814Z-engine-6ca3f78, engine_intents=850, engine_released=40950, engine_stream=81d5de5ba8556a06e9e1c0c790c94921f7d4fdb766d8b4f817cbd2bc1c6558ca, identical=True, max_ts_delta_ns=0, node=20261002T225809Z-node-6ca3f78, node_intents=850, node_released=40950, node_stream=81d5de5ba8556a06e9e1c0c790c94921f7d4fdb766d8b4f817cbd2bc1c6558ca, ts_ns=0
           pass  cost_stress           metric_a=5.146082462397, metric_b=0.14229404757398897, mult_a=2.0, mult_b=3.0, objective=net_edge_bps
           pass  bootstrap             limit_pct=15.0, mdd_p95=0.3845075723749912, n=1000, objective=net_edge_bps, objective_ci90=[7.823304135204396, 12.39450090888979]
objective  net_edge_bps 10.149871 ± 0.603055
pins       engine 1.231.0 · plan 1 · snapshot 4592f8c0dbed3f78ec2f9278f239c5ca080abf029a69553e9c2a8212c394a062 · trial 4
written    /…/certificates/demo_mr/f729a53-heb6db7b-4-p1-e1.231.0.yaml
source     /…/certificates/demo_mr/f729a53.py
next       kanso cert show demo_mr
```

**The certificate's `objective` is measured the way a card's metric is**, and so is the
`certification` number `embargoed_window` and `walk_forward_consistency` record: the
hypothesis's objective over the certification window cut into the workspace's `[research]
folds` calendar folds, reported as the mean of the folds `±` the standard error of their
spread. It is not the mean of the window's sessions, and on a short window the two differ.
Five sessions, Monday to Friday, in four folds are four spans of a day and a quarter; a
session's period ends at its last event, and sessions that end after 18:00 UTC, as a US
equity session does, put Thursday and Friday together in the last fold, each at half the
weight of Monday, Tuesday or Wednesday. Sessions earning 10, 20, 30, 40 and 50 bp of the
capital average 30 bp; the certificate records `(10 + 20 + 30 + 45) / 4 = 26.25`. The same
bytes run in process return those five sessions exactly; the arithmetic is what differs.
Which sessions share a fold follows from the hour the periods end — a daily bar published
at 16:00 UTC puts Wednesday with Thursday instead — and the standard error is the spread of
four fold means, so two sessions that disagree inside one fold cancel there rather than
widening it. A fold that holds no period at all scores zero: Monday to Sunday in four folds
leaves the last, from Saturday morning on, empty, and the week above is certified at
`(10 + 25 + 45 + 0) / 4 = 20`. Only a window whose folds hold the same number of sessions
each is certified at its mean per session; `docs/backlog.md` row 110 says what closing the
difference would take.

Certifying the same bytes again under the same plan **and** the same engine is refused:

```
$ kanso cert run demo_mr --json
{
  "error": "demo_mr already certified f729a53 under plan version 1 and nautilus_trader 1.231.0 on 2026-09-06T01:02:36.258656+00:00; a certificate is immutable",
  "code": 2,
  "remedy": "research a better strategy, replan, or upgrade the engine"
}
```

A subject whose hypothesis declares a `benchmark` is certified against a hold of its first
leg over each window — two more runs of the one runner, from the subject's own request for
that window with the strategy replaced — and every gate that measures the objective reads the
certification hold beside the certification run and the research hold beside the research
run. `param_plateau` moves the subject's parameters and re-runs the subject alone: the hold
it is differenced against stays the one it was measured against.

`param_plateau` is defined around an edge and nowhere else. Its floor is a fraction of the
unperturbed objective, and a fraction of a loss lies above the loss: at −10 and a
`keep_fraction` of a half the floor is −5, so a perturbation that changed nothing would read
as failing to keep half of the result, and one that merely lost less would pass. An
unperturbed objective at or below zero therefore fails the gate before any parameter is
moved, with the `reason` in its evidence and `n_backtests` at zero. It fails rather than
skips because its context is whole and it is the subject that lacks the property: a plateau
around a loss is a robust loss, not evidence of robustness, and a skip is a pass. The runner
spares the same backtests once `embargoed_window` has failed. The plateau is judged after
every other cert gate, and when the window gate judged the subject and refused it, the
plateau is recorded as skipped with the reason and no perturbation is run — the gates are
evidence for a pass, and a certificate whose window failed cannot pass — while the
certificate lists its gates in the plan's order as before. A window gate that judged nothing
spares nothing. `bootstrap` judges both of the numbers it records: the ninety-fifth
percentile of the resampled drawdown against `risk_limits.max_drawdown_pct`, and the
ninety-percent band of the resampled objective, which must reach above zero — a band that
lies at or below zero says the population of trades carries no edge in whatever order it
arrives, and a drawdown inside the limit does not make that a pass.

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

Three gates read what the others cannot. `deflated_contribution` is the multiple-testing
control for a run scored on a contribution, where `deflated_sharpe` skips: the research
estimate in basis points per period, against the expected maximum of the search's trials in
the same units, over the estimate's own standard error, reported as a probability and held
to `min_probability`. The trials' spread is read robustly, as 1.4826 times the median absolute
deviation of their metrics, so a few trials that blew up — a rule that traded itself to ruin, a
seed that fired once — do not widen the search past any candidate the selection weighed; the
spread it used is in the evidence as `trial_spread_bps`. `min_event_days` is a card gate counting the distinct sessions any
fill fell on, for a rule that fires on a regime or an event and could put its whole sample
into a handful of days that `min_trades` would count as many. `cost_scenario` re-prices the
recorded fills under another cost model stated key for key as `costs:` is — a per-share
commission, a flat rate, a maker rate, a fixed width — through the runner's own per-fill
arithmetic on each fill's recorded notional, quantity, price and multiplier, recomputes the
objective on the re-priced run and holds it to `min_metric`: the same fills under the
schedule of another account, without a second backtest, and the card's own schedule
reproduces the card's own costs on any instrument. Neither gate touches a perpetual's
funding, which is a payment on what was held rather than a cost of trading it.

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
  criteria version, the plan version, the data snapshot and the resolved venue model, each
  field of which records its origin as `default`, `config` (`[research]` in `kanso.toml`),
  `broker`, `venue_override` (`venues.<MIC>` in `portfolio.yaml`) or `hypothesis`.
- **`expectation`**: what composition measured by running that implementation over the
  sleeve's certification window — the objective, a ninety-percent interval and the
  ninety-fifth-percentile drawdown. The paper and live gates judge the deployment against
  this band, so it is measured rather than declared. A sleeve measured against a
  `benchmark` has the hold run over the same window, and its value and its band are
  differences from it.

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

**The stage venue is the card's venue.** A simulated stage builds its exchange from the
same configuration a card built its own from, by the same function, from the venue model
each version was certified under: the same latency (`costs.latency_ms`), the same book type
and queue position (a level-two book for a hypothesis that requires `book`, the top of the
book for every other) and the same fill model (`limit_fill`), with the fee model left
unset as on a card, so the exchange charges the instruments' zero rates and the runner
charges once. A version certified under a 20 ms round trip on a level-two book therefore
trades the stage under 20 ms on a level-two book, and two versions on one venue that were
certified under different latencies, or one on a book and one without, are refused at
`deploy` (exit 2) before the stage is written, and at `promote` before an approval is
recorded when the live stage could not hold the version, rather than run on whichever
venue came first.

**A stage node runs in the process that deploys it** — yours, under
`kanso portfolio deploy`, `promote`, `demote` and `strat retire` — **except a demotion the
monitor makes**, which runs in a child of the monitor (below), because the monitor runs all
day and a process that runs all day never gives back what a node allocated in it.

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

A stage node restarts flat, and a version whose hypothesis declares a `warmup` re-warms on
every restart: the sessions before the window on the first, and on a restart the sessions
at or before the stage's clock — the data it already replayed, fed again with every order
dropped. What the session records released, and the clock the next restart resumes from,
are the points after that instant, so a restart with nothing but its prefix to replay is
idle and the clock stands. Two versions on one stage that subscribe one series are fed it
once, cut at the deeper warmup of the two, and each is handed only the span its own
request delivers: a version without a `warmup` beside a warmed one sees nothing of the
prefix, a shallower warmup sees nothing of a deeper one's, and every version's handlers,
indicators and orders are what a run of it alone would produce. A version under a `book`
policy is seeded on a restart with the cushion and the last settled end of its newest
measured window on that stage, and its first carry runs from the instant it resumes.

A version whose sleeve declares a `benchmark` has the hold of its first leg run beside the
stage rather than inside it: once the node stops, the backtest runner runs the hold over the
points that version's realised window is measured on, from the version's request with the
strategy replaced, and the hold is recorded on the same `stage_run` event as the window the
version realised. The two have the same periods, a halted window's included: the version is
marked over its whole view after a halt, and so is its hold. The paper and live gates difference the realised objective against the
holds of the same windows, joined as the windows are; a book recorded without its hold is
skipped with the reason rather than judged against nothing. A separate engine, because two
strategies in one account would share the book and the volume a fill walks.

## Promotion and demotion

**Promotion is the one thing kanso will not do by itself.** Everything else on this page
happens without a person: classification, research, certification, composition, paper
deployment, demotion, halting a stage. Moving a version onto real capital does not.

What moves a paper version to `promotable` is one gate, `paper_forward`, and it is strict
both ways: the version must have been on the stage for the longer of the plan's minimum
duration and its horizon multiple — a shorter window is a `fail`, not a skip — and the
objective it realised must fall **inside** the ninety-percent interval composition
measured, above the band as much a fail as below it, because a stage that out-performs its
certification is not reproducing what was certified. Of the sleeve's card-stage constraints it
judges `max_drawdown` and `maintenance_margin` on the stage's own run, and a breach of either
is a fail. `docs/cli.md` has the pass.

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

**The monitor demotes in a child of its own process.** Each redeploy builds a trading node
and replays everything its stage has not replayed — a stage's clock moves only when it is
deployed, so that is everything loaded since its last deploy — beside the hold a benchmark
objective differences against, and the monitor is a process that runs all day: what a node
allocated in it would never be given back. So a pass makes the demotion in a child it
watches, the same `kanso demote` makes in yours, and keeps only the child's report: the
strategy file, the stages, the sessions, the stage records and every event are written by
the child, and are the ones the monitor would have written, window for window and intent for
intent. Measured in a fresh monitor process on the suite's synthetic saw-tooth, a pass that
demoted a live version and redeployed paper over a month of new daily bars raised the
monitor's own peak by 35.2 MB when it demoted itself — 40.4 MB over nine months — and by 1.0
to 1.1 MB in a child, as much as a pass that only judges moves it (0 to 1.1 MB). Nothing
bounds the child: a demotion takes a failing version off real capital, so no memory share
and no wall time may refuse one, and a `research stop` that lands while it runs leaves it to
finish, as nothing stopped a demotion the monitor made itself. A refusal in the child
reaches the pass as the same refusal, recorded against the version as any other.

There is one exception to demotion, and it is the stronger act rather than a weaker one: a
live version that breaches the stage's daily loss limit **halts the stage** instead of being
demoted, since demoting into a halted stage would change nothing about the money. A halted
stage stays halted until the operator clears the switch — a redeploy that cleared it by
starting a node would make the switch advisory.

## Escalation

kanso escalates six things and nothing else: `misaligned`, `cert_failed`, `promotable`,
`demoted`, `deploy_blocked`, `explored`. Each entry names its subject and the commands that
kind offers over it. An `explored` entry's subject is a hypothesis kanso wrote and did not
register — `hypotheses/<id>/` as a draft — and it offers `hyp validate` and `hyp add` and
nothing further: whether a model's idea deserves a lane is yours to say.

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
