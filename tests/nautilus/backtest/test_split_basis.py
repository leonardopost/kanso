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
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import (
    Bar,
    BarSpecification,
    BarType,
    BookOrder,
    OrderBookDelta,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    BookAction,
    OrderSide,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria import Adjustment, drawdown_pct
from kanso.criteria.quantities import fill_path_drawdown_pct
from kanso.criteria.run import NS_PER_SECOND, Fill, midnight_ns
from kanso.data.types import CorporateAction
from kanso.nautilus import backtest, node
from kanso.nautilus.backtest import Marks, RunRequest, _equity, execute, folded
from kanso.nautilus.book import execute_book
from kanso.nautilus.node import Placement, StageNode
from kanso.schemas import Hypothesis, Limits, VenueModel
from kanso.strategy.impl import Built, Loaded

from .conftest import (
    CAPITAL,
    CLOSE_NS,
    FLAT_SLEEVE,
    INSTRUMENT,
    RESEARCH,
    SECOND_NS,
    SNAPSHOT,
    SYMBOL,
    bar_type,
    catalog,
    hypothesis,
    instrument,
    venue_model,
)
from .test_book import booked
from .test_shared_book import block, hyp, member
from .test_shared_book import request as book_request
from .test_splits import EX as REVERSE_EX
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


# --- the venue's clock is the feed's ------------------------------------------------


def _print(ts: int) -> TradeTick:
    """A print of HEDGE, another name of DEMO's venue."""
    return TradeTick(
        InstrumentId.from_str(SAME_VENUE),
        Price(5.0, 2),
        Quantity.from_int(100),
        AggressorSide.BUYER,
        TradeId("1"),
        ts,
        ts,
    )


@pytest.mark.parametrize(
    ("point", "mark"),
    [
        (_print, (at(date(2024, 1, 2)), 100.0)),
        (_delta, (at(date(2024, 1, 2)), 100.0)),
        (_action, None),
    ],
    ids=["print", "book change", "data"],
)
def test_a_view_is_read_in_the_era_the_feed_s_venue_applied(request_for, point, mark) -> None:
    """A view that folds DEMO and a name of another venue holds no point of DEMO's venue past
    the split; the feed the venue was handed holds HEDGE's, which applied it. Handed the feed
    (`clocked`), the view's Wednesday carries DEMO's Tuesday 10 restated to 100, as the venue
    split the shares; a point the venue is never handed applies nothing. Read off the view
    alone, nothing was restated beside the 100 shares the venue left the view's position."""
    made = request(request_for, period="1d")
    view = Marks(made, VenueModel.model_validate(dict(made.venue_model)), [scheduled(0.1)])
    tuesday, wednesday = at(date(2024, 1, 2)), at(EX)
    view.fold(tuesday, INSTRUMENT, 10.0, 9.0, 10.5, tuesday)
    view.fold(wednesday, OTHER_VENUE, 5.0, 5.0, 5.0, wednesday)

    assert view.periods()[1].marks.get(INSTRUMENT) is None
    view.clocked([point(wednesday), _bar(date(2024, 1, 2), 10.0, 9.0)])
    assert view.periods()[1].marks.get(INSTRUMENT) == mark


def test_a_view_s_point_the_feed_does_not_hold_moves_no_clock(request_for) -> None:
    """A stage version's view holds every point of its own series over the window, and a halt
    can leave some of them unreleased: HEDGE's Wednesday print, past the split, is in the view
    and never reached the venue, which applied nothing. Folded, it moved the view's clock; read
    off the released feed (`clocked`), which holds Tuesday's bar alone, no split is in force
    and DEMO's Tuesday 10 is carried as printed. Kept, the view marked the 1,000 old shares the
    venue still held at 100."""
    made = request(request_for, period="1d")
    view = Marks(made, VenueModel.model_validate(dict(made.venue_model)), [scheduled(0.1)])
    tuesday, wednesday = at(date(2024, 1, 2)), at(EX)
    view.fold(tuesday, INSTRUMENT, 10.0, 9.0, 10.5, tuesday)
    view.fold(wednesday, SAME_VENUE, 5.0, 5.0, 5.0, wednesday)
    assert view.periods()[1].marks[INSTRUMENT] == (tuesday, pytest.approx(100.0))

    view.clocked([_bar(date(2024, 1, 2), 10.0, 9.0)])

    assert view.periods()[1].marks.get(INSTRUMENT) is None


def test_a_price_printed_in_shares_the_venue_has_not_split_into_is_read_in_the_old(
    request_for,
) -> None:
    """A point the venue is never handed carries no print instant and is counted in the era of
    the instant it was published at: DEMO's 100, published past the split with nothing of its
    venue past it, is read in the old shares the venue still holds — 10, and its low 9.90."""
    made = request(request_for, period="1d")
    marks = Marks(made, VenueModel.model_validate(dict(made.venue_model)), [scheduled(0.1)])
    tuesday = at(date(2024, 1, 2))
    marks.fold(tuesday, INSTRUMENT, 10.0, 9.0, 10.5, tuesday)
    marks.fold(at(EX), INSTRUMENT, 100.0, 99.0, 101.0)

    wednesday = marks.periods()[1]
    assert wednesday.marks[INSTRUMENT] == (at(EX), pytest.approx(10.0))
    assert wednesday.lows[INSTRUMENT] == pytest.approx(9.9)


def _republished(bar: Bar, ts_init: int) -> Bar:
    """The same bar, published at another instant."""
    return Bar(
        bar.bar_type,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        ts_event=bar.ts_event,
        ts_init=ts_init,
    )


def test_a_split_applied_by_a_point_published_after_an_end_is_booked_after_it(
    request_for,
) -> None:
    """DEMO's ex-date bar, printed at 16:00 on the 16th, is published at 00:30 on the 17th, and
    the eve's bar, printed on the 15th, at 20:00 on the 16th, which ends that day's period. The
    venue applies the one-for-ten when it is handed the ex-date's bar, and the runner books it
    at that instant: the 16th ends holding the 1,005 old shares at the eve's 10, the 17th 100
    new shares at 100, and the book is worth what it paid at every end before the sale.
    Booked at the instant the ex-date's bar was printed, the 16th held 100 shares at the old
    price, 9,000 under the book, and its ratio counted the same 100 at the old low."""
    made = request_for(
        RESEARCH, source=HOLDER, hypothesis_=booked({"maintenance_pct": 30.0}), period="1d"
    )
    points = restated()
    eve = next(i for i, bar in enumerate(points) if bar.ts_event >= midnight_ns(REVERSE_EX)) - 1
    sixteenth_end = midnight_ns(REVERSE_EX) + 20 * 3_600 * SECOND_NS
    applied = midnight_ns(date(2024, 1, 17)) + 1_800 * SECOND_NS
    points[eve] = _republished(points[eve], sixteenth_end)
    points[eve + 1] = _republished(points[eve + 1], applied)

    card = execute(made, [instrument(info=REVERSE)], [tuple(points)]).run

    (split,) = card.adjustments
    assert split.ts_ns == applied
    bought, sold = card.fills
    cash = CAPITAL - 10_050.0 - bought.cost
    before_sale = [
        index for index, end in enumerate(card.period_ends_ns) if bought.ts_ns <= end < sold.ts_ns
    ]
    assert sixteenth_end in card.period_ends_ns
    assert [card.equity[index] for index in before_sale] == [pytest.approx(cash + 10_050.0)] * len(
        before_sale
    )
    sixteenth = card.period_ends_ns.index(sixteenth_end)
    assert [(held.qty, held.notional) for held in card.held if held.ts_ns == sixteenth_end] == [
        (1_005.0, pytest.approx(10_050.0))
    ]
    assert card.worst_ratio[sixteenth] == pytest.approx((cash + 10_050.0) / 10_050.0)


FIVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done:
            self.done = True
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=5.0)
"""
"""Buys five DEMO on its first bar and holds them."""


def test_a_trade_a_split_paid_out_closes_where_the_runner_booked_it(request_for) -> None:
    """Five DEMO through the one-for-ten are paid out whole, and the split is applied by the
    ex-date's bar, printed at 16:00 on the 16th and published at 00:30 on the 17th: the trade
    closes at 00:30, where the runner booked the payout, and the 16th's end, at 20:00 between
    the two, still holds the five shares. Dated by the bar's print, the trade closed before an
    end that held it."""
    made = request_for(RESEARCH, source=FIVE, period="1d")
    points = restated()
    eve = next(i for i, bar in enumerate(points) if bar.ts_event >= midnight_ns(REVERSE_EX)) - 1
    sixteenth_end = midnight_ns(REVERSE_EX) + 20 * 3_600 * SECOND_NS
    applied = midnight_ns(date(2024, 1, 17)) + 1_800 * SECOND_NS
    points[eve] = _republished(points[eve], sixteenth_end)
    points[eve + 1] = _republished(points[eve + 1], applied)

    card = execute(made, [instrument(info=REVERSE)], [tuple(points)]).run

    (split,) = card.adjustments
    (trade,) = card.trades
    assert (split.qty, split.ts_ns) == (-5.0, applied)
    assert trade.closed_ns == applied
    assert [held.qty for held in card.held if held.ts_ns == sixteenth_end] == [5.0]


# --- a view on a shared venue --------------------------------------------------------

EVE_NS = midnight_ns(REVERSE_EX)
"""The midnight that closes the eve's session, in UTC, where DEMO's split is dated: the eve's
daily bar is stamped there, a nanosecond before the split."""


def _delayed(symbol: str, ts_event: int, ts_init: int, close: float) -> Bar:
    return Bar(
        bar_type(symbol),
        Price(close, 2),
        Price(close, 2),
        Price(round(close * 0.99, 2), 2),
        Price(close, 2),
        Quantity.from_int(100_000),
        ts_event=ts_event,
        ts_init=ts_init,
    )


def _late_daily() -> tuple[Bar, ...]:
    """DEMO's sessions of January: the first published at 16:00 UTC, inside regular hours,
    and each later one stamped at the midnight that closes it and published a quarter of an
    hour after, 10 in the old shares to the eve and 100 in the new ones from the ex-date."""
    first = midnight_ns(RESEARCH[0]) + 86_400 * SECOND_NS + CLOSE_NS
    made = [_delayed(SYMBOL, first, first + SECOND_NS, 10.0)]
    for index in range(1, (RESEARCH[1] - RESEARCH[0]).days):
        stamped = midnight_ns(RESEARCH[0]) + (index + 1) * 86_400 * SECOND_NS
        close = 10.0 if stamped <= EVE_NS else 100.0
        made.append(_delayed(SYMBOL, stamped, stamped + 900 * SECOND_NS, close))
    return tuple(made)


def _early_other() -> tuple[Bar, ...]:
    """OTHR's sessions, each printed five minutes past a UTC midnight and published a second
    later: on the ex-date the first point of DEMO's venue past the split."""
    made = []
    for index in range(1, (RESEARCH[1] - RESEARCH[0]).days + 1):
        stamped = midnight_ns(RESEARCH[0]) + index * 86_400 * SECOND_NS + 300 * SECOND_NS
        made.append(_delayed("OTHR", stamped, stamped + SECOND_NS, 5.0))
    return tuple(made)


def test_a_book_member_whose_split_another_member_applied_is_read_in_one_count() -> None:
    """Member m holds 1,005 DEMO through its one-for-ten; member n holds nothing, and its OTHR
    bar at 00:05 on the ex-date is the first point of their venue past the split, so the venue
    applies it there and m holds 100 shares at the end its eve's bar, published at 00:15,
    closes. m's own points hold nothing past the split; read in the feed's era, m's 100 are
    worth 100 each, and m in the book strikes what m alone and the book's own curve strike,
    at that end and along its fill path. Read in m's own era they were worth 10, 9,000 under
    the book."""
    instruments = (instrument(info=REVERSE), instrument("OTHR"))
    alone = book_request(hyp((INSTRUMENT,)), HOLDER)
    flat = book_request(hyp(("OTHR.XNAS",)), FLAT_SLEEVE)
    demo, other = (_late_daily(),), (_early_other(),)

    booked_run = execute_book(
        block(), [member("m", alone, 2.0), member("n", flat, 1.0)], instruments, [demo, other]
    )
    solo = execute(alone, list(instruments), demo).run
    run = booked_run.runs[booked_run.members.index("m")]

    end = EVE_NS + 900 * SECOND_NS
    (split,) = run.adjustments
    assert split.ts_ns == EVE_NS + 301 * SECOND_NS
    assert [(held.qty, held.notional) for held in run.held if held.ts_ns == end] == [
        (100.0, pytest.approx(10_000.0))
    ]
    assert (
        run.equity[run.period_ends_ns.index(end)]
        == pytest.approx(solo.equity[solo.period_ends_ns.index(end)])
        == pytest.approx(booked_run.equity[booked_run.period_ends_ns.index(end)])
    )
    assert fill_path_drawdown_pct(run) == fill_path_drawdown_pct(solo)


ZED = "ZED.ARCX"


def _listed(name: str, **extra: object) -> Equity:
    """A US equity on the venue its id names."""
    symbol, venue = name.split(".")
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(venue)),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        **extra,
    )


def _sessions(name: str, close) -> list[Bar]:
    """One daily bar of `name` for each January day `close` prices, published a second after
    its 16:00 UTC close."""
    grain = BarType(
        InstrumentId.from_str(name),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for index in range((RESEARCH[1] - RESEARCH[0]).days + 1):
        day = date.fromordinal(RESEARCH[0].toordinal() + index)
        price = close(day)
        if price is not None:
            ts = midnight_ns(day) + CLOSE_NS
            made.append(
                Bar(
                    grain,
                    Price(price, 2),
                    Price(price, 2),
                    Price(price, 2),
                    Price(price, 2),
                    Quantity.from_int(100_000),
                    ts_event=ts,
                    ts_init=ts + SECOND_NS,
                )
            )
    return made


HOLD_DEMO = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done and str(bar.bar_type.instrument_id) == "DEMO.XNAS":
            self.done = True
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=10_050.0)
"""
"""Buys 1,005 DEMO at 10 on its first bar and holds them."""


def _staged(universe: tuple[str, ...], source: bytes) -> RunRequest:
    """A daily request under a book that keeps the maintenance ratio."""
    fields = hypothesis(universe=universe).model_dump(by_alias=True, mode="json")
    fields["book"] = {"maintenance_pct": 30.0}
    subject = Hypothesis.model_validate(fields)
    return RunRequest(
        hyp=subject,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(subject),
        capital=CAPITAL,
        period="1d",
    )


def _placed(name: str, request: RunRequest, directory) -> Placement:
    cls, config = backtest._sleeve(request)
    return Placement(
        strategy_id=name,
        version=1,
        capital=CAPITAL,
        hyp=request.hyp,
        venue_model=VenueModel.model_validate(dict(request.venue_model)),
        snapshot_id=SNAPSHOT,
        period="1d",
        source=request.strategy_source,
        loaded=Loaded(
            manifest=None,  # type: ignore[arg-type]
            directory=directory / name,
            sleeve=Built(
                "sleeve",
                request.hyp.id,
                cls,
                node._reconfigured(config, order_id_tag=name[:3].upper()),
            ),
        ),
    )


def test_a_stage_version_whose_split_a_stage_mate_applied_is_read_in_one_count(tmp_path) -> None:
    """A paper stage over the 15th to the 17th: alpha holds DEMO and trades ZED on another
    venue, under a maintenance floor; beta trades OTHR on DEMO's venue. DEMO prints nothing on
    its ex-date, the 16th, and beta's OTHR bar is the first point of the venue past the split,
    so the venue applies it there and alpha holds 100 shares at the 16th's end, which ZED's
    bar strikes. Read in the stage venue's era they are worth 100 each, the book is worth
    what it was on the 15th, and the ratio counts 10,000 of stock; read in alpha's own, they
    were worth 10, 9,000 under the book, at a ratio of 90.99 where one count reads 10.00."""

    def demo(day: date) -> float | None:
        return None if day == REVERSE_EX else 10.0 if day < REVERSE_EX else 100.0

    points = [*_sessions(INSTRUMENT, demo), *_sessions(ZED, lambda _: 30.0)]
    points += _sessions("OTHR.XNAS", lambda _: 20.0)
    instruments = [_listed(INSTRUMENT, info=REVERSE), _listed(ZED), _listed("OTHR.XNAS")]
    stage = StageNode(
        stage="paper",
        capital=CAPITAL,
        limits=Limits(
            max_gross_pct=100, max_net_pct=100, per_strategy_max_pct=100, daily_loss_pct=50
        ),
        placements=(
            _placed("alpha", _staged((INSTRUMENT, ZED), HOLD_DEMO), tmp_path),
            _placed("beta", _staged(("OTHR.XNAS",), FLAT_SLEEVE), tmp_path),
        ),
        window=(date(2024, 1, 15), date(2024, 1, 17)),
        catalog=catalog(tmp_path / "catalog", points, instruments),
    )

    alpha = next(item.run for item in node.run(stage).realised if item.strategy_id == "alpha")

    assert [item.qty for item in alpha.adjustments] == [-905.0]
    cash = CAPITAL - 10_050.0 - alpha.fills[0].cost + 50.0 - sum(alpha.carry[:2])
    assert alpha.equity[1] == pytest.approx(alpha.equity[0])
    assert alpha.worst_ratio[1] == pytest.approx((cash + 10_000.0) / 10_000.0)


HALTS = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.done = False

    def on_bar(self, bar):
        self.seen += 1
        if not self.done and str(bar.bar_type.instrument_id) == "DEMO.XNAS":
            self.done = True
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=10_050.0)
        if self.seen == 10:
            raise RuntimeError("the stage halts here")
"""
"""Buys 1,005 DEMO at 10 on its first bar, and raises on its tenth, which halts the stage."""


def test_a_halted_stage_s_unreleased_points_apply_no_split(tmp_path) -> None:
    """A paper stage from the 2nd to the 20th: alpha holds 1,005 DEMO and trades OTHR on its
    venue, and raises on its tenth bar, the 6th's, so the node halts there and the venue never
    reaches DEMO's one-for-ten of the 16th. alpha is still marked over its whole view, which
    holds OTHR's bar of the 16th and DEMO's bars in the new shares after it; the venue applied
    nothing, so alpha holds 1,005 old shares at every end, worth 10 each — DEMO's 100s read back
    into the shares it still holds — and its equity is what it paid. Read off the view's own
    points, OTHR's unreleased bar applied the split from the 16th: the old shares were marked at
    100, a 90,450 gain, and the ratio counted 100,500 of stock."""

    def demo(day: date) -> float | None:
        return None if day == REVERSE_EX else 10.0 if day < REVERSE_EX else 100.0

    points = [*_sessions(INSTRUMENT, demo), *_sessions("OTHR.XNAS", lambda _: 20.0)]
    instruments = [_listed(INSTRUMENT, info=REVERSE), _listed("OTHR.XNAS")]
    stage = StageNode(
        stage="paper",
        capital=CAPITAL,
        limits=Limits(
            max_gross_pct=100, max_net_pct=100, per_strategy_max_pct=100, daily_loss_pct=50
        ),
        placements=(_placed("alpha", _staged((INSTRUMENT, "OTHR.XNAS"), HALTS), tmp_path),),
        window=(date(2024, 1, 2), date(2024, 1, 20)),
        catalog=catalog(tmp_path / "catalog", points, instruments),
    )

    ran = node.run(stage)
    alpha = ran.realised[0].run

    assert ran.halted is not None
    assert alpha.adjustments == ()
    ends = [end for end in alpha.period_ends_ns if end >= midnight_ns(REVERSE_EX)]
    assert len(ends) == 5
    held = {item.ts_ns: (item.qty, item.notional) for item in alpha.held}
    assert [held[end] for end in ends] == [(1_005.0, pytest.approx(10_050.0))] * len(ends)
    cash = CAPITAL - 10_050.0 - alpha.fills[0].cost
    for end in ends:
        index = alpha.period_ends_ns.index(end)
        assert alpha.worst_ratio[index] == pytest.approx(
            (cash - sum(alpha.carry[: index + 1]) + 10_050.0) / 10_050.0
        )


# --- two splits at one point ---------------------------------------------------------

TWO = {"splits": [{"ex_date": "2024-01-16", "ratio": 0.1}, {"ex_date": "2024-01-17", "ratio": 2.0}]}
"""A one-for-ten on the 16th and a two-for-one on the 17th: DEMO's venue prints nothing on the
16th, so the venue applies both at the 17th's first point."""

BOTH_NAMES = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = {}

    def on_bar(self, bar):
        key = str(bar.bar_type.instrument_id)
        self.seen[key] = self.seen.get(key, 0) + 1
        if key == "DEMO.XNAS" and self.seen[key] == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=10_050.0)
        if key == "OTHR.XNAS" and self.seen[key] == 17:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)
"""
"""Buys 1,005 DEMO on its first bar and holds them; buys OTHR on its bar of the 18th."""


def test_two_splits_applied_at_one_point_each_restate_by_their_own_ratio() -> None:
    """1,005 DEMO at 10 are 100 at 100 after the one-for-ten and 200 at 50 after the
    two-for-one, both applied at the 17th's first bar, so the book never moves and the fill
    path falls by the costs alone. Matched by their one instant, both adjustments read the
    two-for-one's ratio, DEMO's 10 was restated to 2.50 where the book held it at 50, and the
    path read a fall of 9.51% at OTHR's purchase."""
    subject = hypothesis(universe=(INSTRUMENT, "OTHR.XNAS"))
    made = RunRequest(
        hyp=subject,
        strategy_source=BOTH_NAMES,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(subject),
        capital=CAPITAL,
        period="1w",
    )
    quiet = date(2024, 1, 16)

    def demo(day: date) -> float | None:
        return None if day == quiet else 10.0 if day < quiet else 50.0

    groups = [
        tuple(_sessions(INSTRUMENT, demo)),
        tuple(_sessions("OTHR.XNAS", lambda day: None if day == quiet else 20.0)),
    ]

    card = execute(made, [instrument(info=TWO), instrument("OTHR")], groups).run

    assert [(item.qty, item.ratio) for item in card.adjustments] == [(-905.0, 0.1), (100.0, 2.0)]
    costs = sum(fill.cost for fill in card.fills) / CAPITAL * 100.0
    assert fill_path_drawdown_pct(card)[0] == pytest.approx(costs)
    assert drawdown_pct(card) == pytest.approx(costs)


# --- a fill at the instant a split was applied ----------------------------------------

ADDS = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        if str(bar.bar_type.instrument_id) != "DEMO.XNAS":
            return
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=10_050.0)
        if self.seen == 15:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)
"""
"""Buys 1,005 DEMO at 10 on its first bar, and 100 more on its bar of the 15th, the eve."""


def _latent(universe: tuple[str, ...], source: bytes, latency_ms: float) -> RunRequest:
    """A daily request over `universe` under a latency."""
    document = hypothesis(universe=universe).model_dump(mode="json")
    document["costs"] = {**document["costs"], "latency_ms": latency_ms}
    subject = Hypothesis.model_validate(document)
    return RunRequest(
        hyp=subject,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(subject),
        capital=CAPITAL,
        period="1d",
    )


def test_an_order_landing_at_the_split_s_instant_before_it_is_replayed_before_it() -> None:
    """DEMO's eve order is in flight under a 10 ms latency and lands at the next instant, the
    ex-date's 16:00:01, behind ABC's bar of another venue: the venue matches it there at the
    eve's 10, then DEMO's own bar applies the one-for-ten to all 1,105 shares, which become
    110. The book never falls but by its costs. Replayed with the split first, the path took
    995 shares from the 1,005 it held, then marked the 110 left at the add-on's 10 — 9.91% of
    the capital lost on a book that lost 0.0055%."""
    made = _latent((INSTRUMENT, "ABC.ARCX"), ADDS, 10.0)
    groups = [
        tuple(_sessions("ABC.ARCX", lambda _: 30.0)),
        tuple(_sessions(INSTRUMENT, lambda day: 10.0 if day < REVERSE_EX else 100.0)),
    ]

    card = execute(made, [_listed(INSTRUMENT, info=REVERSE), _listed("ABC.ARCX")], groups).run

    (split,) = card.adjustments
    added = card.fills[-1]
    assert (added.ts_ns, added.qty, added.px) == (split.ts_ns, 100.0, 10.0)
    assert (split.qty, split.fills_before) == (-995.0, 1)
    costs = sum(fill.cost for fill in card.fills) / CAPITAL * 100.0
    assert drawdown_pct(card) == pytest.approx(costs)
    assert fill_path_drawdown_pct(card)[0] == pytest.approx(costs)


RESTS = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        if str(bar.bar_type.instrument_id) != "DEMO.XNAS":
            return
        self.seen += 1
        if self.seen == 1 and HOLDS:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=10_050.0)
        if self.seen == 14:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100.0, price=9.0)
        if self.seen == 16 and not HOLDS:
            self.submit_entry(bar.bar_type.instrument_id, "SELL", qty=5.0)
"""
"""Rests a limit buy of 100 DEMO at 9 from its bar of the 14th, the eve's bar filling it;
holding 1,005 bought at 10 on its first bar, or, holding nothing, selling 5 on the bar of the
16th."""

PUBLISHED = midnight_ns(REVERSE_EX) + 1_800 * SECOND_NS
"""00:30 UTC on the ex-date, when DEMO's eve bar and OTHR's first bar of the ex-date are
both published."""


def _straddling() -> list[tuple[Bar, ...]]:
    """DEMO's sessions, the eve's printed at 16:00 on the 15th, published at 00:30 on the 16th
    and dipping to 8.90, and 100 in the new shares from the 16th; OTHR's, with a bar of the
    ex-date printed at 00:20 and published with the eve's, which applies the split."""
    demo = _sessions(INSTRUMENT, lambda day: 10.0 if day < REVERSE_EX else 100.0)
    eve = next(index for index, bar in enumerate(demo) if bar.ts_event > EVE_NS) - 1
    printed = demo[eve]
    demo[eve] = Bar(
        printed.bar_type,
        printed.open,
        printed.high,
        Price(8.90, 2),
        printed.close,
        printed.volume,
        ts_event=printed.ts_event,
        ts_init=PUBLISHED,
    )
    other = _sessions("OTHR.XNAS", lambda _: 20.0)
    early = midnight_ns(REVERSE_EX) + 1_200 * SECOND_NS
    applies = _delayed("OTHR", early, PUBLISHED, 20.0)
    other.insert(next(i for i, bar in enumerate(other) if bar.ts_init > PUBLISHED), applies)
    return [tuple(demo), tuple(other)]


@pytest.mark.parametrize("holds", [True, False], ids=["held through", "opened at the instant"])
def test_a_fill_an_earlier_point_of_the_instant_matched_is_replayed_before_its_split(
    holds: bool,
) -> None:
    """DEMO's eve bar and OTHR's 00:20 bar are both published at 00:30 on the ex-date, the eve's
    first: it fills the resting buy of 100 at 9 in the old shares, and OTHR's bar, printed past
    the split, then applies it. Held through, the path marks the 1,105 old shares at the 9 the
    fill struck, 1,005 and the costs under the capital, and the split makes them 110 new ones
    at 90 and 50 in lieu; opened there, the 100 bought at 9 are 10 at 90, and the path falls by
    the fill's cost alone. Replayed with the split first, the path marked the 110 at the old 9
    — 9.92% lost — or applied the split to nothing and kept 100 old shares, which the sale at
    100 on the 16th marked at 100 each, 9% lost on a book that never fell."""
    source = RESTS.replace(b"HOLDS", b"True" if holds else b"False")
    made = _latent((INSTRUMENT, "OTHR.XNAS"), source, 0.0)

    card = execute(
        made, [_listed(INSTRUMENT, info=REVERSE), _listed("OTHR.XNAS")], _straddling()
    ).run

    (split,) = card.adjustments
    rested = next(fill for fill in card.fills if fill.ts_ns == PUBLISHED)
    assert (split.ts_ns, rested.qty, rested.px) == (PUBLISHED, 100.0, 9.0)
    assert (split.qty, split.fills_before) == ((-995.0, 1) if holds else (-90.0, 1))
    paid = sum(fill.cost for fill in card.fills if fill.ts_ns <= PUBLISHED)
    fell = (1_005.0 if holds else 0.0) + paid
    assert fill_path_drawdown_pct(card) == (pytest.approx(fell / CAPITAL * 100.0), PUBLISHED)


def test_a_record_kept_for_a_split_alone_hears_the_bus_and_keeps_no_point(request_for) -> None:
    """Where a split fell among the fills is the order the bus carried them in, and nothing
    else of the record is read without a dated spread or a period target: the run keeps the
    record, and its venues load no part that is handed every point to keep each name's last.
    A run that declares no split keeps none."""
    from kanso.nautilus import actions
    from kanso.nautilus.struck import StruckPart

    made = request(request_for)
    model = VenueModel.model_validate(dict(made.venue_model))
    split, plain = Marks(made, model, [scheduled(0.1)]), Marks(made, model, [instrument()])

    assert split.record is not None
    assert split.record.points is False
    assert split.pricing is None
    loaded = {type(part) for part in actions.modules("XNAS", split.record)[0].parts}
    assert StruckPart not in loaded
    assert plain.record is None


class _Filled:
    """A fill event as the extraction reads one off a position."""

    def __init__(self, order: str, ts: int) -> None:
        self.id = f"F-{order}"
        self.ts_event = ts
        self.instrument_id = InstrumentId.from_str(INSTRUMENT)
        self.client_order_id = order
        self.trade_id = TradeId(f"T-{order}")


class _Split:
    """A split's adjustment event as the venue raised it."""

    def __init__(self, ts: int) -> None:
        self.id = "A-1"
        self.ts_init = ts
        self.instrument_id = InstrumentId.from_str(INSTRUMENT)
        self.quantity_change = -905.0
        self.pnl_change = None
        self.reason = "split 2024-01-16"


class _Held:
    def __init__(self, events: list[_Filled], adjustments: list[_Split]) -> None:
        self.events = events
        self.adjustments = adjustments


def test_the_fills_the_venue_matched_before_a_split_at_its_instant_come_first() -> None:
    """Two orders of DEMO fill at the instant its split is applied, O-9 before it and O-10
    after, as the bus carried them; ordered by their ids, O-10 came first and was counted
    among the fills before the split. The record puts O-9 first and counts it alone, and
    without a record both are ordered as they always were and the split before them."""
    from kanso.nautilus.backtest import _adjusted, _fill_events
    from kanso.nautilus.struck import Struck

    instant = at(REVERSE_EX)
    before, after, split = _Filled("O-9", instant), _Filled("O-10", instant), _Split(instant)
    held = [_Held([before, after], [split])]
    heard = Struck()
    heard.filled(before)
    heard.restated(type("Restated", (), {"adjustments": (split,)})())
    heard.filled(after)

    ordered, _ = _fill_events(held, heard)
    unheard, _ = _fill_events(held)

    assert [event.client_order_id for event in ordered] == ["O-9", "O-10"]
    assert [event.client_order_id for event in unheard] == ["O-10", "O-9"]
    assert [item.fills_before for item in _adjusted(held, {}, heard, ordered)] == [1]
    assert [item.fills_before for item in _adjusted(held, {}, None, unheard)] == [0]
