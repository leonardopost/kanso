"""The objectives: the one scalar a run optimises, and which hypotheses each applies to.

Every objective is fold-wise by construction. A statistic is computed on each of the
research window's contiguous folds and the folds are then averaged, so a fold that holds no
period scores zero and every fold weighs the same however much it holds. A relative
objective is the same statistic differenced fold by fold against the host's run, and a
benchmark objective is the same pairing against a benchmark the hypothesis declares — a
hold of its first leg, produced by the runner over the same window, data, costs and book —
so a strategy that only rode the market it trades scores nothing.

**The standard error is the error of that mean, read off the series it is a mean of.** It
used to be the spread of the fold values themselves — four numbers, three degrees of
freedom — which scattered from half to one and a half times the true error card to card
and never read the trade count at all. Folds are disjoint spans, so the mean of `K` fold
values has the variance `Σ Var(v_k) / K²`, and each fold value's variance is read off the
window's own per-period or per-trade series: the variance of a mean over the fold's count
of periods for the contribution, Mertens' variance of a Sharpe for a Sharpe, and for the
per-trade edge the variance of its linearisation period by period — the edge is a ratio of
what the trades earned to how many there were, and its error is read off how far each
period's trades moved it, so the trades of one session that one regime moves together are
priced as one session's worth and not as so many independent draws. Each is widened — never
narrowed — for serial correlation across periods (`quantities.long_run_factor`). A fold
whose value is the constant zero by construction contributes nothing. That one number
is what a card's `metric_se`, the redundancy floor, a certificate's `±`, every deflation
gate and the power a certification is stated at read; none of them computes an error of its
own.

A relative or benchmark objective's error is the paired one wherever the two runs share
their periods, because the host's own variation is in both and is not what a marginal
improvement has to beat: a contribution's per-period differences, a difference of Sharpes
by Memmel's correction of Jobson–Korkie, a difference of per-trade edges by differencing the
two runs' linearisations period by period (trades of two runs pair nowhere else). Runs
whose periods differ are taken as independent and their variances summed, which can only
overstate the error of two runs that share trades. The keep rule reads the same pairing
between two cards (`difference`): a card against the run's best is a marginal improvement
on it, and what varies in both cancels.

Which objectives can score a hypothesis is the `applies` predicate over its own attributes,
with `min` inclusive and `max` exclusive: the set an objective is named from, by the
classifying model or by the operator, and never a choice within it — `priority` orders the
list and selects nothing. The shipped set admits at least one objective in each mode at
every mechanism and horizon either side of a day, so classification always has one to name
and never has to invent one; a hypothesis that declares a benchmark is offered the Sharpe
over that benchmark in place of the absolute Sharpe, and the `benchmark` clause keeps the two
apart. At or above a day the statistic of the horizon is a Sharpe of returns, below it the
mean edge per trade, since a sub-daily holding period produces too few return periods for a
Sharpe to mean anything and enough trades for a per-trade edge to.

The contribution — the mean net return per return period in basis points of the capital —
and its marginal form apply at every horizon. It is the unit a return target is stated in,
and the one objective that counts a period in cash as a zero: a Sharpe cannot say how much
of the capital was put to work, and a per-trade edge rewards trading less — two trades a day
at ten basis points replace forty at 1.2 under it, five basis points a day of a quarter of
the capital kept over twelve. The contribution has the opposite flaw once every trade loses,
moving toward zero as losing trades are dropped, so `edge_per_trade` is declared
`required_for` both forms and every hypothesis scored on one carries it.

A resolution that names an unaggregated grain rather than a bar size is treated as the
finest grain there is — zero length — so a predicate written in durations orders it below
every bar size.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from math import fsum, sqrt
from types import MappingProxyType
from typing import ClassVar, Final, Protocol

from kanso.criteria.quantities import (
    contribution_bps,
    contribution_series,
    correlation,
    edge_bps,
    long_run_factor,
    mean,
    moments,
    sharpe,
    sharpe_difference_variance,
    sharpe_variance,
    stdev,
    variances_of_means,
)
from kanso.criteria.run import BPS, CardRun
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
BENCHMARK: Final = "benchmark"
"""The third mode: differenced against the hypothesis's benchmark run rather than a host's,
so a caller knows which second run to produce."""

SHARPE_FAMILY: Final = frozenset({"wf_sharpe_net", "marginal_wf_sharpe", "wf_sharpe_vs_hold"})
"""The objectives a deflated Sharpe has something to deflate."""


class Objective(Protocol):
    """One scalar a run is optimised against, measured fold-wise."""

    id: ClassVar[str]
    mode: ClassVar[str]
    statistic: ClassVar[str]

    def fold_values(
        self,
        run: CardRun,
        folds: int,
        host: CardRun | None = None,
        benchmark: CardRun | None = None,
    ) -> tuple[float, ...]:
        """The statistic on each fold, in window order.

        `host` is the host-alone run a relative objective differences against and
        `benchmark` the benchmark run a benchmark objective does; each objective reads the
        one its mode names, so a caller hands over both whenever it has them.
        """
        ...

    def compute(
        self,
        run: CardRun,
        folds: int,
        host: CardRun | None = None,
        benchmark: CardRun | None = None,
    ) -> tuple[float, float]:
        """The metric and its standard error: the mean of the folds, and the error of that
        mean read off the window's per-period or per-trade series."""
        ...


STATISTICS: Final[Mapping[str, Callable[[CardRun], float]]] = MappingProxyType(
    {"sharpe": sharpe, "edge_bps": edge_bps, "contribution_bps": contribution_bps}
)
"""The per-fold quantities an objective is built from, named so a class can select one."""

Folds = tuple[CardRun, ...]

Variances = Callable[[CardRun, Folds, CardRun | None, Folds], list[float]]
"""Each fold value's sampling variance, from the run and its folds and — for a relative or
benchmark objective — the run it is differenced against and that run's folds."""


class _FoldObjective:
    """A statistic averaged over folds, absolutely, against a host run or a benchmark run."""

    id: ClassVar[str]
    mode: ClassVar[str]
    statistic: ClassVar[str]

    def fold_values(
        self,
        run: CardRun,
        folds: int,
        host: CardRun | None = None,
        benchmark: CardRun | None = None,
    ) -> tuple[float, ...]:
        against = None if self.mode == ABSOLUTE else _against(self, host, benchmark)
        return self._values(run.folds(folds), () if against is None else against.folds(folds))

    def compute(
        self,
        run: CardRun,
        folds: int,
        host: CardRun | None = None,
        benchmark: CardRun | None = None,
    ) -> tuple[float, float]:
        against = None if self.mode == ABSOLUTE else _against(self, host, benchmark)
        own = run.folds(folds)
        other = () if against is None else against.folds(folds)
        each = VARIANCES[self.statistic](run, own, against, other)
        return mean(self._values(own, other)), sqrt(fsum(each)) / folds

    def _values(self, own: Folds, other: Folds) -> tuple[float, ...]:
        measure = STATISTICS[self.statistic]
        values = tuple(measure(fold) for fold in own)
        if not other:
            return values
        return tuple(a - measure(b) for a, b in zip(values, other, strict=True))


def _paired(run: CardRun, against: CardRun) -> bool:
    """Whether two runs struck their returns at the same instants, and so pair period by
    period."""
    return run.period_ends_ns == against.period_ends_ns


def _summed(left: Sequence[float], right: Sequence[float]) -> list[float]:
    """Two independent estimates' variances, fold by fold: the variance of a difference of
    two runs that share nothing, and an overstatement of it for two that share trades."""
    return [a + b for a, b in zip(left, right, strict=True)]


def _of_means(folds: Folds, series: Callable[[CardRun], tuple[float, ...]]) -> list[float]:
    """The variance of each fold's mean of `series`, its dispersion read off all of them."""
    each = [series(fold) for fold in folds]
    return variances_of_means([v for part in each for v in part], [len(part) for part in each])


def _contribution_variances(
    run: CardRun, own: Folds, against: CardRun | None, other: Folds
) -> list[float]:
    """A contribution is a mean of per-period returns on the capital: the variance of each
    fold's mean, of the paired per-period differences for a marginal one."""
    if against is None:
        return _of_means(own, contribution_series)
    if not _paired(run, against):
        return _summed(_of_means(own, contribution_series), _of_means(other, contribution_series))
    differences = [
        tuple(a - b for a, b in zip(contribution_series(o), contribution_series(h), strict=True))
        for o, h in zip(own, other, strict=True)
    ]
    return variances_of_means(
        [v for part in differences for v in part], [len(part) for part in differences]
    )


def _edge_variances(run: CardRun, own: Folds, against: CardRun | None, other: Folds) -> list[float]:
    """A per-trade edge's variance, read period by period: each fold's edge linearised over
    its periods, or the period-by-period difference of two runs' for a marginal one.

    Not the variance of a mean of the trades taken one by one. That reads forty clips a
    session as forty draws, and simulated in October 2026 on sixty sessions of twenty and of
    forty trades it reported 0.62 to 0.84 of the spread the edge actually had once a fiftieth
    to a tenth of each trade's variance was a session effect the session's trades shared —
    a scalper's ordinary case — so the keep floor, a certificate's `±` and the deflation were
    each that much too narrow. Read period by period, the same runs reported 0.97 to 1.15;
    on trades that share nothing it is the per-trade error again, on fewer degrees of
    freedom.
    """
    if against is None:
        return _linearised(own, edge_bps(run))
    if not _paired(run, against):
        return _summed(_linearised(own, edge_bps(run)), _linearised(other, edge_bps(against)))
    ratios = edge_bps(run), edge_bps(against)
    differences: list[float] = []
    counts: list[int] = []
    for mine, theirs in zip(own, other, strict=True):
        found = (_influence(mine, ratios[0]), _influence(theirs, ratios[1]))
        width = next((len(part) for part in found if part is not None), 0)
        if not width:
            counts.append(0)
            continue
        a, b = (part if part is not None else [0.0] * width for part in found)
        differences.extend(x - y for x, y in zip(a, b, strict=True))
        counts.append(width)
    return variances_of_means(differences, counts)


def _linearised(folds: Folds, ratio: float) -> list[float]:
    """The variance of each fold's per-trade edge, read off its periods' influences on it."""
    each = [_influence(fold, ratio) for fold in folds]
    return variances_of_means(
        [u for part in each if part is not None for u in part],
        [0 if part is None else len(part) for part in each],
    )


def _influence(fold: CardRun, ratio: float) -> list[float] | None:
    """Each period's first-order contribution to this fold's per-trade edge, or `None` for a
    fold that closed no trade, whose edge is the constant zero and varies by nothing.

    A fold's edge is a ratio — what its trades earned over how many there were — and to first
    order it moves by the mean over its periods of `u_p = (E_p − R·M_p) / M̄`, `E_p` the edges
    of the trades closing in period `p`, `M_p` their count, `M̄` the fold's trades per period
    and `R` the run's edge over the window. Its variance is therefore read off the `u_p` as any
    mean's is, over the fold's periods, and trades of one period that move together move one
    `u_p`; two runs over the same periods differ by the mean of `u_p − u'_p`, which is how a
    per-trade edge is paired when no trade of one run is a trade of the other. Taking `R`
    over the window rather than the fold leaves a fold's own departure from it in the
    influences, which can only widen the error. A trade belongs to the first period ending at
    or after its close; one that closed after the fold's last period end, which a fold's
    calendar edge can leave it, to the last; and a fold that holds no period end at all holds
    its trades in one period.
    """
    ends = fold.period_ends_ns
    periods = max(len(ends), 1)
    totals = [0.0] * periods
    counts = [0] * periods
    for trade in fold.trades:
        if trade.notional > 0:
            at = min(bisect_left(ends, trade.closed_ns), periods - 1)
            totals[at] += trade.pnl_net / trade.notional * BPS
            counts[at] += 1
    traded = sum(counts)
    if not traded:
        return None
    per_period = traded / periods
    return [(e - ratio * m) / per_period for e, m in zip(totals, counts, strict=True)]


def _sharpe_variances(
    run: CardRun, own: Folds, against: CardRun | None, other: Folds
) -> list[float]:
    """Each fold's Sharpe's variance by Mertens, or a paired difference's by Memmel.

    A fold where either run's Sharpe is the constant zero — a flat strategy, a hold that
    never moved — has nothing to pair, and its difference varies as the other Sharpe does.
    """
    mine = long_run_factor(run.returns), moments(run.returns)
    if against is None:
        return [sharpe_variance(f.returns, f.window, *mine) for f in own]
    theirs = long_run_factor(against.returns), moments(against.returns)
    paired = _paired(run, against)
    rho = correlation(run.returns, against.returns) if paired else None
    factor = max(mine[0], theirs[0])
    return [
        sharpe_difference_variance(o.returns, h.returns, o.window, factor, rho)
        if rho is not None and stdev(o.returns) > 0 and stdev(h.returns) > 0
        else sharpe_variance(o.returns, o.window, *mine)
        + sharpe_variance(h.returns, h.window, *theirs)
        for o, h in zip(own, other, strict=True)
    ]


VARIANCES: Final[Mapping[str, Variances]] = MappingProxyType(
    {
        "sharpe": _sharpe_variances,
        "edge_bps": _edge_variances,
        "contribution_bps": _contribution_variances,
    }
)
"""Each statistic's fold-value variances, named as `STATISTICS` names the statistic."""


def _against(objective: Objective, host: CardRun | None, benchmark: CardRun | None) -> CardRun:
    """The run a relative or benchmark objective differences against, or the refusal."""
    if objective.mode == BENCHMARK:
        if benchmark is None:
            raise PreconditionError(
                f"{objective.id}: a benchmark objective is measured against the benchmark's "
                "run, and none was supplied"
            )
        return benchmark
    if host is None:
        raise PreconditionError(
            f"{objective.id}: a relative objective is measured against the host's run, "
            "and none was supplied"
        )
    return host


def benchmark_level(objective: Objective, folds: int, benchmark: CardRun | None) -> float:
    """What the benchmark scored on this objective's statistic, averaged over the folds.

    Zero for an objective that measures no benchmark. A resampling of the strategy's own
    trades knows nothing of the benchmark, so this is the constant it is moved by to read
    in the objective's units: the strategy's statistic less the hold's.
    """
    if objective.mode != BENCHMARK:
        return 0.0
    measure = STATISTICS[objective.statistic]
    return mean(tuple(measure(fold) for fold in _against(objective, None, benchmark).folds(folds)))


def measures_benchmark(hyp: Hypothesis) -> bool:
    """Whether this hypothesis's objective needs a benchmark run beside the strategy's."""
    ref = hyp.objective
    found = None if ref is None else REGISTRY.get(ref.id)
    return found is not None and found.mode == BENCHMARK


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


class _WfSharpeVsHold(_FoldObjective):
    """The run's fold-wise Sharpe minus the benchmark hold's, fold by fold."""

    id = "wf_sharpe_vs_hold"
    mode = BENCHMARK
    statistic = "sharpe"


class _WfContributionBps(_FoldObjective):
    """Fold-wise mean net return per return period, in basis points of the capital."""

    id = "wf_contribution_bps"
    mode = ABSOLUTE
    statistic = "contribution_bps"


class _MarginalWfContributionBps(_FoldObjective):
    """The combined run's fold-wise contribution minus the host's, fold by fold."""

    id = "marginal_wf_contribution_bps"
    mode = RELATIVE
    statistic = "contribution_bps"


wf_sharpe_net: Final[Objective] = _WfSharpeNet()
net_edge_bps: Final[Objective] = _NetEdgeBps()
marginal_wf_sharpe: Final[Objective] = _MarginalWfSharpe()
marginal_net_edge_bps: Final[Objective] = _MarginalNetEdgeBps()
wf_sharpe_vs_hold: Final[Objective] = _WfSharpeVsHold()
wf_contribution_bps: Final[Objective] = _WfContributionBps()
marginal_wf_contribution_bps: Final[Objective] = _MarginalWfContributionBps()

REGISTRY: Final[Mapping[str, Objective]] = MappingProxyType(
    {
        objective.id: objective
        for objective in (
            wf_sharpe_net,
            net_edge_bps,
            marginal_wf_sharpe,
            marginal_net_edge_bps,
            wf_sharpe_vs_hold,
            wf_contribution_bps,
            marginal_wf_contribution_bps,
        )
    }
)
"""The implemented objectives, for a caller that already knows the id it wants."""

_MARGINAL: Final[Mapping[str, Objective]] = MappingProxyType(
    {
        objective.statistic: objective
        for objective in REGISTRY.values()
        if objective.mode == RELATIVE
    }
)
"""The relative objective of each statistic: what one run is scored by against another."""


def difference(
    objective: Objective, run: CardRun, other: CardRun, folds: int
) -> tuple[float, float]:
    """`run`'s statistic less `other`'s, fold by fold and averaged, and the error of that mean:
    `run` scored by the relative objective of `objective`'s statistic with `other` in the
    host's place, so paired period by period wherever the two struck their returns at the
    same instants, and summed as two independent errors where they did not.

    Two runs scored against one host or one hold carry the same host's or hold's part in
    each number, which cancels, so this is the difference of their metrics in every mode.
    """
    return _MARGINAL[objective.statistic].compute(run, folds, other)


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
    if applies.benchmark is not None and applies.benchmark != (hyp.benchmark is not None):
        return False
    return applies.history_days is None or _within_int(applies.history_days, history_days(hyp))


def measures_a_hold_over(items: Sequence[CriteriaItem], hyp: Hypothesis) -> bool:
    """Whether some objective that measures a benchmark admits this hypothesis's horizon.

    Read from the toolbox's `benchmark: true` clauses, in either mode, so a hypothesis can be
    refused a benchmark before it is classified: below every such horizon no classification
    could choose an objective that measures the hold.
    """
    horizon = parse_duration(hyp.horizon, "horizon").total_seconds()
    return any(
        item.kind == "objective"
        and item.applies is not None
        and item.applies.benchmark is True
        and (item.applies.horizon is None or _within_duration(item.applies.horizon, horizon))
        for item in items
    )


def applicable(items: Sequence[CriteriaItem], hyp: Hypothesis, mode: str) -> list[tuple[int, str]]:
    """Every applicable objective as `(priority, id)`, in the order they are listed; the
    priority orders the set and selects nothing in it."""
    return sorted(
        (item.priority, item.id)
        for item in items
        if item.kind == "objective"
        and item.applies is not None
        and item.priority is not None
        and applies_to(item.applies, hyp, mode)
    )
