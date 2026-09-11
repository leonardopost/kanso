"""Without a sizing rule an entry is sized to the room the limits leave, unfilled orders applied.

The room is read on the book the sleeve can fund: the smaller of its capital and its balance,
which is cash less what its fills paid and were charged plus its positions at the last print.
"""

from __future__ import annotations

import pytest

from kanso.nautilus.strategy import KansoStrategy

from .conftest import DEEP, DEMO, HEDGE, bar, equity, flat
from .test_sleeve import config

DEMO_SHARES = 10_000.0
"""100,000 of capital at 10.00 under a 100% position ceiling on a free venue, on a book deep
enough to fill it whole at that price."""
HEDGE_SHARES = 5_000.0
"""The same book at 20.00."""


def free(**overrides: object):
    fields: dict[str, object] = {
        "universe": ("DEMO.XNAS", "HEDGE.XNAS"),
        "max_position_pct": 100.0,
        "max_leverage": 1.0,
    }
    fields.update(overrides)
    return config(**fields)


def two_names() -> list[object]:
    return [*flat(DEMO, volume=DEEP), *flat(HEDGE, close=20.0, volume=DEEP)]


class Enters(KansoStrategy):
    """Enters DEMO on the third bar, unsized; subclasses add what happens after."""

    def on_start(self) -> None:
        self.bars = 0
        self.seen: list[object] = []

    def on_bar(self, bar_: object) -> None:
        if str(bar_.bar_type.instrument_id) != "DEMO.XNAS":  # type: ignore[attr-defined]
            return
        self.bars += 1
        if self.bars == 3:
            self.seen.append(self.submit_entry(DEMO, "BUY"))
        self.after(self.bars)

    def after(self, bars: int) -> None:
        return


def intents(run) -> list[tuple[str, str, float]]:
    return [(i.instrument_id, i.side, i.qty) for i in run.strategy.intents]


def test_an_unsized_entry_takes_the_room_the_limits_leave(backtest) -> None:
    run = backtest(Enters(free()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]


def test_an_unsized_flip_is_an_exit_then_an_entry_in_one_handler_at_leverage_one(backtest) -> None:
    """The exit in flight frees the room; read on the venue alone it would not, for a bar."""

    class Flips(Enters):
        def after(self, bars: int) -> None:
            if bars == 6:
                self.submit_exit(DEMO)
                self.seen.append(self.held(DEMO))
                self.seen.append(self.submit_entry(HEDGE, "BUY"))

    run = backtest(Flips(free()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [
        ("DEMO.XNAS", "BUY", DEMO_SHARES),
        ("DEMO.XNAS", "SELL", DEMO_SHARES),
        ("HEDGE.XNAS", "BUY", HEDGE_SHARES),
    ]
    assert run.strategy.seen[1] == 0.0, "the exit in flight nets the old leg to nothing"
    assert run.strategy.seen[2] is not None, "the entry was submitted, not refused for room"
    held = {str(p.instrument_id): float(p.signed_qty) for p in run.engine.cache.positions_open()}
    assert held == {"HEDGE.XNAS": HEDGE_SHARES}
    assert len({i.ts_event for i in run.strategy.intents[1:]}) == 1, "both settle at one instant"


def test_a_second_entry_while_the_first_is_in_flight_finds_no_room(backtest) -> None:
    """The order in flight counts as held, so the book cannot be committed twice."""

    class Twice(Enters):
        def after(self, bars: int) -> None:
            if bars == 3:
                self.seen.append(self.submit_entry(HEDGE, "BUY"))

    run = backtest(Twice(free()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]
    assert run.strategy.seen[1] is None


def test_a_partial_entry_leaves_room_for_the_other_name(backtest) -> None:
    class Halves(Enters):
        def on_bar(self, bar_: object) -> None:
            if str(bar_.bar_type.instrument_id) != "DEMO.XNAS":  # type: ignore[attr-defined]
                return
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", notional=50_000.0)
                self.submit_entry(HEDGE, "BUY", notional=50_000.0)

    run = backtest(Halves(free()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", 5_000.0), ("HEDGE.XNAS", "BUY", 2_500.0)]


# --- the book the room is read on --------------------------------------------

FIXED = {
    "costs": {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "fixed_bps", "fixed_bps": 4.0}
}
"""Three basis points a side and half of a four-point spread: 5.00 on 10,000 of notional."""


def moved(to: float) -> list[object]:
    """DEMO at 10.00 for five bars and at `to` after them, so the sixth bar's exit books a move."""
    return [bar(DEMO, index, 10.0 if index < 5 else to, DEEP) for index in range(20)]


class Again(Enters):
    """Enters on the third bar, exits on the sixth and enters again, unsized, on the eighth."""

    def after(self, bars: int) -> None:
        if bars == 6:
            self.submit_exit(DEMO)
        if bars == 8:
            self.seen.append(self.balance)
            self.seen.append(self.submit_entry(DEMO, "BUY"))


def test_an_entry_after_a_loss_is_cut_to_the_balance_left(backtest) -> None:
    """Read on the capital alone, the second entry bought 20,000 shares with 50,000 left."""
    run = backtest(Again(free(universe=("DEMO.XNAS",))), data=moved(5.0))

    assert intents(run) == [
        ("DEMO.XNAS", "BUY", DEMO_SHARES),
        ("DEMO.XNAS", "SELL", DEMO_SHARES),
        ("DEMO.XNAS", "BUY", 10_000.0),
    ]
    assert run.strategy.seen[1] == 50_000.0


def test_a_gain_does_not_grow_an_entry_past_the_capital(backtest) -> None:
    run = backtest(Again(free(universe=("DEMO.XNAS",))), data=moved(20.0))

    assert intents(run)[-1] == ("DEMO.XNAS", "BUY", 5_000.0)
    assert run.strategy.seen[1] == 200_000.0


def test_the_balance_is_cash_less_costs_plus_the_position_at_its_last_print(backtest) -> None:
    class Holds(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.seen: list[float] = []

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.seen.append(self.balance)
                self.submit_entry(DEMO, "BUY", qty=1_000)
            if self.bars in (5, 7):
                self.seen.append(self.balance)

    run = backtest(Holds(free(universe=("DEMO.XNAS",), venue_model=FIXED)), data=moved(12.0))

    assert run.strategy.seen == [
        100_000.0,
        pytest.approx(100_000.0 - 5.0),
        pytest.approx(100_000.0 - 10_000.0 - 5.0 + 12_000.0),
    ]


def test_a_split_on_the_other_leg_s_first_bar_is_not_read_as_a_loss(backtest) -> None:
    """On a pair's ex-date the venue restates the held leg at the day's first point, which
    can be the other leg's; until the held leg prints again its price is the old one."""
    ex_date = {"splits": [{"ex_date": "1970-01-02", "ratio": 0.1}]}
    next_day = 1_440

    class Holds(KansoStrategy):
        def on_start(self) -> None:
            self.seen: list[tuple[str, float, float]] = []

        def on_bar(self, bar_: object) -> None:
            name = str(bar_.bar_type.instrument_id)  # type: ignore[attr-defined]
            if name == "HEDGE.XNAS" and not self.seen:
                self.submit_entry(HEDGE, "BUY", qty=1_000)
            self.seen.append((name, self.held(HEDGE), self.balance))

    data = [
        bar(HEDGE, 0, 20.0, DEEP),
        bar(DEMO, 1, 10.0, DEEP),
        bar(HEDGE, 2, 20.0, DEEP),
        bar(DEMO, next_day, 10.0, DEEP),
        bar(HEDGE, next_day + 1, 200.0, DEEP),
    ]
    run = backtest(Holds(free()), data=data, instruments=(DEMO, equity(HEDGE, info=ex_date)))

    assert run.strategy.seen[-2:] == [
        ("DEMO.XNAS", 100.0, 100_000.0),
        ("HEDGE.XNAS", 100.0, 100_000.0),
    ]
