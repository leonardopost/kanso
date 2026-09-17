"""Where the harness cuts a book policy's periods: the runner's periods, and none before them.

What a period close books is measured against the runner on the engine
(`tests/nautilus/backtest/test_book.py`); these pin only which points turn a period, since a
point the runner measures in no period must turn none on the harness either.
"""

from __future__ import annotations

from kanso.nautilus.costs import BookPolicy
from kanso.nautilus.cross_section import book, warm
from kanso.nautilus.strategy import KansoStrategy


def test_a_sleeve_without_a_policy_cuts_no_periods() -> None:
    sleeve = KansoStrategy()

    sleeve._turn(1_000)

    assert sleeve._period_index is None


def test_a_point_before_the_anchor_or_inside_a_restart_s_prefix_turns_nothing() -> None:
    """A stage restart anchors its periods at the midnight of the day its clock stands in and
    re-feeds the sessions at or before the clock: those points are after the anchor and
    before the open, and the runner measures none of them."""
    sleeve = KansoStrategy()
    book(sleeve, BookPolicy(reset="monthly"), anchor_ns=100, period_ns=10, settled_ns=50)
    warm(sleeve, 125)

    sleeve._turn(90)
    sleeve._turn(115)

    assert sleeve._period_index is None

    sleeve._turn(126)
    sleeve._turn(129)

    assert (sleeve._period_index, sleeve._period_last_ns, sleeve._settled_ns) == (2, 129, 50)
