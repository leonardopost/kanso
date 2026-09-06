"""Running a certification plan: what is judged, on what data, and what the verdict does.

Certification is the one place a strategy meets the window that was kept away from it.
The subject is a card of the hypothesis — its `best`, or a named one — and it is judged
over what it was researched over: the same snapshot the run that produced it was pinned
to, the same venue model that card was costed with, the same hypothesis bytes the run
pinned. The capital, the fold count and the return period are not on the run record, so
those three are read from `kanso.toml` as it stands now — the same reading research
makes — and both windows are run under that one reading.

**Both windows are run, always.** The certification window is what the plan's gates judge;
the research window is run beside it because the gates that matter most compare the two,
and an out-of-sample number means nothing beside an in-sample one measured under different
conditions. A relative construct adds its host's run over each window, since its objective
is a difference.

**An inadmissible snapshot fails the plan rather than raising.** A snapshot holding a
dataset whose publication nobody declared, or one whose prices a vendor adjusted as of the
day they were requested, cannot support a point-in-time claim: the first cannot be dated,
the second cannot be fetched again. Certification does not run on it and does not exit
with a code either — every gate of the plan is recorded as failed with the reason, so the
verdict is an ordinary failing verdict that counts toward the failure run and reaches the
operator through the escalation everything else uses. That is the difference between a
gate that cannot reach its context, which passes and says so, and one whose context is
present but inadmissible, which fails.

**A gate that perturbs a parameter runs the window again through this runner.** The
certification window's instruments and points are already in hand, so a perturbation costs
one engine run and no catalog read, and the perturbed run is extracted by exactly the
arithmetic the unperturbed one was. Only the subject's own parameters can move — a
sleeve's own configuration fields, or the construct parameters an attached modifier was
composed with — never the capital, the risk limits or anything else the hypothesis injects.

The verdict drives the lifecycle and nothing else does: a pass certifies the hypothesis, a
fail returns it to research with its failing gates recorded where the proposer reads them,
and the configured number of consecutive failures ends it and escalates. A pass also
composes the version the certificate implies and offers it to the paper stage, because both
acts follow from the certificate with no decision left in them; a stage that cannot take it
escalates and the verdict still stands.

Engine facts this module relies on (nautilus_trader 1.231.0): a backtest is built and run
in this process by `kanso.nautilus.backtest`, whose window refusal is what keeps a
certification on the window it asked for; every data point carries `ts_event` and
`ts_init` as nanosecond integers, and the difference between them is the publication delay
the availability gate measures.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import inf
from typing import TYPE_CHECKING, Any, Final, cast

from kanso.certify import certificate
from kanso.certify.plan import plan as plan_for
from kanso.classify.construct import Harness
from kanso.classify.construct import get as construct_for
from kanso.criteria import CardRun, DatasetFacts, GateContext, criteria_version, gates, objectives
from kanso.criteria.run import day_of
from kanso.data.manifest import Manifest, catalog_path, manifests
from kanso.data.publication import NANOS_PER_SECOND, PUBLICATION_RULES
from kanso.data.snapshot import Snapshot
from kanso.data.snapshot import read as read_snapshot
from kanso.data.types import type_id_of
from kanso.env.envelope import engine_version
from kanso.errors import KansoError, PreconditionError
from kanso.hyp import HYPOTHESIS_FILE, Registration, set_status
from kanso.hyp import show as registration_of
from kanso.inbox import escalate
from kanso.nautilus import backtest
from kanso.replay.parity import Parity
from kanso.replay.parity import parity as replay_parity
from kanso.research import records
from kanso.research.lanes import DEFAULT_LANE
from kanso.research.loop import RESEARCHABLE
from kanso.schemas import (
    Certificate,
    CertificationPlan,
    ConstructRef,
    EvaluatedGate,
    GateResult,
    Hypothesis,
    ObjectiveResult,
    PlannedGate,
    RunRecord,
    StrategyFile,
    VenueModel,
    Verdict,
    load_yaml,
    parse_yaml,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["certify", "show"]

CERT_STAGE: Final = "cert"
"""The plan stage certification runs; the other two belong to a deployed stage."""

CANDIDATE: Final = "candidate"
CERTIFIED: Final = "certified"
RESEARCHING: Final = "researching"
FAILED: Final = "failed"

CERTIFICATE: Final = "certificate"
"""The event kind a finished certification appends, under the hypothesis id."""

CERT_FAILED: Final = "cert_failed"
"""The escalation a hypothesis that has run out of attempts raises."""

PARITY: Final = "parity_replay"
"""The one cert gate whose evidence is a replay rather than a run of the window."""


# --- what is being certified --------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """One card of one hypothesis, with everything needed to run it again.

    The bytes, the snapshot and the venue model come from the record of the run that
    produced the card rather than from the workspace as it stands; the capital, the fold
    count and the return period come from `kanso.toml` today, because the run record does
    not carry them. Both windows are run under the same values either way, so the gate
    that compares the certification window against the research one compares like with
    like.
    """

    hyp: Hypothesis
    construct: ConstructRef
    objective_id: str
    strategy_sha: str
    source: bytes
    snapshot_id: str
    harness: Harness
    venue_model: VenueModel
    capital: float
    folds: int
    period: str
    catalog: Path
    host_source: bytes | None = None
    host_modifiers: tuple[tuple[str, bytes, Mapping[str, Any]], ...] = ()

    @property
    def certification(self) -> tuple[date, date]:
        """The embargoed window the plan's gates judge."""
        window = self.hyp.windows.certification
        return window.start, window.end

    @property
    def research(self) -> tuple[date, date]:
        """The window the subject was selected on, run again for the gates that compare."""
        window = self.hyp.windows.research
        return window.start, window.end


@dataclass(frozen=True)
class Measured:
    """What one certification ran: both windows, the host beside them, and the points.

    The resolved instruments and the window's points are kept because a gate that
    perturbs a parameter runs the certification window again, and re-reading the catalog
    per perturbation would make a cheap arithmetic question an expensive one.
    """

    certification: CardRun
    research: CardRun
    groups: tuple[tuple[object, ...], ...]
    host_certification: CardRun | None = None
    host_research: CardRun | None = None
    instruments: tuple[object, ...] = ()


# --- the entry points ---------------------------------------------------------


def certify(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    sha: str | None = None,
    lane: str = DEFAULT_LANE,
) -> Certificate:
    """Run the plan's certification gates for one card of a hypothesis and record the verdict.

    Plans first when no plan is pinned, and runs the plan that is pinned otherwise, which
    is also where a plan this version can no longer run is refused. Refuses when this
    subject was already certified under this plan version and this engine version, and
    when the target file exists, because a certificate is immutable. The verdict passes
    when every gate that judged something passed.
    """
    registration = _registration(ws, store, hyp_id)
    _certifiable(registration)
    subject = _subject(ws, store, hyp_id, sha)
    plan = plan_for(ws, store, hyp_id, lane=lane)
    engine = engine_version()
    n_trials = records.n_trials(store, hyp_id)
    certificate.refuse_repeat(
        ws,
        store,
        hyp_id,
        strategy_sha=subject.strategy_sha,
        n_trials=n_trials,
        plan_version=plan.plan_version,
        nautilus_version=engine,
    )
    if registration.status != CANDIDATE:
        set_status(store, hyp_id, CANDIDATE)
    evaluated, objective = _judge(ws, store, plan, subject, n_trials)
    made = Certificate(
        hyp_id=hyp_id,
        strategy_sha=subject.strategy_sha,
        nautilus_version=engine,
        venue_model=subject.venue_model,
        snapshot_id=subject.snapshot_id,
        criteria_version=criteria_version(),
        plan_version=plan.plan_version,
        construct=subject.construct,
        objective=objective,
        gates=evaluated,
        n_trials=n_trials,
        verdict=_verdict(evaluated),
        created_at=datetime.now(tz=UTC),
    )
    certificate.write(ws, store, made, subject.source)
    _apply(ws, store, made)
    return made


def show(ws: Workspace, store: StateStore, hyp_id: str) -> Certificate | None:
    """This hypothesis's newest certificate, or `None` when it has none."""
    _registration(ws, store, hyp_id)
    return certificate.latest(store, hyp_id)


# --- resolving the subject ----------------------------------------------------


def _registration(ws: Workspace, store: StateStore, hyp_id: str) -> Registration:
    return cast("Registration", registration_of(ws, store, hyp_id))


def _certifiable(registration: Registration) -> None:
    """Refuse a hypothesis whose life has ended, or which was never classified."""
    if registration.status not in RESEARCHABLE:
        raise PreconditionError(
            f"{registration.hyp_id} is {registration.status}, and certification runs on "
            f"{', '.join(sorted(RESEARCHABLE))}",
            remedy=f"a draft is classified with `kanso classify {registration.hyp_id}`; a "
            "hypothesis that is over is replaced by a new one",
        )


def _subject(ws: Workspace, store: StateStore, hyp_id: str, sha: str | None) -> Subject:
    """The card to certify, and the run record that says what it was measured against."""
    chosen = _chosen_sha(store, hyp_id, sha)
    cards = [card for card in records.cards_of(store, hyp_id) if card.strategy_sha == chosen]
    card = cards[-1]
    run = _run_of(store, hyp_id, card.run_id)
    hyp = parse_yaml(
        Hypothesis, store.get_blob(run.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    if hyp.construct is None or hyp.objective is None:
        raise PreconditionError(
            f"{hyp_id} was researched without a construct or an objective, so there is nothing "
            "to certify it as",
            remedy=f"run `kanso classify {hyp_id}` and research it again",
        )
    harness = construct_for(hyp.construct.id, ws).harness(
        hyp, _host(ws, hyp.construct), version=run.host_version
    )
    host_source, modifiers = (
        (None, ()) if harness.host is None else records.host_sources(store, harness.host)
    )
    return Subject(
        hyp=hyp,
        construct=hyp.construct,
        objective_id=hyp.objective.id,
        strategy_sha=chosen,
        source=store.get_blob(chosen),
        snapshot_id=run.snapshot_id,
        harness=harness,
        venue_model=card.venue_model,
        capital=hyp.capital or ws.config.research.capital,
        folds=ws.config.research.folds,
        period=ws.config.research.return_period,
        catalog=catalog_path(ws),
        host_source=host_source,
        host_modifiers=modifiers,
    )


def _chosen_sha(store: StateStore, hyp_id: str, sha: str | None) -> str:
    """The hypothesis's `best`, or the one card sha the given prefix names."""
    if sha is not None:
        return records.card_sha(store, hyp_id, sha)
    best, _ = records.best_of(store, hyp_id)
    if best is None:
        raise PreconditionError(
            f"{hyp_id} has no best card, so there is nothing to certify",
            remedy=f"research it with `kanso research begin {hyp_id}` until a card keeps",
        )
    return best


def _run_of(store: StateStore, hyp_id: str, run_id: str) -> RunRecord:
    """The run that produced the subject card, which is what pinned its data."""
    found = next((run for run in records.runs_of(store, hyp_id) if run.run_id == run_id), None)
    if found is None:  # pragma: no cover - a card's run_id is a foreign key into runs
        raise PreconditionError(f"{hyp_id} has no run {run_id}")
    return found


def _host(ws: Workspace, ref: ConstructRef) -> StrategyFile | None:
    """The certified host strategy a relative construct attaches to, when it names one."""
    if ref.host is None:
        return None
    path = ws.path("strategies", ref.host, "strategy.yaml")
    if not path.is_file():
        raise PreconditionError(
            f"construct.host: {ref.host!r} is not a composed strategy of this workspace",
            remedy="certify and compose the host sleeve before certifying against it",
        )
    return load_yaml(StrategyFile, path)


# --- running both windows -----------------------------------------------------


def _request(
    subject: Subject,
    window: tuple[date, date],
    *,
    host_only: bool = False,
    overrides: Mapping[str, float] | None = None,
) -> backtest.RunRequest:
    """The backtest one window asks for: the sleeve, or the host with this modifier on it.

    `overrides` moves the subject's own parameters and nothing else. For an absolute
    construct the subject is the sleeve, so they replace fields of its configuration; for
    a relative one the subject is the attached modifier, so they replace the construct
    parameters it was attached with, and the host underneath it is left exactly as it was
    certified.
    """
    moved = dict(overrides or {})
    attached: Sequence[tuple[str, bytes, Mapping[str, Any]]] = ()
    if subject.host_source is None:
        strategy, own = subject.source, moved
    else:
        strategy, own = subject.host_source, {}
        attached = (
            subject.host_modifiers
            if host_only
            else (
                *subject.host_modifiers,
                (
                    subject.harness.construct,
                    subject.source,
                    {**dict(subject.construct.params or {}), **moved},
                ),
            )
        )
    return backtest.RunRequest(
        hyp=subject.hyp,
        strategy_source=strategy,
        window=window,
        snapshot_id=subject.snapshot_id,
        venue_model=subject.venue_model.model_dump(),
        capital=subject.capital,
        modifiers=attached,
        period=subject.period,
        overrides=own,
    )


def _tunable(subject: Subject) -> dict[str, float]:
    """The subject's own numeric parameters, at the values it was certified at.

    A sleeve's are the numeric fields its configuration class declares beyond the ones the
    hypothesis injects; an attached construct's are the numeric construct parameters it was
    composed with, which is the whole of what a modifier is configured by.
    """
    if subject.host_source is None:
        return backtest.tunable(_request(subject, subject.certification))
    return {
        name: value
        for name, value in (subject.construct.params or {}).items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _measure(subject: Subject) -> Measured:
    """Run the subject over both windows, with the host beside it for a relative construct.

    The certification window's data is loaded here rather than inside the runner's own
    entry point, because those points are also what the availability gate measures its
    delays on and what the capacity gate compares a day's trading against.

    Certification runs in this process rather than in a child of its own: the subject is a
    card that already passed the integrity gate and already ran, and the window it is run
    over is fixed here rather than chosen by it. A strategy that nevertheless raises stops
    the certification and says so, so the failure reaches the operator as a refusal rather
    than as a verdict — a card that will not run has not failed a test, it has not taken
    one — and the daemon lane that asked for it survives to research something else.
    """
    request = _request(subject, subject.certification)
    backtest.stage_of(subject.hyp, subject.certification)
    instruments, groups = backtest.window_data(request, subject.catalog)
    certification = _ran(subject, lambda: backtest.execute(request, instruments, groups))
    research = _ran(
        subject, lambda: backtest.run(_request(subject, subject.research), subject.catalog)
    )
    host_certification: CardRun | None = None
    host_research: CardRun | None = None
    if subject.harness.relative:
        host_certification = _ran(
            subject,
            lambda: backtest.run(
                _request(subject, subject.certification, host_only=True), subject.catalog
            ),
        )
        host_research = _ran(
            subject,
            lambda: backtest.run(
                _request(subject, subject.research, host_only=True), subject.catalog
            ),
        )
    return Measured(
        certification=certification,
        research=research,
        groups=groups,
        host_certification=host_certification,
        host_research=host_research,
        instruments=instruments,
    )


def _ran(subject: Subject, backtest_run: Callable[[], backtest.RunResult]) -> CardRun:
    """One backtest, with an exception turned into a refusal that names what raised."""
    try:
        return backtest_run().run
    except KansoError:
        raise
    except Exception as exc:
        raise PreconditionError(
            f"{subject.strategy_sha[:7]} raised while certification ran it: "
            f"{type(exc).__name__}: {exc}",
            remedy=f"research {subject.hyp.id} further, or certify a card that runs",
        ) from exc


# --- what the pinned data supports --------------------------------------------


def _inadmissible(ws: Workspace, snapshot: Snapshot) -> list[str]:
    """Why this snapshot cannot support a point-in-time claim, or nothing when it can."""
    held = manifests(ws)
    reasons: list[str] = []
    for name in snapshot.datasets:
        manifest = held.get(name)
        if manifest is None:
            reasons.append(f"{name}: this workspace no longer describes it")
        elif manifest.publication == "unknown":
            reasons.append(f"{name}: nobody declared when its points became public")
        elif manifest.adjusted:
            reasons.append(
                f"{name}: its prices are vendor-adjusted as of "
                f"{manifest.as_of or 'the day they were requested'}, so the same request "
                "returns different numbers after the next corporate action"
            )
    if not reasons and not snapshot.reproducible:
        reasons.append(f"{snapshot.snapshot_id[:12]}: the snapshot records itself irreproducible")
    return reasons


def _dataset_facts(
    ws: Workspace, snapshot: Snapshot, groups: Sequence[Sequence[object]]
) -> tuple[DatasetFacts, ...]:
    """Each pinned dataset with the delay it was published under and the one its class needs.

    The observed delay is the smallest `ts_init - ts_event` seen in the window that was
    just run, per instrument and type; a dataset the window held no point of is recorded
    as infinitely late, since nothing in it was published too early.
    """
    observed = _observed_lags(groups)
    held = manifests(ws)
    return tuple(
        DatasetFacts(
            dataset_id=name,
            type=held[name].type,
            publication=held[name].publication,
            min_lag_s=observed.get((held[name].instrument, held[name].type), inf),
            required_lag_s=_required_lag(held[name]),
        )
        for name in snapshot.datasets
    )


def _observed_lags(groups: Sequence[Sequence[object]]) -> dict[tuple[str, str], float]:
    """The smallest publication delay observed, per instrument and data type, in seconds."""
    lags: dict[tuple[str, str], float] = {}
    for group in groups:
        for point in group:
            stamped = cast("Any", point)
            key = (backtest._instrument_of(point) or "", type_id_of(point))
            delay = (int(stamped.ts_init) - int(stamped.ts_event)) / NANOS_PER_SECOND
            lags[key] = min(lags.get(key, inf), delay)
    return lags


def _required_lag(manifest: Manifest) -> float:
    """The delay this dataset's class is documented to have, in seconds; zero when none is."""
    rule = PUBLICATION_RULES.get(manifest.publication_rule or "")
    if rule is None or rule.lag is None:
        return 0.0
    return rule.lag.total_seconds()


def _daily_volume(groups: Sequence[Sequence[object]]) -> dict[str, list[float]]:
    """Each instrument's daily traded notional over the window, oldest first."""
    from nautilus_trader.model.data import Bar

    per_day: dict[str, dict[date, float]] = {}
    for group in groups:
        for point in group:
            if not isinstance(point, Bar):
                continue
            days = per_day.setdefault(str(point.bar_type.instrument_id), {})
            day = day_of(int(point.ts_event))
            days[day] = days.get(day, 0.0) + float(point.volume) * float(point.close)
    return {name: [days[day] for day in sorted(days)] for name, days in per_day.items()}


# --- the two code paths -------------------------------------------------------


def _parity(
    ws: Workspace, store: StateStore, plan: CertificationPlan, subject: Subject
) -> Parity | None:
    """Replay the subject on both code paths over the certification window, if asked to.

    The parity gate compares what the live path and the research path decided, and nothing
    but a replay can produce that: it is run here, once, only when the plan names the gate,
    and over the window the plan judges rather than the forward one a bare replay defaults
    to.

    A replay that cannot be set up at all — a host that was never composed, a window the
    catalog cannot serve — leaves the gate without its evidence rather than ending the
    certification. The gate then skips and says so, which is the same honesty every other
    gate without its context observes, and the certificate records that nothing compared
    the paths instead of claiming that they agreed.
    """
    if not any(gate.id == PARITY for gate in plan.stage_gates(CERT_STAGE)):
        return None
    opens, closes = subject.certification
    try:
        return replay_parity(
            ws,
            store,
            hyp=subject.hyp.id,
            sha=subject.strategy_sha,
            start=opens,
            end=closes,
        )
    except KansoError:
        return None


# --- judging ------------------------------------------------------------------


def _judge(
    ws: Workspace,
    store: StateStore,
    plan: CertificationPlan,
    subject: Subject,
    n_trials: int,
) -> tuple[list[EvaluatedGate], ObjectiveResult]:
    """Evaluate the plan's certification gates, and measure the objective they judged."""
    planned = plan.stage_gates(CERT_STAGE)
    if not planned:  # pragma: no cover - the planner refuses a plan that reaches no stage
        raise PreconditionError(
            f"plan version {plan.plan_version} of {plan.hyp_id} names no certification gate",
            remedy=f"replan with `kanso cert plan {plan.hyp_id} --replan`",
        )
    objective = objectives()[subject.objective_id]
    snapshot = read_snapshot(ws, subject.snapshot_id)
    blocked = _inadmissible(ws, snapshot)
    if blocked:
        return (
            [_blocked(gate, snapshot, blocked) for gate in planned],
            ObjectiveResult(id=objective.id, value=0.0, se=0.0),
        )
    measured = _measure(subject)
    compared = _parity(ws, store, plan, subject)
    value, se = objective.compute(
        measured.certification, subject.folds, measured.host_certification
    )
    metrics = [
        card.metric for card in records.cards_of(store, subject.hyp.id) if card.status != "crash"
    ]
    facts = _dataset_facts(ws, snapshot, measured.groups)
    volume = _daily_volume(measured.groups)
    parameters = _tunable(subject)

    def rerun(overrides: Mapping[str, float]) -> CardRun:
        """The subject over the certification window again, with parameters moved."""
        request = _request(subject, subject.certification, overrides=overrides)
        return _ran(
            subject, lambda: backtest.execute(request, measured.instruments, measured.groups)
        )

    registry = gates()
    evaluated: list[EvaluatedGate] = []
    for gate in planned:
        context = GateContext(
            hyp=subject.hyp,
            construct=subject.harness.construct,
            stage=CERT_STAGE,
            params=dict(gate.params),
            window=subject.certification,
            run=measured.certification,
            host_run=measured.host_certification,
            research_folds=subject.folds,
            n_trials=n_trials,
            snapshot_id=subject.snapshot_id,
            strategy_sha=subject.strategy_sha,
            research_run=measured.research,
            host_research_run=measured.host_research,
            card_metrics=metrics,
            datasets=facts,
            daily_volume=volume,
            tunable=parameters,
            rerun=rerun,
            session=compared,
        )
        evaluated.append(_evaluated(gate, registry[gate.id].evaluate(context)))
    return evaluated, ObjectiveResult(id=objective.id, value=value, se=se)


def _evaluated(planned: PlannedGate, result: GateResult) -> EvaluatedGate:
    """One gate's verdict, with the values it was given and the evidence it produced."""
    return EvaluatedGate.model_validate(
        {
            "id": result.id,
            "stage": planned.stage,
            "params": dict(planned.params),
            "evidence": dict(result.evidence),
            "pass": result.passed,
            "skipped": result.skipped,
        }
    )


def _blocked(planned: PlannedGate, snapshot: Snapshot, reasons: Sequence[str]) -> EvaluatedGate:
    """A gate that would have judged data nothing can vouch for, recorded as failed."""
    return EvaluatedGate.model_validate(
        {
            "id": planned.id,
            "stage": planned.stage,
            "params": dict(planned.params),
            "evidence": {"snapshot_id": snapshot.snapshot_id, "inadmissible": list(reasons)},
            "pass": False,
        }
    )


def _verdict(evaluated: Sequence[EvaluatedGate]) -> Verdict:
    """A pass exactly when every gate that judged something passed."""
    judged = [gate for gate in evaluated if gate.skipped is None]
    return "pass" if all(gate.passed for gate in judged) else "fail"


# --- what the verdict does ----------------------------------------------------


def _apply(ws: Workspace, store: StateStore, made: Certificate) -> None:
    """Move the hypothesis, and escalate when it has run out of attempts."""
    hyp_id = made.hyp_id
    store.event(
        CERTIFICATE,
        hyp_id,
        {
            "sha": made.sha7,
            "verdict": made.verdict,
            "plan_version": made.plan_version,
            "nautilus_version": made.nautilus_version,
        },
    )
    if made.verdict == "pass":
        _set_failures(store, hyp_id, 0)
        set_status(store, hyp_id, CERTIFIED)
        # Composition reads certificates; this is the one call back the other way, so the
        # import is deferred and the cycle exists only while a verdict is being applied.
        from kanso.portfolio.lifecycle import on_certified

        on_certified(ws, store, made)
        return
    failures = _set_failures(store, hyp_id, _failures(store, hyp_id) + 1)
    if failures < ws.config.certify.n_fail:
        set_status(store, hyp_id, RESEARCHING)
        return
    set_status(store, hyp_id, FAILED)
    failing = ", ".join(gate.id for gate in made.gates if gate.skipped is None and not gate.passed)
    escalate(
        ws,
        store,
        CERT_FAILED,
        hyp_id,
        f"{failures} consecutive certification failures; {made.sha7} failed on {failing}",
    )


def _failures(store: StateStore, hyp_id: str) -> int:
    """How many certifications of this hypothesis have failed in a row."""
    row = store.connection.execute(
        "SELECT consecutive_cert_failures FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return int(row[0])


def _set_failures(store: StateStore, hyp_id: str, count: int) -> int:
    store.connection.execute(
        "UPDATE hypotheses SET consecutive_cert_failures = ? WHERE hyp_id = ?", (count, hyp_id)
    )
    return count
