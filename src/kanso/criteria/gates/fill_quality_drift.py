"""Whether the fills a broker is actually giving cost more than the model said they would.

Everything a certificate claims rests on a cost model applied once, in the runner's
extraction: so many basis points of slippage per fill. On a live stage that number stops being
a model and becomes an observation, and the two can part company — a venue changes, liquidity
thins, an order type behaves differently in size. This gate is where the observation is held
against the assumption.

**It skips on a simulated execution client, which in this version is every one of them.** A
simulated venue fills at the price the cost model says it fills at, so the difference between
realised and modelled slippage is zero by construction and a gate that reported a pass on it
would be reporting arithmetic rather than evidence. The honest verdict is that nothing was
measured, and that is what it records. A broker execution client arrives with the broker
adapters; the gate is written now so that it is the toolbox, not the adapter, that decides
what good fills are.

**A handful of fills is not a measurement.** The plan states how many are needed before the
comparison means anything, and below that the gate skips rather than failing a version on the
one order that crossed a wide spread.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, count, number, skipped, verdict
from kanso.monitor.stage import SIMULATED, StageRecord
from kanso.schemas import GateResult

NO_CHOICE: Final = "no excess or fill count was chosen, so no fill quality was required"
NO_STAGE: Final = "no stage record was supplied, so no execution client was named"
IS_SIMULATED: Final = (
    "the execution client is simulated, and it realises the modelled cost by construction"
)
NO_MEASUREMENT: Final = "the execution client reported no realised slippage to compare"


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
        realised = record.realised_slippage_bps
        if realised is None:
            return skipped(self.id, NO_MEASUREMENT)
        if record.n_fills < minimum:
            return skipped(self.id, f"only {record.n_fills} fills, fewer than the {minimum} asked")
        modelled = _modelled(ctx)
        return verdict(
            self.id,
            realised - modelled <= excess,
            {
                "realised_bps": realised,
                "modelled_bps": modelled,
                "excess_bps": realised - modelled,
                "max_excess_bps": excess,
                "n_fills": record.n_fills,
                "min_fills": minimum,
            },
        )


def _modelled(ctx: GateContext) -> float:
    """The slippage the venue model charged; zero when the run records none."""
    costs = ctx.run.venue_model.get("costs")
    if isinstance(costs, dict):
        value = costs.get("slippage_bps")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return 0.0


gate: Final[Gate] = _FillQualityDrift()
"""What the toolbox entry `fill_quality_drift` resolves to."""
