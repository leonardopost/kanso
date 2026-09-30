"""Construction: an entry plus the convention table becomes an engine instrument."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from kanso.data.instruments import (
    build,
    conventions_for,
    definition_checksum,
    read_store,
    write_store,
)
from kanso.errors import Exit, ValidationError
from kanso.schemas import InstrumentEntry
from kanso.workspace import Workspace

from .conftest import AS_OF, CLASSES, EQUITY, FUTURE, INDEX, OPTION, PERPETUAL


def built(spec: dict[str, Any], as_of: date = AS_OF) -> Any:
    item = InstrumentEntry.model_validate(spec)
    return build(item, conventions_for(item, as_of))


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_every_instrument_class_the_spec_names_is_built(name: str) -> None:
    instrument = built(CLASSES[name])
    assert type(instrument).__name__ == name
    assert instrument.id.value == CLASSES[name]["nautilus_id"]


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_every_instrument_class_round_trips_through_the_store(ws: Workspace, name: str) -> None:
    instrument = built(CLASSES[name])
    write_store(ws, [instrument])
    held = read_store(ws)
    assert definition_checksum(instrument) in held
    assert type(held[definition_checksum(instrument)]).__name__ == name


def test_the_whole_set_round_trips_at_once(ws: Workspace) -> None:
    made = [built(spec) for spec in CLASSES.values()]
    write_store(ws, made)
    assert set(read_store(ws)) == {definition_checksum(item) for item in made}


def test_an_equity_takes_its_tick_and_lot_from_the_table() -> None:
    instrument = built(EQUITY)
    assert str(instrument.price_increment) == "0.01"
    assert instrument.price_precision == 2
    assert str(instrument.lot_size) == "1"


def test_the_timestamps_are_the_date_the_definition_was_resolved_as_of() -> None:
    instrument = built(EQUITY)
    midnight = int(datetime(2024, 6, 3, tzinfo=UTC).timestamp()) * 1_000_000_000
    assert instrument.ts_event == midnight
    assert instrument.ts_init == midnight
    assert instrument.ts_init >= instrument.ts_event


def test_an_override_wins_over_the_convention_table() -> None:
    instrument = built({**EQUITY, "override": {"currency": "USD", "price_increment": "0.05"}})
    assert str(instrument.price_increment) == "0.05"
    assert instrument.price_precision == 2


def test_the_precision_follows_the_increment_it_is_told_to_use() -> None:
    instrument = built({**EQUITY, "override": {"currency": "USD", "price_increment": "0.0001"}})
    assert instrument.price_precision == 4


def test_a_stated_reference_price_picks_the_band() -> None:
    penny = InstrumentEntry.model_validate(
        {**EQUITY, "attributes": {"reference_price": 0.40}},
    )
    assert conventions_for(penny, AS_OF)["price_increment"] == Decimal("0.0001")
    assert conventions_for(penny, AS_OF, price=5.0)["price_increment"] == Decimal("0.01")


def test_an_asset_class_with_no_schedule_defaults_nothing() -> None:
    """The table is silent for an index, so only the timestamps come from kanso."""
    item = InstrumentEntry.model_validate(INDEX)
    assert set(conventions_for(item, AS_OF)) == {"ts_event", "ts_init"}


def test_a_field_the_convention_table_cannot_supply_is_named() -> None:
    with pytest.raises(ValidationError) as caught:
        built({**INDEX, "override": {"currency": "USD"}})
    assert caught.value.code is Exit.VALIDATION
    assert "SPX.XCBO" in caught.value.message
    assert "price_increment" in caught.value.message
    assert caught.value.remedy is not None
    assert "override" in caught.value.remedy


def test_an_override_the_class_does_not_accept_is_refused() -> None:
    with pytest.raises(ValidationError, match="has no field expiry_month"):
        built({**EQUITY, "override": {"currency": "USD", "expiry_month": "Z4"}})


def test_an_override_may_not_forge_an_identity() -> None:
    with pytest.raises(ValidationError, match="may not set instrument_id, raw_symbol"):
        built(
            {
                **EQUITY,
                "override": {
                    "currency": "USD",
                    "instrument_id": "MSFT.XNAS",
                    "raw_symbol": "MSFT",
                },
            }
        )


def test_the_engines_own_refusal_is_reported_as_it_is() -> None:
    with pytest.raises(ValidationError, match="rejected its fields"):
        built(
            {
                **EQUITY,
                "override": {
                    "currency": "USD",
                    "price_increment": "0.01",
                    "price_precision": 4,
                },
            }
        )


def test_a_derivative_states_its_instrument_class() -> None:
    assert type(built(OPTION)).__name__ == "OptionContract"
    assert type(built(FUTURE)).__name__ == "FuturesContract"


def test_an_instrument_class_kanso_does_not_build_is_named() -> None:
    with pytest.raises(ValidationError, match="names no instrument class"):
        built({**FUTURE, "override": {**FUTURE["override"], "instrument_class": "warrant"}})


def test_an_asset_class_implying_no_class_asks_for_one() -> None:
    with pytest.raises(ValidationError) as caught:
        built({**EQUITY, "asset_class": "COMMODITY"})
    assert "implies no instrument class" in caught.value.message
    assert caught.value.remedy is not None
    assert "instrument_class" in caught.value.remedy


def test_the_multiplier_is_never_guessed() -> None:
    """A contract size of one is a claim about the contract, so an absent one refuses."""
    without = dict(FUTURE["override"])
    del without["multiplier"]
    with pytest.raises(ValidationError, match="needs multiplier"):
        built({**FUTURE, "override": without})


def test_an_optional_engine_field_reaches_the_constructor() -> None:
    instrument = built({**EQUITY, "override": {"currency": "USD", "isin": "US0378331005"}})
    assert instrument.isin == "US0378331005"


def test_a_listing_date_is_accepted_as_a_date_or_as_nanoseconds() -> None:
    by_date = built(FUTURE)
    by_ns = built(
        {
            **FUTURE,
            "override": {
                **FUTURE["override"],
                "activation_ns": by_date.activation_ns,
                "expiration_ns": by_date.expiration_ns,
            },
        }
    )
    assert definition_checksum(by_ns) == definition_checksum(by_date)


def test_a_field_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValidationError, match="rejected its fields"):
        built({**EQUITY, "override": {"currency": "USD", "price_increment": "one cent"}})


def test_writing_nothing_writes_nothing(ws: Workspace) -> None:
    write_store(ws, [])
    assert read_store(ws) == {}


# --- the perpetual ------------------------------------------------------------

WORKSPACE_PAGE = Path(__file__).resolve().parents[3] / "docs" / "workspace.md"


def documented_perpetual() -> dict[str, Any]:
    """The manual perpetual entry `docs/workspace.md` shows, read off the page itself."""
    text = WORKSPACE_PAGE.read_text(encoding="utf-8")
    section = text[text.index("\n### A perpetual\n") :]
    block = section[section.index("```yaml\n") + len("```yaml\n") :]
    document: dict[str, Any] = yaml.safe_load(block[: block.index("```")])
    [entry] = document.values()
    return dict(entry)


def perpetual(**override: Any) -> dict[str, Any]:
    """The perpetual entry with these override fields replaced; `None` removes one."""
    merged = {**PERPETUAL["override"], **override}
    return {**PERPETUAL, "override": {k: v for k, v in merged.items() if v is not None}}


def test_the_documented_perpetual_builds_a_linear_contract_of_a_hundredth_of_a_coin() -> None:
    assert documented_perpetual() == PERPETUAL

    contract = built(documented_perpetual())

    assert type(contract).__name__ == "CryptoPerpetual"
    assert str(contract.multiplier) == "0.01"
    assert (contract.maker_fee, contract.taker_fee) == (Decimal(0), Decimal(0))
    assert contract.is_inverse is False
    assert contract.settlement_currency.code == "USDT"
    assert (contract.price_precision, contract.size_precision) == (1, 0)


def test_a_perpetual_s_precisions_follow_its_increments() -> None:
    contract = built(perpetual(price_precision=None, size_precision=None))
    assert (contract.price_precision, contract.size_precision) == (1, 0)


@pytest.mark.parametrize("field", ["multiplier", "lot_size"])
def test_a_perpetual_s_contract_size_is_never_the_engine_s_default(field: str) -> None:
    """The engine would build one with a contract value of one; kanso names the field."""
    with pytest.raises(ValidationError) as caught:
        built(perpetual(**{field: None}))
    assert caught.value.message == (
        f"BTCUSDT-PERP.SIM: CryptoPerpetual needs {field}, which neither the convention "
        "table nor `override` supplies"
    )


@pytest.mark.parametrize("flag", [True, "true"])
def test_an_inverse_perpetual_is_refused_by_name(flag: object) -> None:
    with pytest.raises(ValidationError) as caught:
        built(perpetual(is_inverse=flag))
    assert caught.value.code is Exit.VALIDATION
    assert caught.value.message == (
        "BTCUSDT-PERP.SIM: is_inverse is true in `override` in instruments.yaml; kanso trades "
        "linear perpetuals: the runner's notional is qty x px x multiplier"
    )


def test_a_stated_linear_flag_builds_and_anything_else_is_refused() -> None:
    assert built(perpetual(is_inverse="false")).is_inverse is False
    with pytest.raises(ValidationError, match="'no' is not true or false"):
        built(perpetual(is_inverse="no"))


def test_a_minimum_notional_is_money_in_the_settlement_currency() -> None:
    contract = built(perpetual(min_notional=5, max_notional="1000000 USDT"))
    assert str(contract.min_notional) == "5.00000000 USDT"
    assert str(contract.max_notional) == "1000000.00000000 USDT"
    with pytest.raises(ValidationError, match="is not in the settlement currency USDT"):
        built(perpetual(min_notional="5 USDC"))


def test_a_swap_is_a_cryptocurrency_instrument() -> None:
    with pytest.raises(ValidationError) as caught:
        built({**PERPETUAL, "asset_class": "EQUITY"})
    assert caught.value.message == (
        "BTCUSDT-PERP.SIM: a CryptoPerpetual is a CRYPTOCURRENCY instrument, and this entry "
        "says EQUITY"
    )
    assert (
        caught.value.remedy == "set asset_class to CRYPTOCURRENCY in this entry of instruments.yaml"
    )


def test_the_classes_an_entry_may_name_include_the_swap() -> None:
    with pytest.raises(ValidationError) as caught:
        built({**EQUITY, "asset_class": "COMMODITY"})
    assert caught.value.remedy is not None
    assert "`instrument_class: swap`" in caught.value.remedy


# --- the fee-rate guard -------------------------------------------------------

FEE_REMEDY = (
    "the runner charges commission once from the venue model; state it under "
    "venues.<MIC>.costs or the hypothesis costs, and {step} this entry's `override` in "
    "instruments.yaml"
)


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (perpetual(taker_fee="0.0005"), "taker_fee"),
        ({**EQUITY, "override": {"currency": "USD", "maker_fee": "0.0002"}}, "maker_fee"),
    ],
)
def test_a_fee_rate_in_an_override_is_refused_by_name(spec: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        built(spec)
    assert caught.value.code is Exit.VALIDATION
    assert f"{field} 0.000" in caught.value.message
    assert "in `override` in instruments.yaml" in caught.value.message
    assert caught.value.remedy == FEE_REMEDY.format(step=f"remove {field} from")


def test_a_zero_fee_rate_and_a_margin_rate_are_still_accepted() -> None:
    contract = built(perpetual(maker_fee="0", taker_fee=0, margin_init="0.5", margin_maint="0.25"))
    assert contract.margin_init == Decimal("0.5")
    assert contract.margin_maint == Decimal("0.25")
    assert built({**EQUITY, "override": {"currency": "USD", "margin_init": "0.5"}}).margin_init == (
        Decimal("0.5")
    )


def test_a_fee_rate_in_a_resolved_definition_is_refused_by_name() -> None:
    """A resolved definition reaches `build` as its fields; the rate is refused from there."""
    entry = InstrumentEntry.model_validate({**EQUITY, "manual": False, "override": {}})
    resolved = {**conventions_for(entry, AS_OF), "currency": "USD", "maker_fee": "0.0002"}
    with pytest.raises(ValidationError) as caught:
        build(entry, resolved)
    assert caught.value.message == (
        "AAPL.XNAS: maker_fee 0.0002 in the resolved definition; a kanso definition charges "
        "no fee of its own"
    )
    assert caught.value.remedy == FEE_REMEDY.format(step='set maker_fee: "0" in')


def test_a_resolved_rate_is_zeroed_by_the_override_the_remedy_names() -> None:
    """A refresh fetches the provider's rate again; the override the remedy names wins."""
    entry = InstrumentEntry.model_validate(
        {**EQUITY, "manual": False, "override": {"maker_fee": "0"}}
    )
    resolved = {**conventions_for(entry, AS_OF), "currency": "USD", "maker_fee": "0.0002"}
    assert build(entry, resolved).maker_fee == Decimal(0)


def test_rates_from_both_sources_are_each_given_their_step() -> None:
    entry = InstrumentEntry.model_validate(
        {**EQUITY, "manual": False, "override": {"taker_fee": "0.0005"}}
    )
    resolved = {**conventions_for(entry, AS_OF), "currency": "USD", "maker_fee": "0.0002"}
    with pytest.raises(ValidationError) as caught:
        build(entry, resolved)
    assert caught.value.remedy == FEE_REMEDY.format(
        step='remove taker_fee from, and set maker_fee: "0" in'
    )
