"""The balance a sleeve sizes against is the equity the runner strikes, at every period end.

The harness keeps it from its own fills while the run is going, with the runner's cost
arithmetic; the runner strikes the equity from the same fills after the run. A sleeve that
reads one number and is measured on another would be sized against money it does not have.
"""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

import pytest

from kanso.nautilus.backtest import execute

from .conftest import RESEARCH, bars, hypothesis, instrument, quotes, trades

PROBE = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Takes the room every six sessions, and writes down the balance it read first.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        step = self.seen % 6
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{bar.ts_init} {self.balance!r} {int(step in (1, 4))}\\n")
        if step == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY")
        elif step == 4:
            self.submit_exit(bar.bar_type.instrument_id)
"""

FIXED = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "fixed_bps", "fixed_bps": 4.0}
QUOTED = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "quotes"}


@pytest.mark.parametrize("quoted", [False, True], ids=["fixed spread", "quoted spread"])
def test_the_balance_read_before_acting_is_the_equity_struck_at_that_period_end(
    tmp_path: Path, request_for, quoted: bool
) -> None:
    """Compared on every session the probe does not trade on: a session it trades on is
    struck after the fills its own orders made, which the balance it read before them
    cannot hold."""
    hyp = hypothesis(
        data_requirements=("bar", "quote") if quoted else ("bar",),
        costs=QUOTED if quoted else FIXED,
    )
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH,
        source=PROBE,
        hypothesis_=hyp,
        quotes_available=quoted,
        overrides={"record": str(record)},
    )
    groups: list[tuple[object, ...]] = [tuple(bars(RESEARCH))]
    if quoted:
        groups.append(tuple(quotes(RESEARCH)))

    card = execute(request, [instrument()], groups).run

    assert card.fills
    assert all(fill.cost > 0 for fill in card.fills)
    assert_the_same(card, record, at_least=15)


def assert_the_same(card, record: Path, *, at_least: int) -> None:
    """Every balance the sleeve read is the equity struck at that session's end, on every
    session no fill landed in after the read: one that did is struck after that fill, which
    a balance read before it cannot hold — its own orders, or a stop a later print triggered."""
    ends = list(card.period_ends_ns)
    filled = {ends[bisect_left(ends, fill.ts_ns)] for fill in card.fills}
    struck = dict(zip(ends, card.equity, strict=True))
    read = [line.split() for line in record.read_text().splitlines()]
    compared = [
        (int(ts), float(balance))
        for ts, balance, acted in read
        if acted == "0" and int(ts) not in filled
    ]
    assert len(compared) >= at_least
    for ts, balance in compared:
        assert balance == pytest.approx(struck[ts], rel=1e-12)


EMULATED = b"""
from pathlib import Path

from nautilus_trader.model.enums import OrderSide, TriggerType
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Places one emulated stop to buy 1,000 above the market, and writes down its balance.\"\"\"

    config_cls = Config

    def on_start(self):
        self.placed = False

    def on_bar(self, bar):
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{bar.ts_init} {self.balance!r} {int(not self.placed)}\\n")
        if not self.placed:
            self.placed = True
            self.submit_order(
                self.order_factory.stop_market(
                    bar.bar_type.instrument_id,
                    OrderSide.BUY,
                    Quantity.from_int(1_000),
                    trigger_price=Price.from_str("10.25"),
                    emulation_trigger=TriggerType.LAST_PRICE,
                )
            )
"""


def test_an_emulated_order_s_fill_is_booked_from_the_order_the_emulator_released(
    tmp_path: Path, request_for
) -> None:
    """The engine releases an emulated order as another object under the same id, and the
    fill lands on that one; read from the object submitted, the balance kept the money the
    sleeve had spent on it."""
    hyp = hypothesis(data_requirements=("bar", "trade"), costs=FIXED)
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH, source=EMULATED, hypothesis_=hyp, overrides={"record": str(record)}
    )

    card = execute(request, [instrument()], [tuple(bars(RESEARCH)), tuple(trades(RESEARCH))]).run

    assert [fill.side for fill in card.fills] == ["BUY"]
    assert_the_same(card, record, at_least=25)


RESTING = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Buys and sells twice on the saw-tooth, first with limits that rest on the book, then
    at market, and writes down the balance it read before acting.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        acting = self.seen in (7, 13, 19, 25)
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{bar.ts_init} {self.balance!r} {int(acting)}\\n")
        name = bar.bar_type.instrument_id
        close = float(bar.close)
        if self.seen == 7:
            self.submit_entry(name, "BUY", notional=1_000.0, price=round(close - 0.1, 2))
        elif self.seen == 13:
            self.submit_exit(name, price=round(close + 0.1, 2))
        elif self.seen == 19:
            self.submit_entry(name, "BUY", notional=1_000.0)
        elif self.seen == 25:
            self.submit_exit(name)
"""
"""The saw-tooth peaks at the seventh session and bottoms at the thirteenth, so the buy
resting a dime under the peak and the sell a dime over the trough each fill on the next
session, as makers, at their own prices; the two market orders fill as takers."""

TAKER = (1.0 + 2.0) / 10_000 + 4.0 / 2.0 / 10_000
"""What `FIXED` charges a fill: one bp of commission, two of slippage, half a four-bp width."""


def resting_card(tmp_path: Path, request_for, costs: dict[str, object]):
    """The resting probe's run under these costs, and the record of what it read."""
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH,
        source=RESTING,
        hypothesis_=hypothesis(costs=costs),
        overrides={"record": str(record)},
    )
    return execute(request, [instrument()], [tuple(bars(RESEARCH))]).run, record


@pytest.mark.parametrize("maker_bps", [-0.3, 0.0, 0.5], ids=["rebate", "free", "charge"])
def test_a_fill_that_rested_pays_the_maker_rate_and_the_balance_is_still_the_equity(
    tmp_path: Path, request_for, maker_bps: float
) -> None:
    card, record = resting_card(tmp_path, request_for, {**FIXED, "maker_bps": maker_bps})

    makers = [fill for fill in card.fills if fill.maker]
    takers = [fill for fill in card.fills if not fill.maker]
    assert [(fill.side, fill.px) for fill in makers] == [("BUY", 12.9), ("SELL", 10.1)]
    assert [fill.side for fill in takers] == ["BUY", "SELL"]
    for fill in makers:
        assert fill.cost == pytest.approx(fill.qty * fill.px * maker_bps / 10_000, abs=1e-12)
    for fill in takers:
        assert fill.cost == pytest.approx(fill.qty * fill.px * TAKER, rel=1e-12)
    assert_the_same(card, record, at_least=15)


def test_without_a_maker_rate_a_fill_that_rested_is_charged_as_every_fill_always_was(
    tmp_path: Path, request_for
) -> None:
    """A hypothesis that states no `maker_bps` moves no number: every fill, the two that
    rested among them, costs exactly the arithmetic every fill was charged before the key."""
    card, record = resting_card(tmp_path, request_for, FIXED)

    assert [fill.maker for fill in card.fills] == [True, True, False, False]
    for fill in card.fills:
        assert fill.cost == fill.qty * fill.px * 1.0 * TAKER
    assert_the_same(card, record, at_least=15)
