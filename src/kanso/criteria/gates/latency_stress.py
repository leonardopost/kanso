"""Whether the result survives a slower route to the venue than the one it was measured on.

A venue model states one `latency_ms`, the round trip the strategy is expected to trade
through, and every fill a run has was decided under it: an order lands that long after the
point that sent it, against the book as it then stands. The measured number is therefore a
property of that one latency, and a strategy whose edge lives in the first tens of
milliseconds after a quote keeps it at 20 ms and loses it at 60. Costs can be re-applied to
recorded fills without running anything — a wider commission charges the same fills more —
but a latency changes *which* fills a run has, so the only way to ask the question is to
run the subject again under the slower route. This gate does that at two multiples of the
stated latency over the certification window and asks that each keep at least a stated
fraction of the unstressed objective.

**The strategy is as it was; only the route is slower.** The re-run is the certification
runner's own (`GateContext.rerun_costs`), with `latency_ms` replaced in the venue model and
nothing else: the venue's latency model, the harness's balance and the extraction's costs all
read the replaced model, so a stressed run is measured exactly as the unstressed one was.

**A venue model that states no latency has no route to slow.** Zero times any multiple is
zero, and the same run again would manufacture a pass out of the run being judged; the gate
skips, saying so, and a planner that wants the question asked of such a hypothesis states a
latency on it first. The gate is defined around an edge, as `param_plateau` is: a fraction of
a loss lies above the loss, so an unstressed objective at or below zero fails before any run
is made, with the reason in the evidence.

**Every multiple costs a backtest.** The runs are made in a fixed order, so two runs of the
same certification produce the same numbers, and the count is in the evidence.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, number, skipped, verdict
from kanso.criteria.objectives import REGISTRY, Objective
from kanso.schemas import GateResult

NO_CHOICE: Final = "no latency multiples were chosen, so no slower route was run"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"
NO_RERUN: Final = "no re-run of the subject under another cost model was supplied"
NO_LATENCY: Final = (
    "the venue model states no latency, so there is no route to slow; state costs.latency_ms "
    "on the hypothesis for this gate to have something to stress"
)
NOT_POSITIVE: Final = (
    "the unstressed objective is not positive, and a result that survives a slower route only "
    "by losing less is not evidence of robustness, so no run was made"
)


class _LatencyStress:
    """The objective survives the venue seeing every order later than it was measured at."""

    id: ClassVar[str] = "latency_stress"

    def evaluate(self, ctx: GateContext) -> GateResult:
        first, second, fraction = (
            number(ctx, "mult_a"),
            number(ctx, "mult_b"),
            number(ctx, "keep_fraction"),
        )
        if first is None or second is None or fraction is None:
            return skipped(self.id, NO_CHOICE)
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.rerun_costs is None:
            return skipped(self.id, NO_RERUN)
        stated = _latency_ms(ctx)
        if stated <= 0:
            return skipped(self.id, NO_LATENCY)
        unstressed = objective.compute(
            ctx.run, ctx.research_folds, ctx.host_run, ctx.benchmark_run
        )[0]
        reading: dict[str, object] = {
            "objective": objective.id,
            "latency_ms": stated,
            "unstressed": unstressed,
            "mult_a": first,
            "mult_b": second,
            "keep_fraction": fraction,
        }
        if unstressed <= 0:
            return verdict(
                self.id, False, {**reading, "n_backtests": 0, "runs": [], "reason": NOT_POSITIVE}
            )
        floor = fraction * unstressed
        runs: list[dict[str, float]] = []
        for multiple in sorted({first, second}):
            slowed = stated * multiple
            stressed = ctx.rerun_costs({"latency_ms": slowed})
            metric = objective.compute(
                stressed, ctx.research_folds, ctx.host_run, ctx.benchmark_run
            )[0]
            runs.append({"multiple": multiple, "latency_ms": slowed, "metric": metric})
        return verdict(
            self.id,
            all(run["metric"] >= floor for run in runs),
            {**reading, "floor": floor, "n_backtests": len(runs), "runs": runs},
        )


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


def _latency_ms(ctx: GateContext) -> float:
    """The latency the run was measured under, read off the venue model it recorded."""
    costs = ctx.run.venue_model.get("costs")
    if not isinstance(costs, dict):
        return 0.0
    value = costs.get("latency_ms", 0.0)
    return float(value) if isinstance(value, int | float) else 0.0


gate: Gate = _LatencyStress()
