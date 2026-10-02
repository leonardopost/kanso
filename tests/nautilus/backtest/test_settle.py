"""The balance reads each event of an order once, and books what reading every event did.

A sleeve that keeps one order resting and moves it on every bar gives that order two events
a bar — the pending modify and the update — and the balance is read on every bar. Read whole
each time its count moved, the order cost its history on every read and the square of it on a
card: measured on a crypto workspace, a card that re-priced its resting orders on every
five-second bar ran 2.4 minutes per simulated day. So the balance folds in only what each
order gained since it was last read, and these tests hold it to two things: that it reads
each event once, and that it books exactly what the harness booked when it read every event
every time. For the second, each card is run twice — as the harness books it, and with the
harness's booking replaced by the one kanso 0.13.0 shipped (`WHOLE`) — and every balance the
sleeve read, every fill, every period end and every intent must agree to the last bit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.data import Bar, BarSpecification, BarType, TradeTick
from nautilus_trader.model.enums import AggregationSource, AggressorSide, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus.backtest import RunRequest, RunResult, execute, run
from kanso.nautilus.strategy import KansoStrategy
from kanso.schemas import Hypothesis

from . import test_balance, test_book, test_funding
from .conftest import (
    CAPITAL,
    RESEARCH,
    SECOND_NS,
    SNAPSHOT,
    SYMBOL,
    _venue,
    bars,
    catalog,
    hypothesis,
    instrument,
    perpetual,
    quotes,
    trades,
    venue_model,
)

WHOLE = b'''


from nautilus_trader.model.events import OrderFilled


class Strategy(Strategy):
    """The same sleeve, its balance booked as kanso 0.13.0 booked it: every order the sleeve
    sent was read again whole whenever its count of events moved, and a fill was booked once
    by its event id."""

    def __init__(self, config=None):
        super().__init__(config)
        self._whole = []
        self._whole_booked = set()

    def _start(self):
        self._whole.extend([order, 0] for order in self.cache.orders(strategy_id=self.id))
        super()._start()

    def _track(self, order):
        super()._track(order)
        self._whole.append([order, 0])

    def _settle(self, key=None, until_ns=None):
        waiting = []
        later = []
        for entry in self._whole:
            order = entry[0] = self._current(entry[0])
            if key is not None and order.instrument_id.value != key:
                waiting.append(entry)
                continue
            count = order.event_count
            held_back = False
            if count != entry[1]:
                for event in order.events:
                    if not isinstance(event, OrderFilled) or event.id in self._whole_booked:
                        continue
                    if until_ns is not None and int(event.ts_event) > until_ns:
                        later.append(event)
                        held_back = True
                        continue
                    self._whole_booked.add(event.id)
                    self._cash -= self._paid(event)
                    if self._cfg.books_funding:
                        self._filled.append(
                            (int(event.ts_event), event.instrument_id.value, self._signed(event))
                        )
                if not held_back:
                    entry[1] = count
            if held_back or not order.is_closed:
                waiting.append(entry)
            else:
                self._cancels.pop(order.client_order_id, None)
        self._whole = waiting
        for name, (times, values) in self._quoted.items():
            if key is None or name == key:
                del times[:-1]
                del values[:-1]
        return later
'''
"""`_settle` as kanso 0.13.0 shipped it, over a ledger of its own, appended to a sleeve's
source so the file's `Strategy` is the same sleeve with that booking."""

MOVER = b'''
from pathlib import Path

from nautilus_trader.model.objects import Price

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    """Rests a buy of 1,500 six cents under every close and a sale of what it holds six cents
    over, moves both a cent further out or back in on every bar, and writes down the balance
    and how many orders it still keeps a reading of, on every bar."""

    config_cls = Config

    def on_start(self):
        self.buy = None
        self.sell = None
        self.seen = 0
        self.out = Path(self.kanso_config.record).open("a")

    def on_stop(self):
        self.out.close()

    def on_bar(self, bar):
        self.seen += 1
        self.out.write(f"{bar.ts_init} {self.balance!r} {len(self._ledger)}\\n")
        name = bar.bar_type.instrument_id
        close = float(bar.close)
        tilt = 0.01 * (self.seen % 2)
        bid = Price(round(close - 0.06 - tilt, 2), 2)
        ask = Price(round(close + 0.06 + tilt, 2), 2)
        if self.buy is None or self.buy.is_closed:
            self.buy = self.submit_entry(name, "BUY", notional=15_000.0, price=float(bid))
        elif self.buy.price != bid and not self.buy.is_pending_update:
            self.modify_order(self.buy, price=bid)
        if self.held(name) > 0:
            if self.sell is None or self.sell.is_closed:
                self.sell = self.submit_exit(name, price=float(ask))
            elif self.sell.price != ask and not self.sell.is_pending_update:
                self.modify_order(self.sell, price=ask)
'''

BARS = 3_000
"""Seconds of the mover's session: long enough that its buy lives through all of them."""


def _tooth(index: int, half: int) -> int:
    step = index % (2 * half)
    return step if step < half else 2 * half - step


def session(
    count: int,
    buys: tuple[int, int] = (97, 50),
    sells: tuple[int, int] = (89, 40),
    after: tuple[float, ...] = (0.5,),
) -> tuple[list[Bar], list[TradeTick]]:
    """One-second bars from 14:00 on January 2, never far enough from the last close to reach
    a resting order, and a print of ten at the price the mover's buy rests at every 97th
    second and at its sale's every 89th — `buys` and `sells` as `(every, offset)` — half a
    second after the bar, or at each fraction of a second in `after`: so the buy fills ten at
    a time and lives on."""
    bar_type = BarType(
        InstrumentId(Symbol(SYMBOL), _venue()),
        BarSpecification(1, BarAggregation.SECOND, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    opens = midnight_ns(date(2024, 1, 2)) + 14 * 3_600 * SECOND_NS
    made: list[Bar] = []
    prints: list[TradeTick] = []
    for index in range(count):
        close = round(10.0 + 0.01 * (_tooth(index, 20) + _tooth(index, 7)), 2)
        ts = opens + index * SECOND_NS
        made.append(
            Bar(
                bar_type,
                Price(close, 2),
                Price(close + 0.02, 2),
                Price(close - 0.02, 2),
                Price(close, 2),
                Quantity.from_int(40),
                ts_event=ts,
                ts_init=ts,
            )
        )
        tilt = 0.01 * ((index + 1) % 2)
        for buyer, (every, offset) in ((False, buys), (True, sells)):
            if index % every != offset:
                continue
            price = close + 0.06 + tilt if buyer else close - 0.06 - tilt
            for nth, fraction in enumerate(after):
                at = ts + int(fraction * SECOND_NS)
                prints.append(
                    TradeTick(
                        bar_type.instrument_id,
                        Price(round(price, 2), 2),
                        Quantity.from_int(10),
                        AggressorSide.BUYER if buyer else AggressorSide.SELLER,
                        TradeId(f"T-{index}-{int(buyer)}-{nth}"),
                        ts_event=at,
                        ts_init=at,
                    )
                )
    return made, prints


def assert_the_same(
    booked: RunResult, whole: RunResult, read: list[str], read_whole: list[str]
) -> None:
    """Every balance read, every fill, every period end and every intent, to the last bit."""
    assert read
    assert len(read) == len(read_whole)
    pairs = enumerate(zip(read, read_whole, strict=True))
    differs = next((i for i, (ours, theirs) in pairs if ours != theirs), None)
    assert differs is None, f"read {differs}: {read[differs]} booked, {read_whole[differs]} whole"
    assert repr(booked.run) == repr(whole.run)
    assert booked.intents == whole.intents


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, int, int, int, bool]]:
    """Every read of an order's events the balance makes: the order, how many events it held
    and how many had been read, how many the read handed over, and whether they were the
    ones `handle_event` was handed rather than a copy of the order's own."""
    seen: list[tuple[object, int, int, int, bool]] = []
    gained = KansoStrategy._gained

    def counted(entry: Any, count: int) -> list[Any]:
        unread = entry.unread
        events = gained(entry, count)
        seen.append((entry.order.client_order_id, count, entry.read, len(events), events is unread))
        return events

    monkeypatch.setattr(KansoStrategy, "_gained", staticmethod(counted))
    return seen


@pytest.mark.parametrize("latency_ms", [0.0, 5.0], ids=["no latency", "five milliseconds"])
def test_an_order_moved_on_every_bar_is_read_once_and_booked_as_it_was_read_whole(
    tmp_path: Path, reads: list[tuple[object, int, int, int, bool]], latency_ms: float
) -> None:
    """Three thousand bars of a buy that rests the whole session and is moved on nearly every
    one, filled ten at a time: each event of each order reaches the balance once, a copy of
    an order's own events is taken only on its first read, an order is let go once it is
    closed, and every number is the one reading every event every time gave.

    With a latency stated the venue answers a modify only after the next point's handlers,
    so the mover moves its orders on every other bar and a print can meet one still at an
    earlier price, beyond which it fills whole: there an order lives for hundreds of bars
    rather than the session."""
    costs: dict[str, object] = {**test_balance.FIXED, "maker_bps": 0.5}
    if latency_ms:
        costs["latency_ms"] = latency_ms
    hyp = hypothesis(resolution="1s", data_requirements=("bar", "trade"), costs=costs)
    made, prints = session(BARS)

    def run_with(extra: bytes, name: str) -> tuple[RunResult, Path]:
        record = tmp_path / f"{name}.txt"
        request = RunRequest(
            hyp=hyp,
            strategy_source=MOVER + extra,
            window=RESEARCH,
            snapshot_id=SNAPSHOT,
            venue_model=venue_model(hyp),
            capital=CAPITAL,
            overrides={"record": str(record)},
        )
        return execute(request, [instrument()], [tuple(made), tuple(prints)]), record

    booked, read = run_with(b"", "booked")
    whole, read_whole = run_with(WHOLE, "whole")

    balances = [line.rsplit(" ", 1)[0] for line in read.read_text().splitlines()]
    whole_balances = [line.rsplit(" ", 1)[0] for line in read_whole.read_text().splitlines()]
    assert_the_same(booked, whole, balances, whole_balances)
    held = {order: count for order, count, *_ in reads}
    assert max(held.values()) > (500 if latency_ms else BARS), "an order moved bar after bar"
    if not latency_ms:
        assert sum(fill.side == "BUY" for fill in booked.run.fills) >= 25, "ten at a time"
    assert all(handed == count - before for _, count, before, handed, _ in reads)
    assert sum(handed for *_, handed, _ in reads) == sum(held.values()), "each event once"
    assert [before for _, _, before, _, kept in reads if not kept] == [0] * len(held), (
        "an order's own events copied once, on its first read"
    )
    kept = [int(line.split()[2]) for line in read.read_text().splitlines()]
    assert max(kept) <= 3, "a closed order is let go"


STRADDLER = b"""
from pathlib import Path

from nautilus_trader.model.objects import Price

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Rests a buy of 1,500 six cents under every close and moves it on every bar. On every
    tenth bar, before anything else, it strikes its book as it stood half a second before the
    bar, as a period close or a settlement struck again does, and writes down how many fills
    that left for the next read and the cash it booked.\"\"\"

    config_cls = Config

    def on_start(self):
        self.buy = None
        self.seen = 0
        self.out = Path(self.kanso_config.record).open("a")

    def on_stop(self):
        self.out.close()

    def on_bar(self, bar):
        self.seen += 1
        if self.seen % 10 == 5:
            later = self._settle(until_ns=bar.ts_init - 500_000_000)
            self.out.write(f"{bar.ts_init} {len(later)} {self._cash!r}\\n")
        name = bar.bar_type.instrument_id
        bid = Price(round(float(bar.close) - 0.06 - 0.01 * (self.seen % 2), 2), 2)
        if self.buy is None or self.buy.is_closed:
            self.buy = self.submit_entry(name, "BUY", notional=15_000.0, price=float(bid))
        elif self.buy.price != bid and not self.buy.is_pending_update:
            self.modify_order(self.buy, price=bid)
"""


def test_a_fill_left_for_the_next_read_is_booked_once_beside_the_one_before_it(
    tmp_path: Path, reads: list[tuple[object, int, int, int, bool]]
) -> None:
    """Two prints of ten at the buy between bars, three-tenths and six-tenths of a second
    after one, which the venue matches and the sleeve is not handed — it requires bars alone —
    so nothing reads the balance between them: the book struck half a second after the bar
    books the first and leaves the second, and the harness's next read takes the order up
    from where the first began, skips the fill it booked and books the one it left — once,
    as reading it whole did."""
    hyp = hypothesis(resolution="1s")
    made, prints = session(300, buys=(1, 0), sells=(10**9, -1), after=(0.3, 0.6))
    records = (tmp_path / "booked.txt", tmp_path / "whole.txt")
    results = []
    for extra, record in zip((b"", WHOLE), records, strict=True):
        request = RunRequest(
            hyp=hyp,
            strategy_source=STRADDLER + extra,
            window=RESEARCH,
            snapshot_id=SNAPSHOT,
            venue_model=venue_model(hyp),
            capital=CAPITAL,
            overrides={"record": str(record)},
        )
        results.append(execute(request, [instrument()], [tuple(made), tuple(prints)]))

    read, read_whole = (path.read_text().splitlines() for path in records)
    assert_the_same(*results, read, read_whole)
    left = [line.split()[1] for line in read]
    assert left.count("1") >= 5, "a fill left by each strike until the room is full"
    assert any(handed > 1 and before for _, _, before, handed, _ in reads), "read again"


@pytest.fixture(scope="module")
def daily(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The funding tests' catalog: daily bars of the perpetual closing at 00:00Z, and three
    settlements a day."""
    root = tmp_path_factory.mktemp("daily")
    return catalog(root / "catalog", test_funding.generated("1d"), [perpetual()])


def demo(
    source: bytes,
    hyp: Hypothesis,
    window: tuple[date, date] = RESEARCH,
    **fields: object,
) -> Callable[[bytes, Path, Path], RunResult]:
    """A card of one of `test_balance`'s or `test_book`'s probes over the demo saw-tooth."""
    quoted = "quote" in hyp.data_requirements

    def run_with(extra: bytes, record: Path, _held: Path) -> RunResult:
        groups: list[tuple[object, ...]] = [tuple(bars(window))]
        if quoted:
            groups.insert(0, tuple(quotes(window)))
        if "trade" in hyp.data_requirements:
            groups.append(tuple(trades(window)))
        request = RunRequest(
            hyp=hyp,
            strategy_source=source + extra,
            window=window,
            snapshot_id=SNAPSHOT,
            venue_model=venue_model(hyp, quotes_available=quoted),
            capital=CAPITAL,
            overrides={"record": str(record), **fields},  # type: ignore[dict-item]
        )
        return execute(request, [instrument()], groups)

    return run_with


def funded(source: bytes, **fields: object) -> Callable[[bytes, Path, Path], RunResult]:
    """A card of one of `test_funding`'s sleeves over its daily perpetual."""

    def run_with(extra: bytes, record: Path, held: Path) -> RunResult:
        request = test_funding.perp_request(
            test_funding.perp_hypothesis("1d"), source + extra, record=str(record), **fields
        )
        return run(request, held)

    return run_with


def quoted_book() -> Hypothesis:
    """`test_book`'s levered, monthly-reset book with a quoted spread, as it builds it."""
    fields = test_book.booked(
        {"reset": "monthly", "financing_rate_bps": 500.0}, max_position_pct=200.0, max_leverage=2.0
    ).model_dump(by_alias=True, mode="json")
    fields["data_requirements"] = ["bar", "quote"]
    fields["costs"] = test_balance.QUOTED
    return Hypothesis.model_validate(fields)


CARDS = {
    "every sixth session, fixed spread": demo(
        test_balance.PROBE, hypothesis(costs=test_balance.FIXED)
    ),
    "every sixth session, quoted spread": demo(
        test_balance.PROBE,
        hypothesis(data_requirements=("bar", "quote"), costs=test_balance.QUOTED),
    ),
    "an emulated stop": demo(
        test_balance.EMULATED,
        hypothesis(data_requirements=("bar", "trade"), costs=test_balance.FIXED),
    ),
    "limits that rest, at a maker's rate": demo(
        test_balance.RESTING, hypothesis(costs={**test_balance.FIXED, "maker_bps": 0.5})
    ),
    "a levered book reset monthly": demo(
        test_book.BOOK_PROBE, quoted_book(), test_book.QUARTER, side="BUY"
    ),
    "a fill after a settlement": funded(test_funding.ANSWERING, hour=16, below=0.0005),
    "an amended order at a settlement": funded(
        test_funding.RESTING, enter=1, below=0.05, amend=True
    ),
}


@pytest.mark.parametrize("name", list(CARDS))
def test_a_card_books_what_reading_every_event_every_time_booked(
    name: str, tmp_path: Path, daily: Path, reads: list[tuple[object, int, int, int, bool]]
) -> None:
    """The balance probes of the balance, book and funding tests — fixed and quoted spreads,
    an emulated stop the engine releases as another object, makers, a period close and a
    settlement that each leave a later fill unbooked until the next read — read the same
    balance at every point and strike the same card either way."""
    paths = (tmp_path / "booked.txt", tmp_path / "whole.txt")
    booked = CARDS[name](b"", paths[0], daily)
    whole = CARDS[name](WHOLE, paths[1], daily)

    assert_the_same(booked, whole, *(path.read_text().splitlines() for path in paths))
    assert booked.run.fills
    assert all(handed == count - before for _, count, before, handed, _ in reads)
