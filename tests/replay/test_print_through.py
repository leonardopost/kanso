"""`limit_fill: print_through`: only a later print strictly through a resting order fills it, a
taker fills on the quote in force, and on every case here the two code paths agree on every fill
as well as every intent.

The scenario rows at 0 and 20 ms were measured on 2026-10-09 through both of kanso's paths with
the rule's prototype loaded where `kanso.nautilus.venue.fill_model` and
`kanso.nautilus.actions.modules` build the venue, stacked on the venue that applies every quote
and print; the module that ships reproduces all of them. The rows at 30 ms, the operator's
latency, and the cases below the scenarios were measured on 2026-10-10 through both paths with
the module that ships. Instants are milliseconds after 15:00 UTC on 2024-03-04, a point's
`ts_event` is its `ts_init`, and a print carries no aggressor unless a test names one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import backtest, session
from kanso.nautilus.backtest import HOLD
from tests.replay.conftest import INSTRUMENT, hypothesis, instrument, request_for

MS = 1_000_000
DAY = 86_400_000 * MS
T0 = midnight_ns(date(2024, 3, 4)) + 15 * 3_600 * 1_000_000_000
DEMO = InstrumentId.from_str(INSTRUMENT)
OTHER = InstrumentId.from_str("OTHR.XNAS")
RULES = ("print_through", "print_through_whole")

SCRIPTED = b"""
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    script: dict = {}
    on_fill: dict = {}


class Strategy(KansoStrategy):
    \"\"\"On the n-th point it handles, sends what the script lists for n, and on its n-th fill
    what `on_fill` lists for n.\"\"\"

    config_cls = Config

    def on_start(self) -> None:
        self.seen = 0
        self.filled = 0

    def _send(self, steps) -> None:
        for kind, name, side, qty, price in steps:
            instrument_id = InstrumentId.from_str(name)
            if kind == "cancel_all":
                self.cancel_all_orders(instrument_id)
            elif kind == "market":
                self.submit_entry(instrument_id, side, qty=qty)
            elif kind == "exit":
                self.submit_exit(instrument_id)
            elif kind == "modify":
                for order in self.cache.orders_open(instrument_id=instrument_id):
                    if order.side_string() == side and order.has_price:
                        self.modify_order(order, price=Price(price, 2))
            elif kind in ("ioc", "fok"):
                self.submit_order(
                    self.order_factory.limit(
                        instrument_id,
                        OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                        Quantity.from_int(qty),
                        Price(price, 2),
                        time_in_force=TimeInForce.IOC if kind == "ioc" else TimeInForce.FOK,
                    )
                )
            else:
                self.submit_entry(instrument_id, side, qty=qty, price=price)

    def _point(self, tick) -> None:
        self.seen += 1
        self._send(self.kanso_config.script.get(str(self.seen), []))

    def on_quote_tick(self, tick) -> None:
        self._point(tick)

    def on_trade_tick(self, tick) -> None:
        self._point(tick)

    def on_order_filled(self, event) -> None:
        self.filled += 1
        self._send(self.kanso_config.on_fill.get(str(self.filled), []))
"""

_printed = [0]


def q(
    bid: float, ask: float, ms: int, ask_size: int = 1_000, on: InstrumentId = DEMO, day: int = 0
) -> QuoteTick:
    """A quote showing 1,000 on the bid and `ask_size` on the ask, `day` sessions after the
    first."""
    ns = T0 + day * DAY + ms * MS
    return QuoteTick(
        on,
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_int(1_000),
        Quantity.from_int(ask_size),
        ns,
        ns,
    )


def t(
    px: float,
    size: int,
    ms: int,
    side: AggressorSide = AggressorSide.NO_AGGRESSOR,
    day: int = 0,
) -> TradeTick:
    """A print of the demo name, `day` sessions after the first."""
    _printed[0] += 1
    ns = T0 + day * DAY + ms * MS
    return TradeTick(
        DEMO, Price(px, 2), Quantity.from_int(size), side, TradeId(f"P{_printed[0]}"), ns, ns
    )


def scripted(
    points: list[object],
    script: dict[int, list[tuple[Any, ...]]],
    rule: str,
    latency_ms: float = 0.0,
    names: tuple[str, ...] = (INSTRUMENT,),
    on_fill: dict[int, list[tuple[Any, ...]]] | None = None,
    costs: dict[str, Any] | None = None,
    infos: dict[str, dict[str, Any]] | None = None,
    source: bytes = SCRIPTED,
) -> tuple[backtest.RunResult, backtest.RunResult]:
    """The scripted sleeve on both paths under `limit_fill: rule` at this latency: a step is
    `(kind, side, qty, price)` on the demo name, or `(kind, name, side, qty, price)`; `on_fill`
    lists what the sleeve sends on its n-th fill, `costs` what the venue model's costs state
    besides, and `infos` an instrument's `info`."""
    hyp = hypothesis(
        resolution="tick", horizon="1d", data_requirements=["quote", "trade"], universe=list(names)
    )

    def steps(sent: dict[int, list[tuple[Any, ...]]]) -> dict[str, list[tuple[Any, ...]]]:
        return {
            str(n): [step if len(step) == 5 else (step[0], INSTRUMENT, *step[1:]) for step in each]
            for n, each in sent.items()
        }

    request = request_for(hyp=hyp, source=source)
    model = dict(request.venue_model)
    model["costs"] = {
        **dict(model["costs"]),  # type: ignore[arg-type]
        **(costs or {}),
        "limit_fill": rule,
        "latency_ms": latency_ms,
    }
    overrides = {"script": steps(script), "on_fill": steps(on_fill or {})}
    subject = replace(request, venue_model=model, overrides=overrides if source is SCRIPTED else {})
    groups = []
    for name in names:
        for kind in (QuoteTick, TradeTick):
            group = tuple(p for p in points if isinstance(p, kind) and str(p.instrument_id) == name)
            if group:
                groups.append(group)
    instruments = [
        instrument(name.split(".")[0], **({"info": infos[name]} if infos and name in infos else {}))
        for name in names
    ]
    engine = backtest.execute(subject, instruments, groups)
    node = session.run_node(subject, instruments, groups).result
    return node, engine


def fills_of(result: backtest.RunResult) -> list[tuple[int, float, float, bool]]:
    """Each fill as its instant in milliseconds, its quantity, its price and whether it rested."""
    return [(round((f.ts_ns - T0) / MS), f.qty, f.px, f.maker) for f in result.run.fills]


OPEN = [q(9.48, 9.52, 10), q(9.48, 9.52, 50)]
REST = {1: [("limit", "BUY", 320, 9.5)]}
M = True
T = False


def measured(rows: dict[tuple[str, int], list[Any]]) -> dict[tuple[str, int], list[Any]]:
    """Rows stated once for a rule and latency apply alike wherever they are stated so."""
    return rows


LATENCIES = (0, 20, 30)
"""None, the lanes' 20 ms and the operator's 30 ms."""


def every(fills: list[Any]) -> dict[tuple[str, int], list[Any]]:
    return {(rule, latency): fills for rule in RULES for latency in LATENCIES}


def each_latency(rule: str, fills: list[Any]) -> dict[tuple[str, int], list[Any]]:
    return {(rule, latency): fills for latency in LATENCIES}


SCENARIOS: dict[
    str, tuple[list[object], dict[int, list[Any]], dict[tuple[str, int], list[Any]]]
] = {
    "a quote at the price, twice": (
        [*OPEN, q(9.45, 9.5, 100, ask_size=100), q(9.45, 9.5, 110, ask_size=100)],
        REST,
        every([]),
    ),
    "a quote through the price": ([*OPEN, q(9.45, 9.49, 100, ask_size=100)], REST, every([])),
    "a print at the price": ([*OPEN, t(9.5, 100, 100)], REST, every([])),
    "a print through, smaller than the order": (
        [*OPEN, t(9.49, 100, 100)],
        REST,
        each_latency("print_through", [(100, 100.0, 9.5, M)])
        | each_latency("print_through_whole", [(100, 320.0, 9.5, M)]),
    ),
    "a print through, larger than the order": (
        [*OPEN, t(9.49, 500, 100)],
        REST,
        every([(100, 320.0, 9.5, M)]),
    ),
    "two prints through": (
        [*OPEN, t(9.49, 100, 100), t(9.48, 100, 110)],
        REST,
        each_latency("print_through", [(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)])
        | each_latency("print_through_whole", [(100, 320.0, 9.5, M)]),
    ),
    "a market order sent on a print": (
        [*OPEN, t(9.5, 100, 100), t(9.5, 100, 125), q(9.48, 9.52, 130)],
        {3: [("market", "BUY", 300, 0)]},
        {(rule, 0): [(100, 300.0, 9.52, T)] for rule in RULES}
        | {(rule, 20): [(125, 300.0, 9.52, T)] for rule in RULES}
        | {(rule, 30): [(130, 300.0, 9.52, T)] for rule in RULES},
    ),
    "a limit marketable on the displayed size": (
        [*OPEN, q(9.48, 9.52, 100, ask_size=100), q(9.48, 9.53, 130), t(9.51, 50, 140)],
        {3: [("limit", "BUY", 320, 9.52)]},
        {
            ("print_through", 0): [(100, 100.0, 9.52, T), (140, 50.0, 9.52, M)],
            ("print_through", 20): [(140, 50.0, 9.52, M)],
            ("print_through_whole", 0): [(100, 100.0, 9.52, T), (140, 220.0, 9.52, M)],
            ("print_through_whole", 20): [(140, 320.0, 9.52, M)],
            ("print_through", 30): [(140, 50.0, 9.52, M)],
            ("print_through_whole", 30): [(140, 320.0, 9.52, M)],
        },
    ),
    "prints before and after the order lands": (
        [*OPEN, q(9.48, 9.52, 100), t(9.49, 100, 110), t(9.49, 100, 125), t(9.49, 100, 140)],
        {3: [("limit", "BUY", 320, 9.5)]},
        {
            ("print_through", 0): [
                (110, 100.0, 9.5, M),
                (125, 100.0, 9.5, M),
                (140, 100.0, 9.5, M),
            ],
            ("print_through", 20): [(125, 100.0, 9.5, M), (140, 100.0, 9.5, M)],
            ("print_through", 30): [(140, 100.0, 9.5, M)],
            ("print_through_whole", 0): [(110, 320.0, 9.5, M)],
            ("print_through_whole", 20): [(125, 320.0, 9.5, M)],
            ("print_through_whole", 30): [(140, 320.0, 9.5, M)],
        },
    ),
    "a market order after a quote through a resting buy": (
        [*OPEN, q(9.45, 9.49, 100, ask_size=100), q(9.46, 9.5, 130)],
        {1: [("limit", "BUY", 320, 9.5)], 3: [("market", "BUY", 50, 0)]},
        {(rule, 0): [(100, 50.0, 9.49, T)] for rule in RULES}
        | {(rule, latency): [(130, 50.0, 9.5, T)] for rule in RULES for latency in (20, 30)},
    ),
    "one print through two resting buys": (
        [*OPEN, t(9.49, 300, 100)],
        {1: [("limit", "BUY", 200, 9.51), ("limit", "BUY", 200, 9.5)]},
        each_latency("print_through", [(100, 200.0, 9.51, M), (100, 100.0, 9.5, M)])
        | each_latency("print_through_whole", [(100, 200.0, 9.51, M), (100, 200.0, 9.5, M)]),
    ),
    "a resting sell": (
        [*OPEN, t(9.53, 100, 100), q(9.53, 9.56, 110), t(9.52, 100, 120)],
        {1: [("limit", "SELL", 320, 9.52)]},
        each_latency("print_through", [(100, 100.0, 9.52, M)])
        | each_latency("print_through_whole", [(100, 320.0, 9.52, M)]),
    ),
    "a cancel that lands before the print": (
        [*OPEN, q(9.48, 9.52, 100), t(9.49, 100, 125), q(9.48, 9.52, 150)],
        {1: [("limit", "BUY", 320, 9.5)], 3: [("cancel_all", "BUY", 0, 0)]},
        {(rule, latency): [] for rule in RULES for latency in (0, 20)}
        | {("print_through", 30): [(125, 100.0, 9.5, M)]}
        | {("print_through_whole", 30): [(125, 320.0, 9.5, M)]},
    ),
    "prints at the price after the order lands": (
        [q(9.99, 10.01, 10), q(9.95, 10.0, 20), *[t(9.9, 100, 50 + 10 * k) for k in range(4)]],
        {1: [("limit", "BUY", 320, 9.9)]},
        every([]),
    ),
    "prints through the price after the order lands": (
        [q(9.99, 10.01, 10), q(9.95, 10.0, 20), *[t(9.89, 100, 50 + 10 * k) for k in range(4)]],
        {1: [("limit", "BUY", 320, 9.9)]},
        each_latency(
            "print_through",
            [(50, 100.0, 9.9, M), (60, 100.0, 9.9, M), (70, 100.0, 9.9, M), (80, 20.0, 9.9, M)],
        )
        | each_latency("print_through_whole", [(50, 320.0, 9.9, M)]),
    ),
}


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_both_paths_fill_by_the_print_rule(name: str, rule: str, latency_ms: int) -> None:
    """Only a print through a resting order fills it — by its size under `print_through`,
    whole under `print_through_whole` — a taker fills on the quote, and the two paths agree on
    every fill and every intent at a tolerance of zero. At 30 ms the cancel sent at 100 ms is
    due at 130, after the print at 125 has filled the order."""
    points, script, expected = SCENARIOS[name]

    node, engine = scripted(points, script, rule, latency_ms)

    assert node.intents == engine.intents
    assert fills_of(engine) == expected[(rule, latency_ms)]
    assert fills_of(node) == fills_of(engine)


ENGINE_RULES: dict[str, dict[tuple[str, int], tuple[list[Any], list[Any]]]] = {
    "a quote at the price, twice": {
        ("touch", 0): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2,
        ("through", 0): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2,
        ("through", 20): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2,
        ("through", 30): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2,
    },
    "a quote through the price": {
        ("touch", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
    },
    "a print at the price": {
        ("touch", 0): ([(100, 100.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.5, M)],) * 2,
        ("through", 0): ([],) * 2,
        ("through", 20): ([],) * 2,
        ("through", 30): ([],) * 2,
    },
    "a print through, smaller than the order": {
        ("touch", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
    },
    "a print through, larger than the order": {
        ("touch", 0): ([(100, 320.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 320.0, 9.5, M)],) * 2,
        ("touch", 30): ([(100, 320.0, 9.5, M)],) * 2,
        ("through", 0): ([(100, 320.0, 9.5, M)],) * 2,
        ("through", 20): ([(100, 320.0, 9.5, M)],) * 2,
        ("through", 30): ([(100, 320.0, 9.5, M)],) * 2,
    },
    "two prints through": {
        ("touch", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
        ("through", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2,
    },
    "a market order sent on a print": {
        ("touch", 0): ([(100, 100.0, 9.5, T), (100, 200.0, 9.51, T)],) * 2,
        ("touch", 20): ([(125, 100.0, 9.5, T), (125, 200.0, 9.51, T)],) * 2,
        ("touch", 30): ([(130, 300.0, 9.52, T)],) * 2,
        ("through", 0): ([(100, 100.0, 9.5, T), (100, 200.0, 9.51, T)],) * 2,
        ("through", 20): ([(125, 100.0, 9.5, T), (125, 200.0, 9.51, T)],) * 2,
        ("through", 30): ([(130, 300.0, 9.52, T)],) * 2,
    },
    "a limit marketable on the displayed size": {
        ("touch", 0): (
            [
                (100, 100.0, 9.52, T),
                (100, 100.0, 9.52, M),
                (140, 50.0, 9.52, M),
                (140, 70.0, 9.52, M),
            ],
            [(100, 100.0, 9.52, T), (140, 50.0, 9.52, M), (140, 170.0, 9.52, M)],
        ),
        ("touch", 20): ([(140, 50.0, 9.52, M), (140, 270.0, 9.52, M)],) * 2,
        ("touch", 30): ([(140, 50.0, 9.52, M), (140, 270.0, 9.52, M)],) * 2,
        ("through", 0): (
            [
                (100, 100.0, 9.52, T),
                (100, 100.0, 9.52, M),
                (140, 50.0, 9.52, M),
                (140, 70.0, 9.52, M),
            ],
            [(100, 100.0, 9.52, T), (140, 50.0, 9.52, M), (140, 170.0, 9.52, M)],
        ),
        ("through", 20): ([(140, 50.0, 9.52, M), (140, 270.0, 9.52, M)],) * 2,
        ("through", 30): ([(140, 50.0, 9.52, M), (140, 270.0, 9.52, M)],) * 2,
    },
    "prints before and after the order lands": {
        ("touch", 0): ([(110, 100.0, 9.5, M), (110, 220.0, 9.5, M)],) * 2,
        ("touch", 20): ([(125, 100.0, 9.49, T), (125, 220.0, 9.5, T)],) * 2,
        ("touch", 30): ([(140, 100.0, 9.49, T), (140, 220.0, 9.5, T)],) * 2,
        ("through", 0): ([(110, 100.0, 9.5, M), (110, 220.0, 9.5, M)],) * 2,
        ("through", 20): ([(125, 100.0, 9.49, T), (125, 220.0, 9.5, T)],) * 2,
        ("through", 30): ([(140, 100.0, 9.49, T), (140, 220.0, 9.5, T)],) * 2,
    },
    "a market order after a quote through a resting buy": {
        ("touch", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M), (100, 50.0, 9.49, T)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M), (130, 50.0, 9.5, T)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M), (130, 50.0, 9.5, T)],) * 2,
        ("through", 0): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M), (100, 50.0, 9.49, T)],) * 2,
        ("through", 20): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M), (130, 50.0, 9.5, T)],) * 2,
        ("through", 30): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M), (130, 50.0, 9.5, T)],) * 2,
    },
    "one print through two resting buys": {
        ("touch", 0): ([(100, 200.0, 9.51, M)],) * 2,
        ("touch", 20): ([(100, 200.0, 9.51, M)],) * 2,
        ("touch", 30): ([(100, 200.0, 9.51, M)],) * 2,
        ("through", 0): ([(100, 200.0, 9.51, M)],) * 2,
        ("through", 20): ([(100, 200.0, 9.51, M)],) * 2,
        ("through", 30): ([(100, 200.0, 9.51, M)],) * 2,
    },
    "a resting sell": {
        ("touch", 0): ([(100, 100.0, 9.52, M), (100, 220.0, 9.52, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.52, M), (100, 220.0, 9.52, M)],) * 2,
        ("touch", 30): ([(100, 100.0, 9.52, M), (100, 220.0, 9.52, M)],) * 2,
        ("through", 0): ([(100, 100.0, 9.52, M), (100, 220.0, 9.52, M)],) * 2,
        ("through", 20): ([(100, 100.0, 9.52, M), (100, 220.0, 9.52, M)],) * 2,
        ("through", 30): ([(100, 100.0, 9.52, M), (100, 220.0, 9.52, M)],) * 2,
    },
    "a cancel that lands before the print": {
        ("touch", 0): ([],) * 2,
        ("touch", 20): ([(125, 100.0, 9.5, M), (125, 220.0, 9.5, M)],) * 2,
        ("touch", 30): ([(125, 100.0, 9.5, M), (125, 220.0, 9.5, M)],) * 2,
        ("through", 0): ([],) * 2,
        ("through", 20): ([(125, 100.0, 9.5, M), (125, 220.0, 9.5, M)],) * 2,
        ("through", 30): ([(125, 100.0, 9.5, M), (125, 220.0, 9.5, M)],) * 2,
    },
    "prints at the price after the order lands": {
        ("touch", 0): (
            [(50, 100.0, 9.9, M), (60, 100.0, 9.9, M), (70, 100.0, 9.9, M), (80, 20.0, 9.9, M)],
        )
        * 2,
        ("touch", 20): (
            [(50, 100.0, 9.9, T), (50, 100.0, 9.9, M), (60, 100.0, 9.9, M), (70, 20.0, 9.9, M)],
            [(50, 100.0, 9.9, T), (60, 100.0, 9.9, M), (70, 100.0, 9.9, M), (80, 20.0, 9.9, M)],
        ),
        ("touch", 30): (
            [(50, 100.0, 9.9, T), (50, 100.0, 9.9, M), (60, 100.0, 9.9, M), (70, 20.0, 9.9, M)],
            [(50, 100.0, 9.9, T), (60, 100.0, 9.9, M), (70, 100.0, 9.9, M), (80, 20.0, 9.9, M)],
        ),
        ("through", 0): ([],) * 2,
        ("through", 20): ([(50, 100.0, 9.9, T)],) * 2,
        ("through", 30): ([(50, 100.0, 9.9, T)],) * 2,
    },
    "prints through the price after the order lands": {
        ("touch", 0): ([(50, 100.0, 9.9, M), (50, 220.0, 9.9, M)],) * 2,
        ("touch", 20): ([(50, 100.0, 9.89, T), (50, 220.0, 9.9, T)],) * 2,
        ("touch", 30): ([(50, 100.0, 9.89, T), (50, 220.0, 9.9, T)],) * 2,
        ("through", 0): ([(50, 100.0, 9.9, M), (50, 220.0, 9.9, M)],) * 2,
        ("through", 20): ([(50, 100.0, 9.89, T), (50, 220.0, 9.9, T)],) * 2,
        ("through", 30): ([(50, 100.0, 9.89, T), (50, 220.0, 9.9, T)],) * 2,
    },
}
"""What the engine's own rules fill in every scenario above, as (research, node), measured on
the tree before the print rule's module existed: the module is loaded under every rule and must
move none of it — the two paths' row-154 differences included."""


@pytest.mark.parametrize(
    ("name", "rule", "latency_ms"),
    [(name, rule, latency) for name, rows in ENGINE_RULES.items() for rule, latency in rows],
)
def test_the_tape_module_moves_nothing_under_the_engine_s_rules(
    name: str, rule: str, latency_ms: int
) -> None:
    points, script, _ = SCENARIOS[name]

    node, engine = scripted(points, script, rule, latency_ms)

    assert (fills_of(engine), fills_of(node)) == ENGINE_RULES[name][(rule, latency_ms)]


@pytest.mark.parametrize(
    "side", [AggressorSide.BUYER, AggressorSide.SELLER, AggressorSide.NO_AGGRESSOR]
)
@pytest.mark.parametrize("rule", RULES)
def test_a_print_reaches_only_the_side_it_hit_on_either_path(
    side: AggressorSide, rule: str
) -> None:
    """A buyer's print under a resting buy never fills it, even once an order sent for another
    name lands and the research path matches every resting order again: the rule keeps the
    engine's own side rule on every match, so the two paths agree. A seller's print, and one with
    no aggressor, fill it."""
    points = [
        q(9.48, 9.52, 10),
        q(19.98, 20.02, 15, on=OTHER),
        t(9.49, 100, 100, side),
        q(19.98, 20.02, 110, on=OTHER),
        q(19.98, 20.02, 120, on=OTHER),
    ]
    script = {1: [("limit", "BUY", 320, 9.5)], 4: [("limit", str(OTHER), "BUY", 10, 19.9)]}

    node, engine = scripted(points, script, rule, names=(INSTRUMENT, str(OTHER)))

    filled = [] if side == AggressorSide.BUYER else [(100, 100.0, 9.5, M)]
    if rule == "print_through_whole" and side != AggressorSide.BUYER:
        filled = [(100, 320.0, 9.5, M)]
    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == filled


@pytest.mark.parametrize("rule", RULES)
def test_a_market_order_with_no_quote_to_fill_on_is_refused_on_both_paths(rule: str) -> None:
    """On prints alone there is no touch to take: the market order is refused for want of a
    market and nothing fills, where the engine's own rule fills it at the print."""
    points = [t(9.5, 100, 10), t(9.51, 100, 20)]

    node, engine = scripted(points, {1: [("market", "BUY", 50, 0)]}, rule)
    _, touched = scripted(points, {1: [("market", "BUY", 50, 0)]}, "touch")

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == []
    assert fills_of(touched) == [(10, 50.0, 9.5, T)]


@pytest.mark.parametrize("on", [2, 3], ids=["sent on the quote", "sent on a print after it"])
def test_a_market_order_on_a_quote_that_shows_no_ask_is_refused(on: int) -> None:
    """A quote showing nothing on the ask has no touch for a buy to take, so a market buy is
    refused rather than filled against the book the engine holds — sent on that quote, or on
    the print after it, where the engine's book is the print and would fill it there."""
    points = [q(9.48, 9.52, 10), q(9.48, 9.52, 20, ask_size=0), t(9.5, 100, 30)]

    node, engine = scripted(points, {on: [("market", "BUY", 50, 0)]}, "print_through")
    _, touched = scripted(points, {on: [("market", "BUY", 50, 0)]}, "touch")

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == []
    if on == 3:
        assert fills_of(touched) == [(30, 50.0, 9.5, T)]


def test_a_market_order_past_the_displayed_size_walks_one_increment() -> None:
    """What the rule approximates: it sizes a taker down to the displayed size, and the venue
    fills what the quote shows at its touch and walks the rest one increment past it."""
    points = [q(9.48, 9.52, 10, ask_size=100), q(9.48, 9.52, 20, ask_size=100)]

    node, engine = scripted(points, {2: [("market", "BUY", 300, 0)]}, "print_through")

    assert fills_of(engine) == fills_of(node) == [(20, 100.0, 9.52, T), (20, 200.0, 9.53, T)]


@pytest.mark.parametrize(
    "side", [AggressorSide.NO_AGGRESSOR, AggressorSide.BUYER, AggressorSide.SELLER]
)
def test_a_limit_sent_after_a_print_outside_the_quote_rests_at_its_price(
    side: AggressorSide,
) -> None:
    """A print over the ask ends the quote, whichever side its aggressor hit, so a buy sent on
    it and limited between the ask and the print rests at its price, where the operator's rule
    would take it on the next quote; the quote that follows never fills a resting order, and
    the next print through the limit fills it as a maker, at the limit."""
    points = [
        q(9.48, 9.52, 10),
        t(9.55, 100, 100, side),
        q(9.48, 9.52, 110),
        t(9.52, 100, 120),
        q(9.48, 9.52, 130),
    ]

    node, engine = scripted(points, {2: [("limit", "BUY", 100, 9.53)]}, "print_through")

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == [(120, 100.0, 9.53, M)]


@pytest.mark.parametrize("rule", RULES)
def test_a_marketable_limit_s_rest_past_the_displayed_size_rests_at_its_own_price(
    rule: str,
) -> None:
    """A buy of 320 limited at 9.55 over an ask showing 100 at 9.52 takes the 100 at the touch
    and rests the rest at 9.55, a price the quote is through: neither of the quotes that follow
    fills it, and a print at 9.53 fills it as a maker at its own limit — above the market, by
    the print's size or whole. Without the fill of zero that ends the taker's answer, the
    engine would fill the rest at once, past the touch."""
    points = [
        *OPEN,
        q(9.48, 9.52, 100, ask_size=100),
        q(9.48, 9.52, 110),
        q(9.5, 9.52, 120),
        t(9.53, 100, 130),
        q(9.48, 9.52, 140),
    ]

    node, engine = scripted(points, {3: [("limit", "BUY", 320, 9.55)]}, rule)

    rest = 100.0 if rule == "print_through" else 220.0
    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == [(100, 100.0, 9.52, T), (130, rest, 9.55, M)]


IMMEDIATE: dict[str, tuple[list[object], dict[int, list[Any]], dict[int, list[Any]]]] = {
    "an IOC over the size shown": (
        [
            *OPEN,
            q(9.48, 9.52, 100, ask_size=100),
            q(9.5, 9.53, 110),
            t(9.51, 150, 400),
            q(9.5, 9.53, 410),
        ],
        {3: [("ioc", "BUY", 320, 9.52)]},
        {0: [(100, 100.0, 9.52, T)], 20: [], 30: []},
    ),
    "an IOC sent on a print inside the quote": (
        [*OPEN, t(9.5, 100, 100), t(9.49, 100, 130), q(9.48, 9.52, 140), t(9.49, 100, 150)],
        {3: [("ioc", "BUY", 320, 9.5)]},
        {0: [], 20: [], 30: []},
    ),
    "an IOC sent on a print outside the quote": (
        [*OPEN, t(9.6, 100, 100), t(9.59, 100, 130), q(9.58, 9.62, 140), t(9.55, 100, 150)],
        {3: [("ioc", "BUY", 320, 9.6)]},
        {0: [], 20: [], 30: []},
    ),
    "a FOK over the size shown": (
        [
            *OPEN,
            q(9.48, 9.52, 100, ask_size=100),
            q(9.5, 9.53, 110),
            t(9.51, 150, 400),
            q(9.5, 9.53, 410),
        ],
        {3: [("fok", "BUY", 320, 9.52)]},
        {0: [], 20: [], 30: []},
    ),
    "a FOK within the size shown": (
        [
            *OPEN,
            q(9.48, 9.52, 100, ask_size=100),
            q(9.48, 9.52, 110, ask_size=100),
            t(9.51, 150, 400),
            q(9.5, 9.53, 410),
        ],
        {3: [("fok", "BUY", 100, 9.52)]},
        {0: [(100, 100.0, 9.52, T)], 20: [(400, 100.0, 9.52, T)], 30: [(400, 100.0, 9.52, T)]},
    ),
}
"""Immediate-or-cancel and fill-or-kill limits: what each fills, by latency, under either rule."""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("name", list(IMMEDIATE))
def test_an_ioc_or_fok_limit_takes_what_the_quote_shows_and_never_rests(
    name: str, rule: str, latency_ms: int
) -> None:
    """An IOC limit takes what the quote in force shows within its limit and is cancelled for
    the rest; one the quote gives nothing — a print inside it moved the engine's own ask to the
    limit, or a print outside it ended it — is cancelled whole. A FOK fills whole from the quote
    or not at all. Neither is left resting for a later print to fill as a maker, which the
    rule's closing zero, ending the engine's fill before its own IOC cancel, would do. Under 20
    and 30 ms the IOC over the size shown lands on the print at 400 ms, where the quote in force
    no longer makes it marketable, and the FOK within it fills there on the quote before."""
    points, script, expected = IMMEDIATE[name]

    node, engine = scripted(points, script, rule, latency_ms)

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == expected[latency_ms]


DAY_TWO = [
    q(9.48, 9.52, 10),
    q(9.48, 9.52, 50),
    t(10.5, 100, 100, day=1),
    t(10.51, 100, 200, day=1),
    q(10.49, 10.52, 2_000, day=1),
]
"""A session that ends on 9.48/9.52 and a next that opens on prints over 10.50 before its first
quote."""

JUMP = [
    q(9.48, 9.52, 10),
    q(9.48, 9.52, 50),
    t(9.6, 100, 100),
    t(9.61, 100, 135),
    q(9.59, 9.62, 140),
]
"""Prints that trade over the last quote's ask before the next quote."""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("points", [DAY_TWO, JUMP], ids=["across sessions", "within one"])
def test_a_print_outside_the_quote_ends_it_and_a_market_order_on_it_is_refused(
    points: list[object], rule: str, latency_ms: int
) -> None:
    """A market buy sent on the first print over the ask does not fill at that ask, nine cents
    or 98 cents under the market these tapes print: the print ended the quote, so the order is
    refused for want of a market, at every latency, until a quote arrives. The engine's own
    rule fills it at a print."""
    node, engine = scripted(points, {3: [("market", "BUY", 100, 0)]}, rule, latency_ms)
    _, touched = scripted(points, {3: [("market", "BUY", 100, 0)]}, "touch", latency_ms)

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == []
    assert fills_of(touched) and all(px >= 9.6 for _, _, px, _ in fills_of(touched))


@pytest.mark.parametrize("latency_ms", [20, 30])
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("points", [DAY_TWO, JUMP], ids=["across sessions", "within one"])
def test_a_market_order_in_flight_when_a_print_outside_the_quote_arrives_is_refused(
    points: list[object], rule: str, latency_ms: int
) -> None:
    """A market buy sent on the last quote before the print over its ask, still in flight
    when that print arrives, lands on the print — before it under the rule, which lands what is
    due by a print first — and finds the quote already ended by it: refused, where landing it
    before the print ended the quote filled it at 9.52 under a market printing 9.60 or 10.50.
    The engine's own rule fills it at the print."""
    node, engine = scripted(points, {2: [("market", "BUY", 100, 0)]}, rule, latency_ms)
    _, touched = scripted(points, {2: [("market", "BUY", 100, 0)]}, "touch", latency_ms)

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == []
    assert fills_of(touched) and all(px >= 9.6 for _, _, px, _ in fills_of(touched))


MODIFIED = [*OPEN, t(9.49, 100, 100), q(9.48, 9.52, 200), t(9.47, 100, 300), q(9.48, 9.52, 400)]
"""A buy resting at 9.48 under a print of 100 at 9.49, then a print at 9.47 through 9.50."""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
def test_a_modify_through_the_print_in_hand_is_not_filled_by_it_on_either_path(
    rule: str, latency_ms: int
) -> None:
    """The print at 9.49 is not through the buy resting at 9.48; moved to 9.50 from that print's
    handler, the order is filled only by the next print through it. Once the modify lands the
    research path matches the order again against the print still in hand, which would credit
    it at its new price on that path alone: the print's record holds the price each order
    rested at."""
    script = {1: [("limit", "BUY", 320, 9.48)], 3: [("modify", "BUY", 320, 9.5)]}

    node, engine = scripted(MODIFIED, script, rule, latency_ms)

    filled = 100.0 if rule == "print_through" else 320.0
    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == [(300, filled, 9.5, M)]


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
def test_a_modify_landing_on_another_name_s_point_is_not_filled_by_the_print_in_hand(
    rule: str, latency_ms: int
) -> None:
    """The modify from the print's handler lands on OTHR's quotes under a latency, while the
    print is still the demo name's last point: the research path's match after it lands credits
    nothing, as the node's venue, which matches nothing then, credits nothing."""
    points = [
        *OPEN,
        q(19.98, 20.02, 15, on=OTHER),
        t(9.49, 100, 100),
        *[q(19.98, 20.02, ms, on=OTHER) for ms in (110, 120, 125, 130, 135)],
        q(9.48, 9.52, 200),
        t(9.47, 100, 300),
        q(9.48, 9.52, 400),
    ]
    script = {1: [("limit", "BUY", 320, 9.48)], 4: [("modify", "BUY", 320, 9.5)]}

    node, engine = scripted(points, script, rule, latency_ms, names=(INSTRUMENT, str(OTHER)))

    filled = 100.0 if rule == "print_through" else 320.0
    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == [(300, filled, 9.5, M)]


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize(
    ("printed", "limit", "filled"),
    [
        (9.49, 9.5, [(500, 100.0, 9.5, M)]),
        (9.53, 9.52, [(500, 100.0, 9.52, M)]),
        (9.53, 9.5, "taken"),
    ],
    ids=["under the bid", "inside, limit over the bid", "inside, limit under the bid"],
)
def test_a_buyer_s_print_leaves_the_two_paths_judging_a_limit_alike(
    rule: str, latency_ms: int, printed: float, limit: float, filled: Any
) -> None:
    """After a buyer's print the node's engine puts its bid back to the last quote's, while the
    research engine, matching again once OTHR's order lands, reads both sides from the print.
    A sell limit landing after that is judged alike on both paths: a print under the bid ended
    the quote, so the limit rests; inside it, the quote decides — over its bid of 9.51 the limit
    rests, under it the limit is taken at 9.51. A rest fills from the next print through it."""
    points = [
        q(9.51, 9.55, 10),
        q(19.98, 20.02, 15, on=OTHER),
        t(printed, 100, 100, AggressorSide.BUYER),
        q(19.98, 20.02, 100 + latency_ms + 1, on=OTHER),
        q(19.98, 20.02, 100 + latency_ms + 2, on=OTHER),
        q(19.98, 20.02, 100 + 2 * latency_ms + 3, on=OTHER),
        q(9.46, 9.48, 400),
        t(9.53, 100, 500, AggressorSide.BUYER),
    ]
    script = {3: [("limit", str(OTHER), "BUY", 10, 19.9)], 5: [("limit", "SELL", 100, limit)]}

    node, engine = scripted(points, script, rule, latency_ms, names=(INSTRUMENT, str(OTHER)))

    if filled == "taken":
        filled = [(100 + 2 * latency_ms + 2 + (1 if latency_ms else 0), 100.0, 9.51, T)]
    assert node.intents == engine.intents
    assert [f for f in fills_of(engine) if f[1] == 100.0] == filled
    assert fills_of(engine) == fills_of(node)
    if limit == 9.52:
        touched_node, touched = scripted(
            points, script, "touch", latency_ms, names=(INSTRUMENT, str(OTHER))
        )
        assert fills_of(touched) != fills_of(touched_node), "row 154 parts them under touch"


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
def test_a_print_with_no_aggressor_then_a_buyer_s_leave_the_two_paths_alike(
    rule: str, latency_ms: int
) -> None:
    """A print over the ask, then a buyer's print under the first: the node's engine keeps the
    ask the first print set and the research engine reads the second from its book, so a buy
    limited between them is marketable on one path and not the other; the first print ended
    the quote, so it rests on both and fills from the next print through it."""
    points = [
        q(9.51, 9.55, 10, ask_size=100),
        q(19.98, 20.02, 15, on=OTHER),
        t(9.57, 100, 100),
        t(9.56, 50, 110, AggressorSide.BUYER),
        q(19.98, 20.02, 110 + latency_ms + 1, on=OTHER),
        q(19.98, 20.02, 110 + latency_ms + 2, on=OTHER),
        q(19.98, 20.02, 110 + 2 * latency_ms + 3, on=OTHER),
        q(9.51, 9.55, 400, ask_size=100),
        t(9.5, 100, 500),
    ]
    script = {4: [("limit", str(OTHER), "BUY", 10, 19.9)], 6: [("limit", "BUY", 100, 9.56)]}

    node, engine = scripted(points, script, rule, latency_ms, names=(INSTRUMENT, str(OTHER)))
    touched_node, touched = scripted(
        points, script, "touch", latency_ms, names=(INSTRUMENT, str(OTHER))
    )

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == [(500, 100.0, 9.56, M)]
    assert fills_of(touched) != fills_of(touched_node), "row 154 parts them under touch"


SPLIT = {"splits": [{"ex_date": "2024-03-05", "ratio": 0.1}]}
"""The demo name's one-for-ten reverse split, effective the day after the first session."""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize(
    ("eve", "price"),
    [("quote", 95.1), ("print", 95.2)],
    ids=["eve ends on a quote", "eve ends on a print"],
)
def test_a_split_restates_the_quote_a_taker_fills_on_both_paths(
    eve: str, price: float, rule: str, latency_ms: int
) -> None:
    """Orders for the split name sent from the other name's points before its own first point
    of the ex-date take the restated quote: 9.49/9.51 restated is 94.90/95.10, and an eve that
    ends on a print of 9.50 after a quote of 9.48/9.52 leaves that quote restated, an ask of
    95.20 — not the print restated, 95.00, which is what the book holds."""
    last = q(9.49, 9.51, 500) if eve == "quote" else t(9.5, 100, 500)
    points = [
        q(9.48, 9.52, 10),
        q(19.98, 20.02, 15, on=OTHER),
        last,
        q(19.98, 20.02, 0, on=OTHER, day=1),
        q(19.98, 20.02, 50, on=OTHER, day=1),
        q(19.98, 20.02, 100, on=OTHER, day=1),
        q(94.8, 95.2, 200, day=1),
    ]
    script = {4: [("market", "BUY", 10, 0)], 5: [("limit", "BUY", 10, 95.3)]}

    node, engine = scripted(
        points, script, rule, latency_ms, names=(INSTRUMENT, str(OTHER)), infos={INSTRUMENT: SPLIT}
    )

    landed = (0, 50) if latency_ms == 0 else (50, 100)
    expected = [(DAY // MS + ms, 10.0, price, T) for ms in landed]
    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node) == expected
    assert node.run.equity == engine.run.equity


HANDLED: dict[str, tuple[list[object], dict[int, list[Any]], dict[int, list[Any]]]] = {
    "cancel the other buy on a fill": (
        [*OPEN, t(9.49, 300, 100), q(9.48, 9.52, 110), t(9.47, 300, 150), q(9.48, 9.52, 200)],
        {1: [("limit", "BUY", 200, 9.51), ("limit", "BUY", 200, 9.5)]},
        {1: [("cancel_all", "BUY", 0, 0)]},
    ),
    "sell at market on a fill": (
        [*OPEN, t(9.49, 100, 100), t(9.49, 100, 105), q(9.48, 9.52, 130), t(9.47, 100, 150)],
        {1: [("limit", "BUY", 100, 9.5)]},
        {1: [("market", "SELL", 100, 0)]},
    ),
    "exit on a fill": (
        [*OPEN, t(9.49, 100, 100), q(9.48, 9.52, 130), t(9.47, 100, 150), q(9.47, 9.51, 160)],
        {1: [("limit", "BUY", 100, 9.5)]},
        {1: [("exit", "BUY", 0, 0)]},
    ),
    "post a buy through the print on a fill": (
        [*OPEN, t(9.49, 500, 100), t(9.48, 100, 105), q(9.48, 9.52, 130), t(9.47, 100, 150)],
        {1: [("limit", "BUY", 100, 9.5)]},
        {1: [("limit", "BUY", 100, 9.5)]},
    ),
}
"""Orders a sleeve sends from its fill handler."""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("name", list(HANDLED))
def test_orders_sent_from_a_fill_handler_fill_alike_on_both_paths(
    name: str, rule: str, latency_ms: int
) -> None:
    """What a sleeve sends on a fill lands after the print that made it on both paths: a post
    through that print is never filled by it, and the paths agree on every fill, every intent,
    every recorded cost and every period's equity."""
    points, script, on_fill = HANDLED[name]

    node, engine = scripted(points, script, rule, latency_ms, on_fill=on_fill)

    assert node.intents == engine.intents
    assert node.run.fills == engine.run.fills
    assert node.run.equity == engine.run.equity
    assert engine.run.fills


OPERATOR = {
    "commission_bps": 0.0,
    "commission_per_share": 0.004,
    "slippage_ticks": 1.0,
    "maker_per_share": 0.004,
    "slippage_bps": 0.0,
    "spread": "fixed_bps",
    "fixed_bps": 0.0,
    "sell_fee_bps": 0.206,
    "sell_fee_per_share": 0.000195,
}
"""The operator's cost block, as `docs/workspace.md` shows it."""


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", RULES)
def test_the_operator_s_block_costs_every_fill_alike_on_both_paths(
    rule: str, latency_ms: int
) -> None:
    """Two names, a resting buy filled by prints, a market sale, a limit taken at its own price
    and an exit, under the operator's block: each fill is recorded with the same cost, tick and
    limit on both paths, and every period's equity is the same number. A maker pays $0.004 a
    share, a taker $0.004 and a cent unless its limit caps the cent, and a sale the fees."""
    points = [
        *OPEN,
        q(19.98, 20.02, 15, on=OTHER),
        t(9.49, 100, 100),
        q(19.98, 20.02, 110, on=OTHER),
        t(9.48, 100, 120),
        q(9.48, 9.52, 130),
        q(19.98, 20.02, 140, on=OTHER),
        q(9.5, 9.53, 150),
        q(19.99, 20.03, 160, on=OTHER),
    ]
    script = {
        1: [("limit", "BUY", 320, 9.5)],
        4: [("limit", str(OTHER), "BUY", 50, 20.02)],
        7: [("market", "SELL", 100, 0)],
        9: [("exit", str(OTHER), "SELL", 0, 0)],
    }

    node, engine = scripted(
        points, script, rule, latency_ms, names=(INSTRUMENT, str(OTHER)), costs=OPERATOR
    )

    assert node.intents == engine.intents
    assert node.run.fills == engine.run.fills
    assert node.run.equity == engine.run.equity
    for fill in engine.run.fills:
        room = 0.01 if fill.limit is None else fill.limit - fill.px
        if fill.side == "SELL" and fill.limit is not None:
            room = fill.px - fill.limit
        tick = 0.0 if fill.maker else fill.qty * min(0.01, max(0.0, room))
        fee = (
            fill.qty * fill.px * 0.206 / 10_000 + fill.qty * 0.000195 if fill.side == "SELL" else 0
        )
        assert fill.cost == pytest.approx(fill.qty * 0.004 + tick + fee, rel=1e-12, abs=1e-12)
    assert {fill.maker for fill in engine.run.fills} == {True, False}


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("first", ["print", "quote"])
def test_the_benchmark_hold_waits_for_a_quote_when_its_first_point_is_a_print(
    first: str, latency_ms: int
) -> None:
    """kanso's hold buys on the first point it may trade on. Under `print_through` a market
    order on a print before any quote is refused for want of a market, and the hold sends it
    again on the next point rather than holding nothing for the window."""
    opening = t(9.5, 100, 5) if first == "print" else q(9.48, 9.52, 5)
    points = [opening, q(9.48, 9.52, 10), t(9.51, 100, 20), q(9.5, 9.54, 30), q(9.6, 9.64, 400)]

    node, engine = scripted(points, {}, "print_through", latency_ms, source=HOLD.read_bytes())

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node)
    assert sum(qty for _, qty, _, _ in fills_of(engine)) > 0


@pytest.mark.parametrize("latency_ms", LATENCIES)
@pytest.mark.parametrize("rule", ["touch", "through", *RULES])
def test_the_benchmark_hold_sends_a_refused_entry_again_only_under_the_print_rules(
    rule: str, latency_ms: int
) -> None:
    """A first quote showing nothing on the ask has no touch for the hold's market buy, which
    the venue refuses under every rule. Under the print rules the hold sends it again on the
    next point; under `touch` and `through` it does not, so it holds nothing for the window,
    as it did before the print rules existed and a benchmark measured on them stays the same.
    At 20 and 30 ms the entry lands on a later quote and nothing is refused."""
    points = [
        q(9.48, 9.52, 5, ask_size=0),
        q(9.48, 9.52, 10),
        t(9.51, 100, 20),
        q(9.5, 9.54, 30),
        q(9.6, 9.64, 400),
    ]

    node, engine = scripted(points, {}, rule, latency_ms, source=HOLD.read_bytes())

    assert node.intents == engine.intents
    assert fills_of(engine) == fills_of(node)
    held = sum(qty for _, qty, _, _ in fills_of(engine))
    if latency_ms == 0 and rule in ("touch", "through"):
        assert held == 0 and len(engine.intents) == 1
    else:
        assert held > 0
