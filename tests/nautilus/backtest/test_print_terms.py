"""A print's terms in the catalog, read beside it under the print rules.

Under `print_through` and `print_through_whole` the runner hands the venue each print its
`print_terms` mark not lit as a `kanso.nautilus.tape.Unlit`, which fills no resting order and
moves no queue; a print they mark lit, and one they say nothing of, is the print it always was.
Under `touch` and `through` the terms are not read at all. A sleeve that rests no order reads
a tape with terms exactly as one without.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.data.types import PrintTerms
from kanso.nautilus.backtest import execute, window_chunks, window_data
from kanso.nautilus.tape import Unlit
from kanso.schemas import Hypothesis

from .conftest import INSTRUMENT, RESEARCH, catalog, hypothesis, instrument

SECOND_NS = 1_000_000_000
DAY = date(2024, 1, 3)
OPEN_NS = midnight_ns(DAY) + 15 * 3_600 * SECOND_NS
NAME = InstrumentId.from_str(INSTRUMENT)

POSTING = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    \"\"\"Joins the bid of the first quote it sees with 100, and rests there.\"\"\"

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.posted = False

    def on_quote_tick(self, tick) -> None:
        if not self.posted:
            self.posted = True
            self.submit_entry(tick.instrument_id, "BUY", qty=100, price=float(tick.bid_price))
"""

TAKING = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    \"\"\"Buys 50 at market on the first quote and sells them on the third print.\"\"\"

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.quotes = 0
        self.prints = 0

    def on_quote_tick(self, tick) -> None:
        self.quotes += 1
        if self.quotes == 1:
            self.submit_entry(tick.instrument_id, "BUY", qty=50)

    def on_trade_tick(self, tick) -> None:
        self.prints += 1
        if self.prints == 3:
            self.submit_exit(tick.instrument_id)
"""


def tape() -> list[object]:
    """A quote of 10.00/10.02 showing 100 on each side, then four prints of 100 at the bid a
    second apart — P1 and P3 lit, P2 not, P4 with no terms — and a quote after them."""
    quote = QuoteTick(
        NAME, Price(10.00, 2), Price(10.02, 2), Quantity.from_int(100), Quantity.from_int(100),
        OPEN_NS, OPEN_NS,
    )  # fmt: skip
    prints = [
        TradeTick(
            NAME,
            Price(10.00, 2),
            Quantity.from_int(100),
            AggressorSide.NO_AGGRESSOR,
            TradeId(f"P{n}"),
            OPEN_NS + n * SECOND_NS,
            OPEN_NS + n * SECOND_NS,
        )
        for n in range(1, 5)
    ]
    last = QuoteTick(
        NAME, Price(10.00, 2), Price(10.02, 2), Quantity.from_int(100), Quantity.from_int(100),
        OPEN_NS + 9 * SECOND_NS, OPEN_NS + 9 * SECOND_NS,
    )  # fmt: skip
    return [quote, *prints, last]


def terms(readings: dict[str, bool]) -> list[PrintTerms]:
    """Terms for the prints `readings` names, lit or not, at each print's own instants."""
    return [
        PrintTerms(
            instrument_id=NAME,
            trade_id=name,
            exchange="4" if not lit else "11",
            conditions="" if lit else "37",
            lit=lit,
            ts_event=OPEN_NS + int(name[1:]) * SECOND_NS,
            ts_init=OPEN_NS + int(name[1:]) * SECOND_NS,
        )
        for name, lit in readings.items()
    ]


READINGS = {"P1": True, "P2": False, "P3": True}


def taped(rule: str) -> Hypothesis:
    document = hypothesis(data_requirements=("quote", "trade"), resolution="tick").model_dump(
        mode="json"
    )
    document["costs"] = {**document["costs"], "limit_fill": rule, "latency_ms": 0.0}
    return Hypothesis.model_validate(document)


def kinds(groups: tuple[tuple[object, ...], ...]) -> list[str]:
    return [
        type(point).__name__ for group in groups for point in group if isinstance(point, TradeTick)
    ]


@pytest.mark.parametrize("rule", ["print_through", "print_through_whole"])
def test_a_print_its_terms_mark_not_lit_is_handed_on_unlit_under_a_print_rule(
    tmp_path: Path, request_for, rule: str
) -> None:
    root = catalog(tmp_path / "catalog", [*tape(), *terms(READINGS)], [instrument()])
    request = request_for(RESEARCH, source=POSTING, hypothesis_=taped(rule))

    _, whole = window_data(request, root)
    _, chunks = window_chunks(request, root)

    assert kinds(whole) == ["TradeTick", "Unlit", "TradeTick", "TradeTick"]
    assert [kinds(chunk) for chunk in chunks] == [kinds(whole)]
    unlit = next(p for group in whole for p in group if type(p) is Unlit)
    assert (str(unlit.trade_id), float(unlit.price), float(unlit.size)) == ("P2", 10.0, 100.0)


@pytest.mark.parametrize("rule", ["touch", "through"])
def test_under_the_engine_s_rules_no_print_is_read_for_its_terms(
    tmp_path: Path, request_for, rule: str
) -> None:
    root = catalog(tmp_path / "catalog", [*tape(), *terms(READINGS)], [instrument()])
    _, whole = window_data(request_for(RESEARCH, source=POSTING, hypothesis_=taped(rule)), root)
    assert kinds(whole) == ["TradeTick"] * 4


def test_terms_that_read_one_print_two_ways_are_held_for_neither(
    tmp_path: Path, request_for
) -> None:
    twice = [*terms({"P1": True, "P2": False}), *terms({"P2": True})]
    root = catalog(tmp_path / "catalog", [*tape(), *twice], [instrument()])
    request = request_for(RESEARCH, source=POSTING, hypothesis_=taped("print_through"))
    _, whole = window_data(request, root)
    assert kinds(whole) == ["TradeTick"] * 4


def fills_of(tmp_path: Path, request_for, source: bytes, held: bool) -> object:
    root = catalog(
        tmp_path / ("held" if held else "bare"),
        [*tape(), *(terms(READINGS) if held else [])],
        [instrument()],
    )
    request = request_for(RESEARCH, source=source, hypothesis_=taped("print_through"))
    instruments, groups = window_data(request, root)
    return execute(request, instruments, groups)


def test_a_resting_order_fills_only_on_lit_prints_and_one_with_no_terms(
    tmp_path: Path, request_for
) -> None:
    """The buy joins a bid showing 100: P1 trades what stood ahead of it, and P2 fills it on a
    tape with no terms; with P2 marked not lit it waits for P3."""
    bare = fills_of(tmp_path, request_for, POSTING, held=False)
    held = fills_of(tmp_path, request_for, POSTING, held=True)

    def seconds(result: object) -> list[tuple[int, float]]:
        return [((f.ts_ns - OPEN_NS) // SECOND_NS, f.qty) for f in result.run.fills]  # type: ignore[attr-defined]

    assert seconds(bare) == [(2, 100.0)]
    assert seconds(held) == [(3, 100.0)]


def test_a_card_that_rests_no_order_reads_a_tape_with_terms_as_one_without(
    tmp_path: Path, request_for
) -> None:
    bare = fills_of(tmp_path, request_for, TAKING, held=False)
    held = fills_of(tmp_path, request_for, TAKING, held=True)

    assert held.run.fills  # type: ignore[attr-defined]
    assert held.run == bare.run  # type: ignore[attr-defined]
    assert held.intents == bare.intents  # type: ignore[attr-defined]
