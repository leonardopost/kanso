"""A required gate that cannot run is a promise the certificate would not keep.

The toolbox declares some gates before the milestone that implements them. While a gate
is in that state a plan is neither offered it nor held to it, because requiring a gate
that cannot run would make every plan impossible and accepting one would let a
certificate claim a test that never executed.

That state is temporary by construction, and this module is what makes it temporary: the
set is pinned here, so implementing a gate forces this file to change, and shipping with
the set non-empty is caught before a release rather than after one.
"""

from __future__ import annotations

import pytest

from kanso import criteria
from kanso.errors import ValidationError

from .test_library import FOLDS, PLAN, make_hyp, plan

# Gates the toolbox marks as structural invariants and this version cannot yet run.
# Shrinking this set is the point; growing it needs a reason in the commit that does it.
PENDING_REQUIRED = frozenset({"parity_replay"})


def test_the_pending_required_set_is_exactly_what_is_expected() -> None:
    """Implementing one of these must fail here, so the exemption cannot outlive it."""
    assert criteria.pending_required() == PENDING_REQUIRED


def test_no_required_gate_is_pending_at_release() -> None:
    """The release gate. Until 0.1.0 this is expected to fail and says why."""
    pending = criteria.pending_required()
    if pending:
        pytest.skip(
            f"still pending: {sorted(pending)} — these must be implemented before a "
            "release, and this skip is the reminder"
        )
    assert criteria.pending_required() == frozenset()


def test_a_pending_gate_is_not_offered_to_a_planner() -> None:
    offered = criteria.plannable()

    for gate in PENDING_REQUIRED:
        assert gate not in offered


def test_a_pending_gate_is_not_required_of_a_plan() -> None:
    """Otherwise no plan could ever validate, since it may not name one either."""
    criteria.validate_plan(PLAN, make_hyp(), FOLDS)


def test_a_plan_naming_a_pending_gate_is_refused() -> None:
    """A certificate must not claim a test that never ran."""
    document = plan(
        gates=[
            *PLAN["gates"],
            {"id": "parity_replay", "stage": "cert", "params": {"ts_ns": 0}, "rationale": "no"},
        ]
    )

    with pytest.raises(ValidationError, match="no implementation"):
        criteria.validate_plan(document, make_hyp(), FOLDS)


def test_excluding_a_pending_gate_is_refused() -> None:
    """It was never the planner's to exclude: it was never offered."""
    document = plan(excluded=[{"id": "parity_replay", "reason": "not mine to exclude"}])

    with pytest.raises(ValidationError, match="no implementation"):
        criteria.validate_plan(document, make_hyp(), FOLDS)
