"""`portfolio.yaml`: the two stages and their limits."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import Portfolio

NOW = datetime(2026, 1, 1, tzinfo=UTC)

BASE: dict[str, Any] = {
    "schema": 1,
    "stages": {
        "paper": {
            "exec": "sandbox",
            "data": "replay",
            "speed": 1,
            "capital": 100000,
            "kill_switch": False,
            "strategies": [],
        },
        "live": {
            "exec": "sandbox",
            "data": "replay",
            "speed": 1,
            "capital": 0,
            "kill_switch": False,
            "strategies": [],
        },
    },
    "limits": {
        "max_gross_pct": 100,
        "max_net_pct": 100,
        "per_strategy_max_pct": 40,
        "daily_loss_pct": 3,
    },
}


def build(**changes: Any) -> Portfolio:
    return Portfolio.model_validate({**BASE, **changes})


def test_the_template_is_valid() -> None:
    portfolio = build()
    assert portfolio.stages.paper.exec == "sandbox"
    assert portfolio.stages.live.capital == 0
    assert portfolio.venues is None


def test_a_third_stage_is_refused() -> None:
    stages = {**BASE["stages"], "shadow": BASE["stages"]["paper"]}
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(stages=stages)


def test_a_stage_holds_one_version_of_a_strategy() -> None:
    deployments = [
        {"id": "demo_mr", "version": 1, "capital": 100, "joined_at": NOW},
        {"id": "demo_mr", "version": 2, "capital": 100, "joined_at": NOW},
    ]
    stages = {**BASE["stages"], "paper": {**BASE["stages"]["paper"], "strategies": deployments}}
    with pytest.raises(ValidationError, match="at most one version"):
        build(stages=stages)


def test_allocated_capital_stays_inside_the_stage() -> None:
    deployments = [{"id": "demo_mr", "version": 1, "capital": 200000, "joined_at": NOW}]
    stages = {**BASE["stages"], "paper": {**BASE["stages"]["paper"], "strategies": deployments}}
    with pytest.raises(ValidationError, match="allocate"):
        build(stages=stages)


def test_a_deployment_respects_the_per_strategy_limit() -> None:
    deployments = [{"id": "demo_mr", "version": 1, "capital": 50000, "joined_at": NOW}]
    stages = {**BASE["stages"], "paper": {**BASE["stages"]["paper"], "strategies": deployments}}
    with pytest.raises(ValidationError, match="per_strategy_max_pct"):
        build(stages=stages)
    deployments[0]["capital"] = 40000
    assert build(stages=stages).stages.paper.strategies[0].capital == 40000


def test_venue_overrides_are_optional_and_typed() -> None:
    portfolio = build(venues={"XNAS": {"account": "margin", "currency": "USD"}})
    assert portfolio.venues is not None
    assert portfolio.venues["XNAS"].currency == "USD"
    with pytest.raises(ValidationError, match="venues"):
        build(venues={"xnas": {"currency": "USD"}})
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(venues={"XNAS": {"leverage": 2}})


def test_speed_and_capital_are_non_negative() -> None:
    stages = {**BASE["stages"], "paper": {**BASE["stages"]["paper"], "speed": -1}}
    with pytest.raises(ValidationError, match="speed"):
        build(stages=stages)
