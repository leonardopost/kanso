"""What deployment writes down: version state, named approvals, and what a stage realised.

Three kinds of record, each in the place the store already keeps that kind of thing.

**Version state** lives on `strategy_versions`: which stage a version occupies, what money
it was given and when it joined. Composition writes the version; putting it on a stage and
taking it off again are deployment's acts and deployment's columns.

**Approvals** live on `approvals`. A promotion to real capital exists only if a row here
names the operator who made it, and `deploy` reads that row rather than the portfolio file —
which is what makes editing the file by hand unable to move real money.

**What a stage realised** is an event. A node flattens before every stop, so each redeploy
closes a window and realises its result; the event carries that window's returns, its trades
and the book it held before it was flattened, which is what the paper and live gates read
and what the exposure limits are computed from. It is an event rather than a table because
it is exactly what the append-only log is for: a thing that happened, to a named subject, at
a time, that nothing later rewrites.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from kanso.criteria.run import CardRun, Fill, Trade

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.nautilus.node import Realised
    from kanso.state import StateStore

__all__ = [
    "APPROVAL_ACTION",
    "DEMOTED",
    "DEPLOYED",
    "PROMOTED",
    "STAGE_RUN",
    "Approval",
    "StageResult",
    "approvals",
    "approve",
    "approved",
    "clear_stage",
    "decode_run",
    "encode_run",
    "record_stage_run",
    "set_stage",
    "stage_results",
    "staged",
    "subject_of",
]

STAGE_RUN: Final = "stage_run"
"""The event one version's window of one stage node leaves behind."""

DEPLOYED: Final = "deployed"
PROMOTED: Final = "promoted"
DEMOTED: Final = "demoted"
"""The three events the portfolio appends over a version's life on the stages."""

APPROVAL_ACTION: Final = "promote"
"""What an approval row records: an operator moving a version onto the live stage."""


def subject_of(strategy_id: str, version: int) -> str:
    """How a version is named in an event, an approval and an escalation."""
    return f"{strategy_id}@{version}"


# --- version state ------------------------------------------------------------


def set_stage(
    store: StateStore,
    strategy_id: str,
    version: int,
    *,
    state: str,
    stage: str | None,
    capital: float | None = None,
    joined_at: datetime | None = None,
) -> None:
    """Move one version to a state and a stage, with the money and the moment it joined.

    The stage is cleared before it is set, because at most one version of a strategy holds
    a stage and the unique index enforces it: writing the newcomer first would collide with
    the version it replaces.
    """
    store.connection.execute(
        "UPDATE strategy_versions SET state = ?, stage = ?, capital = ?, joined_at = ?"
        " WHERE strategy_id = ? AND version = ?",
        (
            state,
            stage,
            capital,
            None if joined_at is None else joined_at.isoformat(),
            strategy_id,
            version,
        ),
    )


def clear_stage(store: StateStore, strategy_id: str, stage: str) -> list[int]:
    """Take whatever version holds this stage off it, and report which one did.

    The version keeps its state: what it becomes — retired when it is replaced, paper when
    it is demoted — is the caller's decision and is written in the same transaction.
    """
    rows = store.connection.execute(
        "SELECT version FROM strategy_versions WHERE strategy_id = ? AND stage = ?",
        (strategy_id, stage),
    ).fetchall()
    store.connection.execute(
        "UPDATE strategy_versions SET stage = NULL WHERE strategy_id = ? AND stage = ?",
        (strategy_id, stage),
    )
    return [int(row[0]) for row in rows]


def staged(store: StateStore, stage: str) -> dict[str, int]:
    """Which version of each strategy the record puts on this stage, by strategy id.

    This is what a deployment wrote, so it is the answer to "what is on the stage?" —
    `portfolio.yaml` is where an operator may add an entry no node ever ran, and the file
    is read against this rather than believed. One version per strategy per stage is a
    unique index of the store, so a strategy appears here at most once.
    """
    rows = store.connection.execute(
        "SELECT strategy_id, version FROM strategy_versions WHERE stage = ?", (stage,)
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


# --- approvals ----------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """One named operator act: who moved which version onto real capital, and when."""

    strategy_id: str
    version: int
    operator: str
    created_at: str


def approve(store: StateStore, strategy_id: str, version: int, operator: str) -> Approval:
    """Record that a named operator approved this version for the live stage."""
    made = Approval(
        strategy_id=strategy_id,
        version=version,
        operator=operator,
        created_at=datetime.now(tz=UTC).isoformat(),
    )
    store.connection.execute(
        "INSERT INTO approvals (action, subject, approved_by, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            APPROVAL_ACTION,
            subject_of(strategy_id, version),
            operator,
            json.dumps({"strategy": strategy_id, "version": version}, sort_keys=True),
            made.created_at,
        ),
    )
    return made


def approvals(store: StateStore, strategy_id: str, version: int) -> list[Approval]:
    """Every approval recorded for one version, oldest first."""
    rows = store.connection.execute(
        "SELECT approved_by, created_at FROM approvals WHERE action = ? AND subject = ?"
        " ORDER BY approval_id",
        (APPROVAL_ACTION, subject_of(strategy_id, version)),
    ).fetchall()
    return [
        Approval(
            strategy_id=strategy_id,
            version=version,
            operator=str(row[0]),
            created_at=str(row[1]),
        )
        for row in rows
    ]


def approved(store: StateStore, strategy_id: str, version: int) -> bool:
    """Whether this exact version has an approval on record.

    Per version, never per strategy: an approval is an operator's judgement of the thing
    that was in front of them, and the next composition is a different thing.
    """
    return bool(approvals(store, strategy_id, version))


# --- what a stage realised ----------------------------------------------------


@dataclass(frozen=True)
class StageResult:
    """One version's realised window on one stage, as the event recorded it."""

    stage: str
    session_id: str
    strategy_id: str
    version: int
    capital: float
    run: CardRun
    positions: tuple[tuple[str, float, float], ...]

    @property
    def pnl(self) -> float:
        """What this window added to, or took from, the version's equity."""
        return sum(self.run.returns)

    @property
    def gross(self) -> float:
        """The absolute exposure this version held when the window closed."""
        return sum(abs(qty * price) for _, qty, price in self.positions)

    @property
    def net(self) -> float:
        """The signed exposure this version held when the window closed."""
        return sum(qty * price for _, qty, price in self.positions)


def record_stage_run(
    store: StateStore, stage: str, session_id: str, realised: Sequence[Realised]
) -> list[StageResult]:
    """Append one event per version for the window a stage node just closed."""
    made: list[StageResult] = []
    for one in realised:
        positions = tuple((b.instrument_id, b.qty, b.price) for b in one.positions)
        detail: dict[str, object] = {
            "stage": stage,
            "session_id": session_id,
            "strategy": one.strategy_id,
            "version": one.version,
            "capital": one.capital,
            "run": encode_run(one.run),
            "positions": [
                {"instrument": name, "qty": qty, "price": price} for name, qty, price in positions
            ],
        }
        store.event(STAGE_RUN, subject_of(one.strategy_id, one.version), detail)
        made.append(
            StageResult(
                stage=stage,
                session_id=session_id,
                strategy_id=one.strategy_id,
                version=one.version,
                capital=one.capital,
                run=one.run,
                positions=positions,
            )
        )
    return made


def stage_results(
    store: StateStore,
    *,
    strategy_id: str | None = None,
    version: int | None = None,
    stage: str | None = None,
) -> list[StageResult]:
    """Every realised window recorded, oldest first, narrowed to one version or stage."""
    subject = None if strategy_id is None or version is None else subject_of(strategy_id, version)
    found = [
        _result(event.detail)
        for event in store.events(kind=STAGE_RUN, subject=subject)
        if stage is None or event.detail.get("stage") == stage
    ]
    if strategy_id is not None and subject is None:
        found = [one for one in found if one.strategy_id == strategy_id]
    return found


def _result(detail: Mapping[str, Any]) -> StageResult:
    """One recorded window back out of its event."""
    return StageResult(
        stage=str(detail["stage"]),
        session_id=str(detail["session_id"]),
        strategy_id=str(detail["strategy"]),
        version=int(detail["version"]),
        capital=float(detail["capital"]),
        run=decode_run(detail["run"]),
        positions=tuple(
            (str(p["instrument"]), float(p["qty"]), float(p["price"]))
            for p in detail.get("positions", [])
        ),
    )


# --- a measured run as plain data ---------------------------------------------


def encode_run(run: CardRun) -> dict[str, Any]:
    """One measured window as JSON-shaped data, losing nothing a gate reads."""
    return {
        "window": [run.window[0].isoformat(), run.window[1].isoformat()],
        "period": run.period,
        "period_ends_ns": list(run.period_ends_ns),
        "returns": list(run.returns),
        "equity": list(run.equity),
        "trades": [_encode_trade(trade) for trade in run.trades],
        "fills": [_encode_fill(fill) for fill in run.fills],
        "capital": run.capital,
        "currency": run.currency,
        "venue_model": dict(run.venue_model),
    }


def decode_run(payload: Mapping[str, Any]) -> CardRun:
    """One measured window back from its stored form."""
    opens, closes = payload["window"]
    return CardRun(
        window=(date.fromisoformat(opens), date.fromisoformat(closes)),
        period=str(payload["period"]),
        period_ends_ns=tuple(int(ts) for ts in payload["period_ends_ns"]),
        returns=tuple(float(value) for value in payload["returns"]),
        equity=tuple(float(value) for value in payload["equity"]),
        trades=tuple(_decode_trade(trade) for trade in payload["trades"]),
        fills=tuple(_decode_fill(fill) for fill in payload["fills"]),
        capital=float(payload["capital"]),
        currency=str(payload["currency"]),
        venue_model=dict(payload["venue_model"]),
    )


def _encode_fill(fill: Fill) -> dict[str, Any]:
    return {
        "ts_ns": fill.ts_ns,
        "instrument_id": fill.instrument_id,
        "side": fill.side,
        "qty": fill.qty,
        "px": fill.px,
        "cost": fill.cost,
    }


def _decode_fill(payload: Mapping[str, Any]) -> Fill:
    return Fill(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        side=str(payload["side"]),
        qty=float(payload["qty"]),
        px=float(payload["px"]),
        cost=float(payload["cost"]),
    )


def _encode_trade(trade: Trade) -> dict[str, Any]:
    return {
        "opened_ns": trade.opened_ns,
        "closed_ns": trade.closed_ns,
        "instrument_id": trade.instrument_id,
        "qty": trade.qty,
        "avg_open": trade.avg_open,
        "avg_close": trade.avg_close,
        "pnl_net": trade.pnl_net,
        "cost": trade.cost,
        "fills": [_encode_fill(fill) for fill in trade.fills],
    }


def _decode_trade(payload: Mapping[str, Any]) -> Trade:
    return Trade(
        opened_ns=int(payload["opened_ns"]),
        closed_ns=int(payload["closed_ns"]),
        instrument_id=str(payload["instrument_id"]),
        qty=float(payload["qty"]),
        avg_open=float(payload["avg_open"]),
        avg_close=float(payload["avg_close"]),
        pnl_net=float(payload["pnl_net"]),
        cost=float(payload["cost"]),
        fills=tuple(_decode_fill(fill) for fill in payload["fills"]),
    )
