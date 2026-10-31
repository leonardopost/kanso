"""The run record and the card."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import (
    DIGEST_COLUMNS,
    RESULTS_HEADER,
    TAGS,
    Card,
    CardDigest,
    GateResult,
    RunRecord,
    resolve_venue_model,
)

SHA = "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)
VENUE = resolve_venue_model("XNAS", max_leverage=1.0)

RUN: dict[str, Any] = {
    "run_id": "r1",
    "hyp_id": "demo_mr",
    "tag": "20260101-1",
    "lane": "op",
    "dir": "runs/op/demo_mr",
    "base_sha": SHA,
    "hypothesis_sha": SHA,
    "program_sha": SHA,
    "snapshot_id": "s1",
    "criteria_version": "0.1.0",
    "card_budget_s": 60.0,
    "baseline_wall_s": 12.0,
    "baseline_peak_mem_gb": 1.5,
    "started_at": NOW,
}

CARD: dict[str, Any] = {
    "run_id": "r1",
    "lane": "op",
    "strategy_sha": SHA,
    "metric": 1.25,
    "metric_se": 0.5,
    "n_trials": 3,
    "n_trades": 40,
    "wall_s": 2.5,
    "peak_mem_gb": 1.0,
    "status": "keep",
    "desc": "widen the band",
    "venue_model": VENUE,
    "created_at": NOW,
}


def test_a_run_record_validates() -> None:
    record = RunRecord.model_validate(RUN)
    assert record.best_sha is None
    assert record.ended_at is None


@pytest.mark.parametrize("tag", ["2026-1", "20260101", "20260101-", "x-1"])
def test_the_tag_is_dated_and_numbered(tag: str) -> None:
    with pytest.raises(ValidationError, match="tag"):
        RunRecord.model_validate({**RUN, "tag": tag})


def test_a_run_may_not_end_before_it_started() -> None:
    with pytest.raises(ValidationError, match="ended_at"):
        RunRecord.model_validate({**RUN, "ended_at": datetime(2025, 1, 1, tzinfo=UTC)})


def test_best_metric_travels_with_best_sha() -> None:
    with pytest.raises(ValidationError, match="best_metric"):
        RunRecord.model_validate({**RUN, "best_sha": SHA})
    with pytest.raises(ValidationError, match="best_metric"):
        RunRecord.model_validate({**RUN, "best_metric": 1.0})
    assert RunRecord.model_validate({**RUN, "best_sha": SHA, "best_metric": 1.0}).best_metric == 1.0


def test_a_sha_is_sixty_four_hex_digits() -> None:
    with pytest.raises(ValidationError, match="base_sha"):
        RunRecord.model_validate({**RUN, "base_sha": "abc"})
    with pytest.raises(ValidationError, match="base_sha"):
        RunRecord.model_validate({**RUN, "base_sha": "A" * 64})


def test_the_results_row() -> None:
    card = Card.model_validate(CARD)
    assert card.sha7 == "a" * 7
    assert card.row().split("\t") == [
        "aaaaaaa",
        "1.250000",
        "0.500000",
        "3",
        "40",
        "2.500",
        "1.000",
        "keep",
        "widen the band",
        *[""] * len(DIGEST_COLUMNS),
    ]
    assert len(card.row().split("\t")) == len(RESULTS_HEADER.split("\t"))


DIGEST: dict[str, Any] = {
    "sessions": 20,
    "fills": 80,
    "trades_per_session": 2.0,
    "hit_rate": 0.55,
    "median_hold_s": 1800.0,
    "maker_share": 0.25,
    "fill_ratio": 0.5,
    "gross_bps_per_trade": 3.5,
    "cost_bps_per_trade": 4.0,
    "net_bps_per_trade": -0.5,
    "net_bps_per_trade_se": 0.25,
    "gross_bps_per_session": 1.5,
    "cost_bps_per_session": 1.6,
    "net_bps_per_session": -0.1,
    "period": "1d",
    "filled_periods": 19,
    "first_fill_s": 0.25,
    "last_fill_s": 76.0,
    "active_s": 75.5,
}


def test_a_digest_fills_the_columns_after_the_description() -> None:
    """Appended, not inserted: the nine columns a reader took by position are where they
    were, and the description is still the ninth."""
    card = Card.model_validate({**CARD, "digest": DIGEST})
    cells = card.row().split("\t")
    assert cells[7:9] == ["keep", "widen the band"]
    assert dict(zip(DIGEST_COLUMNS, cells[9:], strict=True)) == {
        "trades_per_session": "2.0000",
        "hit_rate": "0.5500",
        "median_hold_s": "1800.0000",
        "maker_share": "0.2500",
        "fill_ratio": "0.5000",
        "gross_bps_per_trade": "3.5000",
        "cost_bps_per_trade": "4.0000",
        "net_bps_per_trade": "-0.5000",
        "net_bps_per_trade_se": "0.2500",
        "gross_bps_per_session": "1.5000",
        "cost_bps_per_session": "1.6000",
        "net_bps_per_session": "-0.1000",
        "first_fill_s": "0.2500",
        "last_fill_s": "76.0000",
        "active_s": "75.5000",
    }


UNTIMED: dict[str, Any] = {
    "filled_periods": 0,
    "first_fill_s": None,
    "last_fill_s": None,
    "active_s": None,
}
"""When in its periods a run that never filled filled: in none of them, at no offset."""


def test_an_absent_digest_number_is_an_empty_cell_and_not_a_zero() -> None:
    quiet = CardDigest.model_validate(
        {**DIGEST, "fill_ratio": None, "maker_share": None, "fills": 0, **UNTIMED}
    )
    cells = dict(zip(DIGEST_COLUMNS, quiet.cells(), strict=True))
    assert (cells["fill_ratio"], cells["maker_share"]) == ("", "")
    assert (cells["first_fill_s"], cells["last_fill_s"], cells["active_s"]) == ("", "", "")


def test_a_crashed_card_digests_nothing() -> None:
    with pytest.raises(ValidationError, match="digest"):
        Card.model_validate(
            {**CARD, "status": "crash", "metric": 0.0, "crash_tail": "boom", "digest": DIGEST}
        )


def test_a_crashed_card_keeps_no_measurement_and_a_card_that_ran_names_one() -> None:
    crashed = {**CARD, "status": "crash", "metric": 0.0, "crash_tail": "boom"}
    with pytest.raises(ValidationError, match="measured_sha"):
        Card.model_validate({**crashed, "measured_sha": SHA})
    assert Card.model_validate(crashed).measured_sha is None
    assert Card.model_validate({**CARD, "measured_sha": SHA}).measured_sha == SHA
    assert (
        Card.model_validate(CARD).row() == Card.model_validate({**CARD, "measured_sha": SHA}).row()
    ), "results.tsv is read by position, and the measurement adds no column"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"hit_rate": None}, "net_bps_per_trade"),
        ({"sessions": 0}, "net_bps_per_session"),
        ({"trades_per_session": None}, "net_bps_per_session"),
        ({"fills": 0}, "maker_share"),
        ({"gross_bps_per_trade": 4.5}, "gross"),
        ({"cost_bps_per_session": 2.0}, "gross"),
        ({"hit_rate": 1.5}, "hit_rate"),
        ({"net_bps_per_trade_se": -1.0}, "net_bps_per_trade_se"),
        ({"filled_periods": 0}, "filled_periods"),
        ({"filled_periods": 81}, "filled_periods"),
        ({"filled_periods": -1}, "filled_periods"),
        ({"fills": 0, "maker_share": None}, "filled_periods"),
        ({"filled_periods": None}, "period"),
        ({"period": None}, "period"),
        ({"period": "a day"}, "period"),
        ({"filled_periods": None, "period": None}, "filled_periods"),
        ({"first_fill_s": None}, "first_fill_s"),
        ({"active_s": None}, "first_fill_s"),
        ({"fills": 0, "maker_share": None, "filled_periods": 0}, "first_fill_s"),
        ({"last_fill_s": 0.1}, "last_fill_s"),
        ({"active_s": 76.5}, "active_s"),
        ({"first_fill_s": -1.0}, "first_fill_s"),
    ],
)
def test_a_digest_states_its_numbers_together_and_they_add_up(
    change: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        CardDigest.model_validate({**DIGEST, **change})


def test_an_edge_whose_trades_closed_in_one_period_states_no_error() -> None:
    """An error read period by period is zero by construction when one period holds every
    trade, so the edge stands without one; an error with no edge stands for nothing."""
    alone = CardDigest.model_validate({**DIGEST, "net_bps_per_trade_se": None})
    assert alone.net_bps_per_trade == -0.5 and alone.net_bps_per_trade_se is None
    assert dict(zip(DIGEST_COLUMNS, alone.cells(), strict=True))["net_bps_per_trade_se"] == ""
    no_edge = dict.fromkeys(
        (
            "hit_rate",
            "median_hold_s",
            "gross_bps_per_trade",
            "cost_bps_per_trade",
            "net_bps_per_trade",
        )
    )
    with pytest.raises(ValidationError, match="net_bps_per_trade_se"):
        CardDigest.model_validate({**DIGEST, **no_edge})


def test_a_run_that_closed_nothing_digests_its_sessions_alone() -> None:
    idle = CardDigest(
        sessions=5,
        fills=0,
        trades_per_session=0.0,
        **dict.fromkeys(
            ("gross_bps_per_session", "cost_bps_per_session", "net_bps_per_session"), 0.0
        ),
    )
    assert idle.net_bps_per_trade is None and idle.hit_rate is None


def test_when_a_card_traded_is_appended_after_the_columns_a_reader_took_by_position() -> None:
    """The twelve digest columns `results.tsv` has carried since 0.17 sit where they sat,
    and the three that say when in its periods a card filled come after them."""
    assert DIGEST_COLUMNS == (
        "trades_per_session",
        "hit_rate",
        "median_hold_s",
        "maker_share",
        "fill_ratio",
        "gross_bps_per_trade",
        "cost_bps_per_trade",
        "net_bps_per_trade",
        "net_bps_per_trade_se",
        "gross_bps_per_session",
        "cost_bps_per_session",
        "net_bps_per_session",
        "first_fill_s",
        "last_fill_s",
        "active_s",
    )


def test_a_digest_recorded_before_it_timed_its_fills_reads_back_with_no_timing() -> None:
    """A card row written before the digest kept when its fills fell holds none of the five
    keys: it reads back with no timing and three empty cells, and every other cell as it
    was, rather than refused."""
    timing = {"period", *UNTIMED}
    before = CardDigest.model_validate({k: v for k, v in DIGEST.items() if k not in timing})

    assert (before.period, before.filled_periods) == (None, None)
    assert (before.first_fill_s, before.last_fill_s, before.active_s) == (None, None, None)
    assert before.cells() == (*CardDigest.model_validate(DIGEST).cells()[:-3], "", "", "")


def test_a_crashed_card_measures_nothing() -> None:
    with pytest.raises(ValidationError, match="metric"):
        Card.model_validate({**CARD, "status": "crash", "metric": 3.0})
    crashed = Card.model_validate({**CARD, "status": "crash", "metric": 0.0, "crash_tail": "boom"})
    assert crashed.crash_tail == "boom"


def test_a_redundant_card_reports_what_it_measured() -> None:
    """It ran: nothing about repeating another card's book zeroes its own number, and the
    status is the only thing that says the result was already known."""
    repeat = Card.model_validate({**CARD, "status": "redundant", "metric": 1.25})
    assert (repeat.status, repeat.metric) == ("redundant", 1.25)
    assert repeat.row().split("\t")[7] == "redundant"
    with pytest.raises(ValidationError, match="crash_tail"):
        Card.model_validate({**CARD, "status": "redundant", "crash_tail": "boom"})


def test_only_a_crash_carries_a_traceback() -> None:
    with pytest.raises(ValidationError, match="crash_tail"):
        Card.model_validate({**CARD, "crash_tail": "boom"})


def test_a_description_stays_on_one_row() -> None:
    with pytest.raises(ValidationError, match="desc"):
        Card.model_validate({**CARD, "desc": "two\tcolumns"})
    with pytest.raises(ValidationError, match="desc"):
        Card.model_validate({**CARD, "desc": "x" * 121})


def test_a_card_made_by_hand_carries_no_tags_and_a_tag_is_from_the_vocabulary() -> None:
    """The vocabulary is the package's: a coverage table keyed by free text has no cells."""
    assert Card.model_validate(CARD).tags == []
    tagged = Card.model_validate({**CARD, "tags": ["signal_trend", "exit_stop"]})
    assert tagged.tags == ["signal_trend", "exit_stop"]
    assert len(TAGS) == 21 and len(set(TAGS)) == len(TAGS)
    with pytest.raises(ValidationError, match="tags"):
        Card.model_validate({**CARD, "tags": ["clever"]})


def test_a_skipped_gate_passes() -> None:
    with pytest.raises(ValidationError, match="skipped gate passes"):
        GateResult.model_validate({"id": "book_correlation", "pass": False, "skipped": "none"})
    result = GateResult.model_validate(
        {"id": "book_correlation", "pass": True, "skipped": "nothing deployed"}
    )
    assert result.passed
    assert result.model_dump(by_alias=True)["pass"] is True
