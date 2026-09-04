"""The gate toolbox: the tests a card, a certificate, a paper stage or a live stage runs.

A gate carries no default and no threshold. Which gates run and with which values is a
runtime decision — the classifier's for the card stage, the planner's for everything after
— taken inside the ranges the library declares. What lives here is only the arithmetic of
each test and the honesty rule that goes with it: a gate that cannot reach its context
passes and records why it judged nothing, so an absent deployed book or an unset threshold
never manufactures a failure, and a certificate's verdict is the conjunction of the gates
that actually ran.

The card-stage gates judge a research card. The certification gates judge the pinned
strategy over the certification window, several of them against the research window as
well: an out-of-sample result is only meaningful beside the in-sample one it is supposed
to survive. Two of them are deliberately cheap in a different way — `cost_stress`
recomputes from the fills the runner already recorded instead of running the engine again,
which is possible only because costs are applied once, in the extraction, and never inside
the simulated venue.

Six gates are declared in the library and implemented later, when the machinery they read
exists: `parity_replay` and `param_plateau` need a second engine run, and `paper_forward`,
`live_drift`, `fill_quality_drift` and `daily_loss_kill` need a deployed stage.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import replace
from datetime import date
from hashlib import sha256
from math import e, sqrt
from statistics import NormalDist
from typing import ClassVar, Final

import numpy as np

from kanso.criteria.context import (
    Gate,
    GateContext,
    count,
    number,
    skipped,
    verdict,
)
from kanso.criteria.integrity import check as check_integrity
from kanso.criteria.objectives import REGISTRY, SHARPE_FAMILY, Objective
from kanso.criteria.quantities import (
    correlation,
    drawdown_pct,
    mean,
    moments,
    periods_per_year,
    variance,
    years,
)
from kanso.criteria.run import BPS, CardRun, day_of
from kanso.schemas import GateResult

EULER_MASCHERONI: Final = 0.5772156649015329
"""The constant in the expected maximum of a sample of Sharpe ratios."""

BLOCK: Final = 1000
"""Bootstrap replications resampled at a time, so a large `n` stays in memory."""

NO_OBJECTIVE = "the hypothesis carries no objective to measure"
NO_RESEARCH_RUN = "no research-window run was supplied to compare against"


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


def _metric(objective: Objective, run: CardRun, ctx: GateContext, host: CardRun | None) -> float:
    return objective.compute(run, ctx.research_folds, host)[0]


class _StrategyIntegrity:
    """The static half of the anti-cheat boundary: imports, identifiers and scope."""

    id: ClassVar[str] = "strategy_integrity"

    def evaluate(self, ctx: GateContext) -> GateResult:
        if ctx.lane_dir is None:
            return skipped(self.id, "no lane directory was supplied, so nothing was inspected")
        problems = check_integrity(ctx.lane_dir, ctx.pinned)
        return verdict(
            self.id,
            not problems,
            {"n_problems": len(problems), "problems": problems[:20]},
        )


class _MinTrades:
    """Enough closed trades overall, and at least one in every fold."""

    id: ClassVar[str] = "min_trades"

    def evaluate(self, ctx: GateContext) -> GateResult:
        minimum = count(ctx, "min")
        if minimum is None:
            return skipped(self.id, "no minimum was chosen, so no count was required")
        per_fold = [len(fold.trades) for fold in ctx.run.folds(ctx.research_folds)]
        total = len(ctx.run.trades)
        return verdict(
            self.id,
            total >= minimum and all(n >= 1 for n in per_fold),
            {"n_trades": total, "min": minimum, "trades_per_fold": per_fold},
        )


class _MaxDrawdown:
    """Peak-to-trough equity inside the hypothesis's own drawdown limit."""

    id: ClassVar[str] = "max_drawdown"

    def evaluate(self, ctx: GateContext) -> GateResult:
        limit = ctx.hyp.risk_limits.max_drawdown_pct
        observed = drawdown_pct(ctx.run)
        return verdict(
            self.id,
            observed <= limit,
            {"max_drawdown_pct": observed, "limit_pct": limit},
        )


class _EmbargoedWindow:
    """The objective survives the embargo: positive out of sample, and not a collapse."""

    id: ClassVar[str] = "embargoed_window"

    def evaluate(self, ctx: GateContext) -> GateResult:
        fraction = number(ctx, "min_fraction")
        if fraction is None:
            return skipped(self.id, "no minimum fraction was chosen, so no floor was required")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.research_run is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        certified = _metric(objective, ctx.run, ctx, ctx.host_run)
        researched = _metric(objective, ctx.research_run, ctx, ctx.host_research_run)
        return verdict(
            self.id,
            certified > 0 and certified >= fraction * researched,
            {
                "objective": objective.id,
                "certification": certified,
                "research": researched,
                "min_fraction": fraction,
            },
        )


class _WalkForwardConsistency:
    """Most research folds positive, and the certification fold not the worst of them."""

    id: ClassVar[str] = "walk_forward_consistency"

    def evaluate(self, ctx: GateContext) -> GateResult:
        required = count(ctx, "min_positive_folds")
        if required is None:
            return skipped(self.id, "no fold count was chosen, so no consistency was required")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.research_run is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        folds = objective.fold_values(ctx.research_run, ctx.research_folds, ctx.host_research_run)
        certified = _metric(objective, ctx.run, ctx, ctx.host_run)
        positive = sum(1 for value in folds if value > 0)
        return verdict(
            self.id,
            positive >= required and certified > min(folds),
            {
                "objective": objective.id,
                "folds": list(folds),
                "positive_folds": positive,
                "min_positive_folds": required,
                "certification": certified,
            },
        )


class _DeflatedSharpe:
    """The research Sharpe, deflated by how many trials went into selecting it.

    The estimate deflated is the one selection acted on — the research-window metric the
    keep rule compared, which is what the trial count counts. The expected maximum it is
    measured against is built from that trial count and the spread of the non-crash cards'
    own metrics, with the length, skewness and kurtosis of the research return series
    supplying the sampling distribution. Both the estimate and the spread are taken back
    out of annualised units first, so the deflation is done in the units the return series
    was actually sampled in.
    """

    id: ClassVar[str] = "deflated_sharpe"

    def evaluate(self, ctx: GateContext) -> GateResult:
        floor = number(ctx, "min_dsr")
        if floor is None:
            return skipped(self.id, "no minimum was chosen, so no deflation was required")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if objective.id not in SHARPE_FAMILY:
            return skipped(
                self.id,
                f"the objective {objective.id} is not a Sharpe, so no card computed one to deflate",
            )
        if ctx.research_run is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        metrics = list(ctx.card_metrics)
        if len(metrics) < 2:
            return skipped(self.id, "fewer than two non-crash cards, so trial spread is unknown")
        returns = ctx.research_run.returns
        shape = moments(returns)
        if len(returns) < 3 or shape is None:
            return skipped(self.id, "the research return series is too short or does not vary")
        scale = sqrt(periods_per_year(ctx.research_run))
        estimate = _metric(objective, ctx.research_run, ctx, ctx.host_research_run) / scale
        expected = self._expected_maximum(
            variance(metrics) / periods_per_year(ctx.research_run), ctx.n_trials
        )
        skewness, kurtosis = shape
        denominator = 1 - skewness * estimate + (kurtosis - 1) / 4 * estimate**2
        if denominator <= 0:
            return skipped(self.id, "the sampling variance of the estimate is not positive")
        dsr = NormalDist().cdf((estimate - expected) * sqrt(len(returns) - 1) / sqrt(denominator))
        return verdict(
            self.id,
            dsr >= floor,
            {
                "dsr": dsr,
                "min_dsr": floor,
                "sharpe": estimate,
                "expected_maximum": expected,
                "n_trials": ctx.n_trials,
                "n_cards": len(metrics),
                "skew": skewness,
                "kurtosis": kurtosis,
            },
        )

    @staticmethod
    def _expected_maximum(trial_variance: float, n_trials: int) -> float:
        """The Sharpe a search of this width is expected to produce from pure noise."""
        trials = max(n_trials, 2)
        normal = NormalDist()
        return sqrt(trial_variance) * (
            (1 - EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / trials)
            + EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (trials * e))
        )


def stressed(run: CardRun, multiplier: float) -> CardRun:
    """The same run with every recorded cost multiplied, re-applied to its own series.

    Costs are applied once, by the runner, in the extraction that produced this run, so
    multiplying them is arithmetic on the recorded fills rather than another backtest. A
    fill is charged to the return period it falls in; a fill after the last period end
    changed no return and so changes none here.
    """
    extra = multiplier - 1.0
    ends = run.period_ends_ns
    added = [0.0] * len(ends)
    for fill in run.fills:
        index = bisect_left(ends, fill.ts_ns)
        if index < len(ends):
            added[index] += fill.cost * extra
    running = 0.0
    equity: list[float] = []
    for value, charge in zip(run.equity, added, strict=True):
        running += charge
        equity.append(value - running)
    return replace(
        run,
        returns=tuple(r - charge for r, charge in zip(run.returns, added, strict=True)),
        equity=tuple(equity),
        trades=tuple(
            replace(t, pnl_net=t.pnl_net - t.cost * extra, cost=t.cost * multiplier)
            for t in run.trades
        ),
        fills=tuple(replace(f, cost=f.cost * multiplier) for f in run.fills),
    )


class _CostStress:
    """The edge survives costs worse than the venue's, at two multiples of them."""

    id: ClassVar[str] = "cost_stress"

    def evaluate(self, ctx: GateContext) -> GateResult:
        first, second = number(ctx, "mult_a"), number(ctx, "mult_b")
        if first is None or second is None:
            return skipped(self.id, "no cost multiples were chosen, so nothing was stressed")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        at_first = _metric(objective, stressed(ctx.run, first), ctx, ctx.host_run)
        at_second = _metric(objective, stressed(ctx.run, second), ctx, ctx.host_run)
        return verdict(
            self.id,
            at_first > 0 and at_second >= 0,
            {
                "objective": objective.id,
                "mult_a": first,
                "mult_b": second,
                "metric_a": at_first,
                "metric_b": at_second,
            },
        )


class _Bootstrap:
    """What the trade sequence could have looked like, and how deep it could have drawn.

    A replication resamples the closed trades with replacement, keeping the population and
    losing the order, and rebuilds the equity path from the starting capital. The path
    gives the drawdown distribution, which is what the gate judges; the same draw gives
    the objective's own statistic, which is recorded as evidence and becomes a deployed
    version's expectation. The statistic is the run's objective family measured on the
    resampled trades — a Sharpe of the trade series, or its mean edge per trade.
    """

    id: ClassVar[str] = "bootstrap"

    def evaluate(self, ctx: GateContext) -> GateResult:
        replications = count(ctx, "n")
        if replications is None:
            return skipped(self.id, "no replication count was chosen, so nothing was resampled")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if len(ctx.run.trades) < 2:
            return skipped(self.id, "fewer than two closed trades, so there is nothing to resample")
        limit = ctx.hyp.risk_limits.max_drawdown_pct
        low, high, worst = self._resample(ctx, objective, replications)
        return verdict(
            self.id,
            worst <= limit,
            {
                "objective": objective.id,
                "objective_ci90": [low, high],
                "mdd_p95": worst,
                "limit_pct": limit,
                "n": replications,
            },
        )

    @staticmethod
    def _resample(
        ctx: GateContext, objective: Objective, replications: int
    ) -> tuple[float, float, float]:
        trades = ctx.run.trades
        pnl = np.array([t.pnl_net for t in trades], dtype=float)
        edge = np.array(
            [t.pnl_net / t.notional * BPS if t.notional > 0 else 0.0 for t in trades],
            dtype=float,
        )
        capital = ctx.run.capital
        per_year = len(trades) / years(ctx.run.window)
        rng = np.random.default_rng(
            int.from_bytes(sha256(ctx.strategy_sha.encode()).digest()[:8], "big")
        )
        drawdowns: list[np.ndarray] = []
        statistics: list[np.ndarray] = []
        for start in range(0, replications, BLOCK):
            width = min(BLOCK, replications - start)
            draws = rng.integers(0, len(trades), size=(width, len(trades)))
            sample = pnl[draws]
            path = capital + np.cumsum(sample, axis=1)
            peak = np.maximum(np.maximum.accumulate(path, axis=1), capital)
            drawdowns.append(((peak - path) / capital * 100.0).max(axis=1))
            statistics.append(
                _sharpe_of(sample, per_year)
                if objective.id in SHARPE_FAMILY
                else edge[draws].mean(axis=1)
            )
        spread = np.concatenate(statistics)
        return (
            float(np.percentile(spread, 5)),
            float(np.percentile(spread, 95)),
            float(np.percentile(np.concatenate(drawdowns), 95)),
        )


def _sharpe_of(sample: np.ndarray, per_year: float) -> np.ndarray:
    """Annualised Sharpe of each resampled row; a row that cannot vary scores zero."""
    dispersion = sample.std(axis=1, ddof=1)
    safe = np.where(dispersion > 0, dispersion, 1.0)
    return np.where(dispersion > 0, sample.mean(axis=1) / safe * sqrt(per_year), 0.0)


class _BookCorrelation:
    """The candidate's returns are not another deployed book's returns again."""

    id: ClassVar[str] = "book_correlation"

    def evaluate(self, ctx: GateContext) -> GateResult:
        ceiling = number(ctx, "max_corr")
        if ceiling is None:
            return skipped(self.id, "no ceiling was chosen, so no correlation was required")
        if not ctx.deployed:
            return skipped(self.id, "nothing is deployed to correlate against")
        own = dict(zip(ctx.run.period_ends_ns, ctx.run.returns, strict=True))
        found: dict[str, float] = {}
        for book in ctx.deployed:
            paired = [
                (own[ts], value)
                for ts, value in zip(book.period_ends_ns, book.returns, strict=True)
                if ts in own
            ]
            value = correlation([a for a, _ in paired], [b for _, b in paired])
            if value is not None:
                found[book.id] = value
        if not found:
            return skipped(self.id, "no deployed book overlaps this window enough to correlate")
        return verdict(
            self.id,
            max(found.values()) <= ceiling,
            {"correlations": found, "max_corr": ceiling},
        )


class _PublicationLag:
    """Every delayed dataset in the pinned snapshot was published late enough to be real."""

    id: ClassVar[str] = "publication_lag"

    def evaluate(self, ctx: GateContext) -> GateResult:
        tolerance = number(ctx, "tolerance_s")
        if tolerance is None:
            return skipped(self.id, "no tolerance was chosen, so no lag was required")
        if not ctx.datasets:
            return skipped(self.id, "the pinned snapshot's datasets were not supplied")
        unknown = [d.dataset_id for d in ctx.datasets if d.publication == "unknown"]
        early = [
            d.dataset_id
            for d in ctx.datasets
            if d.publication == "delayed" and d.min_lag_s + tolerance < d.required_lag_s
        ]
        return verdict(
            self.id,
            not unknown and not early,
            {
                "unknown": unknown,
                "published_too_early": early,
                "tolerance_s": tolerance,
                "n_datasets": len(ctx.datasets),
            },
        )


class _CapacityVsAdv:
    """A day's traded notional stays a small share of what the instrument actually trades."""

    id: ClassVar[str] = "capacity_vs_adv"

    def evaluate(self, ctx: GateContext) -> GateResult:
        participation, window = number(ctx, "participation"), count(ctx, "adv_days")
        if participation is None or window is None:
            return skipped(self.id, "no participation limit was chosen, so nothing was capped")
        if not ctx.daily_volume:
            return skipped(self.id, "no volume data, so there is no capacity to compare against")
        peaks = _peak_daily_notional(ctx)
        judged: dict[str, dict[str, float]] = {}
        for instrument, peak in sorted(peaks.items()):
            series = ctx.daily_volume.get(instrument)
            if not series:
                continue
            adv = mean(list(series)[-window:])
            judged[instrument] = {"peak_notional": peak, "adv": adv, "cap": participation * adv}
        if not judged:
            return skipped(self.id, "nothing traded here has a volume series to compare against")
        return verdict(
            self.id,
            all(v["peak_notional"] <= v["cap"] for v in judged.values()),
            {"instruments": judged, "participation": participation, "adv_days": window},
        )


def _peak_daily_notional(ctx: GateContext) -> dict[str, float]:
    """The busiest single day of traded notional, per instrument."""
    per_day: dict[tuple[str, date], float] = {}
    for fill in ctx.run.fills:
        key = (fill.instrument_id, day_of(fill.ts_ns))
        per_day[key] = per_day.get(key, 0.0) + fill.notional
    peaks: dict[str, float] = {}
    for (instrument, _), value in per_day.items():
        peaks[instrument] = max(peaks.get(instrument, 0.0), value)
    return peaks


strategy_integrity: Final[Gate] = _StrategyIntegrity()
min_trades: Final[Gate] = _MinTrades()
max_drawdown: Final[Gate] = _MaxDrawdown()
embargoed_window: Final[Gate] = _EmbargoedWindow()
walk_forward_consistency: Final[Gate] = _WalkForwardConsistency()
deflated_sharpe: Final[Gate] = _DeflatedSharpe()
cost_stress: Final[Gate] = _CostStress()
bootstrap: Final[Gate] = _Bootstrap()
book_correlation: Final[Gate] = _BookCorrelation()
publication_lag: Final[Gate] = _PublicationLag()
capacity_vs_adv: Final[Gate] = _CapacityVsAdv()

PENDING: Final = frozenset(
    {
        "parity_replay",
        "param_plateau",
        "paper_forward",
        "live_drift",
        "fill_quality_drift",
        "daily_loss_kill",
    }
)
"""Declared in the library; implemented when a second engine run or a stage node exists."""
