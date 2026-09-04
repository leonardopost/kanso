"""The run record and the card."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import RESULTS_HEADER, Card, GateResult, RunRecord, resolve_venue_model

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
    ]
    assert len(card.row().split("\t")) == len(RESULTS_HEADER.split("\t"))


def test_a_crashed_card_measures_nothing() -> None:
    with pytest.raises(ValidationError, match="metric"):
        Card.model_validate({**CARD, "status": "crash", "metric": 3.0})
    crashed = Card.model_validate({**CARD, "status": "crash", "metric": 0.0, "crash_tail": "boom"})
    assert crashed.crash_tail == "boom"


def test_only_a_crash_carries_a_traceback() -> None:
    with pytest.raises(ValidationError, match="crash_tail"):
        Card.model_validate({**CARD, "crash_tail": "boom"})


def test_a_description_stays_on_one_row() -> None:
    with pytest.raises(ValidationError, match="desc"):
        Card.model_validate({**CARD, "desc": "two\tcolumns"})
    with pytest.raises(ValidationError, match="desc"):
        Card.model_validate({**CARD, "desc": "x" * 121})


def test_a_skipped_gate_passes() -> None:
    with pytest.raises(ValidationError, match="skipped gate passes"):
        GateResult.model_validate({"id": "book_correlation", "pass": False, "skipped": "none"})
    result = GateResult.model_validate(
        {"id": "book_correlation", "pass": True, "skipped": "nothing deployed"}
    )
    assert result.passed
    assert result.model_dump(by_alias=True)["pass"] is True
