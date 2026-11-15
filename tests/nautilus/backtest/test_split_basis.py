"""A period is read in the shares held at its end, whatever split fell inside it.

The catalog's prices are as they were published, each in the share count of the instant it
was printed at, and the venue changes the count a position holds when a split takes effect.
A weekly period holding SOXS's one-for-five of 2011-02-24 held a pre-split low of 10.19 and
the post-split shares the book held at its end, and `maintenance_margin` valued 638 of them
at 10.19 — a ratio of -55.39% where the book read 68.92% in one count. These pin the low, the
high and the mark each restated into the era in force at the end, and the era read off the
venue's own clock. Every expectation is arithmetic a reader can do by hand.
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
    BookOrder,
    OrderBookDelta,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    BarAggregation,
    BookAction,
    OrderSide,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria import Adjustment
from kanso.criteria.run import NS_PER_SECOND, Fill, midnight_ns
from kanso.data.types import CorporateAction
from kanso.nautilus.backtest import Marks, RunRequest, _equity, execute, folded
from kanso.schemas import VenueModel

from .conftest import (
    CAPITAL,
    INSTRUMENT,
    RESEARCH,
    SNAPSHOT,
    SYMBOL,
    instrument,
    venue_model,
)
from .test_book import booked
from .test_splits import HOLDER, REVERSE, restated

WEEK = (date(2024, 1, 1), date(2024, 1, 7))
"""A Monday to a Sunday: one weekly period, or seven daily ones."""

EX = date(2024, 1, 3)
"""The ex-date, a Wednesday dated in UTC: the split holds from a nanosecond past its midnight."""

SAME_VENUE = "HEDGE.XNAS"
OTHER_VENUE = "HEDGE.ARCX"


def scheduled(ratio: float) -> object:
    """DEMO, declaring one split at `ratio` on `EX`."""
    return instrument(SYMBOL, info={"splits": [{"ex_date": EX.isoformat(), "ratio": ratio}]})


def at(day: date, hour: int = 12) -> int:
    """An instant on a day of the week, in UTC."""
    return midnight_ns(day) + hour * 3_600 * NS_PER_SECOND


def request(request_for, period: str = "1w"):
    """A request over `WEEK` under a book that keeps the maintenance ratio."""
    return request_for(WEEK, hypothesis_=booked({"maintenance_pct": 30.0}), period=period)


# --- the adverse range ---------------------------------------------------------------

CASES = {
    # ratio, side, qty, px, (close, low, high) before the split and after it, adverse price
    "reverse split, long": (
        0.2, "BUY", 2_500.0, 53.51, (10.20, 10.19, 10.25), (51.40, 51.35, 51.60), 10.19 / 0.2
    ),
    "forward split, short": (
        4.0, "SELL", 1_000.0, 10.10, (40.70, 40.60, 40.80), (10.12, 10.05, 10.15), 40.80 / 4.0
    ),
    "forward split, long": (
        4.0, "BUY", 1_000.0, 10.10, (39.50, 39.00, 40.00), (10.12, 10.00, 10.20), 39.00 / 4.0
    ),
    "reverse split, short": (
        0.2, "SELL", 500.0, 51.50, (10.50, 10.40, 11.00), (51.50, 51.00, 52.00), 11.00 / 0.2
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_a_period_s_adverse_extreme_is_read_in_the_shares_held_at_its_end(
    request_for, case: tuple
) -> None:
    """A week holding the ex-date, a price printed before it and the position bought after
    it: the position is valued at the week's extreme in the new shares, the earlier print
    divided by the ratio. Read as printed, the reverse split's long was valued at its
    pre-split low, a fifth of the price, and its short at the post-split high, under the
    restated one; the forward split's short at four times its high, and its long at a low
    above the restated one — the error went whichever way the ratio and the side sent it."""
    ratio, side, qty, px, before, after, adverse = case
    made = request(request_for)
    stream = [
        (at(date(2024, 1, 2)), INSTRUMENT, *before),
        (at(date(2024, 1, 4)), INSTRUMENT, *after),
        (at(date(2024, 1, 5)), INSTRUMENT, after[0], after[0], after[0]),
    ]
    bought = Fill(at(date(2024, 1, 4)), INSTRUMENT, side, qty, px, 0.0)
    signed = qty if side == "BUY" else -qty
    printed = min(before[1], after[1]) if side == "BUY" else max(before[2], after[2])

    curve = _equity(made, folded(made, stream, [scheduled(ratio)]), [bought], {})

    cash = CAPITAL - signed * px
    assert curve.worst_ratio == (pytest.approx((cash + signed * adverse) / abs(signed * adverse)),)
    assert curve.equity == (pytest.approx(cash + signed * after[0]),)
    assert printed != pytest.approx(adverse), "read as printed, the week read another price"


def test_a_name_without_a_split_is_read_as_folded(request_for) -> None:
    """A run whose instruments declare no split folds and reads every period as before."""
    made = request(request_for)
    stream = [(at(date(2024, 1, 2)), INSTRUMENT, 10.20, 10.19, 10.25)]

    plain, declared = folded(made, stream), folded(made, stream, [instrument()])

    assert plain.periods()[0].lows == declared.periods()[0].lows == {INSTRUMENT: 10.19}


# --- the mark and the venue's clock --------------------------------------------------


def _held_through(request_for, other: str, adjusted: tuple[Adjustment, ...]):
    """1,000 DEMO bought at 10 on Tuesday and held; on Wednesday, after DEMO's one-for-ten,
    only `other` prints. Daily periods."""
    made = request(request_for, period="1d")
    stream = [
        (at(date(2024, 1, 2)), INSTRUMENT, 10.0, 9.0, 10.5),
        (at(EX), other, 5.0, 5.0, 5.0),
    ]
    bought = Fill(at(date(2024, 1, 2)), INSTRUMENT, "BUY", 1_000.0, 10.0, 0.0)
    return _equity(made, folded(made, stream, [scheduled(0.1)]), [bought], {}, adjusted)


def test_a_held_name_that_has_not_printed_since_its_split_is_marked_restated(request_for) -> None:
    """Another name of DEMO's venue prints first on the ex-date, so the venue applies DEMO's
    split there and the book holds 100 shares; DEMO's last price, 10 in the old shares, is
    100 in the new. Marked as printed, the 100 shares were worth 1,000 and the book read a
    9,000 loss on a bookkeeping change until DEMO printed again; and the period's floor, its
    mark for a name that did not print, was a tenth of the price."""
    split = Adjustment(at(EX), INSTRUMENT, qty=-900.0, paid=0.0, ratio=0.1)

    curve = _held_through(request_for, SAME_VENUE, (split,))

    assert curve.equity == (pytest.approx(CAPITAL), pytest.approx(CAPITAL))
    assert curve.worst_ratio[1] == pytest.approx((CAPITAL - 10_000.0 + 10_000.0) / 10_000.0)
    assert [(held.qty, held.notional) for held in curve.held] == [
        (1_000.0, 10_000.0),
        (100.0, pytest.approx(10_000.0)),
    ]


def test_a_point_of_another_venue_applies_no_split_and_restates_nothing(request_for) -> None:
    """A venue applies the splits of its own instruments when a point of its own reaches it, so
    a print of another venue moves nothing: the book still holds 1,000 old shares, at 10."""
    curve = _held_through(request_for, OTHER_VENUE, ())

    assert curve.equity == (pytest.approx(CAPITAL), pytest.approx(CAPITAL))
    assert [(held.qty, held.notional) for held in curve.held] == [(1_000.0, 10_000.0)] * 2


def test_a_point_printed_before_the_split_and_published_after_it_is_in_the_old_shares(
    request_for,
) -> None:
    """The eve's daily bar, stamped at the midnight that opens the ex-date and published a
    quarter of an hour later, ends the week: the venue applied it at its own stamp, before
    the split's instant, so the book still holds 1,000 old shares and nothing is restated —
    the week's low is Tuesday's 9.00. Read by publication instants, the eve's bar would have
    been in the new shares and Tuesday's low a restated 90.00, beside shares the venue had
    not split, and the week would have read the eve's 9.50."""
    made = request(request_for)
    marks = Marks(made, VenueModel.model_validate(dict(made.venue_model)), [scheduled(0.1)])
    tuesday = at(date(2024, 1, 2))
    marks.fold(tuesday, INSTRUMENT, 10.0, 9.0, 10.5, tuesday)
    eve = midnight_ns(EX)
    marks.fold(eve + 900 * NS_PER_SECOND, INSTRUMENT, 10.0, 9.5, 10.2, eve)
    bought = Fill(tuesday, INSTRUMENT, "BUY", 1_000.0, 10.0, 0.0)

    curve = _equity(made, marks, [bought], {})

    assert curve.worst_ratio == (pytest.approx((CAPITAL - 10_000.0 + 9_000.0) / 9_000.0),)
    assert curve.equity == (pytest.approx(CAPITAL),)


def _bar(day: date, close: float, low: float) -> Bar:
    spec = BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST)
    grain = BarType(InstrumentId.from_str(INSTRUMENT), spec, AggregationSource.EXTERNAL)
    ts = at(day)
    return Bar(
        grain,
        Price(close, 2),
        Price(close, 2),
        Price(low, 2),
        Price(close, 2),
        Quantity.from_int(100),
        ts,
        ts,
    )


def _delta(ts: int) -> OrderBookDelta:
    order = BookOrder(OrderSide.BUY, Price(5.0, 2), Quantity.from_int(100), 1)
    return OrderBookDelta(InstrumentId.from_str(SAME_VENUE), BookAction.ADD, order, 0, 0, ts, ts)


def _action(ts: int) -> CorporateAction:
    return CorporateAction(
        instrument_id=InstrumentId.from_str(SAME_VENUE),
        kind="dividend",
        ratio=1.0,
        cash=0.1,
        currency="USD",
        ex_date_ns=ts,
        ts_event=ts,
        ts_init=ts,
    )


@pytest.mark.parametrize(
    ("point", "mark"),
    [(_delta, (at(date(2024, 1, 2)), 100.0)), (_action, None)],
    ids=["book change", "data"],
)
def test_only_a_point_the_venue_applies_moves_its_clock(request_for, point, mark) -> None:
    """Folded as a run folds them: a book change of DEMO's venue is a point the venue applies,
    and after the split's instant it applies DEMO's split, so DEMO's last price, 10 on
    Tuesday, is carried into Wednesday restated; a point the venue is never handed — a
    corporate action, data the strategy reads — applies nothing, and Wednesday carries no
    restated price: DEMO's is Tuesday's, in the shares it was printed in."""
    made = request(request_for, period="1d")
    marks = Marks(made, VenueModel.model_validate(dict(made.venue_model)), [scheduled(0.1)])
    marks.take([_bar(date(2024, 1, 2), 10.0, 9.0), point(at(EX))])

    assert marks.periods()[1].marks.get(INSTRUMENT) == mark


# --- folded in any order -----------------------------------------------------------------

INSTANTS = st.sampled_from(
    tuple(at(day, hour) for day in (date(2024, 1, 2), EX, date(2024, 1, 4)) for hour in (0, 12, 12))
)

points = st.lists(
    st.tuples(
        INSTANTS,
        st.sampled_from((INSTRUMENT, SAME_VENUE)),
        st.sampled_from((None, 10.0, 25.0, 100.0)),
        st.sampled_from((None, 9.0, 24.0, 99.0)),
        st.sampled_from((None, 11.0, 26.0, 101.0)),
        st.sampled_from((-1, 0, 1)),
    ),
    min_size=1,
    max_size=30,
)


@settings(max_examples=150, deadline=None)
@given(drawn=points, shuffled=st.randoms(use_true_random=False))
def test_a_week_with_a_split_folds_to_the_same_periods_in_any_order(drawn: list, shuffled) -> None:
    """Points of a split name and of another name of its venue on either side of the split —
    some published at the instant they were printed, some printed a nanosecond before or
    after it — folded in the order drawn and in another: every period reads the same, its
    restated range and mark included."""
    hyp = booked({"maintenance_pct": 30.0})
    made = RunRequest(
        hyp=hyp,
        strategy_source=b"",
        window=WEEK,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp),
        capital=CAPITAL,
        period="1d",
    )
    model = VenueModel.model_validate(dict(made.venue_model))
    one, other = Marks(made, model, [scheduled(0.1)]), Marks(made, model, [scheduled(0.1)])
    reordered = list(drawn)
    shuffled.shuffle(reordered)
    for held, order in ((one, drawn), (other, reordered)):
        for ts, key, price, low, high, skew in order:
            held.fold(ts, key, price, low, high, ts + skew)

    def state(marks: Marks) -> object:
        return [
            (
                fold.end,
                sorted(fold.marks.items()),
                sorted(fold.lows.items()),
                sorted(fold.highs.items()),
            )
            for fold in marks.periods()
        ]

    assert repr(state(one)) == repr(state(other))
    assert repr(state(one)) == repr(state(one)), "read twice, the periods read the same"


# --- through the engine --------------------------------------------------------------


def test_a_week_holding_a_split_reads_its_low_in_the_new_shares(request_for) -> None:
    """1,005 shares at 10 through a one-for-ten on Tuesday January 16th, run by the venue:
    the week that holds it opens Monday the 15th, whose bar printed 10 in the old shares,
    and ends holding 100 new shares at 100. The week's low is 100 in the new shares, and the
    ratio is the book over 10,000 of stock; read as printed, the low was the Monday's 10 and
    the book was valued at 1,000 of stock."""
    made = request_for(
        RESEARCH, source=HOLDER, hypothesis_=booked({"maintenance_pct": 30.0}), period="1w"
    )

    card = execute(made, [instrument(info=REVERSE)], [tuple(restated())]).run

    week = next(
        index
        for index, end in enumerate(card.period_ends_ns)
        if end > midnight_ns(date(2024, 1, 16))
    )
    entry = card.fills[0].cost
    cash = CAPITAL - 10_050.0 - entry + 50.0
    assert card.worst_ratio[week] == pytest.approx((cash + 100 * 100.0) / (100 * 100.0))
    assert card.equity[week] == pytest.approx(cash + 100 * 100.0)
