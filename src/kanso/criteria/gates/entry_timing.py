"""Whether the subject's trades were timed, or only held what anything held then would earn.

A book long a rising index some of the time scores above zero whenever it is long, so an
absolute objective — a contribution per period, a Sharpe — credits exposure as if it were
timing. This gate puts the certification window's trades beside the same trades circularly
shifted by whole sessions (`kanso.criteria.timing`): every admissible offset is one draw, each
priced and costed by the runner's arithmetic, and the subject's own value is the same arithmetic
at offset 0. It passes when `p = (1 + #{draws ≥ observed}) / (1 + D)` is at most the plan's
`max_p`. No random number is drawn, so the count is stated and the evidence reproduces bit for
bit.

**It needs prices the run does not carry, and is handed a reader for them.** The certification
runner supplies `GateContext.session_prices`, which reads the window again — a chunk at a time,
folding a number per key per session — for exactly the instruments and times of day the trades
filled at, each point at the instant it was published. It judges nothing it was not handed: no
threshold, an attached construct (whose trades are its host's as much as its own), fewer than
two closed trades, or no reader are skips with the reason, as any gate without its context.

**A missing price is a failure, never a skip.** A required gate that passes because its input is
missing is the hole a search correction that skipped once was. So a traded instrument the window
holds no bar, print or quote of fails the gate naming it; so does a subject any of whose trades
could not be priced at its own instants, counted as `observed_dropped` — a book judged on the
trades that could be priced would leave the rest's timing untested, the drift a leg held among
it; and so does a window too short for any offset — a hold longer than half the window leaves
`D < 1`, stated as `draws: 0`. A window whose smallest attainable `p`, `1/(1 + D)`, is above
`max_p` cannot pass either, and says so in `min_attainable_p`.

**On a venue that matches a level-two book every fill is struck at the touch.** The runner counts
it so for a hypothesis that requires `book`, and the null does too, at offset 0 and in every draw.
"""

from __future__ import annotations

from math import fsum
from typing import ClassVar, Final

import numpy as np

from kanso.criteria.context import Gate, GateContext, number, skipped, verdict
from kanso.criteria.quantities import mean, stdev
from kanso.criteria.timing import (
    draw,
    keys_of,
    offsets,
    place,
    runner_bps_per_trade,
    struck_in,
)
from kanso.schemas import GateResult

MIN_TRADES: Final = 2
"""Fewer closed trades than this have no mean per trade worth shifting."""

NULL_PERCENTILE: Final = 95.0

LEVELLED: Final = "book"
"""The data requirement that makes the venue match a level-two book, where the runner counts
every fill as struck at the touch (`kanso.nautilus.backtest.Marks.struck`)."""

NO_CHOICE: Final = "no max_p was chosen, so no timing was tested"
ATTACHED: Final = (
    "an attached construct trades its host's book, so its trades cannot be shifted apart from it"
)
FEW_TRADES: Final = "fewer than two closed trades, so there is no mean per trade to shift"
NO_PRICES: Final = "no reader of the window's prices was supplied, so nothing could be shifted"

UNPRICED: Final = (
    "the window holds no bar, print or quote of an instrument the subject traded, so its trades "
    "cannot be placed at other times; a missing price fails, it does not pass"
)
UNPLACED: Final = (
    "a trade of the subject's could not be priced at its own instants: the series its instrument "
    "is priced from (its bars where the window holds any, else its prints, else its quotes) holds "
    "no reading at or before a fill's time on that fill's session, so the rest's timing is not "
    "the book's; a missing price fails, it does not pass"
)
TOO_SHORT: Final = (
    "the longest hold is more than half the window, so no offset moves every trade off its own "
    "original: there is no draw"
)


class _EntryTiming:
    """The trades earn more than the same trades shifted in time."""

    id: ClassVar[str] = "entry_timing"

    def evaluate(self, ctx: GateContext) -> GateResult:
        max_p = number(ctx, "max_p")
        if max_p is None:
            return skipped(self.id, NO_CHOICE)
        if ctx.host_run is not None:
            return skipped(self.id, ATTACHED)
        run = ctx.run
        if len(run.trades) < MIN_TRADES:
            return skipped(self.id, FEW_TRADES)
        if ctx.session_prices is None:
            return skipped(self.id, NO_PRICES)
        prices = ctx.session_prices(keys_of(run))
        reading: dict[str, object] = {
            "trades": len(run.trades),
            "observed_runner_bps_per_trade": runner_bps_per_trade(run),
            "sessions": len(prices.sessions),
            "max_p": max_p,
        }
        missing = sorted({t.instrument_id for t in run.trades} - set(prices.source))
        if missing:
            return verdict(self.id, False, {**reading, "unpriced": missing, "reason": UNPRICED})
        levelled = LEVELLED in ctx.hyp.data_requirements
        shares = struck_in(run)
        placed = [place(trade, prices, shares) for trade in run.trades]
        own = draw(placed, prices, run.venue_model, 0, levelled=levelled)
        observed = own.bps_per_trade(run.capital)
        if own.dropped or observed is None:
            return verdict(
                self.id, False, {**reading, "observed_dropped": own.dropped, "reason": UNPLACED}
            )
        floor, shifts = offsets(placed, len(prices.sessions))
        reading.update({"observed_bps_per_trade": observed, "min_offset": floor})
        if not shifts:
            return verdict(
                self.id,
                False,
                {**reading, "draws": 0, "reason": TOO_SHORT},
            )
        drawn = [draw(placed, prices, run.venue_model, k, levelled=levelled) for k in shifts]
        values = [value for d in drawn if (value := d.bps_per_trade(run.capital)) is not None]
        empty = len(drawn) - len(values)
        above = empty + sum(value >= observed for value in values)
        p = (1 + above) / (1 + len(drawn))
        return verdict(
            self.id,
            p <= max_p,
            {
                **reading,
                "max_offset": shifts[-1],
                "draws": len(drawn),
                "empty_draws": empty,
                "dropped_mean": fsum(d.dropped for d in drawn) / len(drawn),
                "null_mean": mean(values) if values else None,
                "null_sd": stdev(values) if values else None,
                "null_p95": float(np.percentile(values, NULL_PERCENTILE)) if values else None,
                "null_max": max(values) if values else None,
                "min_attainable_p": 1 / (1 + len(drawn)),
                "p": p,
            },
        )


gate: Final[Gate] = _EntryTiming()
"""What the toolbox entry `entry_timing` resolves to."""
