"""`hypothesis.yaml`: the unit of research, and the rules that make one admissible.

The file is the operator's statement of a falsifiable idea plus the classification an
agent writes back into it. Status, pins and the hypothesis's best card live in the state
store, never here, so the file's bytes are stable enough to be content-addressed.

Three families of rule are enforced here, all of them local to the file:

* the windows are ordered, do not overlap, and the certification window opens no earlier
  than the embargo after research closes — `max(5 x horizon, 1d)`, rounded up to whole
  days because the windows are dated. The embargo is what keeps the loop from optimising
  against the data that is meant to judge it, so it is arithmetic, not advice;
* the resolution and the cost model must be answerable from the data the hypothesis asks
  for: bar resolution needs bars, a quote-derived spread needs quotes, a fixed spread
  needs its width;
* a classified hypothesis carries `strategy_integrity` among its constraints.

Whether a universe id resolves to an instrument, whether a construct exists in the
catalogue and whether an objective applies are checked where those catalogues live.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator

from kanso.schemas.base import (
    CatalogueId,
    HypId,
    KansoModel,
    NonEmpty,
    Params,
    Versioned,
)
from kanso.schemas.duration import Duration, Resolution, is_duration, parse_duration
from kanso.schemas.venue import CostsOverride

Mechanism = Literal[
    "mean_reversion",
    "momentum",
    "microstructure",
    "stat_arb",
    "event",
    "carry",
    "vol",
    "other",
]

MINIMUM_EMBARGO = timedelta(days=1)
EMBARGO_HORIZONS = 5
INTEGRITY_GATE = "strategy_integrity"


def embargo(horizon: str) -> timedelta:
    """`max(5 x horizon, 1d)`: the gap the certification window keeps from research."""
    return max(EMBARGO_HORIZONS * parse_duration(horizon, "horizon"), MINIMUM_EMBARGO)


def embargo_days(horizon: str) -> int:
    """The embargo in whole days, rounded up, because the windows are dated."""
    return math.ceil(embargo(horizon).total_seconds() / 86400)


class DateWindow(KansoModel):
    """A closed span of dates."""

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        return self


class OpenWindow(KansoModel):
    """A span with no end: paper and live replay run from here to whatever exists."""

    start: date


class Windows(KansoModel):
    """The three windows of a hypothesis, ordered and disjoint."""

    research: DateWindow
    certification: DateWindow
    forward: OpenWindow

    def check_embargo(self, horizon: str) -> None:
        """Refuse a certification window that opens inside the embargo."""
        earliest = self.research.end + timedelta(days=embargo_days(horizon))
        if self.certification.start < earliest:
            raise ValueError(
                f"windows.certification.start: {self.certification.start} is inside the "
                "embargo; "
                f"research ends {self.research.end} and the embargo for a {horizon} horizon "
                f"is {embargo_days(horizon)}d, so certification may not open before {earliest}"
            )

    @model_validator(mode="after")
    def _ordered_and_disjoint(self) -> Windows:
        if self.certification.start <= self.research.end:
            raise ValueError(
                f"certification.start: {self.certification.start} overlaps the research window "
                f"ending {self.research.end}"
            )
        if self.forward.start <= self.certification.end:
            raise ValueError(
                f"forward.start: {self.forward.start} overlaps the certification window "
                f"ending {self.certification.end}"
            )
        return self


class RiskLimits(KansoModel):
    """Percentages of the hypothesis capital, plus the leverage ceiling."""

    max_position_pct: float = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0, le=100)
    max_leverage: float = Field(gt=0)


class ConstructRef(KansoModel):
    """What `classify` decided this hypothesis is, in portfolio-construction terms."""

    id: CatalogueId
    host: HypId | None = None
    params: Params | None = None
    rationale: str | None = Field(default=None, max_length=240)


class ObjectiveParams(KansoModel):
    """The keep rule: an improvement counts when it clears both."""

    min_delta: float = Field(ge=0)
    k_se: float = Field(gt=0)


class ObjectiveRef(KansoModel):
    """The one scalar a run optimises, with the keep rule's two parameters."""

    id: CatalogueId
    params: ObjectiveParams


class ConstraintRef(KansoModel):
    """A card-stage gate and the values the classifier chose for it."""

    id: CatalogueId
    params: Params = Field(default_factory=dict)


class Hypothesis(Versioned):
    """A falsifiable idea, its data requirements, its windows and its classification."""

    id: HypId
    title: NonEmpty
    thesis: NonEmpty
    mechanism: Mechanism
    universe: list[NonEmpty] = Field(min_length=1)
    horizon: Duration
    resolution: Resolution
    data_requirements: list[CatalogueId] = Field(min_length=1)
    costs: CostsOverride | None = None
    capital: float | None = Field(default=None, gt=0)
    risk_limits: RiskLimits
    windows: Windows
    construct_: ConstructRef | None = Field(default=None, alias="construct")
    objective: ObjectiveRef | None = None
    constraints: list[ConstraintRef] | None = None

    @property
    def construct(self) -> ConstructRef | None:  # type: ignore[override]
        """The classification, if `classify` has run. Aliased: `construct` is taken."""
        return self.construct_

    @property
    def embargo(self) -> timedelta:
        """The gap this hypothesis's horizon requires between research and certification."""
        return embargo(self.horizon)

    @model_validator(mode="after")
    def _validate(self) -> Hypothesis:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe: repeats an id")
        if len(set(self.data_requirements)) != len(self.data_requirements):
            raise ValueError("data_requirements: repeats a type")
        if parse_duration(self.horizon, "horizon") <= timedelta(0):
            raise ValueError("horizon: must be longer than zero")
        self.windows.check_embargo(self.horizon)
        self._check_resolution()
        self._check_costs()
        self._check_constraints()
        return self

    def _check_resolution(self) -> None:
        required = self.data_requirements
        if is_duration(self.resolution):
            if parse_duration(self.resolution, "resolution") <= timedelta(0):
                raise ValueError("resolution: must be longer than zero")
            if "bar" not in required:
                raise ValueError(
                    f"data_requirements: resolution {self.resolution} is a bar size, so 'bar' "
                    "must be required"
                )
        elif self.resolution == "tick":
            if not ({"trade", "quote"} & set(required)):
                raise ValueError(
                    "data_requirements: resolution 'tick' needs 'trade' or 'quote' to be required"
                )
        elif self.resolution not in required:
            raise ValueError(
                f"data_requirements: resolution {self.resolution!r} must also be required"
            )

    def _check_costs(self) -> None:
        if self.costs is None:
            return
        if self.costs.spread == "quotes" and "quote" not in self.data_requirements:
            raise ValueError(
                "costs.spread: 'quotes' needs 'quote' in data_requirements to read a spread from"
            )
        if self.costs.spread == "fixed_bps" and self.costs.fixed_bps is None:
            raise ValueError("costs.fixed_bps: required when spread is fixed_bps")

    def _check_constraints(self) -> None:
        if self.constraints is None:
            return
        ids = [c.id for c in self.constraints]
        if len(set(ids)) != len(ids):
            raise ValueError("constraints: repeats a gate id")
        if INTEGRITY_GATE not in ids:
            raise ValueError(
                f"constraints: {INTEGRITY_GATE} is required of every classified hypothesis"
            )
