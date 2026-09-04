"""The band the paper and live gates read: reused where it was recorded, measured where not."""

from __future__ import annotations

from typing import Any

import pytest

from kanso.criteria import GateContext, gates
from kanso.data.manifest import catalog_path
from kanso.errors import PreconditionError
from kanso.nautilus import backtest
from kanso.schemas import Certificate
from kanso.state import StateStore
from kanso.strategy import impl
from kanso.strategy.composition import REPLICATIONS, _replications, compose, expectation
from kanso.workspace import Workspace
from tests.research.conftest import DOCUMENT, HYP_ID

from .conftest import (
    BOOTSTRAP_GATE,
    CERT_GATES,
    FILTER_ID,
    VARYING,
    a_certificate,
    certified_filter,
    certified_sleeve,
    pinned,
)

RECORDED: dict[str, Any] = {
    "id": "bootstrap",
    "stage": "cert",
    "params": {"n": 400},
    "evidence": {
        "objective": "wf_sharpe_net",
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
    a_sleeve_certificate(ws, store, RECORDED)

    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.ci90 == (1.5, 2.5), "recorded once, read back, never recomputed"
    assert expectation.mdd_p95 == 3.25
    assert expectation.value > 0, "the objective is still measured on the run"


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
            capital=100_000.0,
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
