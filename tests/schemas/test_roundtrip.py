"""Every schema round-trips through YAML unchanged."""

from __future__ import annotations

import pytest
from hypothesis import given

from kanso.errors import ValidationError
from kanso.schemas import (
    Card,
    Certificate,
    CertificationPlan,
    ConstructItem,
    CriteriaItem,
    Envelope,
    Hypothesis,
    InstrumentsFile,
    ModelsFile,
    Portfolio,
    RunRecord,
    StrategyFile,
    VenueModel,
    Versioned,
    dump_yaml,
    parse_yaml,
)
from tests.schemas import strategies as gen


@given(gen.hypotheses())
def test_hypothesis_round_trips(value: Hypothesis) -> None:
    assert parse_yaml(Hypothesis, dump_yaml(value)) == value


@given(gen.run_records())
def test_run_record_round_trips(value: RunRecord) -> None:
    assert parse_yaml(RunRecord, dump_yaml(value)) == value


@given(gen.cards())
def test_card_round_trips(value: Card) -> None:
    assert parse_yaml(Card, dump_yaml(value)) == value


@given(gen.plans())
def test_plan_round_trips(value: CertificationPlan) -> None:
    assert parse_yaml(CertificationPlan, dump_yaml(value)) == value


@given(gen.certificates())
def test_certificate_round_trips(value: Certificate) -> None:
    assert parse_yaml(Certificate, dump_yaml(value)) == value


@given(gen.strategy_files())
def test_strategy_file_round_trips(value: StrategyFile) -> None:
    assert parse_yaml(StrategyFile, dump_yaml(value)) == value


@given(gen.portfolios())
def test_portfolio_round_trips(value: Portfolio) -> None:
    assert parse_yaml(Portfolio, dump_yaml(value)) == value


@given(gen.envelopes())
def test_envelope_round_trips(value: Envelope) -> None:
    assert parse_yaml(Envelope, dump_yaml(value)) == value


@given(gen.model_files())
def test_models_file_round_trips(value: ModelsFile) -> None:
    assert parse_yaml(ModelsFile, dump_yaml(value)) == value


@given(gen.instrument_files())
def test_instruments_file_round_trips(value: InstrumentsFile) -> None:
    assert parse_yaml(InstrumentsFile, dump_yaml(value)) == value


@given(gen.construct_items())
def test_construct_item_round_trips(value: ConstructItem) -> None:
    assert parse_yaml(ConstructItem, dump_yaml(value)) == value


@given(gen.criteria_items())
def test_criteria_item_round_trips(value: CriteriaItem) -> None:
    assert parse_yaml(CriteriaItem, dump_yaml(value)) == value


@given(gen.venue_models())
def test_venue_model_round_trips(value: VenueModel) -> None:
    assert parse_yaml(VenueModel, dump_yaml(value)) == value


@pytest.mark.parametrize(
    "model",
    [Hypothesis, CertificationPlan, Certificate, StrategyFile, Portfolio, Envelope, ModelsFile],
)
def test_every_file_model_declares_its_schema(model: type[Versioned]) -> None:
    assert model.model_fields["schema_"].alias == "schema"
    with pytest.raises(ValidationError, match="schema"):
        parse_yaml(model, "{}", "some.yaml")
