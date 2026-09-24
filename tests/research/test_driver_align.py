"""Alignment: the cheap half first, the model second, and what a drift costs a run.

Drift is the one failure the loop cannot measure, because a strategy that stopped testing
the idea can score beautifully. So it is checked on a clock and answered by rewinding: the
run goes back to ground a check already passed, the cards in between are marked, and
research carries on rather than stopping for a judgement call.

A clock that finds nothing new is not a question. A lane on the run's base, or on the bytes
the last check left it on, holds nothing the loop proposed since anything answered for it,
so no model is asked about it and nothing is escalated, however a model would have answered.
"""

from __future__ import annotations

import pytest

from kanso import inbox, research
from kanso.errors import ValidationError
from kanso.models import spend
from kanso.research import align, lanes, records, scheduler
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, classify
from .mocked import ALIGNED, DRIFTED, SEED, fresh_cursors, scripted  # noqa: F401

FOREIGN = SEED.replace(b"# end", b'Strategy.symbol = "OTHER.XNAS"\n\n# end')
"""A file that names an instrument the universe does not hold: drift the tree can prove."""

REVERTING = SEED.replace(b"# end", b'Strategy.mode = "revert"\n\n# end')

WEAK = SEED.replace(b'mode = "flat"', b'mode = "weak"')
"""A file that discards against `REVERTING`, so the lane is restored to it."""

CRASHING = SEED.replace(b"# end", b'Strategy.mode = "boom"\n\n# end')
"""A file that crashes on its first bar, so the lane is restored to whatever it stood on."""

EDITED = REVERTING.replace(b"5_000.0", b"2_500.0")
"""`REVERTING` at half the notional: a file in the lane that no check has seen."""


def started(ws: Workspace, store: StateStore) -> str:
    """A classified hypothesis with an open run whose baseline kept."""
    hyp_id = classify(ws, store, DOCUMENT, SEED)
    research.begin(ws, store, hyp_id)
    return hyp_id


def lane_file(ws: Workspace, hyp_id: str) -> bytes:
    return (lanes.lane_dir(ws, "op", hyp_id) / "strategy.py").read_bytes()


def workspace_file(ws: Workspace, hyp_id: str) -> bytes:
    return ws.path("hypotheses", hyp_id, "strategy.py").read_bytes()


def write_lane(ws: Workspace, hyp_id: str, source: bytes) -> None:
    lanes.write_atomic(lanes.lane_dir(ws, "op", hyp_id) / "strategy.py", source)


def aligned_flags(store: StateStore, hyp_id: str) -> list[int]:
    rows = store.connection.execute(
        "SELECT aligned FROM cards WHERE hyp_id = ? ORDER BY seq", (hyp_id,)
    ).fetchall()
    return [int(row[0]) for row in rows]


def test_the_deterministic_checks_run_first_and_no_model_is_asked(
    ws: Workspace, store: StateStore
) -> None:
    """The cheap tier is scripted with nothing, so a call would fail the whole ladder."""
    scripted(ws, align_check=None)
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, FOREIGN)

    ok, reason = align.check(ws, store, hyp_id)

    assert ok is False
    assert reason is not None and "not in the universe" in reason
    assert spend(store, lane="op").calls == 0


def test_a_source_that_still_tests_the_idea_marks_its_cards_aligned(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[ALIGNED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"

    assert align.check(ws, store, hyp_id) == (True, None)
    assert spend(store, lane="op").calls == 1
    assert aligned_flags(store, hyp_id) == [1, 1]

    run = records.require_active(store, hyp_id)
    assert align.checkpoint(store, run) == align.Checkpoint(2, kept.strategy_sha, judged=True)
    assert align.since(store, run) == 0
    assert [e.kind for e in store.events(subject=hyp_id)].count(align.ALIGNED) == 1


def test_a_lane_restored_to_the_run_s_base_is_not_judged_and_raises_nothing(
    ws: Workspace, store: StateStore
) -> None:
    """Every proposal since the last check failed to keep, so the lane holds the bytes the
    run was handed. A model asked about them judges a seed nobody proposed against a thesis
    that describes what the search is for, and a drift verdict "rewinds" the run onto the
    bytes it is already on — the shape of all ten `misaligned` escalations a live workspace
    raised in under two hours, every one rewound to its own run's base.

    The model here would say drift, so a check that asked it would fail this test twice
    over: by its verdict, and by the call.
    """
    scripted(ws, align_check=[DRIFTED])
    hyp_id = started(ws, store)
    run = records.require_active(store, hyp_id)
    best = records.best_of(store, hyp_id)
    write_lane(ws, hyp_id, CRASHING)
    assert research.card(ws, store, hyp_id, "ask the impossible").status == "crash"
    assert lane_file(ws, hyp_id) == SEED

    assert align.check(ws, store, hyp_id) == (True, None)

    assert spend(store, lane="op").calls == 0
    assert inbox.unread(store) == []
    assert aligned_flags(store, hyp_id) == [1, 1]
    assert align.checkpoint(store, run) == align.Checkpoint(2, run.base_sha, judged=False)
    assert align.since(store, run) == 0
    kinds = [e.kind for e in store.events(subject=hyp_id)]
    assert (kinds.count(align.ALIGNED), kinds.count(align.DRIFTED)) == (1, 0)
    assert lane_file(ws, hyp_id) == SEED
    assert records.best_of(store, hyp_id) == best


def test_the_base_is_not_judged_after_a_check_has_moved_on_from_it(
    ws: Workspace, store: StateStore
) -> None:
    """The base is never judged, not only while it is the last check's ground: bytes an
    operator writes back into the lane by hand are the run's base all the same."""
    scripted(ws, align_check=[ALIGNED, DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    assert research.card(ws, store, hyp_id, "trade the trough").status == "keep"
    assert align.check(ws, store, hyp_id) == (True, None)
    write_lane(ws, hyp_id, SEED)

    assert align.check(ws, store, hyp_id) == (True, None)

    assert spend(store, lane="op").calls == 1, "only the keep was put to the model"
    assert inbox.unread(store) == []
    run = records.require_active(store, hyp_id)
    assert align.checkpoint(store, run) == align.Checkpoint(cards=2, sha=run.base_sha)
    assert lane_file(ws, hyp_id) == SEED


def test_a_check_that_judges_nothing_never_counts_a_keep_it_did_not_judge(
    ws: Workspace, store: StateStore
) -> None:
    """A keep an operator carded and then took back out of the lane is still a proposal no
    check has seen. The check that finds the base back in the lane judges nothing, so it
    leaves that keep ahead of the checkpoint rather than behind it, and the check that next
    finds it in the lane judges it — instead of a later drift "rewinding" onto the keep."""
    scripted(ws, align_check=[DRIFTED])
    hyp_id = started(ws, store)
    run = records.require_active(store, hyp_id)
    write_lane(ws, hyp_id, REVERTING)
    assert research.card(ws, store, hyp_id, "trade the trough").status == "keep"
    write_lane(ws, hyp_id, SEED)

    assert align.check(ws, store, hyp_id) == (True, None)
    assert align.checkpoint(store, run) == align.Checkpoint(cards=1, sha=run.base_sha)
    assert spend(store, lane="op").calls == 0

    write_lane(ws, hyp_id, WEAK)
    assert research.card(ws, store, hyp_id, "react to every step").status == "discard"
    assert lane_file(ws, hyp_id) == REVERTING

    assert align.check(ws, store, hyp_id) == (False, DRIFTED["reason"])

    assert spend(store, lane="op").calls == 1
    assert aligned_flags(store, hyp_id) == [1, 0, 0]
    assert lane_file(ws, hyp_id) == SEED
    assert records.best_of(store, hyp_id)[0] == run.base_sha
    assert [entry.kind for entry in inbox.unread(store)] == ["misaligned"]


def test_a_drift_never_rewinds_onto_the_bytes_it_found_drifted(
    ws: Workspace, store: StateStore
) -> None:
    """A check judges the lane and hands its verdict to every card since the last one, so a
    lane rewritten by hand can pass a keep it never held. Once the keep's own bytes are found
    drifted, it is no ground to rewind to, whatever an earlier verdict said of it."""
    scripted(ws, align_check=[ALIGNED, DRIFTED])
    hyp_id = started(ws, store)
    run = records.require_active(store, hyp_id)
    write_lane(ws, hyp_id, REVERTING)
    assert research.card(ws, store, hyp_id, "trade the trough").status == "keep"
    write_lane(ws, hyp_id, EDITED)
    assert align.check(ws, store, hyp_id) == (True, None)
    write_lane(ws, hyp_id, WEAK)
    assert research.card(ws, store, hyp_id, "react to every step").status == "discard"
    assert lane_file(ws, hyp_id) == REVERTING

    assert align.check(ws, store, hyp_id) == (False, DRIFTED["reason"])

    assert lane_file(ws, hyp_id) == SEED
    assert aligned_flags(store, hyp_id) == [1, 0, 0]
    assert records.best_of(store, hyp_id)[0] == run.base_sha


def test_a_file_a_check_passed_is_stored_for_the_next_check_to_difference_from(
    ws: Workspace, store: StateStore
) -> None:
    """A check can pass bytes no card holds — an edit made by hand and checked before it is
    carded — and the next check differences from them, which it cannot from a hash alone."""
    scripted(ws, align_check=[ALIGNED, ALIGNED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, EDITED)
    assert align.check(ws, store, hyp_id) == (True, None)
    write_lane(ws, hyp_id, REVERTING)

    assert align.check(ws, store, hyp_id) == (True, None)

    assert spend(store, lane="op").calls == 2
    assert store.get_blob(align.checkpoint(store, records.require_active(store, hyp_id)).sha)


def test_a_lane_back_on_the_file_a_check_passed_is_not_asked_again(
    ws: Workspace, store: StateStore
) -> None:
    """A keep moves the lane, and every card after it that does not keep restores the lane
    to that keep, so the loop is back on a file a check passed only when nothing has kept
    since. Its answer is on record, and asking again could only buy a verdict that rewinds
    the lane onto itself."""
    scripted(ws, align_check=[ALIGNED, DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"
    assert align.check(ws, store, hyp_id) == (True, None)
    write_lane(ws, hyp_id, WEAK)
    assert research.card(ws, store, hyp_id, "react to every step").status == "discard"
    assert lane_file(ws, hyp_id) == REVERTING

    assert align.check(ws, store, hyp_id) == (True, None)

    assert spend(store, lane="op").calls == 1
    assert inbox.unread(store) == []
    assert aligned_flags(store, hyp_id) == [1, 1, 1]
    run = records.require_active(store, hyp_id)
    assert align.checkpoint(store, run) == align.Checkpoint(cards=3, sha=kept.strategy_sha)
    assert records.best_of(store, hyp_id) == (kept.strategy_sha, kept.metric)


def test_a_proposal_that_kept_is_put_to_the_model_and_its_drift_rewound(
    ws: Workspace, store: StateStore
) -> None:
    """Bytes the loop proposed since the last check are what the model is for, and a drift
    in them costs what it always did."""
    scripted(ws, align_check=[DRIFTED])
    hyp_id = started(ws, store)
    baseline_sha, baseline_metric = records.best_of(store, hyp_id)
    write_lane(ws, hyp_id, REVERTING)
    assert research.card(ws, store, hyp_id, "trade the trough").status == "keep"

    assert align.check(ws, store, hyp_id) == (False, DRIFTED["reason"])

    assert spend(store, lane="op").calls == 1
    assert lane_file(ws, hyp_id) == SEED
    assert workspace_file(ws, hyp_id) == SEED
    assert records.best_of(store, hyp_id) == (baseline_sha, baseline_metric)
    assert aligned_flags(store, hyp_id) == [1, 0]
    assert [entry.kind for entry in inbox.unread(store)] == ["misaligned"]
    run = records.require_active(store, hyp_id)
    assert align.checkpoint(store, run) == align.Checkpoint(2, run.base_sha, judged=True)


def test_the_syntax_tree_still_reads_the_base(ws: Workspace, store: StateStore) -> None:
    """What the tree finds is a fact about the bytes, whoever wrote them, so a base that
    names an instrument outside the universe is stated as drift — and the model, which
    would have passed it, is never asked."""
    scripted(ws, align_check=[ALIGNED])
    hyp_id = classify(ws, store, DOCUMENT, FOREIGN)
    research.begin(ws, store, hyp_id)

    ok, reason = align.check(ws, store, hyp_id)

    assert ok is False
    assert reason is not None and "'OTHER.XNAS' is not in the universe" in reason
    assert spend(store, lane="op").calls == 0
    assert lane_file(ws, hyp_id) == FOREIGN, "there is nothing behind a base to rewind to"
    assert [entry.kind for entry in inbox.unread(store)] == ["misaligned"]


def test_drift_before_any_proposal_kept_rewinds_to_the_baseline_and_keeps_its_bar(
    ws: Workspace, store: StateStore
) -> None:
    """A run's first check may drift, and the run still has its own starting point.

    No model proposed the baseline card, so no check judges it. That is what leaves a
    rewind something to fall back on: without it `_last_aligned_keep` finds nothing, the
    best is cleared, and the next card that passes its constraints keeps at any metric —
    which is how a hypothesis's best fell from 5.3976 to 5.0256 twenty-three seconds
    after a drift.
    """
    scripted(ws, align_check=[DRIFTED])
    hyp_id = started(ws, store)
    baseline_sha, baseline_metric = records.best_of(store, hyp_id)
    write_lane(ws, hyp_id, REVERTING)

    ok, reason = align.check(ws, store, hyp_id)

    assert (ok, reason) == (False, DRIFTED["reason"])
    assert lane_file(ws, hyp_id) == SEED
    assert workspace_file(ws, hyp_id) == SEED
    assert records.best_of(store, hyp_id) == (baseline_sha, baseline_metric)
    assert records.require_active(store, hyp_id).best_sha == baseline_sha
    assert aligned_flags(store, hyp_id) == [1], "the baseline is not a proposal to judge"


def test_drift_rewinds_to_the_last_keep_a_check_had_already_passed(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[ALIGNED, DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"
    assert align.check(ws, store, hyp_id) == (True, None)

    write_lane(ws, hyp_id, WEAK)
    dropped = research.card(ws, store, hyp_id, "react to every step")
    assert dropped.status == "discard"
    write_lane(ws, hyp_id, EDITED)

    ok, _ = align.check(ws, store, hyp_id)

    assert ok is False
    assert lane_file(ws, hyp_id) == REVERTING
    assert workspace_file(ws, hyp_id) == REVERTING
    assert records.best_of(store, hyp_id) == (kept.strategy_sha, kept.metric)
    assert aligned_flags(store, hyp_id) == [1, 1, 0]


def test_a_lane_still_on_the_file_a_drift_rewound_it_to_is_not_asked_again(
    ws: Workspace, store: StateStore
) -> None:
    """A rewind lands on ground a check already passed, so the next check that finds the
    lane still there has nothing to ask: a drift it reported could only rewind the lane
    onto itself."""
    scripted(ws, align_check=[ALIGNED, DRIFTED, DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"
    assert align.check(ws, store, hyp_id) == (True, None)
    write_lane(ws, hyp_id, EDITED)
    assert align.check(ws, store, hyp_id) == (False, DRIFTED["reason"])
    assert lane_file(ws, hyp_id) == REVERTING

    assert align.check(ws, store, hyp_id) == (True, None)

    assert spend(store, lane="op").calls == 2
    assert [entry.kind for entry in inbox.unread(store)] == ["misaligned"]
    run = records.require_active(store, hyp_id)
    assert align.checkpoint(store, run) == align.Checkpoint(cards=2, sha=kept.strategy_sha)


def test_a_rewind_with_no_keep_of_its_own_leaves_another_run_s_best_standing(
    ws: Workspace, store: StateStore
) -> None:
    """Backlog row 61: the run's best is the run's to clear; the hypothesis's belongs to
    the run that earned it."""
    scripted(ws)
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"
    research.end(ws, store, hyp_id)
    resumed = research.begin(ws, store, hyp_id)
    # The resumed run's only keep is its baseline; mark it as a check would never, to
    # reach the branch a run whose baseline discarded reaches.
    store.connection.execute("UPDATE cards SET aligned = 0 WHERE run_id = ?", (resumed.run_id,))

    align._revert(ws, store, resumed, ws.root / resumed.dir)

    assert records.require_active(store, hyp_id).best_sha is None
    assert records.best_of(store, hyp_id) == (kept.strategy_sha, kept.metric)


def test_a_rewind_in_a_re_seeded_run_leaves_the_champion_file_another_run_earned(
    ws: Workspace, store: StateStore
) -> None:
    """The lane goes back to what the run stands on; the workspace `strategy.py` stays the
    hypothesis's best, whichever branch of the rewind is taken."""
    scripted(ws)
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"
    research.end(ws, store, hyp_id)
    scheduler.requeue(store, hyp_id, scheduler.STALL_PRIORITY, reseed_from=store.put_blob(SEED))
    reseeded = research.begin(ws, store, hyp_id)
    assert workspace_file(ws, hyp_id) == REVERTING

    align._revert(ws, store, reseeded, ws.root / reseeded.dir)  # to its aligned baseline

    assert lane_file(ws, hyp_id) == SEED
    assert workspace_file(ws, hyp_id) == REVERTING
    assert records.best_of(store, hyp_id) == (kept.strategy_sha, kept.metric)

    store.connection.execute("UPDATE cards SET aligned = 0 WHERE run_id = ?", (reseeded.run_id,))
    align._revert(ws, store, reseeded, ws.root / reseeded.dir)  # to the bytes it began with

    assert lane_file(ws, hyp_id) == SEED
    assert workspace_file(ws, hyp_id) == REVERTING
    assert records.best_of(store, hyp_id) == (kept.strategy_sha, kept.metric)


def test_a_drift_escalates_and_writes_one_inbox_line(ws: Workspace, store: StateStore) -> None:
    scripted(ws, align_check=[DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)

    align.check(ws, store, hyp_id)

    entries = inbox.unread(store)
    assert [entry.kind for entry in entries] == ["misaligned"]
    text = inbox.inbox_file(ws).read_text(encoding="utf-8")
    assert f"misaligned {hyp_id}" in text
    assert entries[0].escalation_id in text
    assert [e.kind for e in store.events(subject=hyp_id)].count(align.DRIFTED) == 1


def test_a_lane_that_lost_its_strategy_gets_it_back_instead_of_failing(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[ALIGNED])
    hyp_id = started(ws, store)
    (lanes.lane_dir(ws, "op", hyp_id) / "strategy.py").unlink()

    assert align.check(ws, store, hyp_id) == (True, None)
    assert lane_file(ws, hyp_id) == SEED


def test_a_model_that_reports_drift_with_no_reason_still_gives_one(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[{"aligned": False, "reason": "   "}])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)

    ok, reason = align.check(ws, store, hyp_id)

    assert ok is False
    assert reason == "the model reported drift without giving a reason"


def test_the_first_check_of_a_run_differences_from_the_bytes_it_began_with(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[ALIGNED])
    hyp_id = started(ws, store)
    run = records.require_active(store, hyp_id)

    assert align.checkpoint(store, run).sha == run.base_sha
    assert align.since(store, run) == 1

    write_lane(ws, hyp_id, REVERTING)
    research.card(ws, store, hyp_id, "trade the trough")

    assert align.since(store, run) == 2
    align.check(ws, store, hyp_id)
    assert align.since(store, run) == 0


# --- the inbox the escalation lands in ---------------------------------------


def test_an_escalation_kind_outside_the_five_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(ValidationError, match="not an escalation kind"):
        inbox.escalate(ws, store, "interesting", "demo_mr", "look at this")


def test_a_long_summary_is_shortened_rather_than_lost(ws: Workspace, store: StateStore) -> None:
    entry = inbox.escalate(ws, store, "misaligned", "demo_mr", "x" * 400, actions="a\nb")

    assert len(entry.summary) == inbox.SUMMARY_LIMIT
    assert entry.summary.endswith("…")
    assert entry.actions == "a b"
    assert entry.payload()["kind"] == "misaligned"


def test_acknowledging_marks_one_entry_read_and_is_idempotent(
    ws: Workspace, store: StateStore
) -> None:
    entry = inbox.escalate(ws, store, "misaligned", "demo_mr", "drifted")

    first = inbox.ack(store, entry.escalation_id)
    second = inbox.ack(store, entry.escalation_id)

    assert first.acked_at is not None
    assert second.acked_at == first.acked_at
    assert inbox.unread(store) == []
    assert first.line().startswith("- [x] ")


def test_acknowledging_something_that_is_not_an_entry_is_refused(store: StateStore) -> None:
    with pytest.raises(ValidationError, match="no escalation"):
        inbox.ack(store, "nope")


def test_a_check_of_an_earlier_run_is_not_this_run_s_checkpoint(
    ws: Workspace, store: StateStore
) -> None:
    """A run is the unit a check is counted against, so a new one starts from its base."""
    scripted(ws, align_check=[ALIGNED])
    hyp_id = started(ws, store)
    align.check(ws, store, hyp_id)
    research.end(ws, store, hyp_id)
    research.begin(ws, store, hyp_id)

    run = records.require_active(store, hyp_id)

    assert align.checkpoint(store, run) == align.Checkpoint(cards=0, sha=run.base_sha)
    assert align.since(store, run) == 1
