"""What a replay replays: a composed strategy version, or a hypothesis's card.

Two things can be replayed and they are named differently because they are pinned
differently. A **strategy version** is a finished composition: it runs the generated
implementation of that version — the same directory a stage loads, not the blobs it was
copied from — and its pins carry the venue model and the snapshot it was certified under. A
**hypothesis** is replayed as one of its cards — its `best`, or a named sha — and everything
that card was measured against comes from the run that produced it, exactly as certification
takes it, because a number that moved because a file moved is not evidence.

Both resolve to the same thing: the bytes of a sleeve, the attached constructs with the
parameters they were composed with, and the money and the model to run them under. What
neither carries is a window: a replay's range is the forward window and the catalog, never a
window the hypothesis declared, because the forward window is the one nothing is allowed to
backtest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kanso import strategy
from kanso.classify.construct import HostRef
from kanso.classify.construct import get as construct_for
from kanso.data.manifest import catalog_path
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import HYPOTHESIS_FILE, hypothesis_file
from kanso.hyp import show as registration_of
from kanso.nautilus.backtest import RunRequest
from kanso.research import records
from kanso.schemas import Hypothesis, StrategyFile, VenueModel, load_yaml, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from datetime import date

    from kanso.hyp import Registration
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["Target", "resolve"]

Modifiers = tuple[tuple[str, bytes, Mapping[str, Any]], ...]


@dataclass(frozen=True)
class Target:
    """One runnable subject, with the money, the model and the data it is judged under."""

    label: str
    hyp: Hypothesis
    strategy_source: bytes
    strategy_sha: str
    snapshot_id: str
    venue_model: VenueModel
    capital: float
    period: str
    catalog: Path
    modifiers: Modifiers = ()
    strategy_id: str | None = None
    version: int | None = None

    @property
    def universe(self) -> tuple[str, ...]:
        """The instruments this target trades."""
        return tuple(self.hyp.universe)

    def request(self, window: tuple[date, date]) -> RunRequest:
        """The run this target asks for over a range, on either code path."""
        return RunRequest(
            hyp=self.hyp,
            strategy_source=self.strategy_source,
            window=window,
            snapshot_id=self.snapshot_id,
            venue_model=self.venue_model.model_dump(),
            capital=self.capital,
            modifiers=self.modifiers,
            period=self.period,
        )


def resolve(
    ws: Workspace,
    store: StateStore,
    *,
    strategy: str | None = None,
    version: int | None = None,
    hyp: str | None = None,
    sha: str | None = None,
) -> Target:
    """The target a replay was asked for: one of a strategy version and a hypothesis."""
    if (strategy is None) == (hyp is None):
        raise ValidationError(
            "target: name exactly one of a strategy and a hypothesis to replay",
            remedy="pass --strategy STRATEGY[@V] or --hyp ID",
        )
    if strategy is not None:
        if sha is not None:
            raise ValidationError(
                "sha: a strategy version already names the bytes it was composed from; "
                "--sha names a card and belongs to --hyp"
            )
        return _of_strategy(ws, store, strategy, version)
    if version is not None:
        raise ValidationError(
            "version: a hypothesis has cards rather than versions; --version belongs to --strategy"
        )
    return _of_hypothesis(ws, store, cast(str, hyp), sha)


# --- a composed strategy version ---------------------------------------------


def _of_strategy(ws: Workspace, store: StateStore, strategy_id: str, version: int | None) -> Target:
    """One version of a composed strategy: its generated implementation and its pins.

    The bytes come from `impl/<version>/`, not from the blobs they were copied from. That
    directory is what a stage loads, so it is what a replay has to run: were replay to read
    the blobs instead, it would answer a question about the certificate rather than about
    the thing that will trade. Reading it checks every file against the digest the manifest
    records, so a version whose implementation has been edited is refused here too rather
    than replayed under the label of the version it diverged from.

    Everything a strategy version runs comes from files — `strategy.yaml`, the manifest and
    the sources — except the traded universe and the forward window, which a version does
    not carry and its hypothesis does. Those are read from the pinned hypothesis when the
    registry holds it, and from the committed `hypotheses/<id>/hypothesis.yaml` when it does
    not, so a fresh clone whose `state.db` never travelled replays a committed version from
    its files without re-registering the hypothesis first.
    """
    file = strategy.require(ws, strategy_id)
    chosen = HostRef.of(file, version).version
    composed = file.versions[chosen - 1]
    hypothesis = _hypothesis(ws, store, composed.sleeve.hyp_id)
    sleeve, modifiers = strategy.sources(ws, strategy.read_manifest(ws, strategy_id, chosen))
    return Target(
        label=f"{strategy_id}@{chosen}",
        hyp=hypothesis,
        strategy_source=sleeve,
        strategy_sha=composed.sleeve.strategy_sha,
        snapshot_id=composed.pins.snapshot_id,
        venue_model=composed.pins.venue_model,
        capital=hypothesis.capital or ws.config.research.capital,
        period=ws.config.research.return_period,
        catalog=catalog_path(ws),
        modifiers=modifiers,
        strategy_id=strategy_id,
        version=chosen,
    )


# --- a hypothesis's card ------------------------------------------------------


def _of_hypothesis(ws: Workspace, store: StateStore, hyp_id: str, sha: str | None) -> Target:
    """One card of one hypothesis, on the host it was researched against.

    An attached construct is not a strategy: it is replayed on the certified host version
    its run pinned, with itself appended, which is the only arrangement in which it trades
    at all.
    """
    registration_of(ws, store, hyp_id)
    chosen = _card_sha(store, hyp_id, sha)
    card = [c for c in records.cards_of(store, hyp_id) if c.strategy_sha == chosen][-1]
    run = next(r for r in records.runs_of(store, hyp_id) if r.run_id == card.run_id)
    hypothesis = parse_yaml(
        Hypothesis, store.get_blob(run.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    if hypothesis.construct is None:
        raise PreconditionError(
            f"{hyp_id} has no construct, so there is nothing to replay it as",
            remedy=f"run `kanso classify {hyp_id}`",
        )
    harness = construct_for(hypothesis.construct.id, ws).harness(
        hypothesis, _host(ws, hypothesis.construct.host), version=run.host_version
    )
    source = store.get_blob(chosen)
    if harness.host is None:
        sleeve: bytes = source
        modifiers: Modifiers = ()
    else:
        sleeve, attached = records.host_sources(store, harness.host)
        modifiers = (
            *attached,
            (harness.construct, source, dict(hypothesis.construct.params or {})),
        )
    return Target(
        label=f"{hyp_id}@{chosen[:7]}",
        hyp=hypothesis,
        strategy_source=sleeve,
        strategy_sha=chosen,
        snapshot_id=run.snapshot_id,
        venue_model=card.venue_model,
        capital=hypothesis.capital or ws.config.research.capital,
        period=ws.config.research.return_period,
        catalog=catalog_path(ws),
        modifiers=modifiers,
    )


def _hypothesis(ws: Workspace, store: StateStore, hyp_id: str) -> Hypothesis:
    """The hypothesis a composed version was built from: the pinned bytes, or the file.

    The pinned bytes are preferred: a version was composed from what the hypothesis said
    when it was certified, and an edit since then belongs to the next version rather than to
    this one. When the registry holds no pin — a clone whose `state.db` never travelled has
    no registry at all — the committed `hypotheses/<id>/hypothesis.yaml` is read instead, so
    a version's universe and forward window are recovered from the file that composed it and
    a committed, certified version replays without re-registering the hypothesis first.
    """
    registration = _registration_or_none(ws, store, hyp_id)
    if registration is not None and registration.hypothesis_sha is not None:
        return parse_yaml(
            Hypothesis,
            store.get_blob(registration.hypothesis_sha).decode("utf-8"),
            HYPOTHESIS_FILE,
        )
    path = hypothesis_file(ws, hyp_id)
    if not path.is_file():
        raise PreconditionError(
            f"{hyp_id} is neither registered nor present at {path}, so the version's "
            "universe and forward window cannot be recovered to replay it",
            remedy=f"run `kanso hyp add hypotheses/{hyp_id}/hypothesis.yaml`",
        )
    return load_yaml(Hypothesis, path)


def _registration_or_none(ws: Workspace, store: StateStore, hyp_id: str) -> Registration | None:
    """This hypothesis's registration, or `None` when the registry does not hold it.

    Listed rather than fetched by id, because fetching one by id refuses an id the registry
    does not know, and a clone's empty registry is a state to read past rather than to fail
    on.
    """
    registrations = cast("list[Registration]", registration_of(ws, store))
    return next((one for one in registrations if one.hyp_id == hyp_id), None)


def _host(ws: Workspace, host_id: str | None) -> StrategyFile | None:
    """The composed host an attached construct was researched against."""
    return None if host_id is None else strategy.require(ws, host_id)


def _card_sha(store: StateStore, hyp_id: str, sha: str | None) -> str:
    """This hypothesis's `best`, or the one card its prefix names."""
    if sha is not None:
        return records.card_sha(store, hyp_id, sha)
    best, _ = records.best_of(store, hyp_id)
    if best is None:
        raise PreconditionError(
            f"{hyp_id} has no best card, so there is nothing to replay",
            remedy=f"research it with `kanso research begin {hyp_id}`",
        )
    return best
