"""Hand-built subjects for the criteria suite: a hypothesis, a run, its trades and fills.

Every builder here produces arithmetic a reader can check by eye, because a test of a
statistic whose expected value came from the statistic proves nothing. Returns are whole
numbers of currency, notionals are round, and windows are a few days long so a fold is a
day.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from kanso.criteria import CardRun, DeployedBook, Fill, GateContext, Trade
from kanso.criteria.run import NS_PER_SECOND, midnight_ns
from kanso.schemas import Hypothesis, embargo_days

START = date(2024, 1, 1)
CAPITAL = 100_000.0

HYPOTHESIS: dict[str, Any] = {
    "schema": 1,
    "id": "demo_mr",
    "title": "Demo: mean reversion on a synthetic series",
    "thesis": "A stretched synthetic series reverts within the session.",
    "mechanism": "mean_reversion",
    "universe": ["DEMO"],
    "horizon": "30m",
    "resolution": "1m",
    "data_requirements": ["bar"],
    "risk_limits": {"max_position_pct": 20, "max_drawdown_pct": 15, "max_leverage": 1},
    "windows": {
        "research": {"start": "2024-01-01", "end": "2024-12-31"},
        "certification": {"start": "2025-01-06", "end": "2025-05-30"},
        "forward": {"start": "2025-06-02"},
    },
    "construct": {"id": "sleeve"},
    "objective": {"id": "net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
    "constraints": [{"id": "strategy_integrity"}],
}


def make_hyp(**overrides: Any) -> Hypothesis:
    """The demo hypothesis, classified, with any field replaced.

    A horizon override moves the certification and forward windows with it, since the
    embargo between research and certification is five horizons.
    """
    fields = {**HYPOTHESIS, **overrides}
    if "windows" not in overrides:
        research_end = date(2024, 12, 31)
        opens = research_end + timedelta(days=embargo_days(fields["horizon"]))
        closes = opens + timedelta(days=144)
        fields["windows"] = {
            "research": {"start": "2024-01-01", "end": research_end.isoformat()},
            "certification": {"start": opens.isoformat(), "end": closes.isoformat()},
            "forward": {"start": (closes + timedelta(days=3)).isoformat()},
        }
    return Hypothesis.model_validate(fields)


def at(day: date, hour: int = 12) -> int:
    """An instant inside a calendar day, in nanoseconds since the epoch."""
    return midnight_ns(day) + hour * 3600 * NS_PER_SECOND


def fill(day: date, cost: float = 0.0, qty: float = 100.0, px: float = 100.0) -> Fill:
    """One execution on a day, of `qty x px` notional."""
    return Fill(ts_ns=at(day), instrument_id="DEMO", side="BUY", qty=qty, px=px, cost=cost)


def trade(day: date, pnl: float, notional: float = 10_000.0, cost: float = 0.0) -> Trade:
    """A position opened and closed on one day, netting `pnl` on `notional`."""
    return Trade(
        opened_ns=at(day, 9),
        closed_ns=at(day, 15),
        instrument_id="DEMO",
        qty=100.0,
        avg_open=notional / 100.0,
        avg_close=(notional + pnl) / 100.0,
        pnl_net=pnl,
        cost=cost,
        fills=(fill(day, cost=cost, px=notional / 100.0),),
    )


def build_run(
    returns: tuple[float, ...] = (),
    *,
    start: date = START,
    days: int | None = None,
    trades: tuple[Trade, ...] = (),
    fills: tuple[Fill, ...] = (),
    capital: float = CAPITAL,
    equity: tuple[float, ...] | None = None,
) -> CardRun:
    """A daily run: one return period per day, equity compounded from the returns."""
    span = len(returns) if days is None else days
    running = capital
    curve: list[float] = []
    for value in returns:
        running += value
        curve.append(running)
    return CardRun(
        window=(start, start + timedelta(days=span - 1)),
        period="1d",
        period_ends_ns=tuple(
            midnight_ns(start + timedelta(days=i + 1)) - 1 for i in range(len(returns))
        ),
        returns=returns,
        equity=tuple(curve) if equity is None else equity,
        trades=trades,
        fills=fills,
        capital=capital,
        currency="USD",
        venue_model={},
    )


def book(id_: str, run: CardRun, returns: tuple[float, ...]) -> DeployedBook:
    """A deployed book sharing a run's period ends, with returns of its own."""
    return DeployedBook(id=id_, period_ends_ns=run.period_ends_ns, returns=returns)


def context(subject: CardRun, **overrides: Any) -> GateContext:
    """A gate context over one run, with the card-stage defaults.

    An override may replace the run itself; the window follows whichever run wins.
    """
    fields: dict[str, Any] = {
        "hyp": make_hyp(),
        "construct": "sleeve",
        "stage": "card",
        "params": {},
        "run": subject,
        "research_folds": 4,
    }
    merged = {**fields, **overrides}
    merged.setdefault("window", merged["run"].window)
    return GateContext(**merged)
