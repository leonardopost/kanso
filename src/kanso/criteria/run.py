"""What one backtest produced, and how it is cut into folds.

A `CardRun` is the single subject every objective and every gate is evaluated on. It is
extracted by the runner from its own fills rather than read from the engine's analyser,
so one definition of a return, a trade and an equity curve serves cards, certification
gates, composition expectations and the realised paper and live objectives alike.

The three shapes are deliberately flat and immutable: a `Fill` is one execution with the
cost the runner applied to it, a `Trade` is a closed position with the fills that made it,
and a `CardRun` is a window's worth of both plus the period-end equity curve. A position
still open when the window closes keeps its unrealised profit in `equity` but is not a
trade, because a trade is a closed position.

`folds(n)` cuts the window into `n` contiguous, equal calendar spans and restricts the
run to each. The cut is by calendar, not by observation count, so a fold's length does not
depend on how busy it was; a period, a trade and a fill belong to the fold containing the
instant they are stamped at — a period by its end, a trade by its close, a fill by its
execution. Folds are what the keep rule averages over and what the walk-forward gates
read, so this arithmetic is the noise floor of the whole loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta

from kanso.errors import ValidationError
from kanso.schemas import parse_duration

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
class Fill:
    """One execution, with the cost the runner applied to it once, in the extraction."""

    ts_ns: int
    instrument_id: str
    side: str
    qty: float
    px: float
    cost: float

    @property
    def notional(self) -> float:
        """Absolute traded value, the base a participation limit is measured against."""
        return abs(self.qty) * self.px


@dataclass(frozen=True)
class Trade:
    """A closed position: what was opened, what closed it, and what it netted after costs."""

    opened_ns: int
    closed_ns: int
    instrument_id: str
    qty: float
    avg_open: float
    avg_close: float
    pnl_net: float
    cost: float
    fills: tuple[Fill, ...]

    @property
    def notional(self) -> float:
        """The position's opening value, the base its edge is expressed in bps of."""
        return abs(self.qty) * self.avg_open


@dataclass(frozen=True)
class CardRun:
    """One window of one strategy: its return series, its equity curve and its trades.

    `returns[i]` is the mark-to-market equity change over the period ending at
    `period_ends_ns[i]`, in the account currency, and `equity[i]` is the equity at that
    instant — cash plus positions marked at the period's last price. The three series are
    parallel and ordered; `capital` is the equity the window opened at.
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

    def __post_init__(self) -> None:
        start, end = self.window
        if end < start:
            raise ValidationError(f"window: {end} is before {start}")
        parse_duration(self.period, "period")
        if self.capital <= 0:
            raise ValidationError(f"capital: {self.capital} is not above zero")
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

    @property
    def bounds(self) -> tuple[int, int]:
        """The window as a half-open instant span `[opens, closes)` in nanoseconds."""
        return midnight_ns(self.window[0]), midnight_ns(self.window[1] + timedelta(days=1))

    def folds(self, n: int) -> tuple[CardRun, ...]:
        """`n` contiguous, equal calendar spans of this window, each as its own run."""
        if n < 1:
            raise ValidationError(f"folds: {n} is not a positive number of folds")
        opens, closes = self.bounds
        span = closes - opens
        edges = tuple(opens + (span * i) // n for i in range(n + 1))
        return tuple(self.between(edges[i], edges[i + 1]) for i in range(n))

    def between(self, opens: int, closes: int) -> CardRun:
        """This run restricted to the half-open instant span `[opens, closes)`."""
        kept = [i for i, ts in enumerate(self.period_ends_ns) if opens <= ts < closes]
        return replace(
            self,
            window=(day_of(opens), day_of(closes - 1)),
            period_ends_ns=tuple(self.period_ends_ns[i] for i in kept),
            returns=tuple(self.returns[i] for i in kept),
            equity=tuple(self.equity[i] for i in kept),
            trades=tuple(t for t in self.trades if opens <= t.closed_ns < closes),
            fills=tuple(f for f in self.fills if opens <= f.ts_ns < closes),
        )
