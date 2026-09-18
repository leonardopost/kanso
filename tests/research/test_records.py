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

UNDER = "0123456789ab"
"""A stand-in for `loop.Setup.measured_under`: what a stored number was measured under.

Its value is a digest of the capital, the folds, the return period, the venue model and
the host version, and nothing here depends on how it is struck — only on two rows
carrying the same one or two different ones. `tests/research/test_loop.py` is where the
digest itself is measured against a card.
"""


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


def test_a_holding_on_a_day_the_run_measured_no_period_end_in_widens_the_signature() -> None:
    """The runner samples `held` at the period ends and nowhere else, so this cannot
    happen today — and the line that widens the days for it is defence, which has to
    defend. Reading the second half of the day from keys the first half had not been
    given made it raise `KeyError` on the one case it was written for."""
    stray = midnight_ns(date(2024, 1, 9)) + 1 * 3_600 * 10**9
    run = a_run(held=(Held(stray, "A.X", 1.0, 10.0),))

    assert records.signature(run)["2024-01-09"] == [["A.X", 1, True]]


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
    records.record_signature(store, run, sha_a, stored_a, 1.0, UNDER)
    records.record_signature(store, run, sha_b, stored_b, 1.0, UNDER)
    candidate = {"2024-01-01": long, "2024-01-02": long, "2024-01-03": long}

    like = records.matched_book(store, run, candidate, 97, 1.0, 0.0, UNDER)
    assert like is not None and like.agreed
    assert (like.like, like.matched, like.shared, like.pct) == (sha_b, 3, 3, 100.0)
    assert like.earned == 1.0, "the number the book it repeats earned, which the card cannot say"
    loose = records.matched_book(
        store, run, {"2024-01-01": long, "2024-01-03": []}, 60, 1.0, 0.0, UNDER
    )
    assert loose is not None and loose.like == sha_a, "two of two shared days"
    assert records.matched_book(store, run, candidate, 100, 1.0, 0.0, UNDER) is not None
    unseen = {"2024-01-02": [["B.X", -1, True]]}
    assert records.matched_book(store, run, unseen, 97, 1.0, 0.0, UNDER) is None, (
        "shared, never matched"
    )
    assert records.matched_book(store, run, {"2025-01-01": []}, 1, 1.0, 0.0, UNDER) is None, (
        "no shared"
    )
    other = run.model_copy(update={"snapshot_id": "another"})
    assert records.matched_book(store, other, candidate, 1, 1.0, 0.0, UNDER) is None, (
        "pins bound it"
    )


def test_a_book_matched_on_every_day_is_no_anchor_when_the_number_it_earned_differs(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The second half of the matcher's premise, checked rather than asserted.

    The floor is the hypothesis's own: a difference at or under it is one result measured
    twice, and a difference over it is two results, whatever the books say. Measured on the
    live workspace of 2026-09-18, the half that had never been checked was false on 106 of
    3,092 refusals — 74 of them on one intraday hypothesis whose objective is a per-trade
    edge, where one anchor's 289 matches scored -18.780 .. 9.179 around its own -4.5452.
    """
    run = loop.begin(ws, store, registered)
    long = [["A.X", 1, True]]
    held = {"2024-01-01": long, "2024-01-02": long}
    sha = store.put_blob(b"a")
    records.record_signature(store, run, sha, held, 1.0, UNDER)

    at_the_floor = records.matched_book(store, run, held, 100, 1.25, 0.25, UNDER)
    assert at_the_floor is not None and at_the_floor.agreed, "at the floor"
    below = records.matched_book(store, run, held, 100, 0.75, 0.25, UNDER)
    assert below is not None and below.agreed, "and below it"
    over = records.matched_book(store, run, held, 100, 1.26, 0.25, UNDER)
    assert over is not None and not over.agreed, (
        "over it: the book still matched, the number did not"
    )
    under = records.matched_book(store, run, held, 100, 0.74, 0.25, UNDER)
    assert under is not None and not under.agreed, "either way"
    assert (over.like, over.pct, over.earned) == (sha, 100.0, 1.0), (
        "and the match is on record in full, because the card has no column for it"
    )


def test_a_match_whose_number_agreed_is_preferred_to_a_closer_one_that_did_not(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """Reporting the second kind can never turn a repeat into a discard.

    A candidate matching one stored book on every day whose number moved, and another on
    three of four whose number did not, is the repeat it always was: the agreeing match
    is preferred over the closer one, and only when there is none at all does the closest
    book stand on its own.
    """
    run = loop.begin(ws, store, registered)
    long = [["A.X", 1, True]]
    every = {f"2024-01-0{day}": long for day in range(1, 5)}
    three_of_four = {**every, "2024-01-04": []}
    exact, near = store.put_blob(b"exact"), store.put_blob(b"near")
    records.record_signature(store, run, exact, every, 9.0, UNDER)
    records.record_signature(store, run, near, three_of_four, 1.0, UNDER)

    found = records.matched_book(store, run, every, 70, 1.0, 0.25, UNDER)
    assert found is not None and found.agreed
    assert (found.like, found.pct) == (near, 75.0), "the agreeing match, though it matched less"

    alone = records.matched_book(store, run, every, 90, 1.0, 0.25, UNDER)
    assert alone is not None and not alone.agreed
    assert (alone.like, alone.pct, alone.earned) == (exact, 100.0, 9.0), (
        "and with no agreeing match under the share, the book that did match is still named"
    )


def test_a_book_outside_the_floor_is_not_parsed_once_an_agreeing_one_has_matched(
    ws: Workspace, store: StateStore, registered: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ranking prefers an agreeing match, so a disagreeing row cannot win it back.

    `json.loads` of a stored book is what the matcher costs — 0.5 s to 1.0 s a card over
    the live workspace's largest pin groups — so a row that cannot change the answer is
    not parsed. Counting the parses is the only way to state that, since the answer is the
    same either way, which is the point.
    """
    run = loop.begin(ws, store, registered)
    long = [["A.X", 1, True]]
    held = {"2024-01-01": long, "2024-01-02": long}
    agreeing, disagreeing = store.put_blob(b"agreeing"), store.put_blob(b"disagreeing")
    records.record_signature(store, run, agreeing, held, 1.0, UNDER)
    records.record_signature(store, run, disagreeing, held, 9.0, UNDER)

    parsed: list[str] = []
    loads = records.json.loads
    monkeypatch.setattr(records.json, "loads", lambda text: parsed.append(text) or loads(text))

    found = records.matched_book(store, run, held, 100, 1.0, 0.25, UNDER)
    assert found is not None and (found.like, found.agreed) == (agreeing, True)
    assert len(parsed) == 1, "the book whose number disagreed was never read"

    parsed.clear()
    moved = records.matched_book(store, run, held, 100, 9.0, 0.25, UNDER)
    assert moved is not None and (moved.like, moved.agreed) == (disagreeing, True)
    assert len(parsed) == 1, "and it is the other way round when the other number agrees"

    parsed.clear()
    assert records.matched_book(store, run, held, 100, 5.0, 0.25, UNDER) is not None
    assert len(parsed) == 2, "with no agreeing book at all, both are read and one is named"


def test_a_stored_book_with_no_number_on_record_is_no_anchor_at_all(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """A premise that cannot be tested has not been met, so such a row refuses nothing.

    Only a row written before `signatures.metric` existed can be one, and 0004 empties the
    table in the version that adds the column, so no released workspace holds one. The
    reading is asserted because it is the answer to a NULL the schema admits.
    """
    run = loop.begin(ws, store, registered)
    held = {"2024-01-01": [["A.X", 1, True]]}
    sha = store.put_blob(b"a")
    records.record_signature(store, run, sha, held, 1.0, UNDER)
    store.connection.execute("UPDATE signatures SET metric = NULL WHERE strategy_sha = ?", (sha,))

    assert records.matched_book(store, run, held, 100, 1.0, 1e9, UNDER) is None


def test_two_intraday_books_that_share_no_name_are_told_apart_by_the_matcher(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The defect was a pair, not a reading: two candidates of an intraday hypothesis,
    both flat at every period end, signed as two empty dicts and matched each other on
    every one of 834 shared days. `signature` and `matched_book` are each tested; this
    is the composition on the shape that produced the defect, with nothing held at an end.
    """
    opens = midnight_ns(date(2024, 1, 2)) + 10 * 3_600 * 10**9
    run = loop.begin(ws, store, registered)
    stored = records.signature(a_run(trades=(a_trade("A.X", 3.0, opens, opens + 3_600 * 10**9),)))
    records.record_signature(store, run, store.put_blob(b"a"), stored, 1.0, UNDER)
    other = records.signature(a_run(trades=(a_trade("B.X", -3.0, opens, opens + 3_600 * 10**9),)))

    assert stored["2024-01-02"] == [["A.X", 1, False]], "held, and gone by the end"
    assert records.matched_book(store, run, other, 97, 1.0, 0.0, UNDER) is None, (
        "another name, another bet"
    )
    assert records.matched_book(store, run, stored, 97, 1.0, 0.0, UNDER) is not None, (
        "the same one is not"
    )


def test_a_number_measured_under_another_reading_is_no_anchor(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The four pins fix the question and the data, and fix nothing about the arithmetic.

    `capital`, `folds`, `return_period` and the venue model are workspace settings an
    operator may edit between two cards, and the host version an attached construct is
    differenced against is a per-run pin the four do not include. Each moves a number with
    every pin standing still, so a row measured under one reading answers nothing about a
    card measured under another -- it is not a closer or a poorer anchor, it is none.
    """
    run = loop.begin(ws, store, registered)
    held = {"2024-01-01": [["A.X", 1, True]]}
    sha = store.put_blob(b"a")
    records.record_signature(store, run, sha, held, 1.0, UNDER)

    assert records.matched_book(store, run, held, 100, 1.0, 0.0, UNDER) is not None
    assert records.matched_book(store, run, held, 100, 1.0, 0.0, "another read") is None
    store.connection.execute("UPDATE signatures SET measured_under = NULL")
    assert records.matched_book(store, run, held, 100, 1.0, 0.0, UNDER) is None, (
        "and a row written before the column carries no reading at all, so it is no anchor"
    )


def test_the_same_bytes_are_signed_once_under_one_set_of_pins(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    sha = store.put_blob(b"a")
    records.record_signature(store, run, sha, {"2024-01-01": []}, 0.25, UNDER)
    records.record_signature(store, run, sha, {"2024-01-01": [["A.X", 1, True]]}, 0.75, UNDER)

    rows = store.connection.execute(
        "SELECT signature, sessions, metric FROM signatures WHERE strategy_sha = ?", (sha,)
    ).fetchall()
    assert [(row["signature"], row["sessions"], row["metric"]) for row in rows] == [
        ('{"2024-01-01": [["A.X", 1, true]]}', 1, 0.75)
    ], "the book and the number it earned are replaced together, being one measurement"


def test_the_same_bytes_under_two_readings_are_two_anchors(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """A second reading is a second row, because the first is the only anchor its runs have.

    The selection reads a row under the reading it was measured with, so a write that
    replaced the older reading would leave every run under it with nothing to be refused
    against — an operator who sets `[research] folds = 3`, runs cards and sets it back to
    4 would have bought that with one line of `kanso.toml` and undone nothing by undoing
    it. So the reading is part of the key and the two rows stand together
    (`state/migrations/0008_signature_reading_is_a_key.sql`).
    """
    run = loop.begin(ws, store, registered)
    held = {"2024-01-01": [["A.X", 1, True]]}
    sha = store.put_blob(b"a")
    records.record_signature(store, run, sha, held, 1.0, UNDER)
    records.record_signature(store, run, sha, held, 9.0, "three folds")

    four = records.matched_book(store, run, held, 100, 1.0, 0.25, UNDER)
    assert four is not None and (four.earned, four.agreed) == (1.0, True), (
        "the first reading's number is where it was left"
    )
    three = records.matched_book(store, run, held, 100, 9.0, 0.25, "three folds")
    assert three is not None and (three.earned, three.agreed) == (9.0, True), "and so is the other"
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM signatures WHERE strategy_sha = ?", (sha,)
        ).fetchone()[0]
        == 2
    )


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
