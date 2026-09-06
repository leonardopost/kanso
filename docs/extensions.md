# Extensions

An **extension** is code a workspace carries of its own, reaching kanso through the same
interfaces the package's own components reach it through. It can provide a loader, a custom
data type, a construct, an execution client or a whole data adapter, and once it loads there
is nothing second-class about what it provided: the registry that hands out `synthetic`
hands out your loader, the catalogue that holds `sleeve` holds your construct, and the
refusals that apply to one apply to the other.

Extensions live under the directories `[extensions] paths` names in `kanso.toml`, which is
`kanso_ext` by default. No scaffold creates that directory; make it when you have something
to put in it.

```toml
[extensions]
paths = ["kanso_ext"]
```

Two rules hold for every kind and are worth stating before any particular one.

**A packaged id always wins.** Every registry starts from what ships and adds what an
extension declares under an id nothing has taken, so an extension can add to kanso and
cannot replace part of it. The reason is the fixture: the loaders, constructs and gates the
test suite and the demo run against must be the package's own, or a green suite would say
nothing about the package. A clash is not resolved, it is reported: `kanso ext show` marks
the id `shadowed`, and the packaged definition is what the workspace uses. `kanso doctor`
grades a workspace `warn` for an id that shadows a **packaged** one, in every kind a
declaration may carry — the framework's own `sandbox` client included. `ext show` compares
against what this workspace's registries actually hand out, so it also marks an id that
shadows a loader a packaged *adapter* provides (`massive_bars` and its siblings), and it is
the only one of the two that says why an id is `absent`.

**An extension that fails to import degrades the workspace; it does not stop it.** Extension
code is operator code, so an import failure is expected to happen sometimes. Discovery
records it and raises nothing, every other extension still loads, and every command still
runs. That is what makes an extension safe to keep in a workspace a daemon is working in.
It is also the reason `kanso ext show` exists: a thing that never fails loudly can be absent
without anything telling you.

## What discovery does

Every **package** — a directory with an `__init__.py` — and every **single-module file** —
`something.py` — directly under a configured directory is imported. Paths are visited in
configuration order and their children in name order, so the result is the same on every
host. A configured path that does not exist is passed over rather than refused; so is a name
beginning with `_`, a name that is not a Python identifier, a directory with no
`__init__.py`, and any file that is not `.py`.

An extension is imported by its bare name with its directory on the import path for the
duration of the import. So a package may import its own submodules by name
(`from house_overlay.overlay import CONSTRUCT`), and a name already taken by something
importable is refused rather than silently shadowing it:

```
$ kanso ext show
paths      kanso_ext
json            failed · kanso_ext/json
                the name 'json' was already taken by /…/python3.12/json/__init__.py
0/1 loaded · 0 registered · 0 shadowed · 0 absent
```

Nothing about an extension is lazy. Every configured directory is walked and every extension
imported whenever a command consults a registry — `doctor`, `ext show`, `data adapters`,
`portfolio clients`, and everything that resolves a loader, a construct, a data type or an
execution client. (`status`, `inbox`, `hyp show`, `data show`, `portfolio show` and
`research status` import nothing, because they read the workspace and no registry.) Work in
a module body is work each of the first group pays for every time it runs, so build in a
factory or on first use rather than at import.

## The declaration

An extension says what it registers in a module-level `PROVIDES`, a table of kind to ids:

```python
PROVIDES = {"loaders": ["house_bars"], "data_types": ["house_signal"]}
```

Five kinds are accepted. Each is registered by one module attribute, read by the registry
for that kind:

| kind | where the registry reads it |
|---|---|
| `loaders` | `LOADERS`, a mapping of id to a `Loader` |
| `adapters` | `ADAPTERS`, a mapping of id to an `Adapter` |
| `constructs` | `CONSTRUCTS`, a sequence of `ConstructItem` |
| `exec_clients` | `EXEC_CLIENTS`, a sequence of `ExecutionClientSpec` |
| `data_types` | a `register_custom_type` call made while the module is imported |

Two kinds are **refused**: `gates` and `objectives`. No registry reads either, so declaring
one is refused where it is written rather than collected and forgotten — the section below
says where a gate goes instead.

A kind outside those five, or an id list written as a bare string, is reported as an
unusable declaration and the rest of the table is still read. Neither that nor a refused
kind is free, though: the construct catalogue and the execution client registry take nothing
at all from an extension whose declaration did not read, where the loader, adapter and
data-type registries take what they can. So one bad kind costs an extension its constructs
and its clients and leaves its loaders alone, and `kanso ext show` blames the declaration
rather than the table, so that the thing you go and check is the thing that is wrong:

```
$ kanso ext show
paths      kanso_ext
typo            loaded · kanso_ext/typo
                PROVIDES has unusable kinds: widgets
                constructs   typo_construct      absent · the declaration above did not read, and this registry skips such an extension
                exec_clients typo_exec           absent · the declaration above did not read, and this registry skips such an extension
                loaders      typo_loader         registered
1/1 loaded · 1 registered · 0 shadowed · 2 absent
```

**Declare every id, whether or not its registry insists on it.** `loaders` and `adapters`
take only declared ids: an entry in `LOADERS` that `PROVIDES` does not name is registered
nowhere, and that is the single most common way an extension appears to do nothing.
`constructs` and `exec_clients` are read from their tables whether the declaration mentions
them or not, so an undeclared one *works* — and is reported by nothing, because
`kanso ext show` lists what is declared and the shadow check compares what is declared. An
extension whose declaration is complete is an extension whose absence is diagnosable.

## `kanso ext show`

The declaration is what an extension claims. This is what each registry did with it:

```
$ kanso ext show
paths      kanso_ext
house_bars      loaded · kanso_ext/house_bars.py
                loaders      house_bars          registered
house_broker    loaded · kanso_ext/house_broker.py
                exec_clients house_paper         registered
house_overlay   loaded · kanso_ext/house_overlay
                constructs   vol_target          registered
house_signal    loaded · kanso_ext/house_signal.py
                data_types   house_signal        registered
house_vendor    loaded · kanso_ext/house_vendor.py
                adapters     house               registered
5/5 loaded · 5 registered · 0 shadowed · 0 absent
```

Three states, and each is a fact about the registry for that kind rather than a claim about
where the id came from:

| state | what it means |
|---|---|
| `registered` | this workspace's registry for that kind hands the id out. The command that names it will find it |
| `shadowed` | the registry hands the id out, and what it hands out is the packaged one. Yours is registered nowhere |
| `absent` | nothing hands the id out, and the reason follows it |

The reason is the point of the command. `the module's LOADERS table yields no loader under
that id` is a forgotten table or a class that does not satisfy the protocol, and
`nothing registered it` on a data type is a missing `register_custom_type` call.

`--json` prints the same thing as one object, with `paths`, an `extensions` array carrying
each extension's `loaded`, `error` and `provides`, a `counts` summary, and `notes`.

**It exits 0 whatever it finds**, including a broken extension. `doctor` grades that a
warning rather than a failure, because a workspace with a broken extension still works;
a second grader of one fact would eventually disagree with the first.

`notes` carries what nothing else prints: an extension's catalogue item that does not
validate is recorded by the construct catalogue and raised by no one, so without this it
would stay invisible until a classification asked for a construct that was not there.

```
           misfiled: impl: String should match pattern '^[a-z_][a-z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$'
```

The other thing it carries is the catalogue failing outright. Building the catalogue imports
every declared implementation, and one that raises something other than a kanso error takes
the whole of it down — `kanso classify` and `kanso hyp validate` both fail there. Every
construct really is absent then, so that is what is reported, with the cause named:

```
boom            loaded · kanso_ext/boom
                constructs   boomer              absent · the construct catalogue could not be built at all; the note below says why
           the construct catalogue: RuntimeError: the house model server is not running
```

## A gate or an objective goes in the package

`PROVIDES` refuses `gates` and `objectives`. Certification plans from, and judges by, the
toolbox in the package: one YAML file per item under `src/kanso/criteria/library/`, naming
the implementation it resolves, and nothing that builds that toolbox takes a workspace. A
declaration is therefore refused where it is written:

```
$ kanso ext show
paths      kanso_ext
house_rules     loaded · kanso_ext/house_rules.py
                PROVIDES declares gates, objectives, which a workspace cannot provide: the toolbox a plan is drawn from and judged by is the package's own library, so a gate or an objective is written in the package (docs/extensions.md)
1/1 loaded · 0 registered · 0 shadowed · 0 absent
```

The module still imported; what it said about itself did not read, so its constructs and
its execution clients are skipped with it. Collecting the declaration instead would buy a
green `doctor` and a loaded extension, and then a hypothesis refused a command later for an
id nothing holds:

```
$ kanso hyp validate hypotheses/house_mr/hypothesis.yaml
error: constraints.min_holding_period: is not a gate in the toolbox
```

So a gate or an objective is written against `kanso.criteria`'s `Gate` and `Objective` and
lands **in** the package rather than in the workspace. Prototyping one under `kanso_ext/` is
still worth doing — a gate is a function of a `GateContext` and can be exercised against a
real `CardRun` with nothing else running — but declare nothing for it, and the workspace
will judge nothing with it until it is upstream.

Two files make one item, and both go in the package. The YAML **is** the catalogue: nothing
enumerates gates, so the file is the declaration the classifier and the planner read, and
`ranges` is what they must choose inside. It is `src/kanso/criteria/library/<id>.yaml`, and
the shipped `min_trades` is the shape:

```yaml
id: min_trades
kind: gate                # gate | objective
stage: card               # card | cert | paper | live
required: false           # true makes it a structural invariant every plan must carry
meaningful_when: "Informative once the window is long enough to hold the trades asked for; …"
params: {min: int}
ranges: {min: [1, 10000]}
impl: kanso.criteria.gates.min_trades
```

`impl` is a dotted path to the object itself, not to a module, and its `id` is checked
against the file's. A gate evaluates a `GateContext` and returns a verdict with the numbers
it decided on, or a skip saying what it could not judge — never a `False` it cannot support,
because a `False` decides something (a card discarded, a live version demoted) and a skip
decides nothing:

```python
# src/kanso/criteria/gates/__init__.py
class _MinTrades:
    """Enough closed trades overall, and at least one in every fold."""

    id: ClassVar[str] = "min_trades"

    def evaluate(self, ctx: GateContext) -> GateResult:
        minimum = count(ctx, "min")
        if minimum is None:
            return skipped(self.id, "no minimum was chosen, so no count was required")
        per_fold = [len(fold.trades) for fold in ctx.run.folds(ctx.research_folds)]
        total = len(ctx.run.trades)
        return verdict(
            self.id,
            total >= minimum and all(n >= 1 for n in per_fold),
            {"n_trades": total, "min": minimum, "trades_per_fold": per_fold},
        )
```

An objective's YAML carries `applies` and `priority` instead of `stage` and `required` —
which hypotheses it is meaningful for, and which wins when several are — and its
implementation returns `compute(run, folds) -> (metric, standard error)`. The standard error
is not decoration: the keep rule compares a candidate against `max(min_delta, k_se × se)`,
so an objective that returns a confident-looking zero there makes every card a keep.

## Writing one

Each of these is the whole declaration for its kind. What the interface behind it must
honour is stated where that interface is documented: the command line in `docs/cli.md`, the
adapter contracts in `docs/adapters.md`, the vocabulary in `docs/concepts.md`.

### A loader

A loader is **stateless**: two calls with the same spec produce the same datasets and two
calls with the same ref and window produce the same points. That is what makes a snapshot
reproducible, so it is a requirement rather than a convention. Every point it yields carries
`ts_init` — when the information became public — at or after its `ts_event`, and the catalog
refuses one that does not. The manifest reports the span the source **served**, never the
one that was asked for.

```python
# kanso_ext/house_bars.py
from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from typing import ClassVar

from kanso.data.loader import DatasetRef, Manifest, manifest_for

PROVIDES = {"loaders": ["house_bars"]}


class HouseBars:
    id: ClassVar[str] = "house_bars"

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """The datasets this spec names, one ref each, in a stable order."""
        ...

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The dataset's points over the window, non-decreasing in ts_init."""
        ...

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as Arrow batches, or None. The catalog prefers this path."""
        ...

    def manifest(self, ref: DatasetRef) -> Manifest:
        return manifest_for(ref, self.id, self.load(ref, ref.span))


LOADERS = {"house_bars": HouseBars()}
```

A spec then names it like any other: `kanso data load --loader house_bars --spec house.yaml`.

### A custom data type

Three market-data types are built in — `bar`, `quote` and `trade` — and everything else is
registered under an id. Registration is a **process-wide** fact, not a workspace one,
because the engine keys its serialisable types by class name: a second class claiming a
taken id is refused rather than shadowing the first, and that refusal surfaces as the
extension failing to import.

```python
# kanso_ext/house_signal.py
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass

from kanso.data.types import register_custom_type

PROVIDES = {"data_types": ["house_signal"]}


@customdataclass
class HouseSignal(Data):
    value: float = 0.0


register_custom_type("house_signal", HouseSignal)
```

`@customdataclass` performs the Arrow registration the catalog needs; a `Data` subclass
built any other way passes its `pyarrow.Schema` as the third argument. **Do not write
`from __future__ import annotations` in that module** — the decorator reads the annotations
at class creation and a postponed one is a string it cannot resolve.

From that moment the id is admissible in a hypothesis:

```
$ kanso hyp validate hypotheses/house_mr/hypothesis.yaml
valid      house_mr · hypotheses/house_mr/hypothesis.yaml
universe   DEMO.SIM
grain      1m · bar, house_signal
```

and without it, refused by name — which is the message that sends you here:

```
error: data_requirements: house_signal is not a data type this workspace knows; it knows bar, corporate_action, financial_statement, quote, trade
remedy: require one of those, or install the extension that registers the type
```

### A construct

A construct is two things: the **catalogue item** classification reads, and the
**implementation** it names. The item's `impl` is a dotted module path exposing `CONSTRUCT`,
and the two are checked against each other — an item and an implementation that disagree
about `needs_host`, `objective_mode`, `runnable` or `params` are refused rather than
reconciled.

```python
# kanso_ext/house_overlay/__init__.py
from kanso.schemas import ConstructItem

PROVIDES = {"constructs": ["vol_target"]}

CONSTRUCTS = [
    ConstructItem(
        id="vol_target",
        description="Scales a host sleeve's orders towards a target portfolio volatility.",
        needs_host="sleeve",
        objective_mode="relative",
        params={"target": ["low", "high"]},
        runnable=True,
        impl="house_overlay.overlay",
    )
]
```

```python
# kanso_ext/house_overlay/overlay.py
from collections.abc import Mapping
from typing import ClassVar, Final

from kanso.classify.construct import Attached, Construct


class VolTarget(Attached):
    id = "vol_target"
    params: ClassVar[Mapping[str, tuple[str, ...]]] = {"target": ("low", "high")}
    consults = {"scale": "size"}


CONSTRUCT: Final[Construct] = VolTarget()
```

Subclass `Sleeve` for a construct that is a strategy of its own — absolute objective, no
host — and `Attached` for one layered on a certified sleeve. `consults` is the whole of what
makes attached constructs differ from one another: it maps each `Decision` field the
modifier sets to the host hook that reads it (`allow → before_entry`, `scale → size`,
`hedges → hedges`, `exit → before_exit`).

The catalogue treats it as its own from there. A hypothesis naming it is validated against
its declarations:

```
$ kanso hyp validate hypotheses/house_mr/hypothesis.yaml
error: construct.host: vol_target attaches to a sleeve, so it names the host it attaches to
remedy: set construct.host to a certified strategy, or classify onto a portfolio
```

and `kanso classify` is shown it beside the built-in ones. Nothing in kanso enumerates
constructs, so the item is the only place your construct is described and there is nothing
else to keep in step with it.

### An execution client

A broker adapter's presence in the portfolio is a set of `ExecutionClientSpec`s. The two
fields are the whole of what the core is allowed to know about a broker, and they are what
`portfolio deploy` decides its refusals from — before anything connects, which is why they
are declarations rather than behaviour. `capital` is `simulated`, `broker_paper` or `real`;
`clock` is `replay` or `wall`.

```python
# kanso_ext/house_broker.py
from kanso.schemas import ExecutionClientSpec

PROVIDES = {"exec_clients": ["house_paper"]}

EXEC_CLIENTS = [ExecutionClientSpec(id="house_paper", capital="broker_paper", clock="wall")]
```

A stage names the id in `portfolio.yaml`, `kanso portfolio clients` lists it beside the
packaged ones, and every refusal in `docs/cli.md` applies to it unchanged: a `real` client
is refused anywhere but the live stage, and in this version a `clock: wall` client is
refused outright, because a stage node here is a bounded replay into kanso's own simulated
venue. An id nothing provides is refused by name:

```
paper      exec: 'house_exec' is not an execution client of this workspace, which has alpaca, alpaca_paper, sandbox
```

The execution client itself — the factory the node builds, the order parsing, the
reconciliation — is the broker adapter's, and `docs/adapters.md` states what one must
honour. Declaring the client is what makes it nameable.

### A data adapter

An adapter is one outside party's whole presence: the datasets it offers, the credential
names it needs, the loaders that fetch through it, the instrument provider it supplies and
the reach it measures. `docs/adapters.md` states what each member must honour, and the two
rules worth repeating here are the two an author gets wrong: `loaders(ws)` returns
**factories**, so listing what an adapter offers never builds one and never resolves a
credential; and `survey(ws)` returns **measured** reach, because what a document says a plan
includes is not what a key holds today.

```python
# kanso_ext/house_vendor.py
from dataclasses import dataclass, field

from kanso import creds
from kanso.data.registry import Survey

PROVIDES = {"adapters": ["house"]}

API_KEY = creds.standard_name("house")  # -> KANSO_HOUSE_API_KEY


class Capabilities:
    """What the adapter offers — never what a key reaches. names() and payload()."""


@dataclass(frozen=True)
class HouseAdapter:
    id: str = "house"
    kind: str = "data"  # data | reference | exec
    credentials: tuple[str, ...] = (API_KEY,)
    capabilities: Capabilities = field(default_factory=Capabilities)

    def client(self, ws): ...  # the authenticated connection
    def configured(self, ws): ...  # whether this workspace holds what it needs
    def credential_origins(self, ws): ...  # per variable, where it resolved — never a value
    def quota(self, ws): ...  # the rate limit, reportable with no credential
    def loaders(self, ws): ...  # id -> factory, nothing built
    def provider(self, ws): ...  # an InstrumentProvider, or None
    def survey(self, ws): ...  # measured reach: Survey


ADAPTER = HouseAdapter()
ADAPTERS = {"house": ADAPTER}
```

Derive every variable name with `creds.standard_name`, which is the scheme kanso resolves
under: `KANSO_<SUBJECT>_<PURPOSE>`, the subject being the id the consumer is configured as.
Resolve at the moment of use and never cache the value — two workspaces on one host may hold
two different accounts, and an adapter that cached one would serve the other's data into it.

It then appears in the listing every adapter appears in, with the same shape and the same
silence about values:

```
$ kanso data adapters
house · data · extension · 0/1 credentials resolve · bars
           quota 10/s
           KANSO_HOUSE_API_KEY: unset
```

## When an extension does not do anything

Seven failures, in the order they are worth checking.

| what you see | what happened |
|---|---|
| `failed · …` and a traceback line | the module raised while importing. Fix it, or delete it — every other extension still loaded |
| `failed · …` and `the name … was already taken` | the extension's name is one an importable module already has. Rename the directory or file |
| `loaded` and `declares nothing` | there is no `PROVIDES`, or nothing in it was usable |
| `loaded` and `PROVIDES has unusable kinds` | a kind or an id list kanso cannot read. Its constructs and clients are lost with it; its loaders, adapters and types are not |
| `loaded` and `PROVIDES declares gates` (or `objectives`) | a kind no registry reads. Delete it from the declaration; the criterion itself goes in the package |
| `absent` on a declared id | the registry for that kind found nothing under it; the reason is on the line |
| `shadowed` on a declared id | a packaged id of that name exists, and the packaged one is what the workspace uses |

`kanso doctor` reports the same extensions graded, so a broken one is visible without asking
for it. It does not go further than that: an extension is not a workspace's health, and a
`fail` there would make a workspace with a half-written extension refuse to run.

## Upstreaming

An extension that has proven itself belongs in the package, and moving it is a copy plus
tests plus a pull request. The shipped skill `kanso-upstream` drives it; this is what the
skill does.

1. `kanso doctor` reports the install as its `install` check, `<mode> · <directory>`.
   `editable · /…/kanso/src/kanso` is a local checkout and the rest of this applies;
   `package · …` means there is no checkout to work in, so clone the framework first — or,
   if the change may not belong upstream at all, take the last paragraph instead.
2. `kanso ext show` names the extension and the kind of each id it provides.
3. Copy the module and its declaration into the framework checkout at the home for its kind:
   `src/kanso/data/loaders/`, `src/kanso/data/types/`, `src/kanso/classify/constructs/`,
   `src/kanso/criteria/library/` (the YAML) beside its implementation,
   `src/kanso/data/adapters/<vendor>/` or `src/kanso/nautilus/adapters/<broker>/`. Bring the
   workspace's tests with it and add the ones the package's own components have.
4. In the checkout: `uv run pytest`, `uv run ruff format --check`, `uv run ruff check`,
   `uv run mypy src`. Then a conventional commit on a `feat/<name>` branch and a pull request
   whose body states the **workspace evidence** — what it gated or loaded, over which data —
   and, where it touches a schema or the command line, the semver consequence and the
   `docs/` change that goes with it.
5. Leave the workspace extension in place until a release containing it is installed, then
   delete it. Until you do, `kanso ext show` reports the id `shadowed` — which is the
   reminder, not a problem.

Two things do not upstream as they are. Something that is not written against these
interfaces is a rewrite rather than a copy. And an adapter for a third-party service brings
its own dependencies, which the kanso distribution does not take: a vendor package that
needs a library of its own is a distribution of its own, registered through
`PROVIDES["adapters"]` exactly as a `kanso_ext/` module is.

Where there is no checkout, or where it is not clear the thing belongs upstream at all:
`kanso doctor --report` prints a redacted block for pasting into an issue. The workspace
keeps working with its extension meanwhile, which is the whole point of having one.
