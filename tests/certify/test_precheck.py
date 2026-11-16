"""A stall's search correction, judged on research data before a certification window is read.

The precheck is the certificate's own correction asked early: the gate the plan pins for the
subject's objective, by its own `evaluate`, on the research run the subject card kept — in
the lane that stalled — or on the research run certification has just made — in the child,
before it reads the certification window. What is under test is that it is the same verdict
on the same numbers as the certificate's, and that where it cannot be, it judges nothing
rather than guessing.

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

from kanso.certify import certificate
from kanso.certify.run import Precheck, certify, certify_at_stall, precheck, read_precheck
from kanso.criteria import criteria_version
from kanso.data import snapshot as snapshots
from kanso.errors import PreconditionError
from kanso.hyp import set_status, show
from kanso.nautilus import backtest
from kanso.research import loop, records, scheduler
from kanso.research.records import Measurement
from kanso.schemas import ObjectiveRef
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import CERT_GATES, SEARCHED, a_card, a_grid, a_manifest, write_plan
from tests.criteria.builders import build_run
from tests.research.conftest import DOCUMENT, FLAT, HYP_ID, RESEARCH, REVERTING, classify

RESEARCH_RUN = build_run((3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0, 2.0))
"""A research run that earned on six days of eight, its error read off all eight."""

TIGHT = (1.0001, 0.9999)
"""Two trials beside a best that scored 1.0: a search whose candidates barely differ, so its
expected maximum is next to nothing and an estimate clear of its own error passes."""

WIDE = (-40.0, 40.0, -20.0, 20.0)
"""Four trials spread forty either side: a search whose expected maximum no estimate of this
size reaches, so the correction refuses it."""


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


def test_a_plan_that_carries_no_correction_has_no_search_to_price(
    ws: Workspace, store: StateStore
) -> None:
    """Before the search's second draw a plan carries no correction, and nothing is spared."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    plan = write_plan(ws)

    checked = precheck(ws, store, HYP_ID, sha, plan)

    assert (checked.gate, checked.judged, checked.passed) == ("deflated_sharpe", False, None)
    assert checked.reason == (
        "plan version 1 carries no deflated_sharpe, the search correction for wf_sharpe_net: "
        "a plan carries it once the search has 2 draws, its cards and the prior_trials its "
        "family declares together"
    )


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
        "there is no search correction to judge it by"
    )
    with pytest.raises(PreconditionError, match="without a construct or an objective") as refused:
        scheduler.on_stall(ws, store, hyp_id)
    assert refused.value.remedy == f"run `kanso classify {hyp_id}` and research it again"


@pytest.mark.parametrize(
    "checked",
    [
        Precheck("deflated_edge", True, False, {"probability": 0.25, "trials": 3}, measured=False),
        Precheck("deflated_sharpe", True, True, {"dsr": 0.99}, measured=True),
        Precheck(
            "deflated_contribution", False, None, {}, "the card kept no measurement", measured=False
        ),
        Precheck(None, False, None, {}, "no gate", measured=True),
    ],
)
def test_a_precheck_reads_back_from_what_it_records(checked: Precheck) -> None:
    """The child reports its precheck as JSON and the lane reads it back whole."""
    recorded = json.loads(json.dumps(checked.payload()))

    assert read_precheck(recorded) == checked
    assert ("reason" in recorded) is (checked.reason is not None)


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


def test_the_precheck_is_the_certificate_s_own_correction_to_the_last_bit(
    ws: Workspace, store: StateStore
) -> None:
    """The lane's reading of the card the loop measured, the child's reading of the research
    window it ran again, and the certificate's correction are one number, bit for bit: one
    gate's arithmetic on one run, with no warmup and no host to resolve again in between."""
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
    (certifiable,) = store.events(kind=scheduler.CERTIFIABLE, subject=hyp_id)
    assert certifiable.detail == {"strategy_sha": sha}, "a candidate once the correction held"


def test_a_stall_the_correction_refuses_on_research_data_never_reads_the_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane could not judge — the card kept no measurement — so the child runs the
    research window, judges the correction there and stops before the certification window.
    An operator's `cert run` is never spared, and its certificate's correction is the one the
    child read."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, REVERTING)
    searched(ws, store, *WIDE)
    plan = write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    assert not precheck(ws, store, hyp_id, sha, plan).judged

    read = backtest.window_chunks
    windows: list[tuple[date, date]] = []

    def research_only(request: backtest.RunRequest, *args: Any, **kwargs: Any) -> Any:
        windows.append(request.window)
        if request.window != RESEARCH:
            raise AssertionError("a spared stall read the certification window")
        return read(request, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(backtest, "window_chunks", research_only)
        made, checked = certify_at_stall(ws, store, hyp_id, sha=sha)

    assert windows == [RESEARCH], "the research window was run, and only it"
    assert made is None
    assert checked is not None and checked.spared and checked.measured
    assert checked.evidence["dsr"] < checked.evidence["min_dsr"]
    assert certificate.latest(store, hyp_id) is None
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert store.events(kind=scheduler.CERTIFIABLE) == []

    by_hand = certify(ws, store, hyp_id)

    (deflation,) = [gate for gate in by_hand.gates if gate.id == SEARCHED["id"]]
    assert deflation.evidence == checked.evidence and deflation.passed is False
    assert by_hand.verdict == "fail"


def test_a_stall_on_a_snapshot_that_cannot_support_a_claim_runs_no_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a snapshot fails every gate before anything runs, at a stall as by hand, so there
    is no research run to judge the correction on and no window about to be read: the failing
    certificate is written with no `certifiable` and no move to candidate, beside a precheck
    that judged nothing, ran nothing and says why — the reasons the certificate fails on."""
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
    assert (checked.gate, checked.judged, checked.passed) == ("deflated_sharpe", False, None)
    assert checked.measured is False, "no window was run for it"
    assert checked.reason is not None and "cannot support a claim" in checked.reason
    assert all(gate.evidence == checked.evidence for gate in made.gates)
    assert checked.evidence["inadmissible"]
    assert store.events(kind=scheduler.CERTIFIABLE) == []
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
