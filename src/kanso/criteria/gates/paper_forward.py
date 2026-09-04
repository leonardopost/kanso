"""Whether a version has been on paper long enough, and behaved the way it said it would.

This is the only gate between a composed strategy and a request for real capital, so it
answers three questions at once and fails if any of them answers badly.

**Has enough of the stage's clock passed?** The plan names a floor two ways — an absolute
minimum and a multiple of the hypothesis's own holding period — and the longer of the two is
what must have elapsed since the version joined the stage. Both readings come from the stage
clock, so a stage replaying at speed zero is measured in market time like every other.

**Did it do what composition said it would?** Composition measured the version over the
sleeve's certification window and recorded a 90% interval for the objective. The realised
paper objective must fall inside that interval — inside, not merely above it. A result above
the band is not a better strategy; it is the paper stage failing to reproduce the model that
was certified, and promoting on it would promote an unexplained difference.

**Did it stay inside the risk the hypothesis declared?** Of the sleeve's card-stage
constraints only the drawdown limit is evaluated. `min_trades` is recorded as skipped: it
counts trades over a research window years long, a paper window cannot hold that many, and
applying it would make `promotable` unreachable however well the version behaved. Trade
sufficiency in paper is already the planner's decision, expressed as the minimum duration and
the horizon multiple this gate measures. Any other card-stage constraint is recorded as not a
paper test — `strategy_integrity` inspects a lane directory that a deployment does not have.

A redeploy flattens the stage and restarts the clock, so the window this gate measures is the
one since the current join and not the whole life of the version.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, number, skipped, verdict
from kanso.criteria.objectives import REGISTRY, Objective
from kanso.criteria.quantities import drawdown_pct
from kanso.monitor.stage import StageRecord
from kanso.schemas import GateResult, ParamValue, parse_duration

MIN_TRADES: Final = "min_trades"
MAX_DRAWDOWN: Final = "max_drawdown"

NO_WINDOW: Final = "no paper window was chosen, so no duration was required"
NO_STAGE: Final = "no stage record was supplied, so there is no clock to measure against"
NO_EXPECTATION: Final = "the version carries no expectation, so there is no band to fall in"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"

TRADES_UNREACHABLE: Final = (
    "a research-window trade count cannot be met in a paper window, and requiring it would "
    "make promotable unreachable"
)
NOT_A_PAPER_TEST: Final = "not a test a deployed stage can run"


class _PaperForward:
    """Long enough on paper, inside the expectation, and inside the drawdown limit."""

    id: ClassVar[str] = "paper_forward"

    def evaluate(self, ctx: GateContext) -> GateResult:
        duration, multiple = _duration(ctx.params.get("min_duration")), number(ctx, "horizon_mult")
        if duration is None or multiple is None:
            return skipped(self.id, NO_WINDOW)
        if not isinstance(ctx.session, StageRecord):
            return skipped(self.id, NO_STAGE)
        band = _band(ctx)
        if band is None:
            return skipped(self.id, NO_EXPECTATION)
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        horizon = parse_duration(ctx.hyp.horizon, "horizon").total_seconds()
        required = max(duration, multiple * horizon)
        elapsed = ctx.session.elapsed_s
        low, high = band
        realised = objective.compute(ctx.run, ctx.research_folds, ctx.host_run)[0]
        observed = drawdown_pct(ctx.run)
        limit = ctx.hyp.risk_limits.max_drawdown_pct
        constraints, within_risk = _constraints(ctx, observed, limit)
        return verdict(
            self.id,
            elapsed >= required and low <= realised <= high and within_risk,
            {
                "elapsed_s": elapsed,
                "required_s": required,
                "horizon_mult": multiple,
                "objective": objective.id,
                "realised": realised,
                "ci90": [low, high],
                "max_drawdown_pct": observed,
                "limit_pct": limit,
                "constraints": constraints,
            },
        )


def _constraints(ctx: GateContext, observed: float, limit: float) -> tuple[dict[str, object], bool]:
    """The sleeve's card-stage constraints as a paper window is able to judge them."""
    judged: dict[str, object] = {}
    within = True
    for constraint in ctx.hyp.constraints or ():
        if constraint.id == MAX_DRAWDOWN:
            held = observed <= limit
            judged[MAX_DRAWDOWN] = held
            within = within and held
        elif constraint.id == MIN_TRADES:
            judged[MIN_TRADES] = TRADES_UNREACHABLE
        else:
            judged[constraint.id] = NOT_A_PAPER_TEST
    return judged, within


def _duration(value: ParamValue | None) -> float | None:
    """The chosen minimum in seconds, or `None` when the planner chose none."""
    if not isinstance(value, str):
        return None
    return parse_duration(value, "min_duration").total_seconds()


def _band(ctx: GateContext) -> tuple[float, float] | None:
    """The version's 90% interval for the objective, or `None` when it has none."""
    values = (ctx.expectation or {}).get("ci90")
    if not isinstance(values, list | tuple) or len(values) != 2:
        return None
    return float(values[0]), float(values[1])


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


gate: Final[Gate] = _PaperForward()
"""What the toolbox entry `paper_forward` resolves to."""
