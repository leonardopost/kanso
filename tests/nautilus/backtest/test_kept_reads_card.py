"""A tick sleeve's per-point reads are answered from what it keeps, and every number a card
makes is the number reading them afresh made.

A tick seed reads its balance and what it holds in every name on every point, and an exit
consult runs on every point it holds a position. So `held` answers a kept read itself, the
balance marks the open positions it kept and makes no settling pass with nothing to fold,
a name no split was applied to is priced without a ratio, and an exit consult nothing is
attached to builds no context. Each card here is run twice: as the harness runs it, and with
those reads made as kanso 0.20.0 made them (`AFRESH`) — and every balance and holding the
sleeve read, every fill, every period end and every intent must agree to the last bit. The
suite's own check (`tests/conftest.py`) reads every kept read again beside it as well.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import hooks
from kanso.nautilus.backtest import RunRequest, RunResult, execute
from kanso.nautilus.strategy import KansoStrategy, _kept_plain
from kanso.schemas import Hypothesis

from .conftest import (
    CAPITAL,
    RESEARCH,
    SECOND_NS,
    SNAPSHOT,
    SYMBOL,
    VENUE,
    _venue,
    hypothesis,
    instrument,
    venue_model,
)

OTHER = "OTHR"
NAMES = (f"{SYMBOL}.{VENUE}", f"{OTHER}.{VENUE}")
OPENS_NS = midnight_ns(RESEARCH[0]) + 15 * 3_600 * SECOND_NS
MS_NS = 1_000_000

SLEEVE = b'''
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""
    every: int = 5
    hold: int = 9


class Strategy(KansoStrategy):
    """On every quote reads its balance and what it holds in every name, as a tick seed's
    flatten walk does, and writes both down, and on every print what it holds alone; every
    `every`-th quote of a name it is flat in enters it, at the touch or at market by turns and
    on alternating sides; `hold` quotes of the name after it first holds it, exits it at market
    or at the near touch by turns."""

    config_cls = Config

    def on_start(self):
        self.seen = {}
        self.since = {}
        self.out = Path(self.kanso_config.record).open("a")

    def on_stop(self):
        self.out.close()

    def on_quote_tick(self, tick):
        self._walk(tick)
        name = tick.instrument_id
        key = name.value
        n = self.seen[key] = self.seen.get(key, 0) + 1
        held = self.held(name)
        if held == 0.0:
            self.since.pop(key, None)
            if n % self.kanso_config.every == 0:
                bid, ask = float(tick.bid_price), float(tick.ask_price)
                side = "BUY" if (n // self.kanso_config.every) % 2 else "SELL"
                price = None if n % 3 == 0 else (bid if side == "BUY" else ask)
                self.submit_entry(name, side, notional=self.capital * 0.4, price=price)
            return
        since = self.since.setdefault(key, n)
        if n - since >= self.kanso_config.hold:
            self.since.pop(key, None)
            if n % 2:
                self.submit_exit(name)
            else:
                near = tick.bid_price if held > 0 else tick.ask_price
                self.submit_exit(name, price=float(near))

    def on_trade_tick(self, tick):
        held = " ".join(self.held(name).hex() for name in self.universe)
        self.out.write(f"{tick.ts_init} {held}\\n")

    def _walk(self, tick):
        held = " ".join(self.held(name).hex() for name in self.universe)
        self.out.write(f"{tick.ts_init} {self.balance.hex()} {held}\\n")
'''

AFRESH = b'''


from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.strategy import _name_of


class Strategy(Strategy):
    """The same sleeve, its reads made as kanso 0.20.0 made them: `held` through
    `_own_intended`, the balance from a settling pass and the cache's list on every read, a
    price divided by its split ratios whether or not there are any, and an exit consult that
    builds its context and asks `before_exit` whatever is attached."""

    def held(self, instrument_id):
        key = _name_of(instrument_id) if type(instrument_id) is InstrumentId else str(instrument_id)
        return self._own_intended(key)

    @property
    def balance(self):
        if self.cache is None:
            return self._cash
        self._settle()
        worth = sum(
            (self._worth(position) for position in self.cache.positions_open(strategy_id=self.id)),
            0.0,
        )
        return self._cash + worth

    def _price_now(self, key):
        price = self._last_price.get(key)
        if price is None:
            return None
        return price / self._restating(key, self._price_ns.get(key, 0))

    def _print_now(self, key):
        price = self._last_print.get(key)
        if price is None:
            return None
        return price / self._restating(key, self._print_ns.get(key, 0))

    def _consult_exit(self, instrument_id, *args):
        if self._exiting or not self.is_running or self._warming():
            return
        net = self._own_filled(instrument_id.value)
        if net == 0.0:
            return
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        if self._market_order_in_flight(instrument_id, side):
            return
        ctx = self._context(instrument_id, side, abs(net), None, "MARKET")
        self._exiting = True
        try:
            if self.before_exit(ctx):
                self.submit_exit(instrument_id)
        finally:
            self._exiting = False
'''
"""The reads as kanso 0.20.0 made them, appended to the sleeve's source so the file's
`Strategy` is the same sleeve reading afresh."""

DISPATCHED = b'''


from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.trading.strategy import Strategy as _Engine

from kanso.criteria.run import day_of
from kanso.nautilus.cross_section import KansoCrossSection


class Strategy(Strategy):
    """The same sleeve, every point handled as kanso 0.20.0 handled it: each helper asked in
    turn, the id and the name read again wherever they are used, the point held alone."""

    def _out_of_scope(self, key, ts_init):
        scope = self._cfg.session_scope
        if scope is None or key in scope[2]:
            return False
        return day_of(int(ts_init)) not in self._scope_days.get(key, ())

    def _turn(self, ts_ns):
        if ts_ns > self._instant_ns:
            self._struck(ts_ns)
        if self._settling and ts_ns > self._settling[0][0]:
            self._refund()
        if self._filled:
            self._filled = [filled for filled in self._filled if filled[0] >= ts_ns]
        if self._policy is None or ts_ns < self._anchor_ns or ts_ns < self._trading_from_ns:
            return
        index = (ts_ns - self._anchor_ns) // self._period_ns
        if self._period_index is not None and index != self._period_index:
            self._close_period(self._policy, self._period_last_ns)
        self._period_index = index
        self._period_last_ns = ts_ns

    def handle_quote_tick(self, tick, historical=False):
        if historical:
            _Engine.handle_quote_tick(self, tick, historical)
            return
        key = tick.instrument_id.value
        if self._undelivered(tick.ts_init) or self._out_of_scope(key, tick.ts_init):
            return
        self._show_book(int(tick.ts_init))
        self._turn(int(tick.ts_init))
        self._delivered_ns = int(tick.ts_init)
        self._last_quote[key] = tick
        bid, ask = float(tick.bid_price), float(tick.ask_price)
        mid = (bid + ask) / 2.0
        self._printed(key, mid, int(tick.ts_event), int(tick.ts_init))
        self._observe_price(key, mid, int(tick.ts_event))
        self._quoted(key, int(tick.ts_init), (bid, ask))
        if self._charges.get("spread") == "quotes":
            self._settle(key)
        if self._held():
            self._pending.append(tick)
            return
        self._old_quote(tick)
        self._old_due()

    def handle_trade_tick(self, tick, historical=False):
        if historical:
            _Engine.handle_trade_tick(self, tick, historical)
            return
        key = tick.instrument_id.value
        if self._undelivered(tick.ts_init) or self._out_of_scope(key, tick.ts_init):
            return
        self._show_book(int(tick.ts_init))
        self._turn(int(tick.ts_init))
        self._delivered_ns = int(tick.ts_init)
        self._last_trade[key] = tick
        self._printed(key, float(tick.price), int(tick.ts_event), int(tick.ts_init))
        self._observe_price(key, float(tick.price), int(tick.ts_event))
        if self._held():
            self._pending.append(tick)
            return
        self._old_trade(tick)
        self._old_due()

    def handle_data(self, data):
        if isinstance(data, KansoCrossSection):
            if self._pending:
                event = self._pending.popleft()
                self._flushing = True
                try:
                    if isinstance(event, Bar):
                        self._dispatch_bar(event)
                    elif isinstance(event, QuoteTick):
                        self._old_quote(event)
                    elif isinstance(event, TradeTick):
                        self._old_trade(event)
                    else:
                        self._dispatch_data(event)
                finally:
                    self._flushing = False
            if not self._pending:
                self._old_due()
            return
        _Engine.handle_data(self, data)

    def _old_due(self):
        if self._hold_until_cross_section and (self._pending or self._flushing):
            return
        due = self._overlay_due
        self._overlay_due = None
        if due is not None:
            instrument_id, last_bar, price = due
            self._consult_overlay(instrument_id, last_bar=last_bar, price=price)

    def _old_quote(self, tick):
        self._data_time = int(tick.ts_event)
        self._send_behind_modify()
        _Engine.handle_quote_tick(self, tick, False)
        self._pay_owed()
        instrument_id = tick.instrument_id
        self._consult_exit(instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (instrument_id, None, None)

    def _old_trade(self, tick):
        self._data_time = int(tick.ts_event)
        self._send_behind_modify()
        _Engine.handle_trade_tick(self, tick, False)
        self._pay_owed()
        instrument_id = tick.instrument_id
        self._consult_exit(instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (instrument_id, None, None)
'''
"""Every point handled as kanso 0.20.0 handled it, appended after `AFRESH`. The tapes here hold
quotes, prints and their flush markers and nothing else."""

NEUTRAL_EXIT = b"""
from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Modifier(KansoModifier):
    construct = "exit"
    config_cls = KansoModifierConfig

    def evaluate(self, ctx):
        return Decision(exit=False)
"""
"""An exit rule that never asks for an exit: attached, every consult builds its context."""


def _id(symbol: str) -> InstrumentId:
    return InstrumentId(Symbol(symbol), _venue())


Step = tuple[int, int, bool, int, int, int, int]
"""(milliseconds since the last point, name, a print, cents the mid moves, the spread in cents,
the bid's size in hundreds, where a print lands against the quote)."""

steps = st.lists(
    st.tuples(
        st.sampled_from((0, 0, 1, 3, 40)),
        st.integers(min_value=0, max_value=1),
        st.booleans(),
        st.integers(min_value=-2, max_value=2),
        st.integers(min_value=1, max_value=4),
        st.integers(min_value=0, max_value=6),
        st.integers(min_value=-2, max_value=2),
    ),
    min_size=60,
    max_size=180,
)


def tape(script: list[Step]) -> list[tuple[object, ...]]:
    """Quotes and prints of the two names in `script`'s order: a mid that moves by whole
    cents, a spread of one to four cents, a bid that shows nothing now and then, and prints
    under the bid, at either side, inside or over the ask, with the aggressor that hit them."""
    mids = [1000, 2500]
    quoted: list[tuple[int, int] | None] = [None, None]
    ts = OPENS_NS
    quotes: list[list[object]] = [[], []]
    prints: list[list[object]] = [[], []]
    for index, (gap, name, printed, move, spread, size, where) in enumerate(script):
        ts += gap * MS_NS
        symbol = (SYMBOL, OTHER)[name]
        held = quoted[name]
        if printed and held is not None:
            bid, ask = held
            cents = {-2: bid - 1, -1: bid, 0: (bid + ask) // 2, 1: ask, 2: ask + 1}[where]
            hit = AggressorSide.SELLER if where < 0 else AggressorSide.BUYER
            prints[name].append(
                TradeTick(
                    _id(symbol),
                    Price(cents / 100, 2),
                    Quantity.from_int(100 * (1 + size)),
                    AggressorSide.NO_AGGRESSOR if where == 0 else hit,
                    TradeId(f"P{index}"),
                    ts_event=ts,
                    ts_init=ts,
                )
            )
            continue
        mids[name] += move
        bid = mids[name] - spread // 2
        ask = bid + spread
        quoted[name] = (bid, ask)
        quotes[name].append(
            QuoteTick(
                _id(symbol),
                Price(bid / 100, 2),
                Price(ask / 100, 2),
                Quantity.from_int(100 * size),
                Quantity.from_int(300),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return [tuple(group) for group in (*quotes, *prints) if group]


def tick_hypothesis(spread: str, limit_fill: str, latency_ms: float) -> Hypothesis:
    """Both names' quotes and prints, under the given cost model."""
    document = hypothesis(universe=NAMES, max_leverage=2.0).model_dump(mode="json")
    document.update(resolution="tick", horizon="5m", data_requirements=["quote", "trade"])
    document["risk_limits"]["max_position_pct"] = 100.0
    document["costs"] = {
        "commission_bps": 0.5,
        "commission_per_share": 0.004,
        "slippage_bps": 1.0,
        "slippage_ticks": 1,
        "spread": spread,
        "fixed_bps": 2.0,
        "sell_fee_bps": 0.206,
        "limit_fill": limit_fill,
        "latency_ms": latency_ms,
    }
    return Hypothesis.model_validate(document)


def card(
    hyp: Hypothesis, groups: list[tuple[object, ...]], source: bytes, record: Path, *, ruled: bool
) -> tuple[RunResult, list[str]]:
    """One card of `source` over `groups`, with an exit rule that never exits attached when
    `ruled`; its result, and every line the sleeve wrote down."""
    request = RunRequest(
        hyp=hyp,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=True),
        capital=CAPITAL,
        overrides={"record": str(record)},
        modifiers=(("exit", NEUTRAL_EXIT, {}),) if ruled else (),
    )
    result = execute(request, [instrument(SYMBOL), instrument(OTHER)], groups)
    return result, record.read_text().splitlines()


def assert_the_same(kept: tuple[RunResult, list[str]], afresh: tuple[RunResult, list[str]]) -> None:
    """Every balance and holding read, every fill, period end and intent, to the last bit."""
    (ran, read), (ran_afresh, read_afresh) = kept, afresh
    assert read
    assert len(read) == len(read_afresh)
    differs = next(
        (i for i, pair in enumerate(zip(read, read_afresh, strict=True)) if pair[0] != pair[1]),
        None,
    )
    assert differs is None, f"read {differs}: {read[differs]} kept, {read_afresh[differs]} afresh"
    assert pickle.dumps(ran.run) == pickle.dumps(ran_afresh.run)
    assert ran.intents == ran_afresh.intents


@settings(max_examples=40, deadline=None)
@given(
    script=steps,
    costs=st.sampled_from(
        (("fixed_bps", "touch", 0.0), ("quotes", "touch", 20.0), ("quotes", "print_through", 20.0))
    ),
    rechecked=st.booleans(),
)
def test_a_tick_card_reads_what_reading_afresh_read(
    tmp_path_factory: pytest.TempPathFactory,
    script: list[Step],
    costs: tuple[str, str, float],
    rechecked: bool,
) -> None:
    """Over random tapes of two names, under a quoted and a fixed spread, the touch and the
    print rule, with and without a latency: the sleeve that keeps its reads and the one that
    reads afresh and handles every point as 0.20.0 did read the same balances and holdings on
    every point and make the same card — with the kept reads answered as kept, as in a card,
    and read again beside them."""
    hyp = tick_hypothesis(*costs)
    groups = tape(script)
    root = tmp_path_factory.mktemp("kept")
    was = KansoStrategy._recheck_kept
    KansoStrategy._recheck_kept = rechecked
    try:
        kept = card(hyp, groups, SLEEVE, root / "kept.txt", ruled=False)
    finally:
        KansoStrategy._recheck_kept = was
    afresh = card(hyp, groups, SLEEVE + AFRESH + DISPATCHED, root / "afresh.txt", ruled=False)
    assert_the_same(kept, afresh)


def holding_script() -> list[Step]:
    """A tape that has the sleeve in a position for most of its points: quotes of both names
    a millisecond apart, a print through each side now and then."""
    made: list[Step] = []
    for index in range(240):
        made.append((1, index % 2, index % 7 == 3, (index // 11) % 3 - 1, 2, 3, (index % 5) - 2))
    return made


def test_an_exit_consult_nothing_is_attached_to_asks_no_exit_rule_and_books_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing attached an exit consult builds no context and asks nothing: the card is
    the card a consult that built its context and asked an exit rule that never exits made,
    every balance included, which settles at the consult as the context's balance did."""
    monkeypatch.setattr(hooks, "_REGISTRY", {})  # nothing a test before this one left attached
    asked: list[object] = []
    before_exit = KansoStrategy.before_exit

    def counted(self: KansoStrategy, ctx: object) -> bool:
        asked.append(ctx)
        return before_exit(self, ctx)

    monkeypatch.setattr(KansoStrategy, "before_exit", counted)
    hyp = tick_hypothesis("quotes", "touch", 20.0)
    groups = tape(holding_script())
    alone = card(hyp, groups, SLEEVE, tmp_path / "alone.txt", ruled=False)
    assert asked == []
    ruled = card(hyp, groups, SLEEVE, tmp_path / "ruled.txt", ruled=True)
    assert len(asked) > 50
    assert_the_same(alone, ruled)
    assert len(alone[0].run.trades) > 5


def test_a_sleeve_that_binds_a_context_read_is_consulted_as_before() -> None:
    """Only a sleeve that reads its context through the base's own methods skips building it."""

    class Plain(KansoStrategy):
        pass

    class Holds(Plain):
        def held(self, instrument_id: object) -> float:
            return 0.0

    class Below(Holds):
        pass

    assert _kept_plain(KansoStrategy)
    assert _kept_plain(Plain)
    assert not _kept_plain(Holds)
    assert not _kept_plain(Below)
    assert not _kept_plain(object)


CORRUPTS = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Buys once, then marks a kept read as current that is not, and reads its balance.\"\"\"

    config_cls = Config

    def on_start(self):
        self.bought = False

    def on_quote_tick(self, tick):
        if not self.bought:
            self.bought = self.submit_entry(tick.instrument_id, "BUY", qty=100) is not None
            return
        if self.held(tick.instrument_id) > 0:
            CORRUPT
            self.balance

    def on_order_filled(self, event):
        SETTLED
        self.balance
"""


@pytest.mark.parametrize(
    ("corrupt", "settled", "names"),
    [
        ("self._kept_open = []", "pass", "_kept_positions"),
        ("pass", "self._kept_settled = True", "_kept_settle"),
    ],
    ids=["positions kept stale", "a pass skipped with a fill to book"],
)
def test_a_kept_balance_read_that_went_stale_raises_when_read_again(
    tmp_path: Path, corrupt: str, settled: str, names: str
) -> None:
    """With the suite's check on, a kept list of open positions that no longer matches the
    cache's, and a settling pass skipped although a fill waits to be booked, each raise."""
    hyp = tick_hypothesis("fixed_bps", "touch", 0.0)
    source = CORRUPTS.replace(b"CORRUPT", corrupt.encode()).replace(b"SETTLED", settled.encode())
    groups = tape(holding_script())
    request = RunRequest(
        hyp=hyp,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=True),
        capital=CAPITAL,
    )
    with pytest.raises(AssertionError, match=names):
        execute(request, [instrument(SYMBOL), instrument(OTHER)], groups)


def test_held_names_an_id_by_the_table_kept_with_the_universe_or_afresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A universe id, an id equal to one but another object, an id outside the universe and a
    name all find the name `str` gives them, and the read kept under it."""
    monkeypatch.setattr(KansoStrategy, "_recheck_kept", False)
    sleeve = KansoStrategy(
        KansoStrategy.config_cls(universe=NAMES)  # type: ignore[call-arg]
    )
    ids = sleeve.universe
    assert sleeve._kept_names == {instrument_id: str(instrument_id) for instrument_id in ids}
    sleeve._kept_intended.update({NAMES[0]: 5.0, NAMES[1]: -2.0, "ELSE.XNAS": 1.5})
    assert sleeve.held(ids[0]) == 5.0
    assert sleeve.held(InstrumentId.from_str(NAMES[1])) == -2.0
    assert sleeve.held(InstrumentId.from_str("ELSE.XNAS")) == 1.5
    assert sleeve.held(NAMES[0]) == 5.0
