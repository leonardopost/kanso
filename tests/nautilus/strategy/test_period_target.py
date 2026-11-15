"""The harness under a period target: a week that reaches its target is flattened and stopped.

Two names are fed one-minute bars at New York wall times in January 2024, when New York is five
hours behind UTC; the weeks open on Monday at 04:00. A sleeve buys and sells on a plan keyed by
each bar's instant and name, and writes down what it saw on every bar after the harness read
the target: whether the week is stopped, what it holds, its balance, the week's anchor and
threshold, and how many entries were dropped. Every price is a bar's own close, so the bar a
fill was struck at is read off its price, and costs are zero unless a test states them, so every
P&L is a difference of closes by hand.

What it pins: the first close at or above the target stops the week, under both bases; the
flatten fills at the held name's next print, never at the price that made it due, hours later
or in the next week; under a latency each leg of a pair fills at the close its order lands on,
under both same-instant point orders; an entry in flight at the hit fills and is flattened;
entries of every kind are dropped until the next week has opened and every name is flat; and
the anchor is the equity the card struck at the week before's last point, carry included.
"""

from __future__ import annotations

import ast
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from nautilus_trader.model.data import (
    Bar,
    BarSpecification,
    BarType,
    CustomData,
    DataType,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import CardRun
from kanso.data.types import CorporateAction
from kanso.nautilus.backtest import RunRequest, RunResult, execute
from kanso.schemas import CostsOverride, Hypothesis, resolve_venue_model
from tests.nautilus.backtest.conftest import CAPITAL, SNAPSHOT, VENUE, hypothesis, instrument

LEAD, LAGG = "LEAD", "LAGG"
LEAD_ID, LAGG_ID = f"{LEAD}.{VENUE}", f"{LAGG}.{VENUE}"
NEW_YORK = ZoneInfo("America/New_York")
WINDOW = (date(2024, 1, 1), date(2024, 1, 31))
"""January 2024: weeks open on Mondays the 1st, 8th, 15th, 22nd and 29th, at 04:00."""
QTY = 1000
TARGET_BP = 50.0
"""Half a percent: 500 on the 100,000 capital, a half-dollar move on a thousand shares."""

ZERO = {"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "fixed_bps", "fixed_bps": 0.0}

SLEEVE = """
from pathlib import Path

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

PLAN = {plan!r}
QTY = {qty}


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        key = str(bar.bar_type.instrument_id)
        ts = bar.ts_event
        sent = []
        for act, name, side in PLAN.get((ts, key), ()):
            if act == "entry":
                sent.append(self.submit_entry(name, side, qty=QTY) is not None)
            elif act == "exit":
                sent.append(self.submit_exit(name) is not None)
            elif act == "hand":
                self.submit_order(self._market(name, side))
            elif act == "list":
                both = [self._market(name, side), self._market(name, side)]
                self.submit_order_list(self.order_factory.create_list(both))
            elif act == "rest":
                self.submit_order(
                    self.order_factory.limit(
                        InstrumentId.from_str(name), OrderSide[side], Quantity.from_int(10),
                        Price.from_str("1.00"),
                    )
                )
        with Path(self.kanso_config.record).open("a") as out:
            out.write(repr((
                ts, key, self.period_stopped, self.held("{lead}"), self.held("{lagg}"),
                self.balance, self._target_anchor, self._target_threshold,
                self._target_dropped, sent, len(self.intents),
            )) + "\\n")

    def _market(self, name, side):
        return self.order_factory.market(
            InstrumentId.from_str(name), OrderSide[side], Quantity.from_int(QTY)
        )
"""


@dataclass(frozen=True)
class Seen:
    """What the sleeve read on one bar, after the harness read the target."""

    ts: int
    name: str
    stopped: bool
    lead: float
    lagg: float
    balance: float
    anchor: float
    threshold: float
    dropped: int
    sent: list[bool]
    intents: int


def ny(day: date, hhmm: str) -> int:
    """The instant New York's clock reads `hhmm` on `day`."""
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return int(datetime.combine(day, time(hours, minutes), tzinfo=NEW_YORK).timestamp()) * 10**9


def series(symbol: str, points: Sequence[tuple[int, float]]) -> tuple[Bar, ...]:
    """One name's one-minute bars, each stamped and published at its close, at that close."""
    kind = BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for ts, close in points:
        price = Price(close, 2)
        made.append(Bar(kind, price, price, price, price, Quantity.from_int(10**7), ts, ts))
    return tuple(made)


def target(of: str = "capital", target_bp: float = TARGET_BP) -> dict[str, object]:
    return {
        "period": "week",
        "opens": "Mon 04:00",
        "tz": "America/New_York",
        "target_bp": target_bp,
        "of": of,
    }


def request(
    plan: Mapping[tuple[int, str], Sequence[tuple[str, str, str]]],
    record: Path,
    *,
    of: str = "capital",
    universe: Sequence[str] = (LEAD_ID, LAGG_ID),
    costs: Mapping[str, object] | None = None,
    latency_ms: float = 0.0,
    period: str = "1d",
    book: Mapping[str, object] | None = None,
    max_leverage: float = 1.0,
    max_position_pct: float = 20.0,
    stated: Mapping[str, object] | None = None,
    qty: int = QTY,
    target_bp: float = TARGET_BP,
    data_requirements: Sequence[str] = ("bar",),
    resolution: str = "1m",
    source: str | None = None,
) -> RunRequest:
    """A card of the plan's sleeve under a period target, recording to `record`."""
    document = hypothesis(
        universe=tuple(universe),
        data_requirements=tuple(data_requirements),
        costs={**(costs or ZERO), "latency_ms": latency_ms, **(stated or {})},
        max_leverage=max_leverage,
        resolution=resolution,
    ).model_dump(mode="json")
    document.update(horizon="1d", period_target=target(of, target_bp))
    document["risk_limits"]["max_position_pct"] = max_position_pct
    if book is not None:
        document["book"] = dict(book)
    hyp = Hypothesis.model_validate(document)
    assert hyp.costs is not None
    model = resolve_venue_model(
        VENUE,
        broker="synthetic",
        hypothesis_costs=CostsOverride.model_validate(hyp.costs.model_dump(exclude_none=True)),
        max_leverage=hyp.risk_limits.max_leverage,
        quotes_available=False,
    )
    if source is None:
        source = SLEEVE.format(plan=dict(plan), qty=qty, lead=LEAD_ID, lagg=LAGG_ID)
    return RunRequest(
        hyp=hyp,
        strategy_source=source.encode(),
        window=WINDOW,
        snapshot_id=SNAPSHOT,
        venue_model=model.model_dump(),
        capital=CAPITAL,
        period=period,
        overrides={"record": str(record)},
    )


def card(
    plan: Mapping[tuple[int, str], Sequence[tuple[str, str, str]]],
    lead: Sequence[tuple[int, float]],
    lagg: Sequence[tuple[int, float]],
    tmp_path: Path,
    *,
    order: tuple[str, str] = (LEAD, LAGG),
    info: Mapping[str, object] | None = None,
    **options: Any,
) -> tuple[RunResult, list[Seen]]:
    """Run the plan's sleeve over both names' bars, the names' series handed in `order`, with
    `info` — a split schedule — on LEAD's definition."""
    record = tmp_path / f"seen-{len(list(tmp_path.iterdir()))}.txt"
    made = request(plan, record, **options)
    names = {LEAD: series(LEAD, lead), LAGG: series(LAGG, lagg)}
    held = [symbol for symbol in order if f"{symbol}.{VENUE}" in made.hyp.universe]
    defined = [
        instrument(symbol, info=dict(info)) if info and symbol == LEAD else instrument(symbol)
        for symbol in held
    ]
    result = execute(made, defined, [names[s] for s in held])
    seen = [Seen(*ast.literal_eval(line)) for line in record.read_text().splitlines()]
    return result, seen


def fills(run: CardRun, name: str = LEAD_ID) -> list[tuple[str, float, int]]:
    """A name's fills as `(side, price, instant)`."""
    return [(f.side, f.px, f.ts_ns) for f in run.fills if f.instrument_id == name]


def equity_before(run: CardRun, ts: int) -> float:
    """The equity the card struck at the last period end before the instant `ts`."""
    return run.equity[bisect_right(run.period_ends_ns, ts - 1) - 1]


def flat(day: date, stamps: Sequence[str], close: float) -> list[tuple[int, float]]:
    return [(ny(day, hhmm), close) for hhmm in stamps]


TUESDAY, MONDAY, NEXT_MONDAY = date(2024, 1, 2), date(2024, 1, 8), date(2024, 1, 15)
FRIDAY = date(2024, 1, 12)
MINUTES = ("09:31", "09:32", "09:33", "09:34", "09:35", "09:36")


def _two_weeks() -> tuple[list[tuple[int, float]], list[tuple[int, float]], dict[Any, Any]]:
    """Week one makes 400 and closes flat; week two buys again and rises a cent at a time
    through the target: +300, +500, +510, +600 and +700 at the closes of 09:32 to 09:36."""
    lead = [
        (ny(TUESDAY, "09:31"), 10.00),
        (ny(TUESDAY, "09:32"), 10.40),
        (ny(TUESDAY, "09:33"), 10.40),
        *zip(
            (ny(MONDAY, hhmm) for hhmm in MINUTES),
            (10.00, 10.30, 10.50, 10.51, 10.60, 10.70),
            strict=True,
        ),
        (ny(NEXT_MONDAY, "09:31"), 10.00),
    ]
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(TUESDAY, "09:31"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(TUESDAY, "09:33"), LEAD_ID): [("exit", LEAD_ID, "")],
        (ny(MONDAY, "09:31"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "09:36"), LAGG_ID): [
            ("entry", LAGG_ID, "BUY"),
            ("hand", LAGG_ID, "BUY"),
            ("list", LAGG_ID, "BUY"),
        ],
        (ny(NEXT_MONDAY, "09:31"), LAGG_ID): [("entry", LAGG_ID, "BUY")],
    }
    return lead, lagg, plan


@pytest.mark.parametrize(
    ("of", "stops_at", "threshold", "flattened"),
    [("capital", "09:33", 500.0, 10.51), ("period_open", "09:34", 502.0, 10.60)],
)
def test_the_first_close_at_or_above_the_target_stops_the_week(
    of: str, stops_at: str, threshold: float, flattened: float, tmp_path: Path
) -> None:
    """Week two opens on the 100,400 week one closed at. Of the capital the target is 500,
    reached at the 09:33 close; of that opening it is 502, reached at 09:34. The flatten is
    sent at the next close LEAD prints and fills there, never at the close that stopped it."""
    lead, lagg, plan = _two_weeks()

    result, seen = card(plan, lead, lagg, tmp_path, of=of)

    monday = [row for row in seen if row.ts >= ny(MONDAY, "04:00")]
    week = [row for row in monday if row.ts < ny(NEXT_MONDAY, "04:00")]
    assert {row.anchor for row in week} == {100_400.0}
    assert [row.threshold for row in week] == [pytest.approx(threshold)] * len(week)
    assert min(row.ts for row in seen if row.stopped) == ny(MONDAY, stops_at)
    sold = [
        fill for fill in fills(result.run) if fill[0] == "SELL" and fill[2] > ny(MONDAY, "04:00")
    ]
    stamp = MINUTES[MINUTES.index(stops_at) + 1]
    assert sold == [("SELL", flattened, ny(MONDAY, stamp))]
    hit = dict(lead)[ny(MONDAY, stops_at)]
    assert flattened != hit


def test_entries_of_every_kind_are_dropped_until_the_next_week_and_then_go(
    tmp_path: Path,
) -> None:
    """While the week is stopped `submit_entry` returns `None`, and an order and a list built
    by hand are dropped unsent, each counted and none recorded as an intent; the next week, its
    sleeve flat, takes the entry."""
    lead, lagg, plan = _two_weeks()

    result, seen = card(plan, lead, lagg, tmp_path)

    asked = next(row for row in seen if row.ts == ny(MONDAY, "09:36") and row.name == LAGG_ID)
    assert asked.stopped and asked.sent == [False]
    assert asked.dropped == 4  # the entry, the order and both orders of the list
    before = next(row for row in seen if row.ts == ny(MONDAY, "09:36") and row.name == LEAD_ID)
    assert asked.intents == before.intents
    assert fills(result.run, LAGG_ID) == [("BUY", 11.00, ny(NEXT_MONDAY, "09:31"))]
    week_three = [row for row in seen if row.ts >= ny(NEXT_MONDAY, "04:00")]
    assert not any(row.stopped for row in week_three)
    assert week_three[-1].sent == [True]


def test_the_flatten_waits_for_the_held_name_s_next_print_hours_later(tmp_path: Path) -> None:
    """LEAD's 15:59 close stops the week and LEAD prints next at 09:31 the day after. LAGG's
    evening closes come between, and send nothing: the flatten fills at LEAD's next close."""
    tuesday = date(2024, 1, 9)
    lead = [
        (ny(MONDAY, "04:01"), 10.00),
        (ny(MONDAY, "15:59"), 10.50),
        (ny(tuesday, "09:31"), 10.20),
    ]
    lagg = flat(MONDAY, ("04:01", "15:59", "16:30", "18:00", "19:59"), 11.00) + [
        (ny(tuesday, "09:31"), 11.00)
    ]
    plan = {(ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")]}

    result, seen = card(plan, lead, lagg, tmp_path)

    assert fills(result.run) == [
        ("BUY", 10.00, ny(MONDAY, "04:01")),
        ("SELL", 10.20, ny(tuesday, "09:31")),
    ]
    evening = [row for row in seen if ny(MONDAY, "16:00") < row.ts < ny(tuesday, "00:00")]
    assert [(row.stopped, row.lead) for row in evening] == [(True, QTY)] * 3


def test_a_flatten_due_at_the_next_opening_fills_in_the_new_week_its_anchor_struck_before(
    tmp_path: Path,
) -> None:
    """Friday's last close stops the week and LEAD prints next on Monday at 04:01: the flatten
    goes at that close, in the new week, whose anchor is the equity struck at Friday's last
    point with LEAD held — the card's equity at that week's end. Sent at market, the whole
    position on its way out, it lifts the stop, and LAGG's entry beside it goes."""
    lead = [
        (ny(FRIDAY, "04:01"), 10.00),
        (ny(FRIDAY, "19:59"), 10.50),
        (ny(NEXT_MONDAY, "04:01"), 10.10),
    ]
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(FRIDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(NEXT_MONDAY, "04:01"), LAGG_ID): [("entry", LAGG_ID, "BUY")],
    }

    result, seen = card(plan, lead, lagg, tmp_path, period="1w")

    friday, monday = (
        [row for row in seen if row.ts == ny(FRIDAY, "19:59")],
        [row for row in seen if row.ts == ny(NEXT_MONDAY, "04:01")],
    )
    assert all(row.stopped for row in friday)
    assert {row.anchor for row in monday} == {100_500.0}
    assert equity_before(result.run, ny(NEXT_MONDAY, "04:01")) == 100_500.0
    assert fills(result.run) == [
        ("BUY", 10.00, ny(FRIDAY, "04:01")),
        ("SELL", 10.10, ny(NEXT_MONDAY, "04:01")),
    ]
    assert not any(row.stopped for row in monday)
    assert monday[-1].sent == [True]


@pytest.mark.parametrize("order", [(LEAD, LAGG), (LAGG, LEAD)])
@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_each_leg_of_a_pair_fills_at_the_close_its_order_lands_on(
    order: tuple[str, str], latency_ms: float, tmp_path: Path
) -> None:
    """Both legs are held at the 09:33 hit and both print at 09:34, so both flattens go at the
    first point dispatched there. At no latency each fills at its own 09:34 close. Under 10 ms
    both land at 09:35's first point, after the venue has applied it: the leg whose bar opens
    the instant fills at its 09:35 close, the other at its 09:34 close."""
    stamps = ("04:01", "04:02", "09:33", "09:34", "09:35")
    lead = list(
        zip((ny(MONDAY, s) for s in stamps), (10.00, 10.00, 10.25, 10.26, 10.27), strict=True)
    )
    lagg = list(
        zip((ny(MONDAY, s) for s in stamps), (11.00, 11.00, 11.25, 11.26, 11.27), strict=True)
    )
    plan = {(ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY"), ("entry", LAGG_ID, "BUY")]}

    result, seen = card(plan, lead, lagg, tmp_path, order=order, latency_ms=latency_ms)

    assert min(row.ts for row in seen if row.stopped) == ny(MONDAY, "09:33")
    sold = {
        name: [(px, ts) for side, px, ts in fills(result.run, name) if side == "SELL"]
        for name in (LEAD_ID, LAGG_ID)
    }
    if latency_ms == 0.0:
        assert sold == {
            LEAD_ID: [(10.26, ny(MONDAY, "09:34"))],
            LAGG_ID: [(11.26, ny(MONDAY, "09:34"))],
        }
        return
    first = f"{order[0]}.{VENUE}"
    second = f"{order[1]}.{VENUE}"
    closes = {LEAD_ID: (10.26, 10.27), LAGG_ID: (11.26, 11.27)}
    assert sold == {
        first: [(closes[first][1], ny(MONDAY, "09:35"))],
        second: [(closes[second][0], ny(MONDAY, "09:35"))],
    }


def test_a_flip_in_flight_at_the_hit_fills_and_its_new_leg_is_flattened(tmp_path: Path) -> None:
    """One name alone is dispatched point by point, so under 10 ms an order sent on a point is
    matched after the sleeve's handler of the next. The flip sent at 04:03 — sell the long, sell
    as much again short — is in flight when 04:04's close stops the week: it fills there, and the
    short it opens is flattened at the name's next print, 04:05, filling at 04:06. From that fill
    to the next week the sleeve holds nothing."""
    stamps = ("04:01", "04:02", "04:03", "04:04", "04:05", "04:06", "04:07")
    closes = (10.00, 10.00, 10.49, 10.60, 10.70, 10.80, 10.90)
    lead = list(zip((ny(MONDAY, s) for s in stamps), closes, strict=True))
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:03"), LEAD_ID): [("exit", LEAD_ID, ""), ("entry", LEAD_ID, "SELL")],
        (ny(MONDAY, "04:04"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
    }

    result, seen = card(plan, lead, [], tmp_path, universe=(LEAD_ID,), latency_ms=10.0)

    assert min(row.ts for row in seen if row.stopped) == ny(MONDAY, "04:04")
    assert fills(result.run) == [
        ("BUY", 10.00, ny(MONDAY, "04:02")),
        ("SELL", 10.60, ny(MONDAY, "04:04")),
        ("SELL", 10.60, ny(MONDAY, "04:04")),
        ("BUY", 10.80, ny(MONDAY, "04:06")),
    ]
    assert [row.sent for row in seen if row.ts == ny(MONDAY, "04:04")] == [[False]]
    after = [row for row in seen if row.ts >= ny(MONDAY, "04:06")]
    assert after and all(row.lead == 0.0 for row in after)


def test_a_hit_with_the_position_on_its_way_out_sends_no_second_exit(tmp_path: Path) -> None:
    """The exit sent at 04:03 is in flight when 04:04's close stops the week: nothing more is
    owed, so nothing more is sent, and the entry asked at 04:04 is dropped."""
    stamps = ("04:01", "04:02", "04:03", "04:04", "04:05", "04:06")
    closes = (10.00, 10.00, 10.49, 10.60, 10.70, 10.80)
    lead = list(zip((ny(MONDAY, s) for s in stamps), closes, strict=True))
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:03"), LEAD_ID): [("exit", LEAD_ID, "")],
        (ny(MONDAY, "04:04"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
    }

    result, seen = card(plan, lead, [], tmp_path, universe=(LEAD_ID,), latency_ms=10.0)

    assert fills(result.run) == [
        ("BUY", 10.00, ny(MONDAY, "04:02")),
        ("SELL", 10.60, ny(MONDAY, "04:04")),
    ]
    assert all(row.stopped for row in seen if row.ts >= ny(MONDAY, "04:04"))
    assert seen[-1].dropped == 1


def test_the_anchor_is_the_card_s_equity_at_the_week_before_s_end_with_its_carry(
    tmp_path: Path,
) -> None:
    """With `return_period` a week and a carry on what the book borrows, each week's anchor is
    the equity the card struck at the week before's last point, after its carry: 15,000 shares
    of LEAD at 10 hold 150,000 on a book of 100,000. Week two's flatten, sent at Friday's last
    close, fills at Monday's first: the anchor is struck before it."""
    days = [date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16)]
    lead = [
        (ny(day, hhmm), 10.00 + 0.01 * index)
        for index, day in enumerate(days)
        for hhmm in ("09:31", "15:59")
    ]
    lead = [(ts, round(close, 2)) for ts, close in lead]
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(TUESDAY, "09:31"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(FRIDAY, "15:59"), LEAD_ID): [("exit", LEAD_ID, "")],
    }
    result, seen = card(
        plan,
        lead,
        lagg,
        tmp_path,
        period="1w",
        book={"financing_rate_bps": 500.0},
        max_leverage=2.0,
        max_position_pct=200.0,
        latency_ms=10.0,
        qty=15_000,
        target_bp=10_000.0,
    )

    run = result.run
    weeks = {
        ny(TUESDAY, "09:31"): CAPITAL,
        ny(MONDAY, "09:31"): equity_before(run, ny(MONDAY, "09:31")),
        ny(NEXT_MONDAY, "09:31"): equity_before(run, ny(NEXT_MONDAY, "09:31")),
    }
    struck = {row.ts: row.anchor for row in seen if row.ts in weeks}
    assert struck == pytest.approx(weeks, rel=1e-12)
    assert weeks[ny(MONDAY, "09:31")] < CAPITAL + 0.03 * 15_000  # the week's carry is in it
    assert sum(run.carry) > 0
    sold = [fill for fill in fills(run) if fill[0] == "SELL"]
    assert [ts for _, _, ts in sold] == [ny(NEXT_MONDAY, "09:31")]


def test_a_monday_split_moves_no_p_and_l_and_the_anchor_is_the_card_s_equity(
    tmp_path: Path,
) -> None:
    """LEAD splits four for one on Monday the 15th, its ex-date the first point of the week: the
    venue applies the split before that point is matched, so the anchor is struck on four times
    the shares at a price restated by the ratio, which is the equity the card struck on Friday
    as printed, to a part in a trillion; the split moves no P&L, and a quarter of the target's
    move after it stops the week."""
    lead = [
        (ny(date(2024, 1, 9), "09:31"), 10.00),
        (ny(FRIDAY, "15:59"), 10.40),
        (ny(NEXT_MONDAY, "09:31"), 2.60),
        (ny(NEXT_MONDAY, "09:32"), 2.60),
        (ny(NEXT_MONDAY, "09:33"), 2.73),
    ]
    plan = {(ny(date(2024, 1, 9), "09:31"), LEAD_ID): [("entry", LEAD_ID, "BUY")]}
    split = {"splits": [{"ex_date": NEXT_MONDAY.isoformat(), "ratio": 4.0}]}

    result, seen = card(plan, lead, [], tmp_path, universe=(LEAD_ID,), info=split, period="1w")

    monday = [row for row in seen if row.ts >= ny(NEXT_MONDAY, "04:00")]
    assert [row.anchor for row in monday] == [
        pytest.approx(equity_before(result.run, ny(NEXT_MONDAY, "09:31")), rel=1e-12)
    ] * 3
    assert [(row.stopped, row.lead) for row in monday] == [(False, 4 * QTY)] * 2 + [(True, 4 * QTY)]


TICKS = """
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_quote_tick(self, tick):
        if not self.intents:
            self.submit_entry(tick.instrument_id, "BUY", qty=1000)
        self._write("quote")

    def on_trade_tick(self, tick):
        self._write("trade")

    def on_data(self, data):
        self._write("data")

    def _write(self, kind):
        with Path(self.kanso_config.record).open("a") as out:
            row = (self.data_time, kind, self.period_stopped, self.held("LEAD.XNAS"))
            out.write(repr(row) + "\\n")
"""


def test_a_print_stops_the_week_and_the_next_quote_flattens_it(tmp_path: Path) -> None:
    """On a feed of quotes, prints and a custom point the target is read at the head of every
    one: bought at 10.01, the ask, the print at 10.60 stops the week; the dividend declared
    after it sends nothing, since the name has not printed since; and the next quote, a new
    mark, sends the flatten, which sells at its bid."""
    stamps = [ny(MONDAY, hhmm) for hhmm in ("04:01", "04:02", "04:03", "04:04", "04:05")]
    lead = InstrumentId.from_str(LEAD_ID)

    def quote(ts: int, mid: float) -> QuoteTick:
        bid, ask = Price(mid - 0.01, 2), Price(mid + 0.01, 2)
        return QuoteTick(lead, bid, ask, Quantity.from_int(10**6), Quantity.from_int(10**6), ts, ts)

    def trade(ts: int, px: float, tag: str) -> TradeTick:
        size = Quantity.from_int(500)
        return TradeTick(lead, Price(px, 2), size, AggressorSide.BUYER, TradeId(tag), ts, ts)

    declared = CustomData(  # wrapped, as the catalog hands a custom point over
        DataType(CorporateAction),
        CorporateAction(
            instrument_id=lead,
            kind="dividend",
            ratio=1.0,
            cash=0.10,
            currency="USD",
            ex_date_ns=ny(date(2024, 1, 22), "09:30"),
            ts_event=stamps[2],
            ts_init=stamps[2],
        ),
    )
    record = tmp_path / "ticks.txt"
    made = request(
        {},
        record,
        universe=(LEAD_ID,),
        data_requirements=("quote", "trade", "corporate_action"),
        resolution="tick",
        source=TICKS,
    )

    result = execute(
        made,
        [instrument(LEAD)],
        [
            (quote(stamps[0], 10.00), quote(stamps[3], 10.50)),
            (trade(stamps[1], 10.60, "T1"), trade(stamps[4], 10.40, "T2")),
            (declared,),
        ],
    )

    seen = [ast.literal_eval(line) for line in record.read_text().splitlines()]
    assert seen == [
        (stamps[0], "quote", False, 1000.0),
        (stamps[1], "trade", True, 1000.0),
        (stamps[2], "data", True, 1000.0),
        (stamps[3], "quote", True, 0.0),
        (stamps[4], "trade", True, 0.0),
    ]
    assert fills(result.run) == [("BUY", 10.01, stamps[0]), ("SELL", 10.49, stamps[3])]


def test_under_an_order_rate_the_flatten_is_owed_never_refused(tmp_path: Path) -> None:
    """Two tokens a minute, and two for a cancel: the hit's cancels of two resting orders wait
    for their tokens, the flattens behind them are owed, and both legs still go flat before the
    week ends; the entries the stop drops are not the rate's refusals."""
    stamps = [f"09:{minute:02d}" for minute in range(31, 45)]
    lead = [(ny(MONDAY, "04:01"), 10.00), (ny(MONDAY, "04:02"), 10.00)] + [
        (ny(MONDAY, s), round(10.50 + 0.01 * index, 2)) for index, s in enumerate(stamps)
    ]
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:02"), LAGG_ID): [("rest", LEAD_ID, "BUY"), ("rest", LAGG_ID, "BUY")],
        **{(ny(MONDAY, s), LAGG_ID): [("entry", LAGG_ID, "BUY")] for s in stamps},
    }

    result, seen = card(
        plan,
        lead,
        lagg,
        tmp_path,
        stated={"orders_per_minute": 2, "requests_per_cancel": 2},
    )

    assert min(row.ts for row in seen if row.stopped) == ny(MONDAY, "09:31")
    assert fills(result.run) == [
        ("BUY", 10.00, ny(MONDAY, "04:01")),
        ("SELL", 10.52, ny(MONDAY, "09:33")),
    ]
    assert fills(result.run, LAGG_ID) == []
    assert seen[-1].lead == 0.0 and seen[-1].dropped == len(stamps)
    assert result.commands is not None and result.commands.refused_submits == 0


def _stops(seen: Sequence[Seen]) -> list[int]:
    """The instants at which the sleeve read its week stopped where it had read it trading."""
    found, was = [], False
    for row in seen:
        if row.stopped and not was:
            found.append(row.ts)
        was = row.stopped
    return found


def _recomputed(run: CardRun, lead: Sequence[tuple[int, float]]) -> list[int]:
    """The stops the rule makes, struck by hand from the card's own fills and LEAD's closes:
    the equity at each close, the anchor at each week's first close the equity at the close
    before it, and a stop at the first close of a trading week at or above the target. A week
    stopped lifts at the next week's first close, the sleeve being flat there."""
    cash, held, last = CAPITAL, 0.0, 0.0
    stops: list[int] = []
    weeks = [ny(MONDAY, "04:00"), ny(NEXT_MONDAY, "04:00")]
    anchor, week, stopped = CAPITAL, -1, False
    for ts, close in lead:
        now = sum(1 for opens in weeks if ts >= opens) - 1
        if now != week:
            if week >= 0:
                anchor = cash + held * last
                stopped = False
            week = now
        for fill in run.fills:
            if fill.ts_ns == ts:
                signed = fill.qty if fill.side == "BUY" else -fill.qty
                cash -= signed * fill.px
                held += signed
        last = close
        if not stopped and cash + held * close - anchor >= CAPITAL * TARGET_BP / 10_000:
            stops.append(ts)
            stopped = True
    return stops


@settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(moves=st.lists(st.integers(-40, 40), min_size=24, max_size=24))
def test_held_is_empty_after_the_last_flatten_and_the_stops_are_the_rule_s(
    moves: list[int], tmp_path: Path
) -> None:
    """Over random paths of one name, bought at the first close of each session and sold at its
    last: in every stopped span the sleeve holds nothing from the flatten's fill on, and the
    stops it read are the stops the rule makes from the card's own fills and closes."""
    stamps = ("09:31", "10:00", "12:00", "15:59")
    days = [date(2024, 1, day) for day in (8, 9, 10, 15, 16, 17)]
    closes, price = [], 1000
    for move in moves:
        price = max(500, price + move)
        closes.append(price / 100)
    lead = [
        (ny(day, hhmm), closes[index * len(stamps) + slot])
        for index, day in enumerate(days)
        for slot, hhmm in enumerate(stamps)
    ]
    plan: dict[Any, Any] = {}
    for day in days:
        plan[(ny(day, "09:31"), LEAD_ID)] = [("entry", LEAD_ID, "BUY")]
        plan[(ny(day, "15:59"), LEAD_ID)] = [("exit", LEAD_ID, "")]

    result, seen = card(plan, lead, [], tmp_path, universe=(LEAD_ID,))

    assert _stops(seen) == _recomputed(result.run, lead)
    for row in seen:
        if row.stopped:
            sold_since = [
                ts for side, _, ts in fills(result.run) if side == "SELL" and ts <= row.ts
            ]
            bought_since = [
                ts for side, _, ts in fills(result.run) if side == "BUY" and ts <= row.ts
            ]
            if sold_since and max(sold_since) >= max(bought_since, default=0):
                assert row.lead == 0.0
