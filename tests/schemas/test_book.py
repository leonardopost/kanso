"""The shared book block: the operator's leverage, a name cap, and a ranking it must state."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import ValidationError
from kanso.schemas import SharedBook, dump_yaml, parse_yaml
from kanso.schemas.book import OVERNIGHT_LEVERAGE, RTH_LEVERAGE

BLOCK: dict[str, Any] = {
    "capital": 40_000.0,
    "leverage": {"rth": 4},
    "rank_key": "net_per_trade_bp",
    "k_min": 15,
}


@st.composite
def shared_books(draw: st.DrawFn) -> SharedBook:
    return SharedBook.model_validate(
        {
            "capital": draw(st.floats(1.0, 1e9, allow_nan=False)),
            "leverage": {
                "rth": RTH_LEVERAGE,
                **({"overnight": OVERNIGHT_LEVERAGE} if draw(st.booleans()) else {}),
            },
            "max_name_c": draw(st.none() | st.floats(0.01, 4.0, allow_nan=False)),
            "rank_key": "net_per_trade_bp",
            "k_min": draw(st.integers(1, 250)),
        }
    )


@given(shared_books())
def test_a_shared_book_round_trips_and_its_venue_leverage_is_the_largest_it_declares(
    value: SharedBook,
) -> None:
    assert parse_yaml(SharedBook, dump_yaml(value)) == value
    assert value.leverage.largest == RTH_LEVERAGE, "four intraday is above two overnight"


def test_the_block_names_its_ranking_and_carries_no_name_cap_unless_told() -> None:
    made = SharedBook.model_validate(BLOCK)

    assert (made.rank_key, made.k_min, made.max_name_c) == ("net_per_trade_bp", 15, None)


@pytest.mark.parametrize("missing", ["k_min", "rank_key", "capital", "leverage"])
def test_a_block_without_what_the_plan_must_state_is_refused(missing: str) -> None:
    with pytest.raises(ValidationError, match=missing):
        SharedBook.model_validate({k: v for k, v in BLOCK.items() if k != missing})


@pytest.mark.parametrize(
    ("leverage", "said"),
    [
        ({"rth": 5}, "not the book's 4x intraday"),
        ({"rth": 4, "overnight": 3}, "not the book's 2x overnight"),
        ({"rth": 4, "pre": 2}, "pre: the leverage outside regular hours is not set"),
        ({"rth": 4, "post": 4}, "post: the leverage outside regular hours is not set"),
        ({}, "rth"),
    ],
)
def test_a_leverage_other_than_the_operator_s_is_refused(
    leverage: dict[str, float], said: str
) -> None:
    with pytest.raises(ValidationError, match=said):
        SharedBook.model_validate({**BLOCK, "leverage": leverage})


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        ({"daily_stop_bp": 50}, "a book has no daily stop"),
        ({"daily_target_bp": 100}, "daily exit is not built"),
        ({"daily_exit_pnl": "marked"}, "daily exit is not built"),
        ({"rank_key": "net_t"}, "rank_key"),
        ({"k_min": 0}, "k_min"),
        ({"max_name_c": 0}, "max_name_c"),
    ],
)
def test_what_a_book_does_not_do_is_refused_rather_than_read(
    extra: dict[str, Any], said: str
) -> None:
    with pytest.raises(ValidationError, match=said):
        SharedBook.model_validate({**BLOCK, **extra})
