"""The earlier cards: what a hypothesis tried under other pins, shown by idea and never by number.

The recent cards and the coverage stop at the run's pins, and the criteria version is one of
them, so every release used to hand a hypothesis's next run a blank slate. These tests drive
a run under an earlier criteria version or an earlier snapshot, end it, and read what the
next run's proposer is sent: the earlier cards in the system turn, every call of the run the
same bytes, no number among them, and nothing that judges a card any the wiser.

The earlier kanso is the installed one with its criteria version patched for the life of a
run. A run's pins are written once, when it begins, so that is all an upgrade after it
amounts to: its cards, its signatures and its events are stored under the version it began
under, as a run begun under 0.19.0 leaves them for 0.20.0 to find. One test upgrades a run
between its cards instead, as an operator does, so that the kanso that scored a card and the
pin it is shown under differ.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from kanso.criteria import criteria_version
from kanso.models import Call
from kanso.research import driver, records
from kanso.research import loop as research_loop
from kanso.schemas import RunRecord
from kanso.state import StateStore
from kanso.workspace import Workspace, find

from .conftest import DOCUMENT, classify, load_december
from .mocked import (  # noqa: F401
    SEED,
    Recorder,
    fresh_cursors,
    proposal,
    scripted,
    tuned,
)

OLD: Final = "0.19.0+d0c850ba1d88"
"""The criteria version the live lanes of 2026-10-26 had been begun under, before 0.20.0."""

EARLIER: list[dict[str, Any]] = [
    proposal("revert", desc="earlier: trade the trough", tags=["signal_mean_reversion"]),
    proposal("boom", desc="earlier: ask the impossible", tags=["sizing_volatility"]),
    proposal(
        "weak",
        desc="earlier: react to every step",
        tagged=False,
        tags=["exit_signal", "horizon_shorter"],
    ),
    proposal("aaa", desc="earlier: the same book again", tags=["exit_signal"]),
]
"""A keep, a crash, a discard and a redundant card. The discard is untagged, so the same
diff over the same best is the same bytes in a later run; the last holds the discard's book."""

LATER: list[dict[str, Any]] = [
    proposal("weak", desc="later: react to every step", tagged=False, tags=["filter_time"]),
    proposal("bbb", desc="later: another mode", tags=["filter_liquidity"]),
]
"""The earlier discard's very bytes, then a card that holds its book under the run's own pins."""

SHOWN: Final = frozenset({"sha7", "status", "tags", "desc", "criteria_version", "snapshot"})
"""Everything an earlier card is shown by: what it tried, and the pins of the run it was made in."""

CARD_FLOOR_S: Final = 600.0
"""The card budget's floor in this module: ten minutes, as `tests/cli/test_demo.py` lifts it.

These tests assert which status each card earned, and the differential asserts that two
workspaces earned the same ones, so a card killed for its clock in one of them fails the
test for a reason that has nothing to do with the earlier cards. Measured on a host whose
cores six research lanes held, under `taskpolicy -b`: a later run struck a 117.5 s budget
from a 39.2 s baseline and killed its third card at 137.0 s, in one workspace and not the
other. The slowest card that run measured took 137.0 s and the slowest baseline 104.9 s, so
the floor is over four times the one, and the 3x term binds only past a 200 s baseline. The
formula and its constants are pinned in `tests/research/test_loop.py`, and the kill in
`tests/nautilus/backtest/test_subprocess.py`."""


@pytest.fixture(autouse=True)
def card_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run begun here is given at least `CARD_FLOOR_S` a card."""
    monkeypatch.setattr(research_loop, "MIN_CARD_BUDGET_S", CARD_FLOOR_S)


@pytest.fixture
def prepared_hyp(ws: Workspace, store: StateStore) -> str:
    """The demo hypothesis, classified on the seed strategy, with the mock register."""
    scripted(ws)
    return classify(ws, store, DOCUMENT, SEED)


def run_under(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    propose: list[dict[str, Any]],
    *,
    criteria: str | None = None,
) -> RunRecord:
    """A run of `hyp_id` begun, carded once per answer and ended, under `criteria` when one
    is given: the research an earlier kanso leaves behind it."""
    with pytest.MonkeyPatch.context() as patch:
        if criteria is not None:
            patch.setattr(research_loop, "criteria_version", lambda: criteria)
        scripted(ws, propose=propose)
        driver.run(ws, store, hyp_id, cards=len(propose))
        run = records.require_active(store, hyp_id)
        research_loop.end(ws, store, hyp_id)
    return run


def stable_facts(call: Call, hyp_id: str) -> dict[str, Any]:
    """The facts of a call's system turn, the half a provider caches."""
    _, _, facts = call.system.rpartition(f"These facts do not change while you work on {hyp_id}:\n")
    found: dict[str, Any] = json.loads(facts)
    return found


def cards_of_run(store: StateStore, hyp_id: str, run: RunRecord) -> list[Any]:
    return [card for card in records.cards_of(store, hyp_id) if card.run_id == run.run_id]


def test_cards_under_an_earlier_kanso_are_shown_by_what_they_tried_and_nowhere_else(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """Every release moves the criteria version. The cards before it reach the next run's
    proposer in the cached half, the newest of the recent window's size and a count of all
    of them by tag, with the pins of the run each was made in and no number; the moving half, the
    coverage and the redundancy rule are the run's own."""
    earlier = run_under(ws, store, prepared_hyp, EARLIER, criteria=OLD)
    later_ws = tuned(ws, context_cards=3)
    scripted(later_ws, propose=LATER)

    driver.run(later_ws, store, prepared_hyp, cards=len(LATER))

    later = records.require_active(store, prepared_hyp)
    assert later.criteria_version == criteria_version() != OLD
    assert later.hypothesis_sha == earlier.hypothesis_sha
    assert later.snapshot_id == earlier.snapshot_id
    tried = cards_of_run(store, prepared_hyp, earlier)
    assert [card.status for card in tried] == ["keep", "keep", "crash", "discard", "redundant"]
    calls = recorded.of("propose")[len(EARLIER) :]
    assert len(calls) == len(LATER)

    # The cached half: the same bytes on every call of the run, the earlier cards in it.
    assert len({call.system for call in calls}) == 1
    facts = stable_facts(calls[0], prepared_hyp)
    assert facts["earlier_cards"] == [
        {
            "sha7": card.sha7,
            "status": card.status,
            "tags": list(card.tags),
            "desc": card.desc,
            "criteria_version": OLD,
            "snapshot": earlier.snapshot_id[:12],
        }
        for card in tried[-3:]
    ], "the newest context_cards x cards_per_turn of them, oldest first"
    assert facts["earlier_coverage"] == {
        "exit_signal": 2,
        "horizon_shorter": 1,
        "signal_mean_reversion": 1,
        "sizing_volatility": 1,
    }, "every earlier card by tag, the keep the window left out among them"

    # No number: their ideas count, their numbers do not, and the prompt says why.
    assert all(set(entry) == SHOWN for entry in facts["earlier_cards"])
    assert tried[1].metric != 0 and json.dumps(tried[1].metric) not in calls[0].system
    assert "so their numbers are not this run's comparison" in calls[0].system

    # Nowhere else: the moving half, the coverage and the books are the run's own.
    for call in calls:
        assert "earlier:" not in call.user
        assert OLD not in call.user
    made = cards_of_run(store, prepared_hyp, later)
    assert driver.coverage(store, later).keys() == {"filter_time", "filter_liquidity"}
    assert [card.desc for card in made][1:] == ["later: react to every step", "later: another mode"]
    # The earlier discard's bytes are carded and judged afresh: not refused as carded, and
    # not redundant against the book the earlier kanso stored for them.
    assert made[1].strategy_sha == tried[3].strategy_sha
    assert made[1].status == "discard"
    assert made[2].status == "redundant"
    assert [(entry["desc"], entry["like"]) for entry in driver._redundant_in(store, later)] == [
        ("later: another mode", made[1].sha7)
    ]


def test_cards_under_an_earlier_snapshot_are_earlier_cards_too(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A snapshot is the data a number was struck on, so a card made on another is shown by
    what it tried, under the same kanso as much as under another."""
    earlier = run_under(
        ws, store, prepared_hyp, [proposal("revert", desc="earlier: trade the trough")]
    )
    load_december(ws)  # a new snapshot, as `kanso data load` and `kanso data snapshot` leave
    scripted(ws, propose=[proposal("weak", desc="later: react to every step")])

    driver.run(ws, store, prepared_hyp, cards=1)

    later = records.require_active(store, prepared_hyp)
    assert later.snapshot_id != earlier.snapshot_id
    assert later.criteria_version == earlier.criteria_version
    (call,) = recorded.of("propose")[1:]
    shown = stable_facts(call, prepared_hyp)["earlier_cards"]
    assert [entry["desc"] for entry in shown] == ["baseline", "earlier: trade the trough"]
    assert {entry["snapshot"] for entry in shown} == {earlier.snapshot_id[:12]}
    assert {entry["criteria_version"] for entry in shown} == {earlier.criteria_version}
    assert stable_facts(call, prepared_hyp)["earlier_coverage"] == {"signal_mean_reversion": 1}
    assert "earlier:" not in call.user


def test_an_earlier_card_is_shown_under_its_runs_pin_not_the_kanso_that_scored_it(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A run is carded by whichever kanso is installed when its next card runs, so a run
    upgraded between its cards has later cards scored by the newer kanso under the older
    pin. Such a card is an earlier card of the next run begun under the newer kanso, shown
    under its run's pin, and the instruction says the label is that pin — not that this very
    kanso's card was scored by another."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(research_loop, "criteria_version", lambda: OLD)
        scripted(ws, propose=[proposal("revert", desc="earlier: before the upgrade")])
        driver.run(ws, store, prepared_hyp, cards=1)
    scripted(ws, propose=[proposal("weak", desc="earlier: after the upgrade")])
    driver.run(ws, store, prepared_hyp, cards=1)
    earlier = records.require_active(store, prepared_hyp)
    research_loop.end(ws, store, prepared_hyp)
    scripted(ws, propose=[proposal("weak", desc="later: react to every step")])

    driver.run(ws, store, prepared_hyp, cards=1)

    assert earlier.criteria_version == OLD
    assert records.require_active(store, prepared_hyp).criteria_version == criteria_version()
    *_, before, after = cards_of_run(store, prepared_hyp, earlier)
    assert after.desc == "earlier: after the upgrade"
    scored = [records.measurement_of(store, card) for card in (before, after)]
    assert [measured.criteria_version if measured else None for measured in scored] == [
        OLD,
        criteria_version(),
    ], "the card after the upgrade was scored by the kanso the next run is pinned to"
    (call,) = recorded.of("propose")[2:]
    shown = {entry["desc"]: entry for entry in stable_facts(call, prepared_hyp)["earlier_cards"]}
    assert shown[after.desc]["criteria_version"] == OLD == shown[before.desc]["criteria_version"]
    assert "the pins of the run it was made in" in call.system
    assert "not always the kanso that scored the card" in call.system


def test_the_earlier_window_is_one_question_under_other_pins_and_is_bounded(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """The same hypothesis file and another snapshot or criteria version; never the run's
    own pins, which the recent cards already show, and never another file, which asked
    another question. Bounded in SQL, the newest first to go in and the last to go out."""
    scripted(
        ws,
        propose=[proposal("revert"), proposal("weak"), proposal("aaa", tags=["exit_signal"])],
    )
    driver.run(ws, store, prepared_hyp, cards=3)
    run = records.require_active(store, prepared_hyp)
    cards = records.cards_of(store, prepared_hyp)
    assert len(cards) == 4
    upgraded = run.model_copy(update={"criteria_version": "0.21.0+0123456789ab"})
    resnapped = run.model_copy(update={"snapshot_id": "f" * 64})
    both = upgraded.model_copy(update={"snapshot_id": "f" * 64})
    asked_otherwise = both.model_copy(update={"hypothesis_sha": "0" * 64})

    def sha7s(found: list[dict[str, object]]) -> list[object]:
        return [entry["sha7"] for entry in found]

    assert driver._earlier(store, run, 10) == [], "the run's own pins are the recent cards'"
    assert driver._earlier_coverage(store, run) == {}
    for later in (upgraded, resnapped, both):
        assert sha7s(driver._earlier(store, later, 10)) == [card.sha7 for card in cards]
        assert driver._earlier_coverage(store, later) == {
            "exit_signal": 1,
            "signal_mean_reversion": 2,
        }
    assert driver._earlier(store, asked_otherwise, 10) == [], "another file, another question"
    assert driver._earlier_coverage(store, asked_otherwise) == {}
    assert sha7s(driver._earlier(store, upgraded, 2)) == [card.sha7 for card in cards[-2:]]
    assert driver._earlier(store, upgraded, 0) == []
    assert [str(row["strategy_sha"])[:7] for row in driver._recent(store, run, 10)] == [
        card.sha7 for card in cards
    ], "and the run's own window is untouched by it"

    def window(context_cards: int, cards_per_turn: int) -> list[object]:
        """The earlier cards the system turn carries under these two settings."""
        settings = tuned(ws, context_cards=context_cards, cards_per_turn=cards_per_turn)
        found = driver._stable(settings, store, upgraded)["earlier_cards"]
        assert isinstance(found, list)
        return sha7s(found)

    # The recent window's size, `context_cards` times `cards_per_turn`: each factor alone,
    # or the two added, would give another count out of the four cards.
    assert window(1, 3) == [card.sha7 for card in cards[-3:]]
    assert window(2, 1) == [card.sha7 for card in cards[-2:]]
    assert window(2, 2) == [card.sha7 for card in cards]


@dataclass(frozen=True)
class History:
    """What one workspace's later run judged, and what its proposer was sent."""

    judged: tuple[object, ...]
    systems: list[str]
    users: list[str]


def history(prepared: Path, root: Path, recorded: Recorder, *, withheld: bool) -> History:
    """The same earlier run and the same later one, in a workspace of their own, with the
    earlier block sent or withheld from the later run's proposer."""
    shutil.copytree(prepared, root)
    ws = find(root)
    with StateStore(ws.path("state.db")) as store, pytest.MonkeyPatch.context() as patch:
        store.migrate()
        scripted(ws)
        hyp_id = classify(ws, store, DOCUMENT, SEED)
        run_under(ws, store, hyp_id, EARLIER, criteria=OLD)
        if withheld:
            patch.setattr(driver, "_earlier", lambda *_: [])
            patch.setattr(driver, "_earlier_coverage", lambda *_: {})
        scripted(ws, propose=LATER)
        start = len(recorded.calls)

        driver.run(ws, store, hyp_id, cards=len(LATER))

        later = records.require_active(store, hyp_id)
        judged = (
            [
                (card.strategy_sha, card.status, card.metric, card.metric_se, card.n_trials)
                for card in cards_of_run(store, hyp_id, later)
            ],
            records.best_of(store, hyp_id),
            driver.coverage(store, later),
            driver._redundant_in(store, later),
            records.n_trials(store, hyp_id),
            driver._trailing_non_keeps(store, later),
        )
    calls = [call for call in recorded.calls[start:] if call.task_class == "propose"]
    return History(judged, [call.system for call in calls], [call.user for call in calls])


def test_the_earlier_cards_move_the_system_turn_and_nothing_that_judges(
    prepared: Path, tmp_path: Path, recorded: Recorder
) -> None:
    """The same history twice, the earlier block sent to one later run and withheld from
    the other. Every card that run made, its status, metric and error and trial count, the
    best, the coverage, the books it was shown and the stall count come out the same, and
    so does every byte of the moving half: the block changes what the proposer reads in the
    cached half and nothing else."""
    shown = history(prepared, tmp_path / "shown", recorded, withheld=False)
    withheld = history(prepared, tmp_path / "withheld", recorded, withheld=True)

    assert shown.judged == withheld.judged
    assert shown.users == withheld.users
    assert len(shown.users) == len(LATER)
    assert all('"earlier_cards"' in system for system in shown.systems)
    assert not any('"earlier_cards"' in system for system in withheld.systems)
