"""begin, card and end over synthetic data: the whole loop, end to end."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import fields, replace
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from kanso.cli import doctor
from kanso.criteria import SCOPED_FILES
from kanso.criteria.objectives import wf_sharpe_net
from kanso.data import snapshot
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import show
from kanso.nautilus import backtest
from kanso.research import loop, passages, records, scheduler
from kanso.research.results import results_file, results_tsv
from kanso.schemas import Hypothesis, RunRecord
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import (
    DOCUMENT,
    ENVELOPE,
    FLAT,
    HYP_ID,
    PROGRAM,
    RAISING,
    READING,
    RESEARCH,
    REVERTING,
    WEAK,
    classify,
    document,
    load_december,
    write_days,
    write_envelope,
    write_hypothesis,
)
from .mocked import tuned


def lane_of(ws: Workspace, run: RunRecord) -> Path:
    """The lane directory of a run, as an absolute path."""
    return ws.root / run.dir


def edit(ws: Workspace, run: RunRecord, source: bytes) -> Path:
    """What a researcher does between two cards."""
    path = lane_of(ws, run) / "strategy.py"
    path.write_bytes(source)
    return path


def statuses(store: StateStore, hyp_id: str = HYP_ID) -> list[str]:
    return [card.status for card in records.cards_of(store, hyp_id)]


def champion(ws: Workspace, store: StateStore, hyp_id: str) -> str | None:
    """The hypothesis's best sha, asserted to be what the workspace file hashes to."""
    held = sha256(ws.path("hypotheses", hyp_id, "strategy.py").read_bytes()).hexdigest()
    best, _ = records.best_of(store, hyp_id)
    assert held == best, f"workspace strategy.py is {held[:7]}, best is {str(best)[:7]}"
    return best


def best_run_id(store: StateStore, hyp_id: str) -> str | None:
    row = store.connection.execute(
        "SELECT best_run_id FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return None if row["best_run_id"] is None else str(row["best_run_id"])


# --- begin -------------------------------------------------------------------


def test_begin_pins_the_scope_copies_it_and_runs_the_baseline(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)

    assert run.lane == "op"
    assert run.dir == f"runs/op/{registered}"
    assert run.tag.endswith("-1")
    assert run.base_sha == sha256(FLAT).hexdigest()
    assert run.program_sha == sha256(PROGRAM).hexdigest()
    assert (
        run.hypothesis_sha
        == sha256(ws.path("hypotheses", registered, "hypothesis.yaml").read_bytes()).hexdigest()
    )
    assert run.card_budget_s == max(loop.MIN_CARD_BUDGET_S, loop.HEADROOM * run.baseline_wall_s)
    assert run.baseline_wall_s > 0.0

    directory = lane_of(ws, run)
    assert sorted(child.name for child in directory.iterdir()) == sorted(SCOPED_FILES)
    assert (directory / "hypothesis.yaml").read_bytes() == store.get_blob(run.hypothesis_sha)
    assert (directory / "program.md").read_bytes() == store.get_blob(run.program_sha)
    assert (directory / "strategy.py").read_bytes() == FLAT

    baseline = records.cards_of(store, registered)[0]
    assert (baseline.status, baseline.metric, baseline.n_trades) == ("keep", 0.0, 0)
    assert baseline.desc == loop.BASELINE
    assert baseline.n_trials == 1
    assert run.best_sha == baseline.strategy_sha
    assert show(ws, store, registered).status == "researching"  # type: ignore[union-attr]
    assert (
        results_file(ws, registered)
        .read_text(encoding="utf-8")
        .splitlines()[1]
        .startswith(baseline.sha7)
    )


def test_the_baseline_discards_when_a_card_gate_fails(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(
        ws,
        store,
        document(
            constraints=[{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 4}}]
        ),
    )
    run = loop.begin(ws, store, hyp_id)

    baseline = records.cards_of(store, hyp_id)[0]
    assert baseline.status == "discard"
    assert [gate.id for gate in baseline.gate_results] == ["strategy_integrity", "min_trades"]
    assert run.best_sha is None


def test_a_baseline_that_raises_leaves_no_run_and_no_lane_directory(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, RAISING)
    with pytest.raises(PreconditionError, match="baseline card of demo_mr did not run") as caught:
        loop.begin(ws, store, hyp_id)
    assert records.active(store, hyp_id) is None
    assert not ws.path("runs", "op", hyp_id).exists()
    assert [event.kind for event in store.events(subject=hyp_id)][-1] == loop.BASELINE_FAILED
    # The strategy raised, so the strategy is what there is to fix; a failure that named
    # no remedy of its own is the only case this one answers for. The run started from
    # the workspace file, so beginning again is enough.
    assert "strategy.py" in (caught.value.remedy or "")
    assert "--from-workspace" not in (caught.value.remedy or "")


def test_a_baseline_taken_from_the_best_that_fails_names_from_workspace(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """Beginning again would take the best blob again, so the refusal says how not to."""
    run = loop.begin(ws, store, registered)
    loop.end(ws, store, registered)
    records.set_best(store, run, store.put_blob(RAISING), 9.0)

    with pytest.raises(PreconditionError, match="baseline card of demo_mr did not run") as caught:
        loop.begin(ws, store, registered)

    assert f"kanso research begin {registered} --from-workspace" in (caught.value.remedy or "")
    assert records.active(store, registered) is None


def test_a_baseline_whose_rows_are_gone_carries_the_data_remedy(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """A parquet deleted by hand is a data fault, and the refusal has to say so.

    The manifests still describe the dataset, so the loss surfaces only when the runner
    reads the window — which happens inside the card's own process. Asserted here is that
    the remedy the runner raised crossed that boundary: an operator whose rows are missing
    is sent to load them, not to debug a strategy that never ran.
    """
    for parquet in ws.path("catalog", "data", "bar").rglob("*.parquet"):
        parquet.unlink()

    with pytest.raises(PreconditionError) as caught:
        loop.begin(ws, store, registered)

    assert "the catalog holds nothing" in caught.value.message
    assert "kanso data load" in (caught.value.remedy or "")
    assert "strategy.py" not in (caught.value.remedy or "")


def test_a_baseline_that_reaches_outside_the_lane_is_never_executed(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, READING)
    with pytest.raises(PreconditionError, match="denied identifier"):
        loop.begin(ws, store, hyp_id)
    assert records.active(store, hyp_id) is None


def test_the_second_run_of_a_day_is_the_second_tag(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    first = loop.begin(ws, store, registered)
    loop.end(ws, store, registered)
    second = loop.begin(ws, store, registered)
    assert first.tag.endswith("-1")
    assert second.tag.endswith("-2")
    assert second.tag[:8] == first.tag[:8]


def test_a_named_tag_is_taken_as_given(ws: Workspace, store: StateStore, registered: str) -> None:
    assert loop.begin(ws, store, registered, tag="20240101-7").tag == "20240101-7"


def test_a_second_run_of_one_hypothesis_is_refused(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    loop.begin(ws, store, registered)
    with pytest.raises(PreconditionError, match="already has an active run"):
        loop.begin(ws, store, registered)


def test_an_unregistered_hypothesis_has_nothing_to_research(
    ws: Workspace, store: StateStore
) -> None:
    with pytest.raises(PreconditionError, match="not a registered hypothesis"):
        loop.begin(ws, store, "nobody_here")


def test_a_draft_is_not_researchable(ws: Workspace, store: StateStore) -> None:
    from kanso.hyp import add as register

    unclassified = {**DOCUMENT, "construct": None, "objective": None, "constraints": None}
    register(ws, store, write_hypothesis(ws, unclassified))
    with pytest.raises(PreconditionError, match="is draft"):
        loop.begin(ws, store, HYP_ID)


def test_a_workspace_file_that_left_its_pin_is_refused(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    path = ws.path("hypotheses", registered, "hypothesis.yaml")
    path.write_bytes(path.read_bytes() + b"\n# edited\n")
    with pytest.raises(PreconditionError, match="is not the file demo_mr is registered under"):
        loop.begin(ws, store, registered)


def test_a_workspace_without_an_envelope_has_no_lane_share(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    ws.path("envelope.yaml").unlink()
    with pytest.raises(PreconditionError, match="no envelope"):
        loop.begin(ws, store, registered)


def test_a_hypothesis_no_snapshot_covers_cannot_begin(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    shutil.rmtree(ws.path("catalog", "snapshots"))
    with pytest.raises(PreconditionError, match="no snapshot covers"):
        loop.begin(ws, store, registered)


def test_a_run_pins_the_program_it_follows(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    ws.path("hypotheses", registered, "program.md").unlink()
    with pytest.raises(PreconditionError, match="program.md is missing"):
        loop.begin(ws, store, registered)


def test_a_run_starts_from_a_strategy(ws: Workspace, store: StateStore, registered: str) -> None:
    ws.path("hypotheses", registered, "strategy.py").unlink()
    with pytest.raises(PreconditionError, match="strategy.py is missing"):
        loop.begin(ws, store, registered)


# --- the card sequence -------------------------------------------------------


def test_a_keep_beats_the_noise_floor_and_a_repeat_of_it_does_not(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "buy the trough, sell the peak")

    assert kept.status == "keep"
    assert kept.metric > 0.0
    assert kept.n_trades > 0
    assert kept.n_trials == 2
    assert ws.path("hypotheses", registered, "strategy.py").read_bytes() == REVERTING
    assert records.best_of(store, registered) == (kept.strategy_sha, kept.metric)

    with pytest.raises(loop.RedundantError, match="result is already known") as refused:
        loop.card(ws, store, registered, "the very same file")
    assert kept.sha7 in refused.value.message
    assert f"--sha {kept.sha7}" in str(refused.value.remedy)
    assert records.best_of(store, registered) == (kept.strategy_sha, kept.metric)
    # The backtest ran and the number came back, so the card is written and counted; what
    # the refusal denies is that it was a new experiment, not that it was measured.
    again = records.cards_of(store, registered)[-1]
    assert (again.status, again.metric) == ("redundant", kept.metric)
    assert records.n_trials(store, registered) == 3
    assert records.trial_metrics(store, registered) == [kept.metric, again.metric]
    (event,) = store.events(kind=loop.REDUNDANT, subject=registered)
    assert event.detail["like"] == kept.sha7
    assert event.detail["pct"] == 100.0
    assert event.detail["metric"] == kept.metric, "the same snapshot and code give the same number"
    # Both halves of the premise are recorded, because both halves are what refused it: a
    # reader told only that two books matched could not tell this from the refusal kanso
    # used to make on a book alone.
    assert event.detail["like_metric"] == kept.metric
    assert event.detail["floor"] > 0.0
    assert f"{event.detail['floor']:.6g}" in refused.value.message
    assert f"{event.detail['floor']:.6g}" in str(refused.value.remedy)


def test_a_spelling_that_holds_the_same_book_is_redundant_and_the_lane_is_restored(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The signature reads what a run held, not how the file that held it was written."""
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    respelt = REVERTING.replace(b"self.long = False", b"self.long = bool(0)")
    assert respelt != REVERTING
    edit(ws, run, respelt)

    with pytest.raises(loop.RedundantError):
        loop.card(ws, store, registered, "the same rule, spelt otherwise")

    assert (lane_of(ws, run) / "strategy.py").read_bytes() == REVERTING
    assert statuses(store) == ["keep", "keep", "redundant"]
    assert records.best_of(store, registered) == (kept.strategy_sha, kept.metric)
    (event,) = store.events(kind=loop.REDUNDANT, subject=registered)
    assert event.detail["like"] == kept.sha7
    assert event.detail["sessions"] == 31, "one session per day of the research window"
    assert event.detail["matched"] == 31
    # A redundant miss is judged, so what it held is stored too: the third spelling of an
    # idea is refused against the second as well as the first.
    stored = store.connection.execute(
        "SELECT sessions FROM signatures WHERE strategy_sha = ? AND hyp_id = ?"
        " AND hypothesis_sha = ? AND snapshot_id = ? AND criteria_version = ?",
        (
            sha256(respelt).hexdigest(),
            registered,
            run.hypothesis_sha,
            run.snapshot_id,
            run.criteria_version,
        ),
    ).fetchall()
    assert [row["sessions"] for row in stored] == [31]


def test_a_book_already_measured_whose_number_moved_is_a_discard_and_still_on_record(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The other side of the second clause: the book matched and the numbers did not.

    The stored anchor is moved a thousand away from what this data pays, which is the
    shape the live workspace of 2026-09-18 measured on an intraday hypothesis whose
    objective is a per-trade edge: one anchor, 289 matches, scores from -18.780 to 9.179
    around its own -4.5452. Such a candidate is not a repeat and is not refused -- and
    the fact that its book was already measured is still the one thing about it the next
    proposal can act on, so it is recorded under its own kind.
    """
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    store.connection.execute(
        "UPDATE signatures SET metric = metric + 1000 WHERE strategy_sha = ?",
        (kept.strategy_sha,),
    )
    edit(ws, run, REVERTING.replace(b"self.long = False", b"self.long = bool(0)"))

    made = loop.card(ws, store, registered, "the same rule, spelt otherwise")

    assert made.status == "discard", "an experiment, because the two numbers say so"
    assert store.events(kind=loop.REDUNDANT, subject=registered) == []
    (event,) = store.events(kind=loop.SAME_BOOK, subject=registered)
    assert event.detail["like"] == kept.sha7
    assert event.detail["matched"] == event.detail["sessions"] == 31
    assert event.detail["pct"] == 100.0
    assert event.detail["metric"] == made.metric
    assert event.detail["like_metric"] == kept.metric + 1000
    assert event.detail["desc"] == "the same rule, spelt otherwise"
    assert event.detail["floor"] < 1000, "which is why the two numbers are two results"


def _readings(store: StateStore, sha: str) -> list[tuple[float, str]]:
    """What the stored books for these bytes earned, and what each was measured under.

    A list because the reading is part of the key: the same bytes judged under a second
    reading are a second row and not a replacement.
    """
    return [
        (float(row["metric"]), str(row["measured_under"]))
        for row in store.connection.execute(
            "SELECT metric, measured_under FROM signatures WHERE strategy_sha = ? ORDER BY rowid",
            (sha,),
        )
    ]


def test_a_stored_number_is_measured_under_a_reading_the_four_pins_do_not_carry(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """`folds` moves a number with the hypothesis file, the snapshot and the criteria still.

    A signature is selected on four pins, and `[research] folds` is in none of them: it is
    a `kanso.toml` key, so it moves no hypothesis sha, no snapshot id, and no criteria
    version, which is this package's version and a digest of `criteria/library/*.yaml`.
    The same bytes over the same data measured over three folds instead of four earn a
    different number, so the reading is stored beside the number and it moves with it —
    beside the first reading's row and not over it, which is what leaves the four-fold
    anchor standing for the day the operator sets `folds` back.
    """
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    before = _readings(store, kept.strategy_sha)
    loop.end(ws, store, registered)

    other = tuned(ws, folds=3)
    resumed = loop.begin(other, store, registered)

    assert (resumed.hypothesis_sha, resumed.snapshot_id, resumed.criteria_version) == (
        run.hypothesis_sha,
        run.snapshot_id,
        run.criteria_version,
    ), "every pin the anchor is selected by stands still"
    after = _readings(store, kept.strategy_sha)
    assert len(before) == 1 and len(after) == 2, "a second reading is a second row"
    assert after[0] == before[0], "the four-fold anchor is where the four-fold run left it"
    assert after[1][0] != before[0][0], "and the same bytes over the same data earn another number"
    assert after[1][1] != before[0][1], "which is why the reading is stored, and why it moved"


def test_the_settings_the_digest_leaves_out_move_no_number(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """`annualisation`, `account` and `currency` look like a reading and are not.

    All three are `[research]` keys of the rendered template, two of them commented there
    as changing what a card is measured with, and this package reads none of them: a
    venue's account type and currency come from the broker's declaration, the operator's
    `venues.<MIC>` override and the shipped defaults, and no objective is passed an
    annualisation. So the same bytes over the same data score the same number under all
    three changed, and the digest does not name what changes nothing. Wiring any of them
    is what makes this fail, and the digest has to take it on the same day.
    """
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    before = _readings(store, kept.strategy_sha)
    loop.end(ws, store, registered)

    other = tuned(ws, annualisation=252, account='"cash"', currency='"EUR"')
    loop.begin(other, store, registered)

    assert _readings(store, kept.strategy_sha) == before


def test_the_keep_rule_is_asked_before_the_signature(
    ws: Workspace, store: StateStore, registered: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate that beats the best is a keep whatever it resembles: refusing it
    unmeasured would hide exactly the improvement a signature cannot see."""
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    loop.card(ws, store, registered, "the trough rule")
    monkeypatch.setattr(loop, "_keeps", lambda *_: True)
    edit(ws, run, REVERTING.replace(b"self.long = False", b"self.long = bool(0)"))

    again = loop.card(ws, store, registered, "the same book, judged better")

    assert again.status == "keep"
    assert store.events(kind=loop.REDUNDANT, subject=registered) == []


def test_the_share_of_sessions_that_makes_a_book_redundant_is_read_from_kanso_toml(
    ws: Workspace, store: StateStore, registered: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: WEAK holds nothing on 10 of the window's 31 sessions, as the flat baseline
    does on all 31 — a book matched at 30 percent and not at the template's 97.

    The same run signed at period ends alone matched on 17: seven of those sessions WEAK
    opened and closed a position in, and was flat only at the instant it was sampled.

    The floor is widened to admit any number, because the share is the clause under test
    and WEAK's own result is 23 floors from the baseline's, which the test below is for.
    """
    monkeypatch.setattr(loop, "_noise_floor", lambda *_: float("inf"))
    workspace = tuned(ws, redundant_pct=30)
    run = loop.begin(workspace, store, registered)
    edit(workspace, run, WEAK)

    with pytest.raises(loop.RedundantError):
        loop.card(workspace, store, registered, "buy any fall")

    (event,) = store.events(kind=loop.REDUNDANT, subject=registered)
    assert (event.detail["matched"], event.detail["sessions"]) == (10, 31)
    assert event.detail["like"] == run.base_sha[:7]


def test_a_book_already_held_that_earned_another_number_is_a_discard_not_a_repeat(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The matcher's premise, tested rather than asserted, on the run above with its own
    floor back: WEAK holds the baseline's book on 10 of 31 sessions and scores -0.061605
    against the baseline's 0.0, twenty-three times the 0.002631 that separates two results
    here. Its result is not already known, so it is a card of the ordinary kind — counted,
    coverable and shown to the next proposal as a change that was tried and measured.
    """
    workspace = tuned(ws, redundant_pct=30)
    run = loop.begin(workspace, store, registered)
    edit(workspace, run, WEAK)

    made = loop.card(workspace, store, registered, "buy any fall")

    assert made.status == "discard"
    assert store.events(kind=loop.REDUNDANT, subject=registered) == []
    setup = loop._setup(workspace, store, loop._pinned(workspace, store, run))
    floor = loop._noise_floor(setup, made.metric_se)
    assert abs(made.metric - 0.0) > 20 * floor, "the baseline is flat and this one is not"
    # The book did match at the configured share, so the share is not what let it through.
    books = {
        str(row["strategy_sha"]): json.loads(str(row["signature"]))
        for row in store.connection.execute("SELECT strategy_sha, signature FROM signatures")
    }
    base, candidate = books[run.base_sha], books[made.strategy_sha]
    shared = base.keys() & candidate.keys()
    assert (sum(1 for day in shared if base[day] == candidate[day]), len(shared)) == (10, 31)


def test_a_flat_strategy_repeats_the_flat_baseline_but_the_baseline_repeats_nothing(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """Holding nothing is a book too, and the baseline is exempt: it is the best blob of
    the last run, whose signature is already stored."""
    run = loop.begin(ws, store, registered)
    edit(ws, run, FLAT + b"# spelt otherwise\n")
    with pytest.raises(loop.RedundantError, match="already known"):
        loop.card(ws, store, registered, "still trades nothing")
    edit(ws, run, REVERTING)
    loop.card(ws, store, registered, "the trough rule")
    loop.end(ws, store, registered)

    resumed = loop.begin(ws, store, registered)

    assert resumed.base_sha == sha256(REVERTING).hexdigest()
    assert statuses(store) == ["keep", "redundant", "keep", "keep"], (
        "the flat respelling, then the trough rule, then the baseline of the second run"
    )
    assert len(store.events(kind=loop.REDUNDANT, subject=registered)) == 1


def test_a_baseline_that_discards_on_a_book_already_judged_is_a_card_not_a_redundant_miss(
    ws: Workspace, store: StateStore
) -> None:
    """The exemption the keep rule cannot stand in for: a baseline that fails a gate does not
    keep, and the flat book it holds was stored by the run before it. A run exists to
    climb from its base, so the base is always measured."""
    hyp_id = classify(
        ws,
        store,
        document(
            constraints=[{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 4}}]
        ),
    )
    loop.begin(ws, store, hyp_id)
    loop.end(ws, store, hyp_id)

    again = loop.begin(ws, store, hyp_id, from_workspace=True)

    assert again.base_sha == sha256(FLAT).hexdigest()
    assert statuses(store, hyp_id) == ["discard", "discard"]
    assert records.cards_of(store, hyp_id)[-1].run_id == again.run_id
    assert store.events(kind=loop.REDUNDANT, subject=hyp_id) == []


def test_a_card_that_is_worse_is_discarded_and_the_lane_is_restored(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    loop.card(ws, store, registered, "the trough rule")
    edit(ws, run, WEAK)
    worse = loop.card(ws, store, registered, "one step of memory")

    assert worse.status == "discard"
    assert worse.metric < 0.0
    assert (lane_of(ws, run) / "strategy.py").read_bytes() == REVERTING
    assert ws.path("hypotheses", registered, "strategy.py").read_bytes() == REVERTING


def test_a_discard_before_the_first_keep_restores_the_run_s_base(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(
        ws,
        store,
        document(
            constraints=[{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 99}}]
        ),
    )
    run = loop.begin(ws, store, hyp_id)
    assert run.best_sha is None
    edit(ws, run, WEAK)
    assert loop.card(ws, store, hyp_id, "still not enough trades").status == "discard"
    assert (lane_of(ws, run) / "strategy.py").read_bytes() == FLAT


def test_a_gate_the_operator_required_judges_the_card_and_the_model_cannot_loosen_it(
    ws: Workspace, store: StateStore
) -> None:
    """The authority rule where it has to hold: the loop, on a real card.

    The classification asks for four trades and the operator requires ninety-nine. The
    card trades and is discarded, so what ran is the operator's number.
    """
    hyp_id = classify(
        ws,
        store,
        document(
            required_constraints=[{"id": "min_trades", "params": {"min": 99}}],
            constraints=[{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 4}}],
        ),
    )
    run = loop.begin(ws, store, hyp_id)
    edit(ws, run, WEAK)

    made = loop.card(ws, store, hyp_id, "trades, but not ninety-nine times")

    assert made.status == "discard"
    (gate,) = [found for found in made.gate_results if found.id == "min_trades"]
    assert gate.passed is False
    assert gate.evidence["min"] == 99, "the operator's floor ran, not the classifier's 4"
    assert made.n_trades > 4, "and it would have passed the number the model chose"


def test_a_card_that_raises_is_a_crash_carrying_its_traceback_tail(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    edit(ws, run, RAISING)
    crashed = loop.card(ws, store, registered, "asks the impossible")

    assert crashed.status == "crash"
    assert crashed.metric == 0.0
    assert crashed.crash_tail is not None
    assert "the card asked for the impossible" in crashed.crash_tail
    assert (lane_of(ws, run) / "strategy.py").read_bytes() == FLAT


def test_code_that_violates_the_boundary_is_discarded_before_it_runs(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    edit(ws, run, READING)
    refused = loop.card(ws, store, registered, "reads the filesystem")

    assert refused.status == "discard"
    assert (refused.metric, refused.wall_s, refused.peak_mem_gb) == (0.0, 0.0, 0.0)
    assert [gate.id for gate in refused.gate_results] == ["strategy_integrity"]
    assert not refused.gate_results[0].passed
    assert (lane_of(ws, run) / "strategy.py").read_bytes() == FLAT


def test_editing_the_hypothesis_inside_a_run_is_rejected_and_restored(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    pinned = store.get_blob(run.hypothesis_sha)
    (lane_of(ws, run) / "hypothesis.yaml").write_bytes(pinned + b"\n# a wider window\n")
    edit(ws, run, REVERTING)

    refused = loop.card(ws, store, registered, "widen the window")
    assert refused.status == "discard"
    assert refused.wall_s == 0.0
    assert "no longer equals the blob" in str(refused.gate_results[0].evidence)
    assert (lane_of(ws, run) / "hypothesis.yaml").read_bytes() == pinned


def test_a_file_that_is_not_one_of_the_three_is_rejected(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    (lane_of(ws, run) / "notes.md").write_text("learnings", encoding="utf-8")
    refused = loop.card(ws, store, registered, "left a note behind")
    assert refused.status == "discard"
    assert "notes.md" in str(refused.gate_results[0].evidence)


def test_a_lane_without_a_strategy_has_nothing_to_evaluate(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    (lane_of(ws, run) / "strategy.py").unlink()
    with pytest.raises(PreconditionError, match="it has been restored"):
        loop.card(ws, store, registered, "nothing at all")
    assert (lane_of(ws, run) / "strategy.py").read_bytes() == FLAT


def test_a_card_without_a_run_is_refused(ws: Workspace, store: StateStore, registered: str) -> None:
    with pytest.raises(PreconditionError, match="has no active run"):
        loop.card(ws, store, registered, "no run at all")


def test_a_card_from_another_lane_is_refused(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    loop.begin(ws, store, registered)
    with pytest.raises(PreconditionError, match="lanes never share a lane directory"):
        loop.card(ws, store, registered, "from the wrong lane", lane="l1")


# --- history and isolation ---------------------------------------------------


def test_history_survives_every_restore(ws: Workspace, store: StateStore, registered: str) -> None:
    run = loop.begin(ws, store, registered)
    for source, desc in ((REVERTING, "the trough rule"), (WEAK, "one step"), (RAISING, "boom")):
        edit(ws, run, source)
        loop.card(ws, store, registered, desc)

    assert statuses(store) == ["keep", "keep", "discard", "crash"]
    rows = results_tsv(store, registered).splitlines()
    assert len(rows) == 5
    assert [row.split("\t")[-1] for row in rows[1:]] == [
        loop.BASELINE,
        "the trough rule",
        "one step",
        "boom",
    ]
    assert [int(row.split("\t")[3]) for row in rows[1:]] == [1, 2, 3, 4]
    assert records.n_trials(store, registered) == 4


def test_two_lane_directories_never_touch_each_other_s_files(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    other = classify(ws, store, document(id="demo_alt"), REVERTING)
    left = loop.begin(ws, store, registered)
    right = loop.begin(ws, store, other, lane="l1")

    assert lane_of(ws, left) != lane_of(ws, right)
    assert (lane_of(ws, left) / "strategy.py").read_bytes() == FLAT
    assert (lane_of(ws, right) / "strategy.py").read_bytes() == REVERTING

    edit(ws, left, WEAK)
    loop.card(ws, store, registered, "one step of memory")

    assert (lane_of(ws, right) / "strategy.py").read_bytes() == REVERTING
    assert [card.lane for card in records.cards_of(store, other)] == ["l1"]
    assert {card.lane for card in records.cards_of(store, registered)} == {"op"}


def test_end_closes_the_run_and_removes_only_the_lane_directory(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    log = ws.path("runs", "op", f"{registered}-{run.tag}.jsonl")
    log.write_text("{}\n", encoding="utf-8")

    closed = loop.end(ws, store, registered)

    assert closed.ended_at is not None
    assert not lane_of(ws, run).exists()
    assert log.exists()
    assert records.active(store, registered) is None
    assert len(records.cards_of(store, registered)) == 1
    assert store.get_blob(run.base_sha) == FLAT
    assert records.best_of(store, registered)[0] == run.best_sha
    assert results_file(ws, registered).exists()


def test_ending_a_hypothesis_that_is_not_running_is_refused(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    with pytest.raises(PreconditionError, match="has no active run"):
        loop.end(ws, store, registered)


def test_a_later_run_resumes_from_the_best_and_from_workspace_starts_over(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    loop.end(ws, store, registered)

    resumed = loop.begin(ws, store, registered)
    assert resumed.base_sha == kept.strategy_sha
    assert (lane_of(ws, resumed) / "strategy.py").read_bytes() == REVERTING
    loop.end(ws, store, registered)

    ws.path("hypotheses", registered, "strategy.py").write_bytes(FLAT)
    restarted = loop.begin(ws, store, registered, from_workspace=True)
    assert restarted.base_sha == sha256(FLAT).hexdigest()
    assert restarted.best_sha == restarted.base_sha, "its own baseline is the new best"
    assert [event.kind for event in store.events(subject=registered)].count("best_cleared") == 1


def test_a_run_begins_from_the_reseed_the_queue_passage_carries_and_keeps_the_best(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """Two ancestries, two records: the re-seeded run climbs from its own base, and the
    hypothesis's best moves only when a keep beats it."""
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    loop.end(ws, store, registered)
    weak = store.put_blob(WEAK)
    scheduler.requeue(store, registered, scheduler.STALL_PRIORITY, reseed_from=weak)

    reseeded = loop.begin(ws, store, registered)

    assert reseeded.base_sha == weak
    assert (lane_of(ws, reseeded) / "strategy.py").read_bytes() == WEAK
    assert reseeded.best_sha == weak, "its baseline is its own first keep"
    assert records.best_of(store, registered) == (kept.strategy_sha, kept.metric)
    assert champion(ws, store, registered) == kept.strategy_sha, "the file is the champion"
    assert doctor._best(ws, None).status == "ok"
    begun = store.events(kind=passages.BEGUN, subject=registered)[-1]
    assert begun.detail[passages.RESEED_FROM] == weak
    assert passages.reseed_of(store, registered) is None, "beginning consumed it"
    assert store.events(kind="best_cleared", subject=registered) == []
    # Climbing back to the best's bytes beats this run's own best, so the keep rule —
    # which runs first — keeps it rather than reading it as redundant; and an equal
    # metric does not beat the hypothesis's best, which stays where it was.
    edit(ws, reseeded, REVERTING)
    climbed = loop.card(ws, store, registered, "the trough rule again")
    assert climbed.status == "keep"
    assert records.require_active(store, registered).best_sha == kept.strategy_sha
    assert records.best_of(store, registered) == (kept.strategy_sha, kept.metric)
    assert best_run_id(store, registered) == run.run_id, "an equal metric beats nothing"
    assert champion(ws, store, registered) == kept.strategy_sha


def test_a_re_seeded_keep_moves_the_champion_file_only_when_it_moves_the_best(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    """The workspace `strategy.py` follows the hypothesis's best, not the run's: a lesser
    keep of a re-seeded run leaves it, and the keep that beats the best rewrites it."""
    first = loop.begin(ws, store, registered)
    loop.end(ws, store, registered)
    scheduler.requeue(store, registered, scheduler.STALL_PRIORITY, reseed_from=store.put_blob(WEAK))

    reseeded = loop.begin(ws, store, registered)

    baseline = records.cards_of(store, registered)[-1]
    assert (baseline.status, baseline.strategy_sha) == ("keep", sha256(WEAK).hexdigest())
    assert baseline.metric <= 0.0, "measured: WEAK earns nothing, so it does not beat FLAT"
    assert best_run_id(store, registered) == first.run_id
    assert ws.path("hypotheses", registered, "strategy.py").read_bytes() == FLAT
    assert doctor._best(ws, None).status == "ok"

    edit(ws, reseeded, REVERTING)
    beaten = loop.card(ws, store, registered, "the trough rule")

    assert beaten.status == "keep"
    assert best_run_id(store, registered) == reseeded.run_id
    assert champion(ws, store, registered) == beaten.strategy_sha
    assert ws.path("hypotheses", registered, "strategy.py").read_bytes() == REVERTING


def test_from_workspace_outranks_a_reseed_and_a_missing_blob_is_ignored(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    edit(ws, run, REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")
    loop.end(ws, store, registered)

    scheduler.requeue(store, registered, scheduler.STALL_PRIORITY, reseed_from="c" * 64)
    resumed = loop.begin(ws, store, registered)
    assert resumed.base_sha == kept.strategy_sha, "a blob the store lacks re-seeds nothing"
    loop.end(ws, store, registered)

    scheduler.requeue(store, registered, scheduler.STALL_PRIORITY, reseed_from=store.put_blob(WEAK))
    ws.path("hypotheses", registered, "strategy.py").write_bytes(FLAT)  # a keep rewrote it
    restarted = loop.begin(ws, store, registered, from_workspace=True)
    assert restarted.base_sha == sha256(FLAT).hexdigest()
    assert records.best_of(store, registered)[0] == restarted.base_sha


# --- the pieces the loop is assembled from -----------------------------------


def test_the_memory_cap_is_the_lane_share_floored_at_the_baseline_s_need(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    assert loop._mem_cap(ws, run) == 8.0

    heavy = run.model_copy(update={"baseline_peak_mem_gb": 100.0})
    assert loop._mem_cap(ws, heavy) == loop.HEADROOM * 100.0

    ws.path("envelope.yaml").unlink()
    assert loop._mem_cap(ws, run) == loop.HEADROOM * run.baseline_peak_mem_gb


@pytest.mark.parametrize("declared, peak, cap", [(2.0, 0.25, 2.0), (0.5, 4.0, 12.0)])
def test_a_declared_lane_memory_is_what_a_card_of_that_lane_may_hold(
    ws: Workspace,
    store: StateStore,
    registered: str,
    declared: float,
    peak: float,
    cap: float,
) -> None:
    """`[env] mem_per_lane_gb` is the first way the plan figure can fall under 4 GB, so
    it is the first way a card's kill threshold can: the lane's share unless the run's
    own baseline needed more, and never under three times that."""
    run = loop.begin(ws, store, registered)
    plan = ENVELOPE.plan.model_copy(update={"mem_per_lane_gb": declared})
    write_envelope(ws, ENVELOPE.model_copy(update={"plan": plan}))

    assert loop._mem_cap(ws, run.model_copy(update={"baseline_peak_mem_gb": peak})) == cap


def test_one_card_is_costed_with_one_venue_model() -> None:
    from kanso.schemas import VenueOverride, resolve_venue_model

    left = resolve_venue_model("XNAS", max_leverage=1.0)
    right = resolve_venue_model("XETR", max_leverage=1.0)
    assert loop._one_venue_model({"XNAS": left, "XETR": right}).venue == "XETR"

    cash = resolve_venue_model("XNAS", override=VenueOverride(account="cash"))
    with pytest.raises(ValidationError, match="one card is costed with one model"):
        loop._one_venue_model({"XNAS": cash, "XETR": right})


# --- under a sizing rule -----------------------------------------------------


REVERSING = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 3:
            self.submit_entry(bar.bar_type.instrument_id, "BUY")
        elif self.seen == 5:
            self.submit_entry(bar.bar_type.instrument_id, "SELL")
"""

KNOBBED = REVERSING.replace(b'"SELL")', b'"SELL", notional=100.0)')

SIZED = {"capital": 100_000, "sizing": {"mode": "full_book", "budget": 10_000}}


def test_a_refused_card_is_a_discard_carrying_the_sizing_gate(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, document(**SIZED), FLAT)
    run = loop.begin(ws, store, hyp_id)
    edit(ws, run, REVERSING)

    refused = loop.card(ws, store, hyp_id, "reverses without exiting")

    assert refused.status == "discard"
    assert (refused.metric, refused.n_trades, refused.crash_tail) == (0.0, 0, None)
    assert [gate.id for gate in refused.gate_results] == ["strategy_integrity", "sizing"]
    sizing = refused.gate_results[1]
    assert not sizing.passed
    assert sizing.evidence["rule"] == "one_position"
    assert sizing.evidence["instrument_id"] == "DEMO.XNAS"
    assert "on the other side" in str(sizing.evidence["why"])
    assert (lane_of(ws, run) / "strategy.py").read_bytes() == FLAT


def test_a_size_knob_is_discarded_before_any_backtest_under_sizing(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, document(**SIZED), FLAT)
    run = loop.begin(ws, store, hyp_id)
    edit(ws, run, KNOBBED)

    refused = loop.card(ws, store, hyp_id, "names a notional")

    assert refused.status == "discard"
    assert refused.wall_s == 0.0
    assert [gate.id for gate in refused.gate_results] == ["strategy_integrity"]
    problems = refused.gate_results[0].evidence["problems"]
    assert any("keyword 'notional=' is denied under sizing" in str(p) for p in problems)


def test_a_refused_baseline_refuses_the_run_naming_the_rule(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, document(**SIZED), REVERSING)

    with pytest.raises(PreconditionError, match="sizing refused one_position at DEMO.XNAS"):
        loop.begin(ws, store, hyp_id)

    assert show(ws, store, hyp_id).active_run is None


# --- warming --------------------------------------------------------------------


def warmed(ws: Workspace, store: StateStore, sessions: int = 3) -> str:
    """The demo hypothesis, warming on this many sessions before each window."""
    return classify(ws, store, document(warmup={"sessions": sessions}))


def test_a_warmed_hypothesis_needs_its_sessions_in_the_catalog(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = warmed(ws, store)

    with pytest.raises(PreconditionError) as refused:
        loop.begin(ws, store, hyp_id)

    assert (
        "warmup: demo_mr asks for 3 session(s) before 2024-01-01 and the catalog holds 0"
        in refused.value.message
    )
    assert "`kanso data load`" in (refused.value.remedy or "")
    assert show(ws, store, hyp_id).active_run is None  # type: ignore[union-attr]


def test_a_warmed_run_pins_a_snapshot_that_covers_the_prefix(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = warmed(ws, store)
    load_december(ws, freeze=False)

    with pytest.raises(PreconditionError, match="and the warmup sessions before each"):
        loop.begin(ws, store, hyp_id)

    snapshot.freeze(ws)
    run = loop.begin(ws, store, hyp_id)

    assert run.snapshot_id == snapshot.newest(ws).snapshot_id  # type: ignore[union-attr]
    assert records.cards_of(store, hyp_id)[0].status == "keep"


def test_every_card_of_a_warmed_run_is_handed_the_same_prefix(
    ws: Workspace, store: StateStore
) -> None:
    """Resolved once in the parent; the child re-checks the span rather than computing one."""
    load_december(ws)
    hyp = Hypothesis.model_validate(document(warmup={"sessions": 3}))

    setup = loop._setup(ws, store, hyp)
    request = loop._request(setup, FLAT, "a" * 64, budget_s=None, mem_cap_gb=None)

    assert setup.prefix == (date(2023, 12, 29), date(2023, 12, 31))
    assert request.prefix == setup.prefix
    assert request.window == RESEARCH
    assert loop._warmup_spans(setup) == (setup.prefix, (date(2024, 2, 3), date(2024, 2, 5)))


def test_a_day_the_catalog_gained_moves_the_prefix_and_the_reading_with_it(
    ws: Workspace, store: StateStore
) -> None:
    """The prefix is resolved from the catalog for every card, so two cards can differ in it.

    `_setup` is rebuilt per card and `backtest.warmup_prefix` takes the last N distinct
    days it finds, so a `kanso data load` between two cards of one run moves what the
    second is warmed on while the run's snapshot, its hypothesis file and this package all
    stand still. The number moved with it and said nothing, which is what the digest is
    for.
    """
    hyp = Hypothesis.model_validate(document(warmup={"sessions": 3}))
    write_days(ws, (date(2023, 12, 1), date(2023, 12, 29)))

    before = loop._setup(ws, store, hyp)
    write_days(ws, (date(2023, 12, 30), date(2023, 12, 31)))
    after = loop._setup(ws, store, hyp)

    assert before.prefix == (date(2023, 12, 27), date(2023, 12, 29))
    assert after.prefix == (date(2023, 12, 29), date(2023, 12, 31)), "two days the loader added"
    assert before.measured_under != after.measured_under


def test_every_field_of_a_setup_is_a_reading_or_is_not(ws: Workspace, store: StateStore) -> None:
    """The digest claims completeness against `Setup`, so `Setup` is what states it.

    A field added to the card's setup either moves a card's number — and belongs in the
    digest, or a stored anchor outlives the reading it was measured under — or does not,
    and the reason it does not is worth writing down once. This fails on a field that is
    neither, which is the only way a claim of completeness can be kept.
    """
    read = {"capital", "folds", "period", "venue_model", "harness", "sleeve_budget", "grains"}
    read |= {"prefix"}
    pinned = {"hyp", "impl", "host_source", "host_modifiers"}
    no_number = {"max_lines", "catalog", "extensions"}

    assert not read & (pinned | no_number)
    assert read | pinned | no_number == {field.name for field in fields(loop.Setup)}

    setup = loop._setup(ws, store, Hypothesis.model_validate(DOCUMENT))
    for name, moved in (
        ("capital", 1.0),
        ("folds", 9),
        ("period", "1h"),
        ("sleeve_budget", 1234.0),
        ("grains", ("1m",)),
        ("prefix", (date(2023, 12, 29), date(2023, 12, 31))),
    ):
        assert replace(setup, **{name: moved}).measured_under != setup.measured_under, name
    for name, same in (
        ("max_lines", 999),
        ("catalog", ws.path("nowhere")),
        ("extensions", ((str(ws.path("kanso_ext")), "elsewhere"),)),
    ):
        assert replace(setup, **{name: same}).measured_under == setup.measured_under, name


def test_a_setup_imports_the_workspace_s_extensions_and_hands_them_to_its_cards(
    ws: Workspace, store: StateStore
) -> None:
    """Built once per card in the lane, so the lane has imported them before it reads a
    window, and a card is told what to import before it unpickles one."""
    directory = ws.path("kanso_ext")
    directory.mkdir()
    (directory / "kanso_setup_probe.py").write_text("PROBED = True\n", encoding="utf-8")

    setup = loop._setup(ws, store, Hypothesis.model_validate(DOCUMENT))

    assert setup.extensions == ((str(directory), "kanso_setup_probe"),)
    assert "kanso_setup_probe" in sys.modules


def test_an_unwarmed_run_has_no_prefix_anywhere(ws: Workspace, store: StateStore) -> None:
    setup = loop._setup(ws, store, Hypothesis.model_validate(DOCUMENT))

    assert setup.prefix is None
    assert loop._warmup_spans(setup) == ()


# --- a benchmark -------------------------------------------------------------


HELD = document(
    benchmark={"hold": "first_leg"},
    objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
)
"""The demo sleeve, measured against a hold of its one instrument."""


def test_a_card_is_measured_against_the_hold_the_runner_produced(
    ws: Workspace, store: StateStore
) -> None:
    """A strategy that trades nothing scores minus what the hold scored, fold by fold."""
    hyp_id = classify(ws, store, HELD)

    run = loop.begin(ws, store, hyp_id)

    setup = loop._setup(ws, store, Hypothesis.model_validate(HELD))
    request = loop._request(setup, FLAT, run.snapshot_id, budget_s=None, mem_cap_gb=None)
    hold = backtest.run(backtest.benchmark(request), setup.catalog).run
    assert len(hold.fills) == 1 and hold.trades == ()
    held, spread = wf_sharpe_net.compute(hold, setup.folds)
    baseline = records.cards_of(store, hyp_id)[0]
    assert (baseline.metric, baseline.metric_se) == pytest.approx((-held, spread))
    assert baseline.metric != 0.0


def test_every_card_of_a_run_is_differenced_against_one_hold(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, HELD)
    derived: list[object] = []
    original = backtest.benchmark

    def counted(request: backtest.RunRequest) -> backtest.RunRequest:
        derived.append(request.window)
        return original(request)

    monkeypatch.setattr(loop.backtest, "benchmark", counted)
    run = loop.begin(ws, store, hyp_id)
    edit(ws, run, REVERTING)
    loop.card(ws, store, hyp_id, "buy the trough")
    edit(ws, run, WEAK)
    loop.card(ws, store, hyp_id, "buy any fall")

    assert derived == [RESEARCH]
    trough = records.cards_of(store, hyp_id)[1]
    assert (trough.metric, trough.metric_se) == pytest.approx((14.527, 2.255), abs=1e-3), (
        "measured on the saw-tooth: the trough-buyer's 15.689 by wf_sharpe_net, less the hold's"
    )
    assert [key for key in loop._HOST_RUNS[run.run_id] if key.startswith("benchmark")] == [
        f"benchmark@{run.snapshot_id}@None"
    ]
    loop.end(ws, store, hyp_id)
    assert run.run_id not in loop._HOST_RUNS


def test_an_objective_that_measures_no_benchmark_runs_no_hold(
    ws: Workspace, store: StateStore
) -> None:
    setup = loop._setup(ws, store, Hypothesis.model_validate(DOCUMENT))
    cache: dict[str, object] = {}

    assert loop._benchmark_run(setup, snapshot_id="a" * 64, cache=cache) is None  # type: ignore[arg-type]
    assert cache == {}
