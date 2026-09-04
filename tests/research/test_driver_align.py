"""Alignment: the cheap half first, the model second, and what a drift costs a run.

Drift is the one failure the loop cannot measure, because a strategy that stopped testing
the idea can score beautifully. So it is checked on a clock and answered by rewinding: the
run goes back to ground a check already passed, the cards in between are marked, and
research carries on rather than stopping for a judgement call.
"""

from __future__ import annotations

import pytest

from kanso import inbox, research
from kanso.errors import ValidationError
from kanso.models import spend
from kanso.research import align, lanes, records
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, classify
from .mocked import ALIGNED, DRIFTED, SEED, fresh_cursors, scripted  # noqa: F401

FOREIGN = SEED.replace(b"# end", b'Strategy.symbol = "OTHER.XNAS"\n\n# end')
"""A file that names an instrument the universe does not hold: drift the tree can prove."""

REVERTING = SEED.replace(b"# end", b'Strategy.mode = "revert"\n\n# end')


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

    assert align.check(ws, store, hyp_id) == (True, None)
    assert aligned_flags(store, hyp_id) == [1]

    run = records.require_active(store, hyp_id)
    assert align.checkpoint(store, run).cards == 1
    assert align.since(store, run) == 0
    assert [e.kind for e in store.events(subject=hyp_id)].count(align.ALIGNED) == 1


def test_drift_before_any_aligned_keep_rewinds_to_the_bytes_the_run_began_with(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)

    ok, reason = align.check(ws, store, hyp_id)

    assert (ok, reason) == (False, DRIFTED["reason"])
    assert lane_file(ws, hyp_id) == SEED
    assert workspace_file(ws, hyp_id) == SEED
    assert records.best_of(store, hyp_id) == (None, None)
    assert records.require_active(store, hyp_id).best_sha is None
    assert aligned_flags(store, hyp_id) == [0]


def test_drift_rewinds_to_the_last_keep_a_check_had_already_passed(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws, align_check=[ALIGNED, DRIFTED])
    hyp_id = started(ws, store)
    write_lane(ws, hyp_id, REVERTING)
    kept = research.card(ws, store, hyp_id, "trade the trough")
    assert kept.status == "keep"
    assert align.check(ws, store, hyp_id) == (True, None)

    write_lane(ws, hyp_id, SEED)
    dropped = research.card(ws, store, hyp_id, "back to trading nothing")
    assert dropped.status == "discard"

    ok, _ = align.check(ws, store, hyp_id)

    assert ok is False
    assert lane_file(ws, hyp_id) == REVERTING
    assert workspace_file(ws, hyp_id) == REVERTING
    assert records.best_of(store, hyp_id) == (kept.strategy_sha, kept.metric)
    assert aligned_flags(store, hyp_id) == [1, 1, 0]


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
