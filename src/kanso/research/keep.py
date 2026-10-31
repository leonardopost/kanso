"""The keep rule: when a card's number is an improvement rather than an accident.

Three ideas, and the whole of the loop's defence against fitting noise.

**A metric arrives with its own noise floor.** The objective is computed on each of the
research window's contiguous folds; the metric is their mean and `metric_se` the error of
that mean, read off the window's per-period returns or per-trade edges and widened for
serial correlation (`kanso.criteria.objectives`). A candidate is compared with the run's
best by their difference, whose error is read the same way off the two cards' runs period
by period (`records.Measurement.against`): two cards measured on the same sessions share
most of their day-to-day noise, so a local change has a difference far better known than
either number, and two unrelated strategies one less well known than either. An
improvement counts only when the difference clears `max(min_delta, k_se x se)` on that
error — `min_delta` the operator's smallest interesting difference, `k_se` how many errors
of the difference it must clear. Both parameters come from the hypothesis's own
`objective.params`, chosen at classification, so the bar is set before any result is seen.
Where the best card kept nothing to pair with — one recorded before cards kept their
measurement — or the two do not pair, the error is the candidate's own, which is how the
rule read before it paired, and the comparison says so (`Against`).

**The comparison is strict.** Equal is not better. A loop that kept ties would drift
across a plateau of indistinguishable strategies and call the drift progress.

**Complexity pays double.** A keep that grows `strategy.py` by more than
`[research] max_lines_per_keep` lines must clear twice `k_se`, because added lines are
added parameters however they are spelled, and the cheapest way to move a metric is to
add enough of them. Growth is measured against the file the run is currently climbing
from — its `best`, or its base before the first keep — not against the previous card,
which may have been discarded.

The first keep has nothing to beat: with `best` unset, passing every constraint is the
whole rule, and the complexity clause has no baseline to be a growth over.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from kanso.schemas import ObjectiveParams

__all__ = [
    "COMPLEXITY_FACTOR",
    "Against",
    "grew_by",
    "keep",
    "line_count",
    "noise_floor",
    "threshold",
]

COMPLEXITY_FACTOR: Final = 2.0
"""What `k_se` is multiplied by when a keep grows the file past the line budget."""


@dataclass(frozen=True)
class Against:
    """What a candidate is compared with the run's best on: the best's sha, the candidate's
    metric less the best's, and the error of that difference.

    `unpaired` is `None` when the error was read off both cards' runs period by period, and
    otherwise why it is the candidate's own error instead.
    """

    best: str
    difference: float
    se: float
    unpaired: str | None = None

    def payload(self) -> dict[str, object]:
        """The comparison as the `card` event records it."""
        found: dict[str, object] = {
            "best": self.best[:7],
            "difference": self.difference,
            "se": self.se,
            "paired": self.unpaired is None,
        }
        if self.unpaired is not None:
            found["unpaired"] = self.unpaired
        return found


def line_count(source: bytes) -> int:
    """Lines of a `strategy.py`, counted as an editor would count them."""
    return len(source.decode("utf-8", errors="replace").splitlines())


def grew_by(candidate: bytes, previous: bytes) -> int:
    """How many lines this candidate adds to the file the run is climbing from."""
    return line_count(candidate) - line_count(previous)


def _params(params: Mapping[str, float] | ObjectiveParams) -> tuple[float, float]:
    if isinstance(params, ObjectiveParams):
        return params.min_delta, params.k_se
    return float(params["min_delta"]), float(params["k_se"])


def noise_floor(se: float, params: Mapping[str, float] | ObjectiveParams) -> float:
    """`max(min_delta, k_se x se)`: the smallest difference that is a difference at all.

    With no complexity clause in it. The redundancy rule asks whether a candidate's number
    is the *same* number as one already measured, on the candidate's own error, which is
    two-sided and has nothing to do with how the file was written, so it takes this floor
    undoubled; the keep rule asks whether a candidate is *better* than the best, on the
    error of their difference where the two pair, and doubles the `k_se` term when the file
    grew (`threshold`).
    """
    min_delta, k_se = _params(params)
    return max(min_delta, k_se * se)


def threshold(
    se: float,
    params: Mapping[str, float] | ObjectiveParams,
    grew: int,
    max_lines: int,
) -> float:
    """The margin a candidate must beat `best` by, doubled for an oversized keep."""
    min_delta, k_se = _params(params)
    coefficient = k_se * COMPLEXITY_FACTOR if grew > max_lines else k_se
    return max(min_delta, coefficient * se)


def keep(
    against: Against | None,
    params: Mapping[str, float] | ObjectiveParams,
    grew: int,
    max_lines: int,
) -> bool:
    """Whether this card, whose constraints already passed, becomes the new `best`.

    `against` is the comparison with the run's current best, or `None` before its first
    keep; `grew` is the lines this candidate adds to that best, and `max_lines` the run's
    line budget.
    """
    if against is None:
        return True
    return against.difference > threshold(against.se, params, grew, max_lines)
