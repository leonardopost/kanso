"""The band the paper and live gates read: reused where it was recorded, measured where not."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kanso.criteria import GateContext, gates
from kanso.criteria.objectives import wf_contribution_bps, wf_sharpe_net, wf_sharpe_vs_hold
from kanso.data.manifest import catalog_path
from kanso.errors import PreconditionError
from kanso.nautilus import backtest
from kanso.portfolio import files as portfolio_files
from kanso.schemas import Certificate
from kanso.state import StateStore
from kanso.strategy import impl
from kanso.strategy.composition import (
    REPLICATIONS,
    _measure,
    _replications,
    _struck_at,
    compose,
    expectation,
    restruck,
)
from kanso.workspace import Workspace
from tests.research.conftest import DOCUMENT, HYP_ID

from .conftest import (
    BOOTSTRAP_GATE,
    CERT_GATES,
    FILTER_ID,
    PAPER_SHARE,
    VARYING,
    a_certificate,
    certified_filter,
    certified_sleeve,
    pinned,
    whole_paper_stage,
)

RECORDED: dict[str, Any] = {
    "id": "bootstrap",
    "stage": "cert",
    "params": {"n": 400},
    "evidence": {
        "objective": "wf_sharpe_net",
        "statistic": "trade_sharpe",
        "objective_ci90": [1.5, 2.5],
        "mdd_p95": 3.25,
        "limit_pct": 40.0,
        "n": 400,
    },
    "pass": True,
}
"""Evidence no resampling of this workspace's data would produce, so reuse is visible."""

PASSED: dict[str, Any] = {
    "id": "embargoed_window",
    "stage": "cert",
    "params": {"min_fraction": 0.0},
    "evidence": {},
    "pass": True,
}


def a_sleeve_certificate(ws: Workspace, store: StateStore, gate: dict[str, Any]) -> Certificate:
    """A passing sleeve certificate carrying exactly this bootstrap gate."""
    return a_certificate(
        ws,
        store,
        HYP_ID,
        VARYING,
        construct={"id": "sleeve"},
        objective_id="wf_sharpe_net",
        gates=[PASSED, gate],
        doc=DOCUMENT,
    )


def test_the_certificates_own_bootstrap_evidence_becomes_the_band(
    ws: Workspace, store: StateStore
) -> None:
    whole_paper_stage(ws)
    a_sleeve_certificate(ws, store, RECORDED)

    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.capital == 100_000.0, "struck at the capital the certificate measured at"
    assert expectation.ci90 == (1.5, 2.5), "recorded once, read back, never recomputed"
    assert expectation.mdd_p95 == 3.25
    assert expectation.value > 0, "the objective is still measured on the run"


def test_a_band_recorded_at_another_capital_is_not_reused(ws: Workspace, store: StateStore) -> None:
    """The template's paper stage funds a strategy with 40% of its 100,000, and a band the
    certificate struck at 100,000 is not the band of a version run at 40,000."""
    a_sleeve_certificate(ws, store, RECORDED)

    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.capital == PAPER_SHARE
    assert expectation.ci90 != (1.5, 2.5)


def test_a_band_that_does_not_name_its_statistic_is_not_reused(
    ws: Workspace, store: StateStore
) -> None:
    """A certificate written before the bootstrap named what it resampled banded a
    contribution by per-trade edges; no band without the name is read as this one."""
    whole_paper_stage(ws)
    evidence = {key: value for key, value in RECORDED["evidence"].items() if key != "statistic"}
    a_sleeve_certificate(ws, store, {**RECORDED, "evidence": evidence})

    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.ci90 != (1.5, 2.5)


def test_an_expectation_records_its_error_its_periods_and_its_capital(
    ws: Workspace, store: StateStore
) -> None:
    """What a stage strikes its own band from, measured on the run at the paper share."""
    certificate = certified_sleeve(ws, store)
    version = compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    hyp = pinned(ws, store)

    run, _ = _measure(ws, manifest, version, hyp, PAPER_SHARE)

    held = version.expectation
    assert held.capital == PAPER_SHARE
    assert manifest.sleeve.config["capital"] == 100_000.0, "the manifest keeps the book"
    assert (held.value, held.se) == wf_sharpe_net.compute(run, ws.config.research.folds)
    assert held.periods == len(run.returns) > 0
    assert certificate.verdict == "pass"


def test_a_contribution_is_expected_in_basis_points_of_the_share_it_runs_on(
    ws: Workspace, store: StateStore
) -> None:
    """The band is the contribution's own, around its value, at the share the paper stage
    funds the version with: no test held composition to that before 0.17."""
    contributing = {
        **DOCUMENT,
        "objective": {"id": "wf_contribution_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
    }
    a_certificate(
        ws,
        store,
        HYP_ID,
        VARYING,
        construct={"id": "sleeve"},
        objective_id="wf_contribution_bps",
        gates=[PASSED],
        doc=contributing,
    )

    version = compose(ws, store, HYP_ID)

    run, _ = _measure(ws, impl.read_manifest(ws, HYP_ID, 1), version, pinned(ws, store), 40_000.0)
    held = version.expectation
    low, high = held.ci90
    assert held.objective_id == "wf_contribution_bps"
    assert held.value == wf_contribution_bps.compute(run, ws.config.research.folds)[0]
    assert low < held.value < high
    assert run.capital == held.capital == PAPER_SHARE


def test_a_workspace_without_a_portfolio_strikes_its_expectation_at_the_book(
    ws: Workspace, store: StateStore
) -> None:
    ws.path("portfolio.yaml").unlink()
    certified_sleeve(ws, store)

    assert compose(ws, store, HYP_ID).expectation.capital == 100_000.0


@pytest.mark.parametrize(
    ("budget", "stage_capital", "struck"),
    [
        (0.0, 100_000.0, PAPER_SHARE),
        (0.0, 0.0, 100_000.0),
        (50_000.0, 100_000.0, 100_000.0),
        (30_000.0, 100_000.0, PAPER_SHARE),
    ],
)
def test_a_version_is_struck_at_the_paper_share_unless_the_stage_could_not_hold_it(
    ws: Workspace, budget: float, stage_capital: float, struck: float
) -> None:
    """A stage that would fund nothing, or less than the version's budgets, is one `deploy`
    refuses or blocks; the version is measured at its book until a stage can hold it."""
    held = portfolio_files.read(ws)
    paper = held.stages.paper.model_copy(update={"capital": stage_capital})
    portfolio_files.write(ws, portfolio_files.with_stage(held, "paper", paper))
    manifest = SimpleNamespace(components=[SimpleNamespace(config={"sizing_budget": budget})])

    assert _struck_at(ws, HYP_ID, manifest, 100_000.0) == struck  # type: ignore[arg-type]


def test_a_version_is_struck_again_at_another_capital_on_request(
    ws: Workspace, store: StateStore
) -> None:
    """What `deploy` asks of a version a stage funds with another share."""
    certified_sleeve(ws, store)
    version = compose(ws, store, HYP_ID)

    again = restruck(ws, store, HYP_ID, version, 20_000.0)

    assert again.capital == 20_000.0
    assert again.periods == version.expectation.periods
    assert again.objective_id == version.expectation.objective_id
    assert restruck(ws, store, HYP_ID, version, PAPER_SHARE) == version.expectation


def test_a_plan_without_the_gate_has_its_band_measured_instead(
    ws: Workspace, store: StateStore
) -> None:
    certified_sleeve(ws, store)

    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.ci90[0] < expectation.ci90[1], "a resampling, not a point estimate"
    assert expectation.ci90 != (expectation.value, expectation.value)


def test_a_plan_with_the_gate_agrees_with_what_the_certificate_recorded(
    ws: Workspace, store: StateStore
) -> None:
    whole_paper_stage(ws)
    certificate = certified_sleeve(ws, store, gates=[*CERT_GATES, BOOTSTRAP_GATE])
    recorded = next(gate for gate in certificate.gates if gate.id == "bootstrap")

    expectation = compose(ws, store, HYP_ID).expectation

    assert recorded.skipped is None, "the certification window has trades to resample"
    assert list(expectation.ci90) == recorded.evidence["objective_ci90"]
    assert expectation.mdd_p95 == recorded.evidence["mdd_p95"]


def test_evidence_naming_another_objective_is_not_reused(ws: Workspace, store: StateStore) -> None:
    a_sleeve_certificate(
        ws, store, {**RECORDED, "evidence": {**RECORDED["evidence"], "objective": "net_edge_bps"}}
    )

    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.ci90 != (1.5, 2.5), "an interval of another statistic is not this band"
    assert expectation.mdd_p95 != 3.25


def test_a_skipped_bootstrap_is_not_reused_but_its_count_is(
    ws: Workspace, store: StateStore
) -> None:
    certificate = a_sleeve_certificate(
        ws,
        store,
        {
            "id": "bootstrap",
            "stage": "cert",
            "params": {"n": 250},
            "evidence": {},
            "pass": True,
            "skipped": "fewer than two closed trades",
        },
    )

    expectation = compose(ws, store, HYP_ID).expectation

    assert _replications(certificate) == 250, "the planner's count, even where it judged nothing"
    assert expectation.ci90[0] < expectation.ci90[1]


def test_a_certificate_with_no_bootstrap_gate_resamples_this_modules_own_count(
    ws: Workspace, store: StateStore
) -> None:
    certificate = certified_sleeve(ws, store)

    assert _replications(certificate) == REPLICATIONS


def test_an_attached_constructs_evidence_describes_another_subject_and_is_not_reused(
    ws: Workspace, store: StateStore
) -> None:
    whole_paper_stage(ws)
    certified_sleeve(ws, store)
    first = compose(ws, store, HYP_ID)
    certified_filter(
        ws,
        store,
        gates=[
            PASSED,
            {
                **RECORDED,
                "params": {"n": REPLICATIONS},
                "evidence": {**RECORDED["evidence"], "objective": "marginal_wf_sharpe"},
            },
        ],
    )

    second = compose(ws, store, FILTER_ID)

    assert second.expectation.ci90 != (1.5, 2.5), "the modifier's interval is not the book's"
    assert second.expectation.ci90 == first.expectation.ci90, "same run, same seed, same band"
    assert second.expectation.mdd_p95 == pytest.approx(first.expectation.mdd_p95)
    assert second.expectation.value == pytest.approx(first.expectation.value)


def test_a_measured_band_is_the_bootstrap_of_the_run_the_implementation_produces(
    ws: Workspace, store: StateStore
) -> None:
    certificate = certified_sleeve(ws, store)
    version = compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    source, attached = impl.sources(ws, manifest)
    hyp = pinned(ws, store)

    run = backtest.run(
        backtest.RunRequest(
            hyp=hyp,
            strategy_source=source,
            window=(hyp.windows.certification.start, hyp.windows.certification.end),
            snapshot_id=version.pins.snapshot_id,
            venue_model=version.pins.venue_model.model_dump(),
            capital=PAPER_SHARE,
            modifiers=attached,
        ),
        catalog_path(ws),
    ).run
    result = gates()["bootstrap"].evaluate(
        GateContext(
            hyp=hyp,
            construct="sleeve",
            stage="cert",
            params={"n": REPLICATIONS},
            window=(hyp.windows.certification.start, hyp.windows.certification.end),
            run=run,
            research_folds=ws.config.research.folds,
            snapshot_id=version.pins.snapshot_id,
            strategy_sha=certificate.strategy_sha,
        )
    )

    assert list(version.expectation.ci90) == result.evidence["objective_ci90"]
    assert version.expectation.mdd_p95 == result.evidence["mdd_p95"]


def test_a_sleeve_with_no_objective_has_nothing_to_expect_of_it(
    ws: Workspace, store: StateStore
) -> None:
    certificate = certified_sleeve(ws, store)
    version = compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    unclassified = pinned(ws, store).model_copy(update={"objective": None})

    with pytest.raises(PreconditionError, match="carries no objective"):
        expectation(ws, manifest, version, unclassified, 100_000.0, certificate)


def test_a_sleeve_measured_against_a_hold_expects_a_difference_from_it(
    ws: Workspace, store: StateStore
) -> None:
    """The expectation's run has the hold beside it, and its band is one of differences."""
    held = {
        **DOCUMENT,
        "benchmark": {"hold": "first_leg"},
        "objective": {"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
    }
    certificate = a_certificate(
        ws,
        store,
        HYP_ID,
        VARYING,
        construct={"id": "sleeve"},
        objective_id="wf_sharpe_vs_hold",
        gates=[PASSED],
        doc=held,
    )

    version = compose(ws, store, HYP_ID)

    manifest = impl.read_manifest(ws, HYP_ID, 1)
    hyp = pinned(ws, store)
    run, hold = _measure(ws, manifest, version, hyp, PAPER_SHARE)
    assert hold is not None and len(hold.fills) == 1 and hold.trades == ()
    folds = ws.config.research.folds
    assert version.expectation.objective_id == "wf_sharpe_vs_hold"
    assert version.expectation.value == pytest.approx(
        wf_sharpe_vs_hold.compute(run, folds, benchmark=hold)[0]
    )
    alone = gates()["bootstrap"].evaluate(
        GateContext(
            hyp=hyp.model_copy(
                update={"objective": hyp.objective.model_copy(update={"id": "wf_sharpe_net"})}
            ),
            construct="sleeve",
            stage="cert",
            params={"n": REPLICATIONS},
            window=(hyp.windows.certification.start, hyp.windows.certification.end),
            run=run,
            research_folds=folds,
            snapshot_id=version.pins.snapshot_id,
            strategy_sha=certificate.strategy_sha,
        )
    )
    level = wf_sharpe_net.compute(hold, folds)[0]
    assert list(version.expectation.ci90) == pytest.approx(
        [bound - level for bound in alone.evidence["objective_ci90"]]
    )
