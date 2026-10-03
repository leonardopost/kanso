"""A catalog, a hypothesis and a sleeve, all synthetic and all byte-reproducible.

Every test in this package runs the real engine over real parquet, because what is under
test is the runner's binding to both. The series is a deterministic saw-tooth of daily
bars over January 2024, published one second after each session closes, so `ts_init` is
strictly later than `ts_event` and the availability order is the one the engine uses.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import (
    Bar,
    BarSpecification,
    BarType,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from kanso.criteria.run import midnight_ns
from kanso.nautilus.backtest import RunRequest
from kanso.schemas import Hypothesis, resolve_venue_model
from kanso.schemas.venue import CostsOverride

SYMBOL = "DEMO"
VENUE = "XNAS"
INSTRUMENT = f"{SYMBOL}.{VENUE}"
CAPITAL = 100_000.0
SNAPSHOT = "a" * 64

RESEARCH = (date(2024, 1, 1), date(2024, 1, 31))
CERTIFICATION = (date(2024, 2, 6), date(2024, 2, 29))

SECOND_NS = 1_000_000_000
CLOSE_NS = 16 * 3_600 * SECOND_NS
"""Each session's bar is stamped at 16:00 UTC and published a second later."""

SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    every: int = 6


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        step = self.seen % self.kanso_config.every
        if step == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)
        elif step == 4:
            self.submit_exit(bar.bar_type.instrument_id)
"""

FLAT_SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        pass
"""

RAISING_SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        raise RuntimeError("the card asked for the impossible")
"""

REFUSING_SLEEVE = b"""
from kanso.errors import PreconditionError
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        raise PreconditionError("the window is not there", remedy="load it and try again")
"""
"""A card that fails the way kanso itself fails: with a remedy, which is the part of a
refusal a traceback loses."""

CLOSING_SLEEVE = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys once and closes once, so nothing is left open when the window ends."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)
        elif self.seen == 4:
            self.submit_exit(bar.bar_type.instrument_id)


'''

BOTH_SLEEVE = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Opens one position in every instrument of the universe and holds it."""

    config_cls = Config

    def on_start(self):
        self.opened = []

    def on_bar(self, bar):
        key = str(bar.bar_type.instrument_id)
        if key not in self.opened:
            self.opened.append(key)
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)


'''

SLOW_SLEEVE = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Spends far longer than any card budget before it ever sees a bar."""

    config_cls = Config

    def on_start(self):
        total = 0
        for index in range(400_000_000):
            total += index


'''

VANISHING_SLEEVE = b'''
import os

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Leaves the child with nothing to report."""

    config_cls = Config

    def on_start(self):
        os._exit(0)


'''

TELLING_SLEEVE = b'''
import os

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Reports the environment the card was actually given."""

    config_cls = Config

    def on_start(self):
        raise RuntimeError("environment: " + ",".join(sorted(os.environ)))


'''

FILTER_MODIFIER = b"""
from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    allow: bool = False


class Modifier(KansoModifier):
    construct = "filter"
    config_cls = Config

    def evaluate(self, ctx):
        return Decision(allow=self.modifier_config.allow)
"""


def instrument(symbol: str = SYMBOL, **extra: object) -> Equity:
    """One US equity, priced in cents and traded in whole shares.

    `extra` is whatever else the class accepts — `info`, and so the split schedule.
    """
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), _venue()),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        **extra,
    )


PERP = "BTCUSDT-PERP.SIM"
"""The manual linear perpetual the funding tests hold: 0.01 BTC a contract, settled in USDT."""


def perpetual() -> object:
    """The manual BTCUSDT-PERP entry `docs/workspace.md` shows, built as a workspace would."""
    from kanso.data.instruments import build, conventions_for
    from kanso.schemas import InstrumentEntry

    entry = InstrumentEntry.model_validate(
        {
            "nautilus_id": PERP,
            "asset_class": "CRYPTOCURRENCY",
            "manual": True,
            "corporate_actions": "none",
            "override": {
                "instrument_class": "swap",
                "base_currency": "BTC",
                "quote_currency": "USDT",
                "settlement_currency": "USDT",
                "multiplier": "0.01",
                "price_increment": "0.1",
                "size_increment": "1",
                "lot_size": "1",
            },
        }
    )
    return build(entry, conventions_for(entry, RESEARCH[0]))


def _venue() -> object:
    from nautilus_trader.model.identifiers import Venue

    return Venue(VENUE)


def bar_type(symbol: str = SYMBOL) -> BarType:
    """The daily external bar type a `1d` hypothesis subscribes to."""
    return BarType(
        InstrumentId(Symbol(symbol), _venue()),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def second_bar_type(symbol: str = SYMBOL) -> BarType:
    """The 1-second external bar type an overlay grain subscribes to."""
    return BarType(
        InstrumentId(Symbol(symbol), _venue()),
        BarSpecification(1, BarAggregation.SECOND, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def price(index: int) -> float:
    """A saw-tooth between 10.00 and 13.00 that never repeats a direction by accident."""
    return 10.0 + 0.5 * min(index % 12, 12 - index % 12)


def bars(window: tuple[date, date], symbol: str = SYMBOL) -> list[Bar]:
    """One bar per calendar day of the window, published a second after each close."""
    made: list[Bar] = []
    days = (window[1] - window[0]).days + 1
    for index in range(days):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        close = price(index)
        made.append(
            Bar(
                bar_type(symbol),
                Price(close, 2),
                Price(close + 0.25, 2),
                Price(close - 0.25, 2),
                Price(close, 2),
                Quantity.from_int(10_000),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def second_bars(window: tuple[date, date], symbol: str = SYMBOL) -> list[Bar]:
    """Two 1s bars per calendar day, stamped after the open so they do not share
    `ts_init` with the daily close bar."""
    made: list[Bar] = []
    days = (window[1] - window[0]).days + 1
    open_ns = 13 * 3_600 * SECOND_NS
    for index in range(days):
        day = midnight_ns(window[0]) + index * 86_400 * SECOND_NS
        close = price(index)
        for step in (1, 2):
            ts_event = day + open_ns + step * SECOND_NS
            made.append(
                Bar(
                    second_bar_type(symbol),
                    Price(close, 2),
                    Price(close + 0.25, 2),
                    Price(close - 0.25, 2),
                    Price(close, 2),
                    Quantity.from_int(10_000),
                    ts_event=ts_event,
                    ts_init=ts_event + SECOND_NS,
                )
            )
    return made


def quotes(window: tuple[date, date], symbol: str = SYMBOL, first: int = 0) -> list[QuoteTick]:
    """One quote per day from `first` onwards, two cents wide around the day's close."""
    made: list[QuoteTick] = []
    days = (window[1] - window[0]).days + 1
    for index in range(first, days):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        mid = price(index)
        made.append(
            QuoteTick(
                InstrumentId(Symbol(symbol), _venue()),
                Price(mid - 0.01, 2),
                Price(mid + 0.01, 2),
                Quantity.from_int(100),
                Quantity.from_int(100),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def trades(window: tuple[date, date], symbol: str = SYMBOL) -> list[TradeTick]:
    """One print per day at the day's close, published a second later."""
    made: list[TradeTick] = []
    days = (window[1] - window[0]).days + 1
    for index in range(days):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        made.append(
            TradeTick(
                InstrumentId(Symbol(symbol), _venue()),
                Price(price(index), 2),
                Quantity.from_int(500),
                AggressorSide.BUYER,
                TradeId(f"T-{index}"),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def hypothesis(
    *,
    universe: Sequence[str] = (INSTRUMENT,),
    data_requirements: Sequence[str] = ("bar",),
    costs: dict[str, object] | None = None,
    max_leverage: float = 1.0,
    resolution: str = "1d",
) -> Hypothesis:
    """A classified hypothesis over January 2024, certified in late February."""
    return Hypothesis.model_validate(
        {
            "schema": 1,
            "id": "demo_mr",
            "title": "Demo mean reversion",
            "thesis": "A saw-tooth reverts, so a card has something to trade.",
            "mechanism": "mean_reversion",
            "universe": list(universe),
            "horizon": "1d",
            "resolution": resolution,
            "data_requirements": list(data_requirements),
            "costs": costs
            or {
                "commission_bps": 1.0,
                "slippage_bps": 2.0,
                "spread": "fixed_bps",
                "fixed_bps": 4.0,
            },
            "risk_limits": {
                "max_position_pct": 20.0,
                "max_drawdown_pct": 25.0,
                "max_leverage": max_leverage,
            },
            "windows": {
                "research": {"start": RESEARCH[0], "end": RESEARCH[1]},
                "certification": {"start": CERTIFICATION[0], "end": CERTIFICATION[1]},
                "forward": {"start": date(2024, 3, 1)},
            },
            "construct": {"id": "sleeve"},
            "objective": {"id": "sharpe", "params": {"min_delta": 0.0, "k_se": 1.0}},
            "constraints": [{"id": "strategy_integrity"}],
        }
    )


def venue_model(hyp: Hypothesis, *, quotes_available: bool = False) -> dict[str, object]:
    """The resolved model a run records, with the hypothesis's own costs applied."""
    model = resolve_venue_model(
        VENUE,
        broker="synthetic",
        hypothesis_costs=CostsOverride.model_validate(
            hyp.costs.model_dump(exclude_none=True) if hyp.costs else {}
        ),
        max_leverage=hyp.risk_limits.max_leverage,
        quotes_available=quotes_available,
    )
    dumped: dict[str, object] = model.model_dump()
    return dumped


def catalog(root: Path, points: Sequence[object], instruments: Sequence[object]) -> Path:
    """A parquet catalog holding these definitions and these points."""
    root.mkdir(parents=True, exist_ok=True)
    store = ParquetDataCatalog(str(root))
    store.write_data(list(instruments))
    by_class: dict[str, list[object]] = {}
    for point in points:
        by_class.setdefault(type(point).__name__, []).append(point)
    for name in sorted(by_class):
        store.write_data(sorted(by_class[name], key=lambda p: p.ts_init))
    return root


@pytest.fixture
def hyp() -> Hypothesis:
    """The hypothesis every test runs unless it needs another shape."""
    return hypothesis()


@pytest.fixture
def store(tmp_path: Path, hyp: Hypothesis) -> Path:
    """A catalog holding the research and certification windows of daily bars."""
    points = [*bars(RESEARCH), *bars(CERTIFICATION)]
    return catalog(tmp_path / "catalog", points, [instrument()])


@pytest.fixture
def request_for(hyp: Hypothesis):
    """A run request over a window, defaulting to research and the trading sleeve."""

    def make(
        window: tuple[date, date] = RESEARCH,
        *,
        source: bytes = SLEEVE,
        hypothesis_: Hypothesis | None = None,
        quotes_available: bool = False,
        **kwargs: object,
    ) -> RunRequest:
        subject = hypothesis_ or hyp
        return RunRequest(
            hyp=subject,
            strategy_source=source,
            window=window,
            snapshot_id=SNAPSHOT,
            venue_model=venue_model(subject, quotes_available=quotes_available),
            capital=CAPITAL,
            **kwargs,  # type: ignore[arg-type]
        )

    return make


POSTER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Rests 100 at a name's best bid when flat in it and offers what it holds at its best
    ask, re-posted whenever that side's best price moves; it also counts what it is handed."""

    config_cls = Config

    def on_start(self):
        self.posted = {}
        self.books = 0
        self.prints = 0

    def on_order_book_deltas(self, deltas):
        self.books += 1
        self._post(deltas.instrument_id)

    def on_trade_tick(self, tick):
        self.prints += 1
        self._post(tick.instrument_id)

    def _post(self, instrument_id):
        book = self.cache.order_book(instrument_id)
        bid, ask = book.best_bid_price(), book.best_ask_price()
        if bid is None or ask is None:
            return
        held = self.held(instrument_id)
        want = ("BUY", float(bid)) if held <= 0 else ("SELL", float(ask))
        if want == self.posted.get(instrument_id):
            return
        self.cancel_all_orders(instrument_id)
        if want[0] == "BUY":
            sent = self.submit_entry(instrument_id, "BUY", qty=100, price=want[1])
        else:
            sent = self.submit_exit(instrument_id, price=want[1])
        self.posted[instrument_id] = want if sent is not None else None
'''
"""A sleeve that rests at the touch of each name's level-two book, on the book and on every
print."""

TICK_DAYS = (date(2024, 1, 2), date(2024, 1, 3))
"""The two sessions the tick feed below covers, inside the research window."""

TICK_STEP_NS = 7 * 60 * SECOND_NS
TICK_EVENTS = 40
"""An event every seven minutes from 12:00 UTC, forty a session: four hours and more, so a
read of an hour cuts a session into several and a read of a day holds it whole."""


def ticking(day: date, symbol: str = SYMBOL) -> tuple[list[object], list[TradeTick]]:
    """One session of a level-two book and its prints, built to share instants.

    The book opens 50 bid at 10.00 and 50 offered at 10.02 — two changes at one instant —
    and every event after it either resizes both sides (two changes) or moves the whole book
    a cent up or back (four: each side's best deleted and the next added). A millisecond
    after each event come its prints: two sellers' prints of 30 at the bid sharing their
    nanosecond on even events, one buyer's print of 30 at the offer on odd ones, so some
    instants hold two points of a kind and some one.
    """
    from nautilus_trader.model.enums import BookAction, OrderSide

    from kanso.data.loaders.points import make_delta

    ident = InstrumentId(Symbol(symbol), _venue())
    base = midnight_ns(day) + 12 * 3_600 * SECOND_NS
    book: list[object] = [
        make_delta(ident, BookAction.ADD, OrderSide.BUY, 1_000, 50, 0, 2, 0, base, base),
        make_delta(ident, BookAction.ADD, OrderSide.SELL, 1_002, 50, 0, 2, 0, base, base),
    ]
    made: list[TradeTick] = []
    bid, ask = 1_000, 1_002
    for event in range(1, TICK_EVENTS):
        ts = base + event * TICK_STEP_NS
        if event % 3:
            book += [
                make_delta(
                    ident, BookAction.UPDATE, OrderSide.BUY, bid, 50 + event, 0, 2, 0, ts, ts
                ),
                make_delta(
                    ident, BookAction.UPDATE, OrderSide.SELL, ask, 50 + event, 0, 2, 0, ts, ts
                ),
            ]
        else:
            step = 1 if bid == 1_000 else -1
            book += [
                make_delta(ident, BookAction.DELETE, OrderSide.BUY, bid, 0, 0, 2, 0, ts, ts),
                make_delta(ident, BookAction.ADD, OrderSide.BUY, bid + step, 50, 0, 2, 0, ts, ts),
                make_delta(ident, BookAction.DELETE, OrderSide.SELL, ask, 0, 0, 2, 0, ts, ts),
                make_delta(ident, BookAction.ADD, OrderSide.SELL, ask + step, 50, 0, 2, 0, ts, ts),
            ]
            bid, ask = bid + step, ask + step
        printed = ts + 1_000_000
        if event % 2 == 0:
            sides = [(bid, AggressorSide.SELLER)] * 2
        else:
            sides = [(ask, AggressorSide.BUYER)]
        for index, (px, side) in enumerate(sides):
            made.append(
                TradeTick(
                    ident,
                    Price(px / 100, 2),
                    Quantity.from_int(30),
                    side,
                    TradeId(f"{day:%m%d}-{event}-{index}"),
                    ts_event=printed,
                    ts_init=printed,
                )
            )
    return book, made


QUIET = "OTHR"
"""The second name of a two-name book window: `quiet` beside `ticking`."""


def quiet(day: date, symbol: str = QUIET) -> tuple[list[object], list[TradeTick]]:
    """A quiet name's session beside `ticking`'s: its book changes a microsecond after the
    busy name's, at the open and at every fifth event, and it prints two milliseconds after
    every event — a buyer's print of 30 at the offer on odd events, a seller's at the bid on
    even ones.

    Its book opens 50 bid at 20.00 and 50 offered at 20.02 and is only ever resized. Its
    changes always follow the busy name's with no print between, so they never begin a run
    of book changes; its prints, on instants of their own, begin runs of their own between
    flush markers; and a read of an hour cut to a few points holds its prints and none of
    its changes.
    """
    from nautilus_trader.model.enums import BookAction, OrderSide

    from kanso.data.loaders.points import make_delta

    ident = InstrumentId(Symbol(symbol), _venue())
    base = midnight_ns(day) + 12 * 3_600 * SECOND_NS
    late = 1_000

    def both(action: BookAction, size: int, ts: int) -> list[object]:
        return [
            make_delta(ident, action, side, px, size, 0, 2, 0, ts + late, ts + late)
            for side, px in ((OrderSide.BUY, 2_000), (OrderSide.SELL, 2_002))
        ]

    book = both(BookAction.ADD, 50, base)
    made: list[TradeTick] = []
    for event in range(1, TICK_EVENTS):
        ts = base + event * TICK_STEP_NS
        if event % 5 == 0:
            book += both(BookAction.UPDATE, 50 + event, ts)
        px, side = (2_000, AggressorSide.SELLER) if event % 2 == 0 else (2_002, AggressorSide.BUYER)
        made.append(
            TradeTick(
                ident,
                Price(px / 100, 2),
                Quantity.from_int(30),
                side,
                TradeId(f"{symbol}-{day:%m%d}-{event}"),
                ts_event=ts + 2_000_000,
                ts_init=ts + 2_000_000,
            )
        )
    return book, made


def tick_hypothesis(
    latency_ms: float = 20.0, universe: Sequence[str] = (INSTRUMENT,)
) -> Hypothesis:
    """`hypothesis()` over `universe` on a level-two book and its prints, under a latency."""
    document = hypothesis(universe=universe).model_dump(mode="json")
    document.update(resolution="tick", data_requirements=["book", "trade"])
    document["costs"] = {**document["costs"], "latency_ms": latency_ms}
    return Hypothesis.model_validate(document)


def tick_groups(days: Sequence[date] = TICK_DAYS, *, two: bool = False) -> list[tuple[object, ...]]:
    """The book and the prints of `days`, one type and one name per group, books first and
    names in order, as a catalog serves them; with `two`, the quiet name's after the busy
    one's."""
    named = [[ticking(day) for day in days]] + ([[quiet(day) for day in days]] if two else [])
    return [
        *(tuple(point for book, _ in sessions for point in book) for sessions in named),
        *(tuple(point for _, made in sessions for point in made) for sessions in named),
    ]
