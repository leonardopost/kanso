"""Composition: what a passing certificate turns into, and what is measured of it.

A certificate says a hypothesis survived its embargo. Composition turns that into
something a stage can hold. The construct decides the shape and this module decides
nothing about it: a sleeve becomes a new strategy at version 1, an attached construct
becomes its host's next version with itself appended, and everything else here serves
those two.

**The version records what it was certified under.** `pins` are copied from the
certificate — the engine, the toolbox, the plan, the snapshot and the venue model — so
deployment can refuse a version whose engine has moved and a number can be traced back to
the conditions that produced it.

**The expectation is measured, not assumed.** The generated implementation is run over the
sleeve hypothesis's certification window, and what that run produces becomes the band the
paper and live gates later judge the realised result against: the objective's value, a
ninety-percent interval around it, the ninety-fifth percentile of the resampled drawdown,
and the window all three were measured over. When the certificate already holds that
evidence — the plan included the bootstrap gate, and it judged this same subject on this
same snapshot — it is reused rather than recomputed, because two resamplings of the same
run are two chances for the record and the expectation to disagree about the same thing.
With too few closed trades to resample, the interval is the point estimate and the
drawdown is the one the run actually took: an honest band of zero width beats an invented
one.

**Composing the same subject twice is one version.** Certification is automatic and so is
what follows it, so a repeat returns the version already made instead of a second copy of
it or a refusal that stops the loop.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, cast

from kanso import __version__
from kanso.certify import certificate
from kanso.classify.construct import get as construct_for
from kanso.criteria import CardRun, GateContext, drawdown_pct, gates, objectives
from kanso.data.manifest import catalog_path
from kanso.errors import PreconditionError
from kanso.hyp import HYPOTHESIS_FILE, Registration
from kanso.hyp import show as registration_of
from kanso.nautilus import backtest
from kanso.schemas import (
    Certificate,
    DateWindow,
    Expectation,
    Hypothesis,
    Pins,
    StrategyFile,
    StrategyVersion,
    parse_yaml,
)
from kanso.strategy import files, impl

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["BOOTSTRAP", "COMPOSED", "REPLICATIONS", "compose", "expectation", "strategy_id_of"]

COMPOSED: Final = "composed"
"""The state a fresh version is in, and the event kind composition appends."""

BOOTSTRAP: Final = "bootstrap"
"""The gate whose evidence an expectation's band is: the certificate's, or a fresh one."""

REPLICATIONS: Final = 1000
"""Resamplings used when the plan chose no count of its own, which is enough for a
ninety-percent interval and a ninety-fifth percentile to be stable to the eye."""

CERT_STAGE: Final = "cert"
NO_HOST: Final = "none"
"""What a construct that is a strategy of its own declares it attaches to."""

_BANDS: Final = frozenset({"objective", "objective_ci90", "mdd_p95"})
"""The evidence a bootstrap that judged always carries, and an expectation always needs."""

_UNMEASURED: Final = Expectation(
    objective_id="unmeasured",
    value=0.0,
    ci90=(0.0, 0.0),
    mdd_p95=0.0,
    window=DateWindow(start=datetime(1970, 1, 1).date(), end=datetime(1970, 1, 1).date()),
)
"""What a draft version carries while the implementation that measures the real one is
being written. It never reaches a file: the version that is written is composed a second
time, from the same construct and the same facts, with what was measured."""


def compose(ws: Workspace, store: StateStore, hyp_id: str) -> StrategyVersion:
    """Turn this hypothesis's newest passing certificate into a strategy version.

    Writes `strategies/<id>/impl/<version>/`, measures the expectation over the sleeve
    hypothesis's certification window, appends the version to `strategy.yaml` and indexes
    it. Refuses a hypothesis with no passing certificate, and returns the version already
    composed when this subject has been composed before.
    """
    passed = _certificate(store, hyp_id)
    construct = construct_for(passed.construct.id, ws)
    strategy_id = _strategy_id(construct, passed, hyp_id)
    held = files.read(ws, strategy_id)
    already = _composed(held, passed, construct)
    if already is not None:
        return already

    pins = _pins(passed)
    params = passed.construct.params
    created = datetime.now(tz=UTC)
    draft = construct.compose(
        held,
        hyp_id,
        passed.strategy_sha,
        params,
        pins=pins,
        expectation=_UNMEASURED,
        created_at=created,
    )
    sleeve_hyp = _hypothesis(ws, store, draft.sleeve.hyp_id)
    capital = sleeve_hyp.capital or ws.config.research.capital
    manifest = impl.generate(ws, store, strategy_id, draft, sleeve_hyp, capital, created)
    measured = expectation(ws, manifest, draft, sleeve_hyp, capital, passed)
    version = construct.compose(
        held,
        hyp_id,
        passed.strategy_sha,
        params,
        pins=pins,
        expectation=measured,
        created_at=created,
    )
    files.write(ws, files.appended(held, strategy_id, version))
    files.record(store, strategy_id, version)
    store.event(
        COMPOSED,
        strategy_id,
        {
            "version": version.version,
            "hyp_id": hyp_id,
            "construct": passed.construct.id,
            "strategy_sha": passed.strategy_sha,
            "objective": measured.objective_id,
            "value": measured.value,
        },
    )
    return version


def expectation(
    ws: Workspace,
    manifest: impl.ImplManifest,
    version: StrategyVersion,
    hyp: Hypothesis,
    capital: float,
    passed: Certificate,
) -> Expectation:
    """Run the implementation over the sleeve's certification window and band the result.

    The run is the files in the implementation directory, not the blobs they were copied
    from, so what the expectation describes is what a stage will load.
    """
    if hyp.objective is None:
        raise PreconditionError(
            f"{hyp.id} carries no objective, so there is nothing to expect of the strategy "
            "composed from it",
            remedy=f"run `kanso classify {hyp.id}` and certify it again",
        )
    start, end = _window(hyp)
    run = _measure(ws, manifest, version, hyp, capital)
    objective = objectives()[hyp.objective.id]
    value, _ = objective.compute(run, ws.config.research.folds)
    low, high, worst = _bands(ws, passed, version, hyp, run, value, objective.id)
    return Expectation(
        objective_id=objective.id,
        value=value,
        ci90=(low, high),
        mdd_p95=worst,
        window=DateWindow(start=start, end=end),
    )


# --- what is being composed ---------------------------------------------------


def _certificate(store: StateStore, hyp_id: str) -> Certificate:
    """This hypothesis's newest passing certificate, which is what composes."""
    passed = [held for held in certificate.of(store, hyp_id) if held.verdict == "pass"]
    if not passed:
        raise PreconditionError(
            f"{hyp_id} has no passing certificate, and a version is composed from one",
            remedy=f"run `kanso cert run {hyp_id}` until it passes",
        )
    return passed[0]


def strategy_id_of(passed: Certificate, ws: Workspace | None = None) -> str:
    """Which strategy this certificate belongs to: a new one of its own, or its host.

    A sleeve is a strategy named after itself; everything else is a version of the host it
    was classified onto, and an attached construct that names none has nowhere to go.
    """
    construct = construct_for(passed.construct.id, ws)
    return _strategy_id(construct, passed, passed.hyp_id)


def _strategy_id(construct: Any, passed: Certificate, hyp_id: str) -> str:
    """The strategy this certificate composes into: a new one, or the host it names."""
    if construct.needs_host == NO_HOST:
        return hyp_id
    host = passed.construct.host
    if host is None:
        raise PreconditionError(
            f"{hyp_id} is classified as a {passed.construct.id} but names no host, so there "
            "is no strategy for it to compose onto",
            remedy=f"run `kanso classify {hyp_id}` against a certified sleeve",
        )
    return host


def _composed(
    held: StrategyFile | None, passed: Certificate, construct: Any
) -> StrategyVersion | None:
    """The version this certificate already composed, when it composed one."""
    if held is None:
        return None
    if construct.needs_host == NO_HOST:
        first = held.versions[0]
        return first if first.sleeve.strategy_sha == passed.strategy_sha else None
    return next(
        (
            version
            for version in held.versions
            if version.attached
            and version.attached[-1].hyp_id == passed.hyp_id
            and version.attached[-1].strategy_sha == passed.strategy_sha
        ),
        None,
    )


def _pins(passed: Certificate) -> Pins:
    """What the version was certified under, copied from the certificate and nowhere else."""
    return Pins(
        kanso_version=__version__,
        nautilus_version=passed.nautilus_version,
        criteria_version=passed.criteria_version,
        plan_version=passed.plan_version,
        snapshot_id=passed.snapshot_id,
        venue_model=passed.venue_model,
    )


def _hypothesis(ws: Workspace, store: StateStore, hyp_id: str) -> Hypothesis:
    """The sleeve hypothesis, as the bytes this workspace registered it under.

    The registered bytes rather than the file as it stands, so a version's window and
    universe are the ones the registry pinned rather than whatever an editor left behind.
    """
    registration = cast(Registration, registration_of(ws, store, hyp_id))
    sha = registration.hypothesis_sha
    if sha is None:  # pragma: no cover - `hyp add` pins the bytes it registers
        raise PreconditionError(f"{hyp_id} is registered without bytes, so it cannot be read")
    return parse_yaml(Hypothesis, store.get_blob(sha).decode("utf-8"), HYPOTHESIS_FILE)


# --- measuring it -------------------------------------------------------------


def _window(hyp: Hypothesis) -> tuple[date, date]:
    """The embargoed window an expectation is measured over: the sleeve's own."""
    return hyp.windows.certification.start, hyp.windows.certification.end


def _measure(
    ws: Workspace,
    manifest: impl.ImplManifest,
    version: StrategyVersion,
    hyp: Hypothesis,
    capital: float,
) -> CardRun:
    """One run of the implementation over the sleeve's certification window."""
    sleeve, attached = impl.sources(ws, manifest)
    request = backtest.RunRequest(
        hyp=hyp,
        strategy_source=sleeve,
        window=_window(hyp),
        snapshot_id=version.pins.snapshot_id,
        venue_model=version.pins.venue_model.model_dump(),
        capital=capital,
        modifiers=attached,
        period=ws.config.research.return_period,
    )
    return backtest.run(request, catalog_path(ws)).run


def _bands(
    ws: Workspace,
    passed: Certificate,
    version: StrategyVersion,
    hyp: Hypothesis,
    run: CardRun,
    value: float,
    objective_id: str,
) -> tuple[float, float, float]:
    """The interval and the drawdown percentile: the certificate's, or a fresh resampling.

    A resampling is seeded from the version's sleeve rather than from the certificate that
    triggered the composition, so a construct that changes nothing about the book reads
    back the band its host has, and a sleeve's recomputed band is the one its own
    certificate would have recorded.
    """
    recorded = _recorded(passed, version, objective_id)
    if recorded is not None:
        return recorded
    result = gates()[BOOTSTRAP].evaluate(
        GateContext(
            hyp=hyp,
            construct=impl.SLEEVE,
            stage=CERT_STAGE,
            params={"n": _replications(passed)},
            window=_window(hyp),
            run=run,
            research_folds=ws.config.research.folds,
            snapshot_id=version.pins.snapshot_id,
            strategy_sha=version.sleeve.strategy_sha,
        )
    )
    if result.skipped is not None:
        return value, value, drawdown_pct(run)
    low, high = result.evidence["objective_ci90"]
    return float(low), float(high), float(result.evidence["mdd_p95"])


def _recorded(
    passed: Certificate, version: StrategyVersion, objective_id: str
) -> tuple[float, float, float] | None:
    """The certificate's own bootstrap evidence, when it resampled this very run.

    The subject and the snapshot have to match, and so does the objective: a certificate
    of an attached construct measured a different subject over a different window with a
    relative objective, and none of its evidence describes the strategy it composes into.
    """
    if (
        passed.strategy_sha != version.sleeve.strategy_sha
        or passed.snapshot_id != version.pins.snapshot_id
    ):
        return None
    found = next(
        (
            gate
            for gate in passed.gates
            if gate.id == BOOTSTRAP
            and gate.skipped is None
            and gate.evidence.keys() >= _BANDS
            and gate.evidence["objective"] == objective_id
        ),
        None,
    )
    if found is None:
        return None
    low, high = found.evidence["objective_ci90"]
    return float(low), float(high), float(found.evidence["mdd_p95"])


def _replications(passed: Certificate) -> int:
    """How many resamplings to draw: the planner's count, or this module's own."""
    for gate in passed.gates:
        chosen = gate.params.get("n")
        if gate.id == BOOTSTRAP and isinstance(chosen, int):
            return chosen
    return REPLICATIONS
