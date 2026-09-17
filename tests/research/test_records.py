"""Runs and cards as rows: what the store enforces, and what it hands back."""

from __future__ import annotations

from datetime import date

import pytest

from kanso.criteria.run import CardRun, Held, midnight_ns
from kanso.errors import PreconditionError
from kanso.research import loop, records
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, FLAT, RAISING, REVERTING, classify
from .test_scheduler import open_run


def test_a_hypothesis_that_never_ran_has_no_run_and_no_best(store: StateStore) -> None:
    assert records.active(store, "nobody_here") is None
    assert records.runs_of(store, "nobody_here") == []
    assert records.best_of(store, "nobody_here") == (None, None)
    assert records.n_trials(store, "nobody_here") == 0


def test_a_second_active_run_of_one_hypothesis_cannot_be_written(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    duplicate = run.model_copy(update={"run_id": "another", "tag": "20240102-1"})
    with pytest.raises(PreconditionError, match="already has an active run"):
        records.insert(store, duplicate)


def test_runs_come_back_in_the_order_they_were_started(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    first = loop.begin(ws, store, registered)
    loop.end(ws, store, registered)
    second = loop.begin(ws, store, registered)

    held = records.runs_of(store, registered)
    assert [run.run_id for run in held] == [first.run_id, second.run_id]
    assert held[0].ended_at is not None
    assert held[1].ended_at is None


def test_the_run_s_best_always_moves_and_the_hypothesis_s_only_when_beaten_or_owned(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """Two ancestries: a run re-seeded from a lesser keep climbs its own, and replaces
    the hypothesis's best only by beating it; the run that owns it may rewind it."""
    first = loop.begin(ws, store, registered)
    strong, weak, stronger = (store.put_blob(b) for b in (b"strong", b"weak", b"stronger"))
    records.set_best(store, first, strong, 2.0)
    loop.end(ws, store, registered)
    second = open_run(store, registered)  # a row, not a baseline: the blobs are not sleeves

    second = records.set_best(store, second, weak, 1.0)
    assert (second.best_sha, second.best_metric) == (weak, 1.0)
    assert records.best_of(store, registered) == (strong, 2.0), "not beaten"

    records.set_best(store, second, stronger, 3.0)
    assert records.best_of(store, registered) == (stronger, 3.0), "beaten"

    records.set_best(store, second, weak, 1.0)
    assert records.best_of(store, registered) == (weak, 1.0), "the owner rewinds its own"

    records.unset_best(store, registered, run_id=first.run_id)
    assert records.best_of(store, registered) == (weak, 1.0), "not this run's to clear"
    records.unset_best(store, registered, run_id=second.run_id)
    assert records.best_of(store, registered) == (None, None)
    records.unset_best(store, "nobody_here", run_id="x")


def test_a_card_comes_back_as_it_was_written(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    (ws.root / run.dir / "strategy.py").write_bytes(REVERTING)
    written = loop.card(ws, store, registered, "the trough rule")

    read = records.cards_of(store, registered)[-1]
    assert read.model_dump(exclude={"created_at"}) == written.model_dump(exclude={"created_at"})
    assert read.venue_model.venue == "XNAS"
    assert read.aligned is True
    assert read.crash_tail is None


def test_only_a_card_that_ran_and_traded_is_a_trial(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """`n_trials` counts cards; `trial_metrics` counts the candidates selection could keep.

    The baseline trades, so the card that trades nothing holds a book of its own and is a
    discard rather than a repeat of the baseline's."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    run = loop.begin(ws, store, hyp_id)
    strategy = ws.root / run.dir / "strategy.py"
    for source, desc in ((FLAT, "trades nothing"), (RAISING, "crashes")):
        strategy.write_bytes(source)
        loop.card(ws, store, hyp_id, desc)

    cards = records.cards_of(store, hyp_id)
    assert [(card.status, card.n_trades) for card in cards[-3:]] == [
        ("keep", cards[-3].n_trades),
        ("discard", 0),
        ("crash", 0),
    ]
    assert cards[-3].n_trades > 0
    assert records.n_trials(store, registered) == len(cards)
    assert records.trial_metrics(store, hyp_id) == [cards[-3].metric]


def a_run(held: tuple[Held, ...], days: int = 3) -> CardRun:
    """A run over `days` sessions whose holdings are given, everything else empty."""
    ends = tuple(midnight_ns(date(2024, 1, 1 + i)) + 16 * 3_600 * 10**9 for i in range(days))
    return CardRun(
        window=(date(2024, 1, 1), date(2024, 1, days)),
        period="1d",
        period_ends_ns=ends,
        returns=(0.0,) * days,
        equity=(1.0,) * days,
        trades=(),
        fills=(),
        capital=1.0,
        currency="USD",
        venue_model={},
        held=held,
    )


def test_a_signature_is_the_signed_book_at_each_period_end_keyed_by_its_day() -> None:
    """Size and price are parameters; the instrument and the side are the bet."""
    end = midnight_ns(date(2024, 1, 2)) + 16 * 3_600 * 10**9
    run = a_run(
        (
            Held(end, "B.X", -2.0, 20.0),
            Held(end, "A.X", 50.0, 500.0),
            Held(end + 86_400 * 10**9, "A.X", 1.0, 10.0),
        )
    )

    assert records.signature(run) == {
        "2024-01-01": [],
        "2024-01-02": [["A.X", 1], ["B.X", -1]],
        "2024-01-03": [["A.X", 1]],
    }


def test_a_signature_matches_on_the_share_of_shared_days_and_names_the_closest(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    stored_a = {"2024-01-01": [["A.X", 1]], "2024-01-02": [["A.X", 1]], "2024-01-03": []}
    stored_b = {"2024-01-01": [["A.X", 1]], "2024-01-02": [["A.X", 1]], "2024-01-03": [["A.X", 1]]}
    sha_a, sha_b = store.put_blob(b"a"), store.put_blob(b"b")
    records.record_signature(store, run, sha_a, stored_a)
    records.record_signature(store, run, sha_b, stored_b)
    candidate = {"2024-01-01": [["A.X", 1]], "2024-01-02": [["A.X", 1]], "2024-01-03": [["A.X", 1]]}

    like = records.redundant_with(store, run, candidate, 97)
    assert like is not None
    assert (like.like, like.matched, like.shared, like.pct) == (sha_b, 3, 3, 100.0)
    loose = records.redundant_with(store, run, {"2024-01-01": [["A.X", 1]], "2024-01-03": []}, 60)
    assert loose is not None and loose.like == sha_a, "two of two shared days"
    assert records.redundant_with(store, run, candidate, 100) is not None
    unseen = {"2024-01-02": [["B.X", -1]]}
    assert records.redundant_with(store, run, unseen, 97) is None, "shared, never matched"
    assert records.redundant_with(store, run, {"2025-01-01": []}, 1) is None, "no shared day"
    other = run.model_copy(update={"snapshot_id": "another"})
    assert records.redundant_with(store, other, candidate, 1) is None, "pins bound the memory"


def test_the_same_bytes_are_signed_once_under_one_set_of_pins(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    sha = store.put_blob(b"a")
    records.record_signature(store, run, sha, {"2024-01-01": []})
    records.record_signature(store, run, sha, {"2024-01-01": [["A.X", 1]]})

    rows = store.connection.execute(
        "SELECT signature, sessions FROM signatures WHERE strategy_sha = ?", (sha,)
    ).fetchall()
    assert [(row["signature"], row["sessions"]) for row in rows] == [
        ('{"2024-01-01": [["A.X", 1]]}', 1)
    ]


def test_a_card_keeps_the_tags_its_proposer_gave_it(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The tags are the key of the coverage table, so they must come back as written."""
    run = loop.begin(ws, store, registered)
    (ws.root / run.dir / "strategy.py").write_bytes(REVERTING)
    written = loop.card(ws, store, registered, "the trough rule", tags=["signal_mean_reversion"])

    baseline, read = records.cards_of(store, registered)
    assert written.tags == ["signal_mean_reversion"]
    assert read.tags == ["signal_mean_reversion"]
    assert baseline.tags == [], "no proposer described the baseline"


def test_a_crashed_card_keeps_its_tail(ws: Workspace, store: StateStore, registered: str) -> None:
    run = loop.begin(ws, store, registered)
    (ws.root / run.dir / "strategy.py").write_bytes(RAISING)
    loop.card(ws, store, registered, "boom")

    read = records.cards_of(store, registered)[-1]
    assert read.status == "crash"
    assert read.crash_tail is not None


def test_the_tag_counts_the_runs_of_that_day(store: StateStore) -> None:
    from datetime import date

    assert records.next_tag(store, "demo_mr", date(2024, 5, 6)) == "20240506-1"
