"""Hand-built deployments: a workspace, a sleeve, a composed version and a stage book.

Nothing here runs an engine. A monitor pass reads persisted books, a pinned hypothesis, a
pinned plan and a strategy file, and every one of those is a few lines of data — so the suite
writes them directly and stays a test of the pass rather than of the runner.

The arithmetic is chosen to be checkable by eye: a run whose daily return never varies has a
zero Sharpe on every fold, so the realised objective is exactly zero and a band either
contains it or does not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from kanso.criteria import GateContext
from kanso.criteria.run import NS_PER_DAY, CardRun, Fill, Trade, midnight_ns
from kanso.monitor.stage import StageRecord
from kanso.nautilus.node import Book, Realised
from kanso.portfolio import records
from kanso.schemas import (
    CertificationPlan,
    Deployment,
    Hypothesis,
    Limits,
    Portfolio,
    Stage,
    Stages,
    StrategyFile,
    StrategyVersion,
    dump_yaml,
    write_yaml,
)
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace

HYP_ID = "demo_mr"
STRATEGY_ID = "demo_mr"
START = date(2024, 3, 1)
DAYS = 20
CAPITAL = 100_000.0
INSTRUMENT = "DEMO.XNAS"

VENUE_MODEL: dict[str, Any] = {
    "venue": "XNAS",
    "account": "margin",
    "currency": "USD",
    "default_leverage": 1.0,
    "costs": {"commission_bps": 0.0, "slippage_bps": 1.0, "spread": "fixed_bps", "fixed_bps": 2.0},
    "origins": {"account": "default", "currency": "default", "costs": "default"},
}

DOCUMENT: dict[str, Any] = {
    "schema": 1,
    "id": HYP_ID,
    "title": "Demo: a deployed sleeve",
    "thesis": "The synthetic series reverts within a session.",
    "mechanism": "mean_reversion",
    "universe": [INSTRUMENT],
    "horizon": "1d",
    "resolution": "1d",
    "data_requirements": ["bar"],
    "risk_limits": {"max_position_pct": 20, "max_drawdown_pct": 15, "max_leverage": 1},
    "windows": {
        "research": {"start": "2023-01-01", "end": "2023-12-31"},
        "certification": {"start": "2024-01-06", "end": "2024-02-29"},
        "forward": {"start": "2024-03-01"},
    },
    "construct": {"id": "sleeve"},
    "objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 0.5}},
    "constraints": [{"id": "strategy_integrity"}, {"id": "max_drawdown"}, {"id": "min_trades"}],
}

PLAN: dict[str, Any] = {
    "schema": 1,
    "hyp_id": HYP_ID,
    "plan_version": 1,
    "planned_at": "2024-02-29T00:00:00+00:00",
    "planned_by": "mock",
    "inputs": {
        "hypothesis_sha": "0" * 64,
        "construct": {"id": "sleeve"},
        "data_availability": {"types": ["bar"], "spans": {}},
        "n_trials": 12,
    },
    "gates": [
        {
            "id": "embargoed_window",
            "stage": "cert",
            "params": {"min_fraction": 0.5},
            "rationale": "the embargo is the proof",
        },
        {
            "id": "paper_forward",
            "stage": "paper",
            "params": {"min_duration": "5d", "horizon_mult": 5.0},
            "rationale": "a week of paper, and five horizons",
        },
        {
            "id": "live_drift",
            "stage": "live",
            "params": {},
            "rationale": "the expectation still holds",
        },
    ],
}


def hypothesis(**changes: Any) -> Hypothesis:
    """The deployed sleeve's hypothesis, with any field replaced."""
    return Hypothesis.model_validate({**DOCUMENT, **changes})


def plan(gates: list[dict[str, Any]] | None = None) -> CertificationPlan:
    """The sleeve's pinned plan, optionally with a different gate list."""
    return CertificationPlan.model_validate(
        {**PLAN, "gates": PLAN["gates"] if gates is None else gates}
    )


def run_over(
    returns: tuple[float, ...],
    *,
    start: date = START,
    capital: float = CAPITAL,
    trades: tuple[Trade, ...] = (),
    fills: tuple[Fill, ...] = (),
) -> CardRun:
    """A daily run: one return period per day, ending a nanosecond before midnight."""
    running = capital
    equity: list[float] = []
    for value in returns:
        running += value
        equity.append(running)
    return CardRun(
        window=(start, start + timedelta(days=len(returns) - 1)),
        period="1d",
        period_ends_ns=tuple(
            midnight_ns(start) + (index + 1) * NS_PER_DAY - 1 for index in range(len(returns))
        ),
        returns=returns,
        equity=tuple(equity),
        trades=trades,
        fills=fills,
        capital=capital,
        currency="USD",
        venue_model=VENUE_MODEL,
    )


def flat_run(days: int = DAYS, step: float = 100.0, start: date = START) -> CardRun:
    """A run whose daily return never varies: zero dispersion, so a zero Sharpe."""
    return run_over(tuple([step] * days), start=start)


def fill_at(day: date, qty: float = 100.0, px: float = 10.0) -> Fill:
    """One execution at noon on a day."""
    return Fill(
        ts_ns=midnight_ns(day) + NS_PER_DAY // 2,
        instrument_id=INSTRUMENT,
        side="BUY",
        qty=qty,
        px=px,
        cost=1.0,
    )


def version(
    *,
    number: int = 1,
    state: str = "paper",
    ci90: tuple[float, float] = (-0.5, 0.5),
    objective_id: str = "wf_sharpe_net",
) -> StrategyVersion:
    """One composed version of the sleeve, with the band its paper gates judge against."""
    return StrategyVersion.model_validate(
        {
            "version": number,
            "sleeve": {"hyp_id": HYP_ID, "strategy_sha": "a" * 64},
            "config": {},
            "pins": {
                "kanso_version": "0.1.0",
                "nautilus_version": "1.231.0",
                "criteria_version": "0.1.0+test",
                "plan_version": 1,
                "snapshot_id": "snap",
                "venue_model": VENUE_MODEL,
            },
            "expectation": {
                "objective_id": objective_id,
                "value": 0.0,
                "ci90": list(ci90),
                "mdd_p95": 5.0,
                "window": {"start": "2024-01-06", "end": "2024-02-29"},
            },
            "state": state,
            "created_at": "2024-02-29T00:00:00+00:00",
        }
    )


def portfolio(
    *,
    paper: tuple[tuple[str, int], ...] = (),
    live: tuple[tuple[str, int], ...] = (),
    paper_capital: float = CAPITAL,
    live_capital: float = CAPITAL,
    kill_paper: bool = False,
    kill_live: bool = False,
    max_gross_pct: float = 100.0,
    max_net_pct: float = 100.0,
    daily_loss_pct: float = 3.0,
) -> Portfolio:
    """A deployment surface holding the named versions on each stage."""
    return Portfolio(
        stages=Stages(
            paper=Stage(
                exec="sandbox",
                data="replay",
                speed=1.0,
                capital=paper_capital,
                kill_switch=kill_paper,
                strategies=[_deployment(name, number, paper_capital) for name, number in paper],
            ),
            live=Stage(
                exec="sandbox",
                data="replay",
                speed=1.0,
                capital=live_capital,
                kill_switch=kill_live,
                strategies=[_deployment(name, number, live_capital) for name, number in live],
            ),
        ),
        limits=Limits(
            max_gross_pct=max_gross_pct,
            max_net_pct=max_net_pct,
            per_strategy_max_pct=40.0,
            daily_loss_pct=daily_loss_pct,
        ),
    )


def _deployment(name: str, number: int, capital: float) -> Deployment:
    return Deployment(
        id=name,
        version=number,
        capital=capital * 0.4,
        joined_at=datetime(2024, 3, 1, tzinfo=UTC),
    )


def pin_hypothesis(store: StateStore, hyp: Hypothesis, status: str = "certified") -> str:
    """Register the sleeve the way certification leaves it: pinned bytes and a row."""
    sha = store.put_blob(dump_yaml(hyp).encode("utf-8"))
    now = datetime.now(tz=UTC).isoformat()
    store.connection.execute(
        "INSERT INTO hypotheses (hyp_id, status, hypothesis_sha, pins, created_at, updated_at)"
        " VALUES (?, ?, ?, '{}', ?, ?)"
        " ON CONFLICT (hyp_id) DO UPDATE SET status = excluded.status,"
        " hypothesis_sha = excluded.hypothesis_sha",
        (hyp.id, status, sha, now, now),
    )
    return sha


def write_plan(ws: Workspace, pinned: CertificationPlan) -> None:
    """Pin a plan where `read_plan` looks for it."""
    directory = ws.path("certificates", pinned.hyp_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_yaml(pinned, directory / "plan.yaml")


def write_strategy(
    ws: Workspace, store: StateStore, composed: StrategyVersion, strategy_id: str = STRATEGY_ID
) -> StrategyFile:
    """Write the strategy file and index its version, as composition does."""
    file = StrategyFile(id=strategy_id, versions=[composed])
    strategy_files.write(ws, file)
    strategy_files.record(store, strategy_id, composed)
    return file


def write_book(
    store: StateStore,
    stage: str,
    *,
    run: CardRun,
    strategy_id: str = STRATEGY_ID,
    number: int = 1,
    clock: date | None = None,
    positions: tuple[Book, ...] = (),
    session_id: str = "session-1",
    capital: float = CAPITAL,
) -> None:
    """One closed window, recorded the way a stage node records what it realised."""
    records.record_stage_run(
        store,
        stage,
        session_id,
        [
            Realised(
                strategy_id=strategy_id,
                version=number,
                capital=capital,
                run=run,
                positions=positions,
            )
        ],
    )
    set_clock(store, stage, clock or run.window[1], session_id=session_id)


def set_clock(store: StateStore, stage: str, day: date, *, session_id: str = "session-1") -> None:
    """A session row carrying the stage's replay position, as a stage node leaves one."""
    store.connection.execute(
        "INSERT INTO sessions (session_id, mode, target, instruments, from_ts, to_ts, speed,"
        " exec_id, clock_ts, started_at) VALUES (?, ?, ?, '[]', ?, ?, 1.0, 'sandbox', ?, ?)"
        " ON CONFLICT (session_id) DO UPDATE SET clock_ts = excluded.clock_ts",
        (
            f"{stage}-{session_id}",
            stage,
            STRATEGY_ID,
            START.isoformat(),
            day.isoformat(),
            str(midnight_ns(day) + NS_PER_DAY - 1),
            datetime.now(tz=UTC).isoformat(),
        ),
    )


def position(instrument: str = INSTRUMENT, qty: float = 100.0, price: float = 10.0) -> Book:
    """One open position at the end of a window, before the node flattened it."""
    return Book(instrument_id=instrument, qty=qty, price=price)


def deploy(
    ws: Workspace,
    store: StateStore,
    *,
    stage: str = "paper",
    state: str = "paper",
    ci90: tuple[float, float] = (-0.5, 0.5),
    run: CardRun | None = None,
    positions: tuple[Book, ...] = (),
    **portfolio_changes: Any,
) -> None:
    """A whole deployment: pinned sleeve, pinned plan, composed version, book and file."""
    hyp = hypothesis()
    pin_hypothesis(store, hyp)
    write_plan(ws, plan())
    write_strategy(ws, store, version(state=state, ci90=ci90))
    write_book(store, stage, run=run or flat_run(), positions=positions)
    placed = ((STRATEGY_ID, 1),)
    changes: dict[str, Any] = {("paper" if stage == "paper" else "live"): placed}
    changes.update(portfolio_changes)
    write_yaml(portfolio(**changes), ws.path("portfolio.yaml"))


def stage_record(
    *,
    stage: str = "paper",
    joined: date = START,
    clock: date | None = None,
    paper_window_s: float | None = None,
    day_pnl: float = 0.0,
    capital: float = CAPITAL,
    daily_loss_pct: float = 3.0,
    funding: str = "unknown",
    n_fills: int = 0,
    realised_slippage_bps: float | None = None,
) -> StageRecord:
    """The stage facts a gate reads, with the clock at the end of the default window."""
    reached = clock or (START + timedelta(days=DAYS - 1))
    return StageRecord(
        stage=stage,
        joined_ns=midnight_ns(joined),
        clock_ns=midnight_ns(reached) + NS_PER_DAY - 1,
        paper_window_s=paper_window_s,
        day_pnl=day_pnl,
        capital=capital,
        daily_loss_pct=daily_loss_pct,
        funding=funding,
        n_fills=n_fills,
        realised_slippage_bps=realised_slippage_bps,
    )


def gate_context(
    *,
    stage: str = "paper",
    params: dict[str, Any] | None = None,
    run: CardRun | None = None,
    record: StageRecord | None = None,
    hyp: Hypothesis | None = None,
    ci90: tuple[float, float] | None = (-0.5, 0.5),
    folds: int = 4,
) -> GateContext:
    """What the monitor hands one gate, with only the piece under test varied."""
    measured = run or flat_run()
    expectation = None if ci90 is None else version(ci90=ci90).expectation.model_dump(mode="json")
    return GateContext(
        hyp=hyp or hypothesis(),
        construct="sleeve",
        stage=stage,
        params=params or {},
        window=measured.window,
        run=measured,
        research_folds=folds,
        snapshot_id="snap",
        strategy_sha="a" * 64,
        expectation=expectation,
        session=record,
    )
