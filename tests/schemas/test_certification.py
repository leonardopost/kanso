"""The certification plan and the certificate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import Certificate, CertificationPlan, resolve_venue_model

SHA = "b" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)

PLAN: dict[str, Any] = {
    "schema": 1,
    "hyp_id": "demo_mr",
    "plan_version": 1,
    "planned_at": NOW,
    "planned_by": "some-frontier-model",
    "inputs": {
        "hypothesis_sha": SHA,
        "construct": {"id": "sleeve"},
        "data_availability": {"types": ["bar"], "spans": {}},
        "n_trials": 30,
    },
    "gates": [
        {
            "id": "embargoed_window",
            "stage": "cert",
            "params": {"min_fraction": 0.5},
            "rationale": "r",
        },
        {"id": "paper_forward", "stage": "paper", "params": {}, "rationale": "r"},
        {"id": "live_drift", "stage": "live", "params": {}, "rationale": "r"},
    ],
    "excluded": [{"id": "capacity_vs_adv", "reason": "no volume"}],
}

CERT: dict[str, Any] = {
    "schema": 1,
    "hyp_id": "demo_mr",
    "strategy_sha": SHA,
    "nautilus_version": "1.231.0",
    "venue_model": resolve_venue_model("XNAS", max_leverage=1.0),
    "snapshot_id": "s1",
    "criteria_version": "0.1.0",
    "plan_version": 2,
    "construct": {"id": "sleeve"},
    "objective": {"id": "wf_sharpe_net", "value": 1.2, "se": 0.3},
    "gates": [
        {"id": "embargoed_window", "stage": "cert", "params": {}, "evidence": {}, "pass": True},
    ],
    "n_trials": 30,
    "verdict": "pass",
    "created_at": NOW,
}


def test_a_plan_reaches_every_stage() -> None:
    plan = CertificationPlan.model_validate(PLAN)
    assert [g.id for g in plan.stage_gates("cert")] == ["embargoed_window"]
    assert plan.inputs.construct.id == "sleeve"


@pytest.mark.parametrize("stage", ["cert", "paper", "live"])
def test_a_plan_missing_a_stage_is_refused(stage: str) -> None:
    gates = [g for g in PLAN["gates"] if g["stage"] != stage]
    with pytest.raises(ValidationError) as caught:
        CertificationPlan.model_validate({**PLAN, "gates": gates})
    assert f"no {stage} gate" in caught.value.message


def test_a_plan_names_each_gate_once() -> None:
    with pytest.raises(ValidationError, match="twice"):
        CertificationPlan.model_validate({**PLAN, "gates": [*PLAN["gates"], PLAN["gates"][0]]})


def test_a_gate_is_included_or_excluded_but_not_both() -> None:
    with pytest.raises(ValidationError, match="both included and excluded"):
        CertificationPlan.model_validate(
            {**PLAN, "excluded": [{"id": "live_drift", "reason": "x"}]}
        )


def test_a_rationale_is_bounded() -> None:
    gates = [{**PLAN["gates"][0], "rationale": "x" * 201}, *PLAN["gates"][1:]]
    with pytest.raises(ValidationError, match="rationale"):
        CertificationPlan.model_validate({**PLAN, "gates": gates})


def test_an_empty_plan_is_refused() -> None:
    with pytest.raises(ValidationError, match="gates"):
        CertificationPlan.model_validate({**PLAN, "gates": []})


def test_the_verdict_follows_the_gates() -> None:
    assert Certificate.model_validate(CERT).verdict == "pass"
    failing = [{**CERT["gates"][0], "pass": False}]
    with pytest.raises(ValidationError) as caught:
        Certificate.model_validate({**CERT, "gates": failing})
    assert "embargoed_window" in caught.value.message
    assert (
        Certificate.model_validate({**CERT, "gates": failing, "verdict": "fail"}).verdict == "fail"
    )


def test_a_skipped_gate_does_not_decide_the_verdict() -> None:
    gates = [
        *CERT["gates"],
        {
            "id": "book_correlation",
            "stage": "cert",
            "params": {},
            "evidence": {},
            "pass": True,
            "skipped": "nothing deployed",
        },
    ]
    assert Certificate.model_validate({**CERT, "gates": gates}).verdict == "pass"


def test_the_file_name_carries_the_plan_and_the_engine() -> None:
    cert = Certificate.model_validate(CERT)
    assert cert.filename() == "bbbbbbb-30-p2-e1.231.0.yaml"
    assert cert.source_filename() == "bbbbbbb.py"


def test_a_span_is_ordered() -> None:
    inputs = {
        **PLAN["inputs"],
        "data_availability": {
            "types": ["bar"],
            "spans": {"bar": {"start": "2025-01-02T00:00:00Z", "end": "2024-01-02T00:00:00Z"}},
        },
    }
    with pytest.raises(ValidationError, match="before start"):
        CertificationPlan.model_validate({**PLAN, "inputs": inputs})


def test_a_certificate_names_each_gate_once() -> None:
    with pytest.raises(ValidationError, match="twice"):
        Certificate.model_validate({**CERT, "gates": [*CERT["gates"], CERT["gates"][0]]})


def test_a_skipped_gate_may_not_be_recorded_as_failing() -> None:
    gate = {**CERT["gates"][0], "pass": False, "skipped": "no data"}
    with pytest.raises(ValidationError, match="skipped gate passes"):
        Certificate.model_validate({**CERT, "gates": [gate], "verdict": "fail"})


def test_the_certified_construct_is_exposed_under_its_own_name() -> None:
    assert Certificate.model_validate(CERT).construct.id == "sleeve"
