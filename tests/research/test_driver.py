"""The driver: thirty cards, a diff that will not fit, a stall, and two lanes apart.

Everything here runs through the mock register and the real loop, so a card the driver
produced is a real backtest of a real file and the statuses are facts about the strategy
rather than fixtures.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from kanso.errors import PreconditionError
from kanso.models import Answer, Call, spend
from kanso.models import router as router_module
from kanso.research import driver, lanes, records, scheduler
from kanso.schemas import ModelSpec
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, classify, document
from .mocked import CYCLE, SEED, fresh_cursors, proposal, scripted, tuned  # noqa: F401

NOWHERE = "--- a/strategy.py\n+++ b/strategy.py\n@@ -1,1 +1,1 @@\n-nowhere\n+here\n"
"""A diff whose context is in no version of any file."""

NOOP = "--- a/strategy.py\n+++ b/strategy.py\n@@ -40,1 +40,1 @@\n # end\n"
"""A diff that applies and changes nothing, which is not an experiment."""

SECOND_SEED = SEED.replace(b'mode = "flat"', b'mode = "flat"  # the second hypothesis')


@dataclass
class Recorder:
    """Every call that reached a client, so a test can read the prompt that went out."""

    calls: list[Call]

    def of(self, task: str) -> list[Call]:
        return [call for call in self.calls if call.task_class == task]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Watch the router's one client seam without changing what the client does."""
    seen = Recorder(calls=[])
    build = router_module.client_for

    def watching(root: Any, spec: ModelSpec) -> Any:
        client = build(root, spec)

        class Watched:
            protocol = client.protocol

            def complete(self, spec: ModelSpec, call: Call) -> Answer:
                seen.calls.append(call)
                return client.complete(spec, call)

        return Watched()

    monkeypatch.setattr(router_module, "client_for", watching)
    return seen


@pytest.fixture
def prepared_hyp(ws: Workspace, store: StateStore) -> Iterator[str]:
    """The demo hypothesis, classified on the seed strategy, with the mock register."""
    scripted(ws)
    yield classify(ws, store, DOCUMENT, SEED)


def statuses(store: StateStore, hyp_id: str) -> list[str]:
    return [card.status for card in records.cards_of(store, hyp_id)]


def test_thirty_cards_exercise_a_keep_a_crash_and_a_discard(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """One script of three answers drives a whole run, because the anchor survives."""
    scripted(ws, propose=CYCLE, align_check=[{"aligned": True, "reason": "unchanged idea"}])

    outcome = driver.run(ws, store, prepared_hyp, cards=30)

    assert outcome.proposed == 30
    assert (outcome.keeps, outcome.crashes, outcome.discards) == (1, 10, 19)
    assert outcome.reason == "cards"
    assert outcome.ended is False
    # Every card of the run, the baseline included, is a trial.
    assert records.n_trials(store, prepared_hyp) == 31
    assert statuses(store, prepared_hyp)[:4] == ["keep", "keep", "crash", "discard"]
    # align_every is 10 and the baseline is the run's first card, so checks land on 9, 19, 29.
    assert (outcome.checks, outcome.drifts) == (3, 0)
    assert records.active(store, prepared_hyp) is not None
    assert outcome.best_sha == records.cards_of(store, prepared_hyp)[1].strategy_sha


def test_a_diff_that_does_not_apply_is_invalid_output_and_takes_the_retry(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[{"desc": "move the anchor", "diff": NOWHERE}, proposal("revert")])

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert spend(store, lane="op").calls == 2
    assert statuses(store, prepared_hyp) == ["keep", "keep"]


def test_a_diff_that_changes_nothing_is_refused_before_it_costs_a_card(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[{"desc": "think again", "diff": NOOP}, proposal("revert")])

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert spend(store, lane="op").calls == 2


def test_a_description_that_is_not_one_line_is_corrected_on_the_ladder(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    bad = {"desc": "two\nlines", "diff": proposal("revert")["diff"]}
    scripted(ws, propose=[bad, proposal("revert", desc="trade the trough")])

    driver.run(ws, store, prepared_hyp, cards=1)

    assert records.cards_of(store, prepared_hyp)[-1].desc == "trade the trough"


def test_a_proposer_that_never_fits_fails_the_step_after_the_whole_ladder(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[{"desc": "never fits", "diff": NOWHERE}])

    with pytest.raises(PreconditionError, match="in 3 attempts"):
        driver.run(ws, store, prepared_hyp, cards=1)

    assert statuses(store, prepared_hyp) == ["keep"]
    assert spend(store, lane="op").calls == 3


def test_cards_counts_proposals_and_a_second_call_resumes_the_same_run(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    first = driver.run(ws, store, prepared_hyp, cards=2)
    second = driver.run(ws, store, prepared_hyp, cards=1)

    assert (first.proposed, second.proposed) == (2, 1)
    assert second.run_id == first.run_id
    assert len(records.cards_of(store, prepared_hyp)) == 4
    assert len(records.runs_of(store, prepared_hyp)) == 1


def test_a_stall_ends_the_run_and_requeues_the_hypothesis(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("boom")])

    outcome = driver.run(workspace, store, prepared_hyp)

    assert outcome.reason == "stalled"
    assert (outcome.proposed, outcome.crashes, outcome.ended) == (2, 2, True)
    assert records.active(store, prepared_hyp) is None
    assert not lanes.lane_dir(workspace, "op", prepared_hyp).exists()
    assert [item.hyp_id for item in scheduler.queued(store)] == [prepared_hyp]
    assert scheduler.queued(store)[0].priority == scheduler.STALL_PRIORITY


def test_two_lanes_never_touch_each_other_s_files(ws: Workspace, store: StateStore) -> None:
    scripted(ws, propose=[proposal("revert")])
    first = classify(ws, store, DOCUMENT, SEED)
    second = classify(ws, store, document(id="demo_two"), SECOND_SEED)

    driver.run(ws, store, first, cards=1, lane="op")
    driver.run(ws, store, second, cards=1, lane="l1")

    here = lanes.lane_dir(ws, "op", first)
    there = lanes.lane_dir(ws, "l1", second)
    assert here != there
    for directory in (here, there):
        assert sorted(path.name for path in directory.iterdir()) == [
            "hypothesis.yaml",
            "program.md",
            "strategy.py",
        ]
    assert b"the second hypothesis" not in (here / "strategy.py").read_bytes()
    assert b"the second hypothesis" in (there / "strategy.py").read_bytes()
    mine = {card.strategy_sha for card in records.cards_of(store, first)}
    theirs = {card.strategy_sha for card in records.cards_of(store, second)}
    assert mine.isdisjoint(theirs)


def test_the_prompt_keeps_a_stable_prefix_and_carries_the_moving_half(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    scripted(ws, propose=CYCLE)

    driver.run(ws, store, prepared_hyp, cards=3)

    proposals = recorded.of("propose")
    assert len(proposals) == 3
    # A provider caches by exact prefix bytes, so the system turn may not move.
    assert len({call.system for call in proposals}) == 1
    assert "program.md" in proposals[0].system
    assert "wf_sharpe_net" in proposals[0].system
    assert "recent_cards" in proposals[0].user
    # The third proposal follows the crash the second one caused.
    assert "crash_tail" in proposals[2].user
    assert "the card asked for the impossible" in proposals[2].user
    assert "last_diff" in proposals[1].user


def test_failing_certification_gates_reach_the_next_proposal(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    scripted(ws, propose=[proposal("revert")])
    store.connection.execute(
        "INSERT INTO certificates (hyp_id, strategy_sha, plan_version, nautilus_version,"
        " snapshot_id, criteria_version, gates, n_trials, verdict, path, created_at)"
        " VALUES (?, 'a' * 64, 1, '1.231.0', 'snap', '0.1.0', ?, 1, 'fail', 'p.yaml',"
        " '2024-01-01T00:00:00+00:00')",
        (
            prepared_hyp,
            '[{"id": "embargoed_window", "pass": true},'
            ' {"id": "deflated_sharpe", "pass": false, "evidence": {"dsr": 0.1}}]',
        ),
    )

    driver.run(ws, store, prepared_hyp, cards=1)

    user = recorded.of("propose")[0].user
    assert "failing_certification_gates" in user
    assert "deflated_sharpe" in user
    assert "embargoed_window" not in user


def test_a_lane_that_lost_its_strategy_is_given_it_back_and_carries_on(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[proposal("revert")])
    driver.run(ws, store, prepared_hyp, cards=1)
    (lanes.lane_dir(ws, "op", prepared_hyp) / "strategy.py").unlink()

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1


def test_the_outcome_is_json(ws: Workspace, store: StateStore, prepared_hyp: str) -> None:
    scripted(ws, propose=[proposal("revert")])

    payload = driver.run(ws, store, prepared_hyp, cards=1).payload()

    assert payload["id"] == prepared_hyp
    assert payload["reason"] == "cards"
    assert payload["keeps"] == 1
