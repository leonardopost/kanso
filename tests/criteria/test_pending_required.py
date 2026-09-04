"""A required gate that cannot run is a promise the certificate would not keep.

The toolbox declares some gates before the milestone that implements them. While a gate
is in that state a plan is neither offered it nor held to it, because requiring a gate
that cannot run would make every plan impossible and accepting one would let a
certificate claim a test that never executed.

That state is temporary by construction, and this module is what makes it temporary: the
set is pinned here, so implementing a gate forces this file to change, and shipping with
the set non-empty is caught before a release rather than after one. The set is empty —
`parity_replay` was the last required gate without an implementation — so the release gate
below asserts rather than skips, and every required gate is one a certificate can claim.

The exemption is gone but the machinery is not, and the next gate declared before the
milestone that builds it will need both. The tests that prove such a gate is neither
offered, required, named nor excluded therefore withhold a real one by hand, since the
shipped toolbox no longer holds an example to point at.
"""

from __future__ import annotations

from typing import Any

import pytest

from kanso import criteria
from kanso.criteria import library as lib
from kanso.errors import ValidationError

from .test_library import FOLDS, PLAN, make_hyp, without

# Required gates the toolbox declares and this version cannot run. Empty is the only state
# a release may ship in; growing this set again needs a reason in the commit that does it.
PENDING_REQUIRED: frozenset[str] = frozenset()

WITHHELD = "parity_replay"
"""The gate the refusal tests stand down, as if this version had no implementation."""


@pytest.fixture
def withheld(monkeypatch: Any) -> str:
    """The toolbox with one gate withheld, which is what a pending gate looks like."""
    offered = {id_: item for id_, item in criteria.plannable().items() if id_ != WITHHELD}
    monkeypatch.setattr(lib, "plannable", lambda: offered)
    return WITHHELD


def test_the_pending_required_set_is_exactly_what_is_expected() -> None:
    """Implementing one of these must fail here, so the exemption cannot outlive it."""
    assert criteria.pending_required() == PENDING_REQUIRED


def test_no_required_gate_is_pending_at_release() -> None:
    """The release gate. It asserts now, and a new exemption has to answer to it."""
    assert criteria.pending_required() == frozenset()


def test_every_cert_gate_a_plan_may_name_can_actually_be_run() -> None:
    """Certification runs the plan's cert gates, so planning one it cannot run pins a
    proof that could never be produced."""
    runnable = set(criteria.gates())

    offered = {item.id for item in criteria.plannable().values() if item.stage == "cert"}

    assert offered <= runnable


def test_a_pending_gate_is_not_offered_to_a_planner(withheld: str) -> None:
    assert withheld not in lib.plannable()


def test_a_pending_gate_is_not_required_of_a_plan(withheld: str) -> None:
    """Otherwise no plan could ever validate, since it may not name one either."""
    criteria.validate_plan(without(withheld), make_hyp(), FOLDS)


def test_a_plan_naming_a_pending_gate_is_refused(withheld: str) -> None:
    """A certificate must not claim a test that never ran."""
    assert withheld in {gate["id"] for gate in PLAN["gates"]}

    with pytest.raises(ValidationError, match="no implementation"):
        criteria.validate_plan(PLAN, make_hyp(), FOLDS)


def test_excluding_a_pending_gate_is_refused(withheld: str) -> None:
    """It was never the planner's to exclude: it was never offered."""
    document = {
        **without(withheld),
        "excluded": [{"id": withheld, "reason": "not mine to exclude"}],
    }

    with pytest.raises(ValidationError, match="no implementation"):
        criteria.validate_plan(document, make_hyp(), FOLDS)
