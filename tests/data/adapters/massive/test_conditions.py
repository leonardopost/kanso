"""Which of the vendor's prints are lit: the reference behind it, the rules, and the loader
that keeps a print's terms beside it.

Both fixtures in `fixtures/reference/` are live responses recorded on 2026-10-28 with the key
in a header and stripped: the trade conditions reference, and the 376 SPY prints of
2025-01-02 15:00:00-15:00:02 UTC. `LIT` is an independent derivation's reading of the same
376 prints, read off the vendor's flat file for that session on 2026-10-29: each a lit,
regular-way execution or not, by the rules `kanso.data.adapters.massive.conditions` states.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kanso.data.adapters.massive import conditions
from kanso.data.adapters.massive.loaders.bars import Request
from kanso.data.adapters.massive.loaders.trades import (
    MassivePrintTermsLoader,
    build_terms,
    build_trade,
)
from kanso.data.types import PrintTerms
from tests.data.adapters.massive.test_loaders import load_one, on, tick, tick_loader, tick_spec

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = 335_806
LIT = frozenset(
    FIRST + offset
    for offset in [
        0,
        1,
        12,
        36,
        43,
        49,
        95,
        96,
        98,
        114,
        118,
        121,
        136,
        184,
        190,
        194,
        195,
        205,
        212,
        280,
        287,
        303,
        307,
        318,
        346,
        381,
        382,
        383,
        384,
        385,
        386,
        396,
        398,
        399,
        400,
        404,
        409,
        413,
        414,
        415,
        418,
        420,
        426,
        428,
        429,
        434,
        436,
        438,
        441,
        448,
        449,
        450,
        451,
        454,
        456,
        461,
        466,
        473,
        476,
        498,
        501,
        509,
        534,
        546,
        555,
        560,
        569,
        579,
        580,
        581,
        582,
        583,
        584,
        586,
        588,
        589,
        590,
        595,
        596,
        597,
        598,
        599,
        600,
        601,
        604,
        605,
        606,
        607,
        608,
        609,
        626,
        627,
        641,
        662,
        663,
        664,
        671,
        695,
        696,
        708,
        710,
        733,
        747,
        751,
        752,
        755,
        756,
        761,
        762,
        767,
        775,
        800,
        813,
        815,
        816,
        848,
        849,
        854,
        855,
        921,
        926,
        927,
        928,
        929,
        930,
        934,
        935,
        936,
        952,
        953,
        954,
        955,
        956,
        957,
    ]
)
"""The sequence numbers of the 134 recorded SPY prints the derivation reads lit."""

SPY = Request(
    symbol="SPY",
    venue="ARCX",
    ticker="SPY",
    asset_class="stocks",
    dataset="trades",
    resolution=None,
    adjusted=False,
    publication="realtime",
    publication_rule=None,
    price_precision=2,
    size_precision=0,
)


def recorded(name: str) -> Any:
    """A recorded response's body: the reference files wrap it beside its provenance, the
    cursor files are the body as served."""
    held = json.loads((FIXTURES / name).read_text())
    return held.get("body", held)


def test_the_condition_table_is_the_reference_the_vendor_served() -> None:
    """Each listed code's consolidated `updates_open_close`, and `None` for an indicator the
    reference gives no update rule."""
    served = {
        row["id"]: (row.get("update_rules") or {}).get("consolidated", {}).get("updates_open_close")
        for row in recorded("reference/conditions_trade.json")["results"]
    }
    assert dict(conditions.REFERENCE) == served


def test_the_recorded_spy_prints_read_lit_as_the_flat_file_derivation_reads_them() -> None:
    rows = recorded("reference/trades_SPY_2025-01-02T150000Z.json")["results"]
    terms = [build_terms(SPY, row) for row in rows]

    assert len(terms) == 376
    by_sequence = {row["sequence_number"]: term.lit for row, term in zip(rows, terms, strict=True)}
    assert {number for number, lit in by_sequence.items() if lit} == LIT
    assert (terms[0].exchange, terms[0].conditions) == ("11", "")
    assert (terms[1].exchange, terms[1].conditions) == ("12", "14,41")


def test_terms_are_kept_for_exactly_the_rows_a_print_is_made_of() -> None:
    """A row the trades loader makes no print of has no terms, and a print's terms carry its
    own trade id and instants."""
    rows = [
        *recorded("cursor/trades_page_1.json")["results"],
        *recorded("cursor/trades_page_2.json")["results"],
        *recorded("reference/trades_SPY_2025-01-02T150000Z.json")["results"][:5],
    ]
    for row in rows:
        printed, terms = build_trade(SPY, row), build_terms(SPY, row)
        assert (printed is None) == (terms is None)
        if printed is not None:
            assert str(printed.trade_id) == terms.trade_id
            assert (printed.ts_event, printed.ts_init) == (terms.ts_event, terms.ts_init)


LIT_ROW: dict[str, Any] = {
    "exchange": 11,
    "conditions": [14, 41],
    "price": 590.18,
    "participant_timestamp": 1_735_830_000_071_502_759,
    "sip_timestamp": 1_735_830_000_071_538_432,
}


@pytest.mark.parametrize(
    ("change", "lit"),
    [
        ({}, True),
        ({"conditions": [57]}, True),
        ({"correction": 8}, True),
        ({"exchange": conditions.TRF_EXCHANGE}, False),
        ({"trf_id": 202}, False),
        ({"conditions": [37]}, False),
        ({"conditions": [14, 12]}, False),
        ({"conditions": [8]}, False),
        ({"conditions": [99]}, False),
        ({"correction": 12}, False),
        ({"price": 590.185}, False),
        ({"price": "590.18"}, False),
        ({"price": True}, False),
        ({"sip_timestamp": 1_735_830_001_071_502_760}, False),
    ],
)
def test_each_rule_alone_keeps_a_print_off_the_lit_tape(change: dict[str, Any], lit: bool) -> None:
    """An exchange's regular-way print in whole cents carried within the second is lit; an
    indicator and a later mark leave it so; a facility's print, an odd lot, an extended-hours
    or closing print, a code the reference does not list, a correction record, a sub-penny
    price, a price that is no number and a print carried more than a second late each make it
    not lit."""
    row = {**LIT_ROW, **change}
    assert conditions.lit(row, row["participant_timestamp"], row["sip_timestamp"]) is lit


def test_a_row_without_a_participant_instant_is_not_lit() -> None:
    row = dict(LIT_ROW)
    del row["participant_timestamp"]
    assert conditions.lit(row, row["sip_timestamp"], row["sip_timestamp"]) is False


def test_massive_print_terms_loads_one_point_of_terms_per_print() -> None:
    rows = [tick(date(2024, 2, 1), exchange=4, trf_id=201, conditions=[12, 37])]
    loader, _ = tick_loader(MassivePrintTermsLoader, on(date(2024, 2, 1), rows))
    ref = loader.discover(tick_spec())[0]
    points = load_one(loader, ref)

    assert (ref.type, ref.vendor_dataset) == ("print_terms", "trades")
    assert all(type(point) is PrintTerms for point in points)
    assert [(p.exchange, p.conditions, p.lit) for p in points] == [
        ("4", "12,37", False),
        ("", "", True),
        ("", "", True),
    ]
