"""Every window a stage has closed for one version, joined into the run its gates judge.

A stage node flattens before it stops, so each redeploy closes a window and records what that
window realised. A version's life on a stage is therefore a sequence of those windows rather
than one, and a paper gate asking "how has this behaved since it joined?" has to see all of
them as one run.

**The equity curve is rebuilt, not concatenated.** Inside a window, equity is capital plus the
running sum of the returns; across two windows the second starts from its own capital again,
so laying them end to end would step back at every seam. Summing the returns from the first
window's capital is the same arithmetic within a window and the only one that survives the
join.

**Two windows can share a calendar day.** A stage stopped mid-session and restarted the same
day produces two partial periods ending at the same instant, and a run's periods are strictly
increasing. They are added together, which is what one day's return means.

**A benchmark is joined the same way.** Each window a version measured against a hold
recorded that hold beside it, and the windows' holds are one run by the same arithmetic, so
the two runs a gate differences share their period ends. A version with any window that
recorded no hold has no benchmark, rather than one that skips a window.

**The book is the last window's, not every window's.** Positions are recorded before the
flatten, so each window's book is what that window ended holding; only the most recent one is
still standing, and that is what a stage exposure limit is about.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from kanso.criteria.run import NS_PER_DAY, CardRun, day_of, midnight_ns

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.portfolio.records import StageResult

__all__ = ["Tenure", "combined", "combined_benchmark", "joined", "tenure"]


@dataclass(frozen=True)
class Tenure:
    """One version's whole life on one stage: the run, the book and the two clock readings."""

    stage: str
    strategy_id: str
    version: int
    run: CardRun
    positions: tuple[tuple[str, float, float], ...]
    joined_ns: int
    clock_ns: int
    windows: int
    benchmark: CardRun | None = None

    @property
    def gross(self) -> float:
        """The absolute value of what this version holds, however it is netted."""
        return sum(abs(qty * price) for _, qty, price in self.positions)

    @property
    def net(self) -> float:
        """The signed value of what this version holds."""
        return sum(qty * price for _, qty, price in self.positions)

    def day_pnl(self, day: date) -> float:
        """What this version made over the return periods that ended on one calendar day."""
        return sum(
            value
            for ts, value in zip(self.run.period_ends_ns, self.run.returns, strict=True)
            if day_of(ts) == day
        )


def combined(results: Sequence[StageResult]) -> CardRun | None:
    """The windows a stage closed for one version, as one continuous run."""
    return joined([result.run for result in results])


def combined_benchmark(results: Sequence[StageResult]) -> CardRun | None:
    """The holds those windows recorded beside the version, as one run; `None` unless
    every window recorded one."""
    holds = [result.benchmark for result in results]
    if not holds or any(hold is None for hold in holds):
        return None
    return joined([hold for hold in holds if hold is not None])


def joined(runs: Sequence[CardRun]) -> CardRun | None:
    """Consecutive windows' runs as one continuous run."""
    if not runs:
        return None
    per_end: dict[int, float] = {}
    for run in runs:
        for ts, value in zip(run.period_ends_ns, run.returns, strict=True):
            per_end[ts] = per_end.get(ts, 0.0) + value
    ends = tuple(sorted(per_end))
    returns = tuple(per_end[ts] for ts in ends)
    capital = runs[0].capital
    running = capital
    equity: list[float] = []
    for value in returns:
        running += value
        equity.append(running)
    last = runs[-1]
    return CardRun(
        window=(runs[0].window[0], last.window[1]),
        period=last.period,
        period_ends_ns=ends,
        returns=returns,
        equity=tuple(equity),
        trades=tuple(
            sorted(
                (trade for run in runs for trade in run.trades),
                key=lambda trade: trade.closed_ns,
            )
        ),
        fills=tuple(
            sorted(
                (fill for run in runs for fill in run.fills),
                key=lambda fill: fill.ts_ns,
            )
        ),
        held=tuple(
            sorted(
                (item for run in runs for item in run.held),
                key=lambda item: (item.ts_ns, item.instrument_id),
            )
        ),
        capital=capital,
        currency=last.currency,
        venue_model=last.venue_model,
    )


def tenure(stage: str, results: Sequence[StageResult], clock_ns: int | None) -> Tenure | None:
    """One version's stage record, or `None` when its stage has closed no window yet.

    `joined_ns` is the stage clock where the version's measurement begins, which is the
    first window's own start; `clock_ns` is where the stage stands now, falling back to the
    end of the last window when no session has recorded a clock.
    """
    run = combined(results)
    if run is None:
        return None
    return Tenure(
        stage=stage,
        strategy_id=results[0].strategy_id,
        version=results[0].version,
        run=run,
        positions=results[-1].positions,
        joined_ns=midnight_ns(run.window[0]),
        clock_ns=(
            clock_ns if clock_ns is not None else midnight_ns(run.window[1]) + NS_PER_DAY - 1
        ),
        windows=len(results),
        benchmark=combined_benchmark(results),
    )
