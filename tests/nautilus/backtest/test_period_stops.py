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
from kanso.nautilus import backtest, node, session
from kanso.nautilus.backtest import RunResult, execute
from kanso.nautilus.struck import Struck
from kanso.schemas import PeriodStops
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


def volumed(symbol: str, points: Sequence[Point], *, delay_s: int = 0) -> tuple[Bar, ...]:
    """One name's one-minute bars, each stamped at its close and published `delay_s` after it,
    with the volume `volume_of` gives it."""
    kind = BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for index, (ts, *prices) in enumerate(points):
        ohlc = [Price(price, 2) for price in (prices * 4 if len(prices) == 1 else prices)]
        volume = Quantity.from_int(volume_of(symbol, index))
        made.append(Bar(kind, *ohlc, volume, ts, ts + delay_s * 10**9))
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
    part of the session and that bar's volume — read off the venue's record, which a target
    loads under a dated spread or none, alike."""
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


@pytest.mark.parametrize("is_dated", [False, True], ids=["fixed", "dated"])
@pytest.mark.parametrize(
    ("hit", "struck", "segment"),
    [("09:33", "09:34", "rth"), ("09:30", "09:31", "rth"), ("16:00", "16:01", "post")],
)
def test_a_resting_exit_filled_while_stopped_is_read_at_the_bar_the_venue_filled_it_on(
    hit: str, struck: str, segment: str, is_dated: bool, tmp_path: Path
) -> None:
    """At the hit, a close of 10.60 flat, the sleeve rests a sale of its thousand at 10.65,
    which the next bar trades through: the venue fills it as it applies that bar, before the
    sleeve is handed it, so its price was struck in that bar, whose whole range the hit's
    excludes. The fill is read there, dated spread or none — at that bar's instant, the part
    of the session the minute it covers falls in, and that bar's volume, read off it when the
    sleeve is handed it. The bar stamped 09:30 covers a pre-market minute and the one stamped
    16:00 a regular one, so a fill read at the bar before would name another part of the
    session."""
    lead: list[Point] = [
        (ny(MONDAY, "04:01"), 10.00),
        (ny(MONDAY, "04:02"), 10.00),
        (ny(MONDAY, hit), 10.60),
        (ny(MONDAY, struck), 10.60, 10.70, 10.55, 10.62),
        (ny(MONDAY, struck) + 60 * 10**9, 10.62),
    ]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, hit), LEAD_ID): [("limit", LEAD_ID, "SELL:1000@10.65")],
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
    assert stop.hit_ns == ny(MONDAY, hit)
    assert [(f.ts_ns, f.price, f.price_ts_ns, f.segment, f.volume) for f in stop.fills] == [
        (ny(MONDAY, struck), 10.65, ny(MONDAY, struck), segment, float(volume_of(LEAD, 3)))
    ]


@pytest.mark.parametrize(("hit", "segment"), [("09:30", "pre"), ("16:00", "rth"), ("10:00", "rth")])
@pytest.mark.parametrize("delay_s", [0, 30])
def test_a_stop_names_the_part_of_the_session_its_point_covers(
    hit: str, segment: str, delay_s: int, tmp_path: Path
) -> None:
    """LEAD's bars are stamped at their close and published `delay_s` after it, and the close
    of 10.60 at `hit` stops the week. The stop's instant is the point's availability, and its
    part of the session that of the minute the point covers, read off its stamp as
    `self.session` reads it: the bar stamped 09:30 covers a pre-market minute and the one
    stamped 16:00 a regular one, however late either was published."""
    delay = delay_s * 10**9
    kind = BarType(
        InstrumentId(Symbol(LEAD), Venue(VENUE)),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    closes = [
        (ny(MONDAY, "04:01"), 10.00),
        (ny(MONDAY, "04:02"), 10.00),
        (ny(MONDAY, hit), 10.60),
        (ny(MONDAY, hit) + 60 * 10**9, 10.50),
    ]
    bars = tuple(
        Bar(kind, *[Price(close, 2)] * 4, Quantity.from_int(10**6), ts, ts + delay)
        for ts, close in closes
    )
    plan = {(ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")]}
    made = request(plan, tmp_path / "seen.txt", universe=(LEAD_ID,))

    result = execute(made, [instrument(LEAD)], [bars])

    record = result.run.period_stops
    assert record is not None
    (stop,) = record.stops
    assert (stop.hit_ns, stop.segment) == (ny(MONDAY, hit) + delay, segment)


def test_a_window_that_opens_midweek_holds_its_first_week_in_its_first_fold(
    tmp_path: Path,
) -> None:
    """A window opening on Wednesday 10 January 2024 trades in the week that opened on Monday
    the 8th at 04:00, before the window's first midnight: the sleeve buys at 09:31 and the
    09:33 close stops that week. The run records it, and the window as one fold holds it, its
    stop and the week open at the last point, as a gate reading the run fold-wise is handed."""
    wednesday, friday = date(2024, 1, 10), date(2024, 1, 12)
    lead: list[Point] = [
        (ny(wednesday, "09:31"), 10.00),
        (ny(wednesday, "09:32"), 10.00),
        (ny(wednesday, "09:33"), 10.60),
        (ny(wednesday, "09:34"), 10.60),
    ]
    plan = {(ny(wednesday, "09:31"), LEAD_ID): [("entry", LEAD_ID, "BUY")]}
    made = request(plan, tmp_path / "seen.txt", universe=(LEAD_ID,), window=(wednesday, friday))

    run = execute(made, [instrument(LEAD)], [volumed(LEAD, lead)]).run

    record = run.period_stops
    assert record is not None
    assert (record.openings, record.stopped) == ([ny(date(2024, 1, 8), "04:00")], 1)
    assert record.last is not None and record.last.stopped
    (whole,) = run.folds(1)
    assert whole.period_stops == record


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


@pytest.mark.parametrize("delay_s", [0, 30])
def test_a_stage_reads_a_resting_exit_filled_while_stopped_at_the_bar_the_venue_filled_it_on(
    delay_s: int, tmp_path: Path
) -> None:
    """Under a fixed spread, LEAD's bars published `delay_s` after their close, the close of
    10.60 at 09:33 stops the week and the sleeve rests a sale of its thousand at 10.65, which
    the 09:34 bar trades through. A stage node's venue fills it as it applies that bar, before
    the sleeve is handed it, and the stage records the fill as the card does: at the instant it
    filled, the bar's own instant, the part of the session the minute it covers falls in, and
    that bar's volume. Published late, the fill's instant is the bar's publication and not the
    instant its price was struck at, so a stage that kept no venue record under a target alone
    would read it at neither."""
    delay = delay_s * 10**9
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
    made = replace(
        request(plan, tmp_path / "card.txt", universe=(LEAD_ID,)), window=(MONDAY, MONDAY)
    )
    bars = volumed(LEAD, lead, delay_s=delay_s)
    held = catalog(tmp_path / "catalog", list(bars), [instrument(LEAD)])
    staged = replace(made, overrides={"record": str(tmp_path / "stage.txt")})

    (realised,) = node.run(_stage(_placed(staged, tmp_path), held, MONDAY)).realised
    researched = execute(made, [instrument(LEAD)], [bars])

    def read(record: PeriodStops | None) -> list[tuple[int, float, int, str, float | None]]:
        assert record is not None
        (stop,) = record.stops
        return [(f.ts_ns, f.price, f.price_ts_ns, f.segment, f.volume) for f in stop.fills]

    expected = (ny(MONDAY, "09:34") + delay, 10.65, ny(MONDAY, "09:34"), "rth")
    assert read(researched.run.period_stops) == [(*expected, float(volume_of(LEAD, 3)))]
    assert read(realised.run.period_stops) == read(researched.run.period_stops)


@pytest.mark.parametrize("is_dated", [False, True], ids=["fixed", "dated"])
def test_a_stage_window_s_marks_hold_the_record_its_venue_was_loaded_with(
    is_dated: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage node loads one venue record under a target, dated spread or none, and its
    harness reads the stopped week's fills off it. The marks the window is extracted with hold
    that record, and price at it under a dated spread alone: never a record of their own that
    no venue was handed."""
    loaded: list[Struck] = []

    def loading() -> Struck:
        made = Struck()
        loaded.append(made)
        return made

    extracted: list[tuple[Struck | None, Struck | None]] = []
    extract = backtest._extract

    def extracting(request: Any, engine: Any, marks: Any, period_stops: Any = None) -> Any:
        extracted.append((marks.record, marks.pricing))
        return extract(request, engine, marks, period_stops)

    monkeypatch.setattr(node, "Struck", loading)
    monkeypatch.setattr(backtest, "_extract", extracting)
    lead, lagg, plan = _pair()
    costs = dated(LEAD_ID, LAGG_ID) if is_dated else COSTS
    made = request(plan, tmp_path / "card.txt", costs=costs)

    _, realised = _staged(made, lead, lagg, tmp_path)

    assert realised.run.period_stops is not None and realised.run.period_stops.stops
    (struck,) = loaded
    assert extracted == [(struck, struck if is_dated else None)]


@pytest.mark.parametrize("targeted", [True, False])
def test_an_idle_stage_window_records_what_a_window_handed_no_point_does(
    targeted: bool, tmp_path: Path
) -> None:
    """A stage whose clock already stands at the last point the catalog holds has nothing to
    replay, so it builds no node: a version under a target records the empty record of a
    window handed no point — no week, no stop, none open — and one with no target records
    none, so an idle window never reads as one of a version with no target."""
    lead, lagg, plan = _pair()
    made = request(plan, tmp_path / "card.txt", costs=COSTS)
    if not targeted:
        made = replace(made, hyp=made.hyp.model_copy(update={"period_target": None}))
    names = {LEAD: volumed(LEAD, lead), LAGG: volumed(LAGG, lagg)}
    points = [*names[LEAD], *names[LAGG]]
    held = catalog(tmp_path / "catalog", points, [instrument(LEAD), instrument(LAGG)])
    staged = replace(
        made, window=(MONDAY, MONDAY), overrides={"record": str(tmp_path / "stage.txt")}
    )
    after = max(int(point.ts_init) for point in points)

    ran_idle = node.run(_stage(_placed(staged, tmp_path), held, MONDAY, after=after))

    assert ran_idle.released == 0
    (realised,) = ran_idle.realised
    assert realised.run.period_stops == (PeriodStops() if targeted else None)


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
