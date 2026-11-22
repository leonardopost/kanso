"""What one backtest produced, and how it is cut into folds.

A `CardRun` is the single subject every objective and every gate is evaluated on. It is
extracted by the runner from its own fills rather than read from the engine's analyser,
so one definition of a return, a trade and an equity curve serves cards, certification
gates, composition expectations and the realised paper and live objectives alike.

The shapes are deliberately flat and immutable: a `Fill` is one execution with the cost
the runner applied to it, a `Trade` is a closed position with the fills that made it, a
`FundingPayment` is one perpetual's funding settlement on what was held at it, an
`Adjustment` is one split the venue applied to what was held, and a `CardRun` is a window's
worth of all four plus the period-end equity curve. A position
still open when the window closes keeps its unrealised profit — and what it paid and was
paid in funding — in `equity` but is not a trade, because a trade is a closed position.

**Funding is not a fill cost.** `Fill.cost` and `Trade.cost` are what the fills paid, and
nothing else, so `cost_stress` and `cost_scenario`, which re-price recorded fills, leave
funding exactly as it was booked; it is in `Trade.pnl_net`, in the returns and in the equity
curve, which is why a Sharpe, an edge and a net edge all read one post-funding number.

`folds(n)` cuts the window into `n` contiguous, equal calendar spans and restricts the
run to each. The cut is by calendar, not by observation count, so a fold's length does not
depend on how busy it was; a period, a trade and a fill belong to the fold containing the
instant they are stamped at — a period by its end, a trade by its close, a fill by its
execution. Folds are what the keep rule averages over and what the walk-forward gates
read, so this arithmetic is the noise floor of the whole loop.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta

from kanso.errors import ValidationError
from kanso.schemas import PeriodStops, parse_duration

NS_PER_SECOND = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_SECOND
EPOCH = date(1970, 1, 1)
BPS = 10_000.0


def midnight_ns(day: date) -> int:
    """The UTC instant a calendar day opens, in nanoseconds since the epoch."""
    return (day - EPOCH).days * NS_PER_DAY


def day_of(ts_ns: int) -> date:
    """The UTC calendar day an instant falls in."""
    return EPOCH + timedelta(days=ts_ns // NS_PER_DAY)


@dataclass(frozen=True)
class Held:
    """What one instrument was worth at one period end, marked at that period's price.

    The equity curve already computes this — quantity held times the last mark times the
    contract multiplier — and used to sum it into a scalar and throw the parts away. It is
    kept because it is the only quantity in a run that answers "how much was this position
    worth while it was open", which is what an operator means by a position size and is
    what neither `Fill.notional` (traded value, struck at one price) nor `Trade.notional`
    (opening value, a cost basis) says. One entry per instrument per period, and only for
    an instrument actually held: a flat instrument is not a position.

    `notional` is an absolute, so `below_zero` says what it cannot: that the mark the runner
    valued the position at was under zero — a future's, say — and the position's worth is
    `qty` times the notional over `abs(qty)`, turned negative. A holding recorded before
    runs said so reads as at or above zero.
    """

    ts_ns: int
    instrument_id: str
    qty: float
    notional: float
    below_zero: bool = False


@dataclass(frozen=True)
class Fill:
    """One execution, with the cost the runner applied to it once, in the extraction.

    `maker` is whether the venue reported it as a maker's — a limit that rested on the book
    and was filled at its own price — which is what a venue model's `maker_bps` charges, and
    what a model re-applied to recorded fills needs to know. It is recorded whatever the
    model charged: a maker's fill under a model without `maker_bps` paid what any fill pays,
    and is still a maker's. A fill the venue reported no liquidity side for is not one.

    `multiplier` is the instrument's contract multiplier at the fill — one for a share, the
    contract size for a future or an option — and is what turns a quantity at a price into
    the currency it moved. A fill recorded before it was kept reads as one, a share's; a
    run struck on a multiplied instrument before then is re-run before a cost model is
    re-applied to its record.

    `tick` is the instrument's price increment and `limit` the limit price of the order that
    filled, `None` for an order that carried none, a market order's: what a venue model's
    `slippage_ticks` charges a taker's fill per share, and is capped by, so a model re-applied
    to the record charges it as the runner did. A fill recorded before they were kept reads as
    an increment of zero, which no `slippage_ticks` charges; such a run is re-run before a
    model that states one is re-applied to it.

    `touched` is whether the fill was struck at the touch, read off the fill itself: a buy at
    or above the ask of the last quote of its name at or before it, a sale at or below that
    quote's bid, or any fill on a venue that matches a level-two book. Its price carries the
    spread — a taker bought the ask or sold the bid, or walked past it — so it is charged
    neither the half-spread nor `slippage_bps`, by the runner and by a model re-applied to the
    record alike; a fill struck at a print or a bar's close inside the quote, or in a name with
    no quote before it, pays both. A fill recorded before the field was kept reads as not
    touched, which is how it was charged.

    `price_ts_ns` is, under a dated spread, the instant the fill's price was struck at — the
    `ts_event` of its name's last point the venue had applied when it filled, a bar's close or
    a print's own stamp (`kanso.nautilus.struck`), which under a latency can be another part of
    the session or another day than the fill's own — whose row and part of the session it was
    charged (`kanso.nautilus.costs.dated_half_spread`). `None` where no dated spread was
    charged, and on every fill recorded before the field was kept: no row was read at an
    instant, so none was kept, and `None` is not the fill's own `ts_ns`, which under a latency
    is not the instant its price was struck at.
    """

    ts_ns: int
    instrument_id: str
    side: str
    qty: float
    px: float
    cost: float
    maker: bool = False
    multiplier: float = 1.0
    tick: float = 0.0
    limit: float | None = None
    touched: bool = False
    price_ts_ns: int | None = None

    @property
    def notional(self) -> float:
        """Absolute traded value in the account currency — quantity, price and multiplier —
        the base a cost rate is charged on and a participation limit is measured against."""
        return abs(self.qty) * self.px * self.multiplier


@dataclass(frozen=True)
class FundingPayment:
    """One funding settlement of a perpetual, on the position held at it, booked once.

    `qty` is the signed quantity held at the settlement instant — every fill stamped before
    it, and none stamped at it, since the rate is public there and an order sent in answer
    fills at that same instant; `rate` the realised rate that settled; `paid` what left the book —
    `qty x mark x multiplier x rate` (`kanso.nautilus.costs.funding_payment`), marked at
    the instrument's last print at or before the instant — so a long pays a positive rate
    and a negative `paid` is funding received. A settlement at which nothing was held pays
    nothing and is not recorded.
    """

    ts_ns: int
    instrument_id: str
    qty: float
    rate: float
    paid: float


@dataclass(frozen=True)
class Adjustment:
    """One split the venue applied to a position the run held, as the runner booked it.

    `ts_ns` is the instant the venue applied it: the instant the point that carried the
    market past the split was published at, its `ts_init`, which is when the venue matched
    that point and every fill it made. `qty` is the signed change the split made to the
    quantity held — the venue's own `PositionAdjusted.quantity_change`, floored onto the lot,
    never recomputed from a ratio — and `paid` the cash it paid in lieu of the fraction it
    left, signed as cash moves: a long is paid and a short pays. `ratio` is the split's,
    shares held after per share held before, of the split the venue's event names
    (`kanso.nautilus.splits.applied_by`) — so two splits the venue applies at one point each
    carry their own — and `None` only for an event that names no split of the schedule the
    extraction read, which a split the venue applied from that schedule always names.
    `fills_before` is where it falls among the run's fills of its instrument at `ts_ns`: how
    many of them the venue had matched before it applied the split, which come first among
    them in `CardRun.fills`. An order in flight lands at an instant behind its first point,
    another venue's, say, and a point published at the instant it applied the split can come
    after another of the name's venue printed before it, so a fill at that instant can be
    struck in the old shares; one the venue did not order — an adjustment of no split it
    announced — reads as before them all.
    The runner books the change into what it holds and the payment into cash at `ts_ns`
    (`kanso.nautilus.backtest._equity`); a run recorded before runs kept the adjustments
    carries none, and a reading of it that needs them does without (`fill_path_drawdown_pct`).
    """

    ts_ns: int
    instrument_id: str
    qty: float
    paid: float
    ratio: float | None = None
    fills_before: int = 0


@dataclass(frozen=True)
class Sized:
    """One entry or clip a `sizing` rule with `cap: room` sized: the instant the harness sized
    it, its instrument and side, and the base it was sized to — the smaller of the budget and
    the room the risk limits left then (`kanso.nautilus.strategy.KansoStrategy._sized_base`).

    `clip` says it was an overlay's clip rather than the host's own entry, so a gate judging
    the overlay's entries reads the bases of its clips. What `position_size` judges each entry
    fill against: the base of the latest of these in its name and side at or before the fill.
    """

    ts_ns: int
    instrument_id: str
    side: str
    base: float
    clip: bool = False


@dataclass(frozen=True)
class Trade:
    """A closed position: what was opened, what closed it, and what it netted after costs.

    `multiplier` is the instrument's contract multiplier, copied from the fills that made
    the position, so the opening value is in the currency the profit is.

    `funding` is what the position paid in funding over its life — every settlement in
    `(opened_ns, closed_ns]` of its instrument, since a fill at a settlement is not held
    there, negative when it was paid more than it paid — and `pnl_net` is net of it; `cost`
    is its fills' cost alone."""

    opened_ns: int
    closed_ns: int
    instrument_id: str
    qty: float
    avg_open: float
    avg_close: float
    pnl_net: float
    cost: float
    fills: tuple[Fill, ...]
    multiplier: float = 1.0
    funding: float = 0.0

    @property
    def notional(self) -> float:
        """The position's opening value in the account currency — peak quantity, average
        opening price and multiplier — the base its edge is expressed in bps of."""
        return abs(self.qty) * self.avg_open * self.multiplier


@dataclass(frozen=True)
class CardRun:
    """One window of one strategy: its return series, its equity curve and its trades.

    `returns[i]` is the mark-to-market equity change over the period ending at
    `period_ends_ns[i]`, in the account currency, and `equity[i]` is the equity at that
    instant — cash plus positions marked at the period's last price. The three series are
    parallel and ordered; `capital` is the equity the window opened at.

    `held` is that last sum before it was taken: what each instrument was worth at each
    period end. It is what a size gate has to read, because the two notionals a run
    already carried are neither of them a position's value while it was open.

    Three more series ride beside the equity curve when the hypothesis declares a `book`
    policy, each parallel to the period ends and empty when the run measured none.
    `cushion`, recorded under `reset: monthly` alone and what `max_drawdown` reads to know
    the book was reset, is what the reset has moved out of the book by each period end, so
    `equity[i] - equity[i-1]` is `returns[i]` less the transfer that end made: returns are
    struck before the transfer, and the sum of the returns is the profit of book and
    cushion together. `carry` is the financing charged at each end on what the book held
    above its equity — already in the returns, kept apart because it is a rate on borrowed
    notional and not a fill cost, so `cost_stress` leaves it alone. `worst_ratio` is the
    book's equity over its gross with each end's holdings valued at the period's adverse
    extreme, `None` at an end where nothing was held: what `maintenance_margin` reads.

    `funding` is every perpetual funding settlement the run booked on a held position, in
    time order — already in the returns and the equity at the end of the period each falls
    in, kept apart because it is a payment on what was held and not a fill cost.

    `adjustments` is every split the venue applied to a held position, in time order (each an
    `Adjustment`): a quantity no fill changed and the cash paid in lieu, already in the
    equity and in what `held` holds from the instant each was applied, kept apart because a
    reading of the run that replays its fills — the path `intraday_drawdown` reads — has to
    replay them too. Empty for a run that held nothing through a split, and for a run
    recorded before runs kept them.

    `sized` is every entry and clip a `sizing` rule with `cap: room` sized, in time order (each
    a `Sized`): the base it was sized to, which `position_size` judges the entry fills against.
    Empty without the cap, where every base is the budget, and for a run recorded before runs
    kept them.

    `period_stops` is what the hypothesis's `period_target` did over the run, as the harness
    that applied it recorded it (`PeriodStops`): every New York week it traded in, each week
    it stopped — the hit, what the stopped week dropped unsent, the flatten's fills — and the
    week open at its last point. A run handed no point after its warmup records an empty one,
    unless a stage restart resumed a week in it: a window handed no point of its own while a
    stage-mate's names printed records that week, open at its last point, with the stop it
    resumed under. A stage window with nothing to replay records an empty one, a week its
    restart would resume included, since it runs no harness. `None` for a hypothesis that
    declares no target, for a run that crashed and for a card its sizing refused, whose
    harness's record nothing read, and for a run recorded before runs kept it. It is a record
    of the path and is in no return: the stops are already in the fills and the equity, and
    nothing an objective or a gate reads changes with it.
    """

    window: tuple[date, date]
    period: str
    period_ends_ns: tuple[int, ...]
    returns: tuple[float, ...]
    equity: tuple[float, ...]
    trades: tuple[Trade, ...]
    fills: tuple[Fill, ...]
    capital: float
    currency: str
    venue_model: Mapping[str, object]
    held: tuple[Held, ...] = ()
    cushion: tuple[float, ...] = ()
    carry: tuple[float, ...] = ()
    worst_ratio: tuple[float | None, ...] = ()
    funding: tuple[FundingPayment, ...] = ()
    period_stops: PeriodStops | None = None
    adjustments: tuple[Adjustment, ...] = ()
    sized: tuple[Sized, ...] = ()

    def __post_init__(self) -> None:
        start, end = self.window
        if end < start:
            raise ValidationError(f"window: {end} is before {start}")
        parse_duration(self.period, "period")
        if self.capital <= 0:
            raise ValidationError(f"capital: {self.capital} is not above zero")
        # The venue model is held as the codec stores it and reads it back, so a run carried
        # in memory and the same run decoded from a measurement compare equal: the model
        # dumps a dated spread table as a tuple of rows and JSON reads it back as a list, and
        # a card compared with a best read from the store as a host or a benchmark paired
        # with none (`records.Measurement.against`).
        object.__setattr__(self, "venue_model", json.loads(json.dumps(dict(self.venue_model))))
        lengths = {len(self.period_ends_ns), len(self.returns), len(self.equity)}
        if len(lengths) != 1:
            raise ValidationError(
                "returns: the period ends, the returns and the equity curve are parallel "
                f"series, but they are {len(self.period_ends_ns)}, {len(self.returns)} and "
                f"{len(self.equity)} long"
            )
        ends = self.period_ends_ns
        if any(b <= a for a, b in zip(ends, ends[1:], strict=False)):
            raise ValidationError("period_ends_ns: the periods are not strictly increasing")
        for name in ("cushion", "carry", "worst_ratio"):
            series = getattr(self, name)
            if series and len(series) != len(self.period_ends_ns):
                raise ValidationError(
                    f"{name}: a book series is parallel to the period ends, but it is "
                    f"{len(series)} long against {len(self.period_ends_ns)} period ends"
                )

    @property
    def bounds(self) -> tuple[int, int]:
        """The window as a half-open instant span `[opens, closes)` in nanoseconds."""
        return midnight_ns(self.window[0]), midnight_ns(self.window[1] + timedelta(days=1))

    @property
    def period_ns(self) -> int:
        """The return period in nanoseconds: the length the runner cut the window into, one
        period after another from its first midnight (`RunRequest.period_ns` in
        `kanso.nautilus.backtest`); a period of no time at all is refused, as it is there."""
        length = int(parse_duration(self.period, "period").total_seconds() * NS_PER_SECOND)
        if length <= 0:
            raise ValidationError(
                f"period: {self.period!r} is no time at all, so the window holds no return "
                "periods to measure"
            )
        return length

    def folds(self, n: int) -> tuple[CardRun, ...]:
        """`n` contiguous, equal calendar spans of this window, each as its own run."""
        if n < 1:
            raise ValidationError(f"folds: {n} is not a positive number of folds")
        opens, closes = self.bounds
        span = closes - opens
        edges = tuple(opens + (span * i) // n for i in range(n + 1))
        return tuple(self.between(edges[i], edges[i + 1]) for i in range(n))

    def between(self, opens: int, closes: int) -> CardRun:
        """This run restricted to the half-open instant span `[opens, closes)`.

        Every series is filtered, `held`, `funding`, `adjustments` and `sized` included: `replace`
        would carry the whole run's holdings into each fold, so a gate reading them fold-wise
        would judge periods the fold does not contain. A funding payment belongs to the span
        holding its settlement instant, and a split's adjustment to the span holding the
        instant the venue applied it. A week of the period target belongs to the span holding its
        opening, with its stop — a week that opened before the window, as one does where the
        window opens inside it, to the span that opens with the window — and the week open at
        the run's last point stays only in a span that runs to the window's end
        (`PeriodStops.between`), so the folds of a run hold every week it traded in.
        """
        kept = [i for i, ts in enumerate(self.period_ends_ns) if opens <= ts < closes]
        return replace(
            self,
            window=(day_of(opens), day_of(closes - 1)),
            period_ends_ns=tuple(self.period_ends_ns[i] for i in kept),
            returns=tuple(self.returns[i] for i in kept),
            equity=tuple(self.equity[i] for i in kept),
            trades=tuple(t for t in self.trades if opens <= t.closed_ns < closes),
            fills=tuple(f for f in self.fills if opens <= f.ts_ns < closes),
            held=tuple(h for h in self.held if opens <= h.ts_ns < closes),
            funding=tuple(p for p in self.funding if opens <= p.ts_ns < closes),
            adjustments=tuple(a for a in self.adjustments if opens <= a.ts_ns < closes),
            sized=tuple(s for s in self.sized if opens <= s.ts_ns < closes),
            cushion=tuple(self.cushion[i] for i in kept) if self.cushion else (),
            carry=tuple(self.carry[i] for i in kept) if self.carry else (),
            worst_ratio=tuple(self.worst_ratio[i] for i in kept) if self.worst_ratio else (),
            period_stops=None
            if self.period_stops is None
            else self.period_stops.between(
                opens, closes, starts=opens <= self.bounds[0], ends=closes >= self.bounds[1]
            ),
        )
