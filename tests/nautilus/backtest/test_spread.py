"""The spread is charged where the price does not already carry it, read off each fill.

A fill struck at the touch — a buy at or above the ask of its name's last quote at or before
it, a sale at or below that quote's bid, or any fill on a venue matching a level-two book —
paid the spread in its price, so the extraction charges it neither the half-spread nor
`slippage_bps`; a fill struck at a print or a bar's close inside the quote, or in a name with
no quote before it, pays both. Measured before the first rule, on a quote of 9.99/10.01 with
every other charge zero: a market buy filled at 10.01 and a market sell at 9.99, each charged
10 bp on top, so a flat round trip whose cost was one spread was booked at two. Until 0.20 the
rule was read per name, which charged a name fed bars beside its quotes the half-spread on
the fills its quotes made, a taker that landed on a print inside the quote nothing, and a
phantom maker fill under `touch` nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria import Fill
from kanso.criteria.gates import repriced
from kanso.criteria.run import CardRun, Trade, midnight_ns
from kanso.nautilus.backtest import RunRequest, execute
from kanso.schemas import Hypothesis

from .conftest import (
    CAPITAL,
    RESEARCH,
    SECOND_NS,
    SNAPSHOT,
    SYMBOL,
    VENUE,
    bar_type,
    bars,
    hypothesis,
    instrument,
    venue_model,
)

OTHER = "OTHR"
BPS = 10_000.0
MINUTE_NS = 60 * SECOND_NS
BASE_NS = midnight_ns(RESEARCH[0]) + 15 * 3_600 * SECOND_NS
"""Every point of these tapes is stamped on the first research day, from 15:00 UTC."""

HALF = 0.02 / 10.0 / 2.0
"""Half a 9.99/10.01 quote, as a fraction of its mid."""

TAKER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, q):
        self.seen += 1
        if self.seen == 2:
            self.submit_entry(q.instrument_id, "BUY", qty=100)
        elif self.seen == 6:
            self.submit_exit(q.instrument_id)

    def on_trade_tick(self, t):
        pass

    def on_bar(self, bar):
        pass
"""

ON_PRINTS = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, q):
        pass

    def on_trade_tick(self, t):
        self.seen += 1
        if self.seen == 2:
            self.submit_entry(t.instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            self.submit_exit(t.instrument_id)
"""

ON_QUOTE_OFF_BAR = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Takes the ask on a quote, and gives the position back on a bar.\"\"\"

    config_cls = Config

    def on_start(self):
        self.quotes = 0
        self.bars = 0

    def on_quote_tick(self, q):
        self.quotes += 1
        if self.quotes == 2:
            self.submit_entry(q.instrument_id, "BUY", qty=100)

    def on_bar(self, bar):
        self.bars += 1
        if self.bars == 4:
            self.submit_exit(bar.bar_type.instrument_id)
"""

POSTING = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""
    bid: float = 9.99
    offer: float = 10.01


class Strategy(KansoStrategy):
    \"\"\"Rests a buy at `bid`, then a sale at `offer` once filled, and writes down the balance
    it reads on every quote and on every fill.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.offered = False

    def on_quote_tick(self, q):
        self.seen += 1
        self._write(q.ts_init)
        if self.seen == 2:
            self.submit_entry(q.instrument_id, "BUY", qty=100, price=self.kanso_config.bid)

    def on_trade_tick(self, t):
        pass

    def on_order_filled(self, event):
        self._write(event.ts_event)
        if not self.offered:
            self.offered = True
            self.submit_exit(event.instrument_id, price=self.kanso_config.offer)

    def _write(self, ts):
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{ts} {self.balance!r}\\n")
"""


def _ident(symbol: str = SYMBOL) -> InstrumentId:
    return InstrumentId(Symbol(symbol), Venue(VENUE))


def _quote(i: int, bid: float, ask: float, symbol: str = SYMBOL) -> QuoteTick:
    ts = BASE_NS + i * MINUTE_NS
    return QuoteTick(
        _ident(symbol),
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_int(1_000),
        Quantity.from_int(1_000),
        ts_event=ts,
        ts_init=ts,
    )


def _quotes(n: int = 12, bid: float = 9.99, ask: float = 10.01) -> list[QuoteTick]:
    return [_quote(i, bid, ask) for i in range(n)]


def _print(
    i: int,
    price: float,
    *,
    offset_ns: int = 30 * SECOND_NS,
    aggressor: AggressorSide = AggressorSide.NO_AGGRESSOR,
) -> TradeTick:
    ts = BASE_NS + i * MINUTE_NS + offset_ns
    return TradeTick(
        _ident(),
        Price(price, 2),
        Quantity.from_int(500),
        aggressor,
        TradeId(f"P{i}"),
        ts_event=ts,
        ts_init=ts,
    )


def _minute_bar(i: int, close: float) -> Bar:
    """A one-minute bar half a minute after each quote, at `close` and a cent either side."""
    from nautilus_trader.model.data import BarSpecification, BarType
    from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType

    ts = BASE_NS + i * MINUTE_NS + 30 * SECOND_NS
    kind = BarType(
        _ident(),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    return Bar(
        kind,
        Price(close, 2),
        Price(close + 0.01, 2),
        Price(close - 0.01, 2),
        Price(close, 2),
        Quantity.from_int(10_000),
        ts_event=ts,
        ts_init=ts,
    )


def _hypothesis(
    *,
    limit_fill: str,
    data_requirements: tuple[str, ...] = ("quote", "trade"),
    resolution: str = "tick",
    universe: tuple[str, ...] = (f"{SYMBOL}.{VENUE}",),
    **costs: object,
) -> Hypothesis:
    document = hypothesis(universe=universe).model_dump(mode="json")
    document.update(resolution=resolution, horizon="5m", data_requirements=list(data_requirements))
    document["costs"] = {
        "commission_bps": 0.0,
        "slippage_bps": 1.0,
        "spread": "quotes",
        "limit_fill": limit_fill,
        **costs,
    }
    return Hypothesis.model_validate(document)


def _run(
    hyp: Hypothesis,
    groups: list[tuple[object, ...]],
    source: bytes = TAKER,
    *instruments: object,
    **overrides: object,
) -> CardRun:
    request = RunRequest(
        hyp=hyp,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=True),
        capital=CAPITAL,
        overrides=overrides,
    )
    return execute(request, list(instruments) or [instrument()], groups).run


def _assert_flat_balance_is_the_equity(card: CardRun, record: Path) -> None:
    """The last balance the sleeve read, after its last fill and flat, is the equity the run
    ended on: the harness charged every fill what the extraction charged it."""
    last_fill = max(fill.ts_ns for fill in card.fills)
    reads = [line.split() for line in record.read_text().splitlines()]
    after = [float(balance) for ts, balance in reads if int(ts) > last_fill]
    assert after
    assert after[-1] == pytest.approx(card.equity[-1], rel=1e-12)


@pytest.mark.parametrize("limit_fill", ["touch", "print_through"])
def test_a_quote_made_taker_pays_neither_the_spread_nor_slippage_again(limit_fill: str) -> None:
    """Struck at the ask and the bid, the round trip's cost is one spread, in the prices; the
    slippage the model states in basis points is not charged on top of the touch."""
    run = _run(_hypothesis(limit_fill=limit_fill), [tuple(_quotes())])

    assert [(f.side, f.px) for f in run.fills] == [("BUY", 10.01), ("SELL", 9.99)]
    assert all(f.touched for f in run.fills)
    assert all(f.cost == 0.0 for f in run.fills)
    (trade,) = run.trades
    # one spread, paid once, in the price: 100 shares across two cents
    assert trade.pnl_net == pytest.approx(-2.0)
    assert trade.cost == 0.0


def test_a_bar_priced_fill_pays_the_half_spread_and_the_slippage() -> None:
    hyp = hypothesis(
        costs={
            "commission_bps": 0.0,
            "slippage_bps": 2.0,
            "spread": "fixed_bps",
            "fixed_bps": 4.0,
        }
    )
    request = RunRequest(
        hyp=hyp,
        strategy_source=b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100)
        elif self.seen == 4:
            self.submit_exit(bar.bar_type.instrument_id)
""",
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp),
        capital=CAPITAL,
    )
    run = execute(request, [instrument()], [tuple(bars(RESEARCH))]).run

    assert len(run.fills) == 2
    assert not any(f.touched for f in run.fills)
    for fill in run.fills:
        assert fill.cost == pytest.approx(fill.notional * (2.0 + 2.0) / BPS)


def test_one_name_fed_bars_beside_its_quotes_is_charged_by_the_fill_not_the_name() -> None:
    """The buy its quote made is struck at the ask and charged nothing; the sale its bar made
    is struck at the bar's close, inside the quote, and pays the quoted half-spread and the
    slippage. Read per name, both were charged the half-spread."""
    hyp = _hypothesis(limit_fill="touch", data_requirements=("bar", "quote"), resolution="1m")
    groups: list[tuple[object, ...]] = [
        tuple(_quotes()),
        tuple(_minute_bar(i, 10.00) for i in range(12)),
    ]

    run = _run(hyp, groups, ON_QUOTE_OFF_BAR)

    buy, sale = run.fills
    assert (buy.side, buy.px, buy.touched, buy.cost) == ("BUY", 10.01, True, 0.0)
    assert (sale.side, sale.px, sale.touched) == ("SELL", 10.00, False)
    assert sale.cost == pytest.approx(sale.notional * (HALF + 1.0 / BPS))


def test_a_taker_that_lands_on_a_print_inside_the_quote_pays_the_half_spread() -> None:
    """Under `touch` an order in flight lands on the book a print left, at the print's price:
    here 10.00 inside 9.99/10.01, a cent better than the touch each way. The fill does not
    carry the spread, so it pays the quoted half and the slippage; read per name it paid
    nothing and the round trip read a spread better than any taker earned."""
    hyp = _hypothesis(limit_fill="touch", latency_ms=1_000.0)
    groups: list[tuple[object, ...]] = [
        tuple(_quotes()),
        tuple(_print(i, 10.00) for i in range(12)),
    ]

    run = _run(hyp, groups)

    assert [(f.side, f.px) for f in run.fills] == [("BUY", 10.00), ("SELL", 10.00)]
    assert not any(f.touched for f in run.fills)
    for fill in run.fills:
        assert fill.cost == pytest.approx(fill.notional * (HALF + 1.0 / BPS))


@pytest.mark.parametrize("maker_bps", [None, 0.0], ids=["no schedule", "schedule"])
def test_a_maker_fill_pays_its_schedule_or_is_charged_as_a_fill_at_its_own_price(
    tmp_path: Path, maker_bps: float | None
) -> None:
    """Under `print_through` a buy resting at the bid fills on a print through it, at its own
    price, inside the quote: with no maker schedule it is charged as any fill there — the
    quoted half-spread and the slippage — and with one it pays the schedule alone. The
    balance the harness kept is the equity either way."""
    record = tmp_path / "balance.txt"
    hyp = _hypothesis(
        limit_fill="print_through",
        **({} if maker_bps is None else {"maker_bps": maker_bps}),
    )
    groups: list[tuple[object, ...]] = [
        tuple(_quotes()),
        (_print(3, 9.98), _print(6, 10.02)),
    ]

    run = _run(hyp, groups, POSTING, record=str(record))

    assert [(f.side, f.px, f.maker) for f in run.fills] == [
        ("BUY", 9.99, True),
        ("SELL", 10.01, True),
    ]
    assert not any(f.touched for f in run.fills)
    for fill in run.fills:
        expected = 0.0 if maker_bps is not None else fill.notional * (HALF + 1.0 / BPS)
        assert fill.cost == pytest.approx(expected)
    _assert_flat_balance_is_the_equity(run, record)


def test_a_maker_a_quote_reached_under_touch_is_struck_at_the_touch_and_booked_so(
    tmp_path: Path,
) -> None:
    """Under `touch` a buy resting at 9.99 fills when the ask comes down to it, at 9.99: the
    far touch of the quote that filled it, so its price carries the spread and it is charged
    nothing. The engine hands the sleeve that fill before the quote, and the sleeve reads its
    balance there, against the quote before (ask 10.01); the harness strikes the fill again
    when its quote is delivered, so the balance is still the equity."""
    record = tmp_path / "balance.txt"
    hyp = _hypothesis(limit_fill="touch", data_requirements=("quote",), resolution="quote")
    tape = [
        *(_quote(i, 9.98, 10.01) for i in range(3)),
        _quote(3, 9.97, 9.99),
        *(_quote(i, 9.98, 10.00) for i in range(4, 6)),
        *(_quote(i, 10.01, 10.03) for i in range(6, 12)),
    ]

    run = _run(hyp, [tuple(tape)], POSTING, record=str(record))

    buy = run.fills[0]
    assert (buy.side, buy.px, buy.maker, buy.touched, buy.cost) == ("BUY", 9.99, True, True, 0.0)
    _assert_flat_balance_is_the_equity(run, record)


def test_under_quotes_a_fill_with_no_quote_before_it_pays_the_stated_width() -> None:
    """A name read as bars alone beside a quoted one, under `spread: quotes`, has no quote to
    read a spread from: the `fixed_bps` the model states is its width, and with none stated
    it would pay none, which `hyp validate` refuses."""
    traded = f"{OTHER}.{VENUE}"
    document = hypothesis(universe=(f"{SYMBOL}.{VENUE}", traded)).model_dump(mode="json")
    document.update(
        resolution="1d",
        data_requirements=["bar", "quote"],
        data_by_instrument={f"{SYMBOL}.{VENUE}": ["quote"], traded: ["bar"]},
    )
    document["costs"] = {
        "commission_bps": 0.0,
        "slippage_bps": 1.0,
        "spread": "quotes",
        "fixed_bps": 6.0,
    }
    hyp = Hypothesis.model_validate(document)
    request = RunRequest(
        hyp=hyp,
        strategy_source=b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, q):
        pass

    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100)
        elif self.seen == 4:
            self.submit_exit(bar.bar_type.instrument_id)
""",
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=True),
        capital=CAPITAL,
    )
    from .conftest import quotes as daily_quotes

    run = execute(
        request,
        [instrument(), instrument(OTHER)],
        [tuple(bars(RESEARCH, OTHER)), tuple(daily_quotes(RESEARCH))],
    ).run

    assert len(run.fills) == 2
    assert {f.instrument_id for f in run.fills} == {traded}
    for fill in run.fills:
        assert not fill.touched
        assert fill.cost == pytest.approx(fill.notional * (3.0 + 1.0) / BPS)


def test_a_scenario_charges_fixed_bps_and_slippage_only_where_the_runner_does() -> None:
    touched = Fill(0, "A.X", "BUY", 100.0, 10.0, 0.0, touched=True)
    struck = Fill(0, "B.X", "BUY", 100.0, 10.0, 0.0)
    run = CardRun(
        window=(date(2024, 1, 1), date(2024, 1, 31)),
        period="1d",
        period_ends_ns=(midnight_ns(date(2024, 2, 1)),),
        returns=(0.0,),
        equity=(CAPITAL,),
        trades=(
            Trade(0, 1, "A.X", 100.0, 10.0, 10.0, 0.0, 0.0, (touched,)),
            Trade(0, 1, "B.X", 100.0, 10.0, 10.0, 0.0, 0.0, (struck,)),
        ),
        fills=(touched, struck),
        capital=CAPITAL,
        currency="USD",
        venue_model={},
    )

    under = repriced(run, {"fixed_bps": 4.0})

    by_name = {f.instrument_id: f.cost for f in under.fills}
    assert by_name["A.X"] == 0.0
    assert by_name["B.X"] == pytest.approx(1_000.0 * 2.0 / BPS)
    widened = {f.instrument_id: f.cost for f in repriced(run, {"slippage_bps": 2.0}).fills}
    assert widened["A.X"] == 0.0
    assert widened["B.X"] == pytest.approx(1_000.0 * 2.0 / BPS)
    ticked = repriced(run, {"slippage_ticks": 1.0})
    assert [f.cost for f in ticked.fills] == [0.0, 0.0]  # no increment recorded, nothing to count


def test_a_recorded_fill_without_the_field_reads_as_not_touched() -> None:
    from kanso.criteria.codec import _decode_fill, _encode_fill

    fill = Fill(1, "A.X", "SELL", 5.0, 20.0, 0.1, maker=True, touched=True)
    assert _decode_fill(_encode_fill(fill)) == fill
    payload = _encode_fill(fill)
    del payload["touched"]
    assert _decode_fill(payload).touched is False


def test_the_aggressor_side_of_a_print_does_not_move_the_rule() -> None:
    """Sent on a print rather than a quote, a taker still fills at the quote's touch under
    both rules and is charged nothing on top: the measured case the first rule closed."""
    quotes = _quotes()
    prints = [
        _print(
            i,
            9.99 if i % 2 == 0 else 10.01,
            offset_ns=1_000_000,
            aggressor=AggressorSide.SELLER if i % 2 == 0 else AggressorSide.BUYER,
        )
        for i in range(len(quotes))
    ]
    for limit_fill in ("touch", "print_through"):
        hyp = _hypothesis(limit_fill=limit_fill, resolution="trade")
        run = _run(hyp, [tuple(quotes), tuple(prints)], ON_PRINTS)
        assert [(f.side, f.px, f.cost) for f in run.fills] == [
            ("BUY", 10.01, 0.0),
            ("SELL", 9.99, 0.0),
        ], limit_fill
    assert bar_type  # the daily bar type the bar-only cases above are read under
