"""Whether the result sits on a plateau of settings or on the one setting it was fitted to.

A strategy's numbers are read off one point in its parameter space. If moving a parameter
a little destroys the result, the search found that point rather than the effect, and the
number is a property of the fitting rather than of the market. This gate moves each of the
strategy's own numeric parameters, one at a time and in both directions, scores the window
again at each setting, and asks that every one of them keep at least a stated fraction of
the unperturbed score.

**It comes in two forms, one arithmetic, each on one window.** `param_plateau` scores the
context's own window — the certification window, where certification judges it — so it is
judged after that window is read, and its unperturbed score is the certification one.
`research_plateau` scores the research window alone, its run and the host and benchmark run
beside it, and its re-run moves the subject over that window: it reads nothing of the
certification window, so certification judges it before reading that window, and a stall
whose best it refuses is spared (`kanso.certify.run`). Its unperturbed score is the one the
search selected, which selection inflated, so a sound strategy loses more of it to a move
than of a certification score, and the planner sets `keep_fraction` knowing so. Both forms
share every rule below and the same evidence keys.

**Only the strategy's own parameters move.** The configuration base the engine and the
hypothesis contribute — the capital, the risk limits, the venue model, the universe — is
subtracted before anything is perturbed: those are the terms of the experiment, and moving
them tests the framework rather than the idea. What is left is what the author declared,
and a configuration declaring none of it is the case the toolbox says this gate skips on.

**A perturbation that changes nothing is dropped rather than counted.** A whole-number
parameter of three moved by ten percent rounds back to three, and scoring that again would
manufacture a pass out of the very same run; a parameter sitting at zero has no
proportional step at all. Both are recorded as dropped, and no verdict rests on them.

**The gate is defined around an edge and nowhere else.** The floor is a fraction of the
unperturbed score, and a fraction of a loss lies above the loss: at −10 and a half the floor
is −5, so a move that changed nothing would read as failing to keep half of the result and
one that merely lost less would pass — the test inverts. An unperturbed score at or below
zero therefore fails before any parameter is moved, with the reason in the evidence and a
backtest count of zero. It fails rather than skips because the context is whole and it is
the subject that lacks the property: a plateau around a loss is a robust loss, which is not
what robustness certifies, and a skip is a pass.

**Every perturbation costs a backtest.** The runs go through the re-run the certification
runner supplies, one after another in a fixed order, so two runs of the same certification
produce the same numbers and the price of the gate is visible in its evidence as a count.
The host and the benchmark stay the runs the unperturbed score was measured against: a
perturbation moves the subject, never what it is differenced against.

**A move at which the strategy raises kept nothing.** The subject ran at the setting the
search landed on, so a re-run that raises `MovedRunError` raised because of the move: the
result does not survive it, and the move is recorded with no `metric` and what raised under
`raised`, failing the gate. It is never dropped, which would pass a strategy that raises at
every setting but its own. Certification's re-run of the research window raises it, so
`research_plateau`, which every plan carries, judges such a strategy rather than refusing
every certification of it; its re-run of the certification window refuses the certification
instead, as any run of that window that raises does (`docs/backlog.md` row 342).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, MovedRunError, number, skipped, verdict
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
NO_RESEARCH_RUN: Final = "no research-window run was supplied to score"
NOT_POSITIVE: Final = (
    "the unperturbed objective is not positive, and a plateau around a loss is not evidence "
    "of robustness, so no parameter was moved"
)


_Runs = tuple[CardRun, CardRun | None, CardRun | None]
"""The run a plateau scores, and the host's and the benchmark's run held fixed beside it."""


class _ParamPlateau:
    """The objective survives a small move in every parameter the author declared, over the
    window the context is judged on."""

    id: ClassVar[str] = "param_plateau"

    def runs(self, ctx: GateContext) -> _Runs | None:
        """The context's own window: its run, and its host and benchmark beside it."""
        return ctx.run, ctx.host_run, ctx.benchmark_run

    def evaluate(self, ctx: GateContext) -> GateResult:
        percent, fraction = number(ctx, "perturb_pct"), number(ctx, "keep_fraction")
        if percent is None or fraction is None:
            return skipped(self.id, NO_CHOICE)
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        runs = self.runs(ctx)
        if runs is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        if ctx.rerun is None:
            return skipped(self.id, NO_RERUN)
        if not ctx.tunable:
            return skipped(self.id, NO_PARAMETERS)
        moves, dropped = _plan(ctx.tunable, percent)
        if not moves:
            return skipped(self.id, ALL_DROPPED)
        run, host, benchmark = runs
        unperturbed = objective.compute(run, ctx.research_folds, host, benchmark)[0]
        reading: dict[str, object] = {
            "objective": objective.id,
            "unperturbed": unperturbed,
            "perturb_pct": percent,
            "keep_fraction": fraction,
            "n_fields": len(ctx.tunable),
            "dropped": dropped,
        }
        if unperturbed <= 0:
            return verdict(
                self.id,
                False,
                {**reading, "n_backtests": 0, "perturbations": [], "reason": NOT_POSITIVE},
            )
        floor = fraction * unperturbed
        scored = _score(ctx.research_folds, objective, ctx.rerun, moves, host, benchmark)
        return verdict(
            self.id,
            all(metric is not None and metric >= floor for _, _, _, metric, _ in scored),
            {
                **reading,
                "floor": floor,
                "n_backtests": len(scored),
                "perturbations": [
                    {
                        "field": name,
                        "direction": way,
                        "value": value,
                        "metric": metric,
                        **({} if raised is None else {"raised": raised}),
                    }
                    for name, way, value, metric, raised in scored
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


_Scored = tuple[str, str, float, float | None, str | None]
"""One move: the field, the direction, the value, its score — `None` where the re-run raised —
and what raised."""


def _score(
    folds: int,
    objective: Objective,
    rerun: Callable[[Mapping[str, float]], CardRun],
    moves: tuple[tuple[str, str, float], ...],
    host: CardRun | None,
    benchmark: CardRun | None,
) -> tuple[_Scored, ...]:
    """Each move run and measured in turn, in the order `_plan` fixed, one at a time; a move
    whose re-run raised `MovedRunError` is scored as nothing, with what raised."""
    scored: list[_Scored] = []
    for name, way, value in moves:
        try:
            moved = rerun({name: value})
        except MovedRunError as fault:
            scored.append((name, way, value, None, fault.raised))
            continue
        # The host and the benchmark stay fixed: a perturbation moves the subject alone.
        scored.append((name, way, value, objective.compute(moved, folds, host, benchmark)[0], None))
    return tuple(scored)


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


class _ResearchPlateau(_ParamPlateau):
    """`param_plateau` over the research window: the research run scored, the host's and the
    benchmark's research runs held fixed beside it, and the re-run moving the subject over
    that window — nothing of the certification window is read."""

    id: ClassVar[str] = "research_plateau"

    def runs(self, ctx: GateContext) -> _Runs | None:
        """The research window: its run, and its host and benchmark beside it, or `None`
        where no research run was supplied."""
        if ctx.research_run is None:
            return None
        return ctx.research_run, ctx.host_research_run, ctx.benchmark_research_run


gate: Final[Gate] = _ParamPlateau()
"""What the toolbox entry `param_plateau` resolves to."""

research: Final[Gate] = _ResearchPlateau()
"""What the toolbox entry `research_plateau` resolves to."""
