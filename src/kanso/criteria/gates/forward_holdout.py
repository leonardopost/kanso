"""Whether a paper version holds on the forward window, once that window can say anything.

`embargoed_window` asks of the certification window whether the objective stayed above zero
and kept a stated fraction of what research measured. This asks the same of the forward
window — the one the paper stage replays from the hypothesis's `windows.forward.start`, and
which no research or certification run may read (`kanso.nautilus.backtest.stage_of`) — of
what composition certified, and asks it only once the window is long enough to say something.
It is an extra check and never a replacement: a version reaches paper only from a
certificate, every plan carries `paper_forward` beside it — both are required paper gates —
and `embargoed_window` and every other gate are unchanged.

**Judged once `resolve` forward errors span the certified value.** `expected` is
composition's certified value scaled to the folds the window filled, and `se_fwd` the error
of the window's value under the certified dispersion — both exactly as `kanso.criteria.band`
strikes them for `paper_forward`, without the certification value's own term
(`Band.forward`, `forward_error`). The gate judges once the window holds `required_periods`
and `expected ≥ resolve × se_fwd`: a true edge equal to the certified value then stands
`resolve` forward errors clear of zero. Before that it **fails**, with the periods the window
holds, the periods it needs and its forward error in the evidence. It does not skip, because a
skip passes and the version would become `promotable` on `paper_forward` alone. A window too
thin to strike a band — no fold that scores, or a Sharpe's fold under four periods — has not
judged either, and a certified value at or below zero is one no window judges, as is one
certified with an error of zero, which states no dispersion to resolve it against.

**Once judged it passes when `realised > max(0, min_fraction × expected)`.**

**Its power is in the evidence on every pass**: `se_fwd`; what the window resolves,
`resolves = 2.49 × se_fwd`, the true value a one-sided five-percent test detects four times
in five (`kanso.certify.power.RESOLVES`); and its chance of passing if the truth is the
expected value and if it is zero, `pass_at_expected = Φ((expected − bar) / se_fwd)` and
`pass_at_zero = Φ(−bar / se_fwd)`. At `min_fraction` zero a version with no edge passes half
the time once judged, and the evidence says so in numbers.

**`required_periods`** is the fewest periods the gate judges on, and it judges on none fewer:
`N × (resolve × se / value)²` from the certified value, its error and the `N` periods it was
read over — where a mean over `M` periods, erring by `se × √(N / M)`, first stands `resolve`
errors clear — and never fewer than one a fold, a Sharpe's four a fold, since a window holding
fewer is cut into folds shorter than a period, one of which can strike a band alone. A window
filled evenly judges at this count; a Sharpe's fold weighs its exact small-sample variance,
heavier than `1/m` — twice it at five periods — so a Sharpe's window judges about two or three
periods a fold after it; `None` for a certified value at or below zero or an error of zero.

**Only the forward window counts.** A stage resumes at its clock's day, or at the earliest
forward start of what it holds, so a version can join a stage whose clock is before its own
`windows.forward.start`. The run is therefore read from midnight of that day on
(`CardRun.between`), and a window that holds nothing after it holds no period. A period
belongs to the span holding its end, so where the run opened before that midnight a period
that may have opened before it is not counted either: under a return period that divides a
day none can, since every window is cut from a midnight, and under any other — a week — the
first period that ends less than a period after it is dropped, whether or not it straddled.
A per-trade objective counts no trade opened before it, wherever the trade closed, its days
inside the window with it; a returns objective — a contribution, a Sharpe — counts every period
from it on, so what a position opened before it made after it is counted, a period's return
being the book's and not any one trade's. The run is every window the version has closed on
the stage since it first joined, joined into one (`kanso.monitor.realised`): a redeploy adds a
window and restarts nothing, here as for `paper_forward`.

It skips, saying so, where `paper_forward` does: no parameters chosen, no stage record, no
expectation, no objective, or an expectation that records no error or period count.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from math import ceil
from statistics import NormalDist
from typing import ClassVar, Final

from kanso.certify.power import RESOLVES
from kanso.criteria.band import SHARPE_FOLD, dispersion, prediction
from kanso.criteria.context import Gate, GateContext, number, skipped, verdict
from kanso.criteria.objectives import REGISTRY
from kanso.criteria.run import NS_PER_DAY, CardRun, midnight_ns
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
        fewest = (SHARPE_FOLD if objective.statistic == "sharpe" else 1) * ctx.research_folds
        required = _required(value, se, periods, resolve, fewest)
        held = 0 if run is None else len(run.returns)
        judged = (
            required is not None
            and held >= required
            and se_fwd is not None
            and se_fwd > 0
            and expected >= resolve * se_fwd
        )
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
        return verdict(
            self.id,
            judged and realised is not None and realised > bar,
            {
                "objective": objective.id,
                "forward_start": start.isoformat(),
                "window": None if run is None else [day.isoformat() for day in run.window],
                "periods": held,
                "required_periods": required,
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
    """`run` from midnight of `start` on, or `None` when it holds nothing after it: no period
    that may have opened before that midnight, and no trade that did — which only a per-trade
    objective reads; a returns objective reads the periods, whatever was open across them."""
    if run is None:
        return None
    opens, closes = run.bounds
    cut = midnight_ns(start)
    since = opens if cut <= opens else cut
    if since > opens and NS_PER_DAY % run.period_ns:
        since += run.period_ns - 1
    if since >= closes:
        return None
    kept = run.between(since, closes)
    return replace(kept, trades=tuple(trade for trade in kept.trades if trade.opened_ns >= cut))


def _required(value: float, se: float, periods: float, resolve: float, fewest: int) -> int | None:
    """The fewest periods the gate judges on; `None` for a certified value at or below zero,
    or an error of zero, which no window judges."""
    if value <= 0 or se <= 0:
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
