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
deflation gates read their spread from: the cards that ran to a result and traded, scored
under the objective being deflated. A crash and a card that placed no order are edits that
failed, not candidates the selection could have chosen, so counting them widens the search
on paper without widening it in fact; a card scored under another objective is a number in
other units, so a spread taken across it measures a search that never happened. How many
draws the maximum was taken over has no units, so that count is `candidates`, the same
cards under every objective: a strategy carried across a re-pin was chosen by the cards
before it. A
redundant card is in both, because it is the other way round — it ran, it traded, and the
selection ranked its number and declined it. That another candidate held the same book
makes it a correlated trial, not a non-trial, and dropping it was measured costing
certificates an `n_trials` of 7 and 8 against roughly a thousand backtests run and
selected over — a hundredfold in the count they deflate by. What a correlated trial is
worth is not settled by counting it: the metric enters the spread as well as the count,
and a set dominated by repeats of one book narrows the spread faster than the count
widens the term beside it. `docs/backlog.md` row 59 holds the measurement of both
directions and the reason nothing else changes here. `candidates` counts those cards under
every objective: a card gate that holds one card's edge to the width of the search behind it
needs how many draws there were and no spread, so it has no units to keep apart.

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
the same pins ask the same question of the same data. That is a premise about the book and
not about the number beside it: those pins fix no capital, no fold count, no return period,
no cost model and no host version, and each of those moves a metric while every one of them
stands still. So the number is stored with a digest of what it was measured under
(`loop.Setup.measured_under`), and a row measured under another reading is matched by
nothing — and kept beside it rather than replaced by it, because a reading the selection
cannot reach is still the only anchor a run under that reading has. Signatures are stored
for every judged run, a redundant one included, so the third spelling of one idea is
refused against the second as well as the first. The comparison is
by day rather than by instant because it is a fact about sessions, and a day is what two
runs over the same window share. A stored signature is only ever compared with one read
the same way, so the change of reading came with the migration that emptied the table.

Nothing deletes a signature as research goes on, so the table keeps every book stored under
pins the workspace has since moved past — a re-pinned file, an upgraded kanso, a newer
snapshot, a retired hypothesis — and those can outgrow everything else the store holds:
measured on 2026-10-02 in a live workspace, 3,025 MB of books in a 3,399 MB `state.db`, in
17,105 rows of which 250 sat under pins a run could still be given. `superseded` names the
rest and `prune` deletes it, for `kanso state prune` to call with the daemon held off.

A card that ran keeps its **measurement**: what its metric was computed from — the objective
and folds, the run the card child measured, the host and benchmark runs the objective read
beside it, and the criteria version of the kanso that scored it — stored as one blob,
canonical JSON compressed with zlib, whose sha is the card's `measured_sha`. A metric is one
reading of a run; the run is what every other reading needs, and before cards kept it none
survived the card that measured it. `Measurement` scores itself with the installed
objective's own `compute`, which is the call the loop scored the card with, so under the
criteria version it records what it returns is the card's `metric` and `metric_se` to the
bit. Under another it is that kanso's arithmetic on the same run, which a release that
changes how an objective or its error is computed moves, and the version it records is how a
reader tells the two apart. `between` restricts it to a span of its window for a reading the
loop never took. The runs are encoded by the one codec every stored run goes through
(`kanso.criteria.codec`); the same measurement is the same bytes, so a strategy judged twice
under the same pins and the same reading, by the same kanso, is stored once. Nothing deletes a
measurement, and a card refused before or during its backtest, a crash and a card recorded
before cards kept one name none.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import yaml

from kanso.criteria.codec import decode_run, encode_run
from kanso.criteria.library import objectives
from kanso.criteria.run import CardRun, day_of
from kanso.errors import PreconditionError
from kanso.schemas import Card, CardDigest, GateResult, ObjectiveRef, RunRecord, VenueModel

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore

__all__ = [
    "Match",
    "Measurement",
    "Signature",
    "Superseded",
    "active",
    "best_of",
    "candidates",
    "family",
    "cards_of",
    "close",
    "encode_measurement",
    "insert",
    "matched_book",
    "measurement_of",
    "n_trials",
    "next_tag",
    "now",
    "objective_of",
    "prune",
    "read_measurement",
    "record_card",
    "record_measurement",
    "record_signature",
    "require_active",
    "runs_of",
    "set_best",
    "signature",
    "superseded",
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
    "digest",
    "measured_sha",
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


def family(store: StateStore, hyp_id: str) -> tuple[str, ...]:
    """The hypothesis and every hypothesis it derives from, transitively, nearest first.

    Read off each registered file's `derived_from`, as the store holds it under the
    hypothesis's newest registration. A parent the workspace has not registered counts
    nothing — it has no cards to count — and is left out; a cycle is walked once. A
    hypothesis that derives from none is a family of one, so every count below is what it
    always was for it. The trials a search is priced by are the family's, because the
    cards that chose a draft are cards the selection could have kept: an `explore` draft
    was chosen by scores measured on its parent's research, and a sibling an operator
    wrote after a searched hypothesis's lanes ran hundreds of cards was chosen the same
    way, so a new file does not start the search at one trial.
    """
    found: list[str] = []
    queue = [hyp_id]
    while queue:
        current = queue.pop(0)
        if current in found:
            continue
        row = store.connection.execute(
            "SELECT hypothesis_sha FROM hypotheses WHERE hyp_id = ?", (current,)
        ).fetchone()
        if row is None:
            continue
        found.append(current)
        parents = _derived_from(store, row["hypothesis_sha"])
        queue.extend(parent for parent in parents if parent not in found)
    return tuple(found) or (hyp_id,)


def _derived_from(store: StateStore, sha: object) -> tuple[str, ...]:
    """The `derived_from` a registered hypothesis file names, read as a plain mapping so a
    file pinned under an older schema is still read; nothing for a file that names none."""
    if sha is None or not store.has_blob(str(sha)):
        return ()
    found = yaml.safe_load(store.get_blob(str(sha)).decode("utf-8"))
    parents = found.get("derived_from") if isinstance(found, dict) else None
    if not isinstance(parents, list):
        return ()
    return tuple(str(parent) for parent in parents)


def _placeholders(ids: Sequence[str]) -> str:
    return ", ".join("?" for _ in ids)


def n_trials(store: StateStore, hyp_id: str) -> int:
    """Every card of every run of this hypothesis and of every hypothesis it derives from,
    baselines and crashes included."""
    ids = family(store, hyp_id)
    row = store.connection.execute(
        f"SELECT COUNT(*) FROM cards WHERE hyp_id IN ({_placeholders(ids)})", ids
    ).fetchone()
    return int(row[0])


def trial_metrics(store: StateStore, hyp_id: str, objective_id: str) -> list[float]:
    """The metric of every card of this hypothesis, and of every hypothesis it derives from,
    that ran to a result, traded, and was scored under `objective_id`.

    The multiple-comparisons set: one entry per candidate the selection could have kept,
    so its count and its spread describe the same search. A crash produced no metric to
    compare and a card that placed no order did not trade the hypothesis, and neither is
    excluded from `n_trials`, which counts cards.

    A card scored under another objective is excluded. A hypothesis re-pinned from a
    per-trade edge to a contribution has a search in basis points per trade behind it and one
    in basis points per period, and a spread taken across both is the spread of neither:
    measured on an operator's workspace in October 2026, mixing the two moved one expected
    maximum from 0.055 to 1.82. Those cards still count toward how many draws the maximum is
    taken over (`candidates`), which is a count and carries no units, so the strategy carried
    across the re-pin is priced for the search that chose it; and a certification behind two
    such cards with fewer than two of these is refused (`kanso.certify.run.unpriced`), since
    its spread cannot be read. The objective is read from the hypothesis file each card's run
    pinned, as a plain mapping, so a pin written under an older schema is still read. Nothing
    else about the pins narrows the set: a re-pin under the same objective, a newer snapshot or
    a kanso upgrade is the same search continuing, and a set that reset at each of them would
    price a search by its latest stretch.

    A redundant card is one of these. The keep rule is asked before the signature, so a
    candidate that beat the best would have been kept whatever it resembled: it is a
    candidate the selection could have chosen, and it is here because the deflation prices
    the candidates the search ran, not the ones a reader would call distinct. Whether
    trials that repeat each other should count for less than a whole one is the same
    question the hill-climbing path raises and is open on the same terms
    (`docs/backlog.md` row 59) -- an answer would be one stated rule with its own test,
    never a set quietly narrowed until a bar is passed.
    """
    ids = family(store, hyp_id)
    rows = store.connection.execute(
        "SELECT cards.metric, runs.hypothesis_sha FROM cards JOIN runs USING (run_id)"
        f" WHERE cards.hyp_id IN ({_placeholders(ids)})"
        " AND cards.status != 'crash' AND cards.n_trades > 0"
        " ORDER BY cards.card_id",
        ids,
    ).fetchall()
    scored: dict[str, str | None] = {}
    for row in rows:
        pin = str(row["hypothesis_sha"])
        if pin not in scored:
            scored[pin] = objective_of(store, pin)
    return [
        float(row["metric"]) for row in rows if scored[str(row["hypothesis_sha"])] == objective_id
    ]


def candidates(store: StateStore, hyp_id: str) -> int:
    """How many cards of this hypothesis, and of every hypothesis it derives from, ran to a
    result and traded, under any objective.

    The cards `trial_metrics` reads, before it narrows them to one objective: that set holds
    a spread, which must be in one objective's units, while this is a count of draws, which
    has none — of an edge measured in its own errors, which is what `edge_per_trade` holds a
    card to, and of the candidates a deflation's expected maximum is taken over. A re-pin to
    another objective continues the search, so the cards before it still count.
    """
    ids = family(store, hyp_id)
    row = store.connection.execute(
        f"SELECT COUNT(*) FROM cards WHERE hyp_id IN ({_placeholders(ids)})"
        " AND status != 'crash' AND n_trades > 0",
        ids,
    ).fetchone()
    return int(row[0])


def objective_of(store: StateStore, pin: str) -> str | None:
    """The objective a pinned hypothesis file names, or `None` when it names none."""
    found = yaml.safe_load(store.get_blob(pin).decode("utf-8"))
    objective = found.get("objective") if isinstance(found, dict) else None
    return str(objective["id"]) if isinstance(objective, dict) and "id" in objective else None


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
        None
        if card.digest is None
        else json.dumps(card.digest.model_dump(mode="json"), sort_keys=True),
        card.measured_sha,
    )
    store.connection.execute(_INSERT_CARD, values)
    return card


@dataclass(frozen=True)
class Measurement:
    """What a card's metric was computed from, kept with the card.

    Exactly the arguments the loop scores a card with: the objective its run's pinned
    hypothesis names, with the keep rule's two parameters; the folds `[research] folds` cut
    the window into; the run the card child measured; and the host-alone run and the
    benchmark run the objective reads beside it, each `None` where the run had none — every
    objective is handed both and reads the one its mode names.

    Beside them, `criteria_version` is what `criteria.library.criteria_version` was when
    the card was scored: the kanso whose arithmetic the card's numbers are. It is not read
    off the run, whose pin is the kanso the run began under, because a run is carded by
    whichever kanso is installed when its next card runs. So under that criteria version
    `compute` is the card's `metric` and `metric_se` exactly, and a reading the loop never
    took is the same arithmetic on `between`. Under another, `compute` is the installed
    objective on the same run — which a release that changed how an objective or its error is
    computed makes another number — so a reading set beside the card's own is one arithmetic
    only where the two versions agree. The version is the package version and a digest of
    the criteria library's files, which a release moves and an edit to an objective's code
    between two development builds of one version does not.
    """

    objective: ObjectiveRef
    folds: int
    run: CardRun
    host: CardRun | None = None
    benchmark: CardRun | None = None
    _: KW_ONLY
    criteria_version: str

    def compute(self) -> tuple[float, float]:
        """The installed objective over the folds: the metric and the error of that mean."""
        found = objectives()[self.objective.id]
        return found.compute(self.run, self.folds, self.host, self.benchmark)

    def between(self, opens: int, closes: int) -> Measurement:
        """This measurement restricted to the half-open instant span `[opens, closes)`.

        The host and the benchmark are restricted with the run, because a relative or
        benchmark objective cuts each into the same number of folds over its own window, and
        a whole-window host beside a prefix of the run would pair fold `i` of one span with
        fold `i` of another.
        """
        return replace(
            self,
            run=self.run.between(opens, closes),
            host=None if self.host is None else self.host.between(opens, closes),
            benchmark=None if self.benchmark is None else self.benchmark.between(opens, closes),
        )

    def payload(self) -> dict[str, Any]:
        """The measurement as JSON-shaped data, each run in the codec's form."""
        return {
            "objective": self.objective.model_dump(mode="json"),
            "folds": self.folds,
            "criteria_version": self.criteria_version,
            "run": encode_run(self.run),
            "host": None if self.host is None else encode_run(self.host),
            "benchmark": None if self.benchmark is None else encode_run(self.benchmark),
        }


def encode_measurement(measured: Measurement) -> bytes:
    """A measurement as the bytes its blob holds: canonical JSON — sorted keys, no
    whitespace — compressed with zlib.

    One measurement is one byte string, so a strategy judged twice under the same pins and
    the same reading by the same kanso, which measures the same run, is stored once: the
    second judging's blob is the row the first one wrote.
    """
    text = json.dumps(measured.payload(), sort_keys=True, separators=(",", ":"))
    return zlib.compress(text.encode("utf-8"))


def record_measurement(store: StateStore, measured: Measurement) -> str:
    """Store a measurement as one blob and return its sha, for the card's `measured_sha`."""
    return store.put_blob(encode_measurement(measured))


def measurement_of(store: StateStore, card: Card) -> Measurement | None:
    """What this card's metric was computed from, or `None` when it kept none: a crash, a
    card refused before or during its backtest, one recorded before cards kept it."""
    if card.measured_sha is None:
        return None
    return read_measurement(json.loads(zlib.decompress(store.get_blob(card.measured_sha))))


def read_measurement(payload: Mapping[str, Any]) -> Measurement:
    """A measurement back from its JSON form: the blob's, or `measurement` in what
    `kanso research show ID --measured --json` prints."""
    return Measurement(
        objective=ObjectiveRef.model_validate(payload["objective"]),
        folds=int(payload["folds"]),
        run=decode_run(payload["run"]),
        host=None if payload["host"] is None else decode_run(payload["host"]),
        benchmark=None if payload["benchmark"] is None else decode_run(payload["benchmark"]),
        criteria_version=str(payload["criteria_version"]),
    )


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
    store: StateStore,
    run: RunRecord,
    sha: str,
    held: Signature,
    metric: float,
    measured: str,
) -> None:
    """Store what these bytes held under this run's pins, what they earned, and under what.

    The same bytes under the same pins and the same reading are written once: the second
    judging replaces the row, book and number together, because the same bytes over the
    same data measured the same way twice are one fact and not two. Under a second reading
    they are a second row and not a replacement — the reading is the fifth column of the
    key (`state/migrations/0008_signature_reading_is_a_key.sql`) — because the selection
    reads a row only under the reading it was measured with, so replacing would leave a
    run under the first reading with no anchor at all: an operator who sets
    `[research] folds = 3`, runs cards and sets it back to 4 would lose the four-fold
    anchor of every strategy re-judged in between.

    `metric` is the objective the card was just recorded with, so a stored book is never on
    record for a number no card carries, and `measured` is what that number was measured
    under (`loop.Setup.measured_under`) — the pins say which question was asked and this
    says which arithmetic answered it.
    """
    store.connection.execute(
        "INSERT OR REPLACE INTO signatures (strategy_sha, hyp_id, hypothesis_sha, snapshot_id,"
        " criteria_version, signature, sessions, metric, measured_under, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sha,
            run.hyp_id,
            run.hypothesis_sha,
            run.snapshot_id,
            run.criteria_version,
            json.dumps(held, sort_keys=True),
            len(held),
            metric,
            measured,
            now().isoformat(),
        ),
    )


def matched_book(
    store: StateStore,
    run: RunRecord,
    held: Signature,
    pct: int,
    metric: float,
    floor: float,
    measured: str,
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

    That preference is why the rows are read in two passes rather than one. `json.loads`
    of a stored book is what this function costs, and a row whose number disagreed can
    never beat an agreeing one however much of it matched, so the books outside the floor
    are parsed only when nothing inside it qualified. What that is worth is small and
    measured rather than argued: over the four largest pin groups of the live workspace of
    2026-09-18, driving this function on the books held out of each, a card costs 0.5 s to
    1.0 s against groups of 23 MB to 46 MB, and the second pass falls away on 0.2% to 7.4%
    of it. It is small because a pin group's numbers sit on top of each other — 88% to
    99.6% of the pairs within one group fall inside that hypothesis's floor, against the
    20% the same measurement gives over a whole hypothesis's numbers — and because the
    second pass falls away exactly when an agreeing row matched, which is most of the
    time: over the period in which any book was on record at all, 63.7% of `sox_main_b`'s
    judged candidates found an agreeing anchor, and 84.9% to 94.1% of the other four
    hypotheses' did. An earlier reading of this put `sox_main_b` at 9%, by counting the
    3,043 of its 3,228 cards that were judged before the first signature row existed
    (`2026-09-18T01:16:57Z`); a candidate that matched nothing because the table was empty
    measures the table and not the matcher.
    What a card pays here is the size of the pool and the shape a book is parsed into, and
    neither is the ordering (`docs/backlog.md` row 89).

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

    A stored number is refused the same way when it was not measured under what this run
    measures under. The four pins fix the question and the data; they do not fix the
    capital, the folds, the return period, the cost model or the host version an attached
    construct is differenced against, and every one of those moves a number while every
    pin stands still (`loop.Setup.measured_under`). So `measured` is a fifth term of the
    selection rather than a check on the rows it returns: an anchor is a row measured the
    way this card was, and `= ?` is false for the NULL a row written before the column
    carries, which is the reading that column's migration states.
    """
    rows = store.connection.execute(
        "SELECT strategy_sha, signature, metric FROM signatures WHERE hyp_id = ?"
        " AND hypothesis_sha = ? AND snapshot_id = ? AND criteria_version = ?"
        " AND measured_under = ? ORDER BY created_at, rowid",
        (
            run.hyp_id,
            run.hypothesis_sha,
            run.snapshot_id,
            run.criteria_version,
            measured,
        ),
    ).fetchall()
    within: list[sqlite3.Row] = []
    beyond: list[sqlite3.Row] = []
    for row in rows:
        if row["metric"] is None:
            continue
        (within if abs(metric - float(row["metric"])) <= floor else beyond).append(row)
    agreeing = _closest(held, within, pct, agreed=True)
    return agreeing if agreeing is not None else _closest(held, beyond, pct, agreed=False)


def _closest(held: Signature, rows: list[sqlite3.Row], pct: int, *, agreed: bool) -> Match | None:
    """The row of `rows` whose book `held` matches on the largest share of their shared
    days, the earlier on a tie, or `None` when none of them reaches `pct`.

    Called twice by `matched_book`, over the rows whose numbers agreed with the
    candidate's and then over the rest, which is what keeps the second parse out of the
    common case: the ranking prefers an agreeing match to any disagreeing one, so once one
    agreeing row qualifies no row of the other set can win, and `json.loads` is the cost
    of this function. The rows arrive in the order the selection put them in, so the
    strict `>` leaves a tie with the row stored first.
    """
    closest: Match | None = None
    for row in rows:
        stored: Any = json.loads(str(row["signature"]))
        shared = held.keys() & stored.keys()
        if not shared:
            continue
        matched = sum(1 for day in shared if held[day] == stored[day])
        found = Match(str(row["strategy_sha"]), matched, len(shared), float(row["metric"]), agreed)
        if found.pct >= pct and (closest is None or found.pct > closest.pct):
            closest = found
    return closest


_LIVE_PINS: Final = (
    "SELECT hyp_id, hypothesis_sha, snapshot_id, criteria_version FROM runs"
    " WHERE ended_at IS NULL"
    " UNION"
    " SELECT runs.hyp_id, runs.hypothesis_sha, runs.snapshot_id, runs.criteria_version"
    " FROM runs JOIN hypotheses ON hypotheses.hyp_id = runs.hyp_id"
    " WHERE hypotheses.status != 'retired' AND runs.hypothesis_sha = hypotheses.hypothesis_sha"
    " AND runs.criteria_version = :criteria"
    " AND runs.started_at = (SELECT MAX(newer.started_at) FROM runs AS newer"
    " WHERE newer.hyp_id = runs.hyp_id AND newer.hypothesis_sha = runs.hypothesis_sha"
    " AND newer.criteria_version = runs.criteria_version)"
)
"""The pins a signature can still be selected under: see `superseded`."""

_SUPERSEDED: Final = (
    f"(hyp_id, hypothesis_sha, snapshot_id, criteria_version) NOT IN ({_LIVE_PINS})"
)


@dataclass(frozen=True)
class Superseded:
    """The stored books no run can select any more: how many, how large, and how many stay.

    `size` is the bytes of the books themselves; the pages deleting them frees hold a
    little more."""

    rows: int
    size: int
    kept: int


def superseded(store: StateStore, criteria: str) -> Superseded:
    """The signatures stored under pins no run can be given, with `criteria` the criteria
    version a run begun now would be pinned to (`criteria.library.criteria_version`).

    `matched_book` selects by a run's own pins and nothing else, so a row is reachable only
    while some run can carry its four. An open run carries its own until it ends, whatever
    it was begun under. A run begun now carries the file its hypothesis is registered under
    — `research begin` refuses any other — the criteria this kanso judges by, and the newest
    snapshot that covers its universe; so of a hypothesis's ended runs only the newest under
    that file and those criteria can share pins with the next one, and only when no newer
    covering snapshot has been taken since — a newer one leaves its rows kept and
    unreachable, which errs the safe way. A retired hypothesis begins nothing. Every row
    under any other pins is superseded: under a file since re-pinned, a criteria version
    since upgraded, a snapshot a newer run of the same file and criteria moved past, or a
    retired hypothesis.

    Superseded is not unrecoverable. Content addressing makes a pin a fact about bytes, so
    re-pinning a file's earlier bytes, reinstalling an earlier kanso or `hyp resume` on a
    retired hypothesis hands the old pins out again; a row pruned before that is a book the
    next run re-earns one card at a time, which is what `0004_signatures_reread.sql` cost
    every workspace once. Nothing already recorded depends on a row — no card, trial count,
    best or certificate; only the redundancy check of cards still to come reads the table.

    Every reading under a live pin is kept, since `record_signature` keeps them apart on
    purpose, and two runs of one file and criteria begun at the same instant are both the
    newest and both keep theirs.
    """
    row = store.connection.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(CAST(signature AS BLOB))), 0) AS size"
        f" FROM signatures WHERE {_SUPERSEDED}",
        {"criteria": criteria},
    ).fetchone()
    total = int(store.connection.execute("SELECT COUNT(*) FROM signatures").fetchone()[0])
    return Superseded(rows=int(row["n"]), size=int(row["size"]), kept=total - int(row["n"]))


def prune(store: StateStore, criteria: str) -> int:
    """Delete the signatures `superseded` counts, and return how many went."""
    cursor = store.connection.execute(
        f"DELETE FROM signatures WHERE {_SUPERSEDED}", {"criteria": criteria}
    )
    return int(cursor.rowcount)


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
        digest=None
        if row["digest"] is None
        else CardDigest.model_validate(json.loads(str(row["digest"]))),
        measured_sha=None if row["measured_sha"] is None else str(row["measured_sha"]),
    )
