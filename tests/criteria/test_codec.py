"""The one codec every stored run goes through: whatever a run holds comes back whole.

The named cases — a field added after records were already stored, read back with the
default stating what such a record measured — are in `tests/portfolio/test_records.py`,
written when the stage record was the codec's only user. This is the property over every
run the strategy draws, which is what a card's measurement leans on: the objective on the
decoded run is the card's metric only if decoding loses nothing.
"""

from __future__ import annotations

import json

from hypothesis import given

from kanso.criteria.codec import decode_run, encode_run
from kanso.criteria.run import CardRun
from kanso.portfolio import records as portfolio_records
from tests.schemas import strategies as gen


@given(gen.card_runs())
def test_any_run_survives_the_round_trip_through_json(run: CardRun) -> None:
    """Through the text a blob or an event holds, not just the dict: a float survives JSON
    only because `json` writes its shortest exact repr, and that is the property relied on."""
    text = json.dumps(encode_run(run), sort_keys=True, separators=(",", ":"))

    assert decode_run(json.loads(text)) == run


def test_the_stage_record_encodes_with_the_same_codec() -> None:
    """One codec: the stage record's names are the codec's own functions, not a copy."""
    assert portfolio_records.encode_run is encode_run
    assert portfolio_records.decode_run is decode_run
