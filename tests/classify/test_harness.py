"""The harness: what the runner needs to execute one card, per construct."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kanso.classify import get
from kanso.classify.construct import (
    MODIFIER_BASE,
    MODIFIER_ENTRY,
    MODIFIER_TEMPLATE,
    SLEEVE_BASE,
    SLEEVE_ENTRY,
    SLEEVE_TEMPLATE,
    HostRef,
)
from kanso.errors import PreconditionError, ValidationError
from tests.classify.conftest import hypothesis, strategy

ATTACHED = {
    "filter": {"allow": "before_entry"},
    "overlay": {"scale": "size", "hedges": "hedges"},
    "exit": {"exit": "before_exit"},
}
SEAMS = ("alpha", "execution", "allocation")


def test_a_sleeve_runs_the_strategy_class_itself_with_no_host() -> None:
    hyp = hypothesis("sleeve")
    harness = get("sleeve").harness(hyp)
    assert (harness.construct, harness.hyp_id) == ("sleeve", "demo_mr")
    assert (harness.entrypoint, harness.base) == (SLEEVE_ENTRY, SLEEVE_BASE)
    assert harness.template == SLEEVE_TEMPLATE
    assert (harness.host, harness.attached, harness.relative) == (None, False, False)
    assert harness.consults == {}
    assert harness.hyp.universe == ["DEMO"] and harness.hyp.resolution == "1m"


@pytest.mark.parametrize("construct_id", sorted(ATTACHED))
def test_an_attached_construct_runs_a_modifier_on_its_host(construct_id: str) -> None:
    hyp = hypothesis(construct_id, host="demo_sleeve")
    harness = get(construct_id).harness(hyp, strategy())
    assert (harness.entrypoint, harness.base) == (MODIFIER_ENTRY, MODIFIER_BASE)
    assert harness.template == MODIFIER_TEMPLATE
    assert (harness.attached, harness.relative) == (True, True)
    assert harness.consults == ATTACHED[construct_id]
    assert harness.host == HostRef("demo_sleeve", 1, strategy().versions[0].sleeve, ())
    assert harness.host is not None and harness.host.impl == "strategies/demo_sleeve/impl/1"


def test_each_decision_field_names_the_hook_that_consults_it() -> None:
    hooks = {hook for consults in ATTACHED.values() for hook in consults.values()}
    assert hooks == {"before_entry", "size", "hedges", "before_exit"}
    assert {field for consults in ATTACHED.values() for field in consults} == {
        "allow",
        "scale",
        "hedges",
        "exit",
    }


def test_the_host_is_pinned_by_version_not_by_latest() -> None:
    host = strategy({"hyp_id": "vol_gate", "strategy_sha": "c" * 64, "construct": "filter"})
    hyp = hypothesis("exit", host="demo_sleeve")
    assert get("exit").harness(hyp, host).host == HostRef.of(host)
    pinned = get("exit").harness(hyp, host, version=1).host
    assert pinned is not None and (pinned.version, pinned.attached) == (1, ())
    latest = HostRef.of(host)
    assert (latest.version, [ref.construct for ref in latest.attached]) == (2, ["filter"])


def test_a_host_version_that_does_not_exist_is_refused() -> None:
    hyp = hypothesis("filter", host="demo_sleeve")
    with pytest.raises(PreconditionError, match="has no version 4; it has 1..1"):
        get("filter").harness(hyp, strategy(), version=4)


def test_an_unclassified_hypothesis_has_nothing_to_run() -> None:
    with pytest.raises(PreconditionError, match="not classified"):
        get("sleeve").harness(hypothesis())


def test_a_construct_refuses_a_hypothesis_classified_as_another() -> None:
    with pytest.raises(ValidationError, match="classified 'sleeve', not 'filter'"):
        get("filter").harness(hypothesis("sleeve"), strategy())


def test_a_sleeve_attaches_to_nothing() -> None:
    with pytest.raises(ValidationError, match="attaches to nothing"):
        get("sleeve").harness(hypothesis("sleeve"), strategy())


def test_an_attached_construct_needs_its_certified_host() -> None:
    with pytest.raises(PreconditionError, match="attaches to a sleeve"):
        get("overlay").harness(hypothesis("overlay", host="demo_sleeve"))


def test_the_host_given_must_be_the_host_classified() -> None:
    hyp = hypothesis("filter", host="other_book")
    with pytest.raises(ValidationError, match="classified onto 'other_book'"):
        get("filter").harness(hyp, strategy())


def test_an_overlay_on_the_portfolio_names_its_seam() -> None:
    hyp = hypothesis("overlay", host="portfolio")
    with pytest.raises(PreconditionError, match="not runnable in this version") as raised:
        get("overlay").harness(hyp)
    assert "hosted by the portfolio" in raised.value.message


def test_a_construct_that_only_attaches_to_a_sleeve_refuses_the_portfolio() -> None:
    hyp = hypothesis("exit", host="portfolio")
    with pytest.raises(ValidationError, match="attaches to a sleeve, not to the portfolio"):
        get("exit").harness(hyp)


@pytest.mark.parametrize("construct_id", SEAMS)
def test_a_non_runnable_construct_is_classifiable_and_refused_at_the_run(
    construct_id: str,
) -> None:
    construct = get(construct_id)
    hyp = hypothesis(construct_id, host="demo_sleeve")
    assert hyp.construct is not None and hyp.construct.id == construct_id
    with pytest.raises(PreconditionError, match="classifiable but not runnable") as raised:
        construct.harness(hyp, strategy())
    assert raised.value.message.endswith(construct.seam)  # type: ignore[attr-defined]
    assert raised.value.remedy == (
        f"classify onto a runnable construct, or implement the {construct_id} seam"
    )


def test_a_filter_takes_the_scope_it_declares() -> None:
    for scope in ("time", "instrument"):
        hyp = hypothesis("filter", host="demo_sleeve", params={"scope": scope})
        assert get("filter").harness(hyp, strategy()).construct == "filter"


def test_a_parameter_outside_the_declared_set_is_refused() -> None:
    hyp = hypothesis("filter", host="demo_sleeve", params={"scope": "sector"})
    with pytest.raises(ValidationError, match="'sector' is not one of time, instrument"):
        get("filter").harness(hyp, strategy())


def test_a_parameter_the_construct_does_not_declare_is_refused() -> None:
    hyp = hypothesis("filter", host="demo_sleeve", params={"window": 20})
    with pytest.raises(ValidationError, match="no parameter 'window'; it takes scope"):
        get("filter").harness(hyp, strategy())
    hyp = hypothesis("exit", host="demo_sleeve", params={"window": 20})
    with pytest.raises(ValidationError, match="no parameter 'window'; it takes none"):
        get("exit").harness(hyp, strategy())


class Config:
    lookback = 20


def module(**members: object) -> SimpleNamespace:
    """A stand-in for a loaded `strategy.py`."""
    return SimpleNamespace(**members)


def test_the_harness_finds_the_class_a_card_runs() -> None:
    sleeve = get("sleeve").harness(hypothesis("sleeve"))
    strategy_cls = type("Strategy", (), {"config_cls": Config})
    assert sleeve.entry(module(Strategy=strategy_cls)) is strategy_cls

    hyp = hypothesis("filter", host="demo_sleeve")
    filter_harness = get("filter").harness(hyp, strategy())
    modifier = type("Modifier", (), {"config_cls": Config, "construct": "filter"})
    assert filter_harness.entry(module(Modifier=modifier)) is modifier


def test_a_strategy_missing_the_class_or_its_config_is_refused() -> None:
    sleeve = get("sleeve").harness(hypothesis("sleeve"))
    with pytest.raises(ValidationError, match="defines no class Strategy"):
        sleeve.entry(module(Modifier=object))
    with pytest.raises(ValidationError, match="defines no class Strategy"):
        sleeve.entry(module(Strategy="not a class"))
    with pytest.raises(ValidationError, match="declares no config_cls"):
        sleeve.entry(module(Strategy=type("Strategy", (), {})))


def test_a_modifier_must_be_tagged_with_the_construct_it_runs_as() -> None:
    hyp = hypothesis("exit", host="demo_sleeve")
    harness = get("exit").harness(hyp, strategy())
    mistagged = type("Modifier", (), {"config_cls": Config, "construct": "filter"})
    with pytest.raises(ValidationError, match="Modifier.construct is 'filter', not 'exit'"):
        harness.entry(module(Modifier=mistagged))
