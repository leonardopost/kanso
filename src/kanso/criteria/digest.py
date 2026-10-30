"""A card's digest: what its trades did, read off the run it was scored on.

A card is scored on one scalar, and one scalar cannot say how it moved. A per-trade edge
rises when every clip earns more and when the clips that lost are dropped; a contribution
falls when the trades get worse and when the book trades less; and the loop's proposer,
shown nothing but the number, cannot tell which of the two it caused — so a search scored on
a contribution "improves" by trading less while the loss per trade gets worse. Nor can a
number struck over a window say when in each day the card traded: a book whose every fill
lands in the first second of a session, its day ended there by a stop, scores like one that
trades all day. The digest is the rest of the story: how often the card traded, how its
trades and its orders filled, when in its periods it filled, and what each trade and each
session earned before and after costs.

Every number is read off what the card already holds — the `CardRun` the runner extracted
and the order intents its strategy submitted — so nothing here runs anything, and nothing
is a second definition of a quantity the toolbox already defines:

* a **trade** is a closed position with a notional, as `quantities.edge_series` reads one,
  and its edge is `pnl_net / notional` in basis points; the card's net edge per trade and its
  error are `net_edge_bps` over the window as a single fold — the per-trade objective's own
  arithmetic, the error read period by period so a session's clips that move together count
  as one session's worth — which is exactly what the `edge_per_trade` gate judges. Read that
  way the error has as many degrees of freedom as the window has periods that closed a
  trade, less one, and on `p` of them reads low by `√((p − 1)/p)`, which the gate's margin
  allows for (`gates.periods_margin`) and the number recorded here does not; trades that all
  closed in one period move one period's influence, which is zero by construction: their
  error reads zero whatever they did, so it is `None` below two;
* its **cost** is what its fills paid (`Trade.cost`) on the same notional, and its gross
  edge the net plus that cost, so gross less cost is net trade by trade;
* a **session** is a UTC day the run struck a period end in — the days a signature reads —
  and a session's numbers are in basis points of the capital: net is the sum of the returns,
  the equity the book made, and cost is every fill's cost, an open position's included,
  because both are already in that equity;
* the **maker share** is the share of traded notional the venue reported as a maker's fill;
* the **fill ratio** is the quantity filled on orders that carried a limit over the quantity
  submitted on such orders — a market order fills whatever it is offered, so only a priced
  order can go unfilled. A fill's limit is the one `kanso.nautilus.costs.limit_at` reads off
  the order's own events, and under nautilus_trader 1.231.0 an order the emulator released at
  market keeps the events of the order it was made from and fills with no limit; a modify
  that raised a quantity is not in the intent's either, so the ratio is a reading, not an
  identity, and it can exceed one only through the second;
* **when it filled** is read on the return periods the runner cut the window into, one after
  another from the window's first midnight (`CardRun.period_ns`) — a UTC day each by default
  — each fill in the one its instant falls in, which is the one the runner booked it to: over
  the periods the run filled in, the median seconds from a period's opening to its first fill
  and to its last, and from its first fill to its last. A period opens on the UTC clock and at
  no venue's open, since no calendar is consulted, so at a daily period an offset is the UTC
  time of day. Each median is the lower middle value over an even count, an offset some period
  filled at: the mean of the two middle ones need be no such instant — on ten scalping cards
  of 44 winter and 44 summer sessions it fell half an hour from every first fill, 14:00 where
  the lower one reads 13:30.

Funding is a payment on what was held, not a fill cost, so it stays in gross and in net
alike; a run with none of something reads `None` for what that something measures.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from math import fsum, sqrt
from statistics import median, median_low
from typing import Final

from kanso.criteria.objectives import net_edge_bps
from kanso.criteria.quantities import edge_bps, mean, variances_of_means
from kanso.criteria.run import BPS, NS_PER_SECOND, CardRun, day_of
from kanso.schemas import CardDigest

Intent = tuple[int, str, str, float, str, float | None]
"""One submitted order as the runner reports it: `(ts, instrument, side, qty, type, price)`,
`price` the limit it carried or `None` for an order that carried none."""


MIN_PERIODS: Final = 2
"""The fewest periods that closed a trade an error read period by period says anything of."""


def edge_per_trade(run: CardRun) -> tuple[float, float | None, int]:
    """The run's mean net edge per closed trade in basis points of the position opened, the
    error of that mean — the per-trade objective over the window as one fold — and how many
    of the window's periods closed those trades.

    The error is `None` when fewer than `MIN_PERIODS` did: `objectives._influence` gives each
    period `(E_p − R·M_p) / M̄`, and when one period holds every trade that period's `E_p` is
    `R·M_p` and every other's `E_p` and `M_p` are zero, so the error is zero however far apart
    the trades landed — one trade, or a session of clips, would read as an edge known
    exactly. A trade is counted in the period that error reads it in: the first ending at or
    after its close, the last for one that closed after every period end.
    """
    (whole,) = run.folds(1)
    edge, se = net_edge_bps.compute(run, 1)
    ends = whole.period_ends_ns
    last = max(len(ends), 1) - 1
    periods = len(
        {min(bisect_left(ends, t.closed_ns), last) for t in whole.trades if t.notional > 0}
    )
    return edge, (se if periods >= MIN_PERIODS else None), periods


def session_edge(run: CardRun) -> tuple[float, float | None, int, int] | None:
    """The run's mean net edge per closed trade in basis points of the position opened, its
    error clustered by session, how many sessions closed a trade and how many trades closed;
    `None` for a run that closed none.

    The mean is `edge_per_trade`'s. The error is the linearised one `objectives._influence`
    reads period by period, read here session by session: each session `s` of the run — a
    UTC day it struck a period end in or closed a trade on — has the influence `u_s = (E_s −
    R·M_s) / M̄`, `E_s` the edges of the trades closing that day, `M_s` their count, `M̄` the
    trades per session and `R` the mean, and the error is that of the mean of the `u_s` over
    the sessions (`quantities.variances_of_means`). So a session's clips that move together
    count as one session's worth whatever the run's period, which is what a ranking across
    hypotheses of different periods needs. It is `None` below two sessions with a trade,
    where the influences are zero by construction.
    """
    traded = [t for t in run.trades if t.notional > 0]
    if not traded:
        return None
    edge = edge_bps(run)
    days = sorted({day_of(ts) for ts in run.period_ends_ns} | {day_of(t.closed_ns) for t in traded})
    at = {day: index for index, day in enumerate(days)}
    totals = [0.0] * len(days)
    counts = [0] * len(days)
    for trade in traded:
        index = at[day_of(trade.closed_ns)]
        totals[index] += trade.pnl_net / trade.notional * BPS
        counts[index] += 1
    sessions = sum(1 for n in counts if n)
    if sessions < MIN_PERIODS:
        return edge, None, sessions, len(traded)
    per_session = len(traded) / len(days)
    influences = [(e - edge * m) / per_session for e, m in zip(totals, counts, strict=True)]
    (variance,) = variances_of_means(influences, [len(influences)])
    return edge, sqrt(variance), sessions, len(traded)


def digest(run: CardRun, intents: Sequence[Intent] = ()) -> CardDigest:
    """Every number of the digest, read off `run` and the orders its strategy submitted."""
    traded = [t for t in run.trades if t.notional > 0]
    per_trade: dict[str, float | None] = dict.fromkeys(
        (
            "hit_rate",
            "median_hold_s",
            "gross_bps_per_trade",
            "cost_bps_per_trade",
            "net_bps_per_trade",
            "net_bps_per_trade_se",
        )
    )
    if traded:
        net, se, _ = edge_per_trade(run)
        cost = mean([t.cost / t.notional * BPS for t in traded])
        per_trade = {
            "hit_rate": sum(1 for t in traded if t.pnl_net > 0) / len(traded),
            "median_hold_s": median((t.closed_ns - t.opened_ns) / NS_PER_SECOND for t in traded),
            "gross_bps_per_trade": net + cost,
            "cost_bps_per_trade": cost,
            "net_bps_per_trade": net,
            "net_bps_per_trade_se": se,
        }
    sessions = len({day_of(ts) for ts in run.period_ends_ns})
    per_session: dict[str, float | None] = dict.fromkeys(
        (
            "trades_per_session",
            "gross_bps_per_session",
            "cost_bps_per_session",
            "net_bps_per_session",
        )
    )
    if sessions:
        net_session = fsum(run.returns) / run.capital * BPS / sessions
        cost_session = fsum(f.cost for f in run.fills) / run.capital * BPS / sessions
        per_session = {
            "trades_per_session": len(run.trades) / sessions,
            "gross_bps_per_session": net_session + cost_session,
            "cost_bps_per_session": cost_session,
            "net_bps_per_session": net_session,
        }
    filled, first, last, active = _timing(run)
    return CardDigest(
        sessions=sessions,
        fills=len(run.fills),
        maker_share=_maker_share(run),
        fill_ratio=_fill_ratio(run, intents),
        filled_periods=filled,
        first_fill_s=first,
        last_fill_s=last,
        active_s=active,
        **per_trade,
        **per_session,
    )


def _maker_share(run: CardRun) -> float | None:
    """The share of traded notional the venue reported as a maker's, or `None` with none."""
    traded = fsum(f.notional for f in run.fills)
    if traded <= 0:
        return None
    return fsum(f.notional for f in run.fills if f.maker) / traded


def _fill_ratio(run: CardRun, intents: Sequence[Intent]) -> float | None:
    """The quantity filled on priced orders over the quantity submitted on them, or `None`
    when the strategy submitted no order that carried a limit."""
    submitted = fsum(abs(intent[3]) for intent in intents if intent[5] is not None)
    if submitted <= 0:
        return None
    return fsum(abs(f.qty) for f in run.fills if f.limit is not None) / submitted


def _timing(run: CardRun) -> tuple[int, float | None, float | None, float | None]:
    """When in its periods the run filled: how many periods it filled in and, over those,
    the lower median of the nanoseconds from a period's opening to its first fill and to its
    last, and from its first fill to its last, each in seconds (see the module)."""
    opens, _ = run.bounds
    length = run.period_ns
    spans: dict[int, tuple[int, int]] = {}
    for fill in run.fills:
        index = (fill.ts_ns - opens) // length
        first, last = spans.get(index, (fill.ts_ns, fill.ts_ns))
        spans[index] = (min(first, fill.ts_ns), max(last, fill.ts_ns))
    if not spans:
        return 0, None, None, None
    offsets = [
        (first - opens - index * length, last - opens - index * length)
        for index, (first, last) in spans.items()
    ]
    return (
        len(spans),
        median_low(first for first, _ in offsets) / NS_PER_SECOND,
        median_low(last for _, last in offsets) / NS_PER_SECOND,
        median_low(last - first for first, last in offsets) / NS_PER_SECOND,
    )
