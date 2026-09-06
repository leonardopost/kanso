# Constructs

A **construct** is what a hypothesis *is* in portfolio-construction terms: a whole strategy,
a rule that gates another strategy's entries, an exposure layered over one, an exit rule, a
return forecast, an execution tactic, an allocation rule. `kanso classify` assigns exactly
one, and that assignment decides what the hypothesis attaches to, what its objective
measures, and what class the lane's `strategy.py` has to define. `docs/concepts.md` covers
the surrounding vocabulary; this page is the catalogue.

**The catalogue is the domain, not a menu of what this version implements.** Three of the
seven constructs below cannot be run in 0.1.0, and they are in the catalogue anyway. A
taxonomy that names only what is built teaches an operator to misclassify their own idea —
a forecast that does not trade by itself gets written up as a sleeve, and the thing that
gets measured is the wrapper somebody improvised around it rather than the forecast. So
classification accepts a construct this build cannot run, records it honestly, and the
refusal waits until a run is begun. It is late on purpose.

Each construct is a YAML file in `src/kanso/classify/constructs/` naming an `impl` module,
and the loader refuses an implementation whose declarations disagree with the file — so the
description the classifier reads and the object the runner uses cannot drift apart. A
workspace extension adds constructs through the same item shape; `docs/extensions.md`.

*Every transcript below is real output from a workspace made by `kanso init --demo`, with
hypotheses written for these examples against its shipped `demo_mr` sleeve. Where a refusal
arrives inside a longer report — a card's own summary, or a traceback — the parts that are
not the refusal are elided as `…`.*

## The catalogue

| id | what it is | attaches to | objective | 0.1.0 |
|---|---|---|---|---|
| `sleeve` | A self-contained strategy — signal, entries, exits, sizing — with its own book and P&L attribution. Becomes a new strategy in the portfolio. | nothing | absolute | **runnable** |
| `filter` | A conditioning rule that gates a host's entries: regime, trend, volatility, calendar or liquidity conditions (`scope: time`), or which instruments the host may trade (`scope: instrument`). | a sleeve | relative | **runnable** |
| `overlay` | An exposure modification layered on a host without changing its signal: scaling (volatility targeting, drawdown control) and/or hedge legs (beta, tail, currency, delta). | a sleeve, or the portfolio | relative | **runnable** on a sleeve; the portfolio host is a seam |
| `exit` | Exit logic added to or replacing a host's: stops, targets, time exits, trailing rules. | a sleeve | relative | **runnable** |
| `alpha` | A return forecast that does not trade by itself; combined with other alphas inside an alpha-combining sleeve. | a sleeve | absolute | classification only |
| `execution` | How a host's orders are worked: order types, passive or aggressive tactics, slicing — implementation shortfall. | a sleeve | relative | classification only |
| `allocation` | Capital or risk allocation across sleeves: risk parity, regime switching, a strategy of strategies. | the portfolio | relative | classification only |

Three declarations carry all of that, and each one refuses something.

- **`needs_host`** — `none`, `sleeve` or `portfolio`. A hypothesis whose construct needs a
  host and names none is refused at validation, and so is one that names a host the
  workspace has not certified and composed. A sleeve that names a host is refused too: it
  attaches to nothing.
- **`objective_mode`** — `absolute` or `relative`. This chooses the objective, together with
  the horizon; an objective that does not apply is refused by name, with the applicable ones
  listed.
- **`runnable`** — whether this build can execute it. `false` means classifiable and
  nothing more.

A construct may also declare **`params`**, a fixed set of names with a fixed set of values
each. Only `filter` declares any: `scope`, one of `time` or `instrument`. A parameter the
construct does not declare, or a value outside its set, is refused by `hyp validate` and
`hyp add` (exit 3). The check is the construct's own — the same call the runner makes when it
builds the harness — so a classification validation admits is one a run accepts:

```
$ kanso hyp validate hypotheses/demo_f2/hypothesis.yaml
error: construct.params.scope: 'sideways' is not one of time, instrument
```

## The four runnable constructs

### `sleeve`

A strategy of its own. It attaches to nothing, its objective is measured on its own run, and
the lane's `strategy.py` defines a class `Strategy` subclassing
`kanso.nautilus.strategy.KansoStrategy` with a `config_cls` beside it. That config's own
numeric fields — its ints and floats, not its booleans and not the capital and risk limits
the framework injects — are exactly what the `param_plateau` certification gate perturbs, so
what you make a config field is what you are claiming a plateau for.

Composition makes a certified sleeve **version 1 of a new strategy** in the portfolio. It is
the only construct that creates a strategy; every other runnable one adds a version to a
strategy a sleeve already made.

### `filter`, `overlay` and `exit`

The three that attach to a certified sleeve. All three share one shape: the lane's
`strategy.py` defines a class `Modifier` subclassing
`kanso.nautilus.strategy.KansoModifier`, tagged with its construct id, whose `evaluate(ctx)`
returns a `Decision`. A modifier is an engine actor, not a strategy, so its config subclasses
`KansoModifierConfig`.

Each construct owns exactly one part of a `Decision`, and the host reads that part through
one hook:

| construct | sets | the host hook that consults it | what it may do |
|---|---|---|---|
| `filter` | `allow: bool` | `before_entry` | withhold an entry the host's signal asked for. It never changes the signal |
| `overlay` | `scale: float ∈ [0, 1]`, `hedges: [Hedge]` | `size`, `hedges` | resize what the signal already asked for, and add legs beside it |
| `exit` | `exit: bool` | `before_exit` | close an open position, with the last word over the host's own exit logic |

`Decision.neutral(construct)` is the identity for that part — the answer that leaves the
host exactly as it was. It is what the shipped stub returns, which is why the baseline card
of an attached construct scores zero: the modifier changed nothing, and a relative objective
differences the combined run against the host's own.

Several modifiers may be attached to one sleeve, and they compose without conferring: an
entry needs **every** filter to allow it, scales multiply, hedge legs are the union, and
**any** exit rule closes the position. An unset field is silence, not a "no".

**A modifier that answers a question it was not asked is refused.** A filter that also
returns a `scale` is a modifier wired to the wrong construct, and letting it through would
change the host in a way the construct's objective never measures:

```
$ kanso research card demo_filter --desc "a filter that also scales"
…
kanso.errors.ValidationError: decision: a filter construct answers allow and set scale as well
```

Two more refusals worth meeting before you meet them by accident.

**The entrypoint class is named, not discovered.** A sleeve card loads `Strategy`; a
modifier card loads `Modifier`. Rename it and the card crashes rather than running something
else that happens to be in the file:

```
$ kanso research card demo_filter --desc "rename the entrypoint class"
…
kanso.errors.ValidationError: strategy.py: defines no class Modifier subclassing KansoModifier; a modifier is run by loading Modifier from the file
```

Both of those are recorded as `crash` cards: `research card` exits 0, the lane's
`strategy.py` is restored from `best`, and the traceback tail is what the card carries. A
strategy that cannot be loaded is a failed experiment, not a failed command.

**A construct's `params` are handed to the modifier's config as keyword arguments.** A
`filter` classified with `scope: time` is constructed as `Config(scope="time", …)`, so a
config that does not declare `scope` is a config that cannot receive the classification. It
is refused when the harness is built, which for a fresh run is inside the baseline card:

```
$ kanso research begin demo_filter
error: the baseline card of demo_filter did not run: exception: Traceback (most recent call last):
…
kanso.errors.ValidationError: construct.params: Config does not take these parameters: Unexpected keyword argument 'scope'
remedy: fix hypotheses/demo_filter/strategy.py and begin again
```

The alternative — accepting the config and dropping the parameter — is a hypothesis
classified `scope: instrument` running a time filter, with nothing anywhere saying so.

**The host is pinned by version, never by "latest".** Every card of a run differences
against the same host version, so a host that gains a version mid-run does not silently
change what the run is measuring. Composition then appends this construct to that host's
version n+1.

## The three that classify but do not run

Each declares the seam that would make it runnable and refuses every entry point with it,
so an operator reads what is missing rather than a stack trace. The one exception is a
construct hosted on the portfolio, where an earlier refusal about the missing host arrives
first — see `allocation` below.

### `alpha`

A return forecast — the thing that says *what will happen*, with no statement about entries,
sizing or exits. It cannot be scored on its own P&L because it has none, so it needs two
pieces neither of which exists here: **a canonical wrapper** that turns a forecast into a
strategy measurable on an absolute objective, and **a combination rule** for holding several
forecasts inside one alpha-combining sleeve. Both are decisions about what a forecast is
worth, and shipping an arbitrary one would make every `alpha` result a statement about the
wrapper.

Classification takes it; `research begin` does not:

```
$ kanso research begin demo_alpha
error: alpha is classifiable but not runnable in this version; the seam is the canonical wrapper that turns a forecast into a measurable strategy, and the rule that combines forecasts inside an alpha-combining sleeve
remedy: classify onto a runnable construct, or implement the alpha seam
```

That is exit 2 — a precondition of the workspace, not a malformed file.

### `execution`

How a host's orders are actually worked: passive versus aggressive, slicing, order types —
implementation shortfall. The seam is NautilusTrader's own execution algorithms driving the
host's orders, scored by the net P&L delta that fill quality produces. The construct
interface is not what is missing; the wiring is — nothing yet lets a modifier hand the
host's orders to an execution algorithm instead of letting them be submitted.

```
$ kanso research begin demo_exec
error: execution is classifiable but not runnable in this version; the seam is NautilusTrader execution algorithms driving the host's orders, scored by the net P&L delta that fill quality produces
remedy: classify onto a runnable construct, or implement the execution seam
```

Worth knowing before you plan on it: the same subject has an open measurement gap on the
deployed side. A fill recorded in a run carries the cost kanso's extraction charged, not the
price the order gave up against a reference, which is why the `fill_quality_drift` live gate
skips on every stage today. `docs/backlog.md` entry 22 lists what closes it.

### `allocation`

Capital or risk allocation across the deployed sleeves — risk parity, regime switching, a
strategy of strategies. It hosts on the **portfolio** rather than on one sleeve, and that is
what makes it a seam: a relative objective needs a host run to difference against, and there
is no portfolio-level host run in this build.

What an operator actually sees today is not the seam message. `research begin` resolves the
host before it builds the harness, and there is no composed strategy called `portfolio`:

```
$ kanso research begin demo_alloc
error: construct.host: 'portfolio' is not a composed strategy of this workspace
remedy: certify and compose the host sleeve before researching against it
```

**And there can never be one.** A certified sleeve composes a strategy named after its
hypothesis, so a hypothesis called `portfolio` would make `host: portfolio` mean two things —
that strategy, and the book — and a construct reads it as the book. The word is therefore not
an id a hypothesis may take, which is checked where the hypothesis enters the workspace
rather than at the seam where the two meanings would collide (exit 3):

```
$ kanso hyp add hypotheses/portfolio/hypothesis.yaml
error: id: 'portfolio' is reserved: it is how a construct attached to the book names its host, and a certified sleeve of that name would compose a strategy nothing could tell from the book
remedy: choose another id, and rename hypotheses/portfolio/ to match
```

It costs an operator one word out of `^[a-z0-9_]{3,40}$` and keeps `construct.host`
unambiguous. `kanso hyp new portfolio` still scaffolds the directory — the word is reserved
at validation, so it is `hyp add` that says no, once the scaffolded file has been filled in.

Also a seam: an **`overlay` whose host is the portfolio** rather than one sleeve — allocating
exposure across the book needs the same portfolio-level host run. An overlay is declared to
attach to a sleeve, so writing `host: portfolio` on one is refused earlier still, at
registration, with exit 3:

```
$ kanso hyp add hypotheses/demo_ov/hypothesis.yaml
error: construct.host: 'portfolio' is not a certified strategy of this workspace; a overlay attaches to one
remedy: certify and compose the host sleeve first, or name another host
```

## Objectives

Which objective a construct is scored on is the one deterministic domain rule in kanso: it
follows from the construct's `objective_mode` and the hypothesis's horizon, and no model
chooses it. The four are total over that grid.

| objective | mode | horizon | measures |
|---|---|---|---|
| `net_edge_bps` | absolute | under 1d | fold-wise mean net P&L per trade, in basis points |
| `wf_sharpe_net` | absolute | 1d or more | fold-wise annualised Sharpe of net returns, zero risk-free rate |
| `marginal_net_edge_bps` | relative | under 1d | what the construct adds to its host's edge per trade, differenced fold by fold |
| `marginal_wf_sharpe` | relative | 1d or more | the same difference, on the walk-forward Sharpe |

A sub-daily holding period gives few return periods and many trades, so the edge per trade
is the estimate with a sample behind it; a day or longer gives enough return periods for a
Sharpe to mean something. Naming an objective the grid does not reach is refused at
registration, with the applicable ones listed:

```
$ kanso hyp add hypotheses/demo_alloc/hypothesis.yaml
error: objective.id: 'net_edge_bps' does not apply to this hypothesis; the applicable relative objectives are marginal_net_edge_bps
```

The relative objectives are **differenced fold by fold**, not as a difference of two
summaries, so the standard error an improvement has to clear is the paired one. That is what
makes the keep rule's noise floor mean anything for an attached construct: the host's own
variation is subtracted out before the spread is measured.

## Adding one

`docs/extensions.md` has the mechanics and a worked example: a catalogue item plus a module
exposing `CONSTRUCT`, with `Attached` or `Sleeve` as the base and a `consults` mapping doing
the work.

One base it does not cover is `Seam`, which is what the three above use: give it a `seam`
string and every entry point refuses with that string. Shipping one is a legitimate thing to
do rather than an admission — a construct an operator can classify onto and cannot yet run
is more useful than a taxonomy that pretends the idea does not exist, provided the refusal
says what is missing.

An extension declaring an id this package already defines is reported and the built-in wins,
because an operator package must not silently redefine what a sleeve is.
