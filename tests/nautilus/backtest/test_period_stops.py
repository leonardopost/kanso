"""What a period target did, recorded on the run, on every path the harness runs on.

The harness records each week it trades in, each stop as it makes it — the week's opening,
anchor and threshold, the hit's instant, part of the session and `balance`, what the stopped
week dropped unsent, and every fill of an order sent while stopped with the instant and the
bar its price was struck at — and the week open at the run's last point
(`KansoStrategy._target_record`). The runner reads it once the run has ended and carries it on
the run it extracts (`kanso.criteria.run.CardRun.period_stops`): a card, a replay on the live
code path and a stage window record the same thing for the same points, and a stage window's
own flatten, which is the stage's, is no stop's. The fixtures are the period target's own
(`tests/nautilus/strategy/test_period_target.py`): two names of one-minute bars stamped at New
York wall times in January 2024, the weeks opening on Monday at 04:00, a target of 500 on a
100,000 capital, and every P&L a difference of prices by hand; the bars here carry a volume of
their own, so the bar a fill's price was read at is told by its volume.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.codec import encode_run
from kanso.nautilus import node, session
from kanso.nautilus.backtest import RunResult, execute
from tests.nautilus.strategy.test_period_target import (
    FRIDAY,
    LAGG,
    LAGG_ID,
    LEAD,
    LEAD_ID,
    MONDAY,
    NEXT_MONDAY,
    Seen,
    _two_weeks,
    card,
    ny,
    request,
)

from .conftest import VENUE, catalog, instrument
from .test_dated_stage import _placed, _stage
from .test_period_target import COSTS, _pair

FIRST_MONDAY = date(2024, 1, 1)


def dated(*names: str) -> dict[str, object]:
    """A dated spread on these names: two basis points a fill, half the four-point width every
    part of every session states, small enough that the pair still reaches its target at the
    09:33 close, and charged at the instant the venue struck each fill's price at
    (`kanso.nautilus.struck`)."""
    row = {"from": "2023-01-01", "pre": 4.0, "open": 4.0, "rth": 4.0, "post": 4.0}
    return {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "spread": "dated",
        "dated": [{"instrument": name, **row, "n_sessions": 5, "n_open": 25} for name in names],
    }


Point = tuple[int, float] | tuple[int, float, float, float, float]


def volume_of(symbol: str, index: int) -> int:
    """A volume no other bar of either name carries: LEAD's nth bar trades n million shares,
    LAGG's fifty million more — enough that the venue fills a thousand on any of them."""
    return 1_000_000 * (index + 1) + (0 if symbol == LEAD else 50_000_000)


def volumed(symbol: str, points: Sequence[Point]) -> tuple[Bar, ...]:
    """One name's one-minute bars, each stamped and published at its close, with the volume
    `volume_of` gives it."""
    kind = BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for index, (ts, *prices) in enumerate(points):
        ohlc = [Price(price, 2) for price in (prices * 4 if len(prices) == 1 else prices)]
        volume = Quantity.from_int(volume_of(symbol, index))
        made.append(Bar(kind, *ohlc, volume, ts, ts))
    return tuple(made)


def ran(
    plan: Mapping[tuple[int, str], Sequence[tuple[str, str, str]]],
    names: Mapping[str, Sequence[Point]],
    tmp_path: Path,
    *,
    order: Sequence[str] = (LEAD, LAGG),
    **options: Any,
) -> RunResult:
    """The plan's card over these names' bars, handed in `order`, each bar with its volume."""
    made = request(plan, tmp_path / f"seen-{len(list(tmp_path.iterdir()))}.txt", **options)
    held = [symbol for symbol in order if f"{symbol}.{VENUE}" in made.hyp.universe]
    return execute(made, [instrument(s) for s in held], [volumed(s, names[s]) for s in held])


@pytest.mark.parametrize(
    ("of", "hit", "threshold", "sold", "flat", "next_threshold"),
    [
        ("capital", "09:33", 500.0, (10.51, "09:34"), 100_910.0, 500.0),
        ("period_open", "09:34", 502.0, (10.60, "09:35"), 101_000.0, 505.0),
    ],
)
def test_a_card_records_each_stop_as_the_harness_made_it(
    of: str,
    hit: str,
    threshold: float,
    sold: tuple[float, str],
    flat: float,
    next_threshold: float,
    tmp_path: Path,
) -> None:
    """Week one makes 400, week two buys LEAD a thousand at 10.00 and rises through the target:
    the stop is week two's, read against the 100,400 week one closed at, at the close the
    target is first reached at and the `balance` the sleeve read there; it records the four
    orders the stopped week dropped at 09:36 — the entry, the order built by hand and both of
    the list — and the flatten's one fill, at the next close, priced at that bar. Week three's
    entry goes; it is the week open at the last point, anchored where week two ended."""
    lead, lagg, plan = _two_weeks()

    result, seen = card(plan, lead, lagg, tmp_path, of=of)

    record = result.run.period_stops
    assert record is not None
    assert record.openings == [ny(day, "04:00") for day in (FIRST_MONDAY, MONDAY, NEXT_MONDAY)]
    assert (record.stopped, record.periods) == (1, 3)
    (stop,) = record.stops
    at_hit = next(row for row in seen if row.stopped)
    assert (stop.opening_ns, stop.anchor, stop.hit_ns, stop.segment) == (
        ny(MONDAY, "04:00"),
        100_400.0,
        ny(MONDAY, hit),
        "rth",
    )
    assert stop.threshold == pytest.approx(threshold)
    assert (stop.hit_ns, stop.equity) == (at_hit.ts, at_hit.balance)
    assert stop.dropped == 4 == seen[-1].dropped
    price, stamp = sold
    assert [fill.model_dump() for fill in stop.fills] == [
        {
            "ts_ns": ny(MONDAY, stamp),
            "instrument_id": LEAD_ID,
            "side": "SELL",
            "qty": 1000.0,
            "price": price,
            "price_ts_ns": ny(MONDAY, stamp),
            "segment": "rth",
            "volume": 10.0**7,
        }
    ]
    assert (stop.flat_equity, stop.carried) == (pytest.approx(flat), False)
    last = record.last
    assert last is not None
    assert (last.opening_ns, last.pnl, last.stopped) == (ny(NEXT_MONDAY, "04:00"), 0.0, False)
    assert (last.anchor, last.threshold) == (pytest.approx(flat), pytest.approx(next_threshold))


def test_a_flatten_that_lands_in_the_next_week_is_recorded_as_carried(tmp_path: Path) -> None:
    """Friday's last close, a post-market minute, stops the week, and LEAD prints next on
    Monday at 04:01, a pre-market one: the flatten fills there, in the new week, so the stop is
    carried, and the new week, anchored at the 100,500 struck at Friday's last point, opens
    400 down once the flatten lands."""
    lead = [
        (ny(FRIDAY, "04:01"), 10.00),
        (ny(FRIDAY, "19:59"), 10.50),
        (ny(NEXT_MONDAY, "04:01"), 10.10),
    ]
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {(ny(FRIDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")]}

    result, _ = card(plan, lead, lagg, tmp_path, period="1w")

    record = result.run.period_stops
    assert record is not None
    assert record.openings == [ny(MONDAY, "04:00"), ny(NEXT_MONDAY, "04:00")]
    (stop,) = record.stops
    assert (stop.opening_ns, stop.hit_ns, stop.segment) == (
        ny(MONDAY, "04:00"),
        ny(FRIDAY, "19:59"),
        "post",
    )
    assert [(f.ts_ns, f.price, f.segment) for f in stop.fills] == [
        (ny(NEXT_MONDAY, "04:01"), 10.10, "pre")
    ]
    assert (stop.flat_equity, stop.carried) == (pytest.approx(100_100.0), True)
    assert record.last is not None
    assert (record.last.anchor, record.last.stopped) == (pytest.approx(100_500.0), False)
    assert record.last.pnl == pytest.approx(-400.0)


@pytest.mark.parametrize("is_dated", [False, True], ids=["fixed", "dated"])
@pytest.mark.parametrize("order", [(LEAD, LAGG), (LAGG, LEAD)])
@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_each_flatten_fill_is_read_at_the_bar_its_price_was_struck_at(
    latency_ms: float, order: tuple[str, str], is_dated: bool, tmp_path: Path
) -> None:
    """Both legs are held at the 09:33 hit and flattened at 09:34's first point. At no latency
    each fills at its own 09:34 close. Under 10 ms both land at 09:35's first point: the leg
    whose bar opens the instant fills at its 09:35 close, the other at its 09:34 close. Each
    fill records its own instant, the instant of the bar its price was struck at, that minute's
    part of the session and that bar's volume — read off the venue's record under a dated
    spread, off the last bar the sleeve was handed otherwise, alike."""
    lead, lagg, plan = _pair()
    costs = dated(LEAD_ID, LAGG_ID) if is_dated else None

    result = ran(
        plan, {LEAD: lead, LAGG: lagg}, tmp_path, order=order, latency_ms=latency_ms, costs=costs
    )

    record = result.run.period_stops
    assert record is not None
    (stop,) = record.stops
    assert stop.hit_ns == ny(MONDAY, "09:33")
    first = order[0]
    expected = {}
    for symbol in (LEAD, LAGG):
        later = latency_ms and symbol == first
        index = 4 if later else 3
        expected[f"{symbol}.{VENUE}"] = (
            ny(MONDAY, "09:35" if latency_ms else "09:34"),
            ny(MONDAY, "09:35" if later else "09:34"),
            "rth",
            float(volume_of(symbol, index)),
        )
    assert {f.instrument_id: (f.ts_ns, f.price_ts_ns, f.segment, f.volume) for f in stop.fills} == (
        expected
    )
    assert stop.flat_equity == pytest.approx(result.run.equity[-1])


@pytest.mark.parametrize(
    ("is_dated", "read"), [(False, ("09:33", 2)), (True, ("09:34", 3))], ids=["fixed", "dated"]
)
def test_a_resting_exit_filled_while_stopped_is_read_at_the_bar_it_knew(
    is_dated: bool, read: tuple[str, int], tmp_path: Path
) -> None:
    """At the 09:33 hit the sleeve rests a sale of its thousand at 10.65, which the 09:34 bar
    trades through: the venue fills it as it applies that bar, before the sleeve is handed it.
    Under a dated spread the venue's record says so, and the fill's volume is read off the bar
    when the sleeve is handed it; otherwise the fill is read at the last bar the sleeve had,
    09:33's, which is the point before."""
    lead: list[Point] = [
        (ny(MONDAY, "04:01"), 10.00),
        (ny(MONDAY, "04:02"), 10.00),
        (ny(MONDAY, "09:33"), 10.60),
        (ny(MONDAY, "09:34"), 10.60, 10.70, 10.55, 10.62),
        (ny(MONDAY, "09:35"), 10.62),
    ]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "09:33"), LEAD_ID): [("limit", LEAD_ID, "SELL:1000@10.65")],
    }

    result = ran(
        plan,
        {LEAD: lead},
        tmp_path,
        universe=(LEAD_ID,),
        costs=dated(LEAD_ID) if is_dated else None,
    )

    record = result.run.period_stops
    assert record is not None
    (stop,) = record.stops
    stamp, index = read
    assert [(f.ts_ns, f.price, f.price_ts_ns, f.volume) for f in stop.fills] == [
        (ny(MONDAY, "09:34"), 10.65, ny(MONDAY, stamp), float(volume_of(LEAD, index)))
    ]


def test_a_card_with_no_period_target_records_none_and_stores_none(tmp_path: Path) -> None:
    """The same sleeve over the same weeks with no target: nothing is recorded, and the run is
    stored without the key, as it was before runs kept it."""
    lead, lagg, plan = _two_weeks()
    made = request(plan, tmp_path / "seen.txt")
    untargeted = replace(made, hyp=made.hyp.model_copy(update={"period_target": None}))

    result = execute(
        untargeted,
        [instrument(LEAD), instrument(LAGG)],
        [volumed(LEAD, lead), volumed(LAGG, lagg)],
    )

    assert result.run.period_stops is None
    assert "period_stops" not in encode_run(result.run)


@pytest.mark.parametrize("order", [(LEAD, LAGG), (LAGG, LEAD)])
@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_a_replay_records_the_stops_the_card_does(
    order: tuple[str, str], latency_ms: float, tmp_path: Path
) -> None:
    """The live code path, handed the same points, records the same record element by
    element: the weeks, the stop, its flatten's fills and their bars, and the week open at
    the last point."""
    lead, lagg, plan = _pair()
    made = request(plan, tmp_path / "seen.txt", latency_ms=latency_ms, costs=COSTS)
    instruments = [instrument(symbol) for symbol in order]
    names = {LEAD: volumed(LEAD, lead), LAGG: volumed(LAGG, lagg)}
    groups = [names[symbol] for symbol in order]

    researched = execute(made, instruments, groups)
    replayed = session.run_node(made, instruments, groups).result

    assert researched.run.period_stops is not None
    assert len(researched.run.period_stops.stops) == 1
    assert replayed.run.period_stops == researched.run.period_stops


def _staged(
    made: Any, lead: Sequence[Point], lagg: Sequence[Point], tmp_path: Path
) -> tuple[Any, Any]:
    """The request over one Monday, and what a stage node over that Monday realised."""
    one_day = replace(made, window=(MONDAY, MONDAY))
    names = {LEAD: volumed(LEAD, lead), LAGG: volumed(LAGG, lagg)}
    held = catalog(
        tmp_path / "catalog", [*names[LEAD], *names[LAGG]], [instrument(LEAD), instrument(LAGG)]
    )
    staged = replace(one_day, overrides={"record": str(tmp_path / "stage.txt")})
    (realised,) = node.run(_stage(_placed(staged, tmp_path), held, MONDAY)).realised
    return one_day, realised


def test_a_stage_window_records_the_stops_the_card_does(tmp_path: Path) -> None:
    """A stage node over the same Monday stops and flattens where the card does, and records
    it so: the same weeks, the same stop and the same week open at its last point."""
    lead, lagg, plan = _pair()
    made = request(plan, tmp_path / "card.txt", costs=COSTS)

    one_day, realised = _staged(made, lead, lagg, tmp_path)
    researched = execute(
        one_day,
        [instrument(LEAD), instrument(LAGG)],
        [volumed(LEAD, lead), volumed(LAGG, lagg)],
    )

    assert researched.run.period_stops is not None
    assert len(researched.run.period_stops.stops) == 1
    assert realised.run.period_stops == researched.run.period_stops


def test_a_stage_s_own_flatten_is_no_stop_s_and_the_open_week_is_read_after_it(
    tmp_path: Path,
) -> None:
    """The window's last close stops the week, and its handler rests a sale of 500 at 99.00,
    which never fills: the node's flatten after the window cancels it and closes the thousand,
    and that close is the stage's, so the stop records no fill. The week open at the last point
    is stopped, and its P&L is read after that close: the 600 the thousand rose, less what the
    entry and the close each paid."""
    stamps, closes = ("04:01", "04:02", "09:33"), (10.00, 10.00, 10.60)
    lead = list(zip((ny(MONDAY, s) for s in stamps), closes, strict=True))
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan: dict[Any, Any] = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "09:33"), LEAD_ID): [("limit", LEAD_ID, "SELL:500@99.00")],
    }
    made = request(plan, tmp_path / "card.txt", latency_ms=10.0, costs=COSTS)

    _, realised = _staged(made, lead, lagg, tmp_path)

    last_seen = Seen(*ast.literal_eval((tmp_path / "stage.txt").read_text().splitlines()[-1]))
    assert last_seen.stopped
    assert [(f.side, f.qty) for f in realised.run.fills if f.instrument_id == LEAD_ID] == [
        ("BUY", 1000.0),
        ("SELL", 1000.0),
    ]
    record = realised.run.period_stops
    assert record is not None
    (stop,) = record.stops
    assert (stop.fills, stop.flat_equity, stop.carried) == ([], None, False)
    assert record.last is not None and record.last.stopped
    paid = [fill.cost for fill in realised.run.fills]
    assert len(paid) == 2 and paid[1] > 0
    assert record.last.pnl == pytest.approx(600.0 - sum(paid))
