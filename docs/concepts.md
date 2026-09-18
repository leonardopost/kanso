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
run could use.

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
file, the snapshot, the criteria — and a run under new pins starts with none. The number
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
| `strategy_integrity` | a file that reads what the embargo hides, or imports outside the allow-list |
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
compliant strategy and pass the drifting one.

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
whole, what each order closes freeing room for the next, and a bracket's exits are not asked. On a pair's
ex-date the venue restates the held leg at the day's first point, which may be the other leg's;
until the held leg prints again its last price is restated by the split's ratio, for the balance
and for the room. `position_size` still judges a position against the capital
(`docs/backlog.md`), and under a `sizing` rule the budget is funded by definition (row 76).
`self.balance` reads the number.

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
drawdown from the capital. And `cost_stress` multiplies fill costs and leaves the carry alone,
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
its `mdd_p95` understates the drawdown a levered book recorded. And the attribute reads of
the harness's period close are denied, but an unsized `strategy.py` defining a method of the
same name is not refused: it moves `balance`, never the arithmetic the card is struck with.

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

The engine's history requests — `request_bars` and its quote, trade and custom siblings —
are denied with the rest: history reaches a strategy only as the `warmup` prefix its
hypothesis declares, which the runner resolves and feeds before the window, and a request
would be a second route to the catalog that no window bounds. The state the harness keeps
for a `book` policy — the instant it cuts return periods from, the period it is in, the
cushion and the last end it settled — is denied the same way: it is a clock of where the
window opens and a record of what the book made before this month, and a strategy reads
the book the policy left from `balance` alone.

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

A subject whose hypothesis declares a `benchmark` is certified against a hold of its first
leg over each window — two more runs of the one runner, from the subject's own request for
that window with the strategy replaced — and every gate that measures the objective reads the
certification hold beside the certification run and the research hold beside the research
run. `param_plateau` moves the subject's parameters and re-runs the subject alone: the hold
it is differenced against stays the one it was measured against.

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
