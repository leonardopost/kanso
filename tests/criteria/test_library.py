"""The shipped catalogue, its range grammar and the plan invariants it enforces."""

from __future__ import annotations

from typing import Any

import pytest

from kanso import criteria
from kanso.criteria import catalogue, check_params, criteria_version, gates, validate_plan
from kanso.criteria import library as lib
from kanso.criteria.gates import PENDING
from kanso.criteria.library import resolve_bound
from kanso.errors import ValidationError
from kanso.schemas import CertificationPlan, CriteriaItem
from tests.criteria.builders import make_hyp

CACHED = (lib._catalogue, lib.criteria_version, lib._objectives, lib._gates)


@pytest.fixture
def elsewhere(tmp_path: Any, monkeypatch: Any) -> Any:
    """A library directory of this test's own, with the shipped one's caches cleared."""
    monkeypatch.setattr(lib, "LIBRARY", tmp_path)
    for cached in CACHED:
        cached.cache_clear()
    yield tmp_path
    for cached in CACHED:
        cached.cache_clear()


def write_item(directory: Any, name: str, body: str) -> None:
    (directory / f"{name}.yaml").write_text(body, encoding="utf-8")


FOLDS = 4

REQUIRED_CERT_GATES = ("embargoed_window", "publication_lag")
"""Required cert gates this version can actually run.

`parity_replay` is required too, but has no implementation until replay sessions land, so
a plan is neither offered it nor held to it. `tests/criteria/test_pending_required.py`
pins that exemption and is what forces it to end.
"""

PLAN: dict[str, Any] = {
    "schema": 1,
    "hyp_id": "demo_mr",
    "plan_version": 1,
    "planned_at": "2025-01-02T00:00:00Z",
    "planned_by": "mock",
    "inputs": {
        "hypothesis_sha": "0" * 64,
        "construct": {"id": "sleeve"},
        "data_availability": {"types": ["bar"], "spans": {}},
        "n_trials": 40,
    },
    "gates": [
        {
            "id": "embargoed_window",
            "stage": "cert",
            "params": {"min_fraction": 0.5},
            "rationale": "out of sample",
        },
        {
            "id": "publication_lag",
            "stage": "cert",
            "params": {"tolerance_s": 60.0},
            "rationale": "availability",
        },
        {"id": "bootstrap", "stage": "cert", "params": {"n": 1000}, "rationale": "path risk"},
        {
            "id": "walk_forward_consistency",
            "stage": "cert",
            "params": {"min_positive_folds": 3},
            "rationale": "recurrence",
        },
        {
            "id": "paper_forward",
            "stage": "paper",
            "params": {"min_duration": "30d", "horizon_mult": 10.0},
            "rationale": "forward evidence",
        },
        {"id": "live_drift", "stage": "live", "params": {}, "rationale": "expectation holds"},
    ],
    "excluded": [{"id": "book_correlation", "reason": "nothing deployed"}],
}


def plan(**changes: Any) -> dict[str, Any]:
    """The demo plan with a shallow field replaced."""
    return {**PLAN, **changes}


def without(gate_id: str) -> dict[str, Any]:
    """The demo plan with one gate removed."""
    return plan(gates=[g for g in PLAN["gates"] if g["id"] != gate_id])


def replacing(gate_id: str, **fields: Any) -> dict[str, Any]:
    """The demo plan with one gate's fields changed."""
    return plan(gates=[{**g, **fields} if g["id"] == gate_id else g for g in PLAN["gates"]])


# --- the catalogue ----------------------------------------------------------------


def test_the_catalogue_is_the_shipped_yaml() -> None:
    items = catalogue()
    assert len(items) == 21
    assert all(isinstance(item, CriteriaItem) for item in items.values())
    assert sum(1 for item in items.values() if item.kind == "objective") == 4


def test_every_item_tells_the_planner_when_it_is_informative() -> None:
    for item in catalogue().values():
        assert item.meaningful_when
        assert len(item.meaningful_when) <= 200


def test_every_gate_declares_its_stage_and_every_objective_its_priority() -> None:
    for item in catalogue().values():
        if item.kind == "gate":
            assert item.stage in ("card", "cert", "paper", "live")
            assert item.applies is None
        else:
            assert item.priority is not None
            assert item.applies is not None


def test_the_structural_invariants_are_the_only_required_gates() -> None:
    """Including the ones this version cannot yet run — the catalogue is the declaration,
    and what a plan is held to is a separate question `plannable` answers."""
    required = {item.id for item in catalogue().values() if item.required}
    assert required == {"strategy_integrity", *REQUIRED_CERT_GATES, *criteria.pending_required()}


def test_the_toolbox_reaches_every_stage() -> None:
    stages = {item.stage for item in catalogue().values() if item.kind == "gate"}
    assert stages == {"card", "cert", "paper", "live"}


def test_every_item_resolves_or_is_declared_pending() -> None:
    implemented = set(gates())
    declared = {item.id for item in catalogue().values() if item.kind == "gate"}
    assert declared - implemented == PENDING


def test_the_criteria_version_names_the_package_and_the_toolbox() -> None:
    version = criteria_version()
    package, _, digest = version.partition("+")
    assert package and len(digest) == 12
    assert criteria_version() == version


# --- the range grammar ------------------------------------------------------------


@pytest.mark.parametrize(
    ("bound", "expected"),
    [
        (3.0, 3.0),
        ("1d", 86_400.0),
        ("2 * horizon", 3_600.0),
        ("1 * resolution", 60.0),
        ("1 * history_days", 366.0),
        ("1 * folds", 4.0),
        ("0.5 * folds", 2.0),
    ],
)
def test_a_bound_is_resolved_on_the_hypothesis_own_scale(bound: Any, expected: float) -> None:
    assert resolve_bound(bound, make_hyp(), FOLDS) == pytest.approx(expected)


def test_a_fold_bound_follows_the_workspace_fold_count() -> None:
    item = catalogue()["walk_forward_consistency"]
    assert check_params(item, {"min_positive_folds": 6}, make_hyp(), 8) == []
    assert check_params(item, {"min_positive_folds": 6}, make_hyp(), 4) != []


def test_a_duration_parameter_is_measured_in_seconds() -> None:
    item = catalogue()["paper_forward"]
    assert check_params(item, {"min_duration": "30d"}, make_hyp(), FOLDS) == []
    (problem,) = check_params(item, {"min_duration": "400d"}, make_hyp(), FOLDS)
    assert "outside the range" in problem


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"nonsense": 1}, "declares no such parameter"),
        ({"n": 1.5}, "is not a int"),
        ({"n": 1}, "outside the range"),
    ],
)
def test_a_parameter_that_does_not_fit_is_named(params: Any, message: str) -> None:
    (problem,) = check_params(catalogue()["bootstrap"], params, make_hyp(), FOLDS)
    assert message in problem


def test_a_parameter_without_a_range_is_only_type_checked() -> None:
    item = CriteriaItem.model_validate(
        {
            "id": "example",
            "kind": "gate",
            "stage": "cert",
            "meaningful_when": "an example with a free-form parameter",
            "params": {"label": "str", "on": "bool"},
            "ranges": {},
            "impl": "kanso.criteria.gates.example",
        }
    )
    assert check_params(item, {"label": "x", "on": True}, make_hyp(), FOLDS) == []
    assert check_params(item, {"label": 1, "on": "yes"}, make_hyp(), FOLDS) != []


# --- plan validation --------------------------------------------------------------


def test_a_valid_plan_is_accepted() -> None:
    validate_plan(PLAN, make_hyp(), FOLDS)
    validate_plan(CertificationPlan.model_validate(PLAN), make_hyp(), FOLDS)


@pytest.mark.parametrize("gate_id", REQUIRED_CERT_GATES)
def test_a_plan_missing_a_required_gate_is_refused(gate_id: str) -> None:
    with pytest.raises(ValidationError, match=f"{gate_id} is a structural invariant"):
        validate_plan(without(gate_id), make_hyp(), FOLDS)


def test_a_card_stage_gate_is_not_required_of_a_plan() -> None:
    assert "strategy_integrity" not in {g["id"] for g in PLAN["gates"]}
    validate_plan(PLAN, make_hyp(), FOLDS)


def test_a_plan_that_does_not_reach_every_stage_is_refused() -> None:
    with pytest.raises(ValidationError, match="no live gate"):
        validate_plan(without("live_drift"), make_hyp(), FOLDS)


def test_a_gate_planned_at_the_wrong_stage_is_refused() -> None:
    with pytest.raises(ValidationError, match="runs at the cert stage, not paper"):
        validate_plan(replacing("bootstrap", stage="paper"), make_hyp(), FOLDS)


def test_a_parameter_outside_its_range_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"gates.bootstrap.n: .* outside the range"):
        validate_plan(replacing("bootstrap", params={"n": 5}), make_hyp(), FOLDS)


def test_an_unknown_gate_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not a gate in the toolbox"):
        validate_plan(
            plan(gates=[*PLAN["gates"], {"id": "vibes", "stage": "cert", "rationale": "hunch"}]),
            make_hyp(),
            FOLDS,
        )


def test_an_objective_planned_as_a_gate_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not a gate in the toolbox"):
        validate_plan(
            plan(
                gates=[
                    *PLAN["gates"],
                    {"id": "wf_sharpe_net", "stage": "cert", "rationale": "the objective"},
                ]
            ),
            make_hyp(),
            FOLDS,
        )


def test_excluding_a_required_gate_is_refused() -> None:
    with pytest.raises(ValidationError, match="is required and cannot be left out"):
        validate_plan(
            plan(
                gates=[g for g in PLAN["gates"] if g["id"] != "embargoed_window"],
                excluded=[{"id": "embargoed_window", "reason": "slow"}],
            ),
            make_hyp(),
            FOLDS,
        )


def test_excluding_something_that_is_not_a_gate_is_refused() -> None:
    with pytest.raises(ValidationError, match="excluded.vibes"):
        validate_plan(plan(excluded=[{"id": "vibes", "reason": "not real"}]), make_hyp(), FOLDS)


def test_every_problem_in_a_plan_is_reported_at_once() -> None:
    broken = plan(
        gates=[
            {"id": "vibes", "stage": "cert", "rationale": "hunch"},
            {"id": "bootstrap", "stage": "paper", "params": {"n": 1}, "rationale": "wrong twice"},
            {"id": "live_drift", "stage": "live", "params": {}, "rationale": "kept"},
        ]
    )
    with pytest.raises(ValidationError) as raised:
        validate_plan(broken, make_hyp(), FOLDS)
    message = raised.value.message
    assert message.count(";") >= 4


# --- a library that is not the shipped one ----------------------------------------


GATE_YAML = """id: {id}
kind: gate
stage: cert
required: false
meaningful_when: "an item written for this test"
params: {{}}
ranges: {{}}
impl: {impl}
"""


def test_an_item_filed_under_the_wrong_name_is_refused(elsewhere: Any) -> None:
    write_item(elsewhere, "wrong_name", GATE_YAML.format(id="bootstrap", impl="a.b"))
    with pytest.raises(ValidationError, match="misfiled"):
        lib.catalogue()


def test_an_item_whose_implementation_is_missing_is_refused(elsewhere: Any) -> None:
    write_item(
        elsewhere, "absent", GATE_YAML.format(id="absent", impl="kanso.criteria.gates.absent")
    )
    with pytest.raises(ValidationError, match="is missing"):
        lib.gates()


def test_an_item_pointing_at_another_implementation_is_refused(elsewhere: Any) -> None:
    write_item(
        elsewhere,
        "borrowed",
        GATE_YAML.format(id="borrowed", impl="kanso.criteria.gates.bootstrap"),
    )
    with pytest.raises(ValidationError, match="implements a different item"):
        lib.gates()
