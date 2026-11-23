"""A stall's research gates, judged on research data before a certification window is read.

The precheck is the certificate's own research gates asked early: the search correction the
plan pins for the subject's objective, then `walk_forward_consistency` and `research_plateau`,
each by its own `evaluate`, on the research run the subject card kept — in the lane that
stalled, which runs nothing and so leaves the plateau to the child — or on the research run
certification has just made — in the child, before it reads the certification window. What
is under test is that each is the same verdict on the same numbers as the certificate's, that
a refusal by any of them reads no certification window, and that where one cannot be judged
it judges nothing rather than guessing.

The workspace is the research suite's synthetic saw-tooth. A measurement built here from
`build_run` stands for one the loop kept: what the precheck reads off it is the objective,
the folds, the run's period and capital, the criteria version and the runs themselves, and
nothing about how they were produced. The one test that claims the precheck equals a
certificate measures its card with the loop itself.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from kanso.certify import certificate, run
from kanso.certify.run import (
    LEFT_TO_CHILD,
    RESEARCH_FAILED,
    Precheck,
    certify,
    certify_at_stall,
    precheck,
    read_precheck,
)
from kanso.criteria import criteria_version
from kanso.criteria.context import skipped, verdict
from kanso.data import snapshot as snapshots
from kanso.errors import PreconditionError
from kanso.hyp import set_status, show
from kanso.nautilus import backtest
from kanso.research import loop, records, scheduler
from kanso.research.records import Measurement
from kanso.schemas import ObjectiveRef
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import (
    CERT_GATES,
    SEARCHED,
    a_card,
    a_grid,
    a_manifest,
    write_plan,
)
from tests.criteria.builders import build_run
from tests.criteria.test_param_plateau import source
from tests.research.conftest import (
    CERTIFICATION,
    DOCUMENT,
    FLAT,
    HYP_ID,
    RESEARCH,
    REVERTING,
    classify,
)

RESEARCH_RUN = build_run((3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0, 2.0))
"""A research run that earned on six days of eight, its error read off all eight."""

TIGHT = (1.0001, 0.9999)
"""Two trials beside a best that scored 1.0: a search whose candidates barely differ, so its
expected maximum is next to nothing and an estimate clear of its own error passes."""

WIDE = (-40.0, 40.0, -20.0, 20.0)
"""Four trials spread forty either side: a search whose expected maximum no estimate of this
size reaches, so the correction refuses it."""

FICKLE = build_run((3.0, -1.0, -4.0, -1.0, 5.0, -2.0, -6.0, -2.0))
"""A research run that earned in two of its four folds and lost in the other two."""


def with_gate(gate_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The cert gates a plan for the demo carries, one of them at other values."""
    return [{**gate, "params": params} if gate["id"] == gate_id else gate for gate in CERT_GATES]


EVERY_FOLD = with_gate("walk_forward_consistency", {"min_positive_folds": 4})
"""The demo's cert gates asking every one of the four research folds to be positive."""

STEEP = with_gate("research_plateau", {"perturb_pct": 10.0, "keep_fraction": 0.5})
"""The demo's cert gates asking every move of the research plateau to keep half its score."""

FRAGILE = source(1.0)
"""The reverter whose `min_fall` sits at the deepest fall the saw-tooth has: moved up a tenth
it never trades, so the research plateau refuses it at any `keep_fraction` above zero."""


def read_of(checked: Precheck, gate_id: str) -> Any:
    """The one reading of a gate a precheck took."""
    (found,) = [reading for reading in checked.readings if reading.id == gate_id]
    return found


def gate_ids(checked: Precheck) -> list[str]:
    return [reading.id for reading in checked.readings]


def measurement(ws: Workspace, store: StateStore, **changes: Any) -> str:
    """A stored measurement, struck as certification would strike the research window now
    — the pin's objective, the workspace's folds, its period and capital, this kanso — with
    `changes` replacing any of it."""
    fields: dict[str, Any] = {
        "objective": ObjectiveRef.model_validate(DOCUMENT["objective"]),
        "folds": ws.config.research.folds,
        "run": RESEARCH_RUN,
        "criteria_version": criteria_version(),
        **changes,
    }
    return records.record_measurement(store, Measurement(**fields))


def searched(ws: Workspace, store: StateStore, *metrics: float) -> None:
    """Cards behind the best, one trial of each metric, none of them the best."""
    for seq, metric in enumerate(metrics, start=2):
        a_card(ws, store, FLAT + f"# trial {seq}\n".encode(), seq=seq, metric=metric, best=False)


def best_with(ws: Workspace, store: StateStore, *trials: float, **measured: Any) -> str:
    """The demo hypothesis with a best that kept a measurement, the trials behind it and a
    plan that corrects its search."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store, **measured))
    searched(ws, store, *trials)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    return sha


def plan_of(ws: Workspace) -> Any:
    from kanso.certify.plan import read_plan

    return read_plan(ws, HYP_ID)


# --- in the lane, from the measurement the best kept --------------------------


def test_a_search_wider_than_the_best_s_edge_is_refused_from_the_measurement_it_kept(
    ws: Workspace, store: StateStore
) -> None:
    sha = best_with(ws, store, *WIDE)

    checked = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert (checked.gate, checked.judged, checked.passed) == ("deflated_sharpe", True, False)
    assert checked.spared and checked.reason is None
    assert checked.measured is False, "read in the lane, off the card: nothing was run"
    assert checked.evidence["min_dsr"] == SEARCHED["params"]["min_dsr"], "the plan's threshold"
    assert checked.evidence["dsr"] < checked.evidence["min_dsr"]
    assert (checked.evidence["trials"], checked.evidence["scored_trials"]) == (5, 5)


def test_a_search_whose_trials_barely_differ_passes(ws: Workspace, store: StateStore) -> None:
    sha = best_with(ws, store, *TIGHT)

    checked = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert (checked.judged, checked.passed, checked.spared) == (True, True, False)
    assert checked.evidence["dsr"] >= checked.evidence["min_dsr"]


def test_the_lane_leaves_the_research_plateau_to_the_child(
    ws: Workspace, store: StateStore
) -> None:
    """The plateau re-runs the research window once per move, and the lane runs nothing: it is
    recorded as left to the child, after every research gate the lane could judge."""
    sha = best_with(ws, store, *TIGHT)

    checked = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert gate_ids(checked) == ["deflated_sharpe", "walk_forward_consistency", "research_plateau"]
    plateau = read_of(checked, "research_plateau")
    assert (plateau.skipped, plateau.passed, plateau.evidence) == (LEFT_TO_CHILD, True, {})
    assert read_of(checked, "walk_forward_consistency").skipped is None
    assert not checked.spared
    assert len(LEFT_TO_CHILD) <= 200 and len(RESEARCH_FAILED) <= 200


def test_the_lane_spares_a_best_whose_folds_do_not_recur_without_reading_any_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`walk_forward_consistency` reads the research folds alone, so the lane judges it from the
    measurement the best kept, after the correction — here the plan's word that it carries
    none — and refuses a best positive in two folds of four when the plan asks for all four,
    reading nothing."""

    def unread(*_: object, **__: object) -> Any:
        raise AssertionError("the lane read a window")

    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store, run=FICKLE))
    plan = write_plan(ws, gates=EVERY_FOLD)
    monkeypatch.setattr(backtest, "window_chunks", unread)
    monkeypatch.setattr(backtest, "run", unread)

    checked = precheck(ws, store, HYP_ID, sha, plan)

    assert checked.spared and checked.measured is False
    assert (checked.gate, checked.judged, checked.passed) == (
        "walk_forward_consistency",
        True,
        False,
    )
    assert checked.evidence["positive_folds"] == 2
    assert checked.evidence["min_positive_folds"] == 4
    assert gate_ids(checked) == ["deflated_sharpe", "walk_forward_consistency"], (
        "the correction first, and nothing judged after the refusal"
    )
    assert read_of(checked, "deflated_sharpe").skipped is not None, "the plan carries none"
    assert checked.payload()["gates"][1]["pass"] is False


def test_a_declared_grid_is_among_the_draws_the_precheck_deflates_by(
    ws: Workspace, store: StateStore
) -> None:
    """The stall's correction counts the search the certificate's would: the cards that ran
    and traded and the settings the family declared, from the same spread."""
    sha = best_with(ws, store, *WIDE)
    alone = precheck(ws, store, HYP_ID, sha, plan_of(ws))
    classify(ws, store, {**DOCUMENT, "prior_trials": a_grid(ws, 40)}, REVERTING)

    checked = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert (alone.evidence["trials"], alone.evidence["prior_trials"]) == (5, 0)
    assert (checked.evidence["trials"], checked.evidence["prior_trials"]) == (45, 40)
    assert checked.evidence["scored_trials"] == alone.evidence["scored_trials"] == 5
    assert checked.evidence["expected_maximum"] > alone.evidence["expected_maximum"]
    assert checked.evidence["dsr"] < alone.evidence["dsr"]


@pytest.mark.parametrize(
    ("name", "changes", "was"),
    [
        (
            "objective",
            {
                "objective": ObjectiveRef(
                    id="wf_sharpe_vs_hold", params={"min_delta": 0.0, "k_se": 0.5}
                )
            },
            "wf_sharpe_vs_hold",
        ),
        ("folds", {"folds": 3}, "3"),
        ("return_period", {"run": replace(RESEARCH_RUN, period="1h")}, "1h"),
        ("capital", {"run": replace(RESEARCH_RUN, capital=50_000.0)}, "50000.0"),
        ("criteria_version", {"criteria_version": "0.0.1"}, "0.0.1"),
    ],
)
def test_a_measurement_struck_otherwise_than_certification_would_strike_it_judges_nothing(
    ws: Workspace, store: StateStore, name: str, changes: dict[str, Any], was: str
) -> None:
    """The arithmetic the card's number is would not be the certificate's, so the lane leaves
    the judging to the certification child, which runs the research window first."""
    sha = best_with(ws, store, *WIDE, **changes)

    checked = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert (checked.gate, checked.judged, checked.passed, checked.spared) == (
        "deflated_sharpe",
        False,
        None,
        False,
    )
    assert checked.reason is not None
    assert checked.reason.startswith(
        f"the newest card of {sha[:7]} was measured under {name} {was},"
    )
    assert checked.evidence == {}


def test_a_best_that_kept_no_measurement_judges_nothing(ws: Workspace, store: StateStore) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    searched(ws, store, *WIDE)
    plan = write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    checked = precheck(ws, store, HYP_ID, sha, plan)

    assert (checked.judged, checked.passed, checked.spared) == (False, None, False)
    assert checked.reason == f"the newest card of {sha[:7]} kept no measurement to judge it from"


def test_a_best_that_kept_no_measurement_under_a_plan_with_no_correction_says_it_carries_none(
    ws: Workspace, store: StateStore
) -> None:
    """The deciding reading is the correction's or the plan's word that it carries none,
    wherever the lane could read nothing: never the first research gate after it."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    plan = write_plan(ws)

    checked = precheck(ws, store, HYP_ID, sha, plan)

    assert (checked.gate, checked.judged, checked.passed) == ("deflated_sharpe", False, None)
    assert checked.reason is not None and checked.reason.startswith(
        "plan version 1 carries no deflated_sharpe"
    )
    assert gate_ids(checked) == ["deflated_sharpe", "walk_forward_consistency", "research_plateau"]
    unread = f"the newest card of {sha[:7]} kept no measurement to judge it from"
    assert [reading.skipped for reading in checked.readings[1:]] == [unread, unread]
    assert not checked.spared


def test_a_plan_that_carries_no_correction_has_no_search_to_price_and_still_judges_folds(
    ws: Workspace, store: StateStore
) -> None:
    """Before the search's second draw a plan carries no correction, which spares nothing, and
    the other research gates are judged all the same: the lane reads the folds, and leaves the
    plateau to the child."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    plan = write_plan(ws)

    checked = precheck(ws, store, HYP_ID, sha, plan)

    assert (checked.gate, checked.judged, checked.passed) == ("deflated_sharpe", False, None)
    assert checked.reason == (
        "plan version 1 carries no deflated_sharpe, the search correction for wf_sharpe_net: "
        "a plan carries it from 2 draws, its cards and declared prior_trials together"
    )
    assert gate_ids(checked) == ["deflated_sharpe", "walk_forward_consistency", "research_plateau"]
    folds = read_of(checked, "walk_forward_consistency")
    assert folds.skipped is None and folds.passed
    assert folds.evidence["positive_folds"] == 4
    assert not checked.spared


def test_a_correction_that_skips_on_research_data_spares_nothing(
    ws: Workspace, store: StateStore
) -> None:
    """A skip passes in the certificate too, so it is no reason to spare the window."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    plan = write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    checked = precheck(ws, store, HYP_ID, sha, plan)

    assert (checked.judged, checked.passed, checked.spared) == (False, None, False)
    assert checked.reason == "fewer than two trials, so the trial spread is unknown"


@pytest.mark.parametrize("missing", ["objective", "construct"])
def test_a_best_pinned_to_an_unclassified_file_is_left_to_certification_to_refuse(
    ws: Workspace, store: StateStore, missing: str
) -> None:
    """A card whose run pinned a file without a construct or an objective names no correction,
    so the lane judges nothing, raising nothing of its own, and the stall's certification
    refuses the subject as `cert run` does — a refusal with its remedy, which a daemon lane
    records and goes on from, never a fault that ends the lane."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    pinned = {name: value for name, value in DOCUMENT.items() if name != missing}
    sha = a_card(ws, store, REVERTING, document=pinned, measured_sha=measurement(ws, store))
    searched(ws, store, *WIDE)
    plan = write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    checked = precheck(ws, store, hyp_id, sha, plan)

    assert (checked.gate, checked.judged, checked.spared, checked.measured) == (
        None,
        False,
        False,
        False,
    )
    assert checked.reason == (
        f"the newest card of {sha[:7]} was researched without a construct or an objective, so "
        "there is no research gate to judge it by"
    )
    assert checked.readings == ()
    with pytest.raises(PreconditionError, match="without a construct or an objective") as refused:
        scheduler.on_stall(ws, store, hyp_id)
    assert refused.value.remedy == f"run `kanso classify {hyp_id}` and research it again"


PASSED = verdict("deflated_sharpe", True, {"dsr": 0.99, "min_dsr": 0.95})
REFUSED = verdict("deflated_edge", False, {"probability": 0.25, "min_probability": 0.95})
UNREAD = skipped("deflated_contribution", "the card kept no measurement")
FOLDS = verdict("walk_forward_consistency", False, {"positive_folds": 2, "min_positive_folds": 4})


@pytest.mark.parametrize(
    "checked",
    [
        Precheck([REFUSED], measured=False),
        Precheck([PASSED], measured=True),
        Precheck([UNREAD], measured=False),
        Precheck(measured=True, reason="no gate"),
        Precheck([PASSED, FOLDS, skipped("research_plateau", RESEARCH_FAILED)], measured=True),
        Precheck([UNREAD, verdict("walk_forward_consistency", True, {})], measured=False),
    ],
)
def test_a_precheck_reads_back_from_what_it_records(checked: Precheck) -> None:
    """The child reports its precheck as JSON and the lane reads it back whole, every reading
    under `gates` and the deciding one's keys beside them."""
    recorded = json.loads(json.dumps(checked.payload()))

    assert read_precheck(recorded) == checked
    assert ("reason" in recorded) is (checked.reason is not None)
    assert [entry["gate"] for entry in recorded["gates"]] == gate_ids(checked)
    deciding = checked.deciding
    assert recorded["gate"] == (None if deciding is None else deciding.id)


@pytest.mark.parametrize(
    "checked",
    [
        Precheck([REFUSED], measured=False),
        Precheck([UNREAD], measured=True),
        Precheck(measured=False, reason="no gate"),
    ],
)
def test_a_precheck_recorded_before_stalls_listed_gates_reads_back_as_its_one_reading(
    checked: Precheck,
) -> None:
    """A `stalled` event written before a stall judged more than its correction has no `gates`:
    its own keys are the one reading it took, or none where they name no gate."""
    recorded = json.loads(json.dumps(checked.payload()))
    del recorded["gates"]

    assert read_precheck(recorded) == checked


def test_the_deciding_reading_is_the_first_refusal_else_the_first() -> None:
    """The correction is judged first, so the keys a stall has always recorded stay its own
    unless another research gate spared the stall."""
    spared = Precheck([PASSED, FOLDS], measured=True)
    held = Precheck([UNREAD, verdict("walk_forward_consistency", True, {})], measured=False)

    assert spared.spared and spared.gate == "walk_forward_consistency"
    assert (spared.judged, spared.passed, spared.reason) == (True, False, None)
    assert spared.evidence == {"positive_folds": 2, "min_positive_folds": 4}
    assert not held.spared and held.gate == "deflated_contribution"
    assert (held.judged, held.passed, held.reason) == (False, None, UNREAD.skipped)
    assert Precheck(measured=False, reason="why").evidence == {}


# --- in the certification child, from the research run it makes ---------------


def test_the_child_s_precheck_says_it_ran_the_research_window_whatever_it_found(
    ws: Workspace, store: StateStore
) -> None:
    """`measured` says where a precheck was judged: the lane runs nothing, and the child has
    run the research window before it reads the plan, so its precheck says so even where the
    plan carries no correction to judge."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    plan = write_plan(ws)

    lane = precheck(ws, store, hyp_id, sha, plan)
    made, child = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is not None and child is not None
    assert (lane.judged, lane.measured) == (False, False)
    assert (child.judged, child.measured) == (False, True)
    assert child.reason == lane.reason
    assert read_of(child, "research_plateau").skipped is None, "the child judges the plateau"


def test_the_precheck_is_the_certificate_s_own_correction_to_the_last_bit(
    ws: Workspace, store: StateStore
) -> None:
    """The lane's reading of the card the loop measured, the child's reading of the research
    window it ran again, and the certificate's correction are one number, bit for bit: one
    gate's arithmetic on one run, with no warmup and no host to resolve again in between. So
    are the folds, and the certificate's plateau is the child's."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    loop.begin(ws, store, hyp_id)
    loop.end(ws, store, hyp_id)
    sha, metric = records.best_of(store, hyp_id)
    assert sha is not None and metric is not None and metric > 0
    searched(ws, store, metric + 0.01, metric - 0.01)
    plan = write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    lane = precheck(ws, store, hyp_id, sha, plan)
    made, child = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is not None and child is not None
    (deflation,) = [gate for gate in made.gates if gate.id == SEARCHED["id"]]
    assert (lane.measured, child.measured) == (False, True)
    assert lane.judged and child.judged and deflation.skipped is None
    assert lane.evidence == child.evidence == deflation.evidence
    assert lane.passed is child.passed is deflation.passed is True
    for gate_id in ("walk_forward_consistency", "research_plateau"):
        (recorded,) = [gate for gate in made.gates if gate.id == gate_id]
        assert recorded.evidence == read_of(child, gate_id).evidence, gate_id
    assert (
        read_of(lane, "walk_forward_consistency").evidence
        == read_of(child, "walk_forward_consistency").evidence
    )
    (certifiable,) = store.events(kind=scheduler.CERTIFIABLE, subject=hyp_id)
    assert certifiable.detail == {"strategy_sha": sha}, "a candidate once the research gates held"


def research_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[date, date]]:
    """The window of every read a certification makes from here on, refusing any read of the
    certification window."""
    read = backtest.window_chunks
    windows: list[tuple[date, date]] = []

    def research_only(request: backtest.RunRequest, *args: Any, **kwargs: Any) -> Any:
        windows.append(request.window)
        if request.window != RESEARCH:
            raise AssertionError("a spared stall read the certification window")
        return read(request, *args, **kwargs)

    monkeypatch.setattr(backtest, "window_chunks", research_only)
    return windows


def test_a_stall_the_correction_refuses_on_research_data_never_reads_the_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane could not judge — the card kept no measurement — so the child runs the
    research window, judges the correction there and stops before the certification window,
    and before the plateau's backtests. An operator's `cert run` is never spared: its
    certificate's correction is the one the child read, every research gate is judged but the
    plateau, which is not run behind a refusal, and the certification window is read."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, REVERTING)
    searched(ws, store, *WIDE)
    plan = write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    assert not precheck(ws, store, hyp_id, sha, plan).judged

    with monkeypatch.context() as patched:
        windows = research_reads(patched)
        made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert windows == [RESEARCH], "the research window was run, once, and only it"
    assert made is None
    assert checked is not None and checked.spared and checked.measured
    assert gate_ids(checked) == ["deflated_sharpe"], "the readings end at the refusal"
    assert checked.evidence["dsr"] < checked.evidence["min_dsr"]
    assert certificate.latest(store, hyp_id) is None
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert store.events(kind=scheduler.CERTIFIABLE) == []

    read = backtest.window_chunks
    windows = []

    def counted(request: backtest.RunRequest, *args: Any, **kwargs: Any) -> Any:
        windows.append(request.window)
        return read(request, *args, **kwargs)

    monkeypatch.setattr(backtest, "window_chunks", counted)
    by_hand = certify(ws, store, hyp_id)

    gates = {gate.id: gate for gate in by_hand.gates}
    assert gates["deflated_sharpe"].evidence == checked.evidence
    assert gates["deflated_sharpe"].passed is False
    assert gates["walk_forward_consistency"].skipped is None, "judged, by hand"
    plateau = gates["research_plateau"]
    assert (plateau.skipped, plateau.passed, plateau.evidence) == (RESEARCH_FAILED, True, {})
    assert by_hand.verdict == "fail"
    assert CERTIFICATION in windows, "by hand the certification window is read regardless"
    assert windows.count(RESEARCH) == 1, "and no research-window move was run"


def test_a_stall_the_research_plateau_refuses_never_reads_the_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane leaves the plateau to the child; the child runs the research window, judges
    the cheap research gates, then moves each parameter over the research window — and a best
    fitted to its one setting is refused there, the certification window unread: no
    certificate, no `certifiable` event, no look, and the hypothesis where it was."""
    hyp_id = classify(ws, store, DOCUMENT, FRAGILE)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, FRAGILE)
    plan = write_plan(ws, gates=STEEP)
    assert not precheck(ws, store, hyp_id, sha, plan).spared

    with monkeypatch.context() as patched:
        windows = research_reads(patched)
        made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is None and checked.spared and checked.measured
    assert set(windows) == {RESEARCH}, "the research window, run again for every move"
    assert len(windows) == 1 + checked.evidence["n_backtests"]
    assert (checked.gate, checked.passed) == ("research_plateau", False)
    moved = {(p["field"], p["direction"]): p["metric"] for p in checked.evidence["perturbations"]}
    assert moved[("min_fall", "up")] == 0.0, "1.10 is deeper than the saw-tooth ever falls"
    assert gate_ids(checked) == ["deflated_sharpe", "walk_forward_consistency", "research_plateau"]
    assert certificate.latest(store, hyp_id) is None
    assert store.events(kind=scheduler.CERTIFIABLE) == []
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    from kanso.certify.looks import looks

    assert looks(store, hyp_id, CERTIFICATION).prior == (), "no look was taken"


RAISES_WHEN_MOVED = source(1.0).replace(
    b"    def on_start(self) -> None:\n",
    b"    def on_start(self) -> None:\n"
    b"        if self.kanso_config.min_fall != 1.0:\n"
    b"            raise ValueError('only one setting runs')\n",
)
"""The reverter at its one setting, raising at any other: its card runs, its moves do not."""


def test_a_best_that_raises_when_moved_is_refused_by_the_research_plateau_not_by_certification(
    ws: Workspace, store: StateStore
) -> None:
    """`research_plateau` is in every plan, so a best that runs only where the search left it
    must be judged by it rather than refuse every certification: the moves that raised kept
    nothing, the stall is spared on that evidence with the certification window unread, and a
    certification by hand writes a certificate that fails on it."""
    assert source(1.0) != RAISES_WHEN_MOVED
    hyp_id = classify(ws, store, DOCUMENT, RAISES_WHEN_MOVED)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, RAISES_WHEN_MOVED)
    write_plan(ws)

    made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is None and checked.spared and checked.measured
    assert (checked.gate, checked.passed) == ("research_plateau", False)
    raised = {
        (move["field"], move["direction"]): move.get("raised")
        for move in checked.evidence["perturbations"]
        if move["metric"] is None
    }
    assert raised == {
        ("min_fall", "up"): "ValueError: only one setting runs",
        ("min_fall", "down"): "ValueError: only one setting runs",
    }
    assert store.events(kind=scheduler.CERTIFIABLE) == []

    by_hand = certify(ws, store, hyp_id)

    (plateau,) = [gate for gate in by_hand.gates if gate.id == "research_plateau"]
    assert plateau.passed is False and plateau.evidence == checked.evidence
    assert by_hand.verdict == "fail"


def test_the_certificate_records_the_plateau_the_child_read_and_pays_for_it_once(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every research-window move is one backtest, made once: the certificate's plateau is the
    reading the child took before the window was read, never judged again."""
    hyp_id = classify(ws, store, DOCUMENT, source(0.5))
    sha = a_card(ws, store, source(0.5))
    write_plan(ws)
    original = backtest.run
    research: list[backtest.RunRequest] = []

    def counted(request: backtest.RunRequest, *args: Any, **kwargs: Any) -> Any:
        if request.window == RESEARCH:
            research.append(request)
        return original(request, *args, **kwargs)

    monkeypatch.setattr(backtest, "run", counted)
    made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is not None and made.verdict == "pass"
    (plateau,) = [gate for gate in made.gates if gate.id == "research_plateau"]
    assert plateau.evidence == read_of(checked, "research_plateau").evidence
    assert plateau.evidence["n_backtests"] == 4, "notional and min_fall, each moved both ways"
    assert len(research) == 1 + plateau.evidence["n_backtests"], (
        "the subject's research run, and one run per move"
    )
    assert [request.overrides for request in research[1:]] == [
        {point["field"]: point["value"]} for point in plateau.evidence["perturbations"]
    ]


def test_a_trial_recorded_while_the_window_runs_moves_no_reading_of_the_certificate(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search is priced once, before the window is read: a card another lane records while
    certification runs the window does not reach the certificate's correction."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    searched(ws, store, *TIGHT)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    measure = run._measure

    def another_lane_cards(*args: Any, **kwargs: Any) -> Any:
        a_card(ws, store, FLAT + b"# another lane\n", seq=9, metric=0.5, best=False)
        return measure(*args, **kwargs)

    monkeypatch.setattr(run, "_measure", another_lane_cards)
    made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is not None
    (deflation,) = [gate for gate in made.gates if gate.id == SEARCHED["id"]]
    assert deflation.evidence == checked.evidence
    assert deflation.evidence["trials"] == 3, "the best and its two trials, not the fourth card"
    assert records.candidates(store, hyp_id) == 4


def test_a_stall_on_a_snapshot_that_cannot_support_a_claim_runs_no_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a snapshot fails every gate before anything runs, at a stall as by hand, so there
    is no research run to judge a research gate on and no window about to be read: the failing
    certificate is written with no `certifiable` and no move to candidate, beside a precheck
    that judged nothing, ran nothing and says why — the reasons the certificate fails on — for
    every research gate the plan carries."""
    a_manifest(ws, publication="unknown")
    snapshots.freeze(ws)
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, REVERTING)
    write_plan(ws)

    def unrun(*_: object, **__: object) -> Any:
        raise AssertionError("a certification on an inadmissible snapshot ran a backtest")

    monkeypatch.setattr(backtest, "window_chunks", unrun)
    made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert made is not None and made.verdict == "fail"
    assert (checked.gate, checked.judged, checked.passed) == ("deflated_sharpe", False, None), (
        "the plan carries no correction, and the deciding reading is still the correction's"
    )
    assert gate_ids(checked) == ["deflated_sharpe", "walk_forward_consistency", "research_plateau"]
    assert checked.measured is False, "no window was run for it"
    assert checked.reason is not None and "cannot support a claim" in checked.reason
    assert all(gate.evidence == checked.evidence for gate in made.gates)
    assert all(reading.evidence == checked.evidence for reading in checked.readings)
    assert checked.evidence["inadmissible"]
    assert store.events(kind=scheduler.CERTIFIABLE) == []
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
