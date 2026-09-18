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
lane's own share floored at three times what the baseline actually needed. A baseline
that times out or raises leaves no run record at all — a hypothesis whose starting point
cannot run has nothing to research.

**The order of the card sequence is the embargo.** The static half of
`strategy_integrity` is evaluated *before* the backtest, so a `strategy.py` that reaches
for the catalog, the filesystem or a wall clock is discarded without ever being executed.
Only then does the engine run, on research-window data alone, in a subprocess with no
path to a catalog.

**A discard costs nothing but the trial.** Keep or not, the card is recorded and its
bytes are a blob; a keep that moves the hypothesis's best rewrites
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
the strategy replaced (`backtest.benchmark`) — in this process, before the baseline, and
every card of the run is differenced against the same one, exactly as an attached
construct's cards are against one host-alone run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

from kanso.classify.construct import Construct, Harness, HostRef
from kanso.classify.construct import get as construct_for
from kanso.criteria import CardRun, GateContext, criteria_version, gates, objectives
from kanso.criteria.context import verdict
from kanso.criteria.gates import strategy_integrity
from kanso.criteria.integrity import check as check_integrity
from kanso.criteria.objectives import measures_benchmark
from kanso.data.instruments import resolve_universe
from kanso.data.manifest import catalog_path
from kanso.data.snapshot import covering
from kanso.env import read as read_envelope
from kanso.errors import KansoError, PreconditionError, ValidationError
from kanso.hyp import (
    HYPOTHESIS_FILE,
    PROGRAM_FILE,
    STRATEGY_FILE,
    Registration,
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
    "SAME_BOOK",
    "RedundantError",
    "Setup",
    "begin",
    "card",
    "end",
]

BASELINE: Final = "baseline"
"""The description the run's first card carries; every later one is the researcher's."""

MIN_CARD_BUDGET_S: Final = 60.0
"""No card budget is shorter than this, however fast the baseline was."""

HEADROOM: Final = 3.0
"""What a card is allowed over what the baseline needed, in time and in memory alike."""

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

        Nothing else `[research]` declares reaches a card's number. `annualisation`,
        `account` and `currency` look as though they would and do not: this package reads
        none of the three anywhere, which
        `tests/research/test_loop.py::test_the_settings_the_digest_leaves_out_move_no_number`
        measures by running the same card under all three changed and reading back the
        same metric. Wiring one of them is what makes that test fail, and it belongs here
        on the same day.
        """
        digest = sha256()
        measured: tuple[object, ...] = (
            self.capital,
            self.folds,
            self.period,
            self.venue_model.model_dump(mode="json"),
            None if self.harness.host is None else self.harness.host.version,
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

    The universe is resolved for the venue model and recorded nowhere: the card is priced
    under the definitions the store holds, which its snapshot pins, and a definition
    written here would move the store under that pin without changing what the card runs
    against. Only `kanso data instruments resolve` writes the store.
    """
    ref = hyp.construct
    if ref is None:
        raise PreconditionError(
            f"{hyp.id} is not classified, so there is no construct to research it as",
            remedy=f"run `kanso classify {hyp.id}`",
        )
    impl = construct_for(ref.id, ws)
    harness = impl.harness(hyp, _host(ws, hyp), version=version)
    instruments = resolve_universe(ws, hyp.universe, hyp.windows.research.start, record=False)
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
        result = backtest.run_subprocess(request, setup.catalog, directory)
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


def _benchmark_run(setup: Setup, *, snapshot_id: str, cache: dict[str, CardRun]) -> CardRun | None:
    """The hold a benchmark objective differences against, computed once per run.

    Run in this process over the research window, as a composition or a certification run
    is: the hold is kanso's own sleeve, so there is nothing to confine, and its request is
    the card's with only the strategy replaced — the same snapshot, warmup prefix, money
    and grains. Keyed by the snapshot and the prefix, because those are what a card's data
    is; `None` for an objective that measures no benchmark.
    """
    if not measures_benchmark(setup.hyp):
        return None
    key = f"{BENCHMARK_KEY}@{snapshot_id}@{setup.prefix}"
    if key not in cache:
        request = _request(setup, b"", snapshot_id, budget_s=None, mem_cap_gb=None)
        cache[key] = backtest.run(backtest.benchmark(request), setup.catalog).run
    return cache[key]


def _mem_cap(ws: Workspace, run: RunRecord) -> float:
    """What a card of this run may hold resident: the lane's share, never below the floor.

    The floor is three times what the baseline actually needed, so a run whose own
    starting point is heavier than the lane plan expected still gets cards rather than a
    string of kills. The lane's share is `[env] mem_per_lane_gb` where one is declared,
    which is the only way this threshold falls under the plan's own 4 GB floor
    (`docs/workspace.md`, `envelope.yaml`).
    """
    floor = HEADROOM * run.baseline_peak_mem_gb
    envelope = read_envelope(ws)
    return floor if envelope is None else max(envelope.plan.mem_per_lane_gb, floor)


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
) -> list[GateResult]:
    """Every card-stage gate the classification chose, apart from the one already run."""
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
        )
        results.append(registry[ref.id].evaluate(ctx))
    return results


def _measure(
    setup: Setup,
    card_run: CardRun,
    host_run: CardRun | None,
    benchmark_run: CardRun | None = None,
) -> tuple[float, float]:
    """The objective and its standard error over the run's folds."""
    ref = setup.hyp.objective
    if ref is None:  # pragma: no cover - a classified hypothesis always carries one
        return 0.0, 0.0
    return objectives()[ref.id].compute(card_run, setup.folds, host_run, benchmark_run)


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
            gate_results=[integrity],
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
    )
    metric, se = _measure(setup, result.run, host_run, benchmark_run)
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
    if not like.agreed:
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
    covers. A baseline that will not run leaves no record and no lane directory.
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
    setup = _setup(ws, store, hyp)
    prefixes = _warmup_spans(setup)
    snapshot = covering(
        ws, hyp.universe, hyp.data_requirements, hyp.resolution, hyp.windows, prefixes
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
        host_run = _host_run(
            setup,
            snapshot_id=snapshot.snapshot_id,
            budget_s=float(ws.config.research.baseline_budget_s),
            directory=directory,
            cache=host_cache,
        )
        benchmark_run = _benchmark_run(setup, snapshot_id=snapshot.snapshot_id, cache=host_cache)
        result = _baseline(ws, setup, snapshot.snapshot_id, directory, pins, from_best=from_best)
        _admitted(store, hyp_id, lane)  # the baseline took minutes; the operator may have acted
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
        directory, dict(pins), sized=setup.hyp.sizing is not None, construct=setup.construct
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
    host_run = _host_run(
        setup,
        snapshot_id=run.snapshot_id,
        budget_s=run.card_budget_s,
        directory=directory,
        cache=_HOST_RUNS.setdefault(run.run_id, {}),
    )
    benchmark_run = _benchmark_run(
        setup, snapshot_id=run.snapshot_id, cache=_HOST_RUNS.setdefault(run.run_id, {})
    )
    result = backtest.run_subprocess(
        _request(
            setup,
            source,
            run.snapshot_id,
            budget_s=run.card_budget_s,
            mem_cap_gb=_mem_cap(ws, run),
        ),
        setup.catalog,
        directory,
    )
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
    )


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
        raise PreconditionError(
            f"{hyp_id} was taken out of the queue while lane {lane} held it, so this lane "
            "does not begin its run",
            remedy=f"`kanso research queue add {hyp_id}` when you want it researched again",
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
