"""Entry timing: what a run's trades earned against the same trades placed at other times.

Being long a rising index earns its drift whenever the position is held, so a book that
holds it some of the time scores above zero without having timed anything. The question a
certificate can put to that is how the subject's trades compare with the very same trades
moved in time — the same instruments, sides, opening notional, hold in sessions and New York
time of day — priced and costed by the runner's arithmetic. A drift is earned at any time,
so it is earned by the moved trades too, and only what the timing added is left between the
two. This module is that arithmetic, and only that: pure functions of the run, of the prices
`SessionPrices` holds and of nothing else, so the gate that reads it (`entry_timing`) is
reproduced bit for bit from its inputs.

**The null is the whole schedule, circularly shifted by whole sessions.** The sessions are the
New York dates on which any of the subject's instruments has a price point in the window, in
order — `S` of them, each the date `kanso.nautilus.clock.session_of` gives a point, so a daily
bar stamped at the next New York midnight is its own session's, and the evening before the
window's first date, which a window opening at UTC midnight delivers, is among them where it
holds a point. A fill is placed on the session
its price was struck in and at the New York wall-clock time it was struck at (`Fill.price_ts_ns`,
else `Fill.ts_ns`). Offset `k` moves every fill of every trade to session `(i + k) mod S` at that
same time of day, so the schedule keeps its spacing, its clustering and the exposure it held at
once, translated in time. Every offset in `[h, S − h]` is one draw, `h` the longest closed
trade's hold in sessions plus one, so no draw overlaps its own original; there are
`D = S − 2h + 1` of them and no random number is drawn. A trade whose shifted span would cross
the end of the window is dropped from that draw and counted, and so is one with a leg no price
is held for on the session it lands on. At offset 0 the same arithmetic drops a trade whose own
instants hold no price, which the gate fails rather than judging the rest.

**A moved fill is struck where the market printed, at the size its trade opened at.** Its price
is the last reading of its instrument at or before the moved instant on that same session — a
bar's close where the window holds the instrument's bars, else a print, else the touch of a
quote (a buy at the ask, a sale at the bid) — and never one carried from an earlier session,
since a market order placed before a session's first print fills at none of the last one's. One
factor per trade scales every fill of it, its opening notional — the sum over its opening fills
of quantity, price and multiplier — over the same sum at the moved prices, so the moved trade
opens the same notional and closes flat. A split between two of a trade's fills is undone
before anything is scaled: each fill is restated onto the share basis its trade opened in
(`kanso.nautilus.splits.restating`), the recorded fills by the instants they filled at and the
moved ones by the instants they land on, so a draw that straddles a one-for-five is no fivefold
move.

**Costs are the runner's, charged as it charges a fill struck at that close.** Each moved fill
pays `kanso.nautilus.costs.fill_cost` under the run's venue model: half the spread — the dated
row at the moved instant, the quote's own under `spread: quotes`, else the stated width — the
slippage, the commission, its side's sell fees and `slippage_ticks` of its increment, carrying no
limit. A moved fill is struck at a bar's close and never rested on a book, so it is never a
maker's, and whether it was struck at the touch is read off it against its instrument's last
quote at or before the moved instant, carried from an earlier session as the runner carries one,
where the window holds quotes: false where it holds none. On a venue that matches a level-two
book every fill is struck at the touch, as the runner counts every fill there, and charged no
spread and no slippage in basis points. The subject's own value is the same
arithmetic at offset 0 — its own instants, repriced and costed alike — so the comparison is like
for like, and the runner's own figure, struck with the fills' real flags, is kept beside it.

**What the null keeps of a period target is its cuts.** It is built from the subject's closed
trades, whose holds already end where its `period_target` flattened them. A moved trade keeps the
cut its original took; it does not get the one its own moved week's target would have made. That
is the approximation, and it is why the gate stands beside a weekly stop where `benchmark: hold`
cannot.

The statistic is the mean net profit per trade in basis points of the capital, over the trades a
draw kept, and `p = (1 + #{draws ≥ observed}) / (1 + D)`. A draw that kept no trade cannot be
shown to be beaten, so it is counted among those at or above the observed value. A trade's
profit is its fills' flows less what `fill_cost` charges them and nothing else: no funding of a
perpetual, no cash in lieu of a split, at offset 0 as in every draw, so the runner's own figure,
which carries both, can differ from the subject's value by them.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from math import fsum
from typing import Final
from zoneinfo import ZoneInfo

from kanso.criteria.run import BPS, NS_PER_SECOND, CardRun, Fill, Trade, midnight_ns
from kanso.nautilus.costs import (
    at_touch,
    dated_half_spread,
    dated_rows,
    fill_cost,
    fixed_half_spread,
    quote_half_spread,
    tick_slip,
)
from kanso.nautilus.splits import Split, restating
from kanso.schemas.venue import DatedSpread

__all__ = [
    "BAR",
    "DAY_NS",
    "PRINT",
    "QUOTE",
    "SOURCES",
    "Draw",
    "Fold",
    "Leg",
    "Placed",
    "PriceKey",
    "Reading",
    "SessionPrices",
    "draw",
    "instant_at",
    "keys_of",
    "offsets",
    "place",
    "runner_bps_per_trade",
    "wall_of",
]

NEW_YORK: Final = ZoneInfo("America/New_York")
"""The clock a session and a time of day are read on, as `kanso.nautilus.clock` reads them."""

DAY_NS: Final = 86_400 * NS_PER_SECOND
"""A day of wall-clock time: a time of day lies in `(0, DAY_NS]` past its session's midnight."""

_HOUR_NS: Final = 3_600 * NS_PER_SECOND

BAR: Final = "bar"
PRINT: Final = "print"
QUOTE: Final = "quote"
SOURCES: Final = (BAR, PRINT, QUOTE)
"""The series an instrument can be priced from, in the order one is chosen: its bars where the
window holds any, else its prints, else the touch of its quotes."""

PriceKey = tuple[str, int]
"""An instrument id and a New York wall-clock time of day, in nanoseconds past the midnight that
opens its session, in `(0, DAY_NS]`: where a fill is moved to on every session."""


@lru_cache(maxsize=1 << 16)
def _hour(index: int) -> tuple[date, int]:
    """The New York date and wall-clock nanoseconds past its midnight at which UTC hour `index`
    opens. New York's offsets are whole hours, so one UTC hour is one New York hour and holds
    one date."""
    local = datetime.fromtimestamp(index * 3_600, tz=NEW_YORK)
    return local.date(), ((local.hour * 60 + local.minute) * 60 + local.second) * NS_PER_SECOND


def wall_of(ts_ns: int) -> tuple[date, int]:
    """The session an instant belongs to and its time of day there: the New York date of the
    instant a nanosecond before it (`kanso.nautilus.clock.session_of`), and the wall-clock time
    of the instant itself past that date's midnight, in `(0, DAY_NS]`. A point stamped at a
    session's close belongs to it, and a daily bar stamped at the next midnight reads `DAY_NS`."""
    index, rest = divmod(ts_ns - 1, _HOUR_NS)
    day, opened = _hour(index)
    return day, opened + rest + 1


def instant_at(day: date, wall_ns: int) -> int:
    """The instant the New York clock reads `wall_ns` past the midnight that opens `day`: the
    inverse of `wall_of`, read in wall-clock time, so 15:30 is 15:30 on a day a change of
    clocks makes 23 or 25 hours long."""
    seconds, rest = divmod(wall_ns, NS_PER_SECOND)
    local = datetime.combine(day, time(), tzinfo=NEW_YORK) + timedelta(seconds=seconds)
    return int(local.timestamp()) * NS_PER_SECOND + rest


@dataclass(frozen=True, slots=True)
class Reading:
    """One price point as the null reads it: its instant and its two prices — a bar's close or a
    print's price as both, a quote's bid and ask apart."""

    ts_ns: int
    bid: float
    ask: float

    def price(self, *, sell: bool) -> float:
        """What a fill on this side is struck at: the bid for a sale and the ask for a buy, the
        one price of a bar or a print either way."""
        return self.bid if sell else self.ask

    @property
    def quote(self) -> tuple[float, float]:
        """The reading as the `(bid, ask)` a touch and a quoted spread are read against."""
        return self.bid, self.ask


@dataclass(frozen=True)
class SessionPrices:
    """What the null reads of the window: its sessions, and for every key, per session, the
    reading a moved fill is struck at and the quote it is costed against.

    `sessions` are the New York dates holding a price point of any of the subject's instruments
    in the window, in order. `source` is the series each instrument is priced from (`SOURCES`),
    absent for one the window holds no bar, print or quote of. `marks[key][i]` is that series'
    last reading at or before the key's time on session `i`, and on that session alone; `quotes`
    is the instrument's last quote at or before it, from that session or any before it in the
    window, absent for an instrument the window holds no quote of. `schedules` are the splits each
    instrument's definition carries, which the window's definitions hand certification and
    nothing else does.
    """

    sessions: tuple[date, ...]
    source: Mapping[str, str] = field(default_factory=dict)
    marks: Mapping[PriceKey, tuple[Reading | None, ...]] = field(default_factory=dict)
    quotes: Mapping[PriceKey, tuple[Reading | None, ...]] = field(default_factory=dict)
    schedules: Mapping[str, tuple[Split, ...]] = field(default_factory=dict)


class Fold:
    """`SessionPrices` folded from a window's points one at a time, holding a reading per key per
    session and per stretch between two keys' times, and nothing else of the window.

    The window is the span of instants a run of it delivers, from the UTC midnight its first
    date opens at to the one after its last, and a point outside it belongs to no session: a
    warmup prefix's. The evening before the window's first New York date is inside it — a window
    opening at UTC midnight delivers it, and a daily bar stamped at the first date's New York
    midnight among it — so it is that date's session, as the run that traded on it had it.

    Each point lands in the stretch of its session that ends at the first key at or after it,
    and the stretch keeps the latest point it was handed, so the readings are right whatever
    order the points arrive in. A key ends its stretch at the instant its time of day reads on
    that session's New York clock (`instant_at`), or at a later key's where that is earlier, so
    no reading is later than the instant a fill is moved to — on the night the clocks go back,
    a point of the repeated hour is never read at a key's first pass through it.
    """

    def __init__(
        self,
        keys: Iterable[PriceKey],
        window: tuple[date, date],
        schedules: Mapping[str, tuple[Split, ...]] | None = None,
    ) -> None:
        self.window = window
        self.schedules = dict(schedules or {})
        self._opens = midnight_ns(window[0])
        self._closes = midnight_ns(window[1]) + DAY_NS
        walls: dict[str, set[int]] = {}
        for instrument, wall in keys:
            walls.setdefault(instrument, set()).add(wall)
        self._walls = {name: sorted(held) for name, held in walls.items()}
        self._sessions: set[date] = set()
        self._kinds: dict[str, set[str]] = {}
        self._slots: dict[tuple[str, str], dict[date, list[Reading | None]]] = {}
        self._ends: dict[tuple[str, date], list[int]] = {}

    def take(self, kind: str, instrument: str, ts_event: int, bid: float, ask: float) -> None:
        """Fold one point: a bar's close or a print's price as both `bid` and `ask`."""
        if not self._opens <= ts_event < self._closes:
            return
        day, _ = wall_of(ts_event)
        self._sessions.add(day)
        self._kinds.setdefault(instrument, set()).add(kind)
        walls = self._walls.get(instrument)
        if walls is None:
            return
        days = self._slots.setdefault((kind, instrument), {})
        slots = days.get(day)
        if slots is None:
            slots = days[day] = [None] * (len(walls) + 1)
        slot = bisect_left(self._stretches(instrument, day, walls), ts_event)
        held = slots[slot]
        if held is None or ts_event >= held.ts_ns:
            slots[slot] = Reading(ts_event, bid, ask)

    def _stretches(self, instrument: str, day: date, walls: Sequence[int]) -> list[int]:
        """The instant each of an instrument's keys ends its stretch on a session: its time of
        day on that session's clock, or a later key's where that is earlier — a time the clocks
        skip reads past the next one — so the ends never fall."""
        found = self._ends.get((instrument, day))
        if found is None:
            found = [instant_at(day, wall) for wall in walls]
            for index in range(len(found) - 2, -1, -1):
                found[index] = min(found[index], found[index + 1])
            self._ends[(instrument, day)] = found
        return found

    def prices(self) -> SessionPrices:
        """Every key's readings, session by session."""
        sessions = tuple(sorted(self._sessions))
        source = {
            name: next(kind for kind in SOURCES if kind in kinds)
            for name, kinds in self._kinds.items()
            if name in self._walls
        }
        marks: dict[PriceKey, tuple[Reading | None, ...]] = {}
        quotes: dict[PriceKey, tuple[Reading | None, ...]] = {}
        for name, walls in self._walls.items():
            kind = source.get(name)
            if kind is not None:
                marks.update(self._read(name, kind, walls, sessions, carried=False))
            if QUOTE in self._kinds.get(name, ()):
                quotes.update(self._read(name, QUOTE, walls, sessions, carried=True))
        return SessionPrices(sessions, source, marks, quotes, dict(self.schedules))

    def _read(
        self, name: str, kind: str, walls: Sequence[int], sessions: Sequence[date], *, carried: bool
    ) -> dict[PriceKey, tuple[Reading | None, ...]]:
        """The latest reading at or before each key's time on each session: within the session
        alone, or `carried` from the sessions before it."""
        days = self._slots.get((kind, name), {})
        found: dict[int, list[Reading | None]] = {wall: [] for wall in walls}
        last: Reading | None = None
        for day in sessions:
            slots = days.get(day, [None] * (len(walls) + 1))
            running = last if carried else None
            for index, wall in enumerate(walls):
                if slots[index] is not None:
                    running = slots[index]
                found[wall].append(running)
            if slots[-1] is not None:
                running = slots[-1]
            last = running
        return {(name, wall): tuple(readings) for wall, readings in found.items()}


def keys_of(run: CardRun) -> tuple[PriceKey, ...]:
    """Every key a run's closed trades need: each fill's instrument and the time of day its price
    was struck at, in order."""
    return tuple(
        sorted(
            {
                (fill.instrument_id, wall_of(_struck(fill))[1])
                for t in run.trades
                for fill in t.fills
            }
        )
    )


def _struck(fill: Fill) -> int:
    """The instant a fill's price was struck at: the one the runner kept, else its own."""
    return fill.ts_ns if fill.price_ts_ns is None else fill.price_ts_ns


@dataclass(frozen=True, slots=True)
class Leg:
    """One fill of a placed trade: where it sits on the session grid, which way it went, and its
    quantity and price restated onto the share basis its trade opened in."""

    session: int
    wall: int
    instrument: str
    sell: bool
    opening: bool
    qty: float
    px: float
    multiplier: float
    tick: float


@dataclass(frozen=True, slots=True)
class Placed:
    """A closed trade on the session grid: its legs in fill order, the first and last sessions it
    spans, and its opening notional. `legs` is empty for a trade with a fill on a date that holds
    no session, which no draw can place."""

    legs: tuple[Leg, ...]
    first: int
    last: int
    notional: float

    @property
    def hold(self) -> int:
        """How many sessions after its first the trade's last fill is."""
        return self.last - self.first


def place(trade: Trade, prices: SessionPrices) -> Placed:
    """A trade on the session grid of `prices`, every fill restated onto its opening basis."""
    index = {day: at for at, day in enumerate(prices.sessions)}
    fills = sorted(trade.fills, key=lambda fill: fill.ts_ns)
    schedule = prices.schedules.get(trade.instrument_id, ())
    opened = fills[0].ts_ns
    opening_side = fills[0].side
    legs: list[Leg] = []
    for fill in fills:
        day, wall = wall_of(_struck(fill))
        session = index.get(day)
        if session is None:
            return Placed((), 0, 0, 0.0)
        factor = restating(schedule, opened, fill.ts_ns)
        legs.append(
            Leg(
                session=session,
                wall=wall,
                instrument=fill.instrument_id,
                sell=fill.side == "SELL",
                opening=fill.side == opening_side,
                qty=abs(fill.qty) / factor,
                px=fill.px * factor,
                multiplier=fill.multiplier,
                tick=fill.tick,
            )
        )
    notional = fsum(leg.qty * leg.px * leg.multiplier for leg in legs if leg.opening)
    return Placed(
        tuple(legs),
        min(leg.session for leg in legs),
        max(leg.session for leg in legs),
        notional,
    )


def offsets(placed: Sequence[Placed], sessions: int) -> tuple[int, range]:
    """`h` and every admissible offset `[h, S − h]`: none overlaps a trade with its original."""
    hold = max((trade.hold for trade in placed if trade.legs), default=0)
    floor = hold + 1
    return floor, range(floor, sessions - floor + 1)


@dataclass(frozen=True)
class _Costs:
    """The cost keys of a run's venue model, as the runner reads them."""

    commission_bps: float
    slippage_bps: float
    commission_per_share: float
    maker_bps: float | None
    maker_per_share: float | None
    sell_fee_bps: float
    sell_fee_per_share: float
    slippage_ticks: float
    spread: str
    fixed_half: float
    dated: tuple[DatedSpread, ...]

    @classmethod
    def of(cls, venue_model: Mapping[str, object]) -> _Costs:
        stated = venue_model.get("costs")
        costs: Mapping[str, object] = stated if isinstance(stated, Mapping) else {}

        def rate(name: str) -> float:
            value = costs.get(name)
            return float(value) if isinstance(value, int | float) else 0.0

        def optional(name: str) -> float | None:
            value = costs.get(name)
            return float(value) if isinstance(value, int | float) else None

        return cls(
            commission_bps=rate("commission_bps"),
            slippage_bps=rate("slippage_bps"),
            commission_per_share=rate("commission_per_share"),
            maker_bps=optional("maker_bps"),
            maker_per_share=optional("maker_per_share"),
            sell_fee_bps=rate("sell_fee_bps"),
            sell_fee_per_share=rate("sell_fee_per_share"),
            slippage_ticks=rate("slippage_ticks"),
            spread=str(costs.get("spread", "")),
            fixed_half=fixed_half_spread(optional("fixed_bps")),
            dated=dated_rows(venue_model),
        )

    def half(self, instrument: str, at_ns: int, quote: Reading | None) -> float:
        """Half the spread a fill priced at `at_ns` pays, as the runner's `Marks.half` reads it."""
        if self.spread == "dated":
            return dated_half_spread(self.dated, instrument, at_ns)
        if self.spread != "quotes" or quote is None:
            return self.fixed_half
        return quote_half_spread(*quote.quote)


@dataclass(frozen=True)
class Draw:
    """One offset's trades as the null priced them: each kept trade's net profit and its fills'
    costs, in the order of the run's trades, and how many it dropped."""

    offset: int
    pnl: tuple[float, ...]
    costs: tuple[tuple[float, ...], ...]
    dropped: int

    @property
    def kept(self) -> int:
        """How many trades the draw priced."""
        return len(self.pnl)

    def bps_per_trade(self, capital: float) -> float | None:
        """The mean net profit per kept trade in basis points of `capital`; `None` for none."""
        if not self.pnl:
            return None
        return fsum(self.pnl) / capital * BPS / len(self.pnl)


def draw(
    placed: Sequence[Placed],
    prices: SessionPrices,
    venue_model: Mapping[str, object],
    k: int,
    *,
    levelled: bool = False,
) -> Draw:
    """Every placed trade moved `k` sessions on, priced and costed; offset 0 is the subject's own
    instants, repriced as every draw is. `levelled` is a venue that matches a level-two book,
    where the runner counts every fill as struck at the touch, and so does the null."""
    costs = _Costs.of(venue_model)
    total = len(prices.sessions)
    pnl: list[float] = []
    charged: list[tuple[float, ...]] = []
    dropped = 0
    for trade in placed:
        priced = _priced(trade, prices, costs, k, total, levelled=levelled)
        if priced is None:
            dropped += 1
            continue
        net, fees = priced
        pnl.append(net)
        charged.append(fees)
    return Draw(k, tuple(pnl), tuple(charged), dropped)


def _priced(
    trade: Placed, prices: SessionPrices, costs: _Costs, k: int, total: int, *, levelled: bool
) -> tuple[float, tuple[float, ...]] | None:
    """One trade moved `k` sessions on: its net profit and its fills' costs, or `None` where it
    is dropped — it holds no session, it crosses the window's end, or a leg has no price."""
    if not trade.legs or trade.first + k < total <= trade.last + k:
        return None
    struck: list[tuple[Leg, int, float, Reading | None]] = []
    for leg in trade.legs:
        session = (leg.session + k) % total
        readings = prices.marks.get((leg.instrument, leg.wall))
        mark = None if readings is None else readings[session]
        if mark is None:
            return None
        quoted = prices.quotes.get((leg.instrument, leg.wall))
        quote = None if quoted is None else quoted[session]
        at = instant_at(prices.sessions[session], leg.wall)
        struck.append((leg, at, mark.price(sell=leg.sell), quote))
    schedule = prices.schedules.get(trade.legs[0].instrument, ())
    opened = struck[0][1]
    factors = [restating(schedule, opened, at) for _, at, _, _ in struck]
    moved = fsum(
        leg.qty * px * factor * leg.multiplier
        for (leg, _, px, _), factor in zip(struck, factors, strict=True)
        if leg.opening
    )
    if moved <= 0.0:
        return None
    scale = trade.notional / moved
    flows: list[float] = []
    fees: list[float] = []
    for (leg, at, px, quote), factor in zip(struck, factors, strict=True):
        qty = scale * leg.qty * factor
        notional = qty * px * leg.multiplier
        fees.append(
            fill_cost(
                notional,
                qty,
                costs.commission_bps,
                costs.slippage_bps,
                costs.half(leg.instrument, at, quote),
                costs.maker_bps,
                costs.commission_per_share,
                maker=False,
                sell=leg.sell,
                sell_fee_bps=costs.sell_fee_bps,
                sell_fee_per_share=costs.sell_fee_per_share,
                maker_per_share=costs.maker_per_share,
                slip=tick_slip(costs.slippage_ticks, leg.tick, px, None, sell=leg.sell),
                multiplier=leg.multiplier,
                touched=levelled
                or at_touch(px, sell=leg.sell, quote=None if quote is None else quote.quote),
            )
        )
        flows.append(notional if leg.sell else -notional)
    return fsum(flows) - fsum(fees), tuple(fees)


def runner_bps_per_trade(run: CardRun) -> float:
    """The runner's own mean net profit per closed trade, in basis points of the capital: what
    the fills' real flags were charged, kept beside the null's repricing of them."""
    return fsum(trade.pnl_net for trade in run.trades) / run.capital * BPS / len(run.trades)
