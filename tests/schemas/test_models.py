"""`models.yaml`: the register and the routing table."""

from __future__ import annotations

from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import (
    ROUTING_DEFAULTS,
    TASK_CLASSES,
    ModelsFile,
    Route,
    check_tier_coverage,
    parse_yaml,
)

MODEL: dict[str, Any] = {
    "id": "some-model",
    "provider": "someone",
    "protocol": "anthropic",
    "tier": "frontier",
    "local": False,
    "ctx": 200000,
    "cost_in": 3.0,
    "cost_out": 15.0,
    "tools": True,
}


def build(**changes: Any) -> ModelsFile:
    return ModelsFile.model_validate({"schema": 1, "models": [MODEL], **changes})


def test_the_shipped_routing_defaults() -> None:
    assert ROUTING_DEFAULTS["classify"] == Route(tier="frontier", effort="high", max_output=1024)
    assert ROUTING_DEFAULTS["certify_plan"] == Route(
        tier="frontier", effort="high", max_output=4096
    )
    assert ROUTING_DEFAULTS["propose"] == Route(tier="mid", effort="medium", max_output=4096)
    assert ROUTING_DEFAULTS["align_check"] == Route(tier="cheap", effort="none", max_output=256)


def test_an_absent_routing_table_is_every_default() -> None:
    assert build().routes() == dict(ROUTING_DEFAULTS)
    assert set(build().routes()) == set(TASK_CLASSES)


def test_an_absent_field_takes_its_default() -> None:
    routes = build(routing={"propose": {"tier": "frontier"}}).routes()
    assert routes["propose"] == Route(tier="frontier", effort="medium", max_output=4096)
    assert routes["classify"] == ROUTING_DEFAULTS["classify"]


def test_a_fifth_task_class_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(routing={"summarise": {"tier": "cheap"}})


def test_an_unknown_effort_is_refused() -> None:
    with pytest.raises(ValidationError, match="effort"):
        build(routing={"propose": {"effort": "maximum"}})


def test_a_model_may_serve_several_tiers() -> None:
    register = build(models=[{**MODEL, "tier": ["cheap", "mid", "frontier"]}])
    assert register.models[0].tiers == ("cheap", "mid", "frontier")
    check_tier_coverage(register)


def test_a_tier_listed_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="tier"):
        build(models=[{**MODEL, "tier": ["cheap", "cheap"]}])


def test_every_tier_needs_a_model() -> None:
    with pytest.raises(ValidationError) as caught:
        check_tier_coverage(build())
    assert "cheap" in caught.value.message
    assert "mid" in caught.value.message


def test_a_model_id_appears_once() -> None:
    with pytest.raises(ValidationError, match="twice"):
        build(models=[MODEL, MODEL])


def test_api_key_env_is_a_name_not_a_value() -> None:
    assert build(models=[{**MODEL, "api_key_env": "MY_KEY_VAR"}]).models[0].api_key_env
    with pytest.raises(ValidationError, match="api_key_env"):
        build(models=[{**MODEL, "api_key_env": "sk-live-0123456789"}])


def test_for_tier_selects() -> None:
    register = build(models=[MODEL, {**MODEL, "id": "other", "tier": ["cheap", "frontier"]}])
    assert [m.id for m in register.for_tier("frontier")] == ["some-model", "other"]
    assert [m.id for m in register.for_tier("cheap")] == ["other"]
    assert register.for_tier("mid") == []


def test_a_register_may_be_empty_until_it_is_used() -> None:
    assert parse_yaml(ModelsFile, "schema: 1\n").models == []


def test_a_model_serving_no_tier_is_refused() -> None:
    with pytest.raises(ValidationError, match="tier"):
        build(models=[{**MODEL, "tier": []}])
