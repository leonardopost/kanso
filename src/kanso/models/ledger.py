"""The spend ledger: one row per model call, including the calls that failed.

Every attempt the router makes is written here before its answer is judged, because a
rejected answer was still generated and still billed. A ledger that recorded only the
successful attempt would under-report exactly the runs that cost the most — the ones that
retried and escalated — and the number an operator watches would drift from the invoice.

Nothing in this module can stop research. Spend is reported, never enforced: a budget that
halts a run mid-card would leave the run in a state no rule describes, and an operator who
wants a ceiling has one at the provider.

A row holds the model id, never a credential, and the cost is computed from the register's
own prices rather than from anything the provider says, so a workspace's ledger is
arithmetic the operator can check against the file they wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from kanso.state import StateStore

__all__ = ["NO_LANE", "LedgerEntry", "Spend", "cost_of", "ledger", "spend"]

NO_LANE = "-"
"""How a call made outside any lane — a register check — is keyed in a per-lane total."""

PER_MILLION = 1_000_000.0
"""The register states prices per million tokens, which is how providers quote them."""


@dataclass(frozen=True)
class LedgerEntry:
    """One model call as the ledger records it."""

    task_class: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    lane: str | None = None
    cache_hit: bool | None = None


@dataclass(frozen=True)
class Spend:
    """What a slice of the ledger adds up to.

    `cost` is the total over every row in the slice, including rows with no lane, so
    `cost` is not always the sum of `by_lane`'s values — a per-lane view of a call that
    belonged to no lane would be a fiction.
    """

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    by_lane: Mapping[str, float] = field(default_factory=dict)
    by_day: Mapping[str, float] = field(default_factory=dict)


def cost_of(tokens_in: int, tokens_out: int, cost_in: float, cost_out: float) -> float:
    """What a call cost, from the register's per-million prices.

    Cached input tokens count at the full input price: a register carries one input price
    per model and providers discount cache reads, so this is an upper bound on the true
    cost rather than an estimate that could ever under-report.
    """
    return (tokens_in * cost_in + tokens_out * cost_out) / PER_MILLION


def ledger(store: StateStore, entry: LedgerEntry) -> None:
    """Record one call. Called for every attempt, whatever the attempt produced."""
    store.connection.execute(
        "INSERT INTO spend (ts, lane, task_class, model, tokens_in, tokens_out, cost,"
        " cache_hit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(tz=UTC).isoformat(),
            entry.lane,
            entry.task_class,
            entry.model,
            entry.tokens_in,
            entry.tokens_out,
            entry.cost,
            None if entry.cache_hit is None else int(entry.cache_hit),
        ),
    )


def spend(store: StateStore, *, day: date | None = None, lane: str | None = None) -> Spend:
    """What the ledger holds, optionally for one UTC day or one lane.

    Timestamps are ISO-8601 in UTC, so a day is a prefix match on the stored text and
    needs no date arithmetic in SQL.
    """
    sql = "SELECT lane, ts, tokens_in, tokens_out, cost FROM spend"
    where: list[str] = []
    params: list[object] = []
    if day is not None:
        where.append("ts LIKE ?")
        params.append(f"{day.isoformat()}%")
    if lane is not None:
        where.append("lane = ?")
        params.append(lane)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = store.connection.execute(sql, params).fetchall()

    by_lane: dict[str, float] = {}
    by_day: dict[str, float] = {}
    calls = tokens_in = tokens_out = 0
    total = 0.0
    for row in rows:
        calls += 1
        tokens_in += int(row["tokens_in"])
        tokens_out += int(row["tokens_out"])
        cost = float(row["cost"])
        total += cost
        key = str(row["lane"]) if row["lane"] is not None else NO_LANE
        by_lane[key] = by_lane.get(key, 0.0) + cost
        stamp = str(row["ts"])[:10]
        by_day[stamp] = by_day.get(stamp, 0.0) + cost
    return Spend(
        calls=calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=total,
        by_lane=dict(sorted(by_lane.items())),
        by_day=dict(sorted(by_day.items())),
    )
