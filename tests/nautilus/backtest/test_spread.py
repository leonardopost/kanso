"""The spread is charged where the price does not already carry it.

A fill the venue books from quotes or a level-two book is struck at the touch — a taker buys
the ask and sells the bid — so its price carries the spread and the extraction charges no
half-spread on it; a fill in an instrument fed bars or prints alone is struck at a close or a
last print and pays the model's half-spread. Measured before the rule, on a quote of 9.99/10.01
with every other charge zero: a market buy filled at 10.01 and a market sell at 9.99, each
charged 10 bp on top, so a flat round trip whose cost was one spread was booked at two.
"""

from __future__ import annotations

from datetime import date

import pytest
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria import Fill
from kanso.criteria.gates import repriced
from kanso.criteria.run import CardRun, Trade, midnight_ns
from kanso.nautilus.backtest import RunRequest, execute, touched_names
from kanso.schemas import Hypothesis

from .conftest import (
    CAPITAL,
    RESEARCH,
    SECOND_NS,
    SNAPSHOT,
    SYMBOL,
    VENUE,
    bars,
    hypothesis,
    instrument,
    venue_model,
)

OTHER = "OTHR"

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


def _quotes(symbol: str = SYMBOL, bid: float = 9.99, ask: float = 10.01) -> list[QuoteTick]:
    ident = InstrumentId(Symbol(symbol), Venue(VENUE))
    base = midnight_ns(RESEARCH[0]) + 15 * 3_600 * SECOND_NS
    return [
        QuoteTick(
            ident,
            Price(bid, 2),
            Price(ask, 2),
            Quantity.from_int(1_000),
            Quantity.from_int(1_000),
            ts_event=base + i * 60 * SECOND_NS,
            ts_init=base + i * 60 * SECOND_NS,
        )
        for i in range(12)
    ]


def _quoted_hypothesis(
    limit_fill: str, *, universe: tuple[str, ...] = (f"{SYMBOL}.{VENUE}",), **extra: object
) -> Hypothesis:
    document = hypothesis(universe=universe).model_dump(mode="json")
    document.update(
        resolution="quote",
        horizon="5m",
        data_requirements=["quote", "trade"] if limit_fill != "touch" else ["quote"],
        **extra,
    )
    document["costs"] = {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "spread": "quotes",
        "limit_fill": limit_fill,
    }
    return Hypothesis.model_validate(document)


def _run(hyp: Hypothesis, groups: list[tuple[object, ...]], *instruments: object) -> CardRun:
    request = RunRequest(
        hyp=hyp,
        strategy_source=TAKER,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=True),
        capital=CAPITAL,
    )
    return execute(request, list(instruments) or [instrument()], groups).run


@pytest.mark.parametrize("limit_fill", ["touch", "print_through"])
def test_a_taker_on_a_quoted_name_pays_the_spread_in_its_price_and_not_again(
    limit_fill: str,
) -> None:
    run = _run(_quoted_hypothesis(limit_fill), [tuple(_quotes())])

    assert [(f.side, f.px) for f in run.fills] == [("BUY", 10.01), ("SELL", 9.99)]
    assert all(f.touched for f in run.fills)
    assert all(f.cost == 0.0 for f in run.fills)
    (trade,) = run.trades
    # one spread, paid once, in the price: 100 shares across two cents
    assert trade.pnl_net == pytest.approx(-2.0)
    assert trade.cost == 0.0


def test_a_fill_on_a_name_fed_bars_alone_still_pays_the_fixed_half_spread() -> None:
    hyp = hypothesis(
        costs={
            "commission_bps": 0.0,
            "slippage_bps": 0.0,
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
        assert fill.cost == pytest.approx(fill.notional * 2.0 / 10_000)


def test_touched_names_follow_data_by_instrument() -> None:
    quoted, barred = f"{SYMBOL}.{VENUE}", f"{OTHER}.{VENUE}"
    document = hypothesis(universe=(quoted, barred)).model_dump(mode="json")
    document.update(
        resolution="1d",
        data_requirements=["bar", "quote"],
        data_by_instrument={quoted: ["quote"], barred: ["bar"]},
    )
    document["costs"] = {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "spread": "quotes",
    }
    hyp = Hypothesis.model_validate(document)

    assert touched_names(hyp) == frozenset({quoted})

    book = hypothesis().model_dump(mode="json")
    book.update(resolution="tick", data_requirements=["book", "trade"])
    assert touched_names(Hypothesis.model_validate(book)) == frozenset({f"{SYMBOL}.{VENUE}"})
    assert touched_names(hypothesis()) == frozenset()
    # bars beside quotes on one name: the bar walks the book, so the fill lands at its close
    beside = hypothesis(data_requirements=("bar", "quote")).model_dump(mode="json")
    beside["costs"] = {"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "quotes"}
    assert touched_names(Hypothesis.model_validate(beside)) == frozenset()


def test_a_scenario_charges_fixed_bps_only_where_the_runner_charges_a_spread() -> None:
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
    assert by_name["B.X"] == pytest.approx(1_000.0 * 2.0 / 10_000)
    widened = repriced(run, {"slippage_bps": 2.0})
    assert all(f.cost == pytest.approx(1_000.0 * 2.0 / 10_000) for f in widened.fills)


def test_a_recorded_fill_without_the_field_reads_as_not_touched() -> None:
    from kanso.portfolio.records import _decode_fill, _encode_fill

    fill = Fill(1, "A.X", "SELL", 5.0, 20.0, 0.1, maker=True, touched=True)
    assert _decode_fill(_encode_fill(fill)) == fill
    payload = _encode_fill(fill)
    del payload["touched"]
    assert _decode_fill(payload).touched is False


def test_the_aggressor_side_of_a_print_does_not_move_the_rule() -> None:
    """Sent on a print rather than a quote, a taker still fills at the quote's touch under
    both rules and is charged nothing on top: the measured case this rule closes."""
    from nautilus_trader.model.data import TradeTick

    ident = InstrumentId(Symbol(SYMBOL), Venue(VENUE))
    quotes = _quotes()
    prints = [
        TradeTick(
            ident,
            Price(9.99 if i % 2 == 0 else 10.01, 2),
            Quantity.from_int(200),
            AggressorSide.SELLER if i % 2 == 0 else AggressorSide.BUYER,
            TradeId(f"T{i}"),
            ts_event=int(q.ts_init) + 1_000_000,
            ts_init=int(q.ts_init) + 1_000_000,
        )
        for i, q in enumerate(quotes)
    ]
    for limit_fill in ("touch", "print_through"):
        hyp = _quoted_hypothesis(limit_fill)
        document = hyp.model_dump(mode="json")
        document.update(resolution="trade", data_requirements=["quote", "trade"])
        hyp = Hypothesis.model_validate(document)
        request = RunRequest(
            hyp=hyp,
            strategy_source=ON_PRINTS,
            window=RESEARCH,
            snapshot_id=SNAPSHOT,
            venue_model=venue_model(hyp, quotes_available=True),
            capital=CAPITAL,
        )
        run = execute(request, [instrument()], [tuple(quotes), tuple(prints)]).run
        assert [(f.side, f.px, f.cost) for f in run.fills] == [
            ("BUY", 10.01, 0.0),
            ("SELL", 9.99, 0.0),
        ], limit_fill
