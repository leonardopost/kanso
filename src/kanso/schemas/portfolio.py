"""`portfolio.yaml`: the two stages, their limits and any per-venue override.

A stage names an execution client and a data client, carries the capital the percentage
limits are taken of, and lists the strategy versions deployed on it. `paper` and `live`
are the only stages: the certification toolbox knows exactly those two deployment stages,
and a third would have no gates to judge it.

The venue overrides live here because a venue's model is a property of the account that
trades it, not of any one hypothesis. The pairing rules an execution client's declarations
imply are in `kanso.schemas.venue`; they are preconditions of deployment, not of the file.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field, model_validator

from kanso.schemas.base import HypId, KansoModel, NonEmpty, Versioned
from kanso.schemas.venue import REPLAY_DATA_CLIENT, VenueCode, VenueOverride

STAGES: tuple[str, str] = ("paper", "live")


def _exceeds(value: float, ceiling: float) -> bool:
    """Above a ceiling, allowing for the rounding of a capital split into shares."""
    return value > ceiling and not math.isclose(value, ceiling, rel_tol=1e-9)


class Deployment(KansoModel):
    """One strategy version on a stage, with its slice of the stage capital."""

    id: HypId
    version: int = Field(ge=1)
    capital: float = Field(ge=0)
    joined_at: datetime


class Stage(KansoModel):
    """One node: an execution client, a data client, a clock speed and its capital."""

    exec: NonEmpty
    data: NonEmpty = REPLAY_DATA_CLIENT
    speed: float = Field(default=1.0, ge=0)
    capital: float = Field(ge=0)
    kill_switch: bool = False
    strategies: list[Deployment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_version_per_strategy(self) -> Stage:
        ids = [s.id for s in self.strategies]
        if len(set(ids)) != len(ids):
            raise ValueError("strategies: a stage holds at most one version of each strategy")
        return self


class Stages(KansoModel):
    """The two deployment stages, in the order a version travels them."""

    paper: Stage
    live: Stage


class Limits(KansoModel):
    """Percentages of the stage capital, enforced by the monitor on every pass."""

    max_gross_pct: float = Field(gt=0)
    max_net_pct: float = Field(gt=0)
    per_strategy_max_pct: float = Field(gt=0, le=100)
    daily_loss_pct: float = Field(gt=0, le=100)


class Portfolio(Versioned):
    """The deployment surface of a workspace."""

    stages: Stages
    limits: Limits
    venues: dict[VenueCode, VenueOverride] | None = None

    @model_validator(mode="after")
    def _capital_fits_the_limits(self) -> Portfolio:
        cap = self.limits.per_strategy_max_pct / 100
        for name, stage in (("paper", self.stages.paper), ("live", self.stages.live)):
            allocated = sum(s.capital for s in stage.strategies)
            if _exceeds(allocated, stage.capital):
                raise ValueError(
                    f"stages.{name}.strategies: allocate {allocated} of {stage.capital} capital"
                )
            over = [s.id for s in stage.strategies if _exceeds(s.capital, cap * stage.capital)]
            if over:
                raise ValueError(
                    f"stages.{name}.strategies: {', '.join(over)} exceed "
                    f"per_strategy_max_pct ({self.limits.per_strategy_max_pct}%) "
                    f"of the stage capital"
                )
        return self
