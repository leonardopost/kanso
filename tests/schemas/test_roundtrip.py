"""Every schema round-trips through YAML unchanged."""

from __future__ import annotations

import pytest
from hypothesis import given

from kanso.errors import ValidationError
from kanso.replay.record import Session
from kanso.schemas import (
    DIGEST_COLUMNS,
    Card,
    CardDigest,
    Certificate,
    CertificationPlan,
    ConstructItem,
    CriteriaItem,
    Envelope,
    Hypothesis,
    InstrumentsFile,
    ModelsFile,
    NetPerTrade,
    Portfolio,
    Power,
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


@given(gen.digests())
def test_card_digest_round_trips_through_the_json_a_card_row_holds(value: CardDigest) -> None:
    """The digest is stored as JSON in its card's row and read back whole; gross less cost
    is net, per trade and per session, whatever the numbers."""
    assert CardDigest.model_validate(value.model_dump(mode="json")) == value
    assert len(value.cells()) == len(DIGEST_COLUMNS)
    if value.net_bps_per_trade is not None:
        assert value.gross_bps_per_trade is not None and value.cost_bps_per_trade is not None
        assert abs(
            value.gross_bps_per_trade - value.cost_bps_per_trade - value.net_bps_per_trade
        ) <= 1e-9 * max(1.0, abs(value.gross_bps_per_trade), abs(value.cost_bps_per_trade))


@given(gen.plans())
def test_plan_round_trips(value: CertificationPlan) -> None:
    assert parse_yaml(CertificationPlan, dump_yaml(value)) == value


@given(gen.certificates())
def test_certificate_round_trips(value: Certificate) -> None:
    assert parse_yaml(Certificate, dump_yaml(value)) == value


@given(gen.net_per_trades())
def test_net_per_trade_round_trips(value: NetPerTrade) -> None:
    """A certificate's net per trade reads back whole, its t the value over its error and
    stated only where the error is above zero, which a single session never has."""
    assert NetPerTrade.model_validate(value.model_dump(mode="json")) == value
    assert (value.net_t is None) == (not value.net_se_session)
    assert value.sessions_with_trades >= 2 or value.net_se_session is None
    if value.net_t is not None and value.net_se_session is not None:
        assert value.net_t * value.net_se_session == pytest.approx(value.net_per_trade_bp)


@given(gen.powers())
def test_power_round_trips(value: Power) -> None:
    """A certificate's power statement reads back whole, either side of a judged bar."""
    assert Power.model_validate(value.model_dump(mode="json")) == value
    assert (value.bar is None) == (value.pass_at_research is None) == (value.pass_at_zero is None)


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


@given(gen.sessions())
def test_session_round_trips(value: Session) -> None:
    """`sessions/<id>/session.yaml` spells `from` and `exec` as written, and reads back whole."""
    text = dump_yaml(value)
    assert parse_yaml(Session, text) == value
    assert "from:" in text and "exec:" in text and "from_:" not in text and "exec_:" not in text


@pytest.mark.parametrize(
    "model",
    [Hypothesis, CertificationPlan, Certificate, StrategyFile, Portfolio, Envelope, ModelsFile],
)
def test_every_file_model_declares_its_schema(model: type[Versioned]) -> None:
    assert model.model_fields["schema_"].alias == "schema"
    with pytest.raises(ValidationError, match="schema"):
        parse_yaml(model, "{}", "some.yaml")
