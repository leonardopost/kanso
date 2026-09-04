"""Composition: the strategy version each construct produces on certification."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kanso.classify import get
from kanso.errors import PreconditionError, ValidationError
from kanso.schemas import StrategyFile
from tests.classify.conftest import EXPECTATION, HOST_SHA, NOW, PINS, SHA, strategy

COMPOSED = {"pins": PINS, "expectation": EXPECTATION, "created_at": NOW}


def refile(strategy_id: str, *versions: object) -> StrategyFile:
    """The versions as the file that holds them, so the shape is checked, not asserted."""
    return StrategyFile.model_validate(
        {"schema": 1, "id": strategy_id, "versions": [v.model_dump() for v in versions]}  # type: ignore[attr-defined]
    )


def test_a_sleeve_composes_a_new_strategy_at_version_1() -> None:
    version = get("sleeve").compose(None, "demo_mr", SHA, **COMPOSED)
    assert version.version == 1
    assert (version.sleeve.hyp_id, version.sleeve.strategy_sha) == ("demo_mr", SHA)
    assert version.attached == []
    assert version.state == "composed"
    assert (version.pins, version.expectation) == (PINS, EXPECTATION)
    assert refile("demo_mr", version).latest().version == 1


def test_a_sleeve_composes_a_strategy_that_does_not_exist_yet() -> None:
    with pytest.raises(PreconditionError, match="a sleeve composes a new strategy at version 1"):
        get("sleeve").compose(strategy(), "demo_mr", SHA, **COMPOSED)


def test_an_attached_construct_composes_its_host_version_plus_one() -> None:
    host = strategy()
    version = get("filter").compose(host, "vol_gate", SHA, {"scope": "time"}, **COMPOSED)
    assert version.version == 2
    assert version.sleeve == host.latest().sleeve
    assert version.config == host.latest().config
    assert [(a.hyp_id, a.construct, a.params) for a in version.attached] == [
        ("vol_gate", "filter", {"scope": "time"})
    ]
    assert refile("demo_sleeve", host.versions[0], version).latest().version == 2


def test_each_attachment_appends_to_the_ones_already_composed() -> None:
    host = strategy({"hyp_id": "vol_gate", "strategy_sha": "c" * 64, "construct": "filter"})
    version = get("exit").compose(host, "time_stop", SHA, **COMPOSED)
    assert version.version == 3
    assert [a.construct for a in version.attached] == ["filter", "exit"]
    assert version.sleeve.strategy_sha == HOST_SHA
    assert refile("demo_sleeve", *host.versions, version).latest().attached[-1].params is None


def test_a_hypothesis_cannot_attach_to_the_same_host_twice() -> None:
    host = strategy({"hyp_id": "vol_gate", "strategy_sha": "c" * 64, "construct": "filter"})
    with pytest.raises(ValidationError, match="names a hypothesis twice"):
        get("overlay").compose(host, "vol_gate", SHA, **COMPOSED)


def test_an_attached_construct_composes_onto_a_host() -> None:
    with pytest.raises(PreconditionError, match="composes onto its host's latest version"):
        get("overlay").compose(None, "vol_target", SHA, **COMPOSED)


def test_composition_checks_the_parameters_it_records() -> None:
    with pytest.raises(ValidationError, match="'sector' is not one of time, instrument"):
        get("filter").compose(strategy(), "vol_gate", SHA, {"scope": "sector"}, **COMPOSED)
    with pytest.raises(ValidationError, match="no parameter 'scope'; it takes none"):
        get("sleeve").compose(None, "demo_mr", SHA, {"scope": "time"}, **COMPOSED)


def test_a_version_is_stamped_now_unless_the_caller_says_when() -> None:
    before = datetime.now(UTC)
    version = get("sleeve").compose(None, "demo_mr", SHA, pins=PINS, expectation=EXPECTATION)
    assert before <= version.created_at <= datetime.now(UTC)
    attached = get("exit").compose(strategy(), "time_stop", SHA, pins=PINS, expectation=EXPECTATION)
    assert before <= attached.created_at


@pytest.mark.parametrize("construct_id", ["alpha", "execution", "allocation"])
def test_a_non_runnable_construct_composes_nothing(construct_id: str) -> None:
    with pytest.raises(PreconditionError, match="classifiable but not runnable"):
        get(construct_id).compose(strategy(), "some_idea", SHA, **COMPOSED)
