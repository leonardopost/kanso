"""How much of a stage's money a version gets, and what happens when the answer is none.

Two rules, in order. A new version of a strategy the stage already holds **inherits its
predecessor's capital**: it is the same idea with one more construct on it or one more
improvement in it, and making it re-bid for money it already had would shrink a book every
time research improved it. Anything else takes `min(per_strategy_max_pct of the stage,
whatever is unallocated)`, which is the largest share the limits admit.

Zero is not a deployment. A stage whose capital is fully committed cannot fund another
version, and rather than deploying one at nothing — a strategy that trades no size, produces
no evidence and quietly never becomes promotable — the deployment is refused and the
operator is told, which is what `deploy_blocked` is for.

An inherited share is still capped by the current limit. Lowering `per_strategy_max_pct`
below what a deployed version already holds is the operator saying that no strategy may hold
that much, and honouring the old figure would let the file admit what its own limits forbid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kanso.portfolio.files import held, unallocated

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import Limits, Stage

__all__ = ["assign", "ceiling"]


def ceiling(stage: Stage, limits: Limits) -> float:
    """The most of this stage's capital any one strategy may hold."""
    return limits.per_strategy_max_pct / 100.0 * stage.capital


def assign(stage: Stage, limits: Limits, strategy_id: str) -> float:
    """What this strategy gets on this stage: its predecessor's share, or a new one.

    Returns zero when the stage has nothing left to give, which the caller turns into a
    `deploy_blocked` escalation rather than a deployment at no size.
    """
    top = ceiling(stage, limits)
    predecessor = held(stage, strategy_id)
    if predecessor is not None:
        return min(predecessor.capital, top)
    return max(0.0, min(top, unallocated(stage, without=strategy_id)))
