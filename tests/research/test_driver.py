"""The driver: thirty cards, a diff that will not fit, a stall, and two lanes apart.

Everything here runs through the mock register and the real loop, so a card the driver
produced is a real backtest of a real file and the statuses are facts about the strategy
rather than fixtures.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kanso import inbox
from kanso.certify import certificate
from kanso.config import ResearchConfig
from kanso.errors import PreconditionError
from kanso.hyp import STRATEGY_FILE
from kanso.models import INSTRUCTIONS, reset_mock, spend
from kanso.research import align, driver, lanes, records, scheduler
from kanso.research import loop as research_loop
from kanso.schemas import CardDigest
from kanso.schemas.models import ROUTING_DEFAULTS
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, classify, document, measured_as_scored
from .mocked import (  # noqa: F401
    ALIGNED,
    CYCLE,
    DRIFTED,
    MARKER,
    SEED,
    Recorder,
    fresh_cursors,
    proposal,
    scripted,
    tuned,
)
from .test_scheduler import open_run

NOWHERE = "--- a/strategy.py\n+++ b/strategy.py\n@@ -1,1 +1,1 @@\n-nowhere\n+here\n"
"""A diff whose context is in no version of any file."""

NOOP = "--- a/strategy.py\n+++ b/strategy.py\n@@ -40,1 +40,1 @@\n # end\n"
"""A diff that applies and changes nothing, which is not an experiment."""

CONSTANT_ONLY = (
    "--- a/strategy.py\n"
    "+++ b/strategy.py\n"
    "@@ -5,1 +5,1 @@\n"
    "-    notional: float = 5_000.0\n"
    "+    notional: float = 6_000.0\n"
)
"""A diff that moves a number and nothing else: local in one phase, refused in the other."""

PARAMETER: dict[str, Any] = {
    "desc": "trade a little larger",
    "diff": CONSTANT_ONLY,
    "tags": ["parameter_only"],
}

DENIED = (
    "--- a/strategy.py\n"
    "+++ b/strategy.py\n"
    "@@ -40,1 +40,2 @@\n"
    "+Strategy.peek = lambda self: self.cache\n"
    f" {MARKER}\n"
)
"""A diff that applies and reaches for the cache, which `strategy_integrity` refuses."""

SECOND_SEED = SEED.replace(b'mode = "flat"', b'mode = "flat"  # the second hypothesis')


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
    # The second turn of the cycle holds what the first held: `revert` and `weak` are
    # redundant from then on, and only the crash, which held nothing judged, is a card.
    assert (outcome.keeps, outcome.crashes, outcome.discards) == (1, 10, 1)
    assert (outcome.missed, outcome.redundant) == (0, 18)
    assert outcome.reason == "cards"
    assert outcome.ended is False
    # Every proposal that ran is a card and a trial, the baseline and the eighteen
    # redundant results included: thirty proposals and one baseline.
    assert records.n_trials(store, prepared_hyp) == 31
    assert statuses(store, prepared_hyp)[:4] == ["keep", "keep", "crash", "discard"]
    # align_every is 10 and the baseline is the run's first card, so the checks land on
    # the ninth, nineteenth and twenty-ninth card proposed: every card advances the clock,
    # and here every proposal is one.
    assert (outcome.checks, outcome.drifts) == (3, 0)
    assert records.active(store, prepared_hyp) is not None
    assert outcome.best_sha == records.cards_of(store, prepared_hyp)[1].strategy_sha


def test_a_diff_that_does_not_apply_is_invalid_output_and_takes_the_retry(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(
        ws,
        propose=[
            {"desc": "move the anchor", "diff": NOWHERE, "tags": ["refactor"]},
            proposal("revert"),
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert spend(store, lane="op").calls == 2
    assert statuses(store, prepared_hyp) == ["keep", "keep"]


def test_a_diff_that_changes_nothing_is_refused_before_it_costs_a_card(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(
        ws,
        propose=[{"desc": "think again", "diff": NOOP, "tags": ["refactor"]}, proposal("revert")],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert spend(store, lane="op").calls == 2


def test_a_diff_that_reproduces_carded_bytes_is_refused_with_the_card_it_repeats(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A discard restores the best, so the same untagged diff makes the discarded bytes again."""
    scripted(ws, propose=[proposal("weak", tagged=False)])
    driver.run(ws, store, prepared_hyp, cards=1)
    discarded = records.cards_of(store, prepared_hyp)[-1]
    assert discarded.status == "discard"
    before = spend(store, lane="op").calls
    reset_mock()
    scripted(ws, propose=[proposal("weak", tagged=False), proposal("boom")])

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert spend(store, lane="op").calls == before + 2
    assert statuses(store, prepared_hyp) == ["keep", "discard", "crash"]
    retry = recorded.of("propose")[-1]
    assert (
        f"already carded under the run's pins ({discarded.strategy_sha[:7]}: discard" in retry.user
    )


def test_a_proposer_with_nothing_new_is_a_miss_that_counts_toward_the_stall(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """One discard, then the same bytes again: the ladder runs out, and that is the stall."""
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("weak", tagged=False)])

    outcome = driver.run(workspace, store, prepared_hyp)

    assert outcome.reason == "stalled"
    assert (outcome.proposed, outcome.discards, outcome.missed) == (2, 1, 1)
    assert outcome.payload()["missed"] == 1
    assert statuses(store, prepared_hyp) == ["keep", "discard"], "a miss is not a card"
    repeats = store.events(kind=driver.REPEATED, subject=prepared_hyp)
    assert len(repeats) == 1
    assert "already carded under the run's pins" in str(repeats[0].detail["because"])
    assert records.active(store, prepared_hyp) is None, "the run stalled and ended"


def test_a_repeat_earlier_in_the_ladder_is_still_a_miss(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """Told it repeated itself, the model answered with a diff that does not apply."""
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("weak", tagged=False)])
    driver.run(workspace, store, prepared_hyp, cards=1)
    reset_mock()
    scripted(
        workspace,
        propose=[
            proposal("weak", tagged=False),
            {"desc": "move the anchor", "diff": NOWHERE, "tags": ["refactor"]},
        ],
    )

    outcome = driver.run(workspace, store, prepared_hyp)

    assert (outcome.missed, outcome.reason) == (1, "stalled")
    repeats = store.events(kind=driver.REPEATED, subject=prepared_hyp)
    assert "already carded under the run's pins" in str(repeats[0].detail["because"])


def test_a_ladder_that_never_repeats_still_fails_the_step(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[{"desc": "never fits", "diff": NOWHERE, "tags": ["refactor"]}])

    with pytest.raises(PreconditionError, match="in 3 attempts"):
        driver.run(ws, store, prepared_hyp, cards=1)

    assert store.events(kind=driver.REPEATED) == []


def test_a_miss_counts_against_the_cards_asked_for_and_survives_a_resume(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[proposal("weak", tagged=False)])
    driver.run(ws, store, prepared_hyp, cards=1)
    before = spend(store, lane="op").calls

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert (outcome.proposed, outcome.missed) == (1, 1)
    assert spend(store, lane="op").calls == before + 3, "the whole ladder, then the miss"
    assert statuses(store, prepared_hyp) == ["keep", "discard"]
    active = records.active(store, prepared_hyp)
    assert active is not None
    assert driver._trailing_non_keeps(store, active) == 2, "the discard and the miss"


def test_a_redundant_card_is_recorded_and_the_next_proposal_is_told_what_it_repeated(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A candidate that held what a judged strategy held costs the backtest and is worth
    what the backtest measured: a card of its own, a trial, and a coverage entry. It
    still counts toward the stall exactly once, survives a resume, and reaches the next
    proposal by the name of the card it repeated, which no card carries."""
    scripted(ws, propose=[proposal("revert"), proposal("revert", desc="the same bet again")])

    outcome = driver.run(ws, store, prepared_hyp, cards=2)

    assert (outcome.proposed, outcome.keeps, outcome.redundant, outcome.missed) == (2, 1, 1, 0)
    assert statuses(store, prepared_hyp) == ["keep", "keep", "redundant"]
    (event,) = store.events(kind=research_loop.REDUNDANT, subject=prepared_hyp)
    kept = records.cards_of(store, prepared_hyp)[1]
    repeat = records.cards_of(store, prepared_hyp)[2]
    assert (repeat.desc, repeat.metric) == ("the same bet again", kept.metric)
    assert records.trial_metrics(store, prepared_hyp, "wf_sharpe_net") == [
        kept.metric,
        repeat.metric,
    ]
    walked = driver.coverage(store, records.require_active(store, prepared_hyp))
    assert walked["signal_mean_reversion"] == {
        "count": 2,
        "best_metric": kept.metric,
        "best_status": "keep",
        "newest": repeat.sha7,
    }, "the corner is walked twice, and the redundant card is the newest thing in it"
    assert event.detail["like"] == kept.sha7
    assert event.detail["desc"] == "the same bet again"
    active = records.require_active(store, prepared_hyp)
    assert driver._trailing_non_keeps(store, active) == 1, "the card, counted once"
    assert align.lane_strategy(store, active, ws.root / active.dir) == store.get_blob(
        kept.strategy_sha
    )

    driver.run(ws, store, prepared_hyp, cards=1)

    third = recorded.of("propose")[-1]
    assert '"redundant"' in third.user
    assert f'"like": "{kept.sha7}"' in third.user
    assert "the same bet again" in third.user
    assert json.loads(third.user)["redundant"] == [
        {"desc": "the same bet again", "like": kept.sha7, "pct": 100.0, "repeat": True}
    ], "a repeat, which is the kind the turn was refused for"


def test_a_measured_book_whose_number_moved_reaches_the_next_proposal_as_no_repeat(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The other kind of book already measured, and the proposer is shown both.

    The stored anchor is moved past this hypothesis's floor between the two cards, so the
    second holds a book already measured and earns a number the first denies. Nothing is
    refused -- it is an experiment and an ordinary discard -- and without a record of it
    the proposer is shown nothing at all and re-treads the book it was never told about,
    which is what the `redundant` fact exists to stop.
    """
    scripted(ws, propose=[proposal("revert"), proposal("revert", desc="the same bet again")])
    driver.run(ws, store, prepared_hyp, cards=1)
    kept = records.cards_of(store, prepared_hyp)[1]
    store.connection.execute(
        "UPDATE signatures SET metric = metric + 1000 WHERE strategy_sha = ?",
        (kept.strategy_sha,),
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert (outcome.redundant, outcome.discards, outcome.missed) == (0, 1, 0)
    assert statuses(store, prepared_hyp) == ["keep", "keep", "discard"]
    assert store.events(kind=research_loop.REDUNDANT, subject=prepared_hyp) == []
    (event,) = store.events(kind=research_loop.SAME_BOOK, subject=prepared_hyp)
    assert event.detail["like"] == kept.sha7

    driver.run(ws, store, prepared_hyp, cards=1)

    told = json.loads(recorded.of("propose")[-1].user)["redundant"]
    assert told == [
        {"desc": "the same bet again", "like": kept.sha7, "pct": 100.0, "repeat": False}
    ]


def test_a_redundant_card_advances_the_drift_clock_and_the_next_turn_s_last_diff(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A redundant result is a row in `cards`, and `align.since` counts rows in `cards`.

    A driver that kept its own counter and skipped this one would check for drift half as
    often as the number it resumes from says, for as long as the lane stayed redundant —
    and would hand the next proposal the diff of the card before last while showing it the
    redundant card as the newest thing tried.
    """
    workspace = tuned(ws, align_every=3)
    scripted(workspace, propose=[proposal("revert")], align_check=[ALIGNED])

    outcome = driver.run(workspace, store, prepared_hyp, cards=3)

    assert (outcome.keeps, outcome.redundant) == (1, 2)
    # The baseline is the run's first card, so the third card proposed is the tenth
    # counted here: the check lands on the second redundant result.
    assert (outcome.checks, outcome.drifts) == (1, 0)
    active = records.require_active(store, prepared_hyp)
    assert align.since(store, active) == 1, "the persisted clock is the driver's own"
    repeat = records.cards_of(store, prepared_hyp)[2]
    added = [
        line
        for line in store.get_blob(repeat.strategy_sha).decode("utf-8").splitlines()
        if line.startswith("Strategy.mode")
    ][-1]
    told = json.loads(recorded.of("propose")[2].user)["last_diff"]
    assert f"+{added}" in told, "the redundant card is the change the last card made"


def test_redundant_misses_count_toward_the_stall(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("revert")])

    outcome = driver.run(workspace, store, prepared_hyp)

    assert outcome.reason == "stalled"
    assert (outcome.keeps, outcome.redundant) == (1, 2)
    assert records.active(store, prepared_hyp) is None


def test_a_crash_buys_a_repair_turn_carrying_the_change_that_crashed(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The idea in a crashed card was never judged, and the file that held it was restored
    the moment it crashed — so the proposer is handed it back as a diff over the file it
    now has, with the traceback, and asked for the same idea without the fault."""
    scripted(ws, propose=[proposal("boom"), proposal("revert")])

    outcome = driver.run(ws, store, prepared_hyp, cards=2)

    assert (outcome.crashes, outcome.keeps) == (1, 1)
    first, second = recorded.of("propose")
    assert '"repair"' not in first.user, "nothing has crashed yet"
    asked = json.loads(second.user)["repair"]
    assert (asked["attempt"], asked["of"], asked["desc"]) == (1, driver.REPAIRS, "run in boom mode")
    assert '+Strategy.mode = "boom"' in str(asked["diff"]), "the change, over the file in hand"
    assert "the card asked for the impossible" in second.user, "and why it raised"


def test_the_repairs_run_out_and_the_idea_is_dropped(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """Two goes at one fault, then it is a miss like any other: a proposer that has not
    read the traceback twice will not read it a third time, and the run has other ideas."""
    scripted(ws, propose=[proposal("boom")])

    driver.run(ws, store, prepared_hyp, cards=driver.REPAIRS + 2)

    attempts = [
        json.loads(call.user).get("repair", {}).get("attempt") for call in recorded.of("propose")
    ]
    assert attempts == [None, 1, 2, None]
    assert statuses(store, prepared_hyp) == ["keep"] + ["crash"] * (driver.REPAIRS + 2)


def test_a_repair_returned_verbatim_is_a_wrong_answer_and_not_a_second_crash(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """The repair diff runs from the file in hand to the bytes that crashed, so returning
    it unchanged makes those bytes again — and bytes already carded under the pins are a
    wrong answer on the ladder. That is what stops a repair costing a second identical
    card, and it is the claim the whole bound rests on."""
    scripted(ws, propose=[proposal("boom", tagged=False)])

    outcome = driver.run(ws, store, prepared_hyp, cards=3)

    assert (outcome.crashes, outcome.missed, outcome.redundant) == (1, 2, 0)
    assert statuses(store, prepared_hyp) == ["keep", "crash"], "one crash, carded once"


def test_the_budget_is_spent_per_idea_and_the_next_crash_has_its_own(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The measurement the repair exists for is five consecutive crashes that were five
    ideas, so a budget counted over the streak rather than over the idea would repair the
    first of them and drop the four after it unjudged — which is what it was built to
    stop. The fourth crash is a fresh idea's, and it owes a first repair."""
    scripted(ws, propose=[proposal("boom")])

    driver.run(ws, store, prepared_hyp, cards=2 * (driver.REPAIRS + 1))

    attempts = [
        json.loads(call.user).get("repair", {}).get("attempt") for call in recorded.of("propose")
    ]
    assert attempts == [None, *range(1, driver.REPAIRS + 1)] * 2


def test_a_card_killed_for_its_memory_owes_no_repair(
    ws: Workspace,
    store: StateStore,
    prepared_hyp: str,
    recorded: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kill is the runner's verdict on what a card held, not a fault a traceback names, so
    the turn after it is a fresh proposal, shown the kill's `resources` verdict and no
    traceback. Before this a kill was owed two repairs like any crash, each a model call and
    a child killed again under the same limit."""
    monkeypatch.setattr("kanso.nautilus.backtest.MEMORY_POLL_S", 0.0)
    monkeypatch.setattr(research_loop, "mem_cap", lambda *args, **kwargs: 1e-6)
    scripted(ws, propose=[proposal("revert"), proposal("weak"), proposal("aaa")])

    outcome = driver.run(ws, store, prepared_hyp, cards=3)

    assert outcome.crashes == 3
    calls = recorded.of("propose")
    assert [json.loads(call.user).get("repair") for call in calls] == [None, None, None]
    assert all("crash_tail" not in json.loads(call.user) for call in calls)
    shown = json.loads(calls[1].user)["recent_cards"][-1]
    assert shown["status"] == "crash"
    assert shown["failed_gates"][0]["id"] == research_loop.RESOURCES
    assert shown["failed_gates"][0]["evidence"]["killed"] == "memory"


def test_a_slot_killed_for_its_clock_owes_no_repair_and_the_next_turn_is_a_batch(
    ws: Workspace,
    store: StateStore,
    prepared_hyp: str,
    recorded: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from kanso.nautilus import backtest

    ws = tuned(ws, cards_per_turn=2)
    scripted(
        ws,
        propose=[
            {"proposals": [proposal("weak"), proposal("revert")]},
            {"proposals": [proposal("aaa")]},
        ],
    )
    real = backtest.run_subprocesses

    def second_out_of_time(requests: Any, *args: Any, **kwargs: Any) -> Any:
        ran = real(requests, *args, **kwargs)
        killed = replace(
            ran[-1],
            run=backtest._empty(requests[-1]),
            intents=(),
            crashed=True,
            reason=backtest.BUDGET,
        )
        return [*ran[:-1], killed] if len(ran) == 2 else ran

    monkeypatch.setattr(research_loop.backtest, "run_subprocesses", second_out_of_time)

    driver.run(ws, store, prepared_hyp, cards=2)

    assert statuses(store, prepared_hyp)[:3] == ["keep", "discard", "crash"]
    first, second = recorded.of("propose")
    assert "proposals" in json.dumps(second.schema), "the next turn is a batch again"
    assert "repair" not in json.loads(second.user)
    event = [e for e in store.events(kind="batched", subject=prepared_hyp)][0]
    assert event.detail["crash_sha"] is None, "no slot of the turn owes a repair"


def test_a_width_the_lane_cannot_fund_is_refused_before_a_model_is_asked(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    from .conftest import ENVELOPE, write_envelope

    ws = tuned(ws, cards_per_turn=8)
    plan = ENVELOPE.plan.model_copy(update={"mem_per_lane_gb": 0.5})
    write_envelope(ws, ENVELOPE.model_copy(update={"plan": plan}))
    scripted(ws, propose=[{"proposals": [proposal("revert")]}])

    with pytest.raises(PreconditionError, match="cards a turn") as refused:
        driver.run(ws, store, prepared_hyp, cards=1)

    assert "`[research] cards_per_turn = " in str(refused.value.remedy)
    assert recorded.of("propose") == []


def _fact_keys() -> set[str]:
    """Every key `_dynamic` can put in the user turn, read out of its own source.

    A list written by hand would go stale the first time a fact was added, which is the
    defect this test exists for: the `redundant` array reached the proposer as an
    undeclared key for a day, and 2,822 proposals were refused by a rule the instruction
    never stated.

    The scan reads literal string keys assigned in `_dynamic` and nowhere else, so a fact
    added through `facts.update(...)` or in a helper would not be seen; and the check it
    feeds asks only that the key word appears in backticks somewhere in the instruction,
    which a word used for something else would satisfy. It catches a new undeclared key,
    which is the defect; it is not a proof that every fact is explained.
    """
    tree = ast.parse(Path(driver.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_dynamic"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.AnnAssign) and isinstance(inner.value, ast.Dict):
                found |= {
                    key.value
                    for key in inner.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
            subscripted = (
                isinstance(inner, ast.Subscript)
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "facts"
                and isinstance(inner.slice, ast.Constant)
            )
            if subscripted:
                found.add(str(inner.slice.value))  # type: ignore[attr-defined, union-attr]
    return found


def test_every_fact_the_proposer_is_sent_is_named_in_its_instruction() -> None:
    """A fact the instruction does not name is a rule the proposer was never told."""
    said = INSTRUCTIONS["propose"]
    keys = _fact_keys()
    assert {"recent_cards", "coverage", "phase", "redundant"} <= keys, "the scan found them"
    assert [key for key in sorted(keys) if f"`{key}`" not in said] == []
    assert f"`{STRATEGY_FILE}`" in said, "the file itself, which is not a constant key"


def test_the_instruction_names_every_number_a_recent_card_s_digest_carries() -> None:
    """A field the proposer is shown and never told the meaning of is a number it guesses at."""
    said = INSTRUCTIONS["propose"]
    assert "`n_trades`" in said and "`digest`" in said
    assert [name for name in CardDigest.model_fields if f"`{name}`" not in said] == []


def test_the_instruction_states_the_granularity_the_signature_reads_at() -> None:
    """A signature's mark is per session — the instrument, the side, and whether it was
    still on at the end — so the same holding moved to another hour of the same session
    is the same signature. The instruction told the proposer to change the time of day to
    be in or out of the market, which is advice the rule refuses again, and a refusal the
    proposer was never told about is the wasted ladder this whole paragraph exists for.
    """
    said = INSTRUCTIONS["propose"]
    assert "a different time of day to be in or out of the market" not in said
    assert "The reading is by session" in said


def test_the_phase_is_a_rule_of_misses_and_it_cycles() -> None:
    settings = ResearchConfig(local_cards=10, structural_cards=10)
    assert [driver.phase(settings, n) for n in (0, 9, 10, 19, 20, 29, 30)] == [
        "local",
        "local",
        "structural",
        "structural",
        "local",
        "local",
        "structural",
    ]


def test_a_constant_only_change_is_a_card_in_the_local_phase_and_refused_in_the_structural(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The same answer, judged by the phase the run is in: one miss puts a run tuned to
    `local_cards = 1` into its structural phase, where a number moved is not the
    experiment being asked for and the ladder walks on to an answer that is."""
    workspace = tuned(ws, local_cards=1, structural_cards=5)
    scripted(workspace, propose=[PARAMETER, PARAMETER, proposal("weak")])

    outcome = driver.run(workspace, store, prepared_hyp, cards=2)

    # The first is local: it ran, held nothing like the flat baseline, and was redundant.
    # The second is structural: refused on the ladder, so the third answer is the card.
    assert (outcome.proposed, outcome.redundant, outcome.discards, outcome.missed) == (2, 1, 1, 0)
    calls = recorded.of("propose")
    assert len(calls) == 3
    assert '"name": "local"' in calls[0].user
    assert '"misses_since_keep": 0' in calls[0].user
    assert '"name": "structural"' in calls[1].user
    assert '"misses_since_keep": 1' in calls[1].user
    assert "moves no structure" in calls[2].user, "the ladder carries the complaint"


def test_a_ladder_that_only_moves_numbers_in_the_structural_phase_is_a_miss(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    workspace = tuned(ws, local_cards=1, structural_cards=5)
    scripted(workspace, propose=[PARAMETER])

    outcome = driver.run(workspace, store, prepared_hyp, cards=2)

    assert (outcome.redundant, outcome.missed) == (1, 1)
    (event,) = store.events(kind=driver.REPEATED, subject=prepared_hyp)
    assert "moves no structure" in str(event.detail["because"])
    assert "after 1 misses" in str(event.detail["because"])


def test_a_candidate_that_does_not_parse_is_not_judged_by_its_structure(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """A syntax error is the card's to discard — the static integrity check refuses it
    before any backtest — not the skeleton's to refuse on the ladder."""
    workspace = tuned(ws, local_cards=1, structural_cards=5)
    broken = {
        **PARAMETER,
        "diff": CONSTANT_ONLY.replace("+    notional: float = 6_000.0", "+    notional float"),
    }
    scripted(workspace, propose=[PARAMETER, broken])

    outcome = driver.run(workspace, store, prepared_hyp, cards=2)

    assert (outcome.redundant, outcome.discards, outcome.missed) == (1, 1, 0)


def test_recent_cards_reach_across_runs_under_the_same_pins(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A run begun after a stall is shown what the stalled run tried."""
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("boom")])
    stalled = driver.run(workspace, store, prepared_hyp)
    assert stalled.ended
    reset_mock()
    scripted(workspace, propose=[proposal("weak")])

    driver.run(workspace, store, prepared_hyp, cards=1)

    first = recorded.of("propose")[-1]
    assert "run in boom mode" in first.user
    assert first.user.count('"status": "crash"') == 2
    # As the record of what was tried, never as a traceback over a file this run does not
    # hold: the new run's own baseline card is the newest under the pins, and the crash
    # tail and the repair are both read from that one row.
    payload = json.loads(first.user)
    assert "crash_tail" not in payload and "repair" not in payload


def test_each_recent_card_that_ran_shows_its_trades_and_its_edge_per_trade(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A metric alone cannot say whether it moved because each trade earned more or because
    the strategy traded less, so a card that ran reaches the proposer with its trade count
    and its digest: the same numbers the card recorded. A crash ran to no result and
    carries neither a digest nor a trade."""
    scripted(ws, propose=[proposal("revert"), proposal("boom"), proposal("weak")])

    driver.run(ws, store, prepared_hyp, cards=3)

    shown = json.loads(recorded.of("propose")[-1].user)["recent_cards"]
    baseline, kept, crashed = records.cards_of(store, prepared_hyp)[:3]
    assert [card["status"] for card in shown] == ["keep", "keep", "crash"]
    assert [card["n_trades"] for card in shown] == [0, kept.n_trades, 0]
    assert kept.n_trades > 0 and kept.digest is not None
    assert shown[1]["digest"]["net_bps_per_trade"] == round(kept.digest.net_bps_per_trade, 4)
    assert shown[1]["digest"]["sessions"] == kept.digest.sessions
    assert set(shown[1]["digest"]) == set(type(kept.digest).model_fields)
    assert shown[0]["digest"]["net_bps_per_trade"] is None, "the flat baseline closed nothing"
    assert "digest" not in shown[2] and crashed.digest is None
    assert baseline.digest is not None


def test_a_redundant_result_from_an_earlier_run_reaches_the_next_one(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """Measured on one live hypothesis, 560 of 991 redundant refusals repeated an idea
    from an earlier run against 18 within their own. Scoped to the run, the window a
    proposer is shown was emptied by every reseed; scoped to the pins, it is not."""
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("revert"), proposal("revert", desc="the trough again")])
    stalled = driver.run(workspace, store, prepared_hyp)
    assert stalled.ended and stalled.redundant
    reset_mock()
    scripted(workspace, propose=[proposal("weak")])

    driver.run(workspace, store, prepared_hyp, cards=1)

    # Read out of the `redundant` fact itself: the descriptions are in `recent_cards`
    # too, so a test that searched the whole prompt for them passed with the window
    # scoped back to the run, which is the change it is here to hold down.
    repeated = json.loads(recorded.of("propose")[-1].user)["redundant"]
    kept = records.cards_of(store, prepared_hyp)[1]
    assert [entry["desc"] for entry in repeated] == ["run in revert mode", "the trough again"]
    assert {entry["like"] for entry in repeated} == {kept.sha7}, "the card each one repeated"
    assert all(entry["pct"] >= 97.0 for entry in repeated)


def test_both_kinds_of_book_share_one_window_and_the_newest_ten_win_it(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """One window, not one each, and `repeat` says which kind an entry is.

    They are one fact to the proposer — this book has been held — so ten books is ten
    books and a second window would be a longer prompt rather than a better one. What that
    costs was measured: on the live workspace of 2026-09-18, 106 of 3,092 refusals are the
    second kind, and because they arrive in runs, 506 turns would have been shown a repeat
    they were not.
    """
    active = open_run(store, prepared_hyp)
    kinds = [research_loop.REDUNDANT, research_loop.SAME_BOOK]
    for n in range(driver.REDUNDANT_LINES + 2):
        store.event(
            kinds[n % 2],
            prepared_hyp,
            {"run_id": active.run_id, "like": "a" * 7, "pct": 99.0, "desc": f"book {n}"},
        )

    shown = driver._redundant_in(store, active)

    assert [entry["desc"] for entry in shown] == [
        f"book {n}" for n in reversed(range(2, driver.REDUNDANT_LINES + 2))
    ], "the newest ten of both kinds, newest first"
    assert [entry["repeat"] for entry in shown] == [
        n % 2 == 0 for n in reversed(range(2, driver.REDUNDANT_LINES + 2))
    ], "each says which kind it is, and neither kind has a window of its own"


def test_a_book_measured_under_another_reading_still_says_the_idea_was_tried(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The window is the pins and not the reading, which is a fact about a card.

    An entry is not an anchor — `records.matched_book` picks those under the reading the
    asking card was measured with — it is the record that an idea has been tried, and an
    idea tried under four folds was tried when the next run counts three.
    """
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("revert"), proposal("revert", desc="the trough again")])
    assert driver.run(workspace, store, prepared_hyp).redundant
    reset_mock()

    other = tuned(ws, stall_k=2, folds=3)
    scripted(other, propose=[proposal("weak")])
    driver.run(other, store, prepared_hyp, cards=1)

    readings = {
        row[0]
        for row in store.connection.execute(
            "SELECT DISTINCT measured_under FROM signatures WHERE hyp_id = ?", (prepared_hyp,)
        )
    }
    assert len(readings) == 2, "the second run counts three folds, so its cards read another way"
    told = json.loads(recorded.of("propose")[-1].user)["redundant"]
    assert "the trough again" in [entry["desc"] for entry in told]


def test_a_description_that_is_not_one_line_is_corrected_on_the_ladder(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    bad = {**proposal("revert"), "desc": "two\nlines"}
    scripted(ws, propose=[bad, proposal("revert", desc="trade the trough")])

    driver.run(ws, store, prepared_hyp, cards=1)

    assert records.cards_of(store, prepared_hyp)[-1].desc == "trade the trough"


def test_a_proposer_that_never_fits_fails_the_step_after_the_whole_ladder(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scripted(ws, propose=[{"desc": "never fits", "diff": NOWHERE, "tags": ["refactor"]}])

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


def test_a_driver_told_to_stop_asks_the_model_nothing(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """A lane that was told to stop between two turns begins no proposal."""
    scripted(ws, propose=[proposal("revert")])

    outcome = driver.run(ws, store, prepared_hyp, cards=1, stop=lambda: True)

    assert (outcome.reason, outcome.proposed, outcome.ended) == ("stopped", 0, False)
    assert spend(store, lane="op").calls == 0
    assert statuses(store, prepared_hyp) == ["keep"], "the baseline, and no card"
    assert records.active(store, prepared_hyp) is not None


def test_a_stop_that_lands_during_a_proposal_drops_it_before_its_card(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """The answer in hand is not carded and not written: the lane is left as it was."""
    scripted(ws, propose=[proposal("revert")])
    asked = iter([False, True])
    driver.run(ws, store, prepared_hyp, cards=0)
    run = records.active(store, prepared_hyp)
    assert run is not None
    held = (ws.root / run.dir / STRATEGY_FILE).read_bytes()

    outcome = driver.run(ws, store, prepared_hyp, cards=1, stop=lambda: next(asked))

    assert (outcome.reason, outcome.proposed) == ("stopped", 0)
    assert spend(store, lane="op").calls == 1, "the proposal was asked for, and then dropped"
    assert statuses(store, prepared_hyp) == ["keep"]
    assert (ws.root / run.dir / STRATEGY_FILE).read_bytes() == held


def test_a_stop_that_lands_during_a_card_skips_the_alignment_check_it_made_due(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """The check is owed to the run, which is asked again at its next turn."""
    workspace = tuned(ws, align_every=1)
    scripted(workspace, propose=[proposal("revert")])
    asked = iter([False, False, True])

    outcome = driver.run(workspace, store, prepared_hyp, cards=1, stop=lambda: next(asked))

    assert (outcome.proposed, outcome.checks) == (1, 0)
    assert statuses(store, prepared_hyp) == ["keep", "keep"]
    assert spend(store, lane="op").calls == 1, "no alignment call was made"


def test_a_stall_ends_the_run_and_requeues_the_hypothesis(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("revert"), proposal("boom"), proposal("boom")])

    outcome = driver.run(workspace, store, prepared_hyp)

    assert outcome.reason == "stalled"
    assert (outcome.proposed, outcome.keeps, outcome.crashes, outcome.ended) == (3, 1, 2, True)
    assert records.active(store, prepared_hyp) is None
    assert not lanes.lane_dir(workspace, "op", prepared_hyp).exists()
    assert [item.hyp_id for item in scheduler.queued(store)] == [prepared_hyp]
    assert scheduler.queued(store)[0].priority == scheduler.STALL_PRIORITY
    # The reverting keep scored above zero, so the stall certified it on the way out.
    assert outcome.best_metric is not None and outcome.best_metric > 0
    assert certificate.latest(store, prepared_hyp) is not None


def test_a_stall_on_a_baseline_that_earned_nothing_certifies_nothing(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """The flat seed keeps at zero, and a stall on it has nothing to prove out of sample."""
    workspace = tuned(ws, stall_k=2)
    scripted(workspace, propose=[proposal("boom")])

    outcome = driver.run(workspace, store, prepared_hyp)

    assert outcome.reason == "stalled"
    assert (outcome.proposed, outcome.crashes, outcome.ended) == (2, 2, True)
    assert outcome.best_metric is not None and outcome.best_metric <= 0
    assert certificate.latest(store, prepared_hyp) is None
    assert [item.hyp_id for item in scheduler.queued(store)] == [prepared_hyp]
    assert spend(store, lane="op").calls == 2, "two proposals and no certification plan"


def test_a_stall_whose_certification_cannot_run_leaves_the_hypothesis_owed_to_the_queue(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """The planner's tier answers nothing usable, so on_stall raises; the lane still holds it."""
    workspace = tuned(ws, stall_k=2)
    scripted(
        workspace,
        propose=[proposal("revert"), proposal("boom"), proposal("boom")],
        certify_plan=[{}],
    )

    with pytest.raises(PreconditionError, match="certify_plan"):
        driver.run(workspace, store, prepared_hyp, lane="l1")

    assert records.active(store, prepared_hyp) is None
    assert scheduler.claimed(store, prepared_hyp)
    put = scheduler.put_back(store, prepared_hyp)
    assert put is not None and put.priority == scheduler.BASELINE_PRIORITY


def test_a_lane_whose_hypothesis_was_taken_out_does_not_begin_its_run(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    scheduler.enqueue(store, prepared_hyp)
    assert scheduler.dequeue(store, "l1") == prepared_hyp
    assert scheduler.remove(store, prepared_hyp) == "lane"

    with pytest.raises(PreconditionError, match="taken out of the queue while lane l1 held it"):
        driver.run(ws, store, prepared_hyp, cards=1, lane="l1")

    assert records.active(store, prepared_hyp) is None
    assert not lanes.lane_dir(ws, "l1", prepared_hyp).exists()
    assert scheduler.put_back(store, prepared_hyp) is None
    # By hand, in the interactive lane, the operator may still begin it.
    assert driver.run(ws, store, prepared_hyp, cards=1).proposed == 1


def test_a_removal_that_lands_during_the_baseline_is_honoured_before_the_run_exists(
    ws: Workspace, store: StateStore, prepared_hyp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.enqueue(store, prepared_hyp)
    assert scheduler.dequeue(store, "l1") == prepared_hyp
    real = research_loop._baseline

    def removing(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        assert scheduler.remove(store, prepared_hyp) == "lane"
        return result

    monkeypatch.setattr(research_loop, "_baseline", removing)

    with pytest.raises(research_loop.TakenError, match="taken out of the queue"):
        driver.run(ws, store, prepared_hyp, cards=1, lane="l1")

    assert records.active(store, prepared_hyp) is None
    assert not lanes.lane_dir(ws, "l1", prepared_hyp).exists()
    assert records.cards_of(store, prepared_hyp) == []
    kinds = [event.kind for event in store.events(subject=prepared_hyp)]
    assert research_loop.BASELINE_FAILED not in kinds, "the operator's word, not a failure"


def test_a_run_ended_while_the_model_answers_is_let_go_and_nothing_is_written(
    ws: Workspace, store: StateStore, prepared_hyp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`research end` lands while the proposer is answering: the answer is dropped before it
    is written into the lane directory the end removed, and no card is recorded for it.

    Writing it first put the directory back, and the card then refused the run as having
    none, which a daemon lane recorded as its failure and backed off from."""
    scripted(ws, propose=[proposal("revert")])
    run = research_loop.begin(ws, store, prepared_hyp, lane="l1")
    cards = records.cards_of(store, prepared_hyp)
    propose = driver._propose

    def ending(*args: Any, **kwargs: Any) -> Any:
        answer = propose(*args, **kwargs)
        research_loop.end(ws, store, prepared_hyp)
        return answer

    monkeypatch.setattr(driver, "_propose", ending)

    with pytest.raises(research_loop.TakenError, match="was ended while lane l1 worked it"):
        driver.run(ws, store, prepared_hyp, cards=1, lane="l1")

    assert records.cards_of(store, prepared_hyp) == cards
    assert not (ws.root / run.dir).exists(), "the answer put back the directory `end` removed"
    assert research_loop._HOST_RUNS.get(run.run_id) is None


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


def test_every_card_under_the_pins_reaches_the_proposer_as_coverage_by_tag(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The recent cards are a window; the coverage is the whole search, keyed by what
    each proposal said it was, so a corner already searched reads as searched."""
    scripted(
        ws,
        propose=[
            proposal("revert", tags=["signal_mean_reversion", "exit_signal"]),
            proposal("weak", tags=["signal_mean_reversion"]),
            proposal("boom", tags=["sizing_volatility"]),
        ],
    )

    driver.run(ws, store, prepared_hyp, cards=3)

    cards = records.cards_of(store, prepared_hyp)
    assert [card.tags for card in cards] == [
        [],
        ["signal_mean_reversion", "exit_signal"],
        ["signal_mean_reversion"],
        ["sizing_volatility"],
    ]
    first, _, third = recorded.of("propose")
    assert '"coverage": {}' in first.user, "the baseline carries no tags"
    coverage = driver.coverage(store, records.require_active(store, prepared_hyp))
    assert coverage == {
        "exit_signal": {
            "count": 1,
            "best_metric": cards[1].metric,
            "best_status": "keep",
            "newest": cards[1].sha7,
        },
        "signal_mean_reversion": {
            "count": 2,
            "best_metric": cards[1].metric,
            "best_status": "keep",
            "newest": cards[2].sha7,
        },
        "sizing_volatility": {
            "count": 1,
            "best_metric": 0.0,
            "best_status": "crash",
            "newest": cards[3].sha7,
        },
    }
    assert '"signal_mean_reversion": {' in third.user
    assert '"count": 2' in third.user


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


def test_the_drifts_this_run_was_rewound_for_reach_the_next_proposal(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """The rewound file says nothing about the direction the check refused; this does."""
    workspace = tuned(ws, align_every=1)
    scripted(workspace, propose=CYCLE, align_check=[DRIFTED, ALIGNED])

    driver.run(workspace, store, prepared_hyp, cards=3)

    proposals = recorded.of("propose")
    assert "rewound_for" not in proposals[0].user, "nothing has been rewound yet"
    assert "rewound_for" in proposals[1].user
    assert DRIFTED["reason"] in proposals[1].user
    assert proposals[2].user.count(str(DRIFTED["reason"])) == 1, "the aligned check adds none"


def test_a_run_whose_lane_never_leaves_its_base_puts_nothing_to_the_alignment_model(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """What a live daemon did ten times in under two hours, with the model scripted to
    answer as it answered there. Nothing keeps, so every check finds the lane back on the
    bytes the run was handed; asked about them, the model judged the seed against the
    thesis, and each check "rewound" the run onto the bytes it was already on, told the
    proposer so, and escalated."""
    workspace = tuned(ws, align_every=1)
    scripted(workspace, propose=[proposal("boom")], align_check=[DRIFTED])

    outcome = driver.run(workspace, store, prepared_hyp, cards=3)

    assert (outcome.crashes, outcome.checks, outcome.drifts) == (3, 3, 0)
    assert recorded.of("align_check") == []
    assert inbox.unread(store) == []
    assert all("rewound_for" not in call.user for call in recorded.of("propose"))


def test_only_this_run_s_rewinds_reach_it_and_only_the_newest_few(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    scripted(ws, propose=[proposal("revert")])
    active = records.active(store, prepared_hyp)
    driver.run(ws, store, prepared_hyp, cards=1)
    active = records.active(store, prepared_hyp)
    assert active is not None
    for n in range(driver.DRIFT_LINES + 1):
        store.event(
            align.DRIFTED,
            prepared_hyp,
            {"run_id": active.run_id, "cards": n, "sha": "a" * 64, "reason": f"drift {n}"},
        )
    store.event(
        align.DRIFTED,
        prepared_hyp,
        {"run_id": "another", "cards": 0, "sha": "a" * 64, "reason": "another run's drift"},
    )
    reset_mock()
    scripted(ws, propose=[proposal("weak")])

    driver.run(ws, store, prepared_hyp, cards=1)

    user = recorded.of("propose")[-1].user
    assert "another run's drift" not in user
    assert "drift 0" not in user, "older than the newest few"
    for n in range(1, driver.DRIFT_LINES + 1):
        assert f"drift {n}" in user


def test_the_gate_that_refused_a_card_reaches_the_next_proposal(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A card discarded before any backtest ran must tell the proposer what it did wrong.

    `strategy_integrity` refuses statically, so the card carries a metric of zero and no
    traceback; without the gate's own evidence the proposer sees a discard with no reason
    and writes the same line again.
    """
    scripted(
        ws, propose=[{"desc": "reach for the cache", "diff": DENIED, "tags": ["refactor"]}, *CYCLE]
    )

    driver.run(ws, store, prepared_hyp, cards=2)

    user = recorded.of("propose")[1].user
    assert "discard" in statuses(store, prepared_hyp)
    assert "failed_gates" in user
    assert "strategy_integrity" in user
    assert "attribute '.cache' is denied" in user


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


def test_gate_results_that_are_not_a_list_of_gates_read_as_none() -> None:
    """A column that does not parse, or parses to something other than a list, holds no
    gates; an entry of a list that is not a mapping is left out."""
    assert driver._gates(None) == []
    assert driver._gates("not json") == []
    assert driver._gates(json.dumps({"id": "max_drawdown"})) == []
    assert driver._gates(json.dumps([{"id": "max_drawdown", "pass": True}, 7])) == [
        {"id": "max_drawdown", "pass": True}
    ]


def test_a_failed_edge_per_trade_withholds_the_session_score(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A higher session score from fewer losing trades is not a result the next proposal sees.

    The contribution moves toward zero as losing trades are dropped. The card is shown by
    its per-trade edge, and that card does not become its tag's best metric.
    """
    scripted(ws, propose=[proposal("revert", desc="fewer trades", tags=["filter_liquidity"])])
    driver.run(ws, store, prepared_hyp, cards=1)
    gates = json.dumps(
        [
            {
                "id": "edge_per_trade",
                "pass": False,
                "evidence": {"net_bps_per_trade": -4.1, "se": 0.2, "n_trades": 100},
            }
        ]
    )
    store.connection.execute(
        "UPDATE cards SET metric = ?, metric_se = ?, gate_results = ? WHERE description = ?",
        (-1.0, 0.4, gates, "fewer trades"),
    )
    active = records.require_active(store, prepared_hyp)
    summary = driver._summary(
        store.connection.execute(
            "SELECT strategy_sha, status, metric, metric_se, description, n_trades, digest,"
            " gate_results FROM cards WHERE description = ?",
            ("fewer trades",),
        ).fetchone()
    )
    assert "metric" not in summary
    assert summary["failed_gates"][0]["id"] == "edge_per_trade"
    assert driver.coverage(store, active)["filter_liquidity"]["best_metric"] is None
    shown = json.dumps(driver._dynamic(ws, store, active, b"", "", 0)["recent_cards"])
    assert "net_bps_per_trade" in shown
    assert "-4.1" not in shown or "net_bps_per_trade" in shown
    assert '"metric": -1.0' not in shown and '"metric": -1' not in shown
    assert "is withheld" in recorded.of("propose")[-1].system


def _batch(ws: Workspace, proposals: list[dict[str, Any]]) -> Workspace:
    workspace = tuned(ws, cards_per_turn=3)
    scripted(workspace, propose=[{"proposals": proposals}])
    return workspace


def test_a_batch_keeps_the_best_slot_and_counts_every_card(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    """One call, three cards, one turn: the keep is the best against the pre-turn best."""
    ws = _batch(ws, [proposal("revert"), proposal("boom"), proposal("weak")])

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert (outcome.keeps, outcome.crashes, outcome.discards) == (1, 1, 1)
    assert outcome.missed == 0
    assert records.n_trials(store, prepared_hyp) == 4
    assert statuses(store, prepared_hyp) == ["keep", "keep", "crash", "discard"]
    # Every slot that ran keeps what it was scored from, the slot the turn kept and the one
    # it did not alike; the crash keeps nothing.
    baseline, kept, crashed, discarded = records.cards_of(store, prepared_hyp)
    for ran in (baseline, kept, discarded):
        measured_as_scored(store, ran)
    assert crashed.measured_sha is None
    assert spend(store, lane="op").calls == 1
    assert records.active(store, prepared_hyp) is not None


def test_the_next_turn_is_shown_what_a_batch_s_cards_traded(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """Every card of a turn that ran reaches the next proposal with its digest, as one card
    does, so a batch that scored better by trading less reads as that."""
    ws = tuned(ws, cards_per_turn=2)
    scripted(
        ws,
        propose=[
            {"proposals": [proposal("revert"), proposal("weak")]},
            {"proposals": [proposal("aaa")]},
        ],
    )

    driver.run(ws, store, prepared_hyp, cards=2)

    first, second = recorded.of("propose")
    assert first.max_output == second.max_output == 2 * ROUTING_DEFAULTS["propose"].max_output
    shown = json.loads(second.user)["recent_cards"]
    assert [card["status"] for card in shown] == ["keep", "keep", "discard"]
    assert all(isinstance(card.get("digest"), dict) for card in shown)
    assert all(card.digest is not None for card in records.cards_of(store, prepared_hyp))


def test_a_turn_s_second_keep_is_a_runner_up_and_its_corner_does_not_read_as_searched(
    ws: Workspace, store: StateStore, prepared_hyp: str, recorded: Recorder
) -> None:
    """A turn keeps one card. Another that beat the best the turn began from is a
    `runner_up`, in the store, in the next proposal's recent cards and as its tag's best
    status. Before this it was written `discard`, so the condition it tested read as a
    corner searched and found wanting (assessment N-10-3)."""
    ws = tuned(ws, cards_per_turn=2)
    scripted(
        ws,
        propose=[
            {
                "proposals": [
                    proposal("revert", tags=["signal_mean_reversion"]),
                    proposal("revert", tagged=False, tags=["exit_target"]),
                ]
            },
            {"proposals": [proposal("weak")]},
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=2)

    baseline, kept, runner_up = records.cards_of(store, prepared_hyp)[:3]
    assert (kept.status, runner_up.status) == ("keep", "runner_up")
    assert runner_up.metric > baseline.metric, "it beat the best its turn began from"
    assert records.best_of(store, prepared_hyp)[0] == kept.strategy_sha
    assert outcome.runner_ups == 1
    assert outcome.payload()["runner_ups"] == 1
    facts = json.loads(recorded.of("propose")[1].user)
    assert [card["status"] for card in facts["recent_cards"]] == ["keep", "keep", "runner_up"]
    assert facts["coverage"]["exit_target"]["best_status"] == "runner_up"
    assert "`runner_up`" in recorded.of("propose")[1].system


def test_a_batch_with_no_keep_is_one_miss_and_three_trials(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = _batch(ws, [proposal("aaa"), proposal("bbb"), proposal("ccc")])

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert outcome.keeps == 0
    assert outcome.discards + outcome.redundant == 3
    assert records.n_trials(store, prepared_hyp) == 4
    assert driver._trailing_non_keeps(store, records.require_active(store, prepared_hyp)) == 1


def test_two_slots_with_the_same_bytes_are_one_card(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = _batch(
        ws,
        [proposal("revert", tagged=False), proposal("revert", tagged=False), proposal("weak")],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.proposed == 1
    assert (outcome.keeps, outcome.discards) == (1, 1)
    assert records.n_trials(store, prepared_hyp) == 3


def test_a_crashed_slot_is_repaired_alone_on_the_next_turn(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=3)
    scripted(
        ws,
        propose=[
            {"proposals": [proposal("revert"), proposal("boom"), proposal("weak")]},
            proposal("flat"),
            {"proposals": [proposal("aaa")]},
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=3)

    assert outcome.proposed == 3
    assert outcome.crashes == 1
    assert records.n_trials(store, prepared_hyp) == 6
    assert statuses(store, prepared_hyp).count("crash") == 1
    assert spend(store, lane="op").calls == 3


def test_a_batch_with_two_keeps_keeps_the_higher_and_one_miss_stalls(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=3, stall_k=1)
    scripted(ws, propose=[{"proposals": [proposal("aaa"), proposal("bbb"), proposal("ccc")]}])

    outcome = driver.run(ws, store, prepared_hyp, cards=3)

    assert outcome.ended is True
    assert outcome.reason == "stalled"
    assert outcome.proposed == 1


def test_a_batch_discards_a_slot_the_static_gate_refuses_and_keeps_another(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = _batch(
        ws,
        [
            {"desc": "peek at the cache", "diff": DENIED, "tags": ["refactor"]},
            proposal("revert", tagged=False),
            proposal("revert"),
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.keeps == 1
    assert outcome.discards >= 1
    assert records.n_trials(store, prepared_hyp) == 4


def test_a_batch_drops_slots_that_do_not_apply_and_a_repeat_is_a_miss(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=3, stall_k=5)
    scripted(
        ws,
        propose=[
            {
                "proposals": [
                    {"desc": "bad\nline", "diff": NOWHERE, "tags": ["refactor"]},
                    {"desc": "untouched", "diff": NOOP, "tags": ["refactor"]},
                    proposal("weak", tagged=False),
                ]
            },
            {"proposals": [proposal("weak", tagged=False)]},
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=2)

    assert outcome.missed == 1
    assert outcome.discards == 1


def test_a_structural_batch_drops_a_change_that_moves_no_structure(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=2, local_cards=1, structural_cards=5, stall_k=5)
    scripted(
        ws,
        propose=[
            {"proposals": [proposal("aaa")]},
            {"proposals": [PARAMETER, proposal("revert")]},
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=2)

    assert outcome.keeps == 1
    assert outcome.discards == 1


def test_a_stop_during_a_batch_drops_the_answer(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=2)
    scripted(ws, propose=[{"proposals": [proposal("revert")]}])
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    outcome = driver.run(ws, store, prepared_hyp, cards=2, stop=stop)

    assert outcome.reason == "stopped"
    assert outcome.proposed == 0


def test_a_batch_checks_alignment_when_the_cadence_falls_due(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=2, align_every=1)
    scripted(ws, propose=[{"proposals": [proposal("aaa")]}], align_check=[ALIGNED])

    outcome = driver.run(ws, store, prepared_hyp, cards=1)

    assert outcome.checks == 1


def test_a_batch_without_a_crash_does_not_owe_a_repair(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=2)
    scripted(ws, propose=[{"proposals": [proposal("aaa")]}, {"proposals": [proposal("bbb")]}])

    driver.run(ws, store, prepared_hyp, cards=2)
    active = records.require_active(store, prepared_hyp)
    assert driver._batched_repair(store, active, b"") is None
    store.event(
        "batched",
        prepared_hyp,
        {"run_id": "other", "max_seq": 1, "n": 1, "crash_sha": active.base_sha},
    )
    assert driver._trailing_non_keeps(store, active) >= 0
    store.event(
        "batched",
        prepared_hyp,
        {"run_id": active.run_id, "max_seq": 1, "n": 1, "crash_sha": active.base_sha},
    )
    assert driver._batched_repair(store, active, b"x = 1\n") is None
    store.event(
        "batched",
        prepared_hyp,
        {"run_id": active.run_id, "max_seq": 0, "n": 1, "crash_sha": None},
    )
    assert driver._trailing_non_keeps(store, active) >= 0
    store.event(
        "batched",
        prepared_hyp,
        {"run_id": active.run_id, "max_seq": 10**9, "n": 1, "crash_sha": "not-a-card"},
    )
    assert driver._batched_repair(store, active, b"x = 1\n") is None


def test_a_batch_miss_at_the_stall_ends_the_run(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=2, stall_k=2)
    scripted(
        ws,
        propose=[
            {"proposals": [proposal("weak", tagged=False)]},
            {"proposals": [proposal("weak", tagged=False)]},
        ],
    )

    outcome = driver.run(ws, store, prepared_hyp, cards=3)

    assert outcome.reason == "stalled"
    assert outcome.missed == 1


def test_a_batch_that_never_applies_fails_the_step(
    ws: Workspace, store: StateStore, prepared_hyp: str
) -> None:
    ws = tuned(ws, cards_per_turn=2)
    scripted(
        ws,
        propose=[{"proposals": [{"desc": "never fits", "diff": NOWHERE, "tags": ["refactor"]}]}],
    )

    with pytest.raises(PreconditionError, match="in 3 attempts"):
        driver.run(ws, store, prepared_hyp, cards=1)
