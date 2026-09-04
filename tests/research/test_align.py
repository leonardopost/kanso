"""The deterministic half of alignment: universe, resolution and data types in the AST."""

from __future__ import annotations

from typing import Any

import pytest

from kanso.research.align import REASON_LIMIT, align_static, problems
from kanso.schemas import Hypothesis

from .conftest import DOCUMENT, INSTRUMENT


def hypothesis(**changes: Any) -> Hypothesis:
    """The demo hypothesis with these fields replaced."""
    return Hypothesis.model_validate({**DOCUMENT, **changes})


def aligned(source: str, **changes: Any) -> bool:
    ok, _ = align_static(hypothesis(**changes), source.encode())
    return ok


def test_the_hypothesis_s_own_instrument_and_bar_are_aligned() -> None:
    assert aligned(
        f'''
from nautilus_trader.model.data import BarSpecification
from nautilus_trader.model.enums import BarAggregation

SPEC = BarSpecification(1, BarAggregation.DAY, 1)
BAR = "{INSTRUMENT}-1-DAY-LAST-EXTERNAL"
SELF = "{INSTRUMENT}"


def on_bar(bar):
    pass
'''
    )


def test_an_instrument_outside_the_universe_is_drift() -> None:
    ok, reason = align_static(hypothesis(), b'TARGET = "AAPL.XNAS"\n')
    assert not ok
    assert reason is not None
    assert "'AAPL.XNAS' is not in the universe" in reason


def test_the_bare_symbol_of_a_universe_id_is_not_drift() -> None:
    assert aligned('SYMBOL = "DEMO"\n')


def test_a_bar_type_string_is_read_for_both_its_instrument_and_its_step() -> None:
    ok, reason = align_static(hypothesis(), b'BAR = "AAPL.XNAS-5-MINUTE-LAST-EXTERNAL"\n')
    assert not ok
    assert reason is not None
    assert "'AAPL.XNAS' is not in the universe" in reason
    assert "a 5-MINUTE bar, but the resolution is '1d' (1-DAY)" in reason


def test_a_bar_specification_off_the_resolution_is_drift() -> None:
    source = b"SPEC = BarSpecification(15, BarAggregation.MINUTE, 1)\n"
    ok, reason = align_static(hypothesis(), source)
    assert not ok
    assert reason is not None
    assert "a 15-MINUTE bar" in reason


def test_a_bar_specification_reached_through_a_module_is_read_too() -> None:
    source = b"SPEC = data.BarSpecification(2, BarAggregation.DAY, 1)\n"
    assert not aligned(source.decode())


def test_a_bar_at_all_is_drift_when_the_resolution_is_not_a_bar_size() -> None:
    ok, reason = align_static(
        hypothesis(resolution="tick", data_requirements=["trade"]),
        b"SPEC = BarSpecification(1, BarAggregation.DAY, 1)\n",
    )
    assert not ok
    assert reason is not None
    assert "which is not a bar size" in reason


@pytest.mark.parametrize(
    "source",
    [
        "SPEC = BarSpecification(step, BarAggregation.DAY, 1)",
        "SPEC = BarSpecification(1, aggregation, 1)",
        "SPEC = BarSpecification(1, Other.DAY, 1)",
        "SPEC = BarSpecification(1)",
        "SPEC = other(1, BarAggregation.MINUTE, 1)",
        "SPEC = builders[0](1, BarAggregation.MINUTE, 1)",
        "COUNT = 5",
    ],
)
def test_what_the_checks_cannot_read_is_not_evidence_of_drift(source: str) -> None:
    assert aligned(source + "\n")


def test_a_subscription_for_a_type_the_hypothesis_does_not_require_is_drift() -> None:
    ok, reason = align_static(
        hypothesis(), b"def start(self):\n    self.subscribe_quote_ticks(1)\n"
    )
    assert not ok
    assert reason is not None
    assert "subscribe_quote_ticks reads quote data, which is not required (bar)" in reason


def test_a_handler_for_a_type_the_hypothesis_does_not_require_is_drift() -> None:
    assert not aligned("def on_trade_tick(self, tick):\n    pass\n")


def test_a_handler_for_a_required_type_is_aligned() -> None:
    assert aligned(
        "def on_trade_tick(self, tick):\n    pass\n",
        resolution="tick",
        data_requirements=["trade"],
    )


def test_a_source_that_does_not_parse_cannot_be_shown_to_test_anything() -> None:
    ok, reason = align_static(hypothesis(), b"def on_bar(\n")
    assert not ok
    assert reason is not None
    assert "does not parse" in reason


def test_the_reason_is_capped_at_what_the_model_half_is_allowed() -> None:
    source = "\n".join(f'X{i} = "SYM{i}.XNAS"' for i in range(40))
    ok, reason = align_static(hypothesis(), source.encode())
    assert not ok
    assert reason is not None
    assert len(reason) == REASON_LIMIT
    assert reason.endswith("…")


def test_every_problem_is_reported_once_and_in_file_order() -> None:
    source = b'A = "AAPL.XNAS"\nB = "AAPL.XNAS"\nC = "MSFT.XNAS"\n'
    found = problems(hypothesis(), source)
    assert len(found) == 3
    assert found[0].startswith("line 1")
    assert found[2].startswith("line 3")
