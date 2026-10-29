"""The research loop: begin a run, evaluate a card, end the run.

One hypothesis, one lane, one pinned snapshot, and a file that only ever improves. The
loop is deliberately small, because everything that makes it trustworthy lives elsewhere
and is merely sequenced here.

**A run pins what it is judged against.** `begin` copies `hypothesis.yaml` and
`program.md` into the lane directory and stores both as blobs; every later card is
evaluated against the pinned bytes rather than the workspace's, so editing the hypothesis
mid-run changes nothing except the card that notices. `strategy.py` starts from the
hypothesis's best blob when there is one — research resumes where it left off — from the
blob the scheduler re-seeded it to when the last stalls all ended on that best, and from
the workspace only when the operator asks for it, which also clears the best, because
starting from a worse file and keeping the old best would compare two ancestries. A
re-seeded run does not clear it: it climbs its own ancestry, and `records.set_best`
moves the hypothesis's best only when a keep beats it.

**The baseline calibrates the run.** It runs the unmodified `strategy.py` under
`[research] baseline_budget_s` with memory uncapped, and what it costs becomes what a
card is allowed: three times its wall time (never under a minute) and, for memory, the
lane's own share floored at three times what the baseline actually needed. A turn that runs
several cards at once allows each the same: its clock is the card budget times the cards
the turn runs, because they share the lane's cores and the baseline had them to itself, and
its memory is the share divided across them, never under that floor — a width whose part
of the share would fall under it is refused before the turn is proposed (`mem_cap`). A
baseline that times out or raises leaves no run record at all — a hypothesis whose
starting point cannot run has nothing to research.

**A kill is not a fault in the code.** A card the runner killed for its memory or its
clock measured nothing and is a crash, but it carries a failed `resources` verdict naming
the limit and what the card reached, and the driver owes it no repair: a repair is the
same idea with its traceback's fault fixed, and a kill has no traceback to fix.

**The order of the card sequence is the embargo.** The static half of
`strategy_integrity` is evaluated *before* the backtest, so a `strategy.py` that reaches
for the catalog, the filesystem or a wall clock is discarded without ever being executed.
Only then does the engine run, on research-window data alone, in a subprocess with no
path to a catalog.

**A discard costs nothing but the trial.** Keep or not, the card is recorded and its
bytes are a blob, and so — for a card that ran to a result, a redundant one and the
baseline included — is what its metric was computed from (`records.Measurement`), stored
before the card row that names it; a keep that moves the hypothesis's best rewrites
`hypotheses/<id>/strategy.py`, so the file is always the champion, and anything else
restores the lane copy from the best blob, or from the run's base before the first keep.
`results.tsv` is rendered from the records afterwards, so no restore can lose history.

**A result already known is still a result.** After the keep rule has had its say, a card
that did not keep is compared by what it held — its signature, `research/records.py` —
against every strategy judged under the run's pins, and one that held the same book on
`[research] redundant_pct` percent of their shared sessions *and* scored within the
hypothesis's own noise floor of what that book earned is *redundant*: the card is recorded
with that status and the metric it measured, the lane copy is restored as every non-keep
restores it, a `redundant` event names the card it repeats and both numbers, and
`RedundantError` reaches the caller. Both clauses, because "its result is already known"
is a claim about the result: a candidate whose number differs from the book it repeats by
more than the floor kanso already uses to say a difference is a difference has measured
something the stored card did not, and refusing it as a repeat asserts what the two
numbers deny. That candidate is an ordinary discard and it still appends a `same_book`
event, because the book it matched is a fact about the search whichever way its number
went, and the proposer that is told to move the number of a measured book cannot apply
that rule unless it is shown the books that were measured. It is a card because the
backtest ran: the engine time was spent, a real number came back, and the selection ranked
that number and declined it. Refusing it without a record cost three things, all measured
in a live workspace — the trial, so certificates deflated a Sharpe by a search fifty times
narrower than the one that was run; the coverage entry, so a corner walked a thousand
times read as unwalked to the proposer; and the idea itself, which the runs after this one
could no longer see. What a redundant
card may never be is a keep: the keep rule runs first, so a candidate that beats the best
is a keep whatever it resembles, and the baseline is never redundant, since it is the best
blob of the last run and its own signature is already stored. The two rules cannot both
fire and cannot both miss what the other would catch — the keep bar is the same floor,
doubled only when the file grew past its budget, so a candidate that cleared the floor
upward and not the doubled bar is neither a keep nor a repeat, which is exactly what it is.

**A benchmark is run once per run.** A hypothesis whose objective is measured against a
hold of its first leg has that hold produced by the runner — the card's own request with
the strategy replaced (`backtest.benchmark`) — in a child of the lane, as a card is, before
the baseline, and every card of the run is differenced against the same one, exactly as an
attached construct's cards are against one host-alone run. The lane keeps the run the
child reported and none of what producing it cost.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

from kanso import ext
from kanso.classify.construct import Construct, Harness, HostRef
from kanso.classify.construct import get as construct_for
from kanso.criteria import CardRun, GateContext, criteria_version, gates
from kanso.criteria.context import verdict
from kanso.criteria.digest import digest as digest_of
from kanso.criteria.gates import strategy_integrity
from kanso.criteria.integrity import check as check_integrity
from kanso.criteria.objectives import measures_benchmark
from kanso.data.instruments import run_definitions
from kanso.data.manifest import catalog_path
from kanso.data.snapshot import covering
from kanso.env import read as read_envelope
from kanso.errors import KansoError, PreconditionError, ValidationError
from kanso.hyp import (
    HYPOTHESIS_FILE,
    PROGRAM_FILE,
    STRATEGY_FILE,
    Registration,
    check_tick_costs,
    host_resolution,
    host_sizing,
    hypothesis_dir,
    hypothesis_file,
    read_source,
    set_status,
    show,
    venue_models,
)
from kanso.nautilus import backtest, sizing
from kanso.research import lanes, records
from kanso.research.keep import grew_by as lines_added
from kanso.research.keep import keep as keep_rule
from kanso.research.keep import noise_floor
from kanso.research.passages import BEGUN, RESEED_FROM, reseed_of, taken
from kanso.research.results import write_results
from kanso.schemas import (
    Card,
    CardDigest,
    CardStatus,
    GateResult,
    Hypothesis,
    RunRecord,
    StrategyFile,
    Tag,
    VenueModel,
    load_yaml,
    parse_yaml,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "BASELINE",
    "HEADROOM",
    "MIN_CARD_BUDGET_S",
    "REDUNDANT",
    "RESEARCHABLE",
    "RESOURCES",
    "RUNNER_UP",
    "SAME_BOOK",
    "RedundantError",
    "Setup",
    "TakenError",
    "begin",
    "card",
    "end",
    "mem_cap",
    "ongoing",
]

BASELINE: Final = "baseline"
"""The description the run's first card carries; every later one is the researcher's."""

MIN_CARD_BUDGET_S: Final = 60.0
"""No card budget is shorter than this, however fast the baseline was."""

HEADROOM: Final = 3.0
"""What a card is allowed over what the baseline needed, in time and in memory alike."""

RUNNER_UP: Final = "runner_up"
"""The status of a card of a turn that ran several which cleared the keep floor against the
best the turn began from while a sibling scored higher and was kept. Not a discard, which
says the change did not beat the best: this one did, against the best it was judged by, and
lost only to a card it was never compared with. Never a keep, since a turn keeps one."""

RESOURCES: Final = "resources"
"""The verdict a card killed for its memory or its clock carries, failed, beside the
integrity verdict: what killed it (`memory` or `budget`), the limit and what the card
reached, and how many cards its turn ran at once. Not a gate of the criteria library: a
card is judged by none of those once it is killed, and this says why."""

RESEARCHABLE: Final = frozenset({"classified", "researching", "candidate", "certified", "failed"})
"""The statuses a run may begin from: every one but a draft, which has no construct yet.

`failed` is here because a hypothesis carrying it was ended by a version of kanso that
ended one, and the queue now takes it back (`research/scheduler.py`). Leaving it out would
let such a hypothesis be queued and then refused at `begin`, which is a lane spinning
rather than a refusal anyone reads."""

CARDED: Final = "card"
ENDED: Final = "run_ended"
REDUNDANT: Final = "redundant"
"""The event a redundant card appends under the hypothesis id, and the status it carries:
the candidate held the same book as a strategy already judged under the pins, and scored
what that strategy scored. The event is what names the card it repeats and the two numbers
that agreed, which no column of `cards` holds."""

SAME_BOOK: Final = "same_book"
"""The event a card appends when it held a book already judged under the pins and earned
a number further than the noise floor from it.

It is no repeat — the two numbers deny it — so the card is an ordinary discard, nothing is
refused and the status is not this. What the event carries is the half the card cannot:
which book it matched and on how much of it. The argument for recording a repeat is the
corner it fills in and the record the next run reads, and that argument says nothing about
which way the number went, so it is made here too; the proposer is shown both kinds, and
told which is which, because the rule it is given — hold a book already measured and move
the number, and it is an experiment — cannot be applied by a model shown only the refusals.
Measured on the live workspace of 2026-09-18, 106 of 3,092 refusals were this rather than
a repeat, and each of them stores its own anchor, so a book re-trodden this way costs
roughly `ceil(spread / 2 x floor)` backtests before matching resumes."""


class RedundantError(PreconditionError):
    """The candidate's result is already known: it held what a judged strategy held and
    earned, within this hypothesis's noise floor, what that strategy earned.

    Raised after the card is recorded, not instead of recording it. The card is the
    measurement; this is the answer to whoever asked for another experiment and got none.
    """


class TakenError(PreconditionError):
    """The operator took the hypothesis out of this lane's hands: out of the queue before its
    run began (`research queue remove`), or by ending the run the lane was working
    (`research end`).

    Not a failure: nothing was wrong with the hypothesis or the lane, so a lane that meets
    it records nothing, owes the queue nothing and takes the next hypothesis at once.
    """


BASELINE_FAILED: Final = "baseline_failed"
"""The event kinds this module appends, all under the hypothesis id as subject."""

RESEARCHING: Final = "researching"

_HOST_RUNS: dict[str, dict[str, CardRun]] = {}
"""Host-alone and benchmark runs, by run and then by what they were run on. The same host,
or the same hold, over the same data gives the same run for every card of a run, so each
is computed once and reused; `end` drops the run's entry, so a process that researches all
day holds one of each per live run."""

BENCHMARK_KEY: Final = "benchmark"
"""The cache entries a benchmark run is kept under, beside the host runs of `_HOST_RUNS`."""


@dataclass(frozen=True)
class Setup:
    """Everything a card of this run needs that the run record does not carry.

    Rebuilt for each card from the run's *pinned* hypothesis rather than the workspace
    file, which is what makes `hypothesis.yaml` immutable within a run.
    """

    hyp: Hypothesis
    harness: Harness
    impl: Construct
    venue_model: VenueModel
    capital: float
    folds: int
    period: str
    max_lines: int
    catalog: Path
    host_source: bytes | None = None
    host_modifiers: tuple[tuple[str, bytes, Mapping[str, Any]], ...] = field(default=())
    grains: tuple[str, ...] = ()
    sleeve_budget: float = 0.0
    prefix: tuple[date, date] | None = None
    """The sessions a card is fed before the research window, resolved in the parent from
    the catalog as it stands when the card is built, so the child — which has no catalog —
    is handed a span rather than computing one; `None` for a hypothesis that declares no
    warmup. The run pins its snapshot, not this span: a `kanso data load` that adds a
    printed day inside the lookback between two cards moves the prefix by that day, and
    the certificate records the count it was warmed on, not the days."""
    extensions: tuple[tuple[str, str], ...] = ()
    """The workspace extensions imported while the setup was built, each by its directory
    and module name: what a card child imports before it unpickles the points it is handed,
    so a custom type an extension registers is one it can read (`kanso.ext.imported`)."""

    @property
    def window(self) -> tuple[date, date]:
        """The research window: the only window a card is ever run over."""
        research = self.hyp.windows.research
        return research.start, research.end

    @property
    def construct(self) -> str:
        return self.harness.construct

    @property
    def measured_under(self) -> str:
        """A digest of what a card's number depends on and the run's pins do not carry.

        A signature is selected by four pins — the hypothesis id and file, the snapshot
        and the criteria version — on the premise that two runs under them ask the same
        question of the same data. That is a premise about the *book*, and it is exactly
        true of one. The number beside the book is also `[research] capital`, the balance
        the harness starts with; `folds`, how many walk-forward folds the objective is
        averaged over; `return_period`, the period its returns are struck on; the venue
        model each fill is charged under, which `[research] broker` and `portfolio.yaml`
        resolve together with the hypothesis's own costs; and, for an attached construct,
        the version of the host the card is differenced against. The first three are
        `kanso.toml` keys and the fourth is resolved from `kanso.toml` and
        `portfolio.yaml`, all of them files an operator may edit between two cards; the
        last is a per-run pin; and not one of them moves a hypothesis file, a snapshot id
        or a package version. So the stored number carries this beside itself, and a row
        measured under another reading is no anchor: `records.matched_book`.

        Three more are resolved rather than declared, and each moves a number with every
        pin and every `kanso.toml` key standing still.

        `sleeve_budget` is what the run sizes to, and it reaches every fill as
        `RunRequest.sleeve_budget`. For an attached construct it is the host's budget read
        from the host's *currently registered* hypothesis (`_sleeve_budget` →
        `hyp.registry.host_sizing`), and re-registering that file with another budget
        composes no host version, so the host version above does not cover it. `grains`,
        the bar sizes the run loads, is the host's registered resolution beside the
        sleeve's own and moves by the same edit.

        `prefix` is the warmup sessions a card is fed before the window, and `Setup` is
        rebuilt for every card, so it is resolved from the catalog again for every card:
        `backtest.warmup_prefix` takes the last N distinct days it finds, and a
        `kanso data load` that adds a printed day inside the lookback moves the prefix by
        that day between two cards of one run. `_benchmark_run` keys its cache on the
        snapshot and the prefix together, which is this package's own statement that the
        prefix is part of what a card's data is.

        The claim is completeness against this dataclass, not against the world. Every
        field of `Setup` is either digested here or moves no number, and
        `tests/research/test_loop.py::test_every_field_of_a_setup_is_a_reading_or_is_not`
        fails on a field that is neither. `hyp`, `harness`, `impl`, `host_source` and
        `host_modifiers` are pinned already — the hypothesis file by its sha, this package
        by the criteria version, the host's bytes by its version; `max_lines` is the keep
        rule's line budget and no term of a metric; `catalog` is a path, and so are the
        `extensions` a card imports its data's classes from. What this cannot
        carry is data. It fixes the *span* a card warms on and never the bars the catalog
        holds inside it: those are the snapshot's business, a run pins one at `begin` and
        holds it for its life, and a load that rewrites a day inside a span whose ends do
        not move is a pin gone stale rather than a second reading.

        `[research] account` and `currency` reach a card's number as a layer of the venue
        model — `hyp.validate.venue_models` resolves them beneath the broker's declaration
        and the operator's `venues.<MIC>` override — so the venue model digested above
        carries them. Nothing else `[research]` declares reaches it. `annualisation` looks
        as though it would and does not: no objective is passed one, which
        `tests/research/test_loop.py::test_the_settings_the_digest_leaves_out_move_no_number`
        measures by changing it and reading back the same metric. Wiring it is what makes
        that test fail, and it belongs here on the same day.
        """
        digest = sha256()
        measured: tuple[object, ...] = (
            self.capital,
            self.folds,
            self.period,
            self.venue_model.model_dump(mode="json"),
            None if self.harness.host is None else self.harness.host.version,
            self.sleeve_budget,
            self.grains,
            None if self.prefix is None else [day.isoformat() for day in self.prefix],
        )
        # No `default=`: a field this cannot serialise must raise here rather than be
        # digested as a repr, which for an object without one carries an address and
        # would give the same card two readings in two processes.
        digest.update(json.dumps(measured, sort_keys=True).encode("utf-8"))
        return digest.hexdigest()[:12]


# --- setting a run up --------------------------------------------------------


def _registration(ws: Workspace, store: StateStore, hyp_id: str) -> Registration:
    return cast(Registration, show(ws, store, hyp_id))


def _pinned(ws: Workspace, store: StateStore, run: RunRecord) -> Hypothesis:
    """The hypothesis this run was pinned to, parsed from the blob it stored."""
    text = store.get_blob(run.hypothesis_sha).decode("utf-8")
    return parse_yaml(Hypothesis, text, f"{run.dir}/{HYPOTHESIS_FILE}")


def _one_venue_model(models: Mapping[str, VenueModel]) -> VenueModel:
    """The one model this run is costed with, refusing a universe that needs two.

    The runner applies one cost model to every fill of a card, so venues that resolve to
    different models cannot share a card: the number would be a mixture nothing records.
    """
    chosen = models[sorted(models)[0]]
    shape = chosen.model_dump(exclude={"venue", "origins"})
    disagree = sorted(
        venue
        for venue, model in models.items()
        if model.model_dump(exclude={"venue", "origins"}) != shape
    )
    if disagree:
        raise ValidationError(
            f"venues: {', '.join(disagree)} resolve to a different trading model than "
            f"{chosen.venue}, and one card is costed with one model",
            remedy="give the venues the same model in portfolio.yaml, or split the universe",
        )
    return chosen


def _host(ws: Workspace, hyp: Hypothesis) -> StrategyFile | None:
    """The certified host strategy a relative construct attaches to, when it names one."""
    ref = hyp.construct
    if ref is None or ref.host is None:
        return None
    path = ws.path("strategies", ref.host, "strategy.yaml")
    if not path.is_file():
        raise PreconditionError(
            f"construct.host: {ref.host!r} is not a composed strategy of this workspace",
            remedy="certify and compose the host sleeve before researching against it",
        )
    return load_yaml(StrategyFile, path)


def _host_sources(
    store: StateStore, host: HostRef
) -> tuple[bytes, tuple[tuple[str, bytes, Mapping[str, Any]], ...]]:
    """The host version's own sleeve bytes and the constructs already attached to it."""
    sleeve = _blob(store, host.sleeve.strategy_sha, f"the sleeve of {host.strategy_id}")
    attached = tuple(
        (
            ref.construct,
            _blob(store, ref.strategy_sha, f"the {ref.construct} attached to {host.strategy_id}"),
            dict(ref.params or {}),
        )
        for ref in host.attached
    )
    return sleeve, attached


def _blob(store: StateStore, sha: str, what: str) -> bytes:
    if not store.has_blob(sha):
        raise PreconditionError(
            f"{what} is recorded under {sha[:7]}, and this workspace holds no such bytes",
            remedy="restore the state store this strategy was certified in",
        )
    return store.get_blob(sha)


def _setup(ws: Workspace, store: StateStore, hyp: Hypothesis, version: int | None = None) -> Setup:
    """The construct's harness, the venue model and the run's numeric settings.

    `version` is the host version the run is pinned to, so every card of a run differences
    against the same host however often the host is re-certified while the run is open.

    The venue model is built from the definitions the card is priced under — the ones the
    store holds, which its snapshot pins — so neither a run's start nor any of its cards
    asks a reference adapter about an instrument the store defines: lanes starting together
    would otherwise each ask the vendor for every instrument at once, and be throttled out
    of their runs. Nothing is recorded, because a definition written here would move the
    store under that pin; only `kanso data instruments resolve` writes the store.
    """
    ref = hyp.construct
    if ref is None:
        raise PreconditionError(
            f"{hyp.id} is not classified, so there is no construct to research it as",
            remedy=f"run `kanso classify {hyp.id}`",
        )
    extensions = ext.imported(ws)
    impl = construct_for(ref.id, ws)
    harness = impl.harness(hyp, _host(ws, hyp), version=version)
    instruments = run_definitions(ws, hyp.universe, hyp.windows.research.start)
    model = _one_venue_model(venue_models(ws, hyp, instruments))
    research = ws.config.research
    host_source: bytes | None = None
    modifiers: tuple[tuple[str, bytes, Mapping[str, Any]], ...] = ()
    if harness.host is not None:
        host_source, modifiers = _host_sources(store, harness.host)
    catalog = catalog_path(ws)
    grains = backtest.grains_of(hyp, host_resolution(ws, store, harness.host))
    window = (hyp.windows.research.start, hyp.windows.research.end)
    return Setup(
        hyp=hyp,
        harness=harness,
        impl=impl,
        venue_model=model,
        capital=hyp.capital or research.capital,
        folds=research.folds,
        period=research.return_period,
        max_lines=research.max_lines_per_keep,
        catalog=catalog,
        host_source=host_source,
        host_modifiers=modifiers,
        grains=grains,
        sleeve_budget=_sleeve_budget(ws, store, hyp, harness.host),
        prefix=backtest.warmup_prefix(hyp, window, catalog, grains),
        extensions=extensions,
    )


def _sleeve_budget(
    ws: Workspace, store: StateStore, hyp: Hypothesis, host: HostRef | None
) -> float:
    """What the sleeve of this run sizes to: its own rule, or its host's when attached."""
    if host is not None:
        return host_sizing(ws, store, host)
    return 0.0 if hyp.sizing is None else hyp.sizing.budget


# --- running one backtest ----------------------------------------------------


def _request(
    setup: Setup,
    source: bytes,
    snapshot_id: str,
    *,
    budget_s: float | None,
    mem_cap_gb: float | None,
    host_only: bool = False,
) -> backtest.RunRequest:
    """The backtest this card asks for: the sleeve, or the host with this modifier on it."""
    attached: Sequence[tuple[str, bytes, Mapping[str, Any]]] = ()
    if setup.host_source is None:
        strategy = source
    else:
        strategy = setup.host_source
        params = dict((setup.hyp.construct.params if setup.hyp.construct else None) or {})
        if setup.hyp.sizing is not None:
            params["sizing_budget"] = setup.hyp.sizing.budget
        attached = (
            setup.host_modifiers
            if host_only
            else (*setup.host_modifiers, (setup.construct, source, params))
        )
    return backtest.RunRequest(
        hyp=setup.hyp,
        strategy_source=strategy,
        window=setup.window,
        snapshot_id=snapshot_id,
        venue_model=setup.venue_model.model_dump(),
        capital=setup.capital,
        modifiers=attached,
        budget_s=budget_s,
        mem_cap_gb=mem_cap_gb,
        period=setup.period,
        grains=setup.grains,
        sleeve_budget=setup.sleeve_budget,
        prefix=setup.prefix,
    )


def _warmup_spans(setup: Setup) -> tuple[tuple[date, date], ...]:
    """The sessions both windows warm on, resolved now so the snapshot pinned covers them.

    A run's cards are fed the research prefix, and the certification that judges the run's
    best is fed the certification one; a snapshot that pins neither would refuse the card
    or the certificate at data load, so both are resolved before the snapshot is chosen.
    Empty for a hypothesis that declares no warmup.
    """
    certification = setup.hyp.windows.certification
    spans = (
        setup.prefix,
        backtest.warmup_prefix(
            setup.hyp, (certification.start, certification.end), setup.catalog, setup.grains
        ),
    )
    return tuple(span for span in spans if span is not None)


def _host_run(
    setup: Setup,
    *,
    snapshot_id: str,
    budget_s: float,
    directory: Path,
    cache: dict[str, CardRun],
) -> CardRun | None:
    """The host-alone run a relative objective differences against, computed once.

    Computed before the card it serves — including before the baseline — so a host that
    does not run is reported as a host that does not run rather than as a failed card.
    """
    host = setup.harness.host
    if not setup.harness.relative or host is None:
        return None

    def compute(ref: HostRef) -> CardRun:
        request = _request(
            setup,
            b"",
            snapshot_id,
            budget_s=budget_s,
            mem_cap_gb=None,
            host_only=True,
        )
        result = backtest.run_subprocess(request, setup.catalog, directory, setup.extensions)
        if result.refused is not None:
            raise PreconditionError(
                f"host: {ref.strategy_id} version {ref.version} did not run over the research "
                f"window (sizing refused {result.refused.rule} at {result.refused.instrument_id}: "
                f"{result.refused.why}), so there is nothing to measure against",
                remedy="re-certify the host under its sizing rule, or attach this construct "
                "to another one",
            )
        if result.crashed:
            raise PreconditionError(
                f"host: {ref.strategy_id} version {ref.version} did not run over the research "
                f"window ({result.reason}), so there is nothing to measure against",
                remedy="re-certify the host, or attach this construct to another one",
            )
        return result.run

    return setup.impl.host_run(host, snapshot_id, compute, cache)


def _benchmark_run(
    setup: Setup, *, snapshot_id: str, directory: Path, cache: dict[str, CardRun]
) -> CardRun | None:
    """The hold a benchmark objective differences against, computed once per run.

    Its request is the card's with only the strategy replaced — the same snapshot, warmup
    prefix, money and grains — and it runs over the research window in a child of the lane,
    streamed to it a chunk at a time exactly as a card's window is
    (`backtest.run_subprocess`), so what a whole window of the hold costs goes when the child
    exits rather than staying with a process that researches all day. Like the baseline it
    has no memory cap, because it is what the cards are measured against, and unlike the
    baseline no wall time either: `backtest.benchmark` takes both bounds off its request.
    A window the catalog cannot give is refused while it is read, as a card's is, and a hold
    that does not run in its child is a refusal naming why (`_no_hold`). Keyed by the
    snapshot and the prefix, because those are what a card's data is; `None` for an
    objective that measures no benchmark.
    """
    if not measures_benchmark(setup.hyp):
        return None
    key = f"{BENCHMARK_KEY}@{snapshot_id}@{setup.prefix}"
    if key not in cache:
        request = _request(setup, b"", snapshot_id, budget_s=None, mem_cap_gb=None)
        held = backtest.run_subprocess(
            backtest.benchmark(request), setup.catalog, directory, setup.extensions
        )
        refused = held.refused
        if refused is not None:
            raise _no_hold(
                setup,
                f"sizing refused {refused.rule} at {refused.instrument_id}: {refused.why}",
                f"declare a `sizing` rule a hold of the first leg can take, then run "
                f"`kanso hyp add hypotheses/{setup.hyp.id}/hypothesis.yaml`",
            )
        if held.crashed:
            raise _no_hold(
                setup, f"{held.reason}: {held.traceback_tail or 'no output'}", held.remedy
            )
        cache[key] = held.run
    return cache[key]


def _no_hold(setup: Setup, why: str, remedy: str | None = None) -> PreconditionError:
    """Why a run has no benchmark to measure against: its hold did not run.

    The remedy is the rule's when a sizing rule refused the hold, the one the failure named
    when kanso raised it with one, and otherwise `kanso doctor` — never an edit to
    `strategy.py`: the hold is kanso's own sleeve, so a hold that did not run is not the
    strategy's to fix.
    """
    return PreconditionError(
        f"benchmark: the hold of {setup.hyp.id}'s first leg did not run over the research "
        f"window ({why}), so there is nothing to measure against",
        remedy=remedy or "run `kanso doctor`",
    )


def mem_cap(ws: Workspace, run: RunRecord, *, slots: int = 1) -> float:
    """What a card of this run may hold resident: the lane's share, never below the floor.

    `slots` is how many cards of this turn run at once. The share is divided across them,
    so a batch of three is killed at a third of what one card of the lane would be. A
    child that crosses its own third is a crash of that slot; its siblings are not.

    A stall's certification is one child and keeps the undivided lane share: the lane is
    not also running the turn's other cards (`kanso.certify.child`). The floor is three
    times what the baseline actually needed, so a run whose own
    starting point is heavier than the lane plan expected still gets cards rather than a
    string of kills. The lane's share is `[env] mem_per_lane_gb` where one is declared,
    which is the only way this threshold falls under the plan's own 4 GB floor
    (`docs/workspace.md`, `envelope.yaml`).

    No card is held to less than the floor, one at a time or several, so a width whose
    part of the share falls under it is refused rather than run, with the widest turn the
    share funds and the share the width needs as the remedy. A child of a dense tick
    window holds about 0.4 GB (`backtest.CHUNK_POINTS`), and before this 0.18.0 divided a
    2.5 GB share across seven cards to 0.36 GB each, under what any of them needs.
    """
    floor = HEADROOM * run.baseline_peak_mem_gb
    envelope = read_envelope(ws)
    share = floor if envelope is None else max(envelope.plan.mem_per_lane_gb, floor)
    width = max(1, slots)
    if round(share / width, 6) < round(floor, 6):
        funded = max(1, int(round(share / floor, 6)))
        needed = math.ceil(round(width * floor * 100, 6)) / 100
        raise PreconditionError(
            f"memory: {run.hyp_id}'s lane share of {share:.2f} GB across {width} cards a turn "
            f"is {share / width:.2f} GB a card, under the {floor:.2f} GB no card of this run is "
            f"held to less than — {HEADROOM:g} times the {run.baseline_peak_mem_gb:.2f} GB its "
            "baseline needed — so a card doing what the baseline did could be killed for it",
            remedy=f"set `[research] cards_per_turn = {funded}` in kanso.toml, or declare "
            f"`[env] mem_per_lane_gb = {needed:.2f}` and run `kanso env detect`",
        )
    return share / width


def _killed(
    result: backtest.RunResult, request: backtest.RunRequest, slots: int = 1
) -> list[GateResult]:
    """The failed `RESOURCES` verdict of a card the runner killed for its memory or its
    clock, or nothing for any other card."""
    if result.reason == backtest.MEMORY:
        return [
            verdict(
                RESOURCES,
                False,
                {
                    "killed": backtest.MEMORY,
                    "cap_gb": round(request.mem_cap_gb or 0.0, 3),
                    "peak_gb": round(result.peak_mem_gb, 3),
                    "slots": slots,
                },
            )
        ]
    if result.reason == backtest.BUDGET:
        return [
            verdict(
                RESOURCES,
                False,
                {
                    "killed": backtest.BUDGET,
                    "budget_s": round(request.budget_s or 0.0, 1),
                    "wall_s": round(result.wall_s, 1),
                    "slots": slots,
                },
            )
        ]
    return []


# --- judging one card --------------------------------------------------------


def _context(
    setup: Setup,
    run: RunRecord,
    card_run: CardRun,
    *,
    host_run: CardRun | None,
    strategy_sha: str,
    directory: Path,
    params: Mapping[str, Any] | None = None,
    benchmark_run: CardRun | None = None,
    trials: int = 1,
) -> GateContext:
    return GateContext(
        hyp=setup.hyp,
        construct=setup.construct,
        stage="card",
        params=dict(params or {}),
        window=setup.window,
        run=card_run,
        host_run=host_run,
        benchmark_run=benchmark_run,
        research_folds=setup.folds,
        snapshot_id=run.snapshot_id,
        strategy_sha=strategy_sha,
        lane_dir=directory,
        pinned={HYPOTHESIS_FILE: run.hypothesis_sha, PROGRAM_FILE: run.program_sha},
        trials=trials,
    )


def _empty_run(setup: Setup) -> CardRun:
    """A run over the research window that measured nothing: what a static failure gets."""
    return CardRun(
        window=setup.window,
        period=setup.period,
        period_ends_ns=(),
        returns=(),
        equity=(),
        trades=(),
        fills=(),
        capital=setup.capital,
        currency=setup.venue_model.currency,
        venue_model=setup.venue_model.model_dump(),
    )


def _integrity(setup: Setup, run: RunRecord, strategy_sha: str, directory: Path) -> GateResult:
    """The static half of `strategy_integrity`, evaluated before anything runs."""
    ctx = _context(
        setup,
        run,
        _empty_run(setup),
        host_run=None,
        strategy_sha=strategy_sha,
        directory=directory,
    )
    return strategy_integrity.evaluate(ctx)


def _constraints(
    setup: Setup,
    run: RunRecord,
    card_run: CardRun,
    *,
    host_run: CardRun | None,
    strategy_sha: str,
    directory: Path,
    benchmark_run: CardRun | None = None,
    trials: int,
) -> list[GateResult]:
    """Every card-stage gate the classification chose, apart from the one already run.

    `trials` is the search this card is one draw of: the hypothesis's cards that ran to a
    result and traded before it, and this one.
    """
    registry = gates()
    results: list[GateResult] = []
    for ref in setup.hyp.card_gates:
        if ref.id == strategy_integrity.id:
            continue
        ctx = _context(
            setup,
            run,
            card_run,
            host_run=host_run,
            strategy_sha=strategy_sha,
            directory=directory,
            params=ref.params,
            benchmark_run=benchmark_run,
            trials=trials,
        )
        results.append(registry[ref.id].evaluate(ctx))
    return results


def _measure(
    setup: Setup,
    card_run: CardRun,
    host_run: CardRun | None,
    benchmark_run: CardRun | None = None,
) -> records.Measurement:
    """What a card is scored from: the objective, the folds and the runs it reads, under the
    criteria version of the kanso scoring it now.

    Its `compute` is the objective over the run's folds and the error of that mean, which is
    the card's metric; the card keeps it whole (`records.record_measurement`), so the number
    and what it was computed from are one object and cannot be stored apart.
    """
    ref = setup.hyp.objective
    if ref is None:  # pragma: no cover - a classified hypothesis always carries one
        raise PreconditionError(
            f"{setup.hyp.id} names no objective, so a card of it is scored on nothing",
            remedy=f"run `kanso classify {setup.hyp.id}`",
        )
    return records.Measurement(
        ref, setup.folds, card_run, host_run, benchmark_run, criteria_version=criteria_version()
    )


def _grew_by(store: StateStore, run: RunRecord, source: bytes) -> int:
    """The lines this candidate adds to the file the run is currently climbing from."""
    previous = store.get_blob(run.best_sha or run.base_sha)
    return lines_added(source, previous)


def _keeps(
    setup: Setup,
    store: StateStore,
    run: RunRecord,
    source: bytes,
    metric: float,
    se: float,
) -> bool:
    """Whether a card whose constraints all passed clears the run's noise floor."""
    ref = setup.hyp.objective
    if ref is None:  # pragma: no cover - a classified hypothesis always carries one
        return False
    return keep_rule(
        metric, se, run.best_metric, ref.params, _grew_by(store, run, source), setup.max_lines
    )


def _record(
    ws: Workspace,
    store: StateStore,
    setup: Setup,
    run: RunRecord,
    *,
    strategy_sha: str,
    source: bytes,
    desc: str,
    status: CardStatus,
    metric: float,
    se: float,
    n_trades: int,
    wall_s: float,
    peak_mem_gb: float,
    n_trials: int,
    gate_results: Sequence[GateResult],
    crash_tail: str | None,
    directory: Path,
    tags: Sequence[Tag] = (),
    restore_all: bool = False,
    digest: CardDigest | None = None,
    measured_sha: str | None = None,
) -> Card:
    """Write the card, then move `best` or restore the lane copy, then render the log.

    The workspace `strategy.py` is the hypothesis's champion, so a keep writes it only
    when the hypothesis's best is now these bytes: a keep that moved no more than its
    run's best — a re-seeded run's baseline, a lesser keep under new pins — leaves it.
    """
    made = Card(
        run_id=run.run_id,
        lane=run.lane,
        strategy_sha=strategy_sha,
        metric=metric,
        metric_se=se,
        n_trials=n_trials,
        n_trades=n_trades,
        wall_s=wall_s,
        peak_mem_gb=peak_mem_gb,
        status=status,
        desc=desc,
        tags=list(tags),
        gate_results=list(gate_results),
        crash_tail=crash_tail,
        venue_model=setup.venue_model,
        created_at=records.now(),
        digest=digest,
        measured_sha=measured_sha,
    )
    records.record_card(store, run, made)
    if status == "keep":
        records.set_best(store, run, strategy_sha, metric)
        if records.best_of(store, run.hyp_id)[0] == strategy_sha:
            lanes.write_atomic(hypothesis_dir(ws, run.hyp_id) / STRATEGY_FILE, source)
    else:
        restored = {STRATEGY_FILE: run.best_sha or run.base_sha}
        if restore_all:
            restored[HYPOTHESIS_FILE] = run.hypothesis_sha
            restored[PROGRAM_FILE] = run.program_sha
        lanes.restore(store, directory, restored)
    write_results(ws, store, run.hyp_id)
    store.event(
        CARDED,
        run.hyp_id,
        {"run_id": run.run_id, "sha": strategy_sha[:7], "status": status, "metric": metric},
    )
    return made


def _judge(
    ws: Workspace,
    store: StateStore,
    setup: Setup,
    run: RunRecord,
    *,
    strategy_sha: str,
    source: bytes,
    desc: str,
    integrity: GateResult,
    result: backtest.RunResult,
    host_run: CardRun | None,
    directory: Path,
    tags: Sequence[Tag] = (),
    baseline: bool = False,
    benchmark_run: CardRun | None = None,
    request: backtest.RunRequest | None = None,
) -> Card:
    """Steps 3 to 5: the constraints, the keep rule, the redundancy check and the record.

    `baseline` exempts the run's first card from the redundancy check and nothing else.
    The signature is taken and the card is written whatever the check says: a candidate
    that ran is a card, and the check decides which status it carries and whether the
    caller is refused, never whether there is a record. The measurement comes before the
    check, which is what lets the check ask whether the result repeats as well as whether
    the book does; the signature is stored with that measurement, so the next candidate is
    compared against both.
    """
    n_trials = records.n_trials(store, run.hyp_id) + 1
    if result.refused is not None:
        return _record(
            ws,
            store,
            setup,
            run,
            strategy_sha=strategy_sha,
            source=source,
            desc=desc,
            status="discard",
            metric=0.0,
            se=0.0,
            n_trades=0,
            wall_s=result.wall_s,
            peak_mem_gb=result.peak_mem_gb,
            n_trials=n_trials,
            gate_results=[integrity, verdict(sizing.GATE, False, result.refused.payload())],
            crash_tail=None,
            directory=directory,
            tags=tags,
        )
    if result.crashed:
        return _record(
            ws,
            store,
            setup,
            run,
            strategy_sha=strategy_sha,
            source=source,
            desc=desc,
            status="crash",
            metric=0.0,
            se=0.0,
            n_trades=len(result.run.trades),
            wall_s=result.wall_s,
            peak_mem_gb=result.peak_mem_gb,
            n_trials=n_trials,
            gate_results=[integrity, *([] if request is None else _killed(result, request))],
            crash_tail=result.traceback_tail or result.reason,
            directory=directory,
            tags=tags,
        )
    constraints = _constraints(
        setup,
        run,
        result.run,
        host_run=host_run,
        strategy_sha=strategy_sha,
        directory=directory,
        benchmark_run=benchmark_run,
        trials=records.candidates(store, run.hyp_id) + 1,
    )
    measured = _measure(setup, result.run, host_run, benchmark_run)
    metric, se = measured.compute()
    measured_sha = records.record_measurement(store, measured)
    passed = integrity.passed and all(gate.passed for gate in constraints)
    kept = passed and _keeps(setup, store, run, source, metric, se)
    held = records.signature(result.run)
    floor = _noise_floor(setup, se)
    like = (
        None
        if kept or baseline
        else records.matched_book(
            store,
            run,
            held,
            ws.config.research.redundant_pct,
            metric,
            floor,
            setup.measured_under,
        )
    )
    repeat = like is not None and like.agreed
    records.record_signature(store, run, strategy_sha, held, metric, setup.measured_under)
    made = _record(
        ws,
        store,
        setup,
        run,
        strategy_sha=strategy_sha,
        source=source,
        desc=desc,
        status="keep" if kept else REDUNDANT if repeat else "discard",
        metric=metric,
        se=se,
        n_trades=len(result.run.trades),
        wall_s=result.wall_s,
        peak_mem_gb=result.peak_mem_gb,
        n_trials=n_trials,
        gate_results=[integrity, *constraints],
        crash_tail=None,
        directory=directory,
        tags=tags,
        digest=digest_of(result.run, result.intents),
        measured_sha=measured_sha,
    )
    if like is not None:
        _same_book(store, run, made.strategy_sha, desc, metric, floor, like)
    return made


def _noise_floor(setup: Setup, se: float) -> float:
    """The scale on which two cards of this hypothesis are two results rather than one.

    The keep rule's own floor, undoubled: `research/keep.py` says why a complexity clause
    belongs to an improvement and not to a comparison.
    """
    ref = setup.hyp.objective
    if ref is None:  # pragma: no cover - a classified hypothesis always carries one
        return 0.0
    return noise_floor(se, ref.params)


def _same_book(
    store: StateStore,
    run: RunRecord,
    strategy_sha: str,
    desc: str,
    metric: float,
    floor: float,
    like: records.Match,
    *,
    refuse: bool = True,
) -> None:
    """Name the book this card matched in an event, and refuse the caller when it repeats it.

    The card is already written and the lane is already restored, both by `_record`, so
    what is left here is what a card has no column for: which stored signature it matched,
    on how many of their shared sessions, the metric it measured while doing it, the
    metric that signature earned, and the floor the two of them sat inside. Those five
    facts are the same whichever way the two numbers went, so one event shape carries both
    kinds and the kind says which it is — `redundant` when they agreed and the turn is
    refused, `same_book` when they did not and the card is an ordinary discard.

    The refusal is what stops a redundant result being taken for a new one — the driver
    counts it toward the stall, and an operator gets a non-zero exit — and it says both
    halves, because a reader told only that two books matched cannot tell this refusal
    from the one kanso used to make on a book alone. A match that did not agree refuses
    nothing and returns: it was an experiment, and the record is all it owes.
    """
    store.event(
        REDUNDANT if like.agreed else SAME_BOOK,
        run.hyp_id,
        {
            "run_id": run.run_id,
            "lane": run.lane,
            "sha": strategy_sha[:7],
            "like": like.like[:7],
            "matched": like.matched,
            "sessions": like.shared,
            "pct": round(like.pct, 2),
            "metric": metric,
            "like_metric": like.earned,
            "floor": floor,
            "desc": desc,
        },
    )
    if not like.agreed or not refuse:
        return
    raise RedundantError(
        f"{strategy_sha[:7]} held the same book as {like.like[:7]} on {like.matched} of "
        f"{like.shared} sessions ({like.pct:.0f}%) and scored {metric:.6g} against its "
        f"{like.earned:.6g}, inside the {floor:.6g} that separates two results here, so its "
        "result is already known and it is not an experiment; it is recorded as a redundant "
        "card and the lane copy has been restored",
        remedy=(
            f"move the result further than {floor:.6g} — a different instrument, side, set of "
            "sessions or holding over a session's end, or a size that changes the number and "
            "not only the book; "
            f"`kanso research show {run.hyp_id} --sha {like.like[:7]}` prints the card it repeats"
        ),
    )


# --- the three entry points --------------------------------------------------


def begin(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    tag: str | None = None,
    lane: str = lanes.DEFAULT_LANE,
    from_workspace: bool = False,
) -> RunRecord:
    """Start a run: the lane directory, the pins, the snapshot and the baseline card.

    Refuses a hypothesis that is not registered, is not classified, already has an active
    run, whose workspace `hypothesis.yaml` no longer equals its pin, or which no snapshot
    covers, and one at tick resolution whose venue model, as the workspace resolves it now, a
    validation would refuse (`kanso.hyp.validate.check_tick_costs`) — a file registered before
    the rule, or a `portfolio.yaml` edited since. A baseline that will not run leaves no record
    and no lane directory.

    One the operator takes out of `lane`'s hands (`research queue remove`) is refused with
    `TakenError` wherever the begin has got to: before anything is read, at the next
    catalog read of the warmup, the benchmark or the baseline's window, within
    `backtest.WANTED_POLL_S` of a removal while the hold or the baseline card runs — the
    child is killed — and once more before the run is recorded. It leaves no lane directory and no
    `baseline_failed`, because the baseline did not fail.
    """
    lane = lanes.check_lane(lane)
    registration = _registration(ws, store, hyp_id)
    if registration.status not in RESEARCHABLE:
        raise PreconditionError(
            f"{hyp_id} is {registration.status}, and a run begins from "
            f"{', '.join(sorted(RESEARCHABLE))}",
            remedy=f"run `kanso classify {hyp_id}`",
        )
    if registration.active_run is not None:
        raise PreconditionError(
            f"{hyp_id} already has an active run ({registration.active_run})",
            remedy=f"end it with `kanso research end {hyp_id}`",
        )
    _admitted(store, hyp_id, lane)
    path = hypothesis_file(ws, hyp_id)
    source = read_source(path)
    if not registration.pinned:
        raise PreconditionError(
            f"{path} is not the file {hyp_id} is registered under; a run is pinned to the "
            "registered bytes",
            remedy=f"run `kanso hyp add {path}` to re-pin it, then begin again",
        )
    hyp = parse_yaml(Hypothesis, source.decode("utf-8"), str(path))
    envelope = read_envelope(ws)
    if envelope is None:
        raise PreconditionError(
            "this workspace has no envelope, so no lane has a memory share",
            remedy="run `kanso env detect`",
        )
    held = partial(_admitted, store, hyp_id, lane)
    with backtest.wanted(held):
        setup = _setup(ws, store, hyp)
        check_tick_costs(ws, hyp, {setup.venue_model.venue: setup.venue_model})
        prefixes = _warmup_spans(setup)
    snapshot = covering(
        ws,
        hyp.universe,
        hyp.data_requirements,
        hyp.bar_size,
        hyp.windows,
        prefixes,
        hyp.data_by_instrument,
    )
    if snapshot is None:
        warmed = " and the warmup sessions before each" if prefixes else ""
        raise PreconditionError(
            f"no snapshot covers {', '.join(hyp.universe)} over the research and certification "
            f"windows{warmed} at {hyp.resolution}",
            remedy="load the data and run `kanso data snapshot`",
        )
    program = hypothesis_dir(ws, hyp_id) / PROGRAM_FILE
    if not program.is_file():
        raise PreconditionError(
            f"{program} is missing, and a run pins the program it follows",
            remedy=f"run `kanso hyp new {hyp_id}` in another directory and copy program.md over",
        )
    base, from_best, reseeded = _base_source(
        ws, store, hyp_id, from_workspace=from_workspace, reseed=reseed_of(store, hyp_id)
    )
    pins = {
        HYPOTHESIS_FILE: store.put_blob(source),
        PROGRAM_FILE: store.put_blob(program.read_bytes()),
        STRATEGY_FILE: store.put_blob(base),
    }
    directory = lanes.prepare(lanes.lane_dir(ws, lane, hyp_id))
    lanes.restore(store, directory, pins)
    host_cache: dict[str, CardRun] = {}
    try:
        with backtest.wanted(held):
            host_run = _host_run(
                setup,
                snapshot_id=snapshot.snapshot_id,
                budget_s=float(ws.config.research.baseline_budget_s),
                directory=directory,
                cache=host_cache,
            )
            benchmark_run = _benchmark_run(
                setup, snapshot_id=snapshot.snapshot_id, directory=directory, cache=host_cache
            )
            result = _baseline(
                ws, setup, snapshot.snapshot_id, directory, pins, from_best=from_best
            )
        held()  # the card's last poll may have come before a removal did
    except TakenError:
        lanes.remove(directory)  # the operator's word, not a failure of the baseline
        raise
    except KansoError as exc:
        # Nothing a card could be judged against ran, so the run leaves no trace but the
        # event the scheduler reads to requeue the hypothesis at a lower priority.
        lanes.remove(directory)
        store.event(BASELINE_FAILED, hyp_id, {"reason": exc.message})
        raise
    run = records.insert(
        store,
        RunRecord(
            run_id=uuid.uuid4().hex,
            hyp_id=hyp_id,
            tag=tag or records.next_tag(store, hyp_id, date.today()),
            lane=lane,
            dir=str(directory.relative_to(ws.root)),
            base_sha=pins[STRATEGY_FILE],
            hypothesis_sha=pins[HYPOTHESIS_FILE],
            program_sha=pins[PROGRAM_FILE],
            snapshot_id=snapshot.snapshot_id,
            criteria_version=criteria_version(),
            host_version=setup.harness.host.version if setup.harness.host else None,
            card_budget_s=max(MIN_CARD_BUDGET_S, HEADROOM * result.wall_s),
            baseline_wall_s=result.wall_s,
            baseline_peak_mem_gb=result.peak_mem_gb,
            started_at=records.now(),
        ),
    )
    _HOST_RUNS[run.run_id] = host_cache
    begun: dict[str, object] = {"run_id": run.run_id, "tag": run.tag, "lane": lane}
    if reseeded is not None:
        begun[RESEED_FROM] = reseeded
    store.event(BEGUN, hyp_id, begun)
    if registration.status != RESEARCHING:
        set_status(store, hyp_id, RESEARCHING)
    _judge(
        ws,
        store,
        setup,
        run,
        strategy_sha=pins[STRATEGY_FILE],
        source=base,
        desc=BASELINE,
        integrity=_integrity(setup, run, pins[STRATEGY_FILE], directory),
        result=result,
        host_run=host_run,
        directory=directory,
        baseline=True,
        benchmark_run=benchmark_run,
    )
    return records.require_active(store, hyp_id)


def _base_source(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    from_workspace: bool,
    reseed: str | None = None,
) -> tuple[bytes, bool, str | None]:
    """The `strategy.py` a run starts from, whether it is the hypothesis's best, and the
    sha it was re-seeded from when it was.

    The best blob when one exists and the workspace copy otherwise; `reseed` — the sha
    the scheduler put on the queue passage after a spell of stalls — takes that blob
    instead of the best and leaves the best alone; `--from-workspace` takes the workspace
    copy regardless and clears the best, so the history says the run started over.
    """
    best, _ = records.best_of(store, hyp_id)
    if reseed is not None and not from_workspace and store.has_blob(reseed):
        return store.get_blob(reseed), False, reseed
    if best is not None and not from_workspace:
        return store.get_blob(best), True, None
    path = hypothesis_dir(ws, hyp_id) / STRATEGY_FILE
    if not path.is_file():
        raise PreconditionError(
            f"{path} is missing, and a run starts from a strategy",
            remedy=f"run `kanso classify {hyp_id}` to render the construct's stub",
        )
    if from_workspace and best is not None:
        records.unset_best(store, hyp_id)
        store.event(
            "best_cleared", hyp_id, {"reason": "the run starts from the workspace strategy"}
        )
    return path.read_bytes(), False, None


def _baseline(
    ws: Workspace,
    setup: Setup,
    snapshot_id: str,
    directory: Path,
    pins: Mapping[str, str],
    *,
    from_best: bool,
) -> backtest.RunResult:
    """Run the unmodified `strategy.py`, or refuse the run outright.

    The static half of `strategy_integrity` is checked here too, and for the same reason
    as on a card: a starting point that reaches outside the lane must not be executed.
    Anything wrong here refuses the run outright, because a run whose baseline did not
    run has no budget to give its cards. `from_best` says whether the file that did not
    run was the best card's, which decides what beginning again would take.
    """
    problems = check_integrity(
        directory,
        dict(pins),
        sized=setup.hyp.sizing is not None,
        construct=setup.construct,
        depth=setup.hyp.depth is not None,
    )
    if problems:
        _refuse_baseline(setup.hyp.id, "; ".join(problems[:5]), from_best=from_best)
    result = backtest.run_subprocess(
        _request(
            setup,
            (directory / STRATEGY_FILE).read_bytes(),
            snapshot_id,
            budget_s=float(ws.config.research.baseline_budget_s),
            mem_cap_gb=None,
        ),
        setup.catalog,
        directory,
        setup.extensions,
    )
    if result.refused is not None:
        _refuse_baseline(
            setup.hyp.id,
            f"sizing refused {result.refused.rule} at {result.refused.instrument_id}: "
            f"{result.refused.why}",
            from_best=from_best,
        )
    if result.crashed:
        _refuse_baseline(
            setup.hyp.id,
            f"{result.reason}: {result.traceback_tail or 'no output'}",
            result.remedy,
            from_best=from_best,
        )
    return result


def _refuse_baseline(
    hyp_id: str, why: str, remedy: str | None = None, *, from_best: bool = False
) -> NoReturn:
    """Refuse the run, naming what the baseline did instead of running.

    A baseline fails for causes that are not the strategy's — a catalog that no longer
    holds the window's rows is the one an operator meets — and each wants a different
    next action. Where the cause named its own remedy and that remedy crossed the card's
    process boundary, it is the one reported; fixing `strategy.py` is what is left when
    nothing else was named, which is the case for a strategy that raised. When the
    strategy that raised was the best card's, beginning again would take that blob
    again, so the remedy names the flag that starts from the workspace file instead.
    """
    if remedy is None:
        remedy = f"fix hypotheses/{hyp_id}/{STRATEGY_FILE} and begin again"
        if from_best:
            remedy += (
                f" with `kanso research begin {hyp_id} --from-workspace`, since a run "
                "otherwise starts from the best card's strategy, which is what did not run"
            )
    raise PreconditionError(f"the baseline card of {hyp_id} did not run: {why}", remedy=remedy)


def card(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    desc: str,
    lane: str = lanes.DEFAULT_LANE,
    tags: Sequence[Tag] = (),
) -> Card:
    """Evaluate the lane directory's `strategy.py` as one card of the active run.

    Stores the bytes, checks the static half of `strategy_integrity` before anything
    runs, backtests the research window in a subprocess under the run's budgets,
    evaluates the constraints and the keep rule, and records the card. A candidate that
    ran, held what a judged strategy already held and scored what it scored is recorded all
    the same, with the status `redundant` and the metric it measured, and then
    `RedundantError` refuses the caller another experiment: the check decides the status,
    never whether there is a record. `tags` are the proposer's account of the change, from
    `kanso.schemas.TAGS`; a card made by hand carries none.

    A run the operator ends while its card runs is no longer one to record a card of: the
    card is refused with `TakenError` at the next catalog read of its window, or within
    `backtest.WANTED_POLL_S` of the end once its child runs — the child is killed — and
    nothing is recorded (`ongoing`).
    """
    run = records.require_active(store, hyp_id, lanes.check_lane(lane))
    setup = _setup(ws, store, _pinned(ws, store, run), run.host_version)
    directory = ws.root / run.dir
    source = _lane_source(store, run, directory)
    strategy_sha = store.put_blob(source)
    integrity = _integrity(setup, run, strategy_sha, directory)
    if not integrity.passed:
        return _record(
            ws,
            store,
            setup,
            run,
            strategy_sha=strategy_sha,
            source=source,
            desc=desc,
            status="discard",
            metric=0.0,
            se=0.0,
            n_trades=0,
            wall_s=0.0,
            peak_mem_gb=0.0,
            n_trials=records.n_trials(store, hyp_id) + 1,
            gate_results=[integrity],
            crash_tail=None,
            directory=directory,
            tags=tags,
            restore_all=True,
        )
    still = partial(ongoing, store, run)
    with backtest.wanted(still):
        host_run = _host_run(
            setup,
            snapshot_id=run.snapshot_id,
            budget_s=run.card_budget_s,
            directory=directory,
            cache=_HOST_RUNS.setdefault(run.run_id, {}),
        )
        benchmark_run = _benchmark_run(
            setup,
            snapshot_id=run.snapshot_id,
            directory=directory,
            cache=_HOST_RUNS.setdefault(run.run_id, {}),
        )
        request = _request(
            setup,
            source,
            run.snapshot_id,
            budget_s=run.card_budget_s,
            mem_cap_gb=mem_cap(ws, run),
        )
        result = backtest.run_subprocess(request, setup.catalog, directory, setup.extensions)
    still()  # the card's last poll may have come before the end did
    return _judge(
        ws,
        store,
        setup,
        run,
        strategy_sha=strategy_sha,
        source=source,
        desc=desc,
        integrity=integrity,
        result=result,
        host_run=host_run,
        directory=directory,
        tags=tags,
        benchmark_run=benchmark_run,
        request=request,
    )


@dataclass(frozen=True)
class _Decided:
    """One slot of a turn as it was judged, before it is recorded: what `_record` takes,
    and the book it matched, when it matched one, for the event that names it."""

    desc: str
    source: bytes
    sha: str
    tags: Sequence[Tag]
    status: CardStatus
    gates: list[GateResult]
    metric: float = 0.0
    se: float = 0.0
    n_trades: int = 0
    wall_s: float = 0.0
    peak_mem_gb: float = 0.0
    crash_tail: str | None = None
    like: records.Match | None = None
    measured_sha: str | None = None
    digest: CardDigest | None = None


def run_batch(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    proposals: Sequence[tuple[str, bytes, Sequence[Tag]]],
    lane: str = lanes.DEFAULT_LANE,
    slots: int = 1,
) -> list[Card]:
    """Evaluate every proposal of one turn against the best the turn started from.

    The backtests that integrity lets through run together, from one read of the window,
    each under `slots`' share of the lane's memory (`mem_cap`, which refuses a width the
    share cannot fund) and `slots` times the run's card budget: the children share the
    lane's cores, and the baseline the budget was struck from had them to itself. On 0.18.0
    every slot had one card's budget, so on a host whose lanes already fill its cores each
    child of a turn of `w` had about a `w`-th of its baseline's core and would run about `w`
    times its wall: four at the three-times kill line, eight all killed. Gates and the
    keep rule see that pre-turn best and a trial count that already includes every proposal
    of the turn.
    The highest metric that clears the floor is the one keep; a tie keeps the earlier
    slot, and every other slot that cleared it is a `RUNNER_UP`. Every proposal is
    recorded, in slot order, and a redundant one does not refuse the rest of the turn.
    """
    run = records.require_active(store, hyp_id, lanes.check_lane(lane))
    setup = _setup(ws, store, _pinned(ws, store, run), run.host_version)
    directory = ws.root / run.dir
    frozen = run
    margin = records.candidates(store, hyp_id) + len(proposals)
    cap = mem_cap(ws, run, slots=slots)
    budget = frozen.card_budget_s * max(1, slots)
    prepared: list[tuple[str, bytes, Sequence[Tag], str, GateResult]] = []
    running: list[tuple[int, backtest.RunRequest]] = []
    for desc, source, tags in proposals:
        digest_sha = store.put_blob(source)
        lanes.write_atomic(directory / STRATEGY_FILE, source)
        integrity = _integrity(setup, frozen, digest_sha, directory)
        prepared.append((desc, source, tags, digest_sha, integrity))
        if integrity.passed:
            running.append(
                (
                    len(prepared) - 1,
                    _request(
                        setup,
                        source,
                        frozen.snapshot_id,
                        budget_s=budget,
                        mem_cap_gb=cap,
                    ),
                )
            )
    results: dict[int, backtest.RunResult] = {}
    asked: dict[int, backtest.RunRequest] = {}
    if running:
        still = partial(ongoing, store, frozen)
        with backtest.wanted(still):
            host_run = _host_run(
                setup,
                snapshot_id=frozen.snapshot_id,
                budget_s=frozen.card_budget_s,
                directory=directory,
                cache=_HOST_RUNS.setdefault(frozen.run_id, {}),
            )
            benchmark_run = _benchmark_run(
                setup,
                snapshot_id=frozen.snapshot_id,
                directory=directory,
                cache=_HOST_RUNS.setdefault(frozen.run_id, {}),
            )
            ran = backtest.run_subprocesses(
                [request for _index, request in running],
                setup.catalog,
                directory,
                setup.extensions,
            )
        still()
        for (index, request_made), result in zip(running, ran, strict=True):
            results[index] = result
            asked[index] = request_made
    else:
        host_run = None
        benchmark_run = None
    decided: list[_Decided] = []
    for index, (desc, source, tags, digest_sha, integrity) in enumerate(prepared):
        if not integrity.passed:
            decided.append(_Decided(desc, source, digest_sha, tags, "discard", [integrity]))
            continue
        result = results[index]
        if result.refused is not None:
            decided.append(
                _Decided(
                    desc,
                    source,
                    digest_sha,
                    tags,
                    "discard",
                    [integrity, verdict(sizing.GATE, False, result.refused.payload())],
                    wall_s=result.wall_s,
                    peak_mem_gb=result.peak_mem_gb,
                )
            )
            continue
        if result.crashed:
            decided.append(
                _Decided(
                    desc,
                    source,
                    digest_sha,
                    tags,
                    "crash",
                    [integrity, *_killed(result, asked[index], slots)],
                    n_trades=len(result.run.trades),
                    wall_s=result.wall_s,
                    peak_mem_gb=result.peak_mem_gb,
                    crash_tail=result.traceback_tail or result.reason,
                )
            )
            continue
        constraints = _constraints(
            setup,
            frozen,
            result.run,
            host_run=host_run,
            strategy_sha=digest_sha,
            directory=directory,
            benchmark_run=benchmark_run,
            trials=margin,
        )
        measured = _measure(setup, result.run, host_run, benchmark_run)
        metric, se = measured.compute()
        measured_sha = records.record_measurement(store, measured)
        passed = all(gate.passed for gate in constraints)
        kept = passed and _keeps(setup, store, frozen, source, metric, se)
        held = records.signature(result.run)
        floor = _noise_floor(setup, se)
        like = (
            None
            if kept
            else records.matched_book(
                store,
                frozen,
                held,
                ws.config.research.redundant_pct,
                metric,
                floor,
                setup.measured_under,
            )
        )
        records.record_signature(store, frozen, digest_sha, held, metric, setup.measured_under)
        status: CardStatus = (
            "keep" if kept else REDUNDANT if like is not None and like.agreed else "discard"
        )
        decided.append(
            _Decided(
                desc,
                source,
                digest_sha,
                tags,
                status,
                [integrity, *constraints],
                metric=metric,
                se=se,
                n_trades=len(result.run.trades),
                wall_s=result.wall_s,
                peak_mem_gb=result.peak_mem_gb,
                like=like,
                measured_sha=measured_sha,
                digest=digest_of(result.run, result.intents),
            )
        )
    best_at: int | None = None
    for index, item in enumerate(decided):
        if item.status == "keep" and (best_at is None or item.metric > decided[best_at].metric):
            best_at = index
    if best_at is not None:
        for index, item in enumerate(decided):
            if item.status == "keep" and index != best_at:
                decided[index] = replace(item, status=RUNNER_UP)
    made: list[Card] = []
    current = frozen
    for item in decided:
        made.append(
            _record(
                ws,
                store,
                setup,
                current,
                strategy_sha=item.sha,
                source=item.source,
                desc=item.desc,
                status=item.status,
                metric=item.metric,
                se=item.se,
                n_trades=item.n_trades,
                wall_s=item.wall_s,
                peak_mem_gb=item.peak_mem_gb,
                n_trials=records.n_trials(store, hyp_id) + 1,
                gate_results=item.gates,
                crash_tail=item.crash_tail,
                directory=directory,
                tags=item.tags,
                digest=item.digest,
                measured_sha=item.measured_sha,
            )
        )
        if item.status == "keep":
            current = records.set_best(store, current, item.sha, item.metric)
        if item.like is not None:
            _same_book(
                store,
                current,
                item.sha,
                item.desc,
                item.metric,
                _noise_floor(setup, item.se),
                item.like,
                refuse=False,
            )
    kept_bytes = decided[best_at].source if best_at is not None else None
    if kept_bytes is not None:
        lanes.write_atomic(directory / STRATEGY_FILE, kept_bytes)
    else:
        lanes.restore(store, directory, {STRATEGY_FILE: frozen.best_sha or frozen.base_sha})
    return made


def _lane_source(store: StateStore, run: RunRecord, directory: Path) -> bytes:
    """The lane's `strategy.py`, restored and refused when the researcher removed it."""
    path = directory / STRATEGY_FILE
    if path.is_file():
        return path.read_bytes()
    lanes.restore(store, directory, {STRATEGY_FILE: run.best_sha or run.base_sha})
    raise PreconditionError(
        f"{path} is missing, so there was nothing to evaluate; it has been restored",
        remedy="edit the restored strategy.py and run the card again",
    )


def _admitted(store: StateStore, hyp_id: str, lane: str) -> None:
    """Refuse to begin a run for a hypothesis the operator took out of this lane's hands."""
    if taken(store, hyp_id, lane):
        raise TakenError(
            f"{hyp_id} was taken out of the queue while lane {lane} held it, so this lane "
            "does not begin its run",
            remedy=f"`kanso research queue add {hyp_id}` when you want it researched again",
        )


def ongoing(store: StateStore, run: RunRecord) -> RunRecord:
    """`run` as it stands now, refused with `TakenError` once it has ended.

    `research end` closes a run from another process and removes its lane directory there,
    whatever the lane working it is doing. The lane asks this before it writes a proposal
    into the directory, before every catalog read of a card's window, every
    `backtest.WANTED_POLL_S` while the card runs — the child is killed — once the child has
    exited and before its output is read, once an alignment judge has answered
    (`kanso.research.align.check`) and before the next proposal; so an end costs the card
    or the answer in flight and nothing else, and is never read as the card's failure or
    the lane's. Measured before this, on 0.16.0: a run ended while its card ran left the card
    running to its end, and the lane then died with a `FileNotFoundError` reading the card's
    output from the directory the end had removed. What the lane cached for the run goes
    with it.
    """
    current = records.active(store, run.hyp_id)
    if current is not None and current.run_id == run.run_id:
        return current
    _HOST_RUNS.pop(run.run_id, None)
    raise TakenError(
        f"the run {run.tag} of {run.hyp_id} was ended while lane {run.lane} worked it, so "
        "nothing more is recorded for it",
        remedy=f"`kanso research queue add {run.hyp_id}` when you want it researched again",
    )


def end(ws: Workspace, store: StateStore, hyp_id: str) -> RunRecord:
    """End the active run, removing the lane directory and nothing else.

    The cards, the blobs and the hypothesis's `best` stay in state, and so does the run's
    log beside the lane directory: what a run produced outlives where it produced it.
    """
    run = records.require_active(store, hyp_id)
    closed = records.close(store, run)
    lanes.remove(ws.root / run.dir)
    _HOST_RUNS.pop(run.run_id, None)
    store.event(ENDED, hyp_id, {"run_id": run.run_id, "tag": run.tag, "lane": run.lane})
    return closed
