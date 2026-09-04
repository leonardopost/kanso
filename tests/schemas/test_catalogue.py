"""The construct catalogue and the criteria toolbox."""

from __future__ import annotations

from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import ConstructItem, CriteriaItem, parse_bound

CONSTRUCT: dict[str, Any] = {
    "id": "filter",
    "description": "conditioning rule that gates a host's entries",
    "needs_host": "sleeve",
    "objective_mode": "relative",
    "params": {"scope": ["time", "instrument"]},
    "runnable": True,
    "impl": "kanso.classify.constructs.filter",
}

GATE: dict[str, Any] = {
    "id": "embargoed_window",
    "kind": "gate",
    "stage": "cert",
    "required": True,
    "meaningful_when": "informative once a certification window exists",
    "params": {"min_fraction": "float", "length": "duration"},
    "ranges": {"min_fraction": [0.0, 1.0], "length": ["1 * horizon", "100 * horizon"]},
    "impl": "kanso.criteria.gates.embargoed_window",
}

OBJECTIVE: dict[str, Any] = {
    "id": "wf_sharpe_net",
    "kind": "objective",
    "applies": {"objective_mode": "absolute", "horizon": {"min": "1d"}},
    "priority": 20,
    "params": {"min_delta": "float", "k_se": "float"},
    "ranges": {"min_delta": [0.0, 1000.0], "k_se": [0.5, 3.0]},
    "impl": "kanso.criteria.objectives.wf_sharpe_net",
}


def test_a_construct_validates() -> None:
    item = ConstructItem.model_validate(CONSTRUCT)
    assert item.needs_host == "sleeve"
    assert item.runnable


def test_a_hostless_construct_is_absolute() -> None:
    with pytest.raises(ValidationError, match="nothing to be relative to"):
        ConstructItem.model_validate({**CONSTRUCT, "needs_host": "none"})


def test_a_construct_impl_is_a_dotted_path() -> None:
    with pytest.raises(ValidationError, match="impl"):
        ConstructItem.model_validate({**CONSTRUCT, "impl": "filter"})


def test_a_gate_validates() -> None:
    item = CriteriaItem.model_validate(GATE)
    assert item.stage == "cert"
    assert item.required


def test_an_objective_validates() -> None:
    item = CriteriaItem.model_validate(OBJECTIVE)
    assert item.priority == 20
    assert item.applies is not None
    assert item.applies.horizon is not None
    assert item.applies.horizon.min == "1d"


def test_a_gate_declares_a_stage_and_says_when_it_is_informative() -> None:
    with pytest.raises(ValidationError, match="stage"):
        CriteriaItem.model_validate({**GATE, "stage": None})
    with pytest.raises(ValidationError, match="meaningful_when"):
        CriteriaItem.model_validate({**GATE, "meaningful_when": None})


def test_a_gate_carries_no_applicability() -> None:
    with pytest.raises(ValidationError, match="applies"):
        CriteriaItem.model_validate({**GATE, "applies": {"objective_mode": "absolute"}})
    with pytest.raises(ValidationError, match="priority"):
        CriteriaItem.model_validate({**GATE, "priority": 1})


def test_an_objective_is_not_staged_and_is_not_required() -> None:
    with pytest.raises(ValidationError, match="stage"):
        CriteriaItem.model_validate({**OBJECTIVE, "stage": "cert"})
    with pytest.raises(ValidationError, match="required"):
        CriteriaItem.model_validate({**OBJECTIVE, "required": True})


def test_an_objective_declares_applicability_and_priority() -> None:
    with pytest.raises(ValidationError, match="applies"):
        CriteriaItem.model_validate({**OBJECTIVE, "applies": None})
    with pytest.raises(ValidationError, match="priority"):
        CriteriaItem.model_validate({**OBJECTIVE, "priority": None})


def test_a_range_belongs_to_a_declared_param() -> None:
    with pytest.raises(ValidationError, match="not a declared param"):
        CriteriaItem.model_validate({**GATE, "ranges": {**GATE["ranges"], "other": [0, 1]}})


def test_a_numeric_param_needs_a_range() -> None:
    with pytest.raises(ValidationError, match="needs a range"):
        CriteriaItem.model_validate({**GATE, "ranges": {"min_fraction": [0.0, 1.0]}})


def test_a_boolean_param_needs_none() -> None:
    item = CriteriaItem.model_validate({**GATE, "params": {"strict": "bool"}, "ranges": {}})
    assert item.params == {"strict": "bool"}


def test_a_gate_may_have_no_params_at_all() -> None:
    item = CriteriaItem.model_validate({**GATE, "params": {}, "ranges": {}})
    assert item.ranges == {}


def test_bounds_are_ordered() -> None:
    with pytest.raises(ValidationError, match="below lower bound"):
        CriteriaItem.model_validate({**GATE, "ranges": {**GATE["ranges"], "min_fraction": [1, 0]}})
    with pytest.raises(ValidationError, match="below lower bound"):
        CriteriaItem.model_validate(
            {**GATE, "ranges": {**GATE["ranges"], "length": ["10 * horizon", "1 * horizon"]}}
        )


@pytest.mark.parametrize(
    ("bound", "expected"),
    [
        (0.5, (0.5, None)),
        (10, (10.0, None)),
        ("1d", (86400.0, "seconds")),
        ("2 * horizon", (2.0, "horizon")),
        ("0.5 * resolution", (0.5, "resolution")),
        ("100 * history_days", (100.0, "history_days")),
        ("1 * folds", (1.0, "folds")),
    ],
)
def test_the_bound_grammar(bound: float | str, expected: tuple[float, str | None]) -> None:
    assert parse_bound(bound) == expected


@pytest.mark.parametrize("bound", ["horizon", "2 * lookback", "2x horizon", "", "1.5.2", True])
def test_a_bound_outside_the_grammar_is_refused(bound: float | str) -> None:
    with pytest.raises(ValidationError, match="range bound"):
        parse_bound(bound, "ranges.length")


def test_applicability_bounds_are_ordered() -> None:
    with pytest.raises(ValidationError, match="not above min"):
        CriteriaItem.model_validate(
            {**OBJECTIVE, "applies": {"horizon": {"min": "1d", "max": "1h"}}}
        )
    with pytest.raises(ValidationError, match="not above min"):
        CriteriaItem.model_validate(
            {**OBJECTIVE, "applies": {"universe_size": {"min": 10, "max": 2}}}
        )


def test_every_applicability_clause_is_accepted() -> None:
    applies = {
        "mechanism": ["momentum", "carry"],
        "objective_mode": "absolute",
        "horizon": {"min": "1d", "max": "30d"},
        "resolution": {"max": "1h"},
        "universe_size": {"min": 1, "max": 500},
        "data_requirements": ["bar"],
        "history_days": {"min": 250},
    }
    item = CriteriaItem.model_validate({**OBJECTIVE, "applies": applies})
    assert item.applies is not None
    assert item.applies.universe_size is not None
    assert item.applies.universe_size.max == 500


def test_an_unknown_applicability_clause_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        CriteriaItem.model_validate({**OBJECTIVE, "applies": {"sector": ["tech"]}})
