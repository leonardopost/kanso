"""Whether the live stage has lost more today than the portfolio allows it to lose in a day.

This is the one gate whose subject is the stage rather than the version. A daily loss limit is
a property of the book a stage runs: three versions each a third of the way to the limit are
together at it, and a version cannot see the other two. The monitor is the only component that
sees them all, so it sums the day and this gate judges the sum.

**The gate writes nothing.** It reports that the day is worse than the limit, and the monitor
is what then sets the stage's kill switch and escalates. That separation is deliberate: a gate
runs wherever a plan is evaluated and must be safe to evaluate twice, and halting a stage is
not. It is also why this gate's failure does not demote the version — the monitor halts the
stage instead, which is the stronger act, and demoting a version into a halted stage would
change nothing about the money.

The day is the stage clock's day, not the operator's: a stage replaying the catalog is at
whatever date its feed has reached, and a limit measured against the wall calendar would sum
years of sessions into one.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, skipped, verdict
from kanso.criteria.run import day_of
from kanso.monitor.stage import StageRecord
from kanso.schemas import GateResult

PERCENT: Final = 100.0

NO_STAGE: Final = "no stage record was supplied, so there is no day's book to judge"
NO_CAPITAL: Final = "the stage carries no capital, so a percentage of it is not a limit"


class _DailyLossKill:
    """The stage's profit for the day is above the loss the portfolio limits it to."""

    id: ClassVar[str] = "daily_loss_kill"

    def evaluate(self, ctx: GateContext) -> GateResult:
        if not isinstance(ctx.session, StageRecord):
            return skipped(self.id, NO_STAGE)
        record = ctx.session
        if record.capital <= 0:
            return skipped(self.id, NO_CAPITAL)
        limit = -record.daily_loss_pct / PERCENT * record.capital
        return verdict(
            self.id,
            record.day_pnl > limit,
            {
                "day": day_of(record.clock_ns).isoformat(),
                "day_pnl": record.day_pnl,
                "limit": limit,
                "daily_loss_pct": record.daily_loss_pct,
                "stage_capital": record.capital,
            },
        )


gate: Final[Gate] = _DailyLossKill()
"""What the toolbox entry `daily_loss_kill` resolves to."""
