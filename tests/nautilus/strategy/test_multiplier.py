"""Every notional the harness sizes, reserves or reads is quantity times price times the
contract multiplier.

The instrument is a manual `FuturesContract` entry with a multiplier of 50, built through the
workspace's own instrument builder, on a flat series at 100 so one contract is 5,000 of
notional and every number below can be checked by eye.
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

import pytest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.objects import Quantity

from kanso.data.instruments import build, conventions_for
from kanso.nautilus.sizing import UNFUNDED_ORDER, SizingError, full_book_quantity
from kanso.nautilus.strategy import KansoStrategy
from kanso.schemas import InstrumentEntry

from .conftest import DEEP, VENUE, flat
from .test_sleeve import config

FUT = InstrumentId(Symbol("FUT"), VENUE)
MULTIPLIER = 50.0
PRICE = 100.0
CONTRACT = PRICE * MULTIPLIER
"""What one contract is worth at the flat price."""
BUDGET = 30_000.0
CAPITAL = 100_000.0


def future() -> Any:
    """A manual entry for a 50-times future, built as the workspace builds one."""
    entry = InstrumentEntry.model_validate(
        {
            "nautilus_id": FUT.value,
            "asset_class": "INDEX",
            "manual": True,
            "corporate_actions": "none",
            "override": {
                "instrument_class": "future",
                "currency": "USD",
                "price_increment": "0.25",
                "multiplier": str(int(MULTIPLIER)),
                "lot_size": "1",
                "underlying": "FUT",
                "activation_ns": 0,
                "expiration_ns": 1_900_000_000 * 1_000_000_000,
            },
        }
    )
    return build(entry, conventions_for(entry, date(2024, 1, 1)))


def contract_config(**overrides: object):
    fields: dict[str, object] = {
        "universe": (FUT.value,),
        "max_position_pct": 100.0,
        "max_leverage": 1.0,
        "capital": CAPITAL,
    }
    fields.update(overrides)
    return config(**fields)


def series() -> list[object]:
    return list(flat(FUT, close=PRICE, volume=DEEP))


def intents(run) -> list[tuple[str, str, float]]:
    return [(i.instrument_id, i.side, i.qty) for i in run.strategy.intents]


class OnThird(KansoStrategy):
    """Acts on its third bar; subclasses say how, and what to read afterwards."""

    def on_start(self) -> None:
        self.bars = 0
        self.read: list[float] = []

    def on_bar(self, bar_: object) -> None:
        self.bars += 1
        if self.bars == 3:
            self.act()
        if self.bars == 6:
            self.read.append(self._headroom(FUT, PRICE))
            self.read.append(self.balance)

    def act(self) -> None:
        return


def test_a_full_book_entry_fills_the_budget_in_contracts(backtest) -> None:
    """30,000 over one contract's price plus one tick, 50 x 100.25, is five whole contracts —
    not the 299 that the price of a share would buy."""

    class Enters(OnThird):
        def act(self) -> None:
            self.submit_entry(FUT, "BUY")

    run = backtest(
        Enters(contract_config(sizing_budget=BUDGET)), data=series(), instruments=(future(),)
    )

    expected = float(int(full_book_quantity(BUDGET, PRICE * MULTIPLIER, 0.25 * MULTIPLIER, 0.0)))
    assert expected == 5.0
    assert intents(run) == [(FUT.value, "BUY", expected)]


def test_the_room_after_a_fill_is_reduced_by_the_contract_notional(backtest) -> None:
    """Four contracts at 100 take 20,000 of a 100,000 book, and the balance marks them back."""

    class Four(OnThird):
        def act(self) -> None:
            self.submit_entry(FUT, "BUY", qty=4.0)

    run = backtest(Four(contract_config()), data=series(), instruments=(future(),))

    assert intents(run) == [(FUT.value, "BUY", 4.0)]
    room, balance = run.strategy.read
    assert room == pytest.approx(CAPITAL - 4.0 * CONTRACT)
    assert balance == pytest.approx(CAPITAL)


def test_an_unsized_entry_takes_the_room_in_contracts(backtest) -> None:
    """The whole book at leverage one is twenty contracts, and leaves no room."""

    class Whole(OnThird):
        def act(self) -> None:
            self.submit_entry(FUT, "BUY")

    run = backtest(Whole(contract_config()), data=series(), instruments=(future(),))

    assert intents(run) == [(FUT.value, "BUY", CAPITAL / CONTRACT)]
    assert run.strategy.read[0] == pytest.approx(0.0)


class Builds(OnThird):
    """Buys `contracts` by hand, with the engine's own order factory."""

    contracts: ClassVar[int] = 0

    def act(self) -> None:
        order = self.order_factory.market(FUT, OrderSide.BUY, Quantity.from_int(self.contracts))
        self.submit_order(order)


def test_a_hand_built_entry_is_funded_at_its_contract_notional(backtest) -> None:
    """Twenty contracts are the book; twenty-one are 105,000 of exposure on 100,000 and are
    refused as unfunded, where twenty-one shares at 100 would have been 2,100."""

    class Twenty(Builds):
        contracts = 20

    class OneMore(Builds):
        contracts = 21

    run = backtest(Twenty(contract_config()), data=series(), instruments=(future(),))
    assert intents(run) == [(FUT.value, "BUY", 20.0)]

    with pytest.raises(SizingError) as failure:
        backtest(OneMore(contract_config()), data=series(), instruments=(future(),))

    assert failure.value.refusal.rule == UNFUNDED_ORDER
    assert "105,000.00" in failure.value.refusal.why
