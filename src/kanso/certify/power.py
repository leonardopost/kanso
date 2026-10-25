"""What a certification window can resolve, stated from the research window's dispersion.

A certification is a test, and a test has a size and a power before anyone looks at its
result. kanso used to state neither: a plan named its gates, a certificate recorded its
verdict, and nothing said whether the window the hypothesis declared could have told a real
edge from none. Worked out in October 2026 on an operator's record of 153 certificates: a
window of 122 sessions at a session's spread of 120 bp resolved +22 bp/day at a t of two and
gave a true +15 bp/day a 27% chance, and no line of any of them said so.

The statement is made from research's own numbers. The research estimate and its error are
the objective and the error its card carried — the error of the mean read off the
per-period or per-trade series (`kanso.criteria.objectives`) — and the certification
window is expected to carry that error scaled by the root of the ratio of the two windows'
calendar lengths, because periods and trades arrive at research's rate. Three numbers
follow:

* **what the window resolves**: the true value a one-sided five-percent test on the
  certification window detects four times in five, `(z₀.₉₅ + z₀.₈₀) × se`, 2.49 errors —
  a property of the windows and the dispersion, whatever the plan;
* **the probability of passing at the research estimate**, and **at zero**: for the one
  out-of-sample gate every plan includes, `embargoed_window`, whose estimate must exceed
  `bar = max(0, min_fraction × research)`, so `Φ((truth − bar) / se)`. At the research
  estimate it is optimistic by however much selection inflated that estimate; at zero it
  is the gate's false-pass rate on a candidate with no edge at all, before any deflation.

Nothing is stated from an error of zero. A research window whose trades all fall in one
period, or that holds one period, has no dispersion to read, and its zero error is the
absence of information, not certainty: stated, it would say the certification window
resolves an edge of nothing and passes the research estimate for sure.

Nothing here is shown to the planner. A plan is made blind to research's results, and the
power is computed after it is pinned, for the operator, the way the paper-window note is.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from math import sqrt
from statistics import NormalDist
from typing import Final

from kanso.schemas import CertificationPlan, ParamValue, Power

__all__ = ["RESOLVES", "TEST", "power", "window_days", "window_params"]

TEST: Final = 0.05
"""The one-sided size the resolvable edge is stated at."""

POWER: Final = 0.80
"""The probability of detection the resolvable edge is stated at."""

RESOLVES: Final = NormalDist().inv_cdf(1 - TEST) + NormalDist().inv_cdf(POWER)
"""How many standard errors a true value must sit above zero to be detected at that size
and power: 1.645 + 0.842."""

WINDOW_GATE: Final = "embargoed_window"
"""The out-of-sample gate every plan includes, whose pass probability is stated."""


def window_days(window: tuple[date, date]) -> int:
    """A window's calendar length in days, counting both of its end days."""
    return (window[1] - window[0]).days + 1


def power(
    research: float,
    research_se: float,
    research_window: tuple[date, date],
    certification_window: tuple[date, date],
    window_params: Mapping[str, ParamValue] | None,
) -> Power | None:
    """What the certification window resolves, and how likely the window gate is to pass;
    `None` when the research error is zero, which states no dispersion to work from.

    `window_params` are the parameters the plan gave `embargoed_window`; without a numeric
    `min_fraction` the gate judges nothing and no pass probability is stated.
    """
    if research_se <= 0.0:
        return None
    se = research_se * sqrt(window_days(research_window) / window_days(certification_window))
    fraction = None if window_params is None else window_params.get("min_fraction")
    stated: dict[str, float] = {}
    if isinstance(fraction, int | float) and not isinstance(fraction, bool):
        bar = max(0.0, float(fraction) * research)
        stated = {
            "bar": bar,
            "pass_at_research": _passes(research, bar, se),
            "pass_at_zero": _passes(0.0, bar, se),
        }
    return Power(
        research=research,
        research_se=research_se,
        standard_error=se,
        resolves=RESOLVES * se,
        **stated,
    )


def _passes(truth: float, bar: float, se: float) -> float:
    """The probability an estimate of `truth` with error `se` clears the window gate's bar:
    above zero and at least `min_fraction` of the research one."""
    return NormalDist().cdf((truth - bar) / se)


def window_params(plan: CertificationPlan) -> Mapping[str, ParamValue] | None:
    """The parameters a plan gives `embargoed_window`, or `None` when it names no such gate."""
    return next(
        (dict(gate.params) for gate in plan.stage_gates("cert") if gate.id == WINDOW_GATE), None
    )
