"""Runs and cards in the state store: the only place research writes its own history.

A run and a card are records, not files. `results.tsv` is rendered from these rows, the
bytes of every `strategy.py` a card evaluated are blobs beside them, and the lane
directory holds nothing that is not recoverable from here — which is what lets a discard
restore a file without losing the experiment that produced it.

Three invariants live in SQL rather than in Python. A hypothesis has at most one active
run, enforced by a partial unique index on `ended_at IS NULL`, so two lanes cannot begin
the same hypothesis. A card's `strategy_sha` is a foreign key into the blob table, so a
card can only name bytes the store actually holds. `(run_id, seq)` is unique, so cards of
one run are ordered and countable without a scan.

`n_trials` counts every card of every run of the hypothesis — baselines and crashes
included — because it is the multiple-comparisons count a deflated Sharpe consumes, and
a trial that failed is still a trial that was tried.

Two readings of the same rows serve every command that names a card or runs against a
certified host: a sha prefix is resolved among one hypothesis's own cards, and the bytes
a host version pinned are read back from the blob table, refused by name when the store
no longer holds them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from kanso.errors import PreconditionError, ValidationError
from kanso.schemas import Card, GateResult, RunRecord, VenueModel

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.classify.construct import HostRef
    from kanso.state import StateStore

__all__ = [
    "active",
    "best_of",
    "card_sha",
    "cards_of",
    "close",
    "host_sources",
    "insert",
    "n_trials",
    "next_tag",
    "now",
    "record_card",
    "require_active",
    "runs_of",
    "set_best",
    "unset_best",
]

_HEX: Final = frozenset("0123456789abcdef")

_RUN_COLUMNS = (
    "run_id",
    "hyp_id",
    "tag",
    "lane",
    "dir",
    "base_sha",
    "hypothesis_sha",
    "program_sha",
    "snapshot_id",
    "criteria_version",
    "host_version",
    "card_budget_s",
    "baseline_wall_s",
    "baseline_peak_mem_gb",
    "best_sha",
    "best_metric",
    "started_at",
    "ended_at",
)

_INSERT_RUN = (
    f"INSERT INTO runs ({', '.join(_RUN_COLUMNS)}) VALUES ({', '.join('?' * len(_RUN_COLUMNS))})"
)

_CARD_COLUMNS = (
    "run_id",
    "hyp_id",
    "seq",
    "lane",
    "strategy_sha",
    "status",
    "metric",
    "metric_se",
    "n_trials",
    "n_trades",
    "wall_s",
    "peak_mem_gb",
    "aligned",
    "gate_results",
    "crash_tail",
    "venue_model",
    "description",
    "created_at",
)

_INSERT_CARD = (
    f"INSERT INTO cards ({', '.join(_CARD_COLUMNS)}) VALUES ({', '.join('?' * len(_CARD_COLUMNS))})"
)


def now() -> datetime:
    """The instant a record is stamped with."""
    return datetime.now(tz=UTC)


def insert(store: StateStore, record: RunRecord) -> RunRecord:
    """Write a run record, refusing a second active run for the same hypothesis."""
    values = (
        record.run_id,
        record.hyp_id,
        record.tag,
        record.lane,
        record.dir,
        record.base_sha,
        record.hypothesis_sha,
        record.program_sha,
        record.snapshot_id,
        record.criteria_version,
        record.host_version,
        record.card_budget_s,
        record.baseline_wall_s,
        record.baseline_peak_mem_gb,
        record.best_sha,
        record.best_metric,
        record.started_at.isoformat(),
        None if record.ended_at is None else record.ended_at.isoformat(),
    )
    try:
        store.connection.execute(_INSERT_RUN, values)
    except sqlite3.IntegrityError as exc:
        raise PreconditionError(
            f"{record.hyp_id} already has an active run: {exc}",
            remedy=f"end it with `kanso research end {record.hyp_id}`",
        ) from None
    return record


def active(store: StateStore, hyp_id: str) -> RunRecord | None:
    """This hypothesis's active run, or `None` when it has none."""
    row = store.connection.execute(
        "SELECT * FROM runs WHERE hyp_id = ? AND ended_at IS NULL", (hyp_id,)
    ).fetchone()
    return None if row is None else _run(row)


def require_active(store: StateStore, hyp_id: str, lane: str | None = None) -> RunRecord:
    """The active run, refusing when there is none or when another lane owns it."""
    run = active(store, hyp_id)
    if run is None:
        raise PreconditionError(
            f"{hyp_id} has no active run",
            remedy=f"start one with `kanso research begin {hyp_id}`",
        )
    if lane is not None and run.lane != lane:
        raise PreconditionError(
            f"{hyp_id} is being researched in lane {run.lane!r}, not {lane!r}; lanes never "
            "share a lane directory",
            remedy=f"work in {run.dir}, the lane directory of the active run",
        )
    return run


def runs_of(store: StateStore, hyp_id: str) -> list[RunRecord]:
    """Every run of a hypothesis, oldest first."""
    rows = store.connection.execute(
        "SELECT * FROM runs WHERE hyp_id = ? ORDER BY started_at, run_id", (hyp_id,)
    ).fetchall()
    return [_run(row) for row in rows]


def close(store: StateStore, run: RunRecord) -> RunRecord:
    """Stamp a run as ended and return the closed record."""
    ended = now()
    store.connection.execute(
        "UPDATE runs SET ended_at = ? WHERE run_id = ?", (ended.isoformat(), run.run_id)
    )
    return run.model_copy(update={"ended_at": ended})


def next_tag(store: StateStore, hyp_id: str, today: date) -> str:
    """`<yyyymmdd>-<n>`: the nth run of this hypothesis started on this date."""
    day = f"{today.year:04d}{today.month:02d}{today.day:02d}"
    row = store.connection.execute(
        "SELECT COUNT(*) FROM runs WHERE hyp_id = ? AND tag LIKE ?", (hyp_id, f"{day}-%")
    ).fetchone()
    return f"{day}-{int(row[0]) + 1}"


def set_best(store: StateStore, run: RunRecord, sha: str, metric: float) -> RunRecord:
    """Point the run's and the hypothesis's `best` at this card and return the run."""
    store.connection.execute(
        "UPDATE runs SET best_sha = ?, best_metric = ? WHERE run_id = ?",
        (sha, metric, run.run_id),
    )
    store.connection.execute(
        "UPDATE hypotheses SET best_sha = ?, best_metric = ?, best_run_id = ?, updated_at = ?"
        " WHERE hyp_id = ?",
        (sha, metric, run.run_id, now().isoformat(), run.hyp_id),
    )
    return run.model_copy(update={"best_sha": sha, "best_metric": metric})


def unset_best(store: StateStore, hyp_id: str) -> None:
    """Clear the hypothesis's `best`, as `--from-workspace` does."""
    store.connection.execute(
        "UPDATE hypotheses SET best_sha = NULL, best_metric = NULL, best_run_id = NULL,"
        " updated_at = ? WHERE hyp_id = ?",
        (now().isoformat(), hyp_id),
    )


def best_of(store: StateStore, hyp_id: str) -> tuple[str | None, float | None]:
    """The hypothesis-level `best`: the sha of its best card and that card's metric."""
    row = store.connection.execute(
        "SELECT best_sha, best_metric FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    if row is None:
        return None, None
    sha = None if row["best_sha"] is None else str(row["best_sha"])
    metric = None if row["best_metric"] is None else float(row["best_metric"])
    return sha, metric


def n_trials(store: StateStore, hyp_id: str) -> int:
    """Every card of every run of this hypothesis, baselines and crashes included."""
    row = store.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return int(row[0])


def record_card(store: StateStore, run: RunRecord, card: Card) -> Card:
    """Append one card to a run, in the order the run produced them."""
    row = store.connection.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM cards WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    values = (
        run.run_id,
        run.hyp_id,
        int(row[0]) + 1,
        card.lane,
        card.strategy_sha,
        card.status,
        card.metric,
        card.metric_se,
        card.n_trials,
        card.n_trades,
        card.wall_s,
        card.peak_mem_gb,
        int(card.aligned),
        json.dumps([gate.model_dump(by_alias=True) for gate in card.gate_results], sort_keys=True),
        card.crash_tail,
        json.dumps(card.venue_model.model_dump(mode="json"), sort_keys=True),
        card.desc,
        card.created_at.isoformat(),
    )
    store.connection.execute(_INSERT_CARD, values)
    return card


def cards_of(store: StateStore, hyp_id: str) -> list[Card]:
    """Every card of every run of this hypothesis, in the order they were recorded."""
    rows = store.connection.execute(
        "SELECT * FROM cards WHERE hyp_id = ? ORDER BY card_id", (hyp_id,)
    ).fetchall()
    return [_card(row) for row in rows]


def card_sha(store: StateStore, hyp_id: str, sha: str) -> str:
    """The one card sha of this hypothesis the given prefix names.

    A prefix is resolved among this hypothesis's own cards, so a prefix that is unique
    here is accepted however many other blobs the store holds, and one naming a card of
    another hypothesis is refused rather than silently served.
    """
    prefix = sha.strip().lower()
    if not prefix or not set(prefix) <= _HEX:
        raise ValidationError(
            f"sha: {sha!r} is not a strategy sha or a prefix of one",
            remedy=f"pass the hex prefix of a card's sha, as `kanso research show {hyp_id}` "
            "prints it",
        )
    rows = store.connection.execute(
        "SELECT DISTINCT strategy_sha FROM cards WHERE hyp_id = ? AND strategy_sha >= ?"
        " AND strategy_sha < ? ORDER BY strategy_sha LIMIT 2",
        (hyp_id, prefix, prefix + "g"),
    ).fetchall()
    if not rows:
        raise ValidationError(
            f"sha: no card of {hyp_id} starts with {prefix!r}",
            remedy=f"run `kanso research show {hyp_id}` to list its cards",
        )
    if len(rows) > 1:
        raise ValidationError(
            f"sha: {prefix!r} is ambiguous within {hyp_id}: "
            f"{', '.join(str(row[0])[:12] for row in rows)}",
            remedy="pass more characters of the sha",
        )
    return str(rows[0][0])


def host_sources(
    store: StateStore, host: HostRef
) -> tuple[bytes, tuple[tuple[str, bytes, Mapping[str, Any]], ...]]:
    """The host version's own sleeve bytes and the constructs already attached to it."""
    sleeve = _blob(store, host.sleeve.strategy_sha, f"the sleeve of {host.strategy_id}")
    attached = tuple(
        (
            ref.construct,
            _blob(store, ref.strategy_sha, f"the {ref.construct} attached to {host.strategy_id}"),
            dict(ref.params or {}),
        )
        for ref in host.attached
    )
    return sleeve, attached


def _blob(store: StateStore, sha: str, what: str) -> bytes:
    if not store.has_blob(sha):
        raise PreconditionError(
            f"{what} is recorded under {sha[:7]}, and this workspace holds no such bytes",
            remedy="restore the state store this strategy was certified in",
        )
    return store.get_blob(sha)


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord.model_validate({name: row[name] for name in _RUN_COLUMNS})


def _card(row: sqlite3.Row) -> Card:
    gates: Any = json.loads(str(row["gate_results"]))
    model: Any = json.loads(str(row["venue_model"]))
    return Card(
        run_id=str(row["run_id"]),
        lane=str(row["lane"]),
        strategy_sha=str(row["strategy_sha"]),
        metric=float(row["metric"]),
        metric_se=float(row["metric_se"] or 0.0),
        n_trials=int(row["n_trials"]),
        n_trades=int(row["n_trades"]),
        wall_s=float(row["wall_s"]),
        peak_mem_gb=float(row["peak_mem_gb"] or 0.0),
        status=str(row["status"]),  # type: ignore[arg-type]
        desc=str(row["description"]),
        aligned=bool(row["aligned"]),
        gate_results=[GateResult.model_validate(gate) for gate in gates],
        crash_tail=None if row["crash_tail"] is None else str(row["crash_tail"]),
        venue_model=VenueModel.model_validate(model),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
