"""The harness under a period target: a week that reaches its target is flattened and stopped.

Two names are fed one-minute bars at New York wall times, mostly in January 2024, when New York
is five hours behind UTC, and in March, February and November 2024 where a test crosses a change
of clocks or a closed Monday, so each stamp is built from its own date's offset (`ny`); the
weeks open on Monday at 04:00. A sleeve buys and sells on a plan keyed by each bar's instant and
name, and writes down what it saw on every bar after the harness read the target: whether the
week is stopped, what it holds, its balance, the week's anchor and threshold, and how many
orders were dropped. Every bar but one opens, trades and closes at one price, so the bar a fill
was struck at is read off its price, and costs are zero unless a test states them, so every P&L
is a difference of prices by hand; the one bar that ranges fills a resting exit inside it. The
overlay's clips and hedge legs are fed the strategy fixtures' bars instead, stamped a minute
apart from the epoch, under weeks that open on Wednesdays at 19:30 (`_clipped`).

What it pins: the first close at or above the target stops the week, under both bases, and so
does an exit's fill above the close; the flatten fills at the held name's next print, never at
the price that made it due, hours later or in the next week; under a latency each leg of a pair
fills at the close its order lands on, under both same-instant point orders; an entry in flight
at the hit fills and is flattened; entries of every kind are dropped until the next week has
opened and every name is flat — an order built by hand behind an exit, one by one or in a list
read order by order, an overlay's clip, which then holds nothing — asked at a finer bar the
overlay reads that took the week to its target among them — and a hedge leg that opens, while a
held clip's `FLAT` goes; a new week's entry waits for the old week's legs to be flat;
nothing sent while stopped takes the position past flat — one order bigger than it, an exit
beside a working one, one sized in dollars, a list holding a child that waits on a parent in
another name, a modify that grows a resting exit by any amount,
a regrowth behind a shrink the venue has yet to apply, and an exit built by hand beside a
flatten the order rate owes — while exits that add up to it go, and an order the venue has yet
to answer, a shrink and a regrowth sent before the hit among them, is read at the most it could
close; a modify to a count that is not a `Quantity` raises the engine's own error; the
weeks open at New York's Monday 04:00 across both changes of clocks and on a closed Monday; and
the anchor is the equity the card struck at the week before's last point, carry included.
"""

from __future__ import annotations

import ast
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
    OrderSide,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import CardRun
from kanso.data.types import CorporateAction
from kanso.nautilus import cross_section, session
from kanso.nautilus.backtest import RunRequest, RunResult, execute
from kanso.nautilus.hooks import Clip, Hedge
from kanso.nautilus.strategy import Decision, HookContext, KansoStrategy
from kanso.schemas import CostsOverride, Hypothesis, PeriodTarget, resolve_venue_model
from tests.nautilus.backtest.conftest import CAPITAL, SNAPSHOT, VENUE, hypothesis, instrument
from tests.nautilus.strategy.conftest import DEMO, HEDGE, MINUTE_NS, bar, second_bar
from tests.nautilus.strategy.test_clips import CLIP_BUDGET, HEDGE_CLIP, Clipper, clipper
from tests.nautilus.strategy.test_sizing import Enters, sized

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
            elif act in ("orders", "singles"):
                made = [
                    self._market(each, way)
                    for each, way in zip(name.split(","), side.split(","), strict=True)
                ]
                if act == "orders":
                    self.submit_order_list(self.order_factory.create_list(made))
                else:
                    for order in made:
                        self.submit_order(order)
            elif act == "rest":
                self.submit_order(
                    self.order_factory.limit(
                        InstrumentId.from_str(name), OrderSide[side], Quantity.from_int(10),
                        Price.from_str("1.00"),
                    )
                )
            elif act == "limit":
                way, qty, price = self._sized(side)
                self.resting = self.order_factory.limit(
                    InstrumentId.from_str(name), OrderSide[way], Quantity.from_int(qty),
                    Price.from_str(price),
                )
                self.submit_order(self.resting)
            elif act == "grow":
                self.modify_order(
                    self.resting, quantity=Quantity.from_int(int(side)),
                    price=Price.from_str("10.00"),
                )
            elif act == "count":
                self.modify_order(self.resting, quantity=int(side))
            elif act == "resize":
                qty, _, price = side.partition("@")
                self.modify_order(
                    self.resting, quantity=Quantity.from_int(int(qty)),
                    price=Price.from_str(price) if price else None,
                )
            elif act == "notional":
                way, qty, price = self._sized(side)
                made = (InstrumentId.from_str(name), OrderSide[way], Quantity.from_int(qty))
                if "@" in side:
                    order = self.order_factory.limit(
                        *made, Price.from_str(price), quote_quantity=True
                    )
                else:
                    order = self.order_factory.market(*made, quote_quantity=True)
                self.submit_order(order)
        with Path(self.kanso_config.record).open("a") as out:
            out.write(repr((
                ts, key, self.period_stopped, self.held("{lead}"), self.held("{lagg}"),
                self.balance, self._target_anchor, self._target_threshold,
                self._target_dropped, sent, len(self.intents),
            )) + "\\n")

    def _market(self, name, side):
        way, qty, _ = self._sized(side)
        return self.order_factory.market(
            InstrumentId.from_str(name), OrderSide[way], Quantity.from_int(qty)
        )

    def _sized(self, side):
        way, _, rest = side.partition(":")
        qty, _, price = rest.partition("@")
        return way, int(qty or QTY), price or "10.00"
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


Point = tuple[int, float] | tuple[int, float, float, float, float]
"""A bar as its instant and close, open, high and low all at the close, or as its instant,
open, high, low and close."""


def series(symbol: str, points: Sequence[Point]) -> tuple[Bar, ...]:
    """One name's one-minute bars, each stamped and published at its close."""
    kind = BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for ts, *prices in points:
        ohlc = [Price(price, 2) for price in (prices * 4 if len(prices) == 1 else prices)]
        made.append(Bar(kind, *ohlc, Quantity.from_int(10**7), ts, ts))
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
    window: tuple[date, date] = WINDOW,
    modifiers: Sequence[tuple[str, bytes, Mapping[str, object]]] = (),
) -> RunRequest:
    """A card of the plan's sleeve under a period target, recording to `record`, over
    `window` — declared as the hypothesis's research window — with `modifiers` attached."""
    document = hypothesis(
        universe=tuple(universe),
        data_requirements=tuple(data_requirements),
        costs={**(costs or ZERO), "latency_ms": latency_ms, **(stated or {})},
        max_leverage=max_leverage,
        resolution=resolution,
    ).model_dump(mode="json")
    document.update(horizon="1d", period_target=target(of, target_bp))
    if window != WINDOW:
        certified = window[1] + timedelta(days=10)
        document["windows"] = {
            "research": {"start": window[0].isoformat(), "end": window[1].isoformat()},
            "certification": {
                "start": certified.isoformat(),
                "end": (certified + timedelta(days=30)).isoformat(),
            },
            "forward": {"start": (certified + timedelta(days=31)).isoformat()},
        }
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
        window=window,
        snapshot_id=SNAPSHOT,
        venue_model=model.model_dump(),
        capital=CAPITAL,
        period=period,
        overrides={"record": str(record)},
        modifiers=tuple(modifiers),
    )


def card(
    plan: Mapping[tuple[int, str], Sequence[tuple[str, str, str]]],
    lead: Sequence[Point],
    lagg: Sequence[Point],
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


def test_a_new_week_s_entry_waits_until_every_old_leg_is_flat(tmp_path: Path) -> None:
    """LEAD is bought on Friday at the 04:01 close of 10.00 and the 19:59 close of 10.50 stops
    the week; LEAD does not print again until Monday at 04:05. LAGG prints at 04:01 that Monday,
    after the next week has opened, and its entry is dropped: LEAD is still held, not yet sent
    its flatten, and a new week's entry would meet the old week's leg. At 04:05 LEAD is
    flattened at its own close, the stop lifts, and LAGG's entry there goes."""
    lead = [
        (ny(FRIDAY, "04:01"), 10.00),
        (ny(FRIDAY, "19:59"), 10.50),
        (ny(NEXT_MONDAY, "04:05"), 10.10),
    ]
    lagg = [
        (ny(FRIDAY, "04:01"), 11.00),
        (ny(FRIDAY, "19:59"), 11.00),
        (ny(NEXT_MONDAY, "04:01"), 11.00),
        (ny(NEXT_MONDAY, "04:05"), 11.00),
    ]
    plan = {
        (ny(FRIDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(NEXT_MONDAY, "04:01"), LAGG_ID): [("entry", LAGG_ID, "BUY")],
        (ny(NEXT_MONDAY, "04:05"), LAGG_ID): [("entry", LAGG_ID, "BUY")],
    }

    result, seen = card(plan, lead, lagg, tmp_path)

    early = [row for row in seen if row.ts == ny(NEXT_MONDAY, "04:01")]
    assert [(row.name, row.stopped, row.sent) for row in early] == [(LAGG_ID, True, [False])]
    assert fills(result.run) == [
        ("BUY", 10.00, ny(FRIDAY, "04:01")),
        ("SELL", 10.10, ny(NEXT_MONDAY, "04:05")),
    ]
    assert fills(result.run, LAGG_ID) == [("BUY", 11.00, ny(NEXT_MONDAY, "04:05"))]


@pytest.mark.parametrize(
    ("act", "names", "sides", "sent"),
    [
        ("orders", (LEAD_ID, LEAD_ID), ("SELL", "SELL"), 0),
        ("orders", (LEAD_ID, LEAD_ID), ("SELL", "BUY"), 0),
        ("orders", (LEAD_ID, LAGG_ID), ("SELL", "BUY"), 0),
        ("singles", (LEAD_ID, LEAD_ID), ("SELL", "SELL"), 1),
        ("orders", (LEAD_ID,), ("SELL",), 1),
    ],
)
def test_no_order_built_by_hand_opens_while_stopped_whatever_exit_it_is_sent_behind(
    act: str, names: tuple[str, ...], sides: tuple[str, ...], sent: int, tmp_path: Path
) -> None:
    """LEAD is bought at the 04:01 close of 10.00, and the 04:02 close of 10.60 stops the week
    before that bar's handler runs. There the sleeve sends, by hand, an exit of its long with an
    order behind it. A list is read order by order, each against what the orders before it
    leave: a flip written as a list, a re-entry and another name's entry each drop the whole
    list unsent, and LEAD is flattened at its next print. Sent one by one, the exit goes and the
    sale behind it — an exit to `classify` until the first fills — is dropped, since the exit on
    its way already closes what it would. A list of the exit alone goes. Nothing is bought, or
    sold short, while stopped."""
    stamps = ("04:01", "04:02", "04:03", "04:04")
    lead = list(zip((ny(MONDAY, s) for s in stamps), (10.00, 10.60, 10.70, 10.80), strict=True))
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:02"), LEAD_ID): [(act, ",".join(names), ",".join(sides))],
    }

    result, seen = card(plan, lead, lagg, tmp_path)

    hit = next(row for row in seen if row.ts == ny(MONDAY, "04:02") and row.name == LEAD_ID)
    assert hit.stopped and hit.dropped == len(names) - sent
    out = ("SELL", 10.60, ny(MONDAY, "04:02")) if sent else ("SELL", 10.70, ny(MONDAY, "04:03"))
    assert fills(result.run) == [("BUY", 10.00, ny(MONDAY, "04:01")), out]
    assert fills(result.run, LAGG_ID) == []
    after = [row for row in seen if row.ts >= ny(MONDAY, "04:03")]
    assert after and all((row.lead, row.lagg) == (0.0, 0.0) for row in after)


PAST_FLAT = [
    # one order past flat into a smaller short, alone, as a list, or at a limit
    ([("singles", LEAD_ID, "SELL:1500")], 1, 0),
    ([("singles", LEAD_ID, "SELL:1999")], 1, 0),
    ([("orders", LEAD_ID, "SELL:1500")], 1, 0),
    ([("orders", f"{LEAD_ID},{LEAD_ID}", "SELL:500,SELL:900")], 2, 0),
    ([("limit", LEAD_ID, "SELL:1500")], 1, 0),
    # an exit beside one already working that closes what it would, at market or resting
    ([("limit", LEAD_ID, "SELL"), ("hand", LEAD_ID, "SELL")], 1, 1000),
    ([("hand", LEAD_ID, "SELL"), ("limit", LEAD_ID, "SELL")], 1, 1000),
    ([("limit", LEAD_ID, "SELL"), ("limit", LEAD_ID, "SELL")], 1, 1000),
    # a modify that grows a resting exit, past what is left or to it, alone or with an order,
    # an exit of the sleeve's own or a second grow behind it, read as if the first had not grown
    ([("limit", LEAD_ID, "SELL:500@99.00"), ("grow", LEAD_ID, "2000")], 1, 0),
    ([("limit", LEAD_ID, "SELL:500@99.00"), ("grow", LEAD_ID, "1000")], 1, 0),
    (
        [
            ("limit", LEAD_ID, "SELL:500@99.00"),
            ("grow", LEAD_ID, "1000"),
            ("hand", LEAD_ID, "SELL:500"),
        ],
        1,
        500,
    ),
    (
        [
            ("limit", LEAD_ID, "SELL:500@99.00"),
            ("grow", LEAD_ID, "1000"),
            ("limit", LEAD_ID, "SELL:10@1.00"),
        ],
        1,
        10,
    ),
    (
        [("limit", LEAD_ID, "SELL:500@99.00"), ("grow", LEAD_ID, "1000"), ("exit", LEAD_ID, "")],
        1,
        1000,
    ),
    (
        [
            ("limit", LEAD_ID, "SELL:400@99.00"),
            ("grow", LEAD_ID, "600"),
            ("limit", LEAD_ID, "SELL:400@99.00"),
            ("grow", LEAD_ID, "600"),
        ],
        2,
        0,
    ),
    # an exit sized in the quote's currency, whose shares are known only at its price: within
    # the long at market, and two thousand shares at a limit of 0.50
    ([("notional", LEAD_ID, "SELL:500")], 1, 0),
    ([("notional", LEAD_ID, "SELL:1000@0.50")], 1, 0),
    # exits that close no more than the position, together or alone
    ([("hand", LEAD_ID, "SELL:500"), ("limit", LEAD_ID, "SELL:500")], 0, 1000),
    ([("orders", f"{LEAD_ID},{LEAD_ID}", "SELL:400,SELL:600")], 0, 1000),
    ([("singles", LEAD_ID, "SELL:999")], 0, 999),
]


@pytest.mark.parametrize(("sends", "dropped", "sold"), PAST_FLAT)
def test_nothing_sent_while_stopped_takes_the_position_past_flat(
    sends: list[tuple[str, str, str]], dropped: int, sold: int, tmp_path: Path
) -> None:
    """LEAD is bought 1,000 at the 04:01 close of 10.00 and the 04:02 close of 10.60 stops the
    week. In that bar's handler the sleeve sends, by hand, sales at market, marketable limits
    at 10.00, sales sized in dollars, and a limit resting at 99.00 that it then modifies to
    10.00 and a larger quantity. Each order goes only when it is sized in shares and closes no
    more than is left once every order of the sleeve still working on the closing side has
    filled — a limit resting read as if it filled, a list's orders before it too — so a sale
    bigger than the position, by any amount, a sale beside a working exit of the whole, and a
    sale in dollars are dropped, each counted, while exits that add up to the position go.
    Every modify that grows a limit is dropped, by any amount, so the order behind it — a sale
    at market, a marketable limit, `submit_exit` or a second grow — is read against the limit
    as it was, and goes where that leaves room. What they leave is flattened at LEAD's next
    close: nothing is ever bought back, and LEAD is never short."""
    stamps = ("04:01", "04:02", "04:03", "04:04")
    lead = list(zip((ny(MONDAY, s) for s in stamps), (10.00, 10.60, 10.70, 10.80), strict=True))
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:02"), LEAD_ID): sends,
    }

    result, seen = card(plan, lead, lagg, tmp_path)

    hit = next(row for row in seen if row.ts == ny(MONDAY, "04:02") and row.name == LEAD_ID)
    assert hit.stopped and hit.dropped == dropped
    made = [(f.side, f.qty, f.px, f.ts_ns) for f in result.run.fills]
    assert sum(qty for side, qty, _, ts in made if ts == ny(MONDAY, "04:02")) == sold
    assert all((side, px) == ("SELL", 10.60) for side, _, px, ts in made if ts == hit.ts)
    later = [fill for fill in made if fill[3] > ny(MONDAY, "04:02")]
    assert later == ([("SELL", 1000 - sold, 10.70, ny(MONDAY, "04:03"))] if sold < 1000 else [])
    rows = [row for row in seen if row.ts >= ny(MONDAY, "04:02") and row.name == LAGG_ID]
    assert rows and all(row.lead >= 0.0 for row in rows)
    assert all(row.lead == 0.0 for row in rows if row.ts >= ny(MONDAY, "04:03"))


@pytest.mark.parametrize(("when", "count"), [("04:01", "2000"), ("04:02", "2000"), ("04:02", "1")])
def test_a_modify_to_a_count_that_is_not_a_quantity_raises_the_engine_s_own_error(
    when: str, count: str, tmp_path: Path
) -> None:
    """LEAD is bought 1,000 at the 04:01 close and the 04:02 close of 10.60 stops the week. A
    sale of 500 resting at 99.00, sent from LAGG's handler before the hit or after it, is
    modified to a count of shares written as a number, larger or smaller. A stopped week
    reads nothing of a quantity that is not a `Quantity`, so the modify is handed to the
    engine as it is, and the engine's typed `quantity` refuses it with the error that names
    the argument, as on a hypothesis with no target."""
    stamps = ("04:01", "04:02", "04:03")
    lead = list(zip((ny(MONDAY, s) for s in stamps), (10.00, 10.60, 10.70), strict=True))
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, when), LAGG_ID): [
            ("limit", LEAD_ID, "SELL:500@99.00"),
            ("count", LEAD_ID, count),
        ],
    }

    with pytest.raises(TypeError, match="Argument 'quantity' has incorrect type"):
        card(plan, lead, lagg, tmp_path)


TEN = tuple(f"04:{minute:02d}" for minute in range(1, 11))
IN_FLIGHT = {
    "grow": (
        (10.00, 10.00, 10.00, 10.00, 10.60, 10.70, 10.80, 10.90, 11.00, 11.10),
        QTY,
        [("BUY", 1000.0, 10.00, "04:04"), ("SELL", 1000.0, 10.80, "04:07")],
    ),
    "notional": (
        (0.50, 0.50, 0.50, 0.50, 0.60, 0.61, 0.62, 0.63, 0.64, 0.65),
        10 * QTY,
        [
            ("BUY", 10000.0, 0.50, "04:04"),
            ("SELL", 6452.0, 0.62, "04:07"),
            ("SELL", 3548.0, 0.65, "04:10"),
        ],
    ),
}
"""Two orders the venue has yet to answer at a hit: LEAD's closes over 04:01 to 04:10, the
long held, and the fills the stopped week ends on."""


def _in_flight(kind: str) -> tuple[dict[Any, Any], list[Point], list[Point]]:
    """LEAD bought at 04:01, an order the venue will not have answered by the hit at the 04:05
    close sent before it at 04:04, and a sale of 500 built by hand at the hit."""
    closes, _, _ = IN_FLIGHT[kind]
    before = (
        [("grow", LEAD_ID, str(QTY))] if kind == "grow" else [("notional", LEAD_ID, "SELL:4000")]
    )
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")]
        + ([("limit", LEAD_ID, "SELL:500@99.00")] if kind == "grow" else []),
        (ny(MONDAY, "04:04"), LEAD_ID): before,
        (ny(MONDAY, "04:05"), LEAD_ID): [("hand", LEAD_ID, "SELL:500")],
    }
    lead: list[Point] = list(zip((ny(MONDAY, s) for s in TEN), closes, strict=True))
    return plan, lead, [(point[0], 11.00) for point in lead]


@pytest.mark.parametrize("kind", sorted(IN_FLIGHT))
def test_an_order_the_venue_has_yet_to_answer_is_read_at_the_most_it_could_close(
    kind: str, tmp_path: Path
) -> None:
    """Under a latency of 150 seconds on one-minute bars, longer than the wait for LEAD's next
    bar, the sleeve buys LEAD at 04:01 and, before the hit, sends an order the venue has yet
    to answer when the week stops at the 04:05 close: a modify at 04:04 growing a sale resting
    at 99.00 to the whole long at 10.00, or a sale at market sized in dollars, which the venue
    turns into shares only as it takes it — 4,000 dollars of a name at 0.62, 6,452 shares of
    the 10,000 held. A sale of 500 the sleeve builds at the hit is dropped, and the flatten,
    due at LEAD's next close, is owed until the venue has answered: it sells what the order
    leaves once that order has filled, never the long less the order's old size. Read at the
    limit's 500 or the 4,000 dollars, the two would have sold 1,500 and 12,452 shares and
    bought the rest back in the stopped week."""
    _, qty, expected = IN_FLIGHT[kind]
    plan, lead, lagg = _in_flight(kind)

    result, seen = card(plan, lead, lagg, tmp_path, latency_ms=150_000.0, qty=qty)

    hit = next(row for row in seen if row.ts == ny(MONDAY, "04:05") and row.name == LEAD_ID)
    assert hit.stopped and hit.dropped == 1
    made = [(f.side, f.qty, f.px, f.ts_ns) for f in result.run.fills if f.instrument_id == LEAD_ID]
    assert made == [(side, q, px, ny(MONDAY, hhmm)) for side, q, px, hhmm in expected]
    assert sum(fill[1] for fill in made if fill[0] == "SELL") == qty
    rows = [row for row in seen if row.name == LAGG_ID and row.ts >= ny(MONDAY, "04:05")]
    assert rows and all(row.lead >= 0.0 for row in rows)


@pytest.mark.parametrize("latency_ms", [10.0, 150_000.0])
@pytest.mark.parametrize("kind", sorted(IN_FLIGHT))
def test_a_replay_reads_an_order_the_venue_has_yet_to_answer_as_the_card_does(
    kind: str, latency_ms: float, tmp_path: Path
) -> None:
    """The live code path runs the same two plans, at a latency the venue answers within before
    the hit and at one it does not: the same intents, fills and equity, element by element."""
    _, qty, _ = IN_FLIGHT[kind]
    plan, lead, lagg = _in_flight(kind)
    made = request(plan, tmp_path / "seen.txt", latency_ms=latency_ms, qty=qty)
    instruments = [instrument(LEAD), instrument(LAGG)]
    groups = [series(LEAD, lead), series(LAGG, lagg)]

    researched = execute(made, instruments, groups)
    replayed = session.run_node(made, instruments, groups).result

    assert sum(fill.qty for fill in researched.run.fills if fill.side == "SELL") == qty
    assert replayed.intents == researched.intents
    assert replayed.run.fills == researched.run.fills
    assert replayed.run.equity == researched.run.equity


def _held(run: CardRun) -> list[tuple[str, float, float, int, float]]:
    """LEAD's fills in the order they were struck, each as `(side, quantity, price, instant,
    position after it)`."""
    position, walked = 0.0, []
    for fill in run.fills:
        if fill.instrument_id == LEAD_ID:
            position += fill.qty if fill.side == "BUY" else -fill.qty
            walked.append((fill.side, fill.qty, fill.px, fill.ts_ns, position))
    return walked


def _both_paths(
    plan: Mapping[tuple[int, str], Sequence[tuple[str, str, str]]],
    lead: Sequence[Point],
    lagg: Sequence[Point],
    tmp_path: Path,
    latency_ms: float,
    **options: Any,
) -> tuple[RunResult, list[Seen]]:
    """The plan's card, asserted to send, fill and end on what its replay on the live code
    path does, element by element."""
    result, seen = card(plan, lead, lagg, tmp_path, latency_ms=latency_ms, **options)
    made = request(plan, tmp_path / "replayed.txt", latency_ms=latency_ms, **options)
    replayed = session.run_node(
        made, [instrument(LEAD), instrument(LAGG)], [series(LEAD, lead), series(LAGG, lagg)]
    ).result
    assert replayed.intents == result.intents
    assert replayed.run.fills == result.run.fills
    assert replayed.run.equity == result.run.equity
    return result, seen


@pytest.mark.parametrize("regrown", ["04:12", "04:14"])
def test_a_regrowth_behind_a_shrink_the_venue_has_yet_to_apply_is_dropped_while_stopped(
    regrown: str, tmp_path: Path
) -> None:
    """Under a latency of 150 seconds on one-minute bars, LEAD is bought 1,000 at 04:01 and
    its 04:07 close of 10.60 stops the week; LEAD does not print again until 04:40. From
    LAGG's handlers the sleeve rests a sale of the whole at 99.00 at 04:07 and shrinks it to
    400 at 04:11, then asks 900 of it at 10.00 — at 04:12, while the shrink is still on its
    way and the order reads 1,000, or at 04:14, once it has landed — and sells 600 at market
    at 04:14, before a regrowth sent at 04:12 has landed. A stopped week drops a modify
    asking more than the least the order holds or has been asked to hold, so the regrowth is
    dropped either way, and the sale of 600 is read beside the order at 400: 1,000 is sold
    and LEAD is never short. Read against the 1,000 the order still held when it was sent,
    the regrowth at 04:12 went, the sale was read beside the 400 the order then held, and
    1,500 was sold. The replay sends and fills what the card does."""
    minutes = [f"04:{minute:02d}" for minute in range(1, 60)]
    lead: list[Point] = [(ny(MONDAY, s), 10.00) for s in minutes[:6]]
    lead += [(ny(MONDAY, "04:07"), 10.60), (ny(MONDAY, "04:40"), 10.70)]
    lagg: list[Point] = [(ny(MONDAY, s), 11.00) for s in minutes]
    plan: dict[tuple[int, str], list[tuple[str, str, str]]] = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:07"), LAGG_ID): [("limit", LEAD_ID, "SELL:1000@99.00")],
        (ny(MONDAY, "04:11"), LAGG_ID): [("resize", LEAD_ID, "400")],
        (ny(MONDAY, "04:14"), LAGG_ID): [],
    }
    plan.setdefault((ny(MONDAY, regrown), LAGG_ID), []).append(("resize", LEAD_ID, "900@10.00"))
    plan[(ny(MONDAY, "04:14"), LAGG_ID)].append(("hand", LEAD_ID, "SELL:600"))

    result, seen = _both_paths(plan, lead, lagg, tmp_path, 150_000.0)

    sold = next(row for row in seen if row.ts == ny(MONDAY, "04:14") and row.name == LAGG_ID)
    assert sold.stopped and sold.dropped == 1
    walked = _held(result.run)
    assert ("SELL", 600.0) in [(side, qty) for side, qty, *_ in walked]
    assert all(position >= 0.0 for *_, position in walked), walked
    assert sum(qty for side, qty, *_ in walked if side == "SELL") == QTY
    assert walked[-1][-1] == 0.0


@pytest.mark.parametrize(("latency_ms", "hit"), [(150_000.0, "04:07"), (210_000.0, "04:08")])
def test_the_flatten_reads_a_shrink_and_a_regrowth_sent_before_the_hit_at_the_most_asked(
    latency_ms: float, hit: str, tmp_path: Path
) -> None:
    """Under a latency longer than a minute, LEAD is bought 1,000 at 04:01, a sale of the
    whole rests at 99.00 from 04:02, is shrunk to 400 at 04:05 and asked 900 at 10.00 at
    04:06, and LEAD's close of 10.60 stops the week at `hit`. Each modify was sent before the
    hit; the flatten, at LEAD's next close, finds the shrink landed and the regrowth still on
    its way, and reads the order at the 900 it was asked, not the 400 it holds: it sells 100,
    the regrowth's 900 fills, and LEAD is never short. Read at 400, the flatten sold 600
    beside the 900, and the stopped week bought 500 back. The replay sends and fills what the
    card does."""
    minutes = [f"04:{minute:02d}" for minute in range(1, 21)]
    rising = minutes.index(hit)
    lead: list[Point] = [
        (ny(MONDAY, s), 10.00 if i < rising else round(10.60 + 0.01 * (i - rising), 2))
        for i, s in enumerate(minutes)
    ]
    lagg: list[Point] = [(point[0], 11.00) for point in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:02"), LEAD_ID): [("limit", LEAD_ID, "SELL:1000@99.00")],
        (ny(MONDAY, "04:05"), LEAD_ID): [("resize", LEAD_ID, "400")],
        (ny(MONDAY, "04:06"), LEAD_ID): [("resize", LEAD_ID, "900@10.00")],
    }

    result, seen = _both_paths(plan, lead, lagg, tmp_path, latency_ms)

    assert min(row.ts for row in seen if row.stopped) == ny(MONDAY, hit)
    walked = _held(result.run)
    assert all(position >= 0.0 for *_, position in walked), walked
    assert [side for side, *_ in walked].count("BUY") == 1
    assert walked[-1][-1] == 0.0


CONTINGENT = """
from pathlib import Path

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import ContingencyType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import LimitOrder, OrderList

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        key = str(bar.bar_type.instrument_id)
        lead, lagg = InstrumentId.from_str("{lead}"), InstrumentId.from_str("{lagg}")
        if bar.ts_event == {bought} and key == "{lead}":
            self.submit_entry(lead, "BUY", qty=1000)
            self.submit_entry(lagg, "BUY", qty=500)
        if bar.ts_event == {stopped} and key == "{lead}":
            ids = [self.order_factory.generate_client_order_id() for _ in range(2)]
            listed = self.order_factory.generate_order_list_id()
            triggers = {{"contingency_type": ContingencyType.OTO, "linked_order_ids": [ids[1]]}}
            legs = ((lagg, 500, triggers), (lead, 1000, {{"parent_order_id": ids[0]}}))
            orders = [
                LimitOrder(
                    trader_id=self.trader_id, strategy_id=self.id, instrument_id=name,
                    client_order_id=ids[index], order_side=OrderSide.SELL,
                    quantity=Quantity.from_int(qty), price=Price.from_str("1.00"), init_id=UUID4(),
                    ts_init=self.clock.timestamp_ns(), order_list_id=listed, **links,
                )
                for index, (name, qty, links) in enumerate(legs)
            ]
            self.submit_order_list(OrderList(listed, orders))
            if {hand}:
                self.submit_order(
                    self.order_factory.market(lead, OrderSide.SELL, Quantity.from_int(1000))
                )
        with Path(self.kanso_config.record).open("a") as out:
            out.write(repr((
                bar.ts_event, key, self.period_stopped, self.held("{lead}"), self.held("{lagg}"),
                self.balance, self._target_anchor, self._target_threshold,
                self._target_dropped, [], len(self.intents),
            )) + "\\n")
"""


@pytest.mark.parametrize(("hand", "latency_ms"), [(True, 0.0), (True, 10.0), (False, 10.0)])
def test_a_contingent_order_is_not_sent_while_stopped(
    hand: bool, latency_ms: float, tmp_path: Path
) -> None:
    """LEAD is bought 1,000 and LAGG 500, and LEAD's close of 10.60 stops the week. In that
    handler the sleeve sends a list built by hand: a sale of LAGG's 500 at a marketable limit,
    one-triggers-other with a sale of LEAD's 1,000 — a child that waits on a parent in another
    name, which is not counted as working until that parent fills — and then, or not, a sale
    of LEAD's 1,000 at market. A stopped week sends no order contingent on another, so the
    list is dropped whole, each order counted; the sale at market goes, the harness flattens
    LAGG, and neither name is ever short. Sent, the child sold LEAD's 1,000 again once LAGG's
    sale filled. The replay sends and fills what the card does."""
    stamps = ("04:01", "04:02", "04:03", "04:04", "04:05", "04:06")
    closes = (10.00, 10.60, 10.70, 10.80, 10.90, 11.00)
    if latency_ms:
        closes = (10.00, 10.00, 10.60, 10.70, 10.80, 10.90)
    stopped = ny(MONDAY, "04:02" if not latency_ms else "04:03")
    lead: list[Point] = list(zip((ny(MONDAY, s) for s in stamps), closes, strict=True))
    lagg: list[Point] = [(point[0], 11.00) for point in lead]
    source = CONTINGENT.format(
        lead=LEAD_ID, lagg=LAGG_ID, bought=ny(MONDAY, "04:01"), stopped=stopped, hand=hand
    )

    result, seen = _both_paths({}, lead, lagg, tmp_path, latency_ms, source=source)

    hit = next(row for row in seen if row.ts == stopped and row.name == LEAD_ID)
    assert hit.stopped and hit.dropped == 2
    walked = _held(result.run)
    assert all(position >= 0.0 for *_, position in walked), walked
    assert sum(qty for side, qty, *_ in walked if side == "SELL") == QTY
    sold = [
        fill.qty
        for fill in result.run.fills
        if fill.instrument_id == LAGG_ID and fill.side == "SELL"
    ]
    assert sum(sold) == 500.0


@pytest.mark.parametrize(("limit", "stops"), [("10.55", True), ("10.65", False)])
def test_an_exit_fill_that_reaches_the_target_stops_the_week(
    limit: str, stops: bool, tmp_path: Path
) -> None:
    """LEAD is bought 1,000 at the 04:01 close of 10.00 and a sale of the whole rests at
    `limit` from 04:02. LEAD's 04:03 bar trades from 10.40 up to 10.60 and closes at 10.40: a
    limit at 10.55 fills inside it, and the week's P&L is the 550 that fill realised, over the
    target of 500, where the close alone marks 400. The week stops at the next point handed
    over, LAGG's bar of the same instant, so the entry LAGG's handler asks there is dropped and
    nothing of LAGG is bought. A limit at 10.65 is out of the bar's reach: nothing stops, and
    the entry goes."""
    lead: list[Point] = [
        (ny(MONDAY, "04:01"), 10.00),
        (ny(MONDAY, "04:02"), 10.00),
        (ny(MONDAY, "04:03"), 10.40, 10.60, 10.40, 10.40),
        (ny(MONDAY, "04:04"), 10.40),
    ]
    lagg: list[Point] = [(point[0], 11.00) for point in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:02"), LEAD_ID): [("limit", LEAD_ID, f"SELL:1000@{limit}")],
        (ny(MONDAY, "04:03"), LAGG_ID): [("entry", LAGG_ID, "BUY")],
    }

    result, seen = card(plan, lead, lagg, tmp_path)

    asked = next(row for row in seen if row.ts == ny(MONDAY, "04:03") and row.name == LAGG_ID)
    assert (asked.stopped, asked.sent) == (stops, [not stops])
    sold = [("SELL", 10.55, ny(MONDAY, "04:03"))] if stops else []
    assert fills(result.run) == [("BUY", 10.00, ny(MONDAY, "04:01")), *sold]
    assert len(fills(result.run, LAGG_ID)) == (0 if stops else 1)
    if stops:
        assert asked.balance - asked.anchor == pytest.approx(550.0)


def test_an_exit_built_by_hand_beside_a_flatten_the_rate_owes_is_dropped(tmp_path: Path) -> None:
    """Under a rate of two requests a minute, a cancel costing two, LEAD is bought at 04:01
    and a buy of ten of each name rests far below the market from 04:02. LEAD's 09:31 close
    of 10.50 stops the week, whose cancels of the two resting buys spend the minute's
    requests, so the flatten due at LEAD's 09:32 close is owed for want of one. A sale of the
    whole long the sleeve builds by hand in that bar's handler is read against the long less
    the exit the rate owes, which closes it all, so it is dropped and counted rather than
    asked of the rate, which would refuse the card for an exit the strategy built itself; the
    owed flatten sells the long once the rate allows."""
    stamps = [f"09:{minute:02d}" for minute in range(31, 45)]
    lead = [(ny(MONDAY, "04:01"), 10.00), (ny(MONDAY, "04:02"), 10.00)] + [
        (ny(MONDAY, stamp), round(10.50 + 0.01 * index, 2)) for index, stamp in enumerate(stamps)
    ]
    lagg = [(ts, 11.00) for ts, _ in lead]
    plan = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(MONDAY, "04:02"), LAGG_ID): [("rest", LEAD_ID, "BUY"), ("rest", LAGG_ID, "BUY")],
        (ny(MONDAY, "09:32"), LEAD_ID): [("hand", LEAD_ID, "SELL")],
    }

    result, seen = card(
        plan, lead, lagg, tmp_path, stated={"orders_per_minute": 2, "requests_per_cancel": 2}
    )

    row = next(row for row in seen if row.ts == ny(MONDAY, "09:32") and row.name == LEAD_ID)
    assert row.stopped and row.dropped == 1
    assert [side for side, _, _ in fills(result.run)] == ["BUY", "SELL"]
    assert result.run.fills[-1].qty == QTY


def _room(filled: float, left: float) -> Decimal:
    """What a stopped week leaves a sale of LEAD to close on a sized sleeve, a name of one
    decimal of size, that holds `filled` with nothing working, `left` of it once the exit the
    rate owes has gone (`_target_room`), the harness's reads of all three stood in for."""
    sizes = SimpleNamespace(size_precision=1)
    reads: Any = SimpleNamespace(
        cache=SimpleNamespace(instrument=lambda _: sizes),
        sized=True,
        _is_clip=lambda _: False,
        _own_filled=lambda _: filled,
        _clip_orders={},
        _working=lambda *_, **__: [],
        _left=lambda *_: left,
        _target_leaves=lambda *_: Decimal(0),
    )
    reads._target_exact = lambda *given: KansoStrategy._target_exact(reads, *given)
    sale = SimpleNamespace(instrument_id=InstrumentId.from_str(LEAD_ID), side=OrderSide.SELL)
    return KansoStrategy._target_room(reads, sale)


def test_a_quantity_summed_in_floats_is_read_at_its_name_s_size_precision() -> None:
    """`0.3 - 0.1` is an ulp short of 0.2 in floats, and `1.1 - 0.9` an ulp over it. A stopped
    week reads a sized sleeve's holding, a sum of fills less its clips, and what the rate owes
    of it at the name's size precision, so the room left for an exit of a fractional holding
    is not an ulp short of what is held, nor of what the owed exit leaves of it: an exit of the
    0.2 held, and of the 0.9 a holding of 1.1 leaves once 0.2 is owed, goes (`_target_room`)."""
    sizes = SimpleNamespace(size_precision=1)
    reads = SimpleNamespace(cache=SimpleNamespace(instrument=lambda _: sizes))

    assert Decimal(repr(0.3 - 0.1)) < Decimal("0.2") < Decimal(repr(1.1 - 0.9))
    read = KansoStrategy._target_exact(reads, InstrumentId.from_str(LEAD_ID), 0.3 - 0.1)  # type: ignore[arg-type]
    assert read == Decimal("0.2")
    assert _room(0.3 - 0.1, 0.3 - 0.1) == Decimal("0.2")
    assert _room(1.1, 0.9) == Decimal("0.9")


def _clipped(backtest: Any, asks: Mapping[int, Clip | Hedge], budget: float = CLIP_BUDGET) -> Any:
    """A sized sleeve that enters DEMO on its third bar, under a target of ten basis points of
    the 100,000 capital whose weeks open on Wednesdays at 19:30 New York: 00:30 UTC on the
    Thursday the engine's clock starts, so the bar at index 29 opens the second week. An
    overlay of `budget` asks for the clip or the hedge leg `asks` keys by the index of the bar
    it is consulted at, once. DEMO's rise of ten cents at index 5 makes 299.70 on its 2,997
    shares: the week stops there, DEMO is flattened at its next print, and the second week
    opens with the sleeve flat."""

    class Asks(Clipper):
        pending = dict(asks)

        def on_data(self, ctx: HookContext) -> Decision:
            ask = self.pending.pop(ctx.ts_event // MINUTE_NS - 1, None)
            if ask is None:
                return Decision.neutral(self.construct)
            if isinstance(ask, Hedge):
                return Decision(hedges=(ask,))
            return Decision(clips=(ask,))

    strategy = Enters(sized())
    cross_section.target(
        strategy,
        PeriodTarget(
            period="week", opens="Wed 19:30", tz="America/New_York", target_bp=10, of="capital"
        ),
    )
    data = [
        point
        for index in range(40)
        for point in (
            bar(DEMO, index, 10.0 if index < 5 else 10.10, volume=10**6),
            bar(HEDGE, index, 20.0, volume=10**6),
        )
    ]
    overlays = [clipper(Asks, budget=budget)]
    return backtest(strategy, overlays, data=data, instruments=(DEMO, HEDGE)).strategy


@pytest.mark.parametrize(
    "asked", [Clip("HEDGE.XNAS", "BUY"), Clip("HEDGE.XNAS", "SELL"), Clip("DEMO.XNAS", "BUY")]
)
def test_a_clip_dropped_while_stopped_holds_nothing_after(backtest: Any, asked: Clip) -> None:
    """An overlay's clip asked while the week is stopped opens, so it is dropped unsent, and
    taken off the clip ledger as the rate's drop takes one: it never went, so it holds nothing.
    The next week, the sleeve flat, a clip on the same side, on the other or in another name is
    placed exactly as on a run that never asked for the first — none placing nothing, and none
    refused as one more beside a clip still read as held."""
    stopped = _clipped(backtest, {8: Clip("HEDGE.XNAS", "BUY"), 34: asked})
    control = _clipped(backtest, {34: asked})

    assert (stopped._target_dropped, control._target_dropped) == (1, 0)
    assert stopped.intents == control.intents
    assert any(
        intent.instrument_id == asked.instrument and intent.ts_event >= 30 * MINUTE_NS
        for intent in stopped.intents
    )


@pytest.mark.parametrize("rise", [10.10, 10.02])
def test_an_overlay_s_finer_bar_that_reaches_the_target_stops_the_week_before_its_clip(
    backtest: Any, rise: float
) -> None:
    """A sized sleeve enters DEMO's 2,997 shares at 10.00 on its third one-minute bar, under a
    target of ten basis points of the 100,000 capital, and an attached overlay reads one-second
    bars, which the sleeve's own handler never sees. DEMO's first second bar after the fifth
    minute closes at `rise`, and the overlay asks for a clip of HEDGE there. A rise to 10.10
    marks 299.70 and reaches the target at that bar, so the week stops before the overlay is
    consulted and the clip, which opens, is dropped; DEMO is flattened at the next minute bar.
    A rise to 10.02 marks 59.94, short of it, and the clip goes."""

    class Once(Clipper):
        script = {1: (Clip("HEDGE.XNAS", "BUY"),)}

    strategy = Enters(sized(extra_resolutions=("1s",)))
    cross_section.target(
        strategy,
        PeriodTarget(
            period="week", opens="Wed 19:30", tz="America/New_York", target_bp=10, of="capital"
        ),
    )
    minutes = [
        point
        for index in range(10)
        for point in (bar(DEMO, index, 10.0, volume=10**6), bar(HEDGE, index, 20.0, volume=10**6))
    ]
    seconds = [
        second_bar(DEMO, 0, rise, origin_ns=int(minutes[9].ts_event)),
        second_bar(HEDGE, 0, 20.0, origin_ns=int(minutes[9].ts_event)),
    ]
    data = [*minutes[:10], *seconds, *minutes[10:]]

    run = backtest(strategy, [clipper(Once)], data=data, instruments=(DEMO, HEDGE)).strategy

    reaches = rise == 10.10
    assert run._target_dropped == (1 if reaches else 0)
    hedged = [intent.side for intent in run.intents if intent.instrument_id == "HEDGE.XNAS"]
    assert hedged == ([] if reaches else ["BUY"])


def test_a_held_clip_is_taken_off_while_stopped_and_a_hedge_leg_that_opens_is_not_sent(
    backtest: Any,
) -> None:
    """A clip the overlay bought before the hit is its own, and its `FLAT` asked while the week
    is stopped closes no more than the clip holds, so it goes. A hedge leg an overlay with no
    budget asks while stopped opens, read against the sleeve's own position without its clips,
    so it is dropped and counted; the same leg the next week goes."""
    clipped = _clipped(backtest, {3: Clip("HEDGE.XNAS", "BUY"), 8: Clip("HEDGE.XNAS", "FLAT")})
    hedged = _clipped(
        backtest, {8: Hedge("HEDGE.XNAS", 10.0), 34: Hedge("HEDGE.XNAS", 10.0)}, budget=0.0
    )

    assert clipped._target_dropped == 0
    assert [
        (intent.side, intent.qty, intent.ts_event // MINUTE_NS - 1)
        for intent in clipped.intents
        if intent.instrument_id == "HEDGE.XNAS"
    ] == [("BUY", HEDGE_CLIP, 3), ("SELL", HEDGE_CLIP, 8)]
    assert hedged._target_dropped == 1
    assert [
        (intent.side, intent.qty, intent.ts_event // MINUTE_NS - 1)
        for intent in hedged.intents
        if intent.instrument_id == "HEDGE.XNAS"
    ] == [("BUY", 10.0, 34)]


@pytest.mark.parametrize(
    ("friday", "opening", "before"),
    [
        (date(2024, 3, 8), date(2024, 3, 11), "03:30"),
        (date(2024, 11, 1), date(2024, 11, 4), "03:30"),
        (date(2024, 2, 16), date(2024, 2, 20), None),
    ],
)
def test_a_week_opens_at_new_york_s_04_00_across_a_change_of_clocks_and_on_a_closed_monday(
    friday: date, opening: date, before: str | None, tmp_path: Path
) -> None:
    """Friday's last close stops the week. Across a change of clocks the next week opens at
    Monday's 04:00 by the clock New York reads that day — 08:00 UTC after the March change,
    09:00 after November's — so LAGG's point at 03:30 that morning is still in the stopped week,
    read against its anchor, and at 04:01 LEAD is flattened, the stop lifts and LAGG's entry
    goes, the new week anchored at Friday's last point. A Monday the market is closed,
    2024-02-19, opens its week all the same, and Tuesday's first point is in it."""
    lead = [
        (ny(friday, "04:01"), 10.00),
        (ny(friday, "19:59"), 10.50),
        (ny(opening, "04:01"), 10.10),
    ]
    lagg = [(ts, 11.00) for ts, _ in lead]
    if before is not None:
        lagg.insert(2, (ny(opening, before), 11.00))
    plan = {
        (ny(friday, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY")],
        (ny(opening, "04:01"), LAGG_ID): [("entry", LAGG_ID, "BUY")],
    }
    window = (friday - timedelta(days=4), opening + timedelta(days=3))

    result, seen = card(plan, lead, lagg, tmp_path, window=window)

    old = [(row.stopped, row.anchor) for row in seen if row.ts < ny(opening, "04:00")]
    assert old == [(False, CAPITAL)] * 2 + [(True, CAPITAL)] * (3 if before else 2)
    new = [(row.stopped, row.anchor) for row in seen if row.ts >= ny(opening, "04:00")]
    assert new == [(False, 100_500.0)] * 2
    assert fills(result.run) == [
        ("BUY", 10.00, ny(friday, "04:01")),
        ("SELL", 10.10, ny(opening, "04:01")),
    ]
    assert fills(result.run, LAGG_ID) == [("BUY", 11.00, ny(opening, "04:01"))]


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
