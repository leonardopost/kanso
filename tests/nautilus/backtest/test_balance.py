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
    sleeve had spent on it. The stop is released by a print, which is then the venue's book:
    500 at its price and the rest one increment worse."""
    hyp = hypothesis(data_requirements=("bar", "trade"), costs=FIXED)
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH, source=EMULATED, hypothesis_=hyp, overrides={"record": str(record)}
    )

    card = execute(request, [instrument()], [tuple(bars(RESEARCH)), tuple(trades(RESEARCH))]).run

    assert [(fill.qty, fill.px, fill.maker) for fill in card.fills] == [
        (500.0, 10.5, False),
        (500.0, 10.51, False),
    ]
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


def test_a_taker_pays_the_per_share_commission_on_top_and_a_maker_under_a_rate_does_not(
    tmp_path: Path, request_for
) -> None:
    """Every share of a taker's fill pays $0.01 on top of the rates; a maker's fill under a
    zero maker rate pays nothing at all; the balance the sleeve read is still the equity."""
    card, record = resting_card(
        tmp_path, request_for, {**FIXED, "maker_bps": 0.0, "commission_per_share": 0.01}
    )

    makers = [fill for fill in card.fills if fill.maker]
    takers = [fill for fill in card.fills if not fill.maker]
    assert makers and takers
    for fill in makers:
        assert fill.cost == 0.0
    for fill in takers:
        assert fill.cost == pytest.approx(fill.qty * fill.px * TAKER + fill.qty * 0.01, rel=1e-12)
    assert_the_same(card, record, at_least=1)


def test_a_sale_pays_the_sell_side_fees_on_top_and_the_balance_is_still_the_equity(
    tmp_path: Path, request_for
) -> None:
    """Two bp of notional and a cent a share on every sale, maker or taker, on top of what the
    fill pays otherwise: a maker's purchase under a zero rate still pays nothing at all."""
    card, record = resting_card(
        tmp_path,
        request_for,
        {**FIXED, "maker_bps": 0.0, "sell_fee_bps": 2.0, "sell_fee_per_share": 0.01},
    )
    for fill in card.fills:
        fee = fill.qty * fill.px * 2.0 / 10_000 + fill.qty * 0.01 if fill.side == "SELL" else 0.0
        base = 0.0 if fill.maker else fill.qty * fill.px * TAKER
        assert fill.cost == pytest.approx(base + fee, rel=1e-12)
    assert any(fill.maker and fill.side == "SELL" for fill in card.fills)
    assert any(not fill.maker and fill.side == "SELL" for fill in card.fills)
    assert_the_same(card, record, at_least=1)


@pytest.mark.parametrize("per_share", [-0.002, 0.0, 0.004], ids=["rebate", "free", "charge"])
def test_a_fill_that_rested_pays_exactly_its_per_share_charge_and_the_balance_is_the_equity(
    tmp_path: Path, request_for, per_share: float
) -> None:
    """The operator's account: $0.0040 a share on a resting fill and nothing else, a taker's
    $0.014 a share on top of the rates, and the sell-side fees on every sale either way."""
    card, record = resting_card(
        tmp_path,
        request_for,
        {
            **FIXED,
            "commission_per_share": 0.014,
            "maker_per_share": per_share,
            "sell_fee_bps": 0.206,
            "sell_fee_per_share": 0.000195,
        },
    )

    makers = [fill for fill in card.fills if fill.maker]
    takers = [fill for fill in card.fills if not fill.maker]
    assert [(fill.side, fill.px) for fill in makers] == [("BUY", 12.9), ("SELL", 10.1)]
    assert [fill.side for fill in takers] == ["BUY", "SELL"]
    for fill in card.fills:
        fee = (
            fill.qty * fill.px * 0.206 / 10_000 + fill.qty * 0.000195
            if fill.side == "SELL"
            else 0.0
        )
        base = fill.qty * per_share if fill.maker else fill.qty * fill.px * TAKER + fill.qty * 0.014
        assert fill.cost == pytest.approx(base + fee, rel=1e-12, abs=1e-12)
    assert_the_same(card, record, at_least=15)


TICKED = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Buys with a limit at the close and sells with one a nickel under it, both taken when
    they land, then the same at market, and writes down the balance it read before acting.\"\"\"

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
            self.submit_entry(name, "BUY", notional=1_000.0, price=round(close, 2))
        elif self.seen == 13:
            self.submit_exit(name, price=round(close - 0.05, 2))
        elif self.seen == 19:
            self.submit_entry(name, "BUY", notional=1_000.0)
        elif self.seen == 25:
            self.submit_exit(name)
"""


def test_a_taker_pays_its_ticks_within_its_limit_and_the_balance_is_still_the_equity(
    tmp_path: Path, request_for
) -> None:
    """One tick of a cent a share on every fill that took liquidity: none on the buy taken at
    its own limit, a cent on the sale limited a nickel under its fill and on both market
    orders. Each fill records the increment and the limit it was charged under."""
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH,
        source=TICKED,
        hypothesis_=hypothesis(costs={**FIXED, "slippage_ticks": 1.0}),
        overrides={"record": str(record)},
    )

    card = execute(request, [instrument()], [tuple(bars(RESEARCH))]).run

    assert [(fill.side, fill.maker) for fill in card.fills] == [
        ("BUY", False),
        ("SELL", False),
        ("BUY", False),
        ("SELL", False),
    ]
    limited_buy, limited_sale, *market = card.fills
    assert limited_buy.limit == limited_buy.px
    assert limited_sale.limit == pytest.approx(limited_sale.px - 0.05)
    assert [fill.limit for fill in market] == [None, None]
    assert {fill.tick for fill in card.fills} == {0.01}
    ticked = (0.0, 0.01, 0.01, 0.01)
    for fill, tick in zip(card.fills, ticked, strict=True):
        assert fill.cost == pytest.approx(
            fill.qty * fill.px * TAKER + fill.qty * tick, rel=1e-12, abs=1e-12
        )
    assert_the_same(card, record, at_least=15)


def test_a_fill_that_rested_never_pays_a_tick(tmp_path: Path, request_for) -> None:
    """Two ticks stated: the resting probe's two makers pay their schedule alone and its two
    market orders two cents a share on top of the rates."""
    card, record = resting_card(
        tmp_path, request_for, {**FIXED, "maker_bps": 0.0, "slippage_ticks": 2.0}
    )

    for fill in card.fills:
        expected = 0.0 if fill.maker else fill.qty * fill.px * TAKER + fill.qty * 0.02
        assert fill.cost == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert [fill.maker for fill in card.fills] == [True, True, False, False]
    assert_the_same(card, record, at_least=15)


FLIPPED = b"""
from pathlib import Path

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Buys 100 at market, sells 3,000 limited at a close (taken in part, which flips the
    position), and the next session moves the rest of the sale a nickel under that close.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.sale = None

    def on_bar(self, bar):
        self.seen += 1
        acting = self.seen in (7, 9, 10)
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{bar.ts_init} {self.balance!r} {int(acting)}\\n")
        name = bar.bar_type.instrument_id
        if self.seen == 7:
            bought = self.order_factory.market(name, OrderSide.BUY, Quantity.from_int(100))
            self.submit_order(bought)
        elif self.seen == 9:
            self.limit = float(bar.close)
            self.sale = self.order_factory.limit(
                name, OrderSide.SELL, Quantity.from_int(3_000), Price(self.limit, 2)
            )
            self.submit_order(self.sale)
        elif self.seen == 10:
            live = self.cache.order(self.sale.client_order_id)
            if live is not None and live.is_open:
                self.modify_order(live, price=Price(round(self.limit - 0.05, 2), 2))
"""


def test_a_flipping_fill_before_a_modify_is_capped_by_the_limit_it_filled_under(
    tmp_path: Path, request_for
) -> None:
    """The sale taken at its own limit closes the long and opens a short in one fill, which the
    engine books on two positions, the opening half under a new event id; both halves are
    capped by the limit the order carried when it filled, so neither pays the tick, though the
    order's last limit, after the modify, sits a nickel under the fill. The rest, filled after
    the modify, records the limit it filled under. The balance is the equity throughout."""
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH,
        source=FLIPPED,
        hypothesis_=hypothesis(
            costs={**FIXED, "slippage_ticks": 1.0, "limit_fill": "through"}, max_leverage=2.0
        ),
        overrides={"record": str(record)},
    )

    card = execute(request, [instrument()], [tuple(bars(RESEARCH))]).run

    bought, closing, opening, rest = card.fills
    assert (bought.side, bought.qty, bought.maker, bought.limit) == ("BUY", 100.0, False, None)
    assert (closing.side, closing.qty, closing.maker) == ("SELL", 100.0, False)
    assert (opening.side, opening.qty, opening.maker) == ("SELL", 2_400.0, False)
    assert closing.limit == opening.limit == opening.px == closing.px
    assert (rest.maker, rest.limit) == (True, pytest.approx(opening.px - 0.05))
    assert bought.cost == pytest.approx(100 * bought.px * TAKER + 100 * 0.01, rel=1e-12)
    for fill in (closing, opening):
        assert fill.cost == pytest.approx(fill.qty * fill.px * TAKER, rel=1e-12)
    assert_the_same(card, record, at_least=10)
