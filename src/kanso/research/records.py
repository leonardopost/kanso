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
included — because it numbers the cards and stamps the certificate, and no card may be
dropped from a count that is part of a filename. `trial_metrics` is the narrower set the
deflated Sharpe consumes: the cards that ran to a result and traded. A crash and a card
that placed no order are edits that failed, not candidates the selection could have
chosen, so counting them widens the search on paper without widening it in fact. A
redundant card is in both, because it is the other way round — it ran, it traded, and the
selection ranked its number and declined it. That another candidate held the same book
makes it a correlated trial, not a non-trial, and dropping it was measured costing
certificates an `n_trials` of 7 and 8 against roughly a thousand backtests run and
selected over — a hundredfold in the count they deflate by. What a correlated trial is
worth is not settled by counting it: the metric enters the spread as well as the count,
and a set dominated by repeats of one book narrows the spread faster than the count
widens the term beside it. `docs/backlog.md` row 59 holds the measurement of both
directions and the reason nothing else changes here.

A run's best and the hypothesis's best are two records with two rules. `set_best` always
moves the run's, because a keep is a keep of its run; it moves the hypothesis's only when
the keep beats it, or when the hypothesis's best is already this run's own — a run that
owns it may rewind it, another run may only better it. So a run re-seeded from a lesser
keep climbs its own ancestry, and a drift rewind in one run leaves what another run earned
standing (`docs/backlog.md` row 61).

A **signature** is what a judged run held, day by day: for each UTC day the run measured
a period end in, the sorted (instrument, sign, at-an-end) marks of that day. Both of the
run's own records of a position are read — what it held at each period end, and the spans
of the positions it opened and closed — because a period end is a sample, and a sample
taken once a day says nothing about a strategy that is flat by the close. The two are
marked apart, so the reading is strictly finer than sampling the ends alone rather than a
coarser one that would call two daily strategies alike for holding on the same days.
Measured in a live workspace: of 201 signatures stored for an intraday hypothesis under a
daily return period, 192 recorded a position on none of their 834 sampled days, so every
candidate matched every other on all of them and every proposal after the first was
refused. Two strategies with the same signature on nearly every shared day made the same
bets, however differently they were written; whether they also earned the same result is
the second half of that sentence, and it used to be asserted rather than read. It is read
now — a signature is stored with the metric its card earned, and a candidate is redundant
only when its own number is within the hypothesis's noise floor of that one — because the
objective is computed before the refusal is decided and a premise that can be tested for
nothing should not be assumed. Signatures are stored per strategy under the run's pins —
the hypothesis file, the snapshot and the criteria — never per run, because two runs under
the same pins ask the same question of the same data; and they are stored for every
judged run, a redundant one included, so the third spelling of one idea is refused
against the second as well as the first. The comparison is by day rather than by instant
because it is a fact about sessions, and a day is what two runs over the same window
share. A stored signature is only ever compared with one read the same way, so the change
of reading came with the migration that emptied the table.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from kanso.criteria.run import CardRun, day_of
from kanso.errors import PreconditionError
from kanso.schemas import Card, GateResult, RunRecord, VenueModel

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore

__all__ = [
    "Match",
    "Signature",
    "active",
    "best_of",
    "cards_of",
    "close",
    "insert",
    "matched_book",
    "n_trials",
    "next_tag",
    "now",
    "record_card",
    "record_signature",
    "require_active",
    "runs_of",
    "set_best",
    "signature",
    "trial_metrics",
    "unset_best",
]

Signature = dict[str, list[list[object]]]
"""What a run held on each day it measured: the day, ISO-formatted, to the sorted
`[instrument_id, sign, at_an_end]` marks of that day. `at_an_end` is true for what the
run held at one of the day's period ends and false for what it held during the day and
at none of them, which is the difference between carrying a position over the close and
closing it before. A day with nothing open maps to `[]`, because being flat is a position
too, and no size enters, because a size is a parameter and a parameter is exactly what a
signature exists to see through. What the size did to the number is not lost with it: the
row carries the metric the book earned, and `matched_book` reads it."""

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
    "tags",
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
    """Point the run's `best` at this card, and the hypothesis's when this card beats it
    or the hypothesis's best is this run's own; return the run."""
    store.connection.execute(
        "UPDATE runs SET best_sha = ?, best_metric = ? WHERE run_id = ?",
        (sha, metric, run.run_id),
    )
    held = store.connection.execute(
        "SELECT best_metric, best_run_id FROM hypotheses WHERE hyp_id = ?", (run.hyp_id,)
    ).fetchone()
    standing = None if held is None or held["best_metric"] is None else float(held["best_metric"])
    owned = held is not None and held["best_run_id"] == run.run_id
    if standing is None or metric > standing or owned:
        store.connection.execute(
            "UPDATE hypotheses SET best_sha = ?, best_metric = ?, best_run_id = ?, updated_at = ?"
            " WHERE hyp_id = ?",
            (sha, metric, run.run_id, now().isoformat(), run.hyp_id),
        )
    return run.model_copy(update={"best_sha": sha, "best_metric": metric})


def unset_best(store: StateStore, hyp_id: str, *, run_id: str | None = None) -> None:
    """Clear the hypothesis's `best`, as `--from-workspace` does — or, given `run_id`,
    only when that run is the one that earned it, as a drift rewind does."""
    if run_id is not None:
        held = store.connection.execute(
            "SELECT best_run_id FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
        ).fetchone()
        if held is None or held["best_run_id"] != run_id:
            return
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


def trial_metrics(store: StateStore, hyp_id: str) -> list[float]:
    """The metric of every card of this hypothesis that ran to a result and traded.

    The multiple-comparisons set: one entry per candidate the selection could have kept,
    so its count and its spread describe the same search. A crash produced no metric to
    compare and a card that placed no order did not trade the hypothesis, and neither is
    excluded from `n_trials`, which counts cards.

    A redundant card is one of these. The keep rule is asked before the signature, so a
    candidate that beat the best would have been kept whatever it resembled: it is a
    candidate the selection could have chosen, and it is here because the deflation prices
    the candidates the search ran, not the ones a reader would call distinct. Whether
    trials that repeat each other should count for less than a whole one is the same
    question the hill-climbing path raises and is open on the same terms
    (`docs/backlog.md` row 59) -- an answer would be one stated rule with its own test,
    never a set quietly narrowed until a bar is passed.
    """
    rows = store.connection.execute(
        "SELECT metric FROM cards WHERE hyp_id = ? AND status != 'crash' AND n_trades > 0"
        " ORDER BY card_id",
        (hyp_id,),
    ).fetchall()
    return [float(row["metric"]) for row in rows]


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
        json.dumps(list(card.tags)),
        json.dumps([gate.model_dump(by_alias=True) for gate in card.gate_results], sort_keys=True),
        card.crash_tail,
        json.dumps(card.venue_model.model_dump(mode="json"), sort_keys=True),
        card.desc,
        card.created_at.isoformat(),
    )
    store.connection.execute(_INSERT_CARD, values)
    return card


def signature(run: CardRun) -> Signature:
    """What `run` held on each UTC day it measured a period end in, and whether at its end.

    The days are the run's period ends: the sessions two runs over the same window are
    both known to have measured. Each day is filled from both of the run's own records of
    a position. `CardRun.held` is what the runner extracted once per held instrument per
    period end, and marks its instrument and sign as held *at an end*. `CardRun.trades` is
    every position the run opened and closed, each carrying the instants it spanned, and
    marks each day it was open on — unless that instrument and sign were already held at
    an end of that day, which would say nothing new.

    The two marks are kept apart rather than merged, and that is the whole of the design.
    Merged, a day would read "something was held", and two daily strategies whose only
    difference is whether they carry the position over the close would read alike: measured
    on the demo, the card scoring 9.99 and the card scoring 3.15 matched on every session.
    Kept apart, the reading is strictly finer than sampling the ends alone — two runs that
    differed under that reading differ under this one, because their end marks differ —
    while a hypothesis that is flat at every end, and had no signature at all before, is
    now told apart by what it held during the day.

    Only the sign of a quantity enters, because a size is a parameter and a parameter is
    what a signature exists to see through; a day the book flipped carries both signs,
    since what was held over the day is the bet and the order is a detail.

    The days are taken from the period ends and then widened by the days the holdings
    themselves fall on, before the second reading is seeded from them — the runner samples
    `held` at the period ends and no other instant, so the widening is unreachable today,
    and seeding the two dicts from the same keys is what keeps it a widening rather than a
    `KeyError` if that ever stops being true.
    """
    ends: dict[date, set[tuple[str, int]]] = {day_of(ts): set() for ts in run.period_ends_ns}
    for held in run.held:
        ends.setdefault(day_of(held.ts_ns), set()).add((held.instrument_id, _sign(held.qty)))
    during: dict[date, set[tuple[str, int]]] = {day: set() for day in ends}
    for trade in run.trades:
        mark = (trade.instrument_id, _sign(trade.qty))
        for day in _spanned(trade.opened_ns, trade.closed_ns):
            if day in during:
                during[day].add(mark)
    return {
        day.isoformat(): sorted(
            [[name, sign, True] for name, sign in held]
            + [[name, sign, False] for name, sign in during[day] - held]
        )
        for day, held in sorted(ends.items())
    }


def _sign(qty: float) -> int:
    return 1 if qty > 0 else -1


def _spanned(opened_ns: int, closed_ns: int) -> list[date]:
    """Every UTC day a position was open on, its first and its last included."""
    first, last = day_of(opened_ns), day_of(closed_ns)
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


@dataclass(frozen=True)
class Match:
    """Which stored signature a candidate matched, on how much of it, and what it earned.

    `earned` is the anchor's own metric, which is half of why the candidate is redundant
    and the half no reading of the two books can show. The refusal names it beside the
    candidate's, because "your result is already known" is a claim about two numbers and
    an operator is owed both.

    `agreed` is that second half: whether the candidate's own metric came out within the
    hypothesis's noise floor of `earned`. A match that agreed is a repeat and the loop
    refuses the turn; a match that did not is an ordinary discard that nonetheless held a
    book already measured, which is the one thing about it worth telling the next
    proposal. Both are recorded, because a book matched is a fact either way and the
    card has no column for it.
    """

    like: str
    matched: int
    shared: int
    earned: float
    agreed: bool

    @property
    def pct(self) -> float:
        return 100.0 * self.matched / self.shared


def record_signature(
    store: StateStore, run: RunRecord, sha: str, held: Signature, metric: float
) -> None:
    """Store what these bytes held under this run's pins, and what they earned holding it.

    The same bytes are written once: a second judging under the same pins replaces the
    row, book and number together, because the same bytes over the same data measured
    twice are one fact and not two. `metric` is the objective the card was just recorded
    with, so a stored book is never on record for a number no card carries.
    """
    store.connection.execute(
        "INSERT OR REPLACE INTO signatures (strategy_sha, hyp_id, hypothesis_sha, snapshot_id,"
        " criteria_version, signature, sessions, metric, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sha,
            run.hyp_id,
            run.hypothesis_sha,
            run.snapshot_id,
            run.criteria_version,
            json.dumps(held, sort_keys=True),
            len(held),
            metric,
            now().isoformat(),
        ),
    )


def matched_book(
    store: StateStore, run: RunRecord, held: Signature, pct: int, metric: float, floor: float
) -> Match | None:
    """The stored signature under this run's pins that `held` matches on at least `pct`
    percent of their shared days, and whether its own metric is within `floor` of
    `metric` — the closest one that agreed, else the closest one at all, the earlier on a
    tie — or `None` when no stored book was matched.

    The agreeing match is preferred over a closer one that disagreed, so reporting the
    second kind can never turn a repeat into a discard: a candidate matching one book on
    every day whose number moved and another on 96% of them whose number did not is the
    repeat it always was. The caller refuses only `agreed`, and records both — the
    argument for recording a repeat is the corner it fills in and the record the next run
    reads, and neither of those is about the number that came back.

    Both clauses make the refusal, because the premise is both clauses. Two strategies
    that held the same book on nearly every shared day made the same bets, and the
    inference drawn from that is that they earned the same result; only the first half
    was ever checked, and the second was asserted. It does not need to be: the objective
    is computed before this is asked, so the claim can be tested at the cost of one
    number per stored book.

    Measured against the live workspace of 2026-09-18, over the 3,092 candidates its five
    refusing hypotheses turned away, with each hypothesis's median card standard error
    standing in for the candidate's own. Where the daily book determines the number the
    premise holds and the rule does not move: `sox_drag_budget`, whose thesis is a
    vol-target budget with a cash-withholding cap, refused 1,151 candidates of which 1,090
    matched one anchor scoring 0.4674, and their own scores sat a median 0.0039 and at most
    0.0931 from it against a floor of 0.2713 — so none of the 1,151 is admitted, and a size
    that moved no result is still not an experiment. `sox_drag` 0 of 742, `sox_main_b` 1 of
    325, `sox_dir` 31 of 549. Where it does not, the premise fails outright: `sox_touch` is
    intraday on a per-trade edge, and its 289 candidates matching anchor `ce5ef34` scored
    -18.780 .. 9.179 against that anchor's -4.5452 and a floor of 10.116, a median 9.096
    away and 74 of its 325 refusals further than the floor. 106 of 3,092 in all: the rule
    refuses a repeat as it did, and stops refusing a measurement that disagreed with what
    it was called a repeat of.

    `floor` is struck from the candidate's own standard error, not the anchor's, which is
    on record here and deliberately unread. The keep rule compares a candidate against
    `best_metric` on the candidate's floor and never consults the best card's; one card,
    one noise floor is the convention, and a second scale in the same loop would let two
    rules disagree about whether one difference is a difference.

    Every signature stored under the pins is a candidate, the best card's included: the
    caller applies the keep rule first, so what reaches here has already failed to beat
    the best, and matching it is the strongest reason of all to refuse the candidate. Two
    signatures with no day in common match nothing, and a stored book carrying no metric
    is nothing to compare against: a premise that cannot be tested has not been met, so it
    is no anchor (`state/migrations/0006_signature_metric.sql`).
    """
    rows = store.connection.execute(
        "SELECT strategy_sha, signature, metric FROM signatures WHERE hyp_id = ?"
        " AND hypothesis_sha = ? AND snapshot_id = ? AND criteria_version = ?"
        " ORDER BY created_at, rowid",
        (run.hyp_id, run.hypothesis_sha, run.snapshot_id, run.criteria_version),
    ).fetchall()
    closest: Match | None = None
    for row in rows:
        if row["metric"] is None:
            continue
        earned = float(row["metric"])
        stored: Any = json.loads(str(row["signature"]))
        shared = held.keys() & stored.keys()
        matched = sum(1 for day in shared if held[day] == stored[day])
        found = Match(
            str(row["strategy_sha"]), matched, len(shared), earned, abs(metric - earned) <= floor
        )
        if not shared or found.pct < pct:
            continue
        if closest is None or (found.agreed, found.pct) > (closest.agreed, closest.pct):
            closest = found
    return closest


def cards_of(store: StateStore, hyp_id: str) -> list[Card]:
    """Every card of every run of this hypothesis, in the order they were recorded."""
    rows = store.connection.execute(
        "SELECT * FROM cards WHERE hyp_id = ? ORDER BY card_id", (hyp_id,)
    ).fetchall()
    return [_card(row) for row in rows]


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord.model_validate({name: row[name] for name in _RUN_COLUMNS})


def _card(row: sqlite3.Row) -> Card:
    gates: Any = json.loads(str(row["gate_results"]))
    model: Any = json.loads(str(row["venue_model"]))
    tags: Any = json.loads(str(row["tags"]))
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
        tags=list(tags),
        gate_results=[GateResult.model_validate(gate) for gate in gates],
        crash_tail=None if row["crash_tail"] is None else str(row["crash_tail"]),
        venue_model=VenueModel.model_validate(model),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
