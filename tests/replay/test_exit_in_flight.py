"""A position is held until its exit has filled, and an entry that would grow it while an exit
at market is still on its way to close it is dropped, on both code paths.

Measured on 2026-10-30 (card 39ae96a of a QQQ sweep scout, 10 ms, the print rules): the scout
read its exit in flight as done, entered again behind it, and on 22 of the card's 1,310 trades
the venue refused the exit — a print under the bid had ended the quote — and the entry doubled
the position. The tapes here are that race, measured on 2026-11-01 through both paths: a long of
100 at 9.52, an exit at market sent at 100 ms, an entry sent behind it while it is on its way, a
venue that refuses the exit — the print rules on a print that ends the quote, `touch` on a
quote showing no bid — and a quote on which the entry would fill. Instants are milliseconds
after 15:00 UTC on 2024-03-04, a print's `ts_event` its `ts_init`, as in
`tests/replay/test_print_through.py`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kanso.nautilus import backtest, session
from tests.replay.test_print_through import OTHER, T0, _subject, fills_of, q, scripted, t

LATENCIES = (20, 30)
"""The lanes' 20 ms and the operator's 30 ms: an exit at market is on its way across a point."""

RECORDING = '''
import json
from pathlib import Path

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

RECORD = Path(%(record)r)
STEPS = %(steps)r


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """On the n-th point it handles, notes what `held` reads, sends what STEPS lists for n and
    notes whether each entry went; on stopping, appends its notes to RECORD."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.notes = []

    def _point(self, tick):
        self.seen += 1
        name = InstrumentId.from_str("DEMO.XNAS")
        steps = STEPS.get(self.seen, ())
        if steps:
            self.notes.append([self.seen, "held", self.held(name)])
        for step in steps:
            if step == "exit":
                self.submit_exit(name)
                continue
            if step in ("BUY", "SELL"):
                asked = {} if self.sized else {"qty": 100}
                sent = self.submit_entry(name, step, **asked) is not None
            elif step == "OTHR":
                sent = self.submit_entry("OTHR.XNAS", "BUY", qty=10) is not None
            elif step == "bracket_buy":
                bracket = self.order_factory.bracket(
                    name,
                    OrderSide.BUY,
                    Quantity.from_int(100),
                    sl_trigger_price=Price(9.0, 2),
                    tp_price=Price(10.5, 2),
                )
                self.submit_order_list(bracket)
                sent = self.cache.order(bracket.first.client_order_id) is not None
            else:
                side = OrderSide.BUY if step.endswith("buy") else OrderSide.SELL
                quantity = Quantity.from_int(100)
                if step.startswith("limit"):
                    order = self.order_factory.limit(name, side, quantity, Price(9.6, 2))
                else:
                    order = self.order_factory.market(name, side, quantity)
                self.submit_order(order)
                sent = self.cache.order(order.client_order_id) is not None
            self.notes.append([self.seen, step, sent])

    def on_quote_tick(self, tick):
        self._point(tick)

    def on_trade_tick(self, tick):
        self._point(tick)

    def on_stop(self):
        name = InstrumentId.from_str("DEMO.XNAS")
        self.notes.append(["end", "held", self.held(name)])
        with RECORD.open("a") as out:
            out.write(json.dumps(self.notes) + "\\n")
'''


def recording(record: Path, steps: dict[int, tuple[str, ...]]) -> bytes:
    """The sleeve that notes `held` and sends `steps` — `exit`, an entry `BUY` or `SELL`
    through `submit_entry`, of 100 or under a sizing rule its budget, `OTHR` for 10 of the other
    name, or one built by hand: `market_buy`, `limit_buy` at 9.60, `bracket_buy` a list of a
    market buy of 100 with its stop at 9.00 and its target at 10.50."""
    return (RECORDING % {"record": str(record), "steps": steps}).encode()


def refused_by_a_print(latency_ms: int) -> list[object]:
    """The print rules' race: the long fills on the open, the exit goes at 100 ms, a print
    under the bid ends the quote a quarter of the latency later, the exit lands on a print with
    no quote in force and is refused, and the entry sent behind it lands on the next quote."""
    return [
        q(9.48, 9.52, 10),
        q(9.48, 9.52, 50),
        q(9.48, 9.52, 100),
        t(9.40, 100, 100 + latency_ms // 4),
        t(9.40, 100, 101 + latency_ms),
        q(9.38, 9.42, 100 + 2 * latency_ms),
        q(9.38, 9.42, 300),
    ]


def refused_for_no_bid(latency_ms: int) -> list[object]:
    """`touch`'s race: the same, the exit landing on a quote that shows no bid."""
    return [
        q(9.48, 9.52, 10),
        q(9.48, 9.52, 50),
        q(9.48, 9.52, 100),
        q(9.48, 9.52, 100 + latency_ms // 4, bid_size=0),
        q(9.48, 9.52, 101 + latency_ms, bid_size=0),
        q(9.38, 9.42, 100 + 2 * latency_ms),
        q(9.38, 9.42, 300),
    ]


RACES = {
    "print_through": refused_by_a_print,
    "print_through_whole": refused_by_a_print,
    "touch": refused_for_no_bid,
}


def both(
    tmp_path: Path,
    points: list[object],
    steps: dict[int, tuple[str, ...]],
    rule: str,
    latency_ms: int,
) -> tuple[Any, Any, list[Any]]:
    """The recording sleeve on both paths; the node's run, the engine's, and the notes, which
    must agree."""
    record = tmp_path / "notes.jsonl"
    record.unlink(missing_ok=True)
    node, engine = scripted(points, {}, rule, latency_ms, source=recording(record, steps))
    research, staged = (json.loads(line) for line in record.read_text().splitlines())
    assert not engine.crashed and not node.crashed
    assert research == staged
    assert node.intents == engine.intents
    assert fills_of(node) == fills_of(engine)
    return node, engine, research


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", sorted(RACES))
@pytest.mark.parametrize("entry", ["BUY", "market_buy", "limit_buy", "bracket_buy"])
def test_an_entry_behind_an_exit_the_venue_refuses_is_dropped_and_the_long_is_not_doubled(
    tmp_path: Path, rule: str, latency_ms: int, entry: str
) -> None:
    """While the exit is on its way the sleeve reads its long of 100 as held, and an entry that
    would grow it — `submit_entry`'s, a market or a limit order built by hand, a bracket built
    by hand — is dropped unsent, neither refused nor counted. The venue refuses the exit, and the
    long is still 100 at the end, where an entry sent behind the exit filled on the next quote
    and held 200."""
    points = RACES[rule](latency_ms)

    _, engine, notes = both(
        tmp_path, points, {1: ("BUY",), 3: ("exit",), 4: (entry,)}, rule, latency_ms
    )

    assert notes == [
        [1, "held", 0.0],
        [1, "BUY", True],
        [3, "held", 100.0],
        [4, "held", 100.0],
        [4, entry, False],
        ["end", "held", 100.0],
    ]
    assert fills_of(engine) == [(50, 100.0, 9.52, False)]
    assert engine.commands is not None and engine.commands.refused_submits == 0


@pytest.mark.parametrize("latency_ms", [0, *LATENCIES])
@pytest.mark.parametrize("rule", sorted(RACES))
def test_the_second_leg_of_a_flip_still_goes_beside_its_exit(
    tmp_path: Path, rule: str, latency_ms: int
) -> None:
    """A sale behind the exit of a long sells where the exit does, so it is never behind it:
    `submit_exit` then `submit_entry` on the other side, in one handler, sends both, and the
    venue settles both at one price, the exit first. Measured on #160's head (6056d49) on
    2026-11-01, the same orders and fills on both paths."""
    points = [q(9.48, 9.52, 10), q(9.48, 9.52, 50), q(9.48, 9.52, 100), q(9.48, 9.52, 300)]

    node, engine, notes = both(
        tmp_path, points, {1: ("BUY",), 3: ("exit", "SELL")}, rule, latency_ms
    )

    assert notes == [
        [1, "held", 0.0],
        [1, "BUY", True],
        [3, "held", 100.0],
        [3, "SELL", True],
        ["end", "held", -100.0],
    ]
    sold = [fill for fill in fills_of(engine) if fill[0] > 50]
    assert [qty for _, qty, _, _ in sold] == [100.0, 100.0]
    assert len({(ms, px) for ms, _, px, _ in sold}) == 1


@pytest.mark.parametrize("latency_ms", [0, *LATENCIES])
def test_an_entry_behind_an_exit_sent_in_the_same_handler_is_dropped(
    tmp_path: Path, latency_ms: int
) -> None:
    """The exit and the entry land together, the exit first, and a quote showing no bid
    refuses the sale while the ask fills the buy: sent, the entry doubled the long on the quote
    that refused its exit. Dropped, the long is 100 at the end."""
    points = [
        q(9.48, 9.52, 10),
        q(9.48, 9.52, 50),
        q(9.48, 9.52, 100, bid_size=0),
        q(9.48, 9.52, 101 + latency_ms, bid_size=0),
        q(9.48, 9.52, 300),
    ]

    _, engine, notes = both(
        tmp_path, points, {1: ("BUY",), 3: ("exit", "BUY")}, "touch", latency_ms
    )

    assert notes == [
        [1, "held", 0.0],
        [1, "BUY", True],
        [3, "held", 100.0],
        [3, "BUY", False],
        ["end", "held", 100.0],
    ]
    assert [qty for _, qty, _, _ in fills_of(engine)] == [100.0]


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", ["touch", "through"])
def test_an_exit_on_its_way_across_a_session_gap_is_answered_before_the_next_is_handled(
    tmp_path: Path, rule: str, latency_ms: int
) -> None:
    """An exit at market sent on a session's last point is on its way across the gap and lands
    on the next session's first point before that point is handled: a quote showing no bid
    refuses it there for want of a market, and the long reads 100 on that point, on both
    paths. The exit asked for again there is on its way on the next point, where the long still
    reads 100 and an entry behind it is dropped, and it fills on the point after."""
    points = [
        q(9.48, 9.52, 10),
        q(9.48, 9.52, 50),
        q(9.48, 9.52, 100),
        q(9.48, 9.52, 10, day=1, bid_size=0),
        q(9.48, 9.52, 10 + latency_ms // 4, day=1),
        q(9.38, 9.42, 11 + latency_ms, day=1),
        q(9.38, 9.42, 300, day=1),
    ]
    steps: dict[int, tuple[str, ...]] = {1: ("BUY",), 3: ("exit",), 4: ("exit",), 5: ("BUY",)}

    _, engine, notes = both(tmp_path, points, steps, rule, latency_ms)

    assert notes == [
        [1, "held", 0.0],
        [1, "BUY", True],
        [3, "held", 100.0],
        [4, "held", 100.0],
        [5, "held", 100.0],
        [5, "BUY", False],
        ["end", "held", 0.0],
    ]
    day = 86_400_000
    assert fills_of(engine) == [
        (50, 100.0, 9.52, False),
        (day + 11 + latency_ms, 100.0, 9.38, False),
    ]


LEGS = b"""
from kanso.nautilus.strategy import Decision, Hedge, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    \"\"\"Asks for 100 of the demo name beside every entry in the other.\"\"\"

    construct = "overlay"
    config_cls = Config

    def evaluate(self, ctx):
        if ctx.instrument_id == "OTHR.XNAS":
            return Decision(hedges=(Hedge("DEMO.XNAS", 100.0),))
        return Decision.neutral(self.construct)
"""


@pytest.mark.parametrize("latency_ms", LATENCIES)
def test_an_overlay_s_leg_behind_an_exit_of_its_name_is_dropped(
    tmp_path: Path, latency_ms: int
) -> None:
    """An entry in the other name while the demo long's exit is on its way: the overlay asks
    for 100 of the demo name beside it, a leg that would grow the long the exit is closing, and
    it is dropped once built. The entry itself goes. A quote showing no bid refuses the exit,
    and the long is 100 at the end, where the leg sent filled on the quote after."""
    points = [
        q(19.98, 20.02, 5, on=OTHER),
        q(9.48, 9.52, 10),
        q(9.48, 9.52, 50),
        q(9.48, 9.52, 100),
        q(9.48, 9.52, 100 + latency_ms // 4, bid_size=0),
        q(9.48, 9.52, 101 + latency_ms, bid_size=0),
        q(9.38, 9.42, 100 + 2 * latency_ms),
        q(9.38, 9.42, 300),
    ]
    record = tmp_path / "notes.jsonl"
    source = recording(record, {2: ("BUY",), 4: ("exit",), 5: ("OTHR",)})
    subject, instruments, groups = _subject(
        points, {}, "touch", latency_ms, ("DEMO.XNAS", "OTHR.XNAS"), source=source
    )
    subject = replace(subject, modifiers=(("overlay", LEGS, {}),))

    engine = backtest.execute(subject, instruments, groups)
    node = session.run_node(subject, instruments, groups).result

    research, staged = (json.loads(line) for line in record.read_text().splitlines())
    assert research == staged
    assert node.intents == engine.intents
    assert fills_of(node) == fills_of(engine)
    assert [(name, side, qty) for _, name, side, qty, _, _ in engine.intents] == [
        ("DEMO.XNAS", "BUY", 100.0),
        ("DEMO.XNAS", "SELL", 100.0),
        ("OTHR.XNAS", "BUY", 10.0),
    ]
    assert research[-1] == ["end", "held", 100.0]


CLIPPER = """
from kanso.nautilus.strategy import Clip, Decision, KansoModifier, KansoModifierConfig

T0 = %(t0)d
MS = 1_000_000


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    \"\"\"Clips the demo name long from 50 ms, and between 200 ms and the latency after asks to
    take the clip off and put it back on in one answer.\"\"\"

    construct = "overlay"
    config_cls = Config

    def on_data(self, ctx):
        at = (ctx.ts_event - T0) // MS
        held = ctx.clips.get("DEMO.XNAS", 0.0)
        if 50 <= at < 100 and not held:
            return Decision(clips=(Clip("DEMO.XNAS", "BUY"),))
        if 200 <= at < %(until)d and held > 0:
            return Decision(clips=(Clip("DEMO.XNAS", "FLAT"), Clip("DEMO.XNAS", %(again)r)))
        return Decision.neutral(self.construct)
"""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("again", ["BUY", "SELL"])
def test_a_clip_put_back_on_behind_its_own_exit_is_dropped(
    tmp_path: Path, latency_ms: int, again: str
) -> None:
    """A sized overlay holding a long clip answers its exit and a long clip again in one answer:
    the second would grow the clip the first is on its way to close, and is dropped and forgotten
    as a clip; a short clip in its place sells where the exit does, a switch, and goes. A quote
    showing no bid refuses every sale, so the long clip is all that is held at the end, where a
    long clip sent beside its exit filled on the ask and held twice the budget."""
    points = [
        q(9.48, 9.52, 10),
        q(9.48, 9.52, 50),
        q(9.48, 9.52, 100),
        q(9.48, 9.52, 200),
        q(9.48, 9.52, 201 + latency_ms, bid_size=0),
        q(9.38, 9.42, 300),
        q(9.38, 9.42, 400),
    ]
    record = tmp_path / "notes.jsonl"
    subject, instruments, groups = _subject(
        points, {}, "touch", latency_ms, source=recording(record, {})
    )
    clipper = CLIPPER % {"t0": T0, "until": 201 + latency_ms, "again": again}
    subject = replace(
        subject,
        sleeve_budget=10_000.0,
        modifiers=(("overlay", clipper.encode(), {"sizing_budget": 5_000.0}),),
    )

    engine = backtest.execute(subject, instruments, groups)
    node = session.run_node(subject, instruments, groups).result

    assert not engine.crashed and not node.crashed
    assert node.intents == engine.intents
    assert fills_of(node) == fills_of(engine)
    sides = [side for _, _, side, _, _, _ in engine.intents]
    assert sides == (["BUY", "SELL"] if again == "BUY" else ["BUY", "SELL", "SELL"])
    (bought,) = fills_of(engine)
    assert bought[0] == 100
