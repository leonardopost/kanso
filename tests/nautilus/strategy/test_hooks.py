"""The construct vocabulary and the registry a sleeve reads its modifiers from."""

from __future__ import annotations

import gc

import pytest

from kanso.errors import Exit, ValidationError
from kanso.nautilus.hooks import (
    EXIT,
    FILTER,
    MODIFIER_CONSTRUCTS,
    OVERLAY,
    Decision,
    Hedge,
    HookContext,
    Modifier,
    deregister_modifier,
    modifiers_for,
    register_modifier,
)


class Stub:
    """The least a modifier can be: a construct and an answer."""

    def __init__(self, construct: str = FILTER, decision: Decision | None = None) -> None:
        self.construct = construct
        self.decision = decision or Decision.neutral(construct)

    def evaluate(self, ctx: HookContext) -> Decision:
        return self.decision


def context(**overrides: object) -> HookContext:
    fields: dict[str, object] = {
        "instrument_id": "DEMO.XNAS",
        "ts_event": 1_000,
        "side": "BUY",
        "qty": 10.0,
        "price": None,
        "order_type": "MARKET",
        "position_qty": 0.0,
        "capital": 100_000.0,
        "last_bar": None,
        "last_quote": None,
        "last_trade": None,
        "host_strategy_id": "S-000",
        "cache": None,
    }
    fields.update(overrides)
    return HookContext(**fields)  # type: ignore[arg-type]


# --- Decision ----------------------------------------------------------------


def test_neutral_is_the_identity_of_each_construct() -> None:
    assert Decision.neutral(FILTER) == Decision(allow=True)
    assert Decision.neutral(OVERLAY) == Decision(scale=1.0, hedges=())
    assert Decision.neutral(EXIT) == Decision(exit=False)


def test_a_construct_with_no_attachable_decision_has_no_neutral() -> None:
    with pytest.raises(ValidationError) as raised:
        Decision.neutral("sleeve")
    assert raised.value.code == Exit.VALIDATION
    assert "filter" in raised.value.message


def test_every_attachable_construct_has_a_neutral() -> None:
    for construct in MODIFIER_CONSTRUCTS:
        assert Decision.neutral(construct).check(construct) is not None


def test_a_decision_may_not_answer_a_question_its_construct_was_not_asked() -> None:
    with pytest.raises(ValidationError, match="filter construct answers allow"):
        Decision(allow=True, scale=0.5).check(FILTER)


def test_an_unknown_construct_cannot_check_a_decision() -> None:
    with pytest.raises(ValidationError, match="no attachable decision"):
        Decision(allow=True).check("execution")


def test_a_scale_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"outside \[0, 1\]"):
        Decision(scale=1.5).check(OVERLAY)
    with pytest.raises(ValidationError, match=r"outside \[0, 1\]"):
        Decision(scale=-0.1).check(OVERLAY)


def test_hedges_are_normalised_to_a_tuple() -> None:
    decision = Decision(scale=1.0, hedges=[Hedge("H.XNAS", -5.0)])  # type: ignore[arg-type]
    assert decision.hedges == (Hedge("H.XNAS", -5.0),)


def test_a_stub_satisfies_the_modifier_protocol() -> None:
    assert isinstance(Stub(), Modifier)


# --- registry ----------------------------------------------------------------


def test_a_modifier_is_found_by_the_host_it_registered_against() -> None:
    bus, modifier = object(), Stub()
    register_modifier(bus, "S-000", modifier)
    try:
        assert modifiers_for(bus, "S-000") == (modifier,)
    finally:
        deregister_modifier(bus, "S-000", modifier)


def test_registering_twice_attaches_once() -> None:
    bus, modifier = object(), Stub()
    register_modifier(bus, "S-000", modifier)
    register_modifier(bus, "S-000", modifier)
    try:
        assert modifiers_for(bus, "S-000") == (modifier,)
    finally:
        deregister_modifier(bus, "S-000", modifier)


def test_two_engines_keep_separate_registries() -> None:
    first, second = object(), object()
    mine, yours = Stub(), Stub()
    register_modifier(first, "S-000", mine)
    register_modifier(second, "S-000", yours)
    try:
        assert modifiers_for(first, "S-000") == (mine,)
        assert modifiers_for(second, "S-000") == (yours,)
    finally:
        deregister_modifier(first, "S-000", mine)
        deregister_modifier(second, "S-000", yours)


def test_an_unregistered_host_has_no_modifiers() -> None:
    assert modifiers_for(object(), "nobody") == ()


def test_deregistering_an_unattached_modifier_is_a_no_op() -> None:
    bus, attached = object(), Stub()
    deregister_modifier(bus, "S-000", Stub())
    register_modifier(bus, "S-000", attached)
    try:
        deregister_modifier(bus, "S-000", Stub())
        assert modifiers_for(bus, "S-000") == (attached,)
    finally:
        deregister_modifier(bus, "S-000", attached)


def test_a_construct_selects_only_its_own_modifiers() -> None:
    bus = object()
    gate, overlay = Stub(FILTER), Stub(OVERLAY)
    register_modifier(bus, "S-000", gate)
    register_modifier(bus, "S-000", overlay)
    try:
        assert modifiers_for(bus, "S-000", FILTER) == (gate,)
        assert modifiers_for(bus, "S-000", OVERLAY) == (overlay,)
        assert modifiers_for(bus, "S-000", EXIT) == ()
    finally:
        deregister_modifier(bus, "S-000", gate)
        deregister_modifier(bus, "S-000", overlay)


def test_several_names_for_one_host_return_each_modifier_once() -> None:
    bus = object()
    by_id, by_class = Stub(), Stub()
    register_modifier(bus, "S-000", by_id)
    register_modifier(bus, "Sleeve", by_class)
    register_modifier(bus, "Sleeve", by_id)
    try:
        found = modifiers_for(bus, ("S-000", "Sleeve"))
        assert found == (by_id, by_class)
    finally:
        deregister_modifier(bus, "S-000", by_id)
        deregister_modifier(bus, "Sleeve", by_class)
        deregister_modifier(bus, "Sleeve", by_id)


def test_a_dropped_modifier_leaves_nothing_behind() -> None:
    bus = object()
    register_modifier(bus, "S-000", Stub())
    gc.collect()
    assert modifiers_for(bus, "S-000") == ()
    assert modifiers_for(bus, "S-000") == ()
