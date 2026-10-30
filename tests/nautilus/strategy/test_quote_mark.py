"""`balance_at_quote` on the engine: a quote from before a split the venue has applied is restated
as `balance` restates a print, a sleeve no engine holds reads its capital, what it keeps is read
again and compared under the suite's check, and reading it on every point changes no card."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import kanso
from kanso.criteria.run import midnight_ns
from kanso.nautilus.backtest import RunRequest, execute
from kanso.nautilus.strategy import KansoConfig, KansoStrategy
from tests.nautilus.backtest.conftest import CAPITAL, RESEARCH, SNAPSHOT, SYMBOL, venue_model
from tests.nautilus.backtest.conftest import instrument as backtest_instrument
from tests.nautilus.backtest.test_kept_reads_card import (
    OTHER,
    SLEEVE,
    Step,
    assert_the_same,
    card,
    holding_script,
    steps,
    tape,
    tick_hypothesis,
)

from .conftest import DEMO, HEDGE, equity
from .test_actions import EX, SCHEDULE, _hedge_quote, _stamped, config

SECOND_NS = 1_000_000_000


class ReadsThrough(KansoStrategy):
    """Buys 1,005 HEDGE at market on HEDGE's first quote and holds them through HEDGE's split,
    and writes down both balances on every bar of DEMO's it is handed."""

    config_cls = KansoConfig

    def __init__(self, config: KansoConfig | None = None) -> None:
        super().__init__(config)
        self.bought = False
        self.read: list[tuple[float, float, float]] = []

    def on_quote_tick(self, tick: object) -> None:
        if not self.bought:
            self.bought = True
            self.submit_entry(HEDGE, "BUY", qty=1_005)

    def on_bar(self, bar: object) -> None:
        self.read.append((self.balance, self.balance_at_quote, self._cash))


def test_a_quote_from_before_a_split_is_restated_as_the_balance_restates_a_print(
    backtest,
) -> None:
    """HEDGE's last point when DEMO's first bar of the ex-date applies its one-for-ten reverse
    split to the 1,005 held is its quote of the eve, 9.98/10.03: both balances mark the 100
    shares the split leaves at that quote's mid in the new count, 100.05, and are one number. A
    mid left in the old count would mark them at a tenth of that."""
    midnight = midnight_ns(EX)
    sleeve = ReadsThrough(
        config(universe=(DEMO.value, HEDGE.value), data_requirements=("bar", "quote"))
    )
    run = backtest(
        sleeve,
        instruments=[equity(DEMO), equity(HEDGE, info=SCHEDULE)],
        data=[
            _hedge_quote(
                9.99,
                10.01,
                5_000,
                5_000,
                midnight - 7_200 * SECOND_NS,
                midnight - 7_200 * SECOND_NS,
            ),
            _hedge_quote(
                9.98, 10.03, 5_000, 5_000, midnight - 90 * SECOND_NS, midnight - 30 * SECOND_NS
            ),
            _stamped(DEMO, 10.0, midnight + 10 * SECOND_NS, midnight + 11 * SECOND_NS),
        ],
    )

    (position,) = run.engine.cache.positions(instrument_id=HEDGE)
    assert float(position.quantity) == 100.0
    ((balance, at_quote, cash),) = sleeve.read
    assert at_quote.hex() == balance.hex()
    assert at_quote - cash == pytest.approx(100 * 100.05, rel=1e-12)


def test_a_sleeve_no_engine_holds_reads_its_capital_at_the_quote() -> None:
    sleeve = KansoStrategy(KansoConfig(capital=25_000.0))
    assert sleeve.balance_at_quote == sleeve.balance == 25_000.0


def test_no_part_of_kanso_reads_the_balance_at_quote() -> None:
    """Sizing and the room read `balance`, and a card is measured on the runner's equity: the
    reader is a strategy's alone, so a sleeve that never reads it trades as it did before it
    existed. The one module of the package that names it is the one that defines it."""
    root = Path(kanso.__file__).parent
    naming = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "balance_at_quote" in path.read_text(encoding="utf-8")
    )
    assert naming == ["nautilus/strategy.py"]


READS = b"""


class Strategy(Strategy):
    \"\"\"The same sleeve, reading `balance_at_quote` before it handles each quote and print and
    writing nothing more down.\"\"\"

    def on_quote_tick(self, tick):
        self.balance_at_quote
        super().on_quote_tick(tick)

    def on_trade_tick(self, tick):
        self.balance_at_quote
        super().on_trade_tick(tick)
"""


@settings(max_examples=20, deadline=None)
@given(
    script=steps,
    costs=st.sampled_from(
        (("fixed_bps", "touch", 0.0), ("quotes", "touch", 20.0), ("quotes", "print_through", 20.0))
    ),
    rechecked=st.booleans(),
)
def test_reading_the_balance_at_quote_on_every_point_changes_no_card(
    tmp_path_factory: pytest.TempPathFactory,
    script: list[Step],
    costs: tuple[str, str, float],
    rechecked: bool,
) -> None:
    """Over random tapes of two names, a sleeve that reads `balance_at_quote` on every quote and
    print — on a print, where nothing else of the sleeve's reads its balance, it books the fills
    the venue made on it first — reads every balance and holding and makes every fill, period end
    and intent the sleeve that never reads it makes, to the last bit: with what it keeps answered
    as kept, as in a card, and read again and compared beside it."""
    hyp = tick_hypothesis(*costs)
    groups = tape(script)
    root = tmp_path_factory.mktemp("at_quote")
    plain = card(hyp, groups, SLEEVE, root / "plain.txt", ruled=False)
    was = KansoStrategy._recheck_kept
    KansoStrategy._recheck_kept = rechecked
    try:
        reading = card(hyp, groups, SLEEVE + READS, root / "reading.txt", ruled=False)
    finally:
        KansoStrategy._recheck_kept = was
    assert_the_same(reading, plain)


STALE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Buys once, then marks what `balance_at_quote` kept as current when it is not, and reads
    it.\"\"\"

    config_cls = Config

    def on_start(self):
        self.bought = False

    def on_quote_tick(self, tick):
        if not self.bought:
            self.bought = self.submit_entry(tick.instrument_id, "BUY", qty=100) is not None
            return
        if self.held(tick.instrument_id) > 0:
            self.balance_at_quote
            STALE
            self.balance_at_quote
"""


@pytest.mark.parametrize(
    "stale",
    [
        "self._kept_marked = []",
        "self._kept_marked = [(n, q * 2, m, p) for n, q, m, p in self._kept_marked]",
    ],
    ids=["a position dropped", "a quantity moved"],
)
def test_a_kept_mark_that_went_stale_raises_when_read_again(stale: str) -> None:
    """With the suite's check on, what `balance_at_quote` kept of the open positions is read
    again on every read, and a kept list that no longer matches the positions raises."""
    hyp = tick_hypothesis("fixed_bps", "touch", 0.0)
    request = RunRequest(
        hyp=hyp,
        strategy_source=STALE.replace(b"STALE", stale.encode()),
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=True),
        capital=CAPITAL,
    )
    with pytest.raises(AssertionError, match="_kept_marks"):
        execute(
            request,
            [backtest_instrument(SYMBOL), backtest_instrument(OTHER)],
            tape(holding_script()),
        )


def test_the_marks_kept_are_the_positions_read_afresh(tmp_path: Path) -> None:
    """Read as a card reads it, with what it keeps answered as kept, `balance_at_quote` is on
    every point the number reading every position afresh makes."""
    hyp = tick_hypothesis("quotes", "print_through", 20.0)
    groups = tape(holding_script())
    was = KansoStrategy._recheck_kept
    KansoStrategy._recheck_kept = False
    try:
        kept = card(hyp, groups, SLEEVE + WRITES, tmp_path / "kept.txt", ruled=False)
    finally:
        KansoStrategy._recheck_kept = was
    afresh = card(hyp, groups, SLEEVE + WRITES + AFRESH, tmp_path / "afresh.txt", ruled=False)
    assert_the_same(kept, afresh)
    assert sum(" at " in line for line in kept[1]) > 100


WRITES = b"""


class Strategy(Strategy):
    \"\"\"The same sleeve, writing `balance_at_quote` down before it handles each point.\"\"\"

    def on_quote_tick(self, tick):
        self.out.write(f"{tick.ts_init} at {self.balance_at_quote.hex()}\\n")
        super().on_quote_tick(tick)

    def on_trade_tick(self, tick):
        self.out.write(f"{tick.ts_init} at {self.balance_at_quote.hex()}\\n")
        super().on_trade_tick(tick)
"""

AFRESH = b"""


class Strategy(Strategy):
    \"\"\"The same sleeve, `balance_at_quote` read from every position afresh on every read.\"\"\"

    @property
    def balance_at_quote(self):
        self._settle()
        return self._cash + sum(
            (
                self._at_quote(
                    self._kept_names.get(position.instrument_id) or position.instrument_id.value,
                    float(position.signed_qty),
                    self._multiplier_of(position.instrument_id),
                    position,
                )
                for position in self.cache.positions_open(strategy_id=self.id)
            ),
            0.0,
        )
"""
