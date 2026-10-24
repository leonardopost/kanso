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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from kanso.criteria.codec import decode_run, encode_run
from kanso.criteria.run import CardRun

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
    "book_seed",
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
    positions: tuple[tuple[str, float, float, float], ...]
    """What the window closed holding, per instrument: `(instrument_id, qty, price,
    multiplier)`, the mark being the window's last price and the multiplier the instrument's
    contract size, so `qty x price x multiplier` is the exposure in the account currency."""
    benchmark: CardRun | None = None
    """The hold the version's objective differences against, over the same window; absent
    from a window recorded for a version whose objective measures none."""

    @property
    def pnl(self) -> float:
        """What this window added to, or took from, the version's equity."""
        return sum(self.run.returns)

    @property
    def gross(self) -> float:
        """The absolute exposure this version held when the window closed."""
        return sum(abs(qty * price * multiplier) for _, qty, price, multiplier in self.positions)

    @property
    def net(self) -> float:
        """The signed exposure this version held when the window closed."""
        return sum(qty * price * multiplier for _, qty, price, multiplier in self.positions)


def record_stage_run(
    store: StateStore, stage: str, session_id: str, realised: Sequence[Realised]
) -> list[StageResult]:
    """Append one event per version for the window a stage node just closed."""
    made: list[StageResult] = []
    for one in realised:
        positions = tuple((b.instrument_id, b.qty, b.price, b.multiplier) for b in one.positions)
        detail: dict[str, object] = {
            "stage": stage,
            "session_id": session_id,
            "strategy": one.strategy_id,
            "version": one.version,
            "capital": one.capital,
            "run": encode_run(one.run),
            "positions": [
                {"instrument": name, "qty": qty, "price": price, "multiplier": multiplier}
                for name, qty, price, multiplier in positions
            ],
        }
        if one.benchmark is not None:
            detail["benchmark"] = encode_run(one.benchmark)
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
                benchmark=one.benchmark,
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


def book_seed(results: Sequence[StageResult]) -> tuple[float, int | None]:
    """Where a version's book policy stood when its newest measured window closed.

    The cushion a monthly reset had set aside and the last period end that window settled,
    read from the newest recorded window that measured any period; zero and `None` when no
    window has. A window that measured nothing — the version's universe was quiet — moved
    nothing, so the one before it still holds the answer. The stage restarts flat at the
    version's capital either way: what carries across a restart is the cushion, and the end
    the next window's first carry and month turn are counted from.
    """
    for result in reversed(results):
        run = result.run
        if run.period_ends_ns:
            return (run.cushion[-1] if run.cushion else 0.0), run.period_ends_ns[-1]
    return 0.0, None


def _result(detail: Mapping[str, Any]) -> StageResult:
    """One recorded window back out of its event; a position recorded before the multiplier
    was kept reads as one, a share's."""
    return StageResult(
        stage=str(detail["stage"]),
        session_id=str(detail["session_id"]),
        strategy_id=str(detail["strategy"]),
        version=int(detail["version"]),
        capital=float(detail["capital"]),
        run=decode_run(detail["run"]),
        positions=tuple(
            (
                str(p["instrument"]),
                float(p["qty"]),
                float(p["price"]),
                float(p.get("multiplier", 1.0)),
            )
            for p in detail.get("positions", [])
        ),
        benchmark=None if detail.get("benchmark") is None else decode_run(detail["benchmark"]),
    )
