"""A market order deeper than a level-two book keeps its rest open, and only a cancel of its
name takes it off.

On the top-of-book venue a market order is answered where it lands, so kanso never sends its
cancel (`kanso.nautilus.strategy._never_rests`). On a level-two book one deeper than the book
fills what the book shows and stays open, partly filled, for the rest. Measured on 2026-10-10
on both paths, at 0 and 20 ms, on the level-two session the backtest fixtures build: a market
buy of 100 against an offer of 50 fills 51 at 0 ms and 52 at 20 ms, and nothing more; a
cancel of the order itself, alone or in a list, leaves it open, as the venue's own rejection
of such a cancel did before kanso stopped sending it; the cancel of its name cancels it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kanso.nautilus.backtest import execute
from kanso.nautilus.session import run_node
from tests.nautilus.backtest.conftest import instrument, tick_groups, tick_hypothesis

DEEPER = '''
import json
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

RECORD = Path(%r)
HOW = %r


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys 100 at market on the first print and cancels it on the third, then records what
    the order was then and when the run stopped."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.order = None
        self.notes = []

    def _note(self, when):
        current = self.cache.order(self.order.client_order_id)
        self.notes.append([when, current.status_string(), float(current.filled_qty)])

    def on_trade_tick(self, tick):
        self.seen += 1
        if self.seen == 1:
            self.order = self.submit_entry(tick.instrument_id, "BUY", qty=100)
        if self.seen == 3:
            self._note("cancelled")
            if HOW == "cancel_order":
                self.cancel_order(self.cache.order(self.order.client_order_id))
            elif HOW == "cancel_orders":
                self.cancel_orders([self.cache.order(self.order.client_order_id)])
            else:
                self.cancel_all_orders(tick.instrument_id)

    def on_stop(self):
        self._note("stopped")
        with RECORD.open("a") as out:
            out.write(json.dumps(self.notes) + "\\n")
'''


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize("how", ["cancel_order", "cancel_orders", "cancel_all_orders"])
def test_a_market_order_s_rest_on_a_level_two_book_is_cancelled_only_with_its_name(
    request_for, tmp_path: Path, how: str, latency_ms: float
) -> None:
    record = tmp_path / "notes.jsonl"
    source = (DEEPER % (str(record), how)).encode()
    request = request_for(source=source, hypothesis_=tick_hypothesis(latency_ms))
    groups = tuple(tick_groups())

    engine = execute(request, [instrument()], groups)
    node = run_node(request, [instrument()], groups)

    assert not engine.crashed, engine.traceback_tail
    research, staged = (json.loads(line) for line in record.read_text().splitlines())
    filled = 51.0 if latency_ms == 0 else 52.0
    stopped = "PARTIALLY_FILLED" if how != "cancel_all_orders" else "CANCELED"
    assert (
        research
        == staged
        == [
            ["cancelled", "PARTIALLY_FILLED", filled],
            ["stopped", stopped, filled],
        ]
    )
    assert engine.intents == node.intents


RESTING_EXIT = '''
import json
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

RECORD = Path(%r)


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys 50 at market on the first print and 50 on the fourth, sells the 100 at market on
    the sixth, asks for 10 more on the seventh, cancels the name on the ninth and asks for 10
    more on the tenth, noting what `held` reads and whether each entry went."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.notes = []

    def on_trade_tick(self, tick):
        self.seen += 1
        name = tick.instrument_id
        if self.seen in (1, 4):
            self.submit_entry(name, "BUY", qty=50)
        elif self.seen == 6:
            self.submit_exit(name)
        elif self.seen in (7, 10):
            held = self.held(name)
            self.notes.append([self.seen, held, self.submit_entry(name, "BUY", qty=10) is not None])
        elif self.seen == 9:
            self.cancel_all_orders(name)

    def on_stop(self):
        self.notes.append(["end", self.held(self.universe[0])])
        with RECORD.open("a") as out:
            out.write(json.dumps(self.notes) + "\\n")
'''


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_the_rest_of_a_market_exit_deeper_than_the_book_is_held_until_its_name_is_cancelled(
    request_for, tmp_path: Path, latency_ms: float
) -> None:
    """A sale of 100 against a bid of fifty-odd fills what the bid shows and keeps its rest open.
    The rest counts once it has filled, which nothing does, so the sleeve reads what the sale
    left as held, and an entry that would grow it is dropped; once the cancel of the name has
    taken the rest off, the entry goes."""
    record = tmp_path / "notes.jsonl"
    request = request_for(
        source=(RESTING_EXIT % str(record)).encode(), hypothesis_=tick_hypothesis(latency_ms)
    )
    groups = tuple(tick_groups())

    engine = execute(request, [instrument()], groups)
    node = run_node(request, [instrument()], groups)

    assert not engine.crashed, engine.traceback_tail
    research, staged = (json.loads(line) for line in record.read_text().splitlines())
    assert research == staged
    assert engine.intents == node.intents
    (_, left, sent_behind), (_, still, sent_after), (_, end) = research
    assert 0 < left < 100 and not sent_behind
    assert still == left and sent_after
    assert end == left + 10
