"""Resolving a universe: the cache, the manual entry, the reference adapter, and refusal."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from kanso.data import instruments
from kanso.data.instruments import (
    ManualProvider,
    ResolveError,
    build,
    conventions_for,
    current_definitions,
    definition_checksum,
    read_store,
    resolve_universe,
    write_store,
)
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.schemas import InstrumentEntry, InstrumentsFile, load_yaml
from kanso.workspace import Workspace, init

from .conftest import AS_OF, EQUITY, FUTURE, Probe, reading, write

MSFT: dict[str, Any] = {**EQUITY, "nautilus_id": "MSFT.XNAS"}


def cache(ws: Workspace) -> InstrumentsFile:
    return load_yaml(InstrumentsFile, ws.path("instruments.yaml"))


def equity(nautilus_id: str = "AAPL.XNAS", increment: str = "0.01", as_of: date = AS_OF) -> Any:
    entry = InstrumentEntry.model_validate(
        {**EQUITY, "nautilus_id": nautilus_id, "override": {"currency": "USD"}}
    )
    return build(entry, {**conventions_for(entry, as_of), "price_increment": increment})


CORRECTED: dict[str, Any] = {**EQUITY, "override": {"currency": "USD", "price_increment": "0.05"}}
"""The AAPL entry with its tick corrected by hand: a same-dated resolution now differs."""


def probing(ws: Workspace, probe: Probe, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setitem(instruments.PROVIDERS, Probe.id, lambda _: probe)
    return reading(ws, Probe.id)


# --- the manual path ----------------------------------------------------------


def test_a_manual_entry_resolves_with_no_vendor_and_no_credential(ws: Workspace) -> None:
    write(ws, AAPL=EQUITY)
    resolved = resolve_universe(ws, ["AAPL"], AS_OF)
    assert type(resolved["AAPL"]).__name__ == "Equity"
    assert definition_checksum(resolved["AAPL"]) in read_store(ws)


def test_the_definition_reaches_the_registry_of_record(ws: Workspace) -> None:
    write(ws, AAPL=EQUITY, ES=FUTURE)
    resolved = resolve_universe(ws, ["AAPL", "ES"], AS_OF)
    assert set(read_store(ws)) == {definition_checksum(item) for item in resolved.values()}


def test_resolving_twice_writes_the_definition_once(ws: Workspace) -> None:
    write(ws, AAPL=EQUITY)
    resolve_universe(ws, ["AAPL"], AS_OF)
    resolve_universe(ws, ["AAPL"], AS_OF)
    assert len(read_store(ws)) == 1


def test_an_id_asked_for_twice_is_answered_once(ws: Workspace) -> None:
    write(ws, AAPL=EQUITY)
    assert list(resolve_universe(ws, ["AAPL", "AAPL"], AS_OF)) == ["AAPL"]


def test_a_manual_entry_is_never_resolved(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """`manual` suppresses resolution: the adapter is never built, and nothing is cached."""
    write(ws, AAPL=EQUITY)
    probe = Probe(answers={"AAPL": equity()})
    resolved = resolve_universe(probing(ws, probe, monkeypatch), ["AAPL"], AS_OF)

    assert probe.asked == []
    assert cache(ws)["AAPL"].resolved is None
    assert str(resolved["AAPL"].price_increment) == "0.01"


def test_an_instrument_may_be_named_by_its_fully_qualified_id(ws: Workspace) -> None:
    write(ws, AAPL=EQUITY)
    assert "AAPL.XNAS" in resolve_universe(ws, ["AAPL.XNAS"], AS_OF)


def test_a_workspace_with_no_instruments_file_still_answers(ws: Workspace) -> None:
    ws.path("instruments.yaml").unlink()
    with pytest.raises(ValidationError, match="no entry in instruments.yaml"):
        resolve_universe(ws, ["AAPL"], AS_OF)


# --- the four failures --------------------------------------------------------


def test_an_unknown_id_names_itself(ws: Workspace) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve_universe(ws, ["NOSUCH"], AS_OF)
    assert caught.value.code is Exit.VALIDATION
    assert "NOSUCH" in caught.value.message
    assert "unknown" in caught.value.message
    assert "no reference adapter is configured" in caught.value.message


def test_an_id_ambiguous_across_venues_names_both(ws: Workspace) -> None:
    write(ws, AAPL_US=EQUITY, AAPL_MX={**EQUITY, "nautilus_id": "AAPL.BMV"})
    with pytest.raises(ValidationError) as caught:
        resolve_universe(ws, ["AAPL"], AS_OF)
    assert "AAPL: ambiguous across venues" in caught.value.message
    assert "AAPL.XNAS" in caught.value.message
    assert "AAPL.BMV" in caught.value.message


def test_an_instrument_delisted_before_the_date_names_the_date(ws: Workspace) -> None:
    write(ws, AAPL={**EQUITY, "attributes": {"delisted": "2023-04-05"}})
    with pytest.raises(ValidationError) as caught:
        resolve_universe(ws, ["AAPL"], AS_OF)
    assert "AAPL: delisted 2023-04-05, before 2024-06-03" in caught.value.message


def test_an_instrument_listed_after_the_date_names_the_date(ws: Workspace) -> None:
    write(ws, AAPL={**EQUITY, "attributes": {"listed": "2025-01-06"}})
    with pytest.raises(ValidationError) as caught:
        resolve_universe(ws, ["AAPL"], AS_OF)
    assert "AAPL: listed after 2024-06-03: it was listed 2025-01-06" in caught.value.message


def test_a_derivative_states_its_window_in_the_engines_own_fields(ws: Workspace) -> None:
    write(ws, ES=FUTURE)
    with pytest.raises(ValidationError, match="delisted 2024-12-20"):
        resolve_universe(ws, ["ES"], date(2025, 3, 3))
    with pytest.raises(ValidationError, match="it was listed 2024-01-02"):
        resolve_universe(ws, ["ES"], date(2023, 3, 3))


def test_every_failing_id_is_reported_together(ws: Workspace) -> None:
    write(ws, AAPL={**EQUITY, "attributes": {"delisted": "2023-04-05"}})
    with pytest.raises(ValidationError) as caught:
        resolve_universe(ws, ["ZZZ", "AAPL"], AS_OF)
    assert caught.value.message.index("AAPL") < caught.value.message.index("ZZZ")
    assert caught.value.remedy is not None
    assert "instruments.yaml" in caught.value.remedy


def test_a_listing_date_that_is_not_a_date_is_refused(ws: Workspace) -> None:
    write(ws, AAPL={**EQUITY, "attributes": {"listed": "one day"}})
    with pytest.raises(ValidationError, match="is not a date"):
        resolve_universe(ws, ["AAPL"], AS_OF)


def test_an_entry_that_is_neither_manual_nor_resolved_refuses(ws: Workspace) -> None:
    write(ws, AAPL={**EQUITY, "manual": False})
    with pytest.raises(ValidationError) as caught:
        resolve_universe(ws, ["AAPL"], AS_OF)
    assert "neither `manual` nor freshly resolved" in caught.value.message


def test_a_configured_adapter_that_is_not_installed_is_named(ws: Workspace) -> None:
    with pytest.raises(ValidationError, match="no reference adapter named 'some_vendor'"):
        resolve_universe(reading(ws, "some_vendor"), ["AAPL"], AS_OF)


# --- the reference adapter ----------------------------------------------------


def test_a_reference_adapter_answers_what_the_file_cannot(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Probe(answers={"MSFT": equity("MSFT.XNAS")})
    resolved = resolve_universe(probing(ws, probe, monkeypatch), ["MSFT"], AS_OF)

    assert probe.asked == [("MSFT",)]
    assert resolved["MSFT"].id.value == "MSFT.XNAS"
    assert definition_checksum(resolved["MSFT"]) in read_store(ws)


def test_what_the_adapter_answers_is_cached_with_its_provenance(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Probe(answers={"MSFT": equity("MSFT.XNAS")})
    resolved = resolve_universe(probing(ws, probe, monkeypatch), ["MSFT"], AS_OF)

    entry = cache(ws)["MSFT"]
    assert entry.resolved is not None
    assert entry.resolved.adapter == "probe"
    assert entry.resolved.as_of == AS_OF
    assert entry.resolved.checksum == definition_checksum(resolved["MSFT"])
    assert entry.sources == {"probe": "msft"}
    assert entry.nautilus_id == "MSFT.XNAS"


def test_an_id_the_adapter_does_not_know_fails_with_its_own_reason(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError, match="the probe knows none"):
        resolve_universe(probing(ws, Probe(), monkeypatch), ["MSFT"], AS_OF)


def test_an_override_wins_over_a_resolved_field(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter reports a cent; the operator says a nickel, and the engine gets a nickel."""
    write(ws, MSFT={**MSFT, "manual": False, "override": {"price_increment": "0.05"}})
    probe = Probe(answers={"MSFT": equity("MSFT.XNAS", increment="0.01")})
    resolved = resolve_universe(probing(ws, probe, monkeypatch), ["MSFT"], AS_OF)

    assert str(resolved["MSFT"].price_increment) == "0.05"
    assert resolved["MSFT"].price_precision == 2
    assert cache(ws)["MSFT"].resolved is not None
    assert cache(ws)["MSFT"].resolved.checksum == definition_checksum(resolved["MSFT"])


def test_a_resolution_leaves_the_operators_own_fields_alone(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        ws,
        MSFT={
            **MSFT,
            "manual": False,
            "corporate_actions": "none",
            "override": {"currency": "USD"},
            "attributes": {"sector": "tech"},
            "sources": {"other": "MSFT.US"},
        },
        AAPL=EQUITY,
    )
    probe = Probe(answers={"MSFT": equity("MSFT.XNAS")})
    resolve_universe(probing(ws, probe, monkeypatch), ["MSFT", "AAPL"], AS_OF)

    after = cache(ws)
    assert after["MSFT"].corporate_actions == "none"
    assert after["MSFT"].attributes == {"sector": "tech"}
    assert after["MSFT"].override == {"currency": "USD"}
    assert after["MSFT"].sources == {"other": "MSFT.US", "probe": "msft"}
    assert after["AAPL"] == cache(ws)["AAPL"]
    assert list(after.root) == ["MSFT", "AAPL"]


def test_a_resolved_derivative_records_the_class_it_must_be_rebuilt_as(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = InstrumentEntry.model_validate(FUTURE)
    contract = build(entry, conventions_for(entry, AS_OF))
    probe = Probe(answers={"ESZ4": contract})
    resolved = resolve_universe(probing(ws, probe, monkeypatch), ["ESZ4"], AS_OF)

    assert definition_checksum(resolved["ESZ4"]) == definition_checksum(contract)
    assert cache(ws)["ESZ4"].override == {"instrument_class": "future"}
    assert cache(ws)["ESZ4"].asset_class == "INDEX"


def test_resolving_nothing_changes_nothing(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    before = ws.path("instruments.yaml").read_text(encoding="utf-8")
    assert resolve_universe(probing(ws, Probe(), monkeypatch), [], AS_OF) == {}
    assert ws.path("instruments.yaml").read_text(encoding="utf-8") == before


# --- the cache ----------------------------------------------------------------


def resolve_then_stale(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> tuple[Probe, Workspace]:
    """A workspace whose MSFT entry is resolved and whose store holds the definition."""
    probe = Probe(answers={"MSFT": equity("MSFT.XNAS")})
    configured = probing(ws, probe, monkeypatch)
    resolve_universe(configured, ["MSFT"], AS_OF)
    probe.asked.clear()
    return probe, configured


def test_a_fresh_cache_answers_without_asking_the_adapter(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, configured = resolve_then_stale(ws, monkeypatch)
    resolved = resolve_universe(configured, ["MSFT"], AS_OF)
    assert probe.asked == []
    assert resolved["MSFT"].id.value == "MSFT.XNAS"


def test_a_cache_resolved_as_of_another_date_is_another_question(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, configured = resolve_then_stale(ws, monkeypatch)
    resolve_universe(configured, ["MSFT"], date(2024, 6, 4))
    assert probe.asked == [("MSFT",)]


def test_a_cache_the_store_no_longer_holds_is_no_cache(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, configured = resolve_then_stale(ws, monkeypatch)
    for tree in (ws.path("catalog", "data"), ws.path("catalog")):
        if tree.is_dir():
            for child in tree.rglob("*.parquet"):
                child.unlink()
    resolve_universe(configured, ["MSFT"], AS_OF)
    assert probe.asked == [("MSFT",)]


def test_an_override_edited_since_the_resolution_makes_the_cache_stale(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, configured = resolve_then_stale(ws, monkeypatch)
    held = cache(ws)
    write(
        ws,
        MSFT={
            **held["MSFT"].model_dump(mode="json", exclude_none=True),
            "override": {"price_increment": "0.05"},
        },
    )
    with pytest.raises(PreconditionError, match="MSFT.XNAS: the store already holds"):
        resolve_universe(configured, ["MSFT"], AS_OF)
    assert probe.asked == [("MSFT",)]

    resolved = resolve_universe(configured, ["MSFT"], AS_OF, refresh=True)
    assert str(resolved["MSFT"].price_increment) == "0.05"
    assert str(current_definitions(ws)["MSFT.XNAS"].price_increment) == "0.05"


def test_resolving_without_recording_writes_nothing(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question is answered, and the store and the cache are as they were."""
    probe = Probe(answers={"MSFT": equity("MSFT.XNAS")})
    before = ws.path("instruments.yaml").read_bytes()

    resolved = resolve_universe(probing(ws, probe, monkeypatch), ["MSFT"], AS_OF, record=False)

    assert probe.asked == [("MSFT",)]
    assert resolved["MSFT"].id.value == "MSFT.XNAS"
    assert read_store(ws) == {}
    assert ws.path("instruments.yaml").read_bytes() == before


# --- the provider protocol ----------------------------------------------------


def test_the_manual_provider_reports_rather_than_raises() -> None:
    provider = ManualProvider(InstrumentsFile({"AAPL": {**EQUITY, "manual": False}}))
    answered = provider.resolve(["AAPL", "NOPE"], AS_OF)
    assert isinstance(answered["AAPL"], ResolveError)
    assert "is not `manual`" in answered["AAPL"].reason
    assert isinstance(answered["NOPE"], ResolveError)
    assert str(answered["NOPE"]) == "NOPE: unknown: no entry in instruments.yaml"


def test_the_manual_provider_reports_a_date_it_cannot_answer_for() -> None:
    provider = ManualProvider(InstrumentsFile({"ES": FUTURE}))
    answered = provider.resolve(["ES"], date(2025, 3, 3))
    assert isinstance(answered["ES"], ResolveError)
    assert answered["ES"].reason.startswith("delisted")


def test_a_listing_window_may_be_stated_as_a_date_or_as_nanoseconds() -> None:
    """A hand-written file says 2024-01-02; a resolved one says the engine's nanoseconds."""
    entry = InstrumentEntry.model_validate(FUTURE)
    contract = build(entry, conventions_for(entry, AS_OF))
    provider = ManualProvider(
        InstrumentsFile(
            {
                "AAPL": {
                    **EQUITY,
                    "attributes": {
                        "listed": date(2024, 1, 2),
                        "delisted": datetime(2024, 12, 31, tzinfo=UTC),
                    },
                },
                "ES": {
                    **FUTURE,
                    "override": {
                        **FUTURE["override"],
                        "activation_ns": contract.activation_ns,
                        "expiration_ns": contract.expiration_ns,
                    },
                },
            }
        )
    )
    answered = provider.resolve(["AAPL", "ES"], AS_OF)
    assert [type(item).__name__ for item in answered.values()] == ["Equity", "FuturesContract"]


def test_a_cached_derivative_is_still_a_cache_hit(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`instrument_class` tells kanso which class to build, so it is no field to compare."""
    entry = InstrumentEntry.model_validate(FUTURE)
    contract = build(entry, conventions_for(entry, AS_OF))
    probe = Probe(answers={"ESZ4": contract})
    configured = probing(ws, probe, monkeypatch)
    resolve_universe(configured, ["ESZ4"], AS_OF)
    probe.asked.clear()

    resolved = resolve_universe(configured, ["ESZ4"], AS_OF)
    assert probe.asked == []
    assert definition_checksum(resolved["ESZ4"]) == definition_checksum(contract)


def test_the_manual_provider_reports_the_symbols_a_vendor_knows() -> None:
    provider = ManualProvider(
        InstrumentsFile({"AAPL": {**EQUITY, "sources": {"some_vendor": "AAPL.US"}}})
    )
    assert provider.sources("AAPL") == {"some_vendor": "AAPL.US"}
    assert provider.sources("NOPE") == {}


def test_the_core_ships_no_reference_provider() -> None:
    """kanso knows no vendor: every entry in the registry is placed by an adapter."""
    assert instruments.PROVIDERS == {}


def test_a_definition_written_by_hand_is_still_written(ws: Workspace) -> None:
    write_store(ws, [equity()])
    assert len(read_store(ws)) == 1


# --- the store as the registry of record --------------------------------------


def test_a_same_dated_correction_is_refused_until_refreshed(ws: Workspace) -> None:
    """What an instrument was on a date is one fact, so correcting it is explicit."""
    write(ws, AAPL=EQUITY)
    first = resolve_universe(ws, ["AAPL"], AS_OF)["AAPL"]
    write(ws, AAPL=CORRECTED)

    with pytest.raises(PreconditionError) as raised:
        resolve_universe(ws, ["AAPL"], AS_OF)

    assert raised.value.code is Exit.PRECONDITION
    assert raised.value.message.startswith(
        f"AAPL.XNAS: the store already holds a definition as of {AS_OF}"
    )
    assert "--refresh" in str(raised.value.remedy)
    assert set(read_store(ws)) == {definition_checksum(first)}


def test_a_refresh_replaces_the_same_dated_definition_in_the_store(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """The store holds what the refresh reported — one definition, and the engine says nothing."""
    write(ws, AAPL=EQUITY)
    resolve_universe(ws, ["AAPL"], AS_OF)
    write(ws, AAPL=CORRECTED)
    capsys.readouterr()

    corrected = resolve_universe(ws, ["AAPL"], AS_OF, refresh=True)["AAPL"]

    assert set(read_store(ws)) == {definition_checksum(corrected)}
    assert str(current_definitions(ws)["AAPL.XNAS"].price_increment) == "0.05"
    assert capsys.readouterr().out == ""


def test_a_definition_dated_otherwise_is_added_beside_the_held_one(ws: Workspace) -> None:
    write(ws, AAPL=EQUITY)
    earlier = resolve_universe(ws, ["AAPL"], AS_OF)["AAPL"]
    write(ws, AAPL=CORRECTED)

    later = resolve_universe(ws, ["AAPL"], date(2024, 6, 4))["AAPL"]

    assert set(read_store(ws)) == {definition_checksum(earlier), definition_checksum(later)}
    assert current_definitions(ws)["AAPL.XNAS"].ts_init == later.ts_init


def test_current_definitions_keep_the_newest_dated_definition_of_each_id(ws: Workspace) -> None:
    older = equity(increment="0.01", as_of=date(2024, 1, 2))
    newer = equity(increment="0.05", as_of=date(2025, 3, 3))
    other = equity("MSFT.XNAS")
    write_store(ws, [newer, older, other])

    current = current_definitions(ws)

    assert set(current) == {"AAPL.XNAS", "MSFT.XNAS"}
    assert definition_checksum(current["AAPL.XNAS"]) == definition_checksum(newer)
    assert len(read_store(ws)) == 3


def test_a_write_the_engine_skipped_is_a_failure(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine skips a write whose file exists and says so on stdout; here it is an error."""
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    write_store(ws, [equity()])
    monkeypatch.setattr(ParquetDataCatalog, "delete_data_range", lambda *args, **kw: None)

    with pytest.raises(KansoError) as raised:
        write_store(ws, [equity(increment="0.05")], replace=True)

    assert raised.value.code is Exit.ERROR
    assert "AAPL.XNAS" in raised.value.message
    assert "skipped the write" in raised.value.message


def test_the_demo_workspace_resolves_its_instrument(tmp_path: Path) -> None:
    """`init --demo` ships one manual entry, and it must build with no adapter configured."""
    demo = init(tmp_path / "demo", demo=True)
    resolved = resolve_universe(demo, ["DEMO"], AS_OF)

    assert type(resolved["DEMO"]).__name__ == "Equity"
    assert resolved["DEMO"].id.value == "DEMO.SIM"
    assert str(resolved["DEMO"].price_increment) == "0.01"
    assert definition_checksum(resolved["DEMO"]) in read_store(demo)
