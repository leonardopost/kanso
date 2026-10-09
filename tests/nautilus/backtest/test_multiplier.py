"""A card on a multiplied instrument records, charges and re-prices the contract notional.

The instrument is a manual `FuturesContract` entry with a multiplier of 50, built through the
workspace's own instrument builder rather than the engine's test kit, so what the runner is
handed is what `kanso data instruments resolve` would hand it. The bars are the same
synthetic saw-tooth every other card here trades, filed under the contract's id.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kanso.criteria.gates import SCENARIO_KEYS, repriced
from kanso.criteria.run import BPS
from kanso.data.instruments import build, conventions_for
from kanso.nautilus.backtest import execute
from kanso.nautilus.costs import fill_cost
from kanso.schemas import InstrumentEntry

from .conftest import RESEARCH, bars, hypothesis
from .test_balance import FIXED, PROBE, TAKER, assert_the_same

SYMBOL = "FUT"
CONTRACT = f"{SYMBOL}.XNAS"
MULTIPLIER = 50.0
EXPIRY_NS = 1_900_000_000 * 1_000_000_000
"""2030: a contract still live over every window the suite trades."""


def future() -> Any:
    """A manual entry for a 50-times future, built as the workspace builds one."""
    entry = InstrumentEntry.model_validate(
        {
            "nautilus_id": CONTRACT,
            "asset_class": "INDEX",
            "manual": True,
            "corporate_actions": "none",
            "override": {
                "instrument_class": "future",
                "currency": "USD",
                "price_increment": "0.25",
                "multiplier": str(int(MULTIPLIER)),
                "lot_size": "1",
                "underlying": SYMBOL,
                "activation_ns": 0,
                "expiration_ns": EXPIRY_NS,
            },
        }
    )
    return build(entry, conventions_for(entry, date(2024, 1, 1)))


def test_the_entry_builds_the_contract_the_test_claims() -> None:
    made = future()
    assert type(made).__name__ == "FuturesContract"
    assert (made.id.value, float(made.multiplier)) == (CONTRACT, MULTIPLIER)


def test_a_card_on_a_multiplied_instrument_records_the_contract_notional(request_for) -> None:
    """Every fill's notional is `qty x px x 50`, its cost is the costs module's cost of that
    notional, and the card's own scenario re-prices each fill to the cost it recorded."""
    hyp = hypothesis(universe=(CONTRACT,))
    request = request_for(RESEARCH, hypothesis_=hyp)

    card = execute(request, [future()], [tuple(bars(RESEARCH, SYMBOL))]).run

    assert card.fills and card.trades
    costs = dict(card.venue_model["costs"])  # type: ignore[call-overload]
    half = float(costs["fixed_bps"]) / 2.0 / BPS
    for fill in card.fills:
        assert fill.multiplier == MULTIPLIER
        assert fill.notional == pytest.approx(fill.qty * fill.px * MULTIPLIER)
        assert fill.cost == pytest.approx(
            fill_cost(
                fill.qty * fill.px * MULTIPLIER,
                fill.qty,
                float(costs["commission_bps"]),
                float(costs["slippage_bps"]),
                half,
                None,
                0.0,
                maker=fill.maker,
                sell=fill.side == "SELL",
            )
        )
    assert {trade.multiplier for trade in card.trades} == {MULTIPLIER}
    for trade in card.trades:
        assert trade.notional == pytest.approx(abs(trade.qty) * trade.avg_open * MULTIPLIER)

    scenario = {key: costs[key] for key in SCENARIO_KEYS if costs.get(key) is not None}
    under = repriced(card, scenario)

    for recorded, again in zip(card.fills, under.fills, strict=True):
        assert again.cost == pytest.approx(recorded.cost, abs=1e-9)
    assert under.equity == pytest.approx(card.equity, abs=1e-9)


def test_a_taker_pays_its_ticks_per_contract_and_the_balance_is_still_the_equity(
    tmp_path: Path, request_for
) -> None:
    """One tick of 0.25 on a 50-times contract is $12.50 a contract: every market order the
    probe sends pays it on top of the rates, the card's own scenario re-prices each fill to
    the cost it recorded, and the balance the sleeve read is the equity struck."""
    record = tmp_path / "balance.txt"
    hyp = hypothesis(universe=(CONTRACT,), costs={**FIXED, "slippage_ticks": 1.0})
    request = request_for(
        RESEARCH, source=PROBE, hypothesis_=hyp, overrides={"record": str(record)}
    )

    card = execute(request, [future()], [tuple(bars(RESEARCH, SYMBOL))]).run

    assert card.fills and not any(fill.maker for fill in card.fills)
    for fill in card.fills:
        assert (fill.tick, fill.multiplier, fill.limit) == (0.25, MULTIPLIER, None)
        assert fill.cost == pytest.approx(
            fill.qty * fill.px * MULTIPLIER * TAKER + fill.qty * 0.25 * MULTIPLIER,
            rel=1e-12,
        )
    costs = dict(card.venue_model["costs"])  # type: ignore[call-overload]
    scenario = {key: costs[key] for key in SCENARIO_KEYS if costs.get(key) is not None}
    under = repriced(card, scenario)
    for recorded, again in zip(card.fills, under.fills, strict=True):
        assert again.cost == pytest.approx(recorded.cost, rel=1e-12)
    assert_the_same(card, record, at_least=10)
