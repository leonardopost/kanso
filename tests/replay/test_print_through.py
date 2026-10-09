"""`limit_fill: print_through`: only a later print strictly through a resting order fills it, a
taker fills on the quote, and the two code paths agree on every fill as well as every intent.

Every expected row was measured on 2026-10-09 through both of kanso's paths with the rule's
prototype loaded where `kanso.nautilus.venue.fill_model` and `kanso.nautilus.actions.modules`
build the venue, stacked on the venue that applies every quote and print; the module that ships
reproduces all of them. Instants are milliseconds after 15:00 UTC on 2024-03-04, a point's
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
from tests.replay.conftest import INSTRUMENT, hypothesis, instrument, request_for

MS = 1_000_000
T0 = midnight_ns(date(2024, 3, 4)) + 15 * 3_600 * 1_000_000_000
DEMO = InstrumentId.from_str(INSTRUMENT)
OTHER = InstrumentId.from_str("OTHR.XNAS")
RULES = ("print_through", "print_through_whole")

SCRIPTED = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    script: dict = {}


class Strategy(KansoStrategy):
    \"\"\"On the n-th point it handles, sends what the script lists for n.\"\"\"

    config_cls = Config

    def on_start(self) -> None:
        self.seen = 0

    def _point(self, tick) -> None:
        self.seen += 1
        for kind, name, side, qty, price in self.kanso_config.script.get(str(self.seen), []):
            if kind == "cancel_all":
                self.cancel_all_orders(InstrumentId.from_str(name))
            elif kind == "market":
                self.submit_entry(InstrumentId.from_str(name), side, qty=qty)
            else:
                self.submit_entry(InstrumentId.from_str(name), side, qty=qty, price=price)

    def on_quote_tick(self, tick) -> None:
        self._point(tick)

    def on_trade_tick(self, tick) -> None:
        self._point(tick)


from nautilus_trader.model.identifiers import InstrumentId
"""

_printed = [0]


def q(bid: float, ask: float, ms: int, ask_size: int = 1_000, on: InstrumentId = DEMO) -> QuoteTick:
    """A quote showing 1,000 on the bid and `ask_size` on the ask."""
    ns = T0 + ms * MS
    return QuoteTick(
        on,
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_int(1_000),
        Quantity.from_int(ask_size),
        ns,
        ns,
    )


def t(px: float, size: int, ms: int, side: AggressorSide = AggressorSide.NO_AGGRESSOR) -> TradeTick:
    """A print of the demo name."""
    _printed[0] += 1
    ns = T0 + ms * MS
    return TradeTick(
        DEMO, Price(px, 2), Quantity.from_int(size), side, TradeId(f"P{_printed[0]}"), ns, ns
    )


def scripted(
    points: list[object],
    script: dict[int, list[tuple[Any, ...]]],
    rule: str,
    latency_ms: float = 0.0,
    names: tuple[str, ...] = (INSTRUMENT,),
) -> tuple[backtest.RunResult, backtest.RunResult]:
    """The scripted sleeve on both paths under `limit_fill: rule` at this latency: a step is
    `(kind, side, qty, price)` on the demo name, or `(kind, name, side, qty, price)`."""
    hyp = hypothesis(
        resolution="tick", horizon="1d", data_requirements=["quote", "trade"], universe=list(names)
    )
    steps = {
        str(n): [step if len(step) == 5 else (step[0], INSTRUMENT, *step[1:]) for step in sent]
        for n, sent in script.items()
    }
    request = request_for(hyp=hyp, source=SCRIPTED)
    model = dict(request.venue_model)
    model["costs"] = {**dict(model["costs"]), "limit_fill": rule, "latency_ms": latency_ms}  # type: ignore[arg-type]
    subject = replace(request, venue_model=model, overrides={"script": steps})
    groups = []
    for name in names:
        for kind in (QuoteTick, TradeTick):
            group = tuple(p for p in points if isinstance(p, kind) and str(p.instrument_id) == name)
            if group:
                groups.append(group)
    instruments = [instrument(name.split(".")[0]) for name in names]
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


def every(fills: list[Any]) -> dict[tuple[str, int], list[Any]]:
    return {(rule, latency): fills for rule in RULES for latency in (0, 20)}


def each_latency(rule: str, fills: list[Any]) -> dict[tuple[str, int], list[Any]]:
    return {(rule, latency): fills for latency in (0, 20)}


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
        | {(rule, 20): [(125, 300.0, 9.52, T)] for rule in RULES},
    ),
    "a limit marketable on the displayed size": (
        [*OPEN, q(9.48, 9.52, 100, ask_size=100), q(9.48, 9.53, 130), t(9.51, 50, 140)],
        {3: [("limit", "BUY", 320, 9.52)]},
        {
            ("print_through", 0): [(100, 100.0, 9.52, T), (140, 50.0, 9.52, M)],
            ("print_through", 20): [(140, 50.0, 9.52, M)],
            ("print_through_whole", 0): [(100, 100.0, 9.52, T), (140, 220.0, 9.52, M)],
            ("print_through_whole", 20): [(140, 320.0, 9.52, M)],
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
            ("print_through_whole", 0): [(110, 320.0, 9.5, M)],
            ("print_through_whole", 20): [(125, 320.0, 9.5, M)],
        },
    ),
    "a market order after a quote through a resting buy": (
        [*OPEN, q(9.45, 9.49, 100, ask_size=100), q(9.46, 9.5, 130)],
        {1: [("limit", "BUY", 320, 9.5)], 3: [("market", "BUY", 50, 0)]},
        {(rule, 0): [(100, 50.0, 9.49, T)] for rule in RULES}
        | {(rule, 20): [(130, 50.0, 9.5, T)] for rule in RULES},
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
        every([]),
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


@pytest.mark.parametrize("latency_ms", [0, 20])
@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_both_paths_fill_by_the_print_rule(name: str, rule: str, latency_ms: int) -> None:
    """Only a print through a resting order fills it — by its size under `print_through`,
    whole under `print_through_whole` — a taker fills on the quote, and the two paths agree on
    every fill and every intent at a tolerance of zero."""
    points, script, expected = SCENARIOS[name]

    node, engine = scripted(points, script, rule, latency_ms)

    assert node.intents == engine.intents
    assert fills_of(engine) == expected[(rule, latency_ms)]
    assert fills_of(node) == fills_of(engine)


ENGINE_RULES: dict[str, dict[tuple[str, int], tuple[list[Any], list[Any]]]] = {
    "a quote at the price, twice": {
        (rule, latency): ([(100, 100.0, 9.5, M), (110, 100.0, 9.5, M)],) * 2
        for rule in ("touch", "through")
        for latency in (0, 20)
    },
    "a print at the price": {
        ("touch", 0): ([(100, 100.0, 9.5, M)],) * 2,
        ("touch", 20): ([(100, 100.0, 9.5, M)],) * 2,
        ("through", 0): ([],) * 2,
        ("through", 20): ([],) * 2,
    },
    "a print through, smaller than the order": {
        (rule, latency): ([(100, 100.0, 9.5, M), (100, 220.0, 9.5, M)],) * 2
        for rule in ("touch", "through")
        for latency in (0, 20)
    },
    "a market order sent on a print": {
        (rule, 0): ([(100, 100.0, 9.5, T), (100, 200.0, 9.51, T)],) * 2
        for rule in ("touch", "through")
    }
    | {
        (rule, 20): ([(125, 100.0, 9.5, T), (125, 200.0, 9.51, T)],) * 2
        for rule in ("touch", "through")
    },
    "a limit marketable on the displayed size": {
        (rule, 0): (
            [
                (100, 100.0, 9.52, T),
                (100, 100.0, 9.52, M),
                (140, 50.0, 9.52, M),
                (140, 70.0, 9.52, M),
            ],
            [(100, 100.0, 9.52, T), (140, 50.0, 9.52, M), (140, 170.0, 9.52, M)],
        )
        for rule in ("touch", "through")
    }
    | {
        (rule, 20): ([(140, 50.0, 9.52, M), (140, 270.0, 9.52, M)],) * 2
        for rule in ("touch", "through")
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
        ("through", 0): ([],) * 2,
        ("through", 20): ([(50, 100.0, 9.9, T)],) * 2,
    },
}
"""What the engine's own rules fill in six of the scenarios above, as (research, node), measured
before the print rule's module existed: the module is loaded under every rule and must move none
of it — the two paths' row-154 differences included."""


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


def test_a_market_order_on_a_quote_that_shows_no_ask_is_refused() -> None:
    """A quote showing nothing on the ask has no touch for a buy to take, so a market buy is
    refused rather than filled against the book the engine holds."""
    points = [q(9.48, 9.52, 10), q(9.48, 9.52, 20, ask_size=0), t(9.5, 100, 30)]

    node, engine = scripted(points, {2: [("market", "BUY", 50, 0)]}, "print_through")

    assert fills_of(engine) == fills_of(node) == []


def test_a_market_order_past_the_displayed_size_walks_one_increment() -> None:
    """What the rule approximates: it sizes a taker down to the displayed size, and the venue
    fills what the quote shows at its touch and walks the rest one increment past it."""
    points = [q(9.48, 9.52, 10, ask_size=100), q(9.48, 9.52, 20, ask_size=100)]

    node, engine = scripted(points, {2: [("market", "BUY", 300, 0)]}, "print_through")

    assert fills_of(engine) == fills_of(node) == [(20, 100.0, 9.52, T), (20, 200.0, 9.53, T)]


def test_a_limit_between_the_touch_and_a_print_standing_above_it_rests() -> None:
    """The residual the docs state: sent on a print standing above the ask, a buy limited
    between them is not marketable to the engine, so it rests and fills later from a print
    through it as a maker, where the operator's rule would take it on the next quote."""
    points = [
        q(9.48, 9.52, 10),
        t(9.55, 100, 100),
        q(9.48, 9.52, 110),
        t(9.52, 100, 120),
        q(9.48, 9.52, 130),
    ]

    node, engine = scripted(points, {2: [("limit", "BUY", 100, 9.53)]}, "print_through")

    assert fills_of(engine) == fills_of(node) == [(120, 100.0, 9.53, M)]
