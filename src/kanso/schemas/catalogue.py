"""The two catalogue items: a construct and a criteria item.

Both are shipped YAML files, and both declare capabilities rather than defaults. A
construct says what a hypothesis *is* in portfolio-construction terms, what it attaches to
and whether this version can run it. A criteria item says what can be measured: an
objective, selected deterministically by its applicability predicate, or a gate, whose
values an agent chooses at planning time from the declared ranges.

That split is why a gate carries no default and no threshold and an objective carries no
range on its own applicability: the framework's opinions are the structural invariants,
and everything else is decided at runtime from what these files declare.

A range bound is a number, a duration, or the expression `<number> * <attr>` over the
run's own scale — its horizon, its resolution, the length of its research window in days,
or its fold count — so a toolbox item is written once and sized to each hypothesis.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, model_validator

from kanso.errors import ValidationError
from kanso.schemas.base import CatalogueId, FreeForm, KansoModel, NonEmpty
from kanso.schemas.duration import Duration, is_duration, parse_duration
from kanso.schemas.hypothesis import Mechanism

NeedsHost = Literal["none", "sleeve", "portfolio"]
ObjectiveMode = Literal["absolute", "relative"]
Kind = Literal["objective", "gate"]
GateStage = Literal["card", "cert", "paper", "live"]
ParamType = Literal["int", "float", "bool", "str", "duration"]

RANGE_ATTRS: Final = ("horizon", "resolution", "history_days", "folds")
NUMERIC_PARAM_TYPES: Final = ("int", "float", "duration")

ImplPath = Annotated[
    str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$")
]

_EXPRESSION: Final = re.compile(
    r"^(?P<n>[0-9]+(\.[0-9]+)?)\s*\*\s*(?P<attr>" + "|".join(RANGE_ATTRS) + r")$"
)

RangeBound = float | str
"""A number, a duration, or `<number> * <attr>`."""


def parse_bound(bound: float | str, field: str = "ranges") -> tuple[float, str | None]:
    """A bound as a coefficient and the attribute it scales, `None` for an absolute one."""
    if isinstance(bound, bool):
        raise ValidationError(f"{field}: {bound!r} is not a range bound")
    if isinstance(bound, int | float):
        return float(bound), None
    match = _EXPRESSION.match(bound)
    if match is not None:
        return float(match.group("n")), match.group("attr")
    if is_duration(bound):
        return parse_duration(bound, field).total_seconds(), "seconds"
    raise ValidationError(
        f"{field}: {bound!r} is not a range bound; expected a number, a duration, or "
        f"'<number> * <attr>' with attr one of {', '.join(RANGE_ATTRS)}"
    )


class ConstructItem(KansoModel):
    """One entry of the construct catalogue."""

    id: CatalogueId
    description: NonEmpty
    needs_host: NeedsHost
    objective_mode: ObjectiveMode
    params: FreeForm | None = None
    runnable: bool
    impl: ImplPath

    @model_validator(mode="after")
    def _mode_matches_attachment(self) -> ConstructItem:
        if self.needs_host == "none" and self.objective_mode == "relative":
            raise ValueError(
                "objective_mode: a construct with no host has nothing to be relative to"
            )
        return self


class DurationBounds(KansoModel):
    """A duration predicate: `min` inclusive, `max` exclusive."""

    min: Duration | None = None
    max: Duration | None = None

    @model_validator(mode="after")
    def _ordered(self) -> DurationBounds:
        if (
            self.min is not None
            and self.max is not None
            and parse_duration(self.max, "max") <= parse_duration(self.min, "min")
        ):
            raise ValueError(f"max: {self.max} is not above min {self.min}")
        return self


class IntBounds(KansoModel):
    """An integer predicate: `min` inclusive, `max` exclusive."""

    min: int | None = None
    max: int | None = None

    @model_validator(mode="after")
    def _ordered(self) -> IntBounds:
        if self.min is not None and self.max is not None and self.max <= self.min:
            raise ValueError(f"max: {self.max} is not above min {self.min}")
        return self


class Applies(KansoModel):
    """An objective's selection predicate. Every stated clause must hold."""

    mechanism: list[Mechanism] | None = None
    objective_mode: ObjectiveMode | None = None
    horizon: DurationBounds | None = None
    resolution: DurationBounds | None = None
    universe_size: IntBounds | None = None
    data_requirements: list[CatalogueId] | None = None
    history_days: IntBounds | None = None


class CriteriaItem(KansoModel):
    """One entry of the criteria toolbox: an objective or a gate."""

    id: CatalogueId
    kind: Kind
    stage: GateStage | None = None
    required: bool | None = None
    applies: Applies | None = None
    priority: int | None = Field(default=None, ge=0)
    meaningful_when: str | None = Field(default=None, max_length=200)
    params: dict[str, ParamType] = Field(default_factory=dict)
    ranges: dict[str, tuple[RangeBound, RangeBound]] = Field(default_factory=dict)
    impl: ImplPath

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> CriteriaItem:
        if self.kind == "gate":
            self._check_gate()
        else:
            self._check_objective()
        self._check_ranges()
        return self

    def _check_gate(self) -> None:
        if self.stage is None:
            raise ValueError("stage: a gate declares the stage it runs at")
        if self.applies is not None:
            raise ValueError("applies: a gate has no applicability; the planner decides")
        if self.priority is not None:
            raise ValueError("priority: only objectives are prioritised")
        if self.meaningful_when is None:
            raise ValueError("meaningful_when: a gate tells the planner when it is informative")

    def _check_objective(self) -> None:
        if self.stage is not None:
            raise ValueError("stage: an objective is measured, not staged")
        if self.required is not None:
            raise ValueError("required: only gates are structural invariants")
        if self.applies is None:
            raise ValueError("applies: an objective declares the hypotheses it applies to")
        if self.priority is None:
            raise ValueError("priority: an objective declares one; the lowest applicable wins")

    def _check_ranges(self) -> None:
        unknown = sorted(set(self.ranges) - set(self.params))
        if unknown:
            raise ValueError(f"ranges: {', '.join(unknown)} is not a declared param")
        missing = sorted(
            name
            for name, kind in self.params.items()
            if kind in NUMERIC_PARAM_TYPES and name not in self.ranges
        )
        if missing:
            raise ValueError(f"ranges: {', '.join(missing)} is numeric and needs a range")
        for name, (low, high) in self.ranges.items():
            low_value, low_attr = parse_bound(low, f"ranges.{name}")
            high_value, high_attr = parse_bound(high, f"ranges.{name}")
            if low_attr == high_attr and high_value < low_value:
                raise ValueError(
                    f"ranges.{name}: upper bound {high!r} is below lower bound {low!r}"
                )
