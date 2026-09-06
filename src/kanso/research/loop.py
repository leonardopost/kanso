"""The research loop: begin a run, evaluate a card, end the run.

One hypothesis, one lane, one pinned snapshot, and a file that only ever improves. The
loop is deliberately small, because everything that makes it trustworthy lives elsewhere
and is merely sequenced here.

**A run pins what it is judged against.** `begin` copies `hypothesis.yaml` and
`program.md` into the lane directory and stores both as blobs; every later card is
evaluated against the pinned bytes rather than the workspace's, so editing the hypothesis
mid-run changes nothing except the card that notices. `strategy.py` starts from the
hypothesis's best blob when there is one — research resumes where it left off — and from
the workspace only when the operator asks for it, which also clears the best, because
starting from a worse file and keeping the old best would compare two ancestries.

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
bytes are a blob; a keep rewrites `hypotheses/<id>/strategy.py`, and anything else
restores the lane copy from the best blob, or from the run's base before the first keep.
`results.tsv` is rendered from the records afterwards, so no restore can lose history.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

from kanso.classify.construct import Construct, Harness, HostRef
from kanso.classify.construct import get as construct_for
from kanso.criteria import CardRun, GateContext, criteria_version, gates, objectives
from kanso.criteria.gates import strategy_integrity
from kanso.criteria.integrity import check as check_integrity
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
    hypothesis_dir,
    hypothesis_file,
    read_source,
    set_status,
    show,
    venue_models,
)
from kanso.nautilus import backtest
from kanso.research import lanes, records
from kanso.research.keep import grew_by as lines_added
from kanso.research.keep import keep as keep_rule
from kanso.research.results import write_results
from kanso.schemas import (
    Card,
    CardStatus,
    GateResult,
    Hypothesis,
    RunRecord,
    StrategyFile,
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
    "RESEARCHABLE",
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

RESEARCHABLE: Final = frozenset({"classified", "researching", "candidate", "certified"})
"""The statuses a run may begin from: a draft is unclassified, and the rest are over."""

BEGUN: Final = "run_begun"
CARDED: Final = "card"
ENDED: Final = "run_ended"
BASELINE_FAILED: Final = "baseline_failed"
"""The event kinds this module appends, all under the hypothesis id as subject."""

RESEARCHING: Final = "researching"

_HOST_RUNS: dict[str, dict[str, CardRun]] = {}
"""Host-alone runs, by run and then by snapshot and host version. The same host over the
same data gives the same run for every card of a run, so it is computed once and reused;
`end` drops the run's entry, so a process that researches all day holds one per live run."""


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

    @property
    def window(self) -> tuple[date, date]:
        """The research window: the only window a card is ever run over."""
        research = self.hyp.windows.research
        return research.start, research.end

    @property
    def construct(self) -> str:
        return self.harness.construct


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
    return Setup(
        hyp=hyp,
        harness=harness,
        impl=impl,
        venue_model=model,
        capital=hyp.capital or research.capital,
        folds=research.folds,
        period=research.return_period,
        max_lines=research.max_lines_per_keep,
        catalog=catalog_path(ws),
        host_source=host_source,
        host_modifiers=modifiers,
    )


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
    )


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
        if result.crashed:
            raise PreconditionError(
                f"host: {ref.strategy_id} version {ref.version} did not run over the research "
                f"window ({result.reason}), so there is nothing to measure against",
                remedy="re-certify the host, or attach this construct to another one",
            )
        return result.run

    return setup.impl.host_run(host, snapshot_id, compute, cache)


def _mem_cap(ws: Workspace, run: RunRecord) -> float:
    """What a card of this run may hold resident: the lane's share, never below the floor.

    The floor is three times what the baseline actually needed, so a run whose own
    starting point is heavier than the lane plan expected still gets cards rather than a
    string of kills.
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
    n_trials: int,
    directory: Path,
    params: Mapping[str, Any] | None = None,
) -> GateContext:
    return GateContext(
        hyp=setup.hyp,
        construct=setup.construct,
        stage="card",
        params=dict(params or {}),
        window=setup.window,
        run=card_run,
        host_run=host_run,
        research_folds=setup.folds,
        n_trials=n_trials,
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
        n_trials=1,
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
    n_trials: int,
    directory: Path,
) -> list[GateResult]:
    """Every card-stage gate the classification chose, apart from the one already run."""
    registry = gates()
    results: list[GateResult] = []
    for ref in setup.hyp.constraints or []:
        if ref.id == strategy_integrity.id:
            continue
        ctx = _context(
            setup,
            run,
            card_run,
            host_run=host_run,
            strategy_sha=strategy_sha,
            n_trials=n_trials,
            directory=directory,
            params=ref.params,
        )
        results.append(registry[ref.id].evaluate(ctx))
    return results


def _measure(setup: Setup, card_run: CardRun, host_run: CardRun | None) -> tuple[float, float]:
    """The objective and its standard error over the run's folds."""
    ref = setup.hyp.objective
    if ref is None:  # pragma: no cover - a classified hypothesis always carries one
        return 0.0, 0.0
    return objectives()[ref.id].compute(card_run, setup.folds, host_run)


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
    restore_all: bool = False,
) -> Card:
    """Write the card, then move `best` or restore the lane copy, then render the log."""
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
        gate_results=list(gate_results),
        crash_tail=crash_tail,
        venue_model=setup.venue_model,
        created_at=records.now(),
    )
    records.record_card(store, run, made)
    if status == "keep":
        records.set_best(store, run, strategy_sha, metric)
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
) -> Card:
    """Steps 3 to 5: the constraints, the keep rule and the record."""
    n_trials = records.n_trials(store, run.hyp_id) + 1
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
        )
    constraints = _constraints(
        setup,
        run,
        result.run,
        host_run=host_run,
        strategy_sha=strategy_sha,
        n_trials=n_trials,
        directory=directory,
    )
    metric, se = _measure(setup, result.run, host_run)
    passed = integrity.passed and all(gate.passed for gate in constraints)
    kept = passed and _keeps(setup, store, run, source, metric, se)
    return _record(
        ws,
        store,
        setup,
        run,
        strategy_sha=strategy_sha,
        source=source,
        desc=desc,
        status="keep" if kept else "discard",
        metric=metric,
        se=se,
        n_trades=len(result.run.trades),
        wall_s=result.wall_s,
        peak_mem_gb=result.peak_mem_gb,
        n_trials=n_trials,
        gate_results=[integrity, *constraints],
        crash_tail=None,
        directory=directory,
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
    snapshot = covering(ws, hyp.universe, hyp.data_requirements, hyp.resolution, hyp.windows)
    if snapshot is None:
        raise PreconditionError(
            f"no snapshot covers {', '.join(hyp.universe)} over the research and certification "
            f"windows at {hyp.resolution}",
            remedy="load the data and run `kanso data snapshot`",
        )
    program = hypothesis_dir(ws, hyp_id) / PROGRAM_FILE
    if not program.is_file():
        raise PreconditionError(
            f"{program} is missing, and a run pins the program it follows",
            remedy=f"run `kanso hyp new {hyp_id}` in another directory and copy program.md over",
        )
    base, from_best = _base_source(ws, store, hyp_id, from_workspace=from_workspace)
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
        result = _baseline(ws, setup, snapshot.snapshot_id, directory, pins, from_best=from_best)
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
    store.event(BEGUN, hyp_id, {"run_id": run.run_id, "tag": run.tag, "lane": lane})
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
    )
    return records.require_active(store, hyp_id)


def _base_source(
    ws: Workspace, store: StateStore, hyp_id: str, *, from_workspace: bool
) -> tuple[bytes, bool]:
    """The `strategy.py` a run starts from, and whether it is the hypothesis's best.

    The best blob when one exists and the workspace copy otherwise; `--from-workspace`
    takes the workspace copy regardless and clears the best, so the history says the
    run started over.
    """
    best, _ = records.best_of(store, hyp_id)
    if best is not None and not from_workspace:
        return store.get_blob(best), True
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
    return path.read_bytes(), False


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
    problems = check_integrity(directory, dict(pins))
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
) -> Card:
    """Evaluate the lane directory's `strategy.py` as one card of the active run.

    Stores the bytes, checks the static half of `strategy_integrity` before anything
    runs, backtests the research window in a subprocess under the run's budgets,
    evaluates the constraints and the keep rule, and records the card.
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
            restore_all=True,
        )
    host_run = _host_run(
        setup,
        snapshot_id=run.snapshot_id,
        budget_s=run.card_budget_s,
        directory=directory,
        cache=_HOST_RUNS.setdefault(run.run_id, {}),
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
