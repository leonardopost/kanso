"""The one sizing rule a hypothesis may declare, what it refuses, and its arithmetic.

`sizing: {mode: full_book, budget: N}` in `hypothesis.yaml` moves the size of every order
from the strategy to the harness. Under it a sleeve's entry is the whole budget in one
instrument, its exit the whole position, and a sized overlay's clip the whole of its own
budget; nothing a `strategy.py` writes can name a quantity, and the one-position rule is
decided inside the handler that would break it. A proposal that tries is not capped and
not let through: it is refused, by name, with the book that was held at the instant, and
the run stops there because the card can no longer validate and every bar after it is
spend. The refusal crosses the card's process boundary as a value, reaches the card as a
failed `sizing` gate, and so reaches the proposer with its next twenty cards.

One refusal holds without a sizing rule: `unfunded_order`, an entry a strategy built by hand
that would take gross exposure past what the book can fund. kanso cuts the orders it builds to
its room; it does not rebuild one it did not build, so that one is refused the same way.

The quantity is `budget / ((1 + 2 x cost_rate) x (price + increment))`, floored onto the
lot: the round trip the runner will charge and one price increment are reserved inside the
budget, because the simulated venue fills a market order past a quarter of the bar's volume
one increment worse for the remainder (`nautilus/facts.py`), and a fill one increment over
the budget is what a floor on entry fills would otherwise refuse.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

from kanso.errors import ValidationError

SIZE_ARGUMENT: Final = "size_argument"
"""A `notional`, `qty` or `price` was passed where the harness sizes and places at market."""

ONE_POSITION: Final = "one_position"
"""An entry into an instrument while another is held with no exit of it in flight."""

ONE_CLIP: Final = "one_clip"
"""A clip into an instrument while a clip in another is held or in flight."""

CLIP_DIRECTION: Final = "clip_direction"
"""A clip on the other side of one already held; it is taken off with `FLAT` first."""

HEDGE_UNDER_SIZING: Final = "hedge_under_sizing"
"""A sized overlay answered with hedge legs, which carry a quantity of their own."""

CLIP_WITHOUT_BUDGET: Final = "clip_without_budget"
"""An overlay with no budget answered with clips, which have nothing to be sized to."""

SCALE_UNDER_SIZING: Final = "scale_under_sizing"
"""An overlay scaled a sized host's entry, which is the whole budget or nothing."""

HAND_BUILT_ORDER: Final = "hand_built_order"
"""An order reached the venue through anything but the harness's own helpers."""

BUDGET_BELOW_LOT: Final = "budget_below_lot"
"""The budget at this price floors to no whole lot."""

UNFUNDED_ORDER: Final = "unfunded_order"
"""An entry a strategy with no sizing rule built by hand, larger than the book can fund."""

RULES: Final = (
    SIZE_ARGUMENT,
    ONE_POSITION,
    ONE_CLIP,
    CLIP_DIRECTION,
    HEDGE_UNDER_SIZING,
    CLIP_WITHOUT_BUDGET,
    SCALE_UNDER_SIZING,
    HAND_BUILT_ORDER,
    BUDGET_BELOW_LOT,
    UNFUNDED_ORDER,
)

GATE: Final = "sizing"
"""The gate id a refusal is recorded under on the card; no plan names it, the runner does."""

ROUND_TRIP: Final = 2
"""A round trip is two one-way costs."""


@dataclass(frozen=True)
class Refusal:
    """What the harness refused, when, and what was held: the whole of a refused card's evidence."""

    rule: str
    ts_event: int
    instrument_id: str
    asked: str
    held: dict[str, float]
    why: str

    def payload(self) -> dict[str, Any]:
        """The refusal as the card's gate evidence records it."""
        return asdict(self)


class SizingError(ValidationError):
    """An order the harness refused, raised inside the handler that asked for it: one a
    sizing rule forbids, or an entry built by hand that the book cannot fund."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(
            f"sizing: {refusal.rule} at {refusal.instrument_id}: {refusal.why}",
            remedy="the strategy placed an order the harness refuses; the card is "
            "discarded with this refusal as its `sizing` gate",
        )
        self.refusal = refusal


def full_book_quantity(budget: float, price: float, increment: float, cost_rate: float) -> float:
    """The raw quantity of the whole budget at this price, reserves inside the budget.

    `cost_rate` is one-way; a round trip is reserved. `increment` is the instrument's price
    increment, reserved because the venue walks a market order one increment for whatever
    is past a quarter of the bar's volume.
    """
    return budget / ((1.0 + ROUND_TRIP * cost_rate) * (price + increment))
