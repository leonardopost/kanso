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


def test_a_run_with_no_period_target_is_stored_as_it_was_and_one_with_one_keeps_it() -> None:
    """A run of a hypothesis with no period target writes no `period_stops` key, so its stored
    run, and a measurement blob of it, are what they were before runs kept the record; one
    stored without the key reads back with none, and a record written reads back whole."""
    from dataclasses import replace

    from kanso.schemas import PeriodStops
    from tests.criteria.builders import build_run

    run = build_run((1.0, -1.0))
    stored = encode_run(run)
    record = PeriodStops.model_validate({"openings": [5], "stops": []})

    assert "period_stops" not in stored
    assert decode_run(stored).period_stops is None
    kept = encode_run(replace(run, period_stops=record))
    assert kept["period_stops"] == {"openings": [5], "stops": [], "last": None}
    assert decode_run(kept).period_stops == record


def test_a_run_that_held_nothing_through_a_split_is_stored_as_it_was() -> None:
    """A run with no split applied to what it held writes no `adjustments` key, so it is stored
    as runs were before they kept them; one stored that way — every run recorded before —
    decodes with none, and a run that has some reads them back whole, the ratio included, and
    one recorded without a ratio as none; where it fell among the fills of its instant is
    written only where some came before it, and one recorded without it reads as before them
    all."""
    from dataclasses import replace

    from kanso.criteria.run import Adjustment
    from tests.criteria.builders import build_run

    run = build_run((1.0, -1.0))
    stored = encode_run(run)
    split = Adjustment(ts_ns=7, instrument_id="SOXS.ARCX", qty=-2_434.0, paid=23.42, ratio=0.2)
    unknown = Adjustment(ts_ns=9, instrument_id="SOXS.ARCX", qty=-1.0, paid=0.0)

    assert "adjustments" not in stored
    assert decode_run(stored).adjustments == ()
    kept = encode_run(replace(run, adjustments=(split, unknown)))
    assert kept["adjustments"] == [
        {"ts_ns": 7, "instrument_id": "SOXS.ARCX", "qty": -2_434.0, "paid": 23.42, "ratio": 0.2},
        {"ts_ns": 9, "instrument_id": "SOXS.ARCX", "qty": -1.0, "paid": 0.0, "ratio": None},
    ]
    assert decode_run(kept).adjustments == (split, unknown)
    without_ratio = {**kept, "adjustments": [{**kept["adjustments"][1]}]}
    del without_ratio["adjustments"][0]["ratio"]
    assert decode_run(without_ratio).adjustments == (unknown,)
    after = replace(split, fills_before=2)
    ordered = encode_run(replace(run, adjustments=(after,)))
    assert ordered["adjustments"][0]["fills_before"] == 2
    assert decode_run(ordered).adjustments == (after,)


def test_a_run_sized_without_the_cap_is_stored_as_it_was_and_its_bases_are_kept() -> None:
    """A run no `cap: room` sized writes no `sized` key, so it is stored as runs were before
    they kept the bases; one that has them reads them back whole, a clip's said where it is."""
    from dataclasses import replace

    from kanso.criteria.run import Sized
    from tests.criteria.builders import build_run

    run = build_run((1.0, -1.0))
    entry = Sized(ts_ns=7, instrument_id="SOXL.ARCX", side="BUY", base=27_449.5)
    clip = Sized(ts_ns=9, instrument_id="SOXS.ARCX", side="SELL", base=1_000.0, clip=True)

    assert "sized" not in encode_run(run)
    assert decode_run(encode_run(run)).sized == ()
    kept = encode_run(replace(run, sized=(entry, clip)))
    assert kept["sized"] == [
        {"ts_ns": 7, "instrument_id": "SOXL.ARCX", "side": "BUY", "base": 27_449.5},
        {"ts_ns": 9, "instrument_id": "SOXS.ARCX", "side": "SELL", "base": 1_000.0, "clip": True},
    ]
    assert decode_run(kept).sized == (entry, clip)
    assert run.between(0, 8).sized == ()
    assert replace(run, sized=(entry, clip)).between(0, 8).sized == (entry,)


def test_a_holding_says_its_mark_was_under_zero_only_where_it_was() -> None:
    """A holding marked at or above zero writes no `below_zero` key, so it is stored as
    holdings were before they said so, and one stored that way decodes as at or above zero;
    one marked under zero reads back as under it."""
    from dataclasses import replace

    from kanso.criteria.run import Held
    from tests.criteria.builders import build_run

    above = Held(ts_ns=1, instrument_id="CL.XNYM", qty=10.0, notional=500.0)
    under = Held(ts_ns=2, instrument_id="CL.XNYM", qty=10.0, notional=376.3, below_zero=True)

    stored = encode_run(replace(build_run((1.0, -1.0)), held=(above, under)))

    assert stored["held"] == [
        {"ts_ns": 1, "instrument_id": "CL.XNYM", "qty": 10.0, "notional": 500.0},
        {
            "ts_ns": 2,
            "instrument_id": "CL.XNYM",
            "qty": 10.0,
            "notional": 376.3,
            "below_zero": True,
        },
    ]
    assert decode_run(stored).held == (above, under)
