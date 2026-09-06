"""Whether the fills a broker is actually giving cost more than the model said they would.

Everything a certificate claims rests on a cost model applied once, in the runner's
extraction: so many basis points of slippage per fill. On a live stage that number stops being
a model and becomes an observation, and the two can part company — a venue changes, liquidity
thins, an order type behaves differently in size. This gate is where the observation is held
against the assumption, and the assumption it is held against is the one the run recorded:
`costs.slippage_bps` of the venue model the extraction charged, which is the same number
composition measured the version's expectation through.

**The realised half is a fact about the broker, and only a broker can report it.** A fill
carries the price it printed at but not the price the order was worth at the instant it was
sent, so no arithmetic on the run recovers what the fill gave up; the figure is measured by
the execution client and reaches the gate on the stage record. Where the stage record carries
none, nothing was measured, and the gate says so rather than deriving a number that would only
restate the model.

**The gate measures only where the fills are brokered.** A simulated venue realises the
modelled cost by construction, so a comparison against it compares a number with itself; and an
execution client this build cannot resolve to a declaration may be a simulated one, so it is no
safer to judge. Both skip. What is left — a broker's paper account and a broker's real one —
both print against the live tape, and both are worth measuring: paper is where fill quality is
learned before real capital is at stake.

**The comparison is one-sided, like every drift test here.** Fills better than the model are
not a fault, so only excess above the stated allowance fails.

**A handful of fills is not a measurement.** The plan states how many are needed before the
comparison means anything, and below that the gate skips rather than failing a version on the
one order that crossed a wide spread.
"""

from __future__ import annotations

from math import isfinite
from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, count, number, skipped, verdict
from kanso.monitor.stage import SIMULATED, UNKNOWN, StageRecord
from kanso.schemas import GateResult

NO_CHOICE: Final = "no excess or fill count was chosen, so no fill quality was required"
NO_STAGE: Final = "no stage record was supplied, so no execution client was named"
IS_SIMULATED: Final = (
    "the execution client is simulated, and it realises the modelled cost by construction"
)
UNRESOLVED: Final = "the execution client resolves to no declaration, so it may be a simulated one"
NO_MEASUREMENT: Final = "the execution client reported no realised slippage to compare"
NOT_A_NUMBER: Final = "the realised slippage the execution client reported is not a finite number"


class _FillQualityDrift:
    """Realised slippage is no worse than the modelled slippage plus the stated excess."""

    id: ClassVar[str] = "fill_quality_drift"

    def evaluate(self, ctx: GateContext) -> GateResult:
        excess, minimum = number(ctx, "max_excess_bps"), count(ctx, "min_fills")
        if excess is None or minimum is None:
            return skipped(self.id, NO_CHOICE)
        if not isinstance(ctx.session, StageRecord):
            return skipped(self.id, NO_STAGE)
        record = ctx.session
        if record.funding == SIMULATED:
            return skipped(self.id, IS_SIMULATED)
        if record.funding == UNKNOWN:
            return skipped(self.id, UNRESOLVED)
        if record.n_fills < minimum:
            return skipped(self.id, f"only {record.n_fills} fills, fewer than the {minimum} asked")
        realised = record.realised_slippage_bps
        if realised is None:
            return skipped(self.id, NO_MEASUREMENT)
        if not isfinite(realised):
            return skipped(self.id, NOT_A_NUMBER)
        modelled = _modelled(ctx)
        return verdict(
            self.id,
            realised - modelled <= excess,
            {
                "funding": record.funding,
                "realised_bps": realised,
                "modelled_bps": modelled,
                "excess_bps": realised - modelled,
                "max_excess_bps": excess,
                "n_fills": record.n_fills,
                "min_fills": minimum,
            },
        )


def _modelled(ctx: GateContext) -> float:
    """The slippage the venue model charged; zero when the run records no usable number.

    A run whose venue model states no slippage charged none, so the whole realised figure
    is excess. That is the strict reading and the safe one: it can only fail a version the
    model made no promise about, never pass one.
    """
    costs = ctx.run.venue_model.get("costs")
    if isinstance(costs, dict):
        value = costs.get("slippage_bps")
        if isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value):
            return float(value)
    return 0.0


gate: Final[Gate] = _FillQualityDrift()
"""What the toolbox entry `fill_quality_drift` resolves to."""
