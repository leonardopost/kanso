"""A window folds to the prices its points carry, however the fold reads them.

`Marks.feed` reads a quote, a print and a bar from the prices each carries and every other
point through `_price_of`, `_range_of` and `_instrument_of`. Both readings are held here to
the plain stream `folded` takes — the instant, the name, the mark and the adverse range —
which is what the extraction strikes the equity curve at, and to the quoted half-spreads a
fill is charged.
"""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nautilus_trader.model.data import (
    Bar,
    BarSpecification,
    BarType,
    CustomData,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import AggregationSource, AggressorSide, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import NS_PER_DAY, midnight_ns
from kanso.errors import PreconditionError
from kanso.nautilus.backtest import Marks, RunRequest, folded
from kanso.nautilus.cross_section import MARKER_TYPE, KansoCrossSection
from kanso.schemas import VenueModel

from .conftest import INSTRUMENT, SNAPSHOT, hypothesis, venue_model

OTHER = "HEDGE.XNAS"
WINDOW = (date(2024, 1, 1), date(2024, 1, 3))
PREFIX = (date(2023, 12, 29), date(2023, 12, 29))
OPENS = midnight_ns(PREFIX[0])
CLOSES = midnight_ns(WINDOW[1]) + NS_PER_DAY


def _request(*, quoted: bool) -> RunRequest:
    costs: dict[str, object] = {"commission_bps": 1.0}
    costs.update({"spread": "quotes"} if quoted else {"spread": "fixed_bps", "fixed_bps": 4.0})
    hyp = hypothesis(
        universe=(INSTRUMENT, OTHER), data_requirements=("bar", "quote", "trade"), costs=costs
    )
    return RunRequest(
        hyp=hyp,
        strategy_source=b"",
        window=WINDOW,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=quoted),
        capital=100_000.0,
        prefix=PREFIX,
    )


def _point(kind: str, name: str, ts: int, cents: int, width: int) -> object:
    instrument_id = InstrumentId.from_str(name)
    low, high = Price(cents / 100, 2), Price((cents + width) / 100, 2)
    if kind == "quote":
        size = Quantity.from_int(100)
        return QuoteTick(instrument_id, low, high, size, size, ts, ts)
    if kind == "trade":
        return TradeTick(
            instrument_id, low, Quantity.from_int(10), AggressorSide.BUYER, TradeId(str(ts)), ts, ts
        )
    if kind == "bar":
        spec = BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST)
        bar_type = BarType(instrument_id, spec, AggregationSource.EXTERNAL)
        mid = Price((cents + width // 2) / 100, 2)
        return Bar(bar_type, mid, high, low, mid, Quantity.from_int(1_000), ts, ts)
    return CustomData(MARKER_TYPE, KansoCrossSection(n=1, ts_event=ts, ts_init=ts))


def _plain(point: object) -> tuple[int, str, float, float, float] | None:
    """The mark and the range a point carries, read off it by hand."""
    if isinstance(point, QuoteTick):
        bid, ask = float(point.bid_price), float(point.ask_price)
        return int(point.ts_init), point.instrument_id.value, (bid + ask) / 2.0, bid, ask
    if isinstance(point, TradeTick):
        px = float(point.price)
        return int(point.ts_init), point.instrument_id.value, px, px, px
    if isinstance(point, Bar):
        name = point.bar_type.instrument_id.value
        return int(point.ts_init), name, float(point.close), float(point.low), float(point.high)
    return None


def _state(marks: Marks) -> object:
    folds = [(fold.end, fold.marks, fold.lows, fold.highs) for fold in marks.periods()]
    return folds, marks.prefix.marks, marks._credited


points = st.lists(
    st.tuples(
        st.sampled_from(("quote", "trade", "bar", "marker")),
        st.sampled_from((INSTRUMENT, OTHER)),
        st.integers(min_value=OPENS, max_value=CLOSES - 1),
        st.integers(min_value=100, max_value=10_000),
        st.integers(min_value=0, max_value=50),
    ),
    max_size=60,
)


@settings(max_examples=60, deadline=None)
@given(drawn=points, quoted=st.booleans())
def test_every_point_folds_to_the_prices_it_carries(drawn: list, quoted: bool) -> None:
    request = _request(quoted=quoted)
    stream = [_point(*each) for each in drawn]
    marks = Marks(request, VenueModel.model_validate(dict(request.venue_model)))
    marks.take(stream)
    expected = folded(request, [plain for p in stream if (plain := _plain(p)) is not None])

    assert _state(marks) == _state(expected)
    # every quote is kept, under either spread: a fill's touch is read against it
    series: dict[str, list[tuple[int, float, float]]] = {}
    for p in stream:
        if isinstance(p, QuoteTick):
            quote = (int(p.ts_init), float(p.bid_price), float(p.ask_price))
            series.setdefault(p.instrument_id.value, []).append(quote)
    assert {
        name: list(zip(*(list(column) for column in held), strict=True))
        for name, held in marks._series.items()
    } == series


@pytest.mark.parametrize("kind", ["quote", "trade", "bar"])
def test_a_market_point_outside_the_span_is_refused(kind: str) -> None:
    request = _request(quoted=False)
    marks = Marks(request, VenueModel.model_validate(dict(request.venue_model)))

    with pytest.raises(PreconditionError, match="lies outside the requested window"):
        marks.take([_point(kind, INSTRUMENT, CLOSES, 1_000, 2)])
