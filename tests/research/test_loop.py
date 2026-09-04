"""begin, card and end over synthetic data: the whole loop, end to end."""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from kanso.criteria import SCOPED_FILES
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import show
from kanso.research import loop, records
from kanso.research.results import results_file, results_tsv
from kanso.schemas import RunRecord
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import (
    DOCUMENT,
    FLAT,
    HYP_ID,
    PROGRAM,
    RAISING,
    READING,
    REVERTING,
    WEAK,
    classify,
    document,
    write_hypothesis,
)


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
    with pytest.raises(PreconditionError, match="baseline card of demo_mr did not run"):
        loop.begin(ws, store, hyp_id)
    assert records.active(store, hyp_id) is None
    assert not ws.path("runs", "op", hyp_id).exists()
    assert [event.kind for event in store.events(subject=hyp_id)][-1] == loop.BASELINE_FAILED


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

    again = loop.card(ws, store, registered, "the very same file")
    assert again.status == "discard"
    assert again.metric == kept.metric, "the same snapshot and code give the same number"
    assert records.best_of(store, registered) == (kept.strategy_sha, kept.metric)


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


def test_one_card_is_costed_with_one_venue_model() -> None:
    from kanso.schemas import VenueOverride, resolve_venue_model

    left = resolve_venue_model("XNAS", max_leverage=1.0)
    right = resolve_venue_model("XETR", max_leverage=1.0)
    assert loop._one_venue_model({"XNAS": left, "XETR": right}).venue == "XETR"

    cash = resolve_venue_model("XNAS", override=VenueOverride(account="cash"))
    with pytest.raises(ValidationError, match="one card is costed with one model"):
        loop._one_venue_model({"XNAS": cash, "XETR": right})
