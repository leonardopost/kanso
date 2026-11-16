"""Whether a paper version holds on the forward window, once that window can say anything.

`embargoed_window` asks of the certification window whether the objective stayed above zero
and kept a stated fraction of what research measured. This asks the same of the forward
window — the one the paper stage replays from the hypothesis's `windows.forward.start`, and
which no research or certification run may read (`kanso.nautilus.backtest.stage_of`) — of
what composition certified, and asks it only once the window is long enough to say something.
It is an extra check and never a replacement: a version reaches paper only from a
certificate, and `paper_forward`, `embargoed_window` and every other gate are unchanged.

**Judged once `resolve` forward errors span the certified value.** `expected` is
composition's certified value scaled to the folds the window filled, and `se_fwd` the error
of the window's value under the certified dispersion — both exactly as `kanso.criteria.band`
strikes them for `paper_forward`, without the certification value's own term
(`Band.forward`, `forward_error`). The gate judges once `expected ≥ resolve × se_fwd` with
`expected` above zero: a true edge equal to the certified value then stands `resolve`
forward errors clear of zero. Before that it **fails**, with the periods the window holds,
the periods it needs and its forward error in the evidence. It does not skip, because a
skip passes and the version would become `promotable` on `paper_forward` alone. A window too
thin to strike a band — no fold that scores, or a Sharpe's fold under four periods — has not
judged either, and a certified value at or below zero is one no window judges.

**Once judged it passes when `realised > max(0, min_fraction × expected)`.**

**Its power is in the evidence on every pass**: `se_fwd`; what the window resolves,
`resolves = 2.49 × se_fwd`, the true value a one-sided five-percent test detects four times
in five (`kanso.certify.power.RESOLVES`); and its chance of passing if the truth is the
expected value and if it is zero, `pass_at_expected = Φ((expected − bar) / se_fwd)` and
`pass_at_zero = Φ(−bar / se_fwd)`. At `min_fraction` zero a version with no edge passes half
the time once judged, and the evidence says so in numbers.

**`required_periods`** is the period count at which a window filled evenly first judges:
`N × (resolve × se / value)²` from the certified value, its error and the `N` periods it was
read over, since a mean over `M` such periods errs by `se × √(N / M)` — and at least the
fewest that strike a band, one a fold or a Sharpe's four. A Sharpe's exact small-sample
weight makes the real count a little longer at a few periods a fold; `None` for a certified
value at or below zero.

**Only the forward window counts.** A stage resumes at its clock's day, or at the earliest
forward start of what it holds, so a version can join a stage whose clock is before its own
`windows.forward.start`. The run is therefore read from midnight of that day on
(`CardRun.between`), and a window that holds nothing after it holds no period. Like
`paper_forward` it reads the window since the version's current join, so a redeploy
restarts it.

It skips, saying so, where `paper_forward` does: no parameters chosen, no stage record, no
expectation, no objective, or an expectation that records no error or period count.
"""

from __future__ import annotations

from datetime import date
from math import ceil
from statistics import NormalDist
from typing import ClassVar, Final

from kanso.certify.power import RESOLVES
from kanso.criteria.band import SHARPE_FOLD, dispersion, prediction
from kanso.criteria.context import Gate, GateContext, number, skipped, verdict
from kanso.criteria.objectives import REGISTRY
from kanso.criteria.run import CardRun, midnight_ns
from kanso.monitor.stage import StageRecord
from kanso.schemas import GateResult

NO_PARAMS: Final = "no resolve or min_fraction was chosen, so there is nothing to judge by"
NO_STAGE: Final = "no stage record was supplied, so there is no paper window to read"
NO_EXPECTATION: Final = "the version carries no expectation, so there is no certified value"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"
NO_DISPERSION: Final = (
    "the version's expectation records no error or period count, so no forward error can be "
    "struck; the stage's next deploy measures it again"
)


class _ForwardHoldout:
    """Above zero and a stated fraction of the certified value, once the window resolves it."""

    id: ClassVar[str] = "forward_holdout"

    def evaluate(self, ctx: GateContext) -> GateResult:
        resolve, fraction = number(ctx, "resolve"), number(ctx, "min_fraction")
        if resolve is None or fraction is None:
            return skipped(self.id, NO_PARAMS)
        if not isinstance(ctx.session, StageRecord):
            return skipped(self.id, NO_STAGE)
        if ctx.expectation is None:
            return skipped(self.id, NO_EXPECTATION)
        objective = None if ctx.hyp.objective is None else REGISTRY.get(ctx.hyp.objective.id)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        recorded = dispersion(ctx.expectation)
        if recorded is None:
            return skipped(self.id, NO_DISPERSION)
        value, se, periods = recorded
        start = ctx.hyp.windows.forward.start
        run = _forward(ctx.run, start)
        band = (
            None if run is None else prediction(objective, ctx.expectation, run, ctx.research_folds)
        )
        expected = 0.0 if band is None else band.expected
        se_fwd = None if band is None else band.forward
        judged = se_fwd is not None and expected > 0 and expected >= resolve * se_fwd
        bar = max(0.0, fraction * expected)
        realised = (
            None
            if run is None
            else objective.compute(
                run,
                ctx.research_folds,
                _forward(ctx.host_run, start),
                _forward(ctx.benchmark_run, start),
            )[0]
        )
        fewest = (SHARPE_FOLD if objective.statistic == "sharpe" else 1) * ctx.research_folds
        return verdict(
            self.id,
            judged and realised is not None and realised > bar,
            {
                "objective": objective.id,
                "forward_start": start.isoformat(),
                "window": None if run is None else [day.isoformat() for day in run.window],
                "periods": 0 if run is None else len(run.returns),
                "required_periods": _required(value, se, periods, resolve, fewest),
                "resolve": resolve,
                "min_fraction": fraction,
                "judged": judged,
                "realised": realised,
                "expected": expected,
                "bar": bar,
                "se_fwd": se_fwd,
                **_power(expected, bar, se_fwd),
            },
        )


def _forward(run: CardRun | None, start: date) -> CardRun | None:
    """`run` from midnight of `start` on, or `None` when it holds nothing after it."""
    if run is None:
        return None
    opens, closes = run.bounds
    since = max(opens, midnight_ns(start))
    return None if since >= closes else run.between(since, closes)


def _required(value: float, se: float, periods: float, resolve: float, fewest: int) -> int | None:
    """The periods an evenly filled window needs before it judges; `None` for a certified
    value at or below zero, which no window judges."""
    if value <= 0:
        return None
    return max(ceil(periods * (resolve * se / value) ** 2), fewest)


def _power(expected: float, bar: float, se_fwd: float | None) -> dict[str, float | None]:
    """What the window resolves, and the gate's chance of passing at the expected value and
    at zero; `None` each where the window strikes no forward error, or one of zero."""
    if se_fwd is None or se_fwd <= 0:
        return {"resolves": None, "pass_at_expected": None, "pass_at_zero": None}
    normal = NormalDist()
    return {
        "resolves": RESOLVES * se_fwd,
        "pass_at_expected": normal.cdf((expected - bar) / se_fwd),
        "pass_at_zero": normal.cdf(-bar / se_fwd),
    }


gate: Final[Gate] = _ForwardHoldout()
"""What the toolbox entry `forward_holdout` resolves to."""
