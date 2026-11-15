"""The venue's record of the instant each fill's price was struck at.

`kanso.nautilus.struck` keeps each name's last point as the venue is about to apply it — a
bar only of the type the matching engine executes on, and only on a venue that applies bars
at all — and each fill's when the execution engine applies it. These tests hand it points and
fills directly, and load it on a real venue to see what it is handed there.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarSpecification, BarType, TradeTick
from nautilus_trader.model.enums import (
    AccountType,
    AggregationSource,
    AggressorSide,
    BarAggregation,
    BookType,
    OmsType,
    PriceType,
)
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.objects import Money, Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import actions
from kanso.nautilus.availability import Availability
from kanso.nautilus.struck import Struck, StruckPart
from kanso.nautilus.tape import Tape

from .conftest import SECOND_NS, instrument

T0 = midnight_ns(date(2024, 1, 2)) + 15 * 3_600 * SECOND_NS
MINUTE = 60 * SECOND_NS
NAME = instrument().id


def _bar(
    step: int,
    ts_event: int,
    ts_init: int | None = None,
    source: AggregationSource = AggregationSource.EXTERNAL,
) -> Bar:
    kind = BarType(NAME, BarSpecification(step, BarAggregation.MINUTE, PriceType.LAST), source)
    px = Price(10.0 + (ts_event - T0) / MINUTE / 100, 2)
    return Bar(
        kind,
        px,
        px,
        px,
        px,
        Quantity.from_int(100),
        ts_event,
        ts_event if ts_init is None else ts_init,
    )


def _print(ts: int) -> TradeTick:
    return TradeTick(
        NAME, Price(10.0, 2), Quantity.from_int(10), AggressorSide.BUYER, TradeId(str(ts)), ts, ts
    )


def _fill(trade_id: str) -> SimpleNamespace:
    """A fill as the bus carries it, as far as the record reads one: its name and the trade id
    its matching engine gave it."""
    return SimpleNamespace(instrument_id=NAME, trade_id=TradeId(trade_id))


def _heard(struck: Struck, trade_id: str) -> int | None:
    struck.filled(_fill(trade_id))
    return struck.at(_fill(trade_id))


def test_a_fill_is_kept_at_its_name_s_last_point_at_that_point_s_reference_time() -> None:
    """A bar published after its close is kept at its close; a print at its own stamp; and a
    fill of a name the venue had applied nothing of is not kept at all."""
    struck = Struck()

    assert _heard(struck, "none") is None
    struck.bar(_bar(1, T0, T0 + 15 * MINUTE))
    assert _heard(struck, "bar") == T0
    struck.point(_print(T0 + 30 * SECOND_NS), NAME)
    assert _heard(struck, "print") == T0 + 30 * SECOND_NS
    assert struck.at(_fill("bar")) == T0


def test_every_event_of_one_fill_reads_its_instant_and_no_other_fill_of_the_name_does() -> None:
    """A fill that flips a position reaches the positions as two events, the second under an
    event id the bus never carried; both keep the fill's name and trade id, which is what the
    record keys on. Another fill of the name, under a trade id of its own, was not heard."""
    struck = Struck()
    struck.point(_print(T0), NAME)
    struck.filled(SimpleNamespace(id="carried", instrument_id=NAME, trade_id=TradeId("flip")))
    opened = SimpleNamespace(id="never-carried", instrument_id=NAME, trade_id=TradeId("flip"))

    assert struck.at(opened) == T0
    assert struck.at(_fill("another")) is None


def test_of_a_name_s_bar_types_only_the_one_the_venue_executes_on_is_kept() -> None:
    """The matching engine executes on the first bar type it is handed, replaced by one of a
    step no longer than it; a bar of a longer step, or one aggregated inside the engine, moves
    nothing."""
    struck = Struck()

    struck.bar(_bar(5, T0))
    assert _heard(struck, "five") == T0
    struck.bar(_bar(1, T0 + MINUTE))
    assert _heard(struck, "one") == T0 + MINUTE
    struck.bar(_bar(5, T0 + 5 * MINUTE))
    assert _heard(struck, "skipped") == T0 + MINUTE
    struck.bar(_bar(1, T0 + 2 * MINUTE, source=AggregationSource.INTERNAL))
    assert _heard(struck, "internal") == T0 + MINUTE
    struck.bar(_bar(1, T0 + 2 * MINUTE))
    assert _heard(struck, "again") == T0 + 2 * MINUTE


def _venue(bar_execution: bool, struck: Struck) -> BacktestEngine:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=NAME.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(1_000_000, USD)],
        book_type=BookType.L1_MBP,
        bar_execution=bar_execution,
        modules=actions.modules(NAME.venue.value, struck),
    )
    engine.add_instrument(instrument())
    engine.add_data([_bar(1, T0), _print(T0 + 30 * SECOND_NS), _bar(1, T0 + MINUTE)])
    engine.run()
    return engine


def test_a_venue_hands_the_part_its_bars_only_where_it_applies_them() -> None:
    """A venue that executes on bars applies them and a print, so the last is its last bar; one
    that does not applies only the print."""
    barred = Struck()
    unbarred = Struck()
    for bar_execution, struck in ((True, barred), (False, unbarred)):
        _venue(bar_execution, struck).dispose()

    assert _heard(barred, "barred") == T0 + MINUTE
    assert _heard(unbarred, "unbarred") == T0 + 30 * SECOND_NS


def test_the_part_is_loaded_last_and_only_with_a_record() -> None:
    """After `Tape`, which may land what a print makes due before it is applied: an order
    landed there is struck at the market before the print."""
    struck = Struck()
    loaded = actions.modules("XNAS", struck)[0].parts

    assert [type(part) for part in loaded] == [
        actions.CorporateActions,
        Availability,
        Tape,
        StruckPart,
    ]
    assert str(loaded[-1].id) == "Struck-XNAS"
    assert loaded[-1].struck is struck
    assert StruckPart not in {type(part) for part in actions.modules("XNAS")[0].parts}
    unpointed = actions.modules("XNAS", Struck(points=False))[0].parts
    assert StruckPart not in {type(part) for part in unpointed}
    part = loaded[-1]
    part.process(T0)
    part.log_diagnostics(None)
    part.reset()
