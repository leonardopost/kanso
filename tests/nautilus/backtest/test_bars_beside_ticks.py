"""A sleeve clocked on ticks reads a signal's bars at the size its hypothesis states.

A future, an index or a pair carries bars where an equity beside it carries quotes and
prints. A hypothesis at `resolution: tick` names the size its `bar` instruments are read at
(`bar_resolution`), and the runner reads them at it, the sleeve subscribes them at it, and
both are merged among the ticks by `ts_init` — a bar is stamped at its close, so it reaches
the sleeve at the instant its minute ended, never before. Before the key existed the same
file passed validation and every card stopped at subscribing bars of no size.

Synthetic points over a few instants of one day, small enough to read at a glance.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import session
from kanso.nautilus.backtest import (
    Marks,
    _bar_sizes,
    checked_chunks,
    execute,
    warmup_prefix,
    window_chunks,
    window_data,
)
from kanso.nautilus.cross_section import is_marker
from kanso.schemas import Hypothesis, VenueModel, Warmup

from .conftest import INSTRUMENT, RESEARCH, SECOND_NS, catalog, hypothesis, instrument

SIGNAL = "OTHR.XNAS"
"""The instrument that carries bars alone: a future's or an index's minute bars, say."""

DAY = date(2024, 1, 2)
MINUTE_NS = 60 * SECOND_NS
OPEN_NS = midnight_ns(DAY) + (14 * 60 + 30) * MINUTE_NS
"""14:30 UTC on the day every point is on."""

BAR_CLOSES = [OPEN_NS + MINUTE_NS, OPEN_NS + 2 * MINUTE_NS]
"""14:31:00 and 14:32:00."""

QUOTES_AT = [OPEN_NS + 30 * SECOND_NS, OPEN_NS + MINUTE_NS, OPEN_NS + 90 * SECOND_NS]
"""14:30:30, 14:31:00 — the instant the first bar closes — and 14:31:30."""


def minute_bars(closes_ns: list[int]) -> list[Bar]:
    """One-minute bars of the signal, each stamped at its close as a loader stamps one."""
    bar_type = BarType(
        InstrumentId.from_str(SIGNAL),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    return [
        Bar(
            bar_type,
            Price(100.0 + index, 2),
            Price(100.5 + index, 2),
            Price(99.5 + index, 2),
            Price(100.0 + index, 2),
            Quantity.from_int(10),
            ts_event=close,
            ts_init=close,
        )
        for index, close in enumerate(closes_ns)
    ]


def demo_quotes(at_ns: list[int]) -> list[QuoteTick]:
    return [
        QuoteTick(
            InstrumentId.from_str(INSTRUMENT),
            Price(10.0, 2),
            Price(10.02, 2),
            Quantity.from_int(100),
            Quantity.from_int(100),
            ts_event=at,
            ts_init=at,
        )
        for at in at_ns
    ]


def tick_bars() -> Hypothesis:
    """DEMO's quotes beside OTHR's bars at tick resolution, the bars read a minute at a time."""
    base = hypothesis(
        universe=(INSTRUMENT, SIGNAL), data_requirements=("quote",), resolution="tick"
    )
    return Hypothesis.model_validate(
        {
            **base.model_dump(by_alias=True, mode="json", exclude_none=True),
            "data_requirements": ["bar", "quote"],
            "bar_resolution": "1m",
            "data_by_instrument": {INSTRUMENT: ["quote"], SIGNAL: ["bar"]},
        }
    )


def held(tmp_path: Path) -> Path:
    return catalog(
        tmp_path / "catalog",
        [*minute_bars(BAR_CLOSES), *demo_quotes(QUOTES_AT)],
        [instrument(), instrument("OTHR")],
    )


def test_a_tick_request_reads_its_bars_at_the_size_its_hypothesis_states(
    request_for, tmp_path: Path
) -> None:
    request = request_for(hypothesis_=tick_bars())

    _, groups = window_data(request, held(tmp_path))

    assert _bar_sizes(request) == ("1m",)
    assert {
        (type(point).__name__, str(getattr(point, "bar_type", "")))
        for group in groups
        for point in group
    } == {("Bar", f"{SIGNAL}-1-MINUTE-LAST-EXTERNAL"), ("QuoteTick", "")}


def test_a_grain_that_is_a_bar_size_is_read_as_itself(request_for) -> None:
    """A version's manifest hands a replay and a stage node the sleeve's own grain, and an
    overlay's beside it; a bar size is read as it is, a tick grain at `bar_resolution`."""
    assert _bar_sizes(request_for(hypothesis_=hypothesis(resolution="1d"))) == ("1d",)
    assert _bar_sizes(request_for(hypothesis_=tick_bars(), grains=("tick", "5m"))) == ("1m", "5m")


def test_the_bars_reach_the_sleeve_among_the_ticks_at_their_close(
    request_for, tmp_path: Path
) -> None:
    """Both paths order every chunk by `ts_init` with a stable sort, the groups in requirement
    order: a bar lands at the instant its minute closed, between the quotes before and after
    it, and ahead of a quote of that very instant."""
    request = request_for(hypothesis_=tick_bars())
    instruments, chunks = window_chunks(request, held(tmp_path))
    marks = Marks(request, VenueModel.model_validate(dict(request.venue_model)))

    stream = [
        point
        for points in checked_chunks(request, instruments, chunks, marks)
        for point in points
        if not is_marker(point)
    ]

    assert [(type(point).__name__, int(point.ts_init)) for point in stream] == [
        ("QuoteTick", QUOTES_AT[0]),
        ("Bar", BAR_CLOSES[0]),
        ("QuoteTick", QUOTES_AT[1]),
        ("QuoteTick", QUOTES_AT[2]),
        ("Bar", BAR_CLOSES[1]),
    ]


ON_BARS = b"""
from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_quote_tick(self, tick):
        pass

    def on_bar(self, bar):
        self.submit_entry(InstrumentId.from_str("DEMO.XNAS"), "BUY", qty=1)
"""
"""Buys DEMO on every bar of the signal, so each intent's instant is the bar it was sent on."""


def test_a_tick_sleeve_is_handed_its_signal_s_bars_at_on_bar(request_for) -> None:
    """The sleeve subscribes the signal's bars at `bar_resolution`: each reaches `on_bar` at
    its close, and an order sent there is stamped with that instant."""
    request = request_for(source=ON_BARS, hypothesis_=tick_bars())

    result = execute(
        request,
        [instrument(), instrument("OTHR")],
        [tuple(minute_bars(BAR_CLOSES)), tuple(demo_quotes(QUOTES_AT))],
    )

    assert not result.crashed, result.reason
    assert [intent[:3] for intent in result.intents] == [
        (BAR_CLOSES[0], INSTRUMENT, "BUY"),
        (BAR_CLOSES[1], INSTRUMENT, "BUY"),
    ]


def test_the_node_path_hands_the_sleeve_the_same_bars_at_the_same_instants(request_for) -> None:
    """Parity at a tolerance of zero: the live code path subscribes, orders and dispatches the
    signal's bars among the ticks exactly as the research path does."""
    request = request_for(source=ON_BARS, hypothesis_=tick_bars())
    instruments = [instrument(), instrument("OTHR")]
    groups = [tuple(minute_bars(BAR_CLOSES)), tuple(demo_quotes(QUOTES_AT))]

    engine = execute(request, instruments, groups)
    node = session.run_node(request, instruments, groups).result

    assert not node.crashed, node.reason
    assert node.intents == engine.intents
    assert len(engine.intents) == len(BAR_CLOSES)


def test_a_tick_sleeve_counts_its_warmup_sessions_on_its_ticks(tmp_path: Path) -> None:
    """A session is a day the sleeve's own grain printed: its quotes, whatever bars a signal
    carries beside them. A day of the signal's bars alone is not a session of a tick sleeve."""
    quiet = midnight_ns(date(2024, 1, 1)) + (14 * 60 + 31) * MINUTE_NS
    store = catalog(
        tmp_path / "catalog",
        [*minute_bars([quiet, *BAR_CLOSES]), *demo_quotes(QUOTES_AT)],
        [instrument(), instrument("OTHR")],
    )
    warmed = tick_bars().model_copy(update={"warmup": Warmup(sessions=1)})

    assert warmup_prefix(warmed, (date(2024, 1, 3), RESEARCH[1]), store) == (DAY, DAY)
