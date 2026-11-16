"""`entry_timing`: the subject's trades against the same trades circularly shifted in time.

The gate is arithmetic on a run and on the prices `SessionPrices` holds, so it is tested here
on hand-built sessions whose readings a reader can check by eye, and on the `synthetic`
loader's bars where the claim is about a whole series. Every price is a close at 16:00 New
York unless a test says otherwise, so a fill struck at a close is one the null can reprice
exactly; the certification runner's own reading of a real window is tested beside
certification (`tests/certify/test_run.py`).
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kanso.criteria import CardRun, Fill, Trade, gates
from kanso.criteria.gates import entry_timing
from kanso.criteria.gates.entry_timing import (
    ATTACHED,
    FEW_TRADES,
    NO_CHOICE,
    NO_PRICES,
    TOO_SHORT,
    UNPLACED,
    UNPRICED,
    gate,
)
from kanso.criteria.run import BPS, NS_PER_SECOND, day_of
from kanso.criteria.timing import (
    BAR,
    DAY_NS,
    PRINT,
    QUOTE,
    Fold,
    Reading,
    SessionPrices,
    draw,
    instant_at,
    keys_of,
    offsets,
    place,
    runner_bps_per_trade,
    wall_of,
)
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.nautilus.costs import at_touch, dated_half_spread, fill_cost, fixed_half_spread
from kanso.nautilus.splits import Split
from tests.criteria.builders import build_run, context

NAME = "X.XNAS"
OTHER = "Y.XNAS"
CAPITAL = 100_000.0
HOUR = 3_600 * NS_PER_SECOND
CLOSE = 16 * HOUR
"""The New York time of day every close here is struck at."""

FIRST = date(2025, 3, 3)
"""A Monday; New York's clocks change on the Sunday after it, so the grid spans a change."""

COSTS: dict[str, Any] = {
    "commission_bps": 0.5,
    "slippage_bps": 1.0,
    "spread": "fixed_bps",
    "fixed_bps": 2.0,
}
MODEL: dict[str, Any] = {"costs": COSTS}


def weekdays(count: int, start: date = FIRST) -> list[date]:
    """`count` weekdays from `start`, in order: the sessions a US listing trades on."""
    found: list[date] = []
    day = start
    while len(found) < count:
        if day.weekday() < 5:
            found.append(day)
        day += timedelta(days=1)
    return found


def folded(
    closes: list[float],
    days: list[date],
    *,
    name: str = NAME,
    keys: tuple[tuple[str, int], ...] = ((NAME, CLOSE),),
    schedules: dict[str, tuple[Split, ...]] | None = None,
    quotes: list[tuple[float, float]] | None = None,
) -> SessionPrices:
    """The readings of one close a session at 16:00, and of a quote there where given."""
    fold = Fold(keys, (days[0], days[-1]), schedules)
    for index, (day, close) in enumerate(zip(days, closes, strict=True)):
        at = instant_at(day, CLOSE)
        fold.take(BAR, name, at, close, close)
        if quotes is not None:
            bid, ask = quotes[index]
            fold.take(QUOTE, name, at, bid, ask)
    return fold.prices()


def a_fill(
    day: date,
    side: str,
    qty: float,
    px: float,
    *,
    name: str = NAME,
    maker: bool = False,
    touched: bool = False,
    model: dict[str, Any] | None = None,
    wall: int = CLOSE,
) -> Fill:
    """One fill at a close, charged exactly as the runner charges one (`backtest._fill`)."""
    costs = (model or MODEL)["costs"]
    notional = qty * px * 1.0
    return Fill(
        ts_ns=instant_at(day, wall),
        instrument_id=name,
        side=side,
        qty=qty,
        px=px,
        cost=fill_cost(
            notional,
            qty,
            costs.get("commission_bps", 0.0),
            costs.get("slippage_bps", 0.0),
            fixed_half_spread(costs.get("fixed_bps")),
            costs.get("maker_bps"),
            costs.get("commission_per_share", 0.0),
            maker=maker,
            sell=side == "SELL",
            touched=touched,
        ),
        maker=maker,
        touched=touched,
    )


def a_trade(*fills: Fill) -> Trade:
    """A closed trade made of these fills, netted as the runner nets a long or a short."""
    opened = fills[0]
    sign = 1.0 if opened.side == "BUY" else -1.0
    cash = sum((f.qty * f.px if f.side == "SELL" else -f.qty * f.px) for f in fills)
    bought = [f for f in fills if f.side == opened.side]
    qty = sum(f.qty for f in bought)
    return Trade(
        opened_ns=min(f.ts_ns for f in fills),
        closed_ns=max(f.ts_ns for f in fills),
        instrument_id=opened.instrument_id,
        qty=sign * qty,
        avg_open=sum(f.qty * f.px for f in bought) / qty,
        avg_close=0.0,
        pnl_net=cash - sum(f.cost for f in fills),
        cost=sum(f.cost for f in fills),
        fills=fills,
    )


def round_trip(
    days: list[date], closes: list[float], opens: int, closes_at: int, qty: float = 100.0
) -> Trade:
    """A long bought at one session's close and sold at another's."""
    return a_trade(
        a_fill(days[opens], "BUY", qty, closes[opens]),
        a_fill(days[closes_at], "SELL", qty, closes[closes_at]),
    )


def a_run(trades: list[Trade], days: list[date], model: dict[str, Any] | None = None) -> CardRun:
    """A run over the sessions holding these trades, costed under `model`."""
    span = (days[-1] - days[0]).days + 1
    run = build_run((0.0,) * span, start=days[0], trades=tuple(trades), capital=CAPITAL)
    return replace(run, venue_model=model or MODEL)


def judged(run: CardRun, prices: SessionPrices | None, max_p: float | None = 0.05, **more: Any):
    """The gate's verdict on a certification context handing it these prices."""
    params = {} if max_p is None else {"max_p": max_p}
    reader = None if prices is None else (lambda keys: prices)
    return gate.evaluate(context(run, stage="cert", params=params, session_prices=reader, **more))


# --- the clock --------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2025, 3, 7), date(2025, 3, 10), date(2025, 11, 3)])
@pytest.mark.parametrize("wall", [1, 9 * HOUR + 31 * 60 * NS_PER_SECOND, CLOSE, DAY_NS])
def test_a_time_of_day_reads_the_same_on_either_side_of_a_change_of_clocks(
    day: date, wall: int
) -> None:
    at = instant_at(day, wall)

    assert wall_of(at) == (day, wall)
    local = datetime.fromtimestamp(at // NS_PER_SECOND, tz=UTC).astimezone()
    assert local is not None


def test_a_close_is_struck_at_its_wall_clock_time_whatever_the_offset() -> None:
    """16:00 New York is 21:00Z before the March change and 20:00Z after it, and a daily bar
    stamped at the next New York midnight belongs to its own session, at the day's end."""
    friday, monday = date(2025, 3, 7), date(2025, 3, 10)

    assert datetime.fromtimestamp(instant_at(friday, CLOSE) // NS_PER_SECOND, tz=UTC).hour == 21
    assert datetime.fromtimestamp(instant_at(monday, CLOSE) // NS_PER_SECOND, tz=UTC).hour == 20
    assert wall_of(instant_at(friday, DAY_NS)) == (friday, DAY_NS)
    assert instant_at(friday, DAY_NS) == instant_at(date(2025, 3, 8), 0)


# --- the readings -----------------------------------------------------------------


def test_a_reading_is_struck_at_the_side_it_trades() -> None:
    quote = Reading(1, 9.98, 10.02)

    assert (quote.price(sell=True), quote.price(sell=False), quote.quote) == (
        9.98,
        10.02,
        quote.quote,
    )


def test_the_fold_keeps_each_session_s_last_reading_at_or_before_each_key() -> None:
    """A bar after the key's time is not read at it; an earlier session's is never carried into
    a mark; a quote is, the last of a session's evening among them, as the runner carries one; a
    point arriving after a later one keeps it;
    the prefix and the evening before the window's first date belong to no session; a name
    nobody traded still names the sessions."""
    days = weekdays(3)
    early, late = 10 * HOUR, CLOSE
    fold = Fold(((NAME, early), (NAME, late)), (days[1], days[2]))
    evening = instant_at(days[0], 19 * HOUR + 30 * 60 * NS_PER_SECOND)
    for outside in (instant_at(days[0], early), evening):
        fold.take(BAR, NAME, outside, 1.0, 1.0)
    for price, wall in ((10.0, early - 60 * NS_PER_SECOND), (11.0, early + 1), (12.0, late)):
        fold.take(BAR, NAME, instant_at(days[1], wall), price, price)
    fold.take(BAR, NAME, instant_at(days[1], early - 120 * NS_PER_SECOND), 99.0, 99.0)
    fold.take(QUOTE, NAME, instant_at(days[1], early), 9.9, 10.1)
    fold.take(QUOTE, NAME, instant_at(days[1], late + HOUR), 9.8, 10.2)
    fold.take(BAR, OTHER, instant_at(days[2], CLOSE), 5.0, 5.0)

    prices = fold.prices()

    assert day_of(evening) == days[1], "UTC already counts the evening as the next date"
    assert prices.sessions == (days[1], days[2])
    assert prices.source == {NAME: BAR}
    assert [r and r.ask for r in prices.marks[(NAME, early)]] == [10.0, None]
    assert [r and r.ask for r in prices.marks[(NAME, late)]] == [12.0, None]
    assert [r and r.quote for r in prices.quotes[(NAME, early)]] == [(9.9, 10.1), (9.8, 10.2)]
    assert [r and r.quote for r in prices.quotes[(NAME, late)]] == [(9.9, 10.1), (9.8, 10.2)]
    assert (NAME, early) not in Fold(((NAME, early),), (days[0], days[0])).prices().marks


@pytest.mark.parametrize(
    ("kinds", "source"),
    [((BAR, PRINT, QUOTE), BAR), ((PRINT, QUOTE), PRINT), ((QUOTE,), QUOTE)],
)
def test_a_name_is_priced_from_its_bars_else_its_prints_else_its_quotes(
    kinds: tuple[str, ...], source: str
) -> None:
    day = weekdays(1)[0]
    fold = Fold(((NAME, CLOSE),), (day, day))
    for kind in kinds:
        fold.take(kind, NAME, instant_at(day, CLOSE), 9.0, 11.0)

    assert fold.prices().source == {NAME: source}


# --- the grid and the offsets -----------------------------------------------------


def test_the_offsets_are_every_shift_that_moves_each_trade_off_itself() -> None:
    days = weekdays(20)
    closes = [10.0] * 20
    prices = folded(closes, days)
    placed = [place(round_trip(days, closes, i, i + hold), prices) for i, hold in ((0, 1), (5, 3))]

    floor, shifts = offsets(placed, len(prices.sessions))

    assert floor == 4, "the longest hold, three sessions, plus one"
    assert list(shifts) == list(range(4, 17))
    assert len(shifts) == 20 - 2 * floor + 1


def test_offset_zero_reprices_the_subject_s_own_fills_as_the_runner_charged_them() -> None:
    days = weekdays(10)
    closes = [10.0 + 0.1 * i for i in range(10)]
    trades = [round_trip(days, closes, 1, 3), round_trip(days, closes, 5, 8, qty=37.0)]
    run = a_run(trades, days)
    prices = folded(closes, days)

    own = draw([place(t, prices) for t in trades], prices, MODEL, 0)

    assert own.costs == tuple(tuple(f.cost for f in t.fills) for t in trades), "to the last bit"
    assert own.bps_per_trade(CAPITAL) == pytest.approx(runner_bps_per_trade(run))
    assert own.dropped == 0


def test_a_trade_whose_shift_crosses_the_window_s_end_is_dropped_and_counted() -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    prices = folded(closes, days)
    placed = [place(round_trip(days, closes, 6, 8), prices)]

    assert draw(placed, prices, MODEL, 2).dropped == 1, "sessions 8 to 10 cross the end"
    assert draw(placed, prices, MODEL, 4).kept == 1, "sessions 0 to 2, wrapped whole"


def test_a_trade_with_a_fill_on_no_session_is_dropped_from_every_draw() -> None:
    days = weekdays(6)
    closes = [10.0] * 6
    prices = folded(closes[:4], days[:4])
    trade = round_trip(days, closes, 1, 5)

    placed = place(trade, prices)

    assert placed.legs == ()
    assert draw([placed], prices, MODEL, 0).dropped == 1


def test_a_leg_with_no_price_on_the_session_it_lands_on_is_dropped() -> None:
    days = weekdays(6)
    closes = [10.0] * 6
    prices = folded(closes, days)
    gapped = replace(
        prices, marks={key: (*marks[:3], None, *marks[4:]) for key, marks in prices.marks.items()}
    )
    placed = [place(round_trip(days, closes, 0, 1), gapped)]

    assert draw(placed, gapped, MODEL, 1).dropped == 0
    assert draw(placed, gapped, MODEL, 2).dropped == 1, "its exit lands on the unpriced session"
    assert draw(placed, gapped, MODEL, 3).dropped == 1, "and here its entry does"


def test_a_price_of_zero_cannot_size_a_moved_trade() -> None:
    days = weekdays(4)
    closes = [10.0, 10.0, 0.0, 10.0]
    prices = folded(closes, days)

    assert draw([place(round_trip(days, closes, 0, 1), prices)], prices, MODEL, 2).dropped == 1


# --- what a moved trade is ------------------------------------------------------


def test_a_trade_opened_by_two_fills_is_scaled_by_one_factor_and_closes_flat() -> None:
    days = weekdays(12)
    closes = [10.0, 12.0, 15.0, 9.0, 11.0, 13.0, 10.0, 8.0, 14.0, 10.0, 10.0, 10.0]
    trade = a_trade(
        a_fill(days[0], "BUY", 100.0, closes[0]),
        a_fill(days[1], "BUY", 50.0, closes[1]),
        a_fill(days[2], "SELL", 150.0, closes[2]),
    )
    prices = folded(closes, days)
    placed = place(trade, prices)

    moved = draw([placed], prices, {"costs": {}}, 6)

    assert placed.notional == 100.0 * 10.0 + 50.0 * 12.0
    scale = placed.notional / (100.0 * closes[6] + 50.0 * closes[7])
    bought, sold = 150.0 * scale, 150.0 * scale
    assert bought == pytest.approx(sold), "one factor for every fill, so it closes flat"
    expected = sold * closes[8] - (100.0 * scale * closes[6] + 50.0 * scale * closes[7])
    assert moved.pnl == (pytest.approx(expected),)


def test_a_draw_across_a_reverse_split_is_restated_and_moves_nothing() -> None:
    """A one-for-five turns a 10.00 share into a 50.00 one; a trade moved across it buys the
    one and sells the other, and restated onto its opening basis it made nothing but its
    costs."""
    days = weekdays(10)
    split = Split(days[6], 0.2, "America/New_York")
    closes = [10.0] * 6 + [50.0] * 4
    prices = folded(closes, days, schedules={NAME: (split,)})
    trade = round_trip(days, closes, 1, 3)

    moved = draw([place(trade, prices)], prices, MODEL, 4)

    assert moved.kept == 1
    assert moved.pnl[0] == pytest.approx(-sum(moved.costs[0]))
    assert moved.costs[0][1] == pytest.approx(trade.fills[1].cost), "the same notional sold"


def test_a_trade_the_runner_held_through_a_split_is_restated_onto_the_shares_it_opened_in() -> None:
    days = weekdays(6)
    split = Split(days[3], 0.2, "America/New_York")
    closes = [10.0, 10.0, 10.0, 50.0, 50.0, 50.0]
    prices = folded(closes, days, schedules={NAME: (split,)})
    trade = a_trade(a_fill(days[1], "BUY", 100.0, 10.0), a_fill(days[4], "SELL", 20.0, 50.0))

    placed = place(trade, prices)
    own = draw([placed], prices, MODEL, 0)

    assert [leg.qty for leg in placed.legs] == [100.0, pytest.approx(100.0)]
    assert [leg.px for leg in placed.legs] == [10.0, pytest.approx(10.0)]
    assert own.costs[0] == tuple(f.cost for f in trade.fills)


def test_a_dated_spread_is_read_at_the_drawn_instant() -> None:
    days = weekdays(10)
    row = {"pre": 1.0, "open": 1.0, "rth": 2.0, "post": 1.0, "n_sessions": 20, "n_open": 20}
    model = {
        "costs": {
            "commission_bps": 0.0,
            "slippage_bps": 0.0,
            "spread": "dated",
            "dated": [
                {"instrument": NAME, "from": days[0].isoformat(), **row},
                {"instrument": NAME, "from": days[5].isoformat(), **row, "rth": 30.0},
            ],
        }
    }
    closes = [10.0] * 10
    prices = folded(closes, days)
    placed = [place(round_trip(days, closes, 0, 1), prices)]

    early, late = draw(placed, prices, model, 0), draw(placed, prices, model, 6)

    def half(day: date) -> float:
        from kanso.nautilus.costs import dated_rows

        return dated_half_spread(dated_rows(model), NAME, instant_at(day, CLOSE))

    assert half(days[0]) == 1.0 / BPS and half(days[6]) == 15.0 / BPS
    assert early.costs[0][0] == pytest.approx(1_000.0 * half(days[0]))
    assert late.costs[0][0] == pytest.approx(1_000.0 * half(days[6]))


@pytest.mark.parametrize(
    ("quote", "touched"), [((9.99, 10.01), False), ((9.98, 10.0), True)], ids=["inside", "touch"]
)
def test_a_maker_or_touched_fill_is_repriced_as_struck_at_the_close(
    quote: tuple[float, float], touched: bool
) -> None:
    """A moved fill never rested on a book, so it is never a maker's, and whether a close is at
    the touch is read off the quote at the drawn instant, at offset 0 and in every draw."""
    days = weekdays(8)
    closes = [10.0] * 8
    model = {"costs": {**COSTS, "maker_bps": -0.2, "spread": "quotes"}}
    prices = folded(closes, days, quotes=[quote] * 8)
    trade = a_trade(
        a_fill(days[0], "BUY", 100.0, 10.0, maker=True, model=model),
        a_fill(days[1], "SELL", 100.0, 10.0, touched=True, model=model),
    )
    placed = [place(trade, prices)]

    def charged(sell: bool) -> float:
        half = (quote[1] - quote[0]) / ((quote[0] + quote[1]) / 2.0) / 2.0
        return fill_cost(
            1_000.0,
            100.0,
            0.5,
            1.0,
            half,
            -0.2,
            0.0,
            maker=False,
            sell=sell,
            touched=at_touch(10.0, sell=sell, quote=quote),
        )

    for k in (0, 3):
        assert draw(placed, prices, model, k).costs == ((charged(False), charged(True)),)
    assert at_touch(10.0, sell=False, quote=quote) is touched


def test_a_name_with_quotes_alone_is_struck_at_their_touch() -> None:
    days = weekdays(4)
    fold = Fold(((NAME, CLOSE),), (days[0], days[-1]))
    quoted = [(9.9, 10.1), (10.9, 11.1), (9.9, 10.1), (9.9, 10.1)]
    for day, (bid, ask) in zip(days, quoted, strict=True):
        fold.take(QUOTE, NAME, instant_at(day, CLOSE), bid, ask)
    prices = fold.prices()
    trade = a_trade(a_fill(days[0], "BUY", 100.0, 10.1), a_fill(days[1], "SELL", 100.0, 10.9))

    own = draw([place(trade, prices)], prices, {"costs": {}}, 0)

    assert own.pnl == (pytest.approx(100.0 * (10.9 - 10.1)),)


# --- the gate -----------------------------------------------------------------------


def test_the_toolbox_entry_resolves_to_this_gate() -> None:
    assert gates()["entry_timing"] is gate is entry_timing.gate


@pytest.mark.parametrize(
    ("max_p", "more", "reason"),
    [(None, {}, NO_CHOICE), (0.05, {"host_run": "host"}, ATTACHED), (0.05, {"few": 1}, FEW_TRADES)],
)
def test_the_gate_judges_nothing_it_was_not_handed(
    max_p: float | None, more: dict[str, Any], reason: str
) -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    trades = [round_trip(days, closes, 0, 1), round_trip(days, closes, 3, 4)]
    few = more.pop("few", None)
    run = a_run(trades[:few] if few else trades, days)
    if "host_run" in more:
        more["host_run"] = run

    result = judged(run, folded(closes, days), max_p, **more)

    assert (result.passed, result.skipped) == (True, reason)


def test_without_a_reader_of_the_window_the_gate_judges_nothing() -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    run = a_run([round_trip(days, closes, 0, 1), round_trip(days, closes, 3, 4)], days)

    assert judged(run, None).skipped == NO_PRICES


def test_a_traded_instrument_with_no_price_series_fails_and_does_not_pass() -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    run = a_run([round_trip(days, closes, 0, 1), round_trip(days, closes, 3, 4)], days)

    result = judged(run, SessionPrices(tuple(days)))

    assert (result.passed, result.skipped) == (False, None)
    assert result.evidence["unpriced"] == [NAME]
    assert result.evidence["reason"] == UNPRICED


def test_a_subject_none_of_whose_trades_can_be_priced_at_its_own_instants_fails() -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    run = a_run([round_trip(days, closes, 0, 1), round_trip(days, closes, 3, 4)], days)
    prices = folded(closes, days)
    blank = replace(prices, marks={key: (None,) * 10 for key in prices.marks})

    result = judged(run, blank)

    assert (result.passed, result.evidence["reason"]) == (False, UNPLACED)


def test_a_hold_longer_than_half_the_window_fails_with_no_draw() -> None:
    days = weekdays(10)
    closes = [10.0 + i for i in range(10)]
    run = a_run([round_trip(days, closes, 0, 5), round_trip(days, closes, 6, 7)], days)

    result = judged(run, folded(closes, days))

    assert (result.passed, result.skipped) == (False, None)
    assert (result.evidence["draws"], result.evidence["min_offset"]) == (0, 6)
    assert result.evidence["reason"] == TOO_SHORT


def sawtooth(seed: int, cycles: int) -> tuple[list[float], list[int]]:
    """A saw-tooth whose falls last two to five sessions, drawn from `seed`: each trough at
    10.00 rises to an 11.00 peak in two sessions; and the troughs, in order."""
    draws = random.Random(seed)
    closes: list[float] = []
    troughs: list[int] = []
    for _ in range(cycles):
        troughs.append(len(closes))
        fall = draws.randint(2, 5)
        closes.extend([10.0, 10.5, 11.0])
        closes.extend(11.0 - (step + 1) / fall for step in range(fall - 1))
    return closes, troughs


def test_a_book_that_buys_an_irregular_saw_tooth_s_troughs_has_the_smallest_p_there_is() -> None:
    closes, troughs = sawtooth(7, 12)
    days = weekdays(len(closes))
    prices = folded(closes, days)
    trades = [round_trip(days, closes, t, t + 2) for t in troughs[1:-1]]
    run = a_run(trades, days)

    result = judged(run, prices)

    assert result.passed
    assert result.evidence["p"] == result.evidence["min_attainable_p"]
    assert result.evidence["p"] == 1 / (1 + result.evidence["draws"])
    assert result.evidence["observed_bps_per_trade"] == pytest.approx(
        result.evidence["observed_runner_bps_per_trade"]
    )
    assert result.evidence["null_max"] < result.evidence["observed_bps_per_trade"]


def test_a_periodic_series_cannot_tell_timing_from_a_whole_cycle_later() -> None:
    """Every shift by whole cycles of a series that repeats exactly lands each trade on a trough,
    earns exactly what the subject did, and counts against it."""
    closes = [10.0, 10.5, 11.0, 10.5] * 6
    days = weekdays(len(closes))
    trades = [round_trip(days, closes, t, t + 2) for t in range(4, 21, 4)]

    result = judged(a_run(trades, days), folded(closes, days), max_p=0.25)

    assert not result.passed
    assert result.evidence["null_max"] == result.evidence["observed_bps_per_trade"]
    assert result.evidence["p"] == pytest.approx((1 + 5) / (1 + 19))


def test_a_draw_that_keeps_no_trade_counts_against_the_subject() -> None:
    """A draw the null could not price cannot be shown to be beaten."""
    days = weekdays(10)
    closes = [10.0] * 10
    run = a_run([round_trip(days, closes, 0, 1), round_trip(days, closes, 4, 5)], days)
    prices = folded(closes, days)
    own = {
        key: tuple(m if i in (0, 1, 4, 5) else None for i, m in enumerate(marks))
        for key, marks in prices.marks.items()
    }

    result = judged(run, replace(prices, marks=own), max_p=0.25)

    assert result.evidence["empty_draws"] == result.evidence["draws"] - 2
    assert result.evidence["p"] > 0.25


def test_every_draw_empty_leaves_the_null_unmeasured() -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    run = a_run([round_trip(days, closes, 0, 1), round_trip(days, closes, 1, 2)], days)
    prices = folded(closes, days)
    own = {
        key: tuple(m if i < 3 else None for i, m in enumerate(marks))
        for key, marks in prices.marks.items()
    }

    result = judged(run, replace(prices, marks=own))

    assert result.evidence["empty_draws"] == result.evidence["draws"]
    assert result.evidence["null_mean"] is None and result.evidence["p"] == 1.0


def test_the_evidence_reproduces_bit_for_bit_in_any_order() -> None:
    closes, troughs = sawtooth(3, 10)
    days = weekdays(len(closes))
    trades = [round_trip(days, closes, t, t + 3) for t in troughs[:-1]]
    prices = folded(closes, days)

    first = judged(a_run(trades, days), prices)
    again = judged(a_run(trades, days), prices)
    reversed_ = judged(a_run(trades[::-1], days), prices)

    assert first.evidence == again.evidence == reversed_.evidence
    assert keys_of(a_run(trades, days)) == ((NAME, CLOSE),)


def rising_book(seed: int) -> tuple[CardRun, SessionPrices]:
    """Long a drifting random walk the synthetic loader draws from `seed`, at random times."""
    spec = {
        "loader": "synthetic",
        "model": "gbm",
        "seed": seed,
        "instruments": ["UP"],
        "venue": "XNAS",
        "resolution": "30m",
        "types": ["bar"],
        "start": "2025-01-06",
        "end": "2025-09-30",
        "mu_bps": 3.0,
        "sigma_bps": 15.0,
    }
    loader = SyntheticLoader()
    (ref,) = loader.discover(spec)
    loaded = list(loader.load(ref, ref.span))
    name = "UP.XNAS"
    fold = Fold(((name, CLOSE),), ref.span)
    for bar in loaded:
        close = float(bar.close)
        fold.take(BAR, name, int(bar.ts_event), close, close)
    prices = fold.prices()
    days = list(prices.sessions)
    marks = prices.marks[(name, CLOSE)]
    draws = random.Random(seed)
    trades: list[Trade] = []
    at = draws.randint(0, 5)
    while at + 3 < len(days):
        held = draws.randint(1, 3)
        entry, exit_ = marks[at], marks[at + held]
        assert entry is not None and exit_ is not None
        trades.append(
            a_trade(
                a_fill(days[at], "BUY", 100.0, entry.ask, name=name),
                a_fill(days[at + held], "SELL", 100.0, exit_.ask, name=name),
            )
        )
        at += held + draws.randint(1, 12)
    return a_run(trades, days), prices


def test_a_book_long_a_rising_series_at_arbitrary_times_is_not_credited_with_timing() -> None:
    """Long a drift at random times earns the drift, and so does every shift of it: p lands
    anywhere, and at 0.05 the gate refuses it in nearly every seed."""
    found = []
    for seed in range(10):
        run, prices = rising_book(seed)
        result = judged(run, prices)
        assert result.evidence["observed_runner_bps_per_trade"] > 0, "the drift pays"
        found.append((result.passed, result.evidence["p"]))

    assert sum(passed for passed, _ in found) <= 2
    assert len({p for _, p in found}) > 5, "p is spread, not pinned at either end"


def test_the_window_is_read_once_for_every_key_the_trades_need() -> None:
    days = weekdays(10)
    closes = [10.0] * 10
    trades = [
        round_trip(days, closes, 0, 1),
        a_trade(
            a_fill(days[3], "BUY", 10.0, 10.0, wall=10 * HOUR),
            a_fill(days[4], "SELL", 10.0, 10.0, wall=10 * HOUR),
        ),
    ]
    asked: list[tuple[tuple[str, int], ...]] = []

    def reader(keys: Any) -> SessionPrices:
        asked.append(tuple(keys))
        return SessionPrices(tuple(days))

    gate.evaluate(
        context(a_run(trades, days), stage="cert", params={"max_p": 0.1}, session_prices=reader)
    )

    assert asked == [((NAME, 10 * HOUR), (NAME, CLOSE))]


def test_a_fill_is_placed_where_its_price_was_struck() -> None:
    """Under a latency a fill lands after the point its price was read off; the null moves the
    price's instant, not the fill's."""
    days = weekdays(4)
    fill = replace(a_fill(days[1], "BUY", 1.0, 10.0), price_ts_ns=instant_at(days[0], CLOSE))
    trade = a_trade(fill, a_fill(days[2], "SELL", 1.0, 10.0))

    assert keys_of(a_run([trade], days)) == ((NAME, CLOSE),)
    assert place(trade, folded([10.0] * 4, days)).first == 0
