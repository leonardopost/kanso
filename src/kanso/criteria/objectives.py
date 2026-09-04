"""The objectives: the one scalar a run optimises, and which hypotheses each applies to.

Every objective is fold-wise by construction. A statistic is computed on each of the
research window's contiguous folds and the folds are then averaged, so the number a card
reports carries its own standard error and the keep rule has a noise floor to clear. A
relative objective is the same statistic differenced fold by fold against the host's run,
because the standard error of the differences is what a marginal improvement has to beat
— the difference of two separately averaged metrics would throw that pairing away.

Which objective a hypothesis gets is the one deterministic domain rule in the system: the
`applies` predicate over the hypothesis's own attributes, with `min` inclusive and `max`
exclusive, and the lowest priority winning among those that apply. The shipped set is
total over mechanism, objective mode and horizon either side of a day, so classification
always has an objective to write and never has to invent one: at or above a day the
statistic is a Sharpe of returns, below it the mean edge per trade, since a sub-daily
holding period produces too few return periods for a Sharpe to mean anything and enough
trades for a per-trade edge to.

A resolution that names an unaggregated grain rather than a bar size is treated as the
finest grain there is — zero length — so a predicate written in durations orders it below
every bar size.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import ClassVar, Final, Protocol

from kanso.criteria.quantities import edge_bps, mean, sharpe, standard_error
from kanso.criteria.run import CardRun
from kanso.errors import PreconditionError
from kanso.schemas import (
    Applies,
    CriteriaItem,
    DurationBounds,
    Hypothesis,
    IntBounds,
    is_duration,
    parse_duration,
)

ABSOLUTE: Final = "absolute"
RELATIVE: Final = "relative"

SHARPE_FAMILY: Final = frozenset({"wf_sharpe_net", "marginal_wf_sharpe"})
"""The objectives a deflated Sharpe has something to deflate."""


class Objective(Protocol):
    """One scalar a run is optimised against, measured fold-wise."""

    id: ClassVar[str]
    mode: ClassVar[str]

    def fold_values(
        self, run: CardRun, folds: int, host: CardRun | None = None
    ) -> tuple[float, ...]:
        """The statistic on each fold, in window order."""
        ...

    def compute(self, run: CardRun, folds: int, host: CardRun | None = None) -> tuple[float, float]:
        """The metric and its standard error: the mean of the folds and their spread."""
        ...


STATISTICS: Final[Mapping[str, Callable[[CardRun], float]]] = MappingProxyType(
    {"sharpe": sharpe, "edge_bps": edge_bps}
)
"""The per-fold quantities an objective is built from, named so a class can select one."""


class _FoldObjective:
    """A statistic averaged over folds, absolutely or against a host run."""

    id: ClassVar[str]
    mode: ClassVar[str]
    statistic: ClassVar[str]

    def fold_values(
        self, run: CardRun, folds: int, host: CardRun | None = None
    ) -> tuple[float, ...]:
        measure = STATISTICS[self.statistic]
        own = tuple(measure(fold) for fold in run.folds(folds))
        if self.mode == ABSOLUTE:
            return own
        if host is None:
            raise PreconditionError(
                f"{self.id}: a relative objective is measured against the host's run, "
                "and none was supplied"
            )
        against = tuple(measure(fold) for fold in host.folds(folds))
        return tuple(a - b for a, b in zip(own, against, strict=True))

    def compute(self, run: CardRun, folds: int, host: CardRun | None = None) -> tuple[float, float]:
        values = self.fold_values(run, folds, host)
        return mean(values), standard_error(values)


class _WfSharpeNet(_FoldObjective):
    """Fold-wise annualised Sharpe of net returns, at a zero risk-free rate."""

    id = "wf_sharpe_net"
    mode = ABSOLUTE
    statistic = "sharpe"


class _NetEdgeBps(_FoldObjective):
    """Fold-wise mean net profit per closed trade, in basis points."""

    id = "net_edge_bps"
    mode = ABSOLUTE
    statistic = "edge_bps"


class _MarginalWfSharpe(_FoldObjective):
    """The combined run's fold-wise Sharpe minus the host's, fold by fold."""

    id = "marginal_wf_sharpe"
    mode = RELATIVE
    statistic = "sharpe"


class _MarginalNetEdgeBps(_FoldObjective):
    """The combined run's fold-wise per-trade edge minus the host's, fold by fold."""

    id = "marginal_net_edge_bps"
    mode = RELATIVE
    statistic = "edge_bps"


wf_sharpe_net: Final[Objective] = _WfSharpeNet()
net_edge_bps: Final[Objective] = _NetEdgeBps()
marginal_wf_sharpe: Final[Objective] = _MarginalWfSharpe()
marginal_net_edge_bps: Final[Objective] = _MarginalNetEdgeBps()

REGISTRY: Final[Mapping[str, Objective]] = MappingProxyType(
    {
        objective.id: objective
        for objective in (wf_sharpe_net, net_edge_bps, marginal_wf_sharpe, marginal_net_edge_bps)
    }
)
"""The implemented objectives, for a caller that already knows the id it wants."""


def resolution_seconds(resolution: str) -> float:
    """A bar size in seconds; an unaggregated grain is the finest there is, so zero."""
    if is_duration(resolution):
        return parse_duration(resolution, "resolution").total_seconds()
    return 0.0


def history_days(hyp: Hypothesis) -> int:
    """The research window's length in days, counting both end days."""
    window = hyp.windows.research
    return (window.end - window.start).days + 1


def _within_duration(bounds: DurationBounds, seconds: float) -> bool:
    if bounds.min is not None and seconds < parse_duration(bounds.min, "min").total_seconds():
        return False
    return not (
        bounds.max is not None and seconds >= parse_duration(bounds.max, "max").total_seconds()
    )


def _within_int(bounds: IntBounds, value: int) -> bool:
    if bounds.min is not None and value < bounds.min:
        return False
    return not (bounds.max is not None and value >= bounds.max)


def applies_to(applies: Applies, hyp: Hypothesis, mode: str) -> bool:
    """Whether every clause the predicate states holds for this hypothesis and mode."""
    if applies.mechanism is not None and hyp.mechanism not in applies.mechanism:
        return False
    if applies.objective_mode is not None and applies.objective_mode != mode:
        return False
    horizon = parse_duration(hyp.horizon, "horizon").total_seconds()
    if applies.horizon is not None and not _within_duration(applies.horizon, horizon):
        return False
    if applies.resolution is not None and not _within_duration(
        applies.resolution, resolution_seconds(hyp.resolution)
    ):
        return False
    if applies.universe_size is not None and not _within_int(
        applies.universe_size, len(hyp.universe)
    ):
        return False
    if applies.data_requirements is not None and not set(applies.data_requirements) <= set(
        hyp.data_requirements
    ):
        return False
    return applies.history_days is None or _within_int(applies.history_days, history_days(hyp))


def applicable(items: Sequence[CriteriaItem], hyp: Hypothesis, mode: str) -> list[tuple[int, str]]:
    """Every applicable objective as `(priority, id)`, best first; the lowest wins."""
    return sorted(
        (item.priority, item.id)
        for item in items
        if item.kind == "objective"
        and item.applies is not None
        and item.priority is not None
        and applies_to(item.applies, hyp, mode)
    )
