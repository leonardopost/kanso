"""Without a sizing rule an entry is sized to the room the limits leave, unfilled orders applied."""

from __future__ import annotations

from kanso.nautilus.strategy import KansoStrategy

from .conftest import DEMO, HEDGE, flat
from .test_sleeve import config

DEMO_SHARES = 10_000.0
"""100,000 of capital at 10.00 under a 100% position ceiling on a free venue."""
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
    return [*flat(DEMO), *flat(HEDGE, close=20.0)]


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
