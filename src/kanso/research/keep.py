"""The keep rule: when a card's number is an improvement rather than an accident.

Three ideas, and the whole of the loop's defence against fitting noise.

**A metric arrives with its own noise floor.** The objective is computed on each of the
research window's contiguous folds; the metric is their mean and `metric_se` their
standard error. An improvement counts only when it clears `max(min_delta, k_se x se)` —
the operator's smallest interesting difference, and the spread of the folds that produced
the number. Both parameters come from the hypothesis's own `objective.params`, chosen at
classification, so the bar is set before any result is seen.

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
from typing import Final

from kanso.schemas import ObjectiveParams

__all__ = ["COMPLEXITY_FACTOR", "grew_by", "keep", "line_count", "threshold"]

COMPLEXITY_FACTOR: Final = 2.0
"""What `k_se` is multiplied by when a keep grows the file past the line budget."""


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
    metric: float,
    se: float,
    best_metric: float | None,
    params: Mapping[str, float] | ObjectiveParams,
    grew: int,
    max_lines: int,
) -> bool:
    """Whether this card, whose constraints already passed, becomes the new `best`.

    `best_metric` is the run's current best, or `None` before its first keep; `grew` is
    the lines this candidate adds to that best, and `max_lines` the run's line budget.
    """
    if best_metric is None:
        return True
    return metric - best_metric > threshold(se, params, grew, max_lines)
