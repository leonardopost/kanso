"""Whether a live version is still the version that was measured, or has drifted below it.

A strategy that passed its paper window arrives on the live stage carrying one number: the
interval composition measured for its objective. This gate re-measures that objective on the
live book and asks only that it has not fallen through the bottom of that interval.

**The comparison is one-sided.** A version doing better than expected is not evidence of a
fault, and demoting on it would demote every version that met a good market. Only the lower
bound is a failure, because only the lower bound says the thing being traded is no longer the
thing that was certified.

**The window is the paper window, not the whole live book.** The expectation was confirmed
over a paper window of a stated length, so the live measurement that can be compared with it
is one of the same length: the most recent one. Averaging over a live book months long would
hide a month of drift inside a good quarter, and measuring over a day would fail on noise. The
gate therefore says nothing until the version has been live at least as long as it was on
paper — that is the point at which the two measurements become comparable, and until then it
skips rather than judging on a shorter sample.

The rolling window is cut against the stage clock, so a stage replaying at speed zero rolls
in market time exactly as one running in real time would.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, skipped, verdict
from kanso.criteria.objectives import REGISTRY, Objective
from kanso.monitor.stage import NS_PER_SECOND, StageRecord
from kanso.schemas import GateResult

NO_STAGE: Final = "no stage record was supplied, so there is no clock to roll against"
NO_PAPER_WINDOW: Final = "the paper window that set the expectation is unknown"
NO_EXPECTATION: Final = "the version carries no expectation, so there is no floor to hold"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"
TOO_SHORT: Final = "live for less than the paper window, so the two are not yet comparable"


class _LiveDrift:
    """The rolling live objective has not fallen below the expectation's lower bound."""

    id: ClassVar[str] = "live_drift"

    def evaluate(self, ctx: GateContext) -> GateResult:
        if not isinstance(ctx.session, StageRecord):
            return skipped(self.id, NO_STAGE)
        window_s = ctx.session.paper_window_s
        if window_s is None or window_s <= 0:
            return skipped(self.id, NO_PAPER_WINDOW)
        floor = _floor(ctx)
        if floor is None:
            return skipped(self.id, NO_EXPECTATION)
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.session.elapsed_s < window_s:
            return skipped(self.id, TOO_SHORT)
        closes = ctx.session.clock_ns + 1
        rolling = ctx.run.between(closes - int(window_s * NS_PER_SECOND), closes)
        realised = objective.compute(rolling, ctx.research_folds, ctx.host_run)[0]
        return verdict(
            self.id,
            realised >= floor,
            {
                "objective": objective.id,
                "realised": realised,
                "floor": floor,
                "window_s": window_s,
                "rolling_window": [rolling.window[0].isoformat(), rolling.window[1].isoformat()],
                "n_periods": len(rolling.returns),
            },
        )


def _floor(ctx: GateContext) -> float | None:
    """The lower bound of the version's 90% interval, or `None` when it has none."""
    values = (ctx.expectation or {}).get("ci90")
    if not isinstance(values, list | tuple) or len(values) != 2:
        return None
    return float(values[0])


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


gate: Final[Gate] = _LiveDrift()
"""What the toolbox entry `live_drift` resolves to."""
