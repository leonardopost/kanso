"""An exit never goes past flat, counting the exits still working.

A sleeve that follows the ask with its exit cancels the resting one and sends another on
every quote that moves it. Under a stated latency the cancel is in flight as long as the
new order is, so the old exit can fill before its cancel lands; if the new one were sized
to the whole position it would fill as well, and a long-only sleeve would end short with
nothing in it to buy the shares back. Measured on origin/main before the repair: the run
below sold more than it bought and held the short across the night.
"""

from __future__ import annotations

from datetime import date

import pytest

from kanso.criteria.gates import max_hold
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.nautilus.backtest import execute
from kanso.schemas import Hypothesis
from tests.criteria.builders import context
from tests.nautilus.backtest.conftest import RESEARCH, SYMBOL, VENUE, hypothesis, instrument

SESSIONS = (date(2024, 1, 2), date(2024, 1, 3))
"""Two sessions, so a position stranded by the race is held across the night."""

CHASING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys once at market, then rests its exit at the ask and follows the ask with it:
    on every quote that moves it, cancel the resting exit and send another."""

    config_cls = Config

    def on_start(self):
        self.bought = False
        self.asked = None

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        if not self.bought:
            self.bought = self.submit_entry(instrument_id, "BUY", qty=100) is not None
            return
        held = self.held(instrument_id)
        if held <= 0:
            return
        ask = float(tick.ask_price)
        if ask == self.asked:
            return
        self.cancel_all_orders(instrument_id)
        placed = self.submit_exit(instrument_id, qty=held, price=ask)
        self.asked = ask if placed is not None else None
'''


def points(sessions: tuple[date, date] = SESSIONS) -> list[tuple[object, ...]]:
    """Quotes and prints a second apart over ten minutes of each session, from the generator."""
    loader = SyntheticLoader()
    spec = {
        "seed": 7,
        "instruments": [SYMBOL],
        "venue": VENUE,
        "resolution": "1s",
        "types": ["quote", "trade"],
        "start": sessions[0],
        "end": sessions[1],
        "start_price": 10.0,
        "sigma_bps": 10.0,
        "session_start": "09:30",
        "session_end": "09:40",
    }
    return [tuple(loader.load(ref, sessions)) for ref in loader.discover(spec)]


def chasing_costs(latency_ms: float) -> dict[str, object]:
    """A venue that fills a resting limit the market reaches, after `latency_ms`."""
    return {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "spread": "fixed_bps",
        "fixed_bps": 1.0,
        "limit_fill": "touch",
        "latency_ms": latency_ms,
    }


def never_short(fills: list[object]) -> tuple[float, float, float]:
    """What the run bought, what it sold, and the lowest position it reached at any fill."""
    position, lowest, bought, sold = 0.0, 0.0, 0.0, 0.0
    for fill in fills:
        signed = fill.qty if fill.side == "BUY" else -fill.qty  # type: ignore[attr-defined]
        position += signed
        lowest = min(lowest, position)
        bought += max(signed, 0.0)
        sold += max(-signed, 0.0)
    return bought, sold, lowest


def _run(request_for, latency_ms: float):
    document = hypothesis().model_dump(mode="json")
    document.update(resolution="tick", data_requirements=["quote", "trade"])
    document["costs"] = chasing_costs(latency_ms)
    hyp = Hypothesis.model_validate(document)
    result = execute(
        request_for(RESEARCH, source=CHASING, hypothesis_=hyp), [instrument()], points()
    )
    assert not result.crashed, result.traceback_tail
    return result.run


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_that_follows_the_ask_never_sells_what_it_did_not_buy(
    request_for, latency_ms: float
) -> None:
    """Bought once, sold no more than that, never short at any fill, and the hold is the
    strategy's own — seconds, not the night a stranded short would be carried across."""
    run = _run(request_for, latency_ms)
    bought, sold, lowest = never_short(run.fills)
    assert bought == 100.0
    assert sold <= bought, f"sold {sold:g} of the {bought:g} bought"
    assert lowest >= 0.0, f"the position reached {lowest:g}"
    held = max_hold.evaluate(context(run, params={"days": 1})).evidence
    assert held["longest_days"] < 0.01, held
