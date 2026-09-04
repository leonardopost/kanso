"""Whether the result sits on a plateau of settings or on the one setting it was fitted to.

A strategy's numbers are read off one point in its parameter space. If moving a parameter
a little destroys the result, the search found that point rather than the effect, and the
number is a property of the fitting rather than of the market. This gate moves each of the
strategy's own numeric parameters, one at a time and in both directions, scores the
certification window again at each setting, and asks that every one of them keep at least
a stated fraction of the unperturbed score.

**Only the strategy's own parameters move.** The configuration base the engine and the
hypothesis contribute — the capital, the risk limits, the venue model, the universe — is
subtracted before anything is perturbed: those are the terms of the experiment, and moving
them tests the framework rather than the idea. What is left is what the author declared,
and a configuration declaring none of it is the case the toolbox says this gate skips on.

**A perturbation that changes nothing is dropped rather than counted.** A whole-number
parameter of three moved by ten percent rounds back to three, and scoring that again would
manufacture a pass out of the very same run; a parameter sitting at zero has no
proportional step at all. Both are recorded as dropped, and no verdict rests on them.

**Every perturbation costs a backtest.** The runs go through the re-run the certification
runner supplies, one after another in a fixed order, so two runs of the same certification
produce the same numbers and the price of the gate is visible in its evidence as a count.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, number, skipped, verdict
from kanso.criteria.objectives import REGISTRY, Objective
from kanso.criteria.run import CardRun
from kanso.schemas import GateResult

PERCENT: Final = 100.0

DIRECTIONS: Final = (("up", 1.0), ("down", -1.0))
"""Both moves of every parameter, in the order the evidence lists them."""

NO_CHOICE: Final = "no perturbation was chosen, so no parameter was moved"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"
NO_RERUN: Final = "no re-run of the subject was supplied, so nothing could be scored again"
NO_PARAMETERS: Final = (
    "the strategy configuration declares no numeric parameter of its own, so nothing moved"
)
ALL_DROPPED: Final = "every perturbation rounded back to the value it started from"


class _ParamPlateau:
    """The objective survives a small move in every parameter the author declared."""

    id: ClassVar[str] = "param_plateau"

    def evaluate(self, ctx: GateContext) -> GateResult:
        percent, fraction = number(ctx, "perturb_pct"), number(ctx, "keep_fraction")
        if percent is None or fraction is None:
            return skipped(self.id, NO_CHOICE)
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.rerun is None:
            return skipped(self.id, NO_RERUN)
        if not ctx.tunable:
            return skipped(self.id, NO_PARAMETERS)
        moves, dropped = _plan(ctx.tunable, percent)
        if not moves:
            return skipped(self.id, ALL_DROPPED)
        unperturbed = objective.compute(ctx.run, ctx.research_folds, ctx.host_run)[0]
        floor = fraction * unperturbed
        scored = _score(ctx, objective, ctx.rerun, moves)
        return verdict(
            self.id,
            all(metric >= floor for _, _, _, metric in scored),
            {
                "objective": objective.id,
                "unperturbed": unperturbed,
                "perturb_pct": percent,
                "keep_fraction": fraction,
                "floor": floor,
                "n_backtests": len(scored),
                "n_fields": len(ctx.tunable),
                "dropped": dropped,
                "perturbations": [
                    {"field": name, "direction": way, "value": value, "metric": metric}
                    for name, way, value, metric in scored
                ],
            },
        )


def _moved(value: float, percent: float, direction: float) -> float | None:
    """One parameter a percentage either way, or `None` when the move changes nothing.

    A whole number stays a whole number: a parameter the author declared as an integer is
    one the strategy counts with, and handing it a fraction would perturb its type rather
    than its value.
    """
    shifted = value + abs(value) * percent / PERCENT * direction
    if isinstance(value, int):
        return None if round(shifted) == value else round(shifted)
    return None if shifted == value else shifted


def _plan(
    tunable: Mapping[str, float], percent: float
) -> tuple[tuple[tuple[str, str, float], ...], list[str]]:
    """Every move worth running, in parameter order, and the ones that changed nothing."""
    moves: list[tuple[str, str, float]] = []
    dropped: list[str] = []
    for name in sorted(tunable):
        for way, direction in DIRECTIONS:
            value = _moved(tunable[name], percent, direction)
            if value is None:
                dropped.append(f"{name} {way}")
            else:
                moves.append((name, way, value))
    return tuple(moves), dropped


def _score(
    ctx: GateContext,
    objective: Objective,
    rerun: Callable[[Mapping[str, float]], CardRun],
    moves: tuple[tuple[str, str, float], ...],
) -> tuple[tuple[str, str, float, float], ...]:
    """Each move run and measured in turn, in the order `_plan` fixed, one at a time."""
    return tuple(
        (
            name,
            way,
            value,
            objective.compute(rerun({name: value}), ctx.research_folds, ctx.host_run)[0],
        )
        for name, way, value in moves
    )


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


gate: Final[Gate] = _ParamPlateau()
"""What the toolbox entry `param_plateau` resolves to."""
