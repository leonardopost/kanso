"""The engine facts are checked against the engine actually installed here.

These tests are the guard on the module docstring: they assert that every claim
is checked, that the checks agree with the docstring's record, and that the claims
the package records as design constraints are exactly the ones that fail. An engine
upgrade that closes one of those gaps, or opens a new one, fails here first.
"""

from __future__ import annotations

import nautilus_trader
import pytest

from kanso.nautilus import facts
from kanso.nautilus.facts import DESIGN_CONSTRAINTS, ENGINE_VERSION, Fact, verify

CLAIMS = [claim for claim, _ in facts._CHECKS]


@pytest.fixture(scope="module")
def verified() -> list[Fact]:
    return verify()


def test_engine_version_matches_the_installed_package() -> None:
    assert nautilus_trader.__version__ == ENGINE_VERSION


def test_docstring_records_the_engine_version() -> None:
    assert facts.__doc__ is not None
    assert ENGINE_VERSION in facts.__doc__


def test_one_fact_per_claim(verified: list[Fact]) -> None:
    assert [fact.claim for fact in verified] == CLAIMS


def test_claims_are_unique() -> None:
    assert len(set(CLAIMS)) == len(CLAIMS)


def test_every_fact_carries_evidence(verified: list[Fact]) -> None:
    for fact in verified:
        assert fact.evidence.strip(), fact.claim
        assert isinstance(fact.holds, bool)


def test_facts_are_immutable(verified: list[Fact]) -> None:
    with pytest.raises(AttributeError):
        verified[0].holds = True  # type: ignore[misc]


def test_every_claim_but_the_design_constraints_holds(verified: list[Fact]) -> None:
    failed = {fact.claim: fact.evidence for fact in verified if not fact.holds}
    assert set(failed) == DESIGN_CONSTRAINTS, failed


def test_design_constraints_are_claims() -> None:
    assert set(CLAIMS) >= DESIGN_CONSTRAINTS
    assert len(DESIGN_CONSTRAINTS) == 4


BINDINGS = {
    "the risk engine performs no balance or margin check for a margin account",
    "LeveragedMarginModel asks zero margin of an instrument whose margin rates are zero",
    "handle_bar(historical=True) routes to on_historical_data and never to on_bar",
    "a Bar carries low and high, and every market point carries ts_init",
}
"""The claims recorded ahead of the work that rests on them; deleting one fails here."""


def test_the_claims_later_work_rests_on_stay_listed_and_hold(verified: list[Fact]) -> None:
    assert set(CLAIMS) >= BINDINGS
    assert all(fact.holds for fact in verified if fact.claim in BINDINGS)


def test_verify_is_repeatable() -> None:
    first = {fact.claim: fact.holds for fact in verify()}
    second = {fact.claim: fact.holds for fact in verify()}
    assert first == second


def test_a_raising_check_is_reported_rather_than_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> tuple[bool, str]:
        raise RuntimeError("engine gone")

    monkeypatch.setattr(facts, "_CHECKS", (("a claim that cannot be checked", explode),))
    (fact,) = verify()
    assert fact.holds is False
    assert "RuntimeError: engine gone" in fact.evidence


def test_raises_helper_reports_the_exception() -> None:
    def boom() -> object:
        raise ValueError("nope")

    assert facts._raises(boom) == "ValueError: nope"
    assert facts._raises(lambda: 1) is None
