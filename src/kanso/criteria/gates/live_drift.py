"""Whether a live version is still the version that was measured, or has drifted below it.

A strategy that passed its paper window arrives on the live stage carrying what composition
measured of its objective — the value, its error and the periods it was read over, struck
again at the share the live stage funds it with. This gate re-measures that objective on the
live book and asks only that it has not fallen through the bottom of the band those strike
for the window it re-measures over: a prediction band for a window of that many periods
(`kanso.criteria.band`), whose lower end a version that is what was certified falls below
one time in twenty, nominally — 0.014 to 0.075 of the time, look by look, in simulation. A
confidence band of the certified value would be no floor for a window a fraction of the
certification window's length — a version behaving exactly as certified would fall through
it far more often than that, and be demoted for being measured.

**An expectation composed before kanso recorded its error is held to the floor it carries.**
Such a version — one an upgraded workspace has not redeployed — has no error to strike a
band from, and skipping it would leave a live version held only to the daily loss and the
exposure limits until its stage is next deployed. It is judged instead as kanso judged it
before, by the lower end of the bootstrap interval it was composed with (`ci90`), which is a
confidence band of the certified value and so the tighter floor; the evidence names it.

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
skips rather than judging on a shorter sample. A rolling window that holds no period says
nothing, and is skipped. One that holds periods but is too thin to strike a band — a
Sharpe's whose scoring folds hold fewer than four periods each, which `paper_forward` waited
out on paper, or a per-trade edge's that closed no trade — is opened a period earlier at a
time, back through the version's live tenure, until it strikes one; a tenure that cannot is
skipped, named, rather than judged.

The rolling window is cut against the stage clock, so a stage replaying at speed zero rolls
in market time exactly as one running in real time would.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.band import Band, prediction
from kanso.criteria.context import Gate, GateContext, skipped, verdict
from kanso.criteria.objectives import REGISTRY, Objective
from kanso.criteria.run import CardRun
from kanso.monitor.stage import NS_PER_SECOND, StageRecord
from kanso.schemas import GateResult

NO_STAGE: Final = "no stage record was supplied, so there is no clock to roll against"
NO_PAPER_WINDOW: Final = "the paper window that set the expectation is unknown"
NO_EXPECTATION: Final = "the version carries no expectation, so there is no floor to hold"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"
NO_DISPERSION: Final = (
    "the version's expectation records no error, period count or interval, so no floor can "
    "be struck for the rolling window; the stage's next deploy measures it again"
)
TOO_SHORT: Final = "live for less than the paper window, so the two are not yet comparable"
NO_PERIOD: Final = "the rolling window holds no return period, so there is nothing to judge"
THIN: Final = (
    "the live tenure holds no window whose folds the objective scores with the periods a band "
    "needs — four a fold for a Sharpe, a closed trade for a per-trade edge — so there is "
    "nothing yet to judge"
)
LEGACY: Final = "ci90"
"""What the floor of an expectation that records no error is: its bootstrap interval's."""


class _LiveDrift:
    """The rolling live objective has not fallen below the expectation's lower bound."""

    id: ClassVar[str] = "live_drift"

    def evaluate(self, ctx: GateContext) -> GateResult:
        if not isinstance(ctx.session, StageRecord):
            return skipped(self.id, NO_STAGE)
        window_s = ctx.session.paper_window_s
        if window_s is None or window_s <= 0:
            return skipped(self.id, NO_PAPER_WINDOW)
        if ctx.expectation is None:
            return skipped(self.id, NO_EXPECTATION)
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.session.elapsed_s < window_s:
            return skipped(self.id, TOO_SHORT)
        closes = ctx.session.clock_ns + 1
        opens = closes - int(window_s * NS_PER_SECOND)
        rolling = ctx.run.between(opens, closes)
        band = prediction(objective, ctx.expectation, rolling, ctx.research_folds)
        if band is None:
            return _legacy(ctx, objective, opens, closes, window_s)
        if not band.periods:
            return skipped(self.id, NO_PERIOD)
        opens, band = _struck(ctx, objective, opens, closes, band)
        if not band.struck:
            return skipped(self.id, THIN)
        rolling = ctx.run.between(opens, closes)
        realised = _realised(ctx, objective, rolling, opens, closes)
        return verdict(
            self.id,
            realised >= band.low,
            {
                "objective": objective.id,
                "realised": realised,
                "floor": band.low,
                "expected": band.expected,
                "window_s": window_s,
                "rolling_window": [rolling.window[0].isoformat(), rolling.window[1].isoformat()],
                "n_periods": band.periods,
                "fold_periods": list(band.folds),
            },
        )


def _struck(
    ctx: GateContext, objective: Objective, opens: int, closes: int, band: Band
) -> tuple[int, Band]:
    """The rolling window's opening and its band, opened a period earlier at a time through
    the live tenure until the band is struck or the tenure has no earlier period."""
    earlier = [end for end in ctx.run.period_ends_ns if end < opens]
    while not band.struck and earlier:
        opens = earlier.pop()
        rolling = ctx.run.between(opens, closes)
        band = prediction(objective, ctx.expectation, rolling, ctx.research_folds) or band
    return opens, band


def _legacy(
    ctx: GateContext, objective: Objective, opens: int, closes: int, window_s: float
) -> GateResult:
    """An expectation with no error, held to the lower end of the interval it carries."""
    values = (ctx.expectation or {}).get("ci90")
    if not isinstance(values, list | tuple) or len(values) != 2:
        return skipped(_LiveDrift.id, NO_DISPERSION)
    floor = float(values[0])
    rolling = ctx.run.between(opens, closes)
    realised = _realised(ctx, objective, rolling, opens, closes)
    return verdict(
        _LiveDrift.id,
        realised >= floor,
        {
            "objective": objective.id,
            "realised": realised,
            "floor": floor,
            "floor_of": LEGACY,
            "window_s": window_s,
            "rolling_window": [rolling.window[0].isoformat(), rolling.window[1].isoformat()],
            "n_periods": len(rolling.returns),
        },
    )


def _realised(
    ctx: GateContext, objective: Objective, rolling: CardRun, opens: int, closes: int
) -> float:
    """The objective over the rolling window; the benchmark rolls with the book, since a
    difference is only paired over one span."""
    against = None if ctx.benchmark_run is None else ctx.benchmark_run.between(opens, closes)
    return objective.compute(rolling, ctx.research_folds, ctx.host_run, against)[0]


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


gate: Final[Gate] = _LiveDrift()
"""What the toolbox entry `live_drift` resolves to."""
