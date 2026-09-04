"""`strategy.yaml`: a deployable strategy and its versions.

A strategy is composed from certified hypotheses, never written by hand: a sleeve becomes
version 1, and every attached construct — a filter, an overlay, an exit rule — becomes the
host's next version. Each version records what it was certified under (`pins`) and what
composition measured of it over the sleeve's certification window (`expectation`), which
is the band the paper and live gates later judge the realised objective against.

A stage holds at most one version of a strategy, so at most one version is `live` and at
most one is on paper — `paper` and `promotable` being the same stage seen before and after
its gates passed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from kanso.schemas.base import (
    CatalogueId,
    FreeForm,
    HypId,
    KansoModel,
    NonEmpty,
    Params,
    Sha256,
    Versioned,
)
from kanso.schemas.hypothesis import DateWindow
from kanso.schemas.venue import VenueModel

VersionState = Literal["composed", "paper", "promotable", "live", "retired"]
PAPER_STATES: tuple[VersionState, ...] = ("paper", "promotable")


class SleeveRef(KansoModel):
    """The certified sleeve a strategy is built on."""

    hyp_id: HypId
    strategy_sha: Sha256


class AttachedRef(KansoModel):
    """A certified construct layered on the sleeve, and the values it was composed with."""

    hyp_id: HypId
    strategy_sha: Sha256
    construct_: CatalogueId = Field(alias="construct")
    params: Params | None = None

    @property
    def construct(self) -> CatalogueId:  # type: ignore[override]
        """The construct id. Aliased: `construct` is taken."""
        return self.construct_


class Pins(KansoModel):
    """What a version was certified under; `deploy` refuses an engine that has moved."""

    kanso_version: NonEmpty
    nautilus_version: NonEmpty
    criteria_version: NonEmpty
    plan_version: int = Field(ge=1)
    snapshot_id: NonEmpty
    venue_model: VenueModel


class Expectation(KansoModel):
    """What composition measured, and the band monitoring judges the realised result by."""

    objective_id: CatalogueId
    value: float
    ci90: tuple[float, float]
    mdd_p95: float = Field(ge=0)
    window: DateWindow

    @model_validator(mode="after")
    def _interval_ordered(self) -> Expectation:
        low, high = self.ci90
        if high < low:
            raise ValueError(f"ci90: upper bound {high} is below lower bound {low}")
        return self


class StrategyVersion(KansoModel):
    """One immutable composition of a sleeve and its attached constructs."""

    version: int = Field(ge=1)
    sleeve: SleeveRef
    attached: list[AttachedRef] = Field(default_factory=list)
    config: FreeForm = Field(default_factory=dict)
    pins: Pins
    expectation: Expectation
    state: VersionState
    created_at: datetime

    @model_validator(mode="after")
    def _attached_once(self) -> StrategyVersion:
        ids = [a.hyp_id for a in self.attached]
        if len(set(ids)) != len(ids):
            raise ValueError("attached: names a hypothesis twice")
        if any(a.hyp_id == self.sleeve.hyp_id for a in self.attached):
            raise ValueError("attached: the sleeve hypothesis cannot also be attached")
        return self


class StrategyFile(Versioned):
    """`strategies/<id>/strategy.yaml`: every version of one strategy, in order."""

    id: HypId
    versions: list[StrategyVersion] = Field(min_length=1)

    def latest(self) -> StrategyVersion:
        """The highest-numbered version."""
        return self.versions[-1]

    def deployed(self, state: VersionState) -> StrategyVersion | None:
        """The version in the given state, if one is."""
        return next((v for v in self.versions if v.state == state), None)

    @model_validator(mode="after")
    def _versions_are_a_sequence(self) -> StrategyFile:
        numbers = [v.version for v in self.versions]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(
                f"versions: must be numbered 1..{len(numbers)} in order, got {numbers}"
            )
        on_paper = [v.version for v in self.versions if v.state in PAPER_STATES]
        if len(on_paper) > 1:
            raise ValueError(f"versions: {on_paper} are both on the paper stage; a stage holds one")
        live = [v.version for v in self.versions if v.state == "live"]
        if len(live) > 1:
            raise ValueError(f"versions: {live} are both live; a stage holds one version")
        return self
