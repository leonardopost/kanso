"""How a schema failure reaches the operator."""

from __future__ import annotations

import pytest

from kanso.errors import Exit, KansoError, ValidationError
from kanso.schemas import Hypothesis, RiskLimits, Windows
from tests.schemas.test_hypothesis import BASE


def test_direct_construction_raises_the_kanso_error() -> None:
    with pytest.raises(ValidationError) as caught:
        RiskLimits(max_position_pct=0, max_drawdown_pct=15, max_leverage=1)
    assert isinstance(caught.value, KansoError)
    assert caught.value.code is Exit.VALIDATION
    assert "max_position_pct" in caught.value.message


def test_a_nested_failure_carries_its_path() -> None:
    with pytest.raises(ValidationError) as caught:
        Hypothesis.model_validate(
            {**BASE, "risk_limits": {**BASE["risk_limits"], "max_leverage": -1}}
        )
    assert "risk_limits.max_leverage" in caught.value.message


def test_several_failures_are_reported_together() -> None:
    with pytest.raises(ValidationError) as caught:
        Hypothesis.model_validate({**BASE, "id": "X", "mechanism": "vibes"})
    assert "id" in caught.value.message
    assert "mechanism" in caught.value.message
    assert ";" in caught.value.message


def test_a_directly_built_nested_model_still_raises_kansos_error() -> None:
    with pytest.raises(ValidationError, match="before start"):
        Windows(
            research={"start": "2024-12-31", "end": "2024-01-02"},
            certification={"start": "2025-01-06", "end": "2025-05-30"},
            forward={"start": "2025-06-02"},
        )
