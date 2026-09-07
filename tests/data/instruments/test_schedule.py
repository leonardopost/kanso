"""The split schedule on an instrument definition: accepted, addressed, and cached by it.

`info` is an equity's free-form map and the only structured field an override may set, so
these tests pin the three things that follow from that: the constructor takes it, the
content address includes it, and the cache goes stale when the operator edits it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from kanso.data.instruments import (
    build,
    conventions_for,
    current_definitions,
    definition_checksum,
    instruments_checksum,
    resolve_universe,
)
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.splits import schedule_of
from kanso.schemas import InstrumentEntry, InstrumentsFile, write_yaml
from kanso.workspace import Workspace

from .conftest import AS_OF, EQUITY, Probe

EX = date(2026, 7, 15)
SCHEDULE: dict[str, Any] = {"splits": [{"ex_date": EX, "ratio": 0.1}]}


def entry(**override: Any) -> InstrumentEntry:
    """The manual equity of the fixtures, with these override keys added."""
    spec = {**EQUITY, "override": {**EQUITY["override"], **override}}
    return InstrumentEntry.model_validate(spec)


def built(**override: Any) -> Any:
    item = entry(**override)
    return build(item, conventions_for(item, AS_OF))


# --- what the constructor takes ----------------------------------------------


def test_an_equity_accepts_a_split_schedule_in_its_info() -> None:
    """The one line that used to refuse `override: {info: ...}`."""
    assert schedule_of(built(info=SCHEDULE)) == schedule_of(built(info=SCHEDULE))
    assert [(split.ex_date, split.ratio) for split in schedule_of(built(info=SCHEDULE))] == [
        (EX, 0.1)
    ]


def test_a_schedule_written_as_a_date_and_as_a_string_is_one_definition() -> None:
    """YAML parses an unquoted date into a `date`; the stored definition is canonical."""
    written = built(info={"splits": [{"ex_date": "2026-07-15", "ratio": 0.1}]})

    assert definition_checksum(built(info=SCHEDULE)) == definition_checksum(written)


def test_the_schedule_reaches_the_content_address_and_therefore_the_snapshot() -> None:
    """`engine_fields` is `to_dict` and `info` is one of its keys, so this comes for free."""
    plain, scheduled = built(), built(info=SCHEDULE)

    assert definition_checksum(plain) != definition_checksum(scheduled)
    assert instruments_checksum([plain]) != instruments_checksum([scheduled])


def test_a_restated_ratio_is_a_different_definition() -> None:
    restated = {"splits": [{"ex_date": EX, "ratio": 0.05}]}

    assert definition_checksum(built(info=SCHEDULE)) != definition_checksum(built(info=restated))


def test_a_schedule_the_sleeve_could_not_apply_is_refused_at_construction() -> None:
    """Refused where the operator can act on it, not on the first bar of the ex-date."""
    with pytest.raises(ValidationError, match="a schedule carries no cash"):
        built(info={"splits": [{"ex_date": EX, "ratio": 0.1, "cash": 0.25}]})


def test_info_that_is_not_a_map_is_refused_by_the_engine_s_own_words() -> None:
    with pytest.raises(ValidationError, match="Equity rejected its fields"):
        built(info=[{"ex_date": EX, "ratio": 0.1}])


@pytest.mark.parametrize("name", ["SPX.XCBO", "EUR/USD.SIM"])
def test_only_an_equity_takes_a_schedule(name: str) -> None:
    """A split is an equity's corporate action; a dated derivative expires instead."""
    from .conftest import CLASSES

    spec = next(item for item in CLASSES.values() if item["nautilus_id"] == name)
    item = InstrumentEntry.model_validate(
        {**spec, "override": {**spec["override"], "info": SCHEDULE}}
    )

    with pytest.raises(ValidationError, match="has no field info"):
        build(item, conventions_for(item, AS_OF))


# --- what the cache does with it ---------------------------------------------


def write(ws: Workspace, **override: Any) -> None:
    write_yaml(InstrumentsFile({"AAPL": entry(**override)}), ws.path("instruments.yaml"))


def test_adding_a_schedule_to_a_resolved_instrument_is_a_correction(ws: Workspace) -> None:
    """The cache answers only while it honours the override, and a same-dated definition
    that changed is a correction the operator asks for by name."""
    write(ws)
    resolve_universe(ws, ["AAPL"], AS_OF)
    write(ws, info=SCHEDULE)

    with pytest.raises(PreconditionError, match="would put"):
        resolve_universe(ws, ["AAPL"], AS_OF)
    resolve_universe(ws, ["AAPL"], AS_OF, refresh=True)

    assert schedule_of(current_definitions(ws)["AAPL.XNAS"])


def test_a_schedule_that_did_not_change_leaves_the_cache_fresh(ws: Workspace) -> None:
    """A `date` in the file and an ISO string in the store are the same assertion."""
    write(ws, info=SCHEDULE)
    first = resolve_universe(ws, ["AAPL"], AS_OF)
    write(ws, info={"splits": [{"ex_date": "2026-07-15", "ratio": 0.1}]})

    again = resolve_universe(ws, ["AAPL"], AS_OF)

    assert definition_checksum(again["AAPL"]) == definition_checksum(first["AAPL"])


def test_a_cached_definition_still_honours_a_schedule_written_as_a_date(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache is stale only when the operator changed something, and a `date` in the
    file and the ISO string the store holds are not a change."""
    from .test_resolve import equity, probing

    probe = Probe(answers={"MSFT": equity("MSFT.XNAS")})
    configured = probing(ws, probe, monkeypatch)
    write_yaml(
        InstrumentsFile(
            {
                "MSFT": InstrumentEntry.model_validate(
                    {
                        "nautilus_id": "MSFT.XNAS",
                        "asset_class": "EQUITY",
                        "corporate_actions": "adjust_all",
                        "override": {"info": SCHEDULE},
                    }
                )
            }
        ),
        ws.path("instruments.yaml"),
    )
    resolve_universe(configured, ["MSFT"], AS_OF)
    probe.asked.clear()

    again = resolve_universe(configured, ["MSFT"], AS_OF)

    assert probe.asked == []
    assert [split.ex_date for split in schedule_of(again["MSFT"])] == [EX]
