"""A card's digest: what its trades did, read off the run it was scored on.

A card is scored on one scalar, and one scalar cannot say how it moved. A per-trade edge
rises when every clip earns more and when the clips that lost are dropped; a contribution
falls when the trades get worse and when the book trades less; and the loop's proposer,
shown nothing but the number, cannot tell which of the two it caused — so a search scored on
a contribution "improves" by trading less while the loss per trade gets worse. The digest is
the rest of the story in a dozen numbers: how often the card traded, how its trades and its
orders filled, and what each trade and each session earned before and after costs.

Every number is read off what the card already holds — the `CardRun` the runner extracted
and the order intents its strategy submitted — so nothing here runs anything, and nothing
is a second definition of a quantity the toolbox already defines:

* a **trade** is a closed position with a notional, as `quantities.edge_series` reads one,
  and its edge is `pnl_net / notional` in basis points; the card's net edge per trade and its
  error are `net_edge_bps` over the window as a single fold — the per-trade objective's own
  arithmetic, the error read period by period so a session's clips that move together count
  as one session's worth — which is exactly what the `edge_per_trade` gate judges;
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
  identity, and it can exceed one only through the second.

Funding is a payment on what was held, not a fill cost, so it stays in gross and in net
alike; a run with none of something reads `None` for what that something measures.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import fsum
from statistics import median

from kanso.criteria.objectives import net_edge_bps
from kanso.criteria.quantities import mean
from kanso.criteria.run import BPS, NS_PER_SECOND, CardRun, day_of
from kanso.schemas import CardDigest

Intent = tuple[int, str, str, float, str, float | None]
"""One submitted order as the runner reports it: `(ts, instrument, side, qty, type, price)`,
`price` the limit it carried or `None` for an order that carried none."""


def edge_per_trade(run: CardRun) -> tuple[float, float]:
    """The run's mean net edge per closed trade in basis points of the position opened, and
    the error of that mean: the per-trade objective over the window as one fold."""
    return net_edge_bps.compute(run, 1)


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
        net, se = edge_per_trade(run)
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
    return CardDigest(
        sessions=sessions,
        fills=len(run.fills),
        maker_share=_maker_share(run),
        fill_ratio=_fill_ratio(run, intents),
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
