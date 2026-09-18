"""Runs and cards as rows: what the store enforces, and what it hands back."""

from __future__ import annotations

from datetime import date

import pytest

from kanso.criteria.run import CardRun, Held, Trade, midnight_ns
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


def test_only_a_card_that_ran_and_traded_is_a_trial(ws: Workspace, store: StateStore) -> None:
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
    assert records.n_trials(store, hyp_id) == len(cards)
    assert records.trial_metrics(store, hyp_id) == [cards[-3].metric]


def a_run(held: tuple[Held, ...] = (), trades: tuple[Trade, ...] = (), days: int = 3) -> CardRun:
    """A run over `days` sessions whose holdings and trades are given, the rest empty."""
    ends = tuple(midnight_ns(date(2024, 1, 1 + i)) + 16 * 3_600 * 10**9 for i in range(days))
    return CardRun(
        window=(date(2024, 1, 1), date(2024, 1, days)),
        period="1d",
        period_ends_ns=ends,
        returns=(0.0,) * days,
        equity=(1.0,) * days,
        trades=trades,
        fills=(),
        capital=1.0,
        currency="USD",
        venue_model={},
        held=held,
    )


def a_trade(instrument: str, qty: float, opened: int, closed: int) -> Trade:
    """One closed position, with nothing the signature does not read left meaningful."""
    return Trade(
        opened_ns=opened,
        closed_ns=closed,
        instrument_id=instrument,
        qty=qty,
        avg_open=10.0,
        avg_close=10.0,
        pnl_net=0.0,
        cost=0.0,
        fills=(),
    )


def test_a_signature_marks_what_was_held_at_each_period_end_of_the_day() -> None:
    """Size and price are parameters; the instrument, the side and whether the position
    was still on at the end of the day are the bet."""
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
        "2024-01-02": [["A.X", 1, True], ["B.X", -1, True]],
        "2024-01-03": [["A.X", 1, True]],
    }


def test_a_position_opened_and_closed_inside_one_day_is_in_that_day_s_signature() -> None:
    """The defect this reading exists for: a strategy flat at every period end held
    something on 834 days and signed as flat on all of them, so every candidate of an
    intraday hypothesis matched every other and none was ever an experiment."""
    opens = midnight_ns(date(2024, 1, 2)) + 10 * 3_600 * 10**9
    run = a_run(trades=(a_trade("A.X", 3.0, opens, opens + 3_600 * 10**9),))

    assert records.signature(run) == {
        "2024-01-01": [],
        "2024-01-02": [["A.X", 1, False]],
        "2024-01-03": [],
    }


def test_carrying_a_position_over_the_close_is_not_the_same_book_as_closing_before_it() -> None:
    """The reading is strictly finer than sampling the ends alone, never coarser: a day
    marked only from a trade span says the position was gone by the end, and a run that
    held it at the end does not match it. Merged into one mark, the demo's card scoring
    9.99 and its card scoring 3.15 read as the same book on every session."""
    opens = midnight_ns(date(2024, 1, 2)) + 10 * 3_600 * 10**9
    end = midnight_ns(date(2024, 1, 2)) + 16 * 3_600 * 10**9
    intraday = a_run(trades=(a_trade("A.X", 1.0, opens, opens + 3_600 * 10**9),))
    overnight = a_run(
        held=(Held(end, "A.X", 1.0, 10.0),),
        trades=(a_trade("A.X", 1.0, opens, opens + 86_400 * 10**9),),
    )

    assert records.signature(intraday)["2024-01-02"] == [["A.X", 1, False]]
    assert records.signature(overnight)["2024-01-02"] == [["A.X", 1, True]]


def test_a_position_signs_every_day_it_spanned_and_a_day_the_run_never_measured_signs_none() -> (
    None
):
    """A hold is on the days it was open, whichever end it straddled; a day outside the
    run's period ends — a weekend a position was carried over — is not a shared session
    and never becomes one."""
    opened = midnight_ns(date(2024, 1, 1)) + 15 * 3_600 * 10**9
    closed = midnight_ns(date(2024, 1, 6)) + 10 * 3_600 * 10**9
    run = a_run(trades=(a_trade("A.X", -1.0, opened, closed),), days=3)

    assert records.signature(run) == {
        "2024-01-01": [["A.X", -1, False]],
        "2024-01-02": [["A.X", -1, False]],
        "2024-01-03": [["A.X", -1, False]],
    }


def test_a_day_the_book_flipped_carries_both_sides_once() -> None:
    """Long then short is neither long nor short, and holding it twice is holding it once."""
    day = midnight_ns(date(2024, 1, 2))
    run = a_run(
        trades=(
            a_trade("A.X", 2.0, day + 10 * 3_600 * 10**9, day + 11 * 3_600 * 10**9),
            a_trade("A.X", 5.0, day + 12 * 3_600 * 10**9, day + 13 * 3_600 * 10**9),
            a_trade("A.X", -1.0, day + 14 * 3_600 * 10**9, day + 15 * 3_600 * 10**9),
        )
    )

    assert records.signature(run)["2024-01-02"] == [["A.X", -1, False], ["A.X", 1, False]]


def test_a_position_held_at_the_end_is_marked_once_however_often_it_was_traded() -> None:
    """A day that already says the instrument and side were on at its end learns nothing
    from a trade of the same instrument and side inside it."""
    day = midnight_ns(date(2024, 1, 2))
    end = day + 16 * 3_600 * 10**9
    run = a_run(
        held=(Held(end, "A.X", 4.0, 40.0),),
        trades=(a_trade("A.X", 1.0, day + 10 * 3_600 * 10**9, day + 11 * 3_600 * 10**9),),
    )

    assert records.signature(run)["2024-01-02"] == [["A.X", 1, True]]


def test_a_signature_matches_on_the_share_of_shared_days_and_names_the_closest(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    long = [["A.X", 1, True]]
    stored_a = {"2024-01-01": long, "2024-01-02": long, "2024-01-03": []}
    stored_b = {"2024-01-01": long, "2024-01-02": long, "2024-01-03": long}
    sha_a, sha_b = store.put_blob(b"a"), store.put_blob(b"b")
    records.record_signature(store, run, sha_a, stored_a)
    records.record_signature(store, run, sha_b, stored_b)
    candidate = {"2024-01-01": long, "2024-01-02": long, "2024-01-03": long}

    like = records.redundant_with(store, run, candidate, 97)
    assert like is not None
    assert (like.like, like.matched, like.shared, like.pct) == (sha_b, 3, 3, 100.0)
    loose = records.redundant_with(store, run, {"2024-01-01": long, "2024-01-03": []}, 60)
    assert loose is not None and loose.like == sha_a, "two of two shared days"
    assert records.redundant_with(store, run, candidate, 100) is not None
    unseen = {"2024-01-02": [["B.X", -1, True]]}
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
    records.record_signature(store, run, sha, {"2024-01-01": [["A.X", 1, True]]})

    rows = store.connection.execute(
        "SELECT signature, sessions FROM signatures WHERE strategy_sha = ?", (sha,)
    ).fetchall()
    assert [(row["signature"], row["sessions"]) for row in rows] == [
        ('{"2024-01-01": [["A.X", 1, true]]}', 1)
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
