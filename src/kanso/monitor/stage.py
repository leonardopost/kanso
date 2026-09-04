"""The stage facts a paper or live gate reads, and the one place they are assembled.

A gate is a pure evaluator over a `CardRun`, and a run says nothing about the stage it was
produced on: how long the version has been there, what clock the stage is at, what the whole
stage made today, or what kind of money its execution client spends. Those are properties of
a deployment, and a deployment is what the monitor sees. So the monitor builds one record per
version per pass and hands it to the gates as their session; a gate that does not find one
skips, which is how the toolbox stays independent of the component that deploys.

**Time here is stage-clock time, never wall-clock time.** A stage in this version replays the
catalog from the forward window's start, so its clock is the availability instant of the last
point its node released. A version's tenure on a stage is therefore the distance between the
stage clock when it joined and the stage clock now — two instants from the same clock. The
wall-clock `joined_at` in `portfolio.yaml` is the operator's record of when the deployment
happened and is deliberately not what any gate measures against: at speed zero a year of
market passes in seconds, and a paper window measured in wall time would never open.

**Funding is what makes fill quality measurable.** An execution client declares whether it
spends simulated money, a broker's paper money or real money. A simulated venue realises the
modelled cost by construction, so a slippage comparison against it compares a number with
itself; the gate skips on it, which in this version is every client the framework provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

NS_PER_SECOND: Final = 1_000_000_000

SIMULATED: Final = "simulated"
"""The funding an execution client declares when its fills are modelled, not brokered."""

UNKNOWN: Final = "unknown"
"""The funding of a client this build cannot resolve to a declaration."""


@dataclass(frozen=True)
class StageRecord:
    """One deployed version's stage, as the paper and live gates need to see it.

    `joined_ns` and `clock_ns` are two readings of the same stage clock: the position the
    stage had reached when this version joined it, and the position it has reached now.
    `paper_window_s` is how long the paper window that set the version's expectation was
    required to be, which is the window a live drift is measured over. `day_pnl` and
    `capital` are the whole stage's, not the version's, because a daily loss limit is a
    property of the book a stage runs, and one version cannot see the others.
    """

    stage: str
    joined_ns: int
    clock_ns: int
    paper_window_s: float | None = None
    day_pnl: float = 0.0
    capital: float = 0.0
    daily_loss_pct: float = 0.0
    funding: str = UNKNOWN
    n_fills: int = 0
    realised_slippage_bps: float | None = None

    @property
    def elapsed_s(self) -> float:
        """Stage-clock seconds since this version joined; never negative."""
        return max(self.clock_ns - self.joined_ns, 0) / NS_PER_SECOND
