"""`strategy.yaml`: versions, stages and expectations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import StrategyFile, resolve_venue_model

SHA = "c" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)

PINS: dict[str, Any] = {
    "kanso_version": "0.1.0",
    "nautilus_version": "1.231.0",
    "criteria_version": "0.1.0",
    "plan_version": 1,
    "snapshot_id": "s1",
    "venue_model": resolve_venue_model("XNAS", max_leverage=1.0),
}
EXPECTATION: dict[str, Any] = {
    "objective_id": "wf_sharpe_net",
    "value": 1.1,
    "ci90": [0.4, 1.8],
    "mdd_p95": 9.0,
    "window": {"start": "2025-01-06", "end": "2025-05-30"},
}


def version(number: int, state: str, **changes: Any) -> dict[str, Any]:
    return {
        "version": number,
        "sleeve": {"hyp_id": "demo_mr", "strategy_sha": SHA},
        "attached": [],
        "config": {"lookback": 20},
        "pins": PINS,
        "expectation": EXPECTATION,
        "state": state,
        "created_at": NOW,
        **changes,
    }


def build(*versions: dict[str, Any]) -> StrategyFile:
    return StrategyFile.model_validate({"schema": 1, "id": "demo_mr", "versions": list(versions)})


def test_a_single_version() -> None:
    strategy = build(version(1, "paper"))
    assert strategy.latest().version == 1
    assert strategy.deployed("paper") is not None
    assert strategy.deployed("live") is None


def test_versions_are_numbered_in_order() -> None:
    with pytest.raises(ValidationError, match="numbered"):
        build(version(1, "retired"), version(3, "paper"))
    with pytest.raises(ValidationError, match="numbered"):
        build(version(2, "paper"), version(1, "retired"))


def test_a_stage_holds_one_version() -> None:
    with pytest.raises(ValidationError, match="paper stage"):
        build(version(1, "paper"), version(2, "promotable"))
    with pytest.raises(ValidationError, match="live"):
        build(version(1, "live"), version(2, "live"))
    assert build(version(1, "live"), version(2, "paper")).latest().state == "paper"


def test_the_sleeve_is_not_also_attached() -> None:
    attached = [{"hyp_id": "demo_mr", "strategy_sha": SHA, "construct": "filter"}]
    with pytest.raises(ValidationError, match="sleeve hypothesis"):
        build(version(1, "paper", attached=attached))


def test_an_attached_hypothesis_appears_once() -> None:
    attached = [
        {"hyp_id": "gate_hyp", "strategy_sha": SHA, "construct": "filter"},
        {"hyp_id": "gate_hyp", "strategy_sha": SHA, "construct": "exit"},
    ]
    with pytest.raises(ValidationError, match="twice"):
        build(version(1, "paper", attached=attached))


def test_the_attached_construct_is_exposed_under_its_own_name() -> None:
    attached = [{"hyp_id": "gate_hyp", "strategy_sha": SHA, "construct": "filter"}]
    strategy = build(version(1, "paper", attached=attached))
    assert strategy.latest().attached[0].construct == "filter"


def test_the_confidence_interval_is_ordered() -> None:
    with pytest.raises(ValidationError, match="ci90"):
        build(version(1, "paper", expectation={**EXPECTATION, "ci90": [1.8, 0.4]}))


def test_a_strategy_has_at_least_one_version() -> None:
    with pytest.raises(ValidationError, match="versions"):
        build()
