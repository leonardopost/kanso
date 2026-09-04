"""One monitoring pass: what it judges, what it moves, and what it refuses to move twice."""

from __future__ import annotations

from typing import Any

import pytest

from kanso import inbox, monitor
from kanso.errors import PreconditionError
from kanso.portfolio import files as portfolio_files
from kanso.schemas import load_yaml, write_yaml
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace

from .builders import (
    CAPITAL,
    PLAN,
    STRATEGY_ID,
    deploy,
    flat_run,
    hypothesis,
    pin_hypothesis,
    plan,
    portfolio,
    position,
    run_over,
    version,
    write_book,
    write_plan,
    write_strategy,
)

LOSS_GATE: list[dict[str, Any]] = [
    *PLAN["gates"][:2],
    {"id": "daily_loss_kill", "stage": "live", "params": {}, "rationale": "the day's limit"},
]
BOTH_LIVE_GATES: list[dict[str, Any]] = [*PLAN["gates"], LOSS_GATE[2]]

WIDE = (-1e9, 1e9)


class Recorder:
    """A stand-in for the portfolio's demote, so a pass can be observed without one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, ws: Workspace, store: StateStore, strategy: str, number: int) -> None:
        self.calls.append((strategy, number))


def _version_outcome(outcomes: list[monitor.Outcome]) -> monitor.Outcome:
    return next(outcome for outcome in outcomes if outcome.strategy is not None)


def _stage_outcome(outcomes: list[monitor.Outcome], stage: str) -> monitor.Outcome:
    return next(
        outcome for outcome in outcomes if outcome.strategy is None and outcome.stage == stage
    )


# --- the paper stage ----------------------------------------------------------


def test_a_version_passing_every_paper_gate_becomes_promotable(
    ws: Workspace, store: StateStore
) -> None:
    """The one decision the framework escalates rather than takes: real capital."""
    deploy(ws, store)

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    judged = _version_outcome(outcomes)
    assert judged.actions == ("promotable",)
    assert strategy_files.require(ws, STRATEGY_ID).versions[0].state == "promotable"
    row = store.connection.execute(
        "SELECT state FROM strategy_versions WHERE strategy_id = ?", (STRATEGY_ID,)
    ).fetchone()
    assert row["state"] == "promotable"
    entries = inbox.unread(store)
    assert [entry.kind for entry in entries] == ["promotable"]
    assert entries[0].subject == "demo_mr@1"


def test_a_version_already_promotable_is_not_escalated_again(
    ws: Workspace, store: StateStore
) -> None:
    """The pass runs every few minutes; an inbox repeating itself is one nobody reads."""
    deploy(ws, store)

    monitor.run_once(ws, store, demote=Recorder())
    second = monitor.run_once(ws, store, demote=Recorder())

    assert _version_outcome(second).actions == ()
    assert len(inbox.unread(store)) == 1


def test_a_version_that_has_not_waited_long_enough_is_left_on_paper(
    ws: Workspace, store: StateStore
) -> None:
    deploy(ws, store, run=flat_run(days=3))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert _version_outcome(outcomes).actions == ()
    assert strategy_files.require(ws, STRATEGY_ID).versions[0].state == "paper"
    assert inbox.unread(store) == []


def test_a_version_whose_every_paper_gate_skipped_is_not_promoted(
    ws: Workspace, store: StateStore
) -> None:
    """A version tested by nothing is not a version ready for money."""
    bare = [
        PLAN["gates"][0],
        {"id": "paper_forward", "stage": "paper", "params": {}, "rationale": "no floor chosen"},
        PLAN["gates"][2],
    ]
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan(gates=bare))
    write_strategy(ws, store, version())
    write_book(store, "paper", run=flat_run())
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    judged = _version_outcome(outcomes)
    assert judged.passed and judged.judged == ()
    assert judged.actions == ()


# --- the live stage -----------------------------------------------------------


def test_a_failing_live_gate_demotes_and_escalates(ws: Workspace, store: StateStore) -> None:
    demote = Recorder()
    deploy(ws, store, stage="live", state="live", ci90=(1.0, 2.0), live=((STRATEGY_ID, 1),))

    outcomes = monitor.run_once(ws, store, demote=demote)

    assert demote.calls == [(STRATEGY_ID, 1)]
    judged = _version_outcome(outcomes)
    assert judged.actions == ("demoted",)
    entries = inbox.unread(store)
    assert [entry.kind for entry in entries] == ["demoted"]
    assert "live_drift" in entries[0].summary


def test_the_daily_loss_halts_the_stage_and_does_not_demote(
    ws: Workspace, store: StateStore
) -> None:
    """Halting is the stronger act; demoting into a halted stage would change nothing."""
    demote = Recorder()
    losing = run_over(tuple([100.0] * 19 + [-5_000.0]))
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan(gates=LOSS_GATE))
    write_strategy(ws, store, version(state="live", ci90=WIDE))
    write_book(store, "live", run=losing)
    write_yaml(portfolio(live=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=demote)

    assert demote.calls == []
    judged = _version_outcome(outcomes)
    assert judged.actions == ("halted",)
    assert portfolio_files.read(ws).stages.live.kill_switch is True
    assert [entry.kind for entry in inbox.unread(store)] == ["deploy_blocked"]


def test_a_daily_loss_beside_another_failure_halts_and_demotes(
    ws: Workspace, store: StateStore
) -> None:
    """The kill switch is the daily loss's; the other failure still demotes the version."""
    demote = Recorder()
    losing = run_over(tuple([100.0] * 19 + [-5_000.0]))
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan(gates=BOTH_LIVE_GATES))
    write_strategy(ws, store, version(state="live", ci90=(1.0, 2.0)))
    write_book(store, "live", run=losing)
    write_yaml(portfolio(live=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=demote)

    assert demote.calls == [(STRATEGY_ID, 1)]
    assert _version_outcome(outcomes).actions == ("halted", "demoted")
    summaries = {entry.kind: entry.summary for entry in inbox.unread(store)}
    assert "kill_switch" in summaries["demoted"]


def test_a_halted_stage_still_demotes_rather_than_deadlocking(
    ws: Workspace, store: StateStore
) -> None:
    """The switch and the demotion it triggers must not wait on each other."""
    demote = Recorder()
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan())
    write_strategy(ws, store, version(state="live", ci90=(1.0, 2.0)))
    write_book(store, "live", run=flat_run())
    write_yaml(portfolio(live=((STRATEGY_ID, 1),), kill_live=True), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=demote)

    assert demote.calls == [(STRATEGY_ID, 1)]
    assert _version_outcome(outcomes).actions == ("demoted",)


def test_a_stage_already_halted_is_not_halted_again(ws: Workspace, store: StateStore) -> None:
    demote = Recorder()
    losing = run_over(tuple([100.0] * 19 + [-5_000.0]))
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan(gates=LOSS_GATE))
    write_strategy(ws, store, version(state="live", ci90=WIDE))
    write_book(store, "live", run=losing)
    write_yaml(portfolio(live=((STRATEGY_ID, 1),), kill_live=True), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=demote)

    assert _version_outcome(outcomes).actions == ()
    assert inbox.unread(store) == []


def test_a_live_book_inside_its_band_is_left_alone(ws: Workspace, store: StateStore) -> None:
    demote = Recorder()
    deploy(ws, store, stage="live", state="live", live=((STRATEGY_ID, 1),))

    outcomes = monitor.run_once(ws, store, demote=demote)

    assert demote.calls == []
    assert _version_outcome(outcomes).actions == ()


# --- stage exposure -----------------------------------------------------------


def test_exposure_inside_the_limits_reports_and_does_nothing(
    ws: Workspace, store: StateStore
) -> None:
    deploy(
        ws,
        store,
        positions=(position(qty=100.0),),
    )

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    exposure = _stage_outcome(outcomes, "paper").exposure
    assert exposure is not None
    assert exposure.gross == 1_000.0
    assert exposure.net == 1_000.0
    assert not exposure.breached
    assert portfolio_files.read(ws).stages.paper.kill_switch is False


def test_a_gross_breach_halts_the_stage_and_escalates(ws: Workspace, store: StateStore) -> None:
    """No per-order risk configuration can express this, so the pass is where it lives."""
    deploy(
        ws,
        store,
        ci90=(1.0, 2.0),
        positions=(position(qty=20_000.0),),
    )

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    stage = _stage_outcome(outcomes, "paper")
    assert stage.actions == ("halted",)
    assert stage.exposure is not None and stage.exposure.gross == 200_000.0
    assert portfolio_files.read(ws).stages.paper.kill_switch is True
    assert [entry.kind for entry in inbox.unread(store)] == ["deploy_blocked"]


def test_a_net_breach_is_judged_on_its_magnitude(ws: Workspace, store: StateStore) -> None:
    """A book that is short a great deal is as exposed as one that is long a great deal."""
    deploy(
        ws,
        store,
        positions=(position(qty=-20_000.0),),
        max_gross_pct=1_000.0,
    )

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    stage = _stage_outcome(outcomes, "paper")
    assert stage.exposure is not None and stage.exposure.net == -200_000.0
    assert stage.actions == ("halted",)


def test_gross_and_net_are_summed_over_every_version_on_the_stage(
    ws: Workspace, store: StateStore
) -> None:
    """No version sees the others, which is the whole reason this lives in the pass."""
    deploy(
        ws,
        store,
        ci90=(1.0, 2.0),
        positions=(position(instrument="AAA.XNAS", qty=6_000.0),),
        paper=((STRATEGY_ID, 1), ("other", 1)),
    )
    write_book(
        store,
        "paper",
        run=flat_run(),
        strategy_id="other",
        positions=(position(instrument="BBB.XNAS", qty=-5_000.0),),
        session_id="session-2",
    )

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    exposure = _stage_outcome(outcomes, "paper").exposure
    assert exposure is not None
    assert exposure.gross == 110_000.0
    assert exposure.net == 10_000.0
    assert exposure.breached


def test_an_already_halted_stage_is_not_escalated_again(ws: Workspace, store: StateStore) -> None:
    deploy(
        ws,
        store,
        ci90=(1.0, 2.0),
        positions=(position(qty=20_000.0),),
        kill_paper=True,
    )

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    stage = _stage_outcome(outcomes, "paper")
    assert stage.exposure is not None and stage.exposure.breached
    assert stage.actions == ()
    assert inbox.unread(store) == []


# --- what a pass cannot judge -------------------------------------------------


def test_a_deployment_with_no_composed_version_is_skipped(ws: Workspace, store: StateStore) -> None:
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert _version_outcome(outcomes).skipped == monitor.run.NO_VERSION


def test_a_deployment_whose_version_number_is_absent_is_skipped(
    ws: Workspace, store: StateStore
) -> None:
    pin_hypothesis(store, hypothesis())
    write_strategy(ws, store, version())
    write_yaml(portfolio(paper=((STRATEGY_ID, 2),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert _version_outcome(outcomes).skipped == monitor.run.NO_VERSION


def test_a_version_whose_node_has_written_no_book_is_skipped(
    ws: Workspace, store: StateStore
) -> None:
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan())
    write_strategy(ws, store, version())
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert _version_outcome(outcomes).skipped == monitor.run.NO_BOOK


def test_a_sleeve_with_no_pinned_copy_is_skipped(ws: Workspace, store: StateStore) -> None:
    """The bytes that were certified are what the gates are run against, or nothing is."""
    write_plan(ws, plan())
    write_strategy(ws, store, version())
    write_book(store, "paper", run=flat_run())
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert "no pinned copy" in str(_version_outcome(outcomes).skipped)


def test_a_sleeve_row_without_a_blob_is_skipped(ws: Workspace, store: StateStore) -> None:
    store.connection.execute(
        "INSERT INTO hypotheses (hyp_id, status, hypothesis_sha, created_at, updated_at)"
        " VALUES ('demo_mr', 'certified', NULL, '', '')"
    )
    write_plan(ws, plan())
    write_strategy(ws, store, version())
    write_book(store, "paper", run=flat_run())
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    assert "no pinned copy" in str(_version_outcome(monitor.run_once(ws, store)).skipped)


def test_a_sleeve_with_no_plan_for_the_stage_is_skipped(ws: Workspace, store: StateStore) -> None:
    pin_hypothesis(store, hypothesis())
    write_strategy(ws, store, version())
    write_book(store, "paper", run=flat_run())
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert "no plan naming a paper gate" in str(_version_outcome(outcomes).skipped)


def test_a_plan_naming_a_gate_this_build_cannot_run_records_it_as_skipped(
    ws: Workspace, store: StateStore
) -> None:
    """A pinned plan outlives the build that wrote it; a certificate must not claim it ran."""
    unknown = [
        PLAN["gates"][0],
        {"id": "made_up_gate", "stage": "paper", "params": {}, "rationale": "from the future"},
        PLAN["gates"][2],
    ]
    pin_hypothesis(store, hypothesis())
    write_plan(ws, plan(gates=unknown))
    write_strategy(ws, store, version())
    write_book(store, "paper", run=flat_run())
    write_yaml(portfolio(paper=((STRATEGY_ID, 1),)), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    judged = _version_outcome(outcomes)
    assert judged.gates[0].id == "made_up_gate"
    assert judged.gates[0].skipped == monitor.run.UNIMPLEMENTED
    assert judged.actions == ()


def test_a_workspace_without_a_portfolio_is_refused(ws: Workspace, store: StateStore) -> None:
    ws.path("portfolio.yaml").unlink()

    with pytest.raises(PreconditionError, match="portfolio.yaml"):
        monitor.run_once(ws, store)


# --- the seams ----------------------------------------------------------------


def test_demotion_is_the_portfolios_act(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass handed no demote calls the portfolio's, which moves *and* redeploys."""
    import kanso.portfolio

    assert monitor.run.portfolio_demote is kanso.portfolio.demote
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        monitor.run,
        "portfolio_demote",
        lambda _ws, _store, strategy, number: calls.append((strategy, number)),
    )
    deploy(ws, store, stage="live", state="live", ci90=(1.0, 2.0), live=((STRATEGY_ID, 1),))

    monitor.run_once(ws, store)

    assert calls == [(STRATEGY_ID, 1)]


def test_a_demotion_that_cannot_be_carried_out_does_not_end_the_pass(
    ws: Workspace, store: StateStore
) -> None:
    """The real demotion redeploys, and a workspace with no generated impl cannot."""
    deploy(ws, store, stage="live", state="live", ci90=(1.0, 2.0), live=((STRATEGY_ID, 1),))

    outcomes = monitor.run_once(ws, store)

    assert "no implementation to run" in str(_version_outcome(outcomes).skipped)
    assert len(outcomes) == 3


def test_the_paper_window_is_the_floor_the_plan_set() -> None:
    """Five days absolute against five one-day horizons: the same, and the longer wins."""
    longer = [
        PLAN["gates"][0],
        {
            "id": "paper_forward",
            "stage": "paper",
            "params": {"min_duration": "5d", "horizon_mult": 12.0},
            "rationale": "twelve horizons",
        },
        PLAN["gates"][2],
    ]

    assert monitor.paper_window_s(plan(), hypothesis()) == 5 * 86_400
    assert monitor.paper_window_s(plan(gates=longer), hypothesis()) == 12 * 86_400


def test_a_plan_whose_paper_gates_state_no_duration_leaves_the_window_unknown() -> None:
    bare = [
        PLAN["gates"][0],
        {"id": "paper_forward", "stage": "paper", "params": {}, "rationale": "no floor"},
        PLAN["gates"][2],
    ]

    assert monitor.paper_window_s(plan(gates=bare), hypothesis()) is None


def test_halting_writes_the_flag_an_operator_clears_by_hand(ws: Workspace) -> None:
    write_yaml(portfolio(), ws.path("portfolio.yaml"))

    monitor.halt(ws, portfolio_files.read(ws), "live")

    from kanso.schemas import Portfolio

    written = load_yaml(Portfolio, ws.path("portfolio.yaml"))
    assert written.stages.live.kill_switch is True
    assert written.stages.paper.kill_switch is False


def test_an_outcome_renders_as_one_json_object(ws: Workspace, store: StateStore) -> None:
    deploy(ws, store)

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    stage = _stage_outcome(outcomes, "paper").payload()
    assert stage["exposure"] is not None
    assert stage["strategy"] is None
    judged = _version_outcome(outcomes).payload()
    assert judged["strategy"] == STRATEGY_ID
    assert judged["actions"] == ["promotable"]
    assert judged["gates"][0]["id"] == "paper_forward"


def test_a_stage_outcomes_subject_is_the_stage(ws: Workspace, store: StateStore) -> None:
    deploy(ws, store)

    outcomes = monitor.run_once(ws, store, demote=Recorder())

    assert _stage_outcome(outcomes, "live").subject == "live"
    assert _version_outcome(outcomes).subject == "demo_mr@1"


def test_the_pass_reports_both_stages_even_when_nothing_is_deployed(
    ws: Workspace, store: StateStore
) -> None:
    write_yaml(portfolio(), ws.path("portfolio.yaml"))

    outcomes = monitor.run_once(ws, store)

    assert [outcome.stage for outcome in outcomes] == ["paper", "live"]
    assert all(outcome.exposure is not None for outcome in outcomes)


def test_capital_of_zero_leaves_no_room_for_a_position(ws: Workspace, store: StateStore) -> None:
    """A stage funded with nothing is over its limit the moment it holds anything."""
    deploy(
        ws,
        store,
        stage="live",
        state="live",
        ci90=(1.0, 2.0),
        positions=(position(qty=1.0, price=1.0),),
        live=((STRATEGY_ID, 1),),
        live_capital=0.0,
    )

    outcomes = monitor.run_once(ws, store)

    stage = _stage_outcome(outcomes, "live")
    assert stage.exposure is not None and stage.exposure.max_gross == 0.0
    assert stage.actions == ("halted",)


def test_the_default_paper_stage_capital_is_what_the_limits_are_taken_of(
    ws: Workspace, store: StateStore
) -> None:
    deploy(ws, store)

    exposure = _stage_outcome(monitor.run_once(ws, store, demote=Recorder()), "paper").exposure

    assert exposure is not None
    assert exposure.capital == CAPITAL
    assert exposure.max_gross == CAPITAL
