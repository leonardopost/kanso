"""The anti-cheat boundary: every denial the toolbox states, and everything it must allow."""

from __future__ import annotations

import inspect
import re
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from nautilus_trader.trading.strategy import Strategy

from kanso.criteria.integrity import (
    AUTHORED,
    DENIED_BOOK,
    DENIED_BUILTINS,
    DENIED_CLOCK,
    DENIED_DEPTH,
    DENIED_DUNDERS,
    DENIED_HISTORY,
    DENIED_MODULES,
    DENIED_NUMPY_FILE,
    DENIED_SCHEDULE,
    DENIED_STALE_BASIS,
    DENIED_UNDER_DEPTH,
    SEND_MARKS,
    check,
    clashes,
    import_allowed,
    overridden,
    owned,
    scan,
    scope,
    sends,
)
from kanso.nautilus.strategy import KansoModifier, KansoStrategy
from kanso.workspace import PACKAGE_ROOT

ALLOWED_SOURCES = [
    "import numpy as np",
    "import numpy.linalg",
    "from numpy import array",
    "import math",
    "from math import sqrt",
    "import statistics",
    "from statistics import median",
    "from collections import deque",
    "from collections.abc import Sequence",
    "from dataclasses import dataclass",
    "from typing import ClassVar",
    "from decimal import Decimal",
    "from datetime import timedelta",
    "from nautilus_trader.model.data import Bar",
    "import nautilus_trader.model.data",
    "from nautilus_trader.trading.strategy import Strategy",
    "from nautilus_trader.indicators.average.ema import ExponentialMovingAverage",
    "from nautilus_trader.core.datetime import unix_nanos_to_dt",
    "from nautilus_trader.core.uuid import UUID4",
    "from nautilus_trader.core.message import Event",
    "from nautilus_trader.core.data import Data",
    "from nautilus_trader.core.stats import fast_mean",
    "from nautilus_trader.core.correctness import PyCondition",
    "from nautilus_trader.core.fsm import FiniteStateMachine",
    "from kanso.nautilus.strategy import KansoStrategy",
    "import kanso.nautilus.strategy",
]

DENIED_PATHS = [
    "nautilus_trader",
    "nautilus_trader.core",
    "nautilus_trader.core.nautilus_pyo3",
    "nautilus_trader.core.rust",
    "nautilus_trader.core.rust.model",
    "nautilus_trader.persistence",
    "nautilus_trader.persistence.catalog",
    "nautilus_trader.backtest",
    "nautilus_trader.backtest.engine",
    "nautilus_trader.adapters.binance",
    "nautilus_trader.live.node",
    "kanso",
    "kanso.nautilus",
    "kanso.data",
    "kanso.data.catalog",
    "requests",
    "os.path",
    "pandas",
]


@pytest.mark.parametrize("source", ALLOWED_SOURCES)
def test_an_allowed_import_passes(source: str) -> None:
    assert scan(source) == []


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_a_denied_import_path_is_refused_both_ways(path: str) -> None:
    assert not import_allowed(path)
    head, _, leaf = path.rpartition(".")
    assert scan(f"import {path}"), f"import {path} was allowed"
    if head:
        assert scan(f"from {head} import {leaf}"), f"from {head} import {leaf} was allowed"


@pytest.mark.parametrize(
    "path",
    [
        "nautilus_trader.core.nautilus_pyo3",
        "nautilus_trader.core.rust",
        "nautilus_trader.persistence",
        "nautilus_trader.backtest",
    ],
)
def test_the_catalog_reaching_packages_are_refused_by_name(path: str) -> None:
    (problem,) = scan(f"import {path}")
    assert "is denied" in problem and "catalog" in problem or "certification" in problem


def test_a_relative_import_has_no_path_to_allow() -> None:
    assert scan("from . import helper") == ["line 1: a relative import has no allow-listed path"]


def test_a_star_import_binds_names_nobody_declared() -> None:
    (problem,) = scan("from numpy import *")
    assert "binds undeclared names" in problem


@pytest.mark.parametrize("name", sorted(DENIED_BUILTINS))
def test_every_denied_builtin_is_refused_as_a_name(name: str) -> None:
    assert scan(f"x = {name}"), f"{name} was allowed as a name"


@pytest.mark.parametrize("name", sorted(DENIED_MODULES))
def test_every_denied_module_is_refused_as_a_name_and_an_attribute(name: str) -> None:
    assert scan(f"x = {name}"), f"{name} was allowed as a name"
    assert scan(f"x = obj.{name}"), f".{name} was allowed as an attribute"
    assert scan(f"import {name}"), f"import {name} was allowed"


@pytest.mark.parametrize("name", sorted(DENIED_DUNDERS))
def test_every_introspection_dunder_is_refused_as_an_attribute(name: str) -> None:
    assert scan(f"x = obj.{name}"), f".{name} was allowed"


@pytest.mark.parametrize("name", sorted(DENIED_NUMPY_FILE))
def test_every_numpy_file_function_is_refused(name: str) -> None:
    assert scan(f"x = np.{name}('f')"), f"np.{name} was allowed"
    assert scan(f"from numpy import {name}"), f"from numpy import {name} was allowed"


@pytest.mark.parametrize("name", ["nautilus_pyo3", "capsule_to_list"])
def test_the_bridge_names_are_refused(name: str) -> None:
    assert scan(f"x = obj.{name}"), f".{name} was allowed"
    assert scan(f"{name} = 1"), f"{name} was allowed as a name"


@pytest.mark.parametrize("name", sorted(DENIED_CLOCK))
def test_the_component_clock_and_its_timer_api_are_refused(name: str) -> None:
    assert scan(f"x = self.{name}"), f"self.{name} was allowed"


@pytest.mark.parametrize("name", sorted(DENIED_SCHEDULE))
def test_the_route_to_the_split_schedule_is_out_of_reach(name: str) -> None:
    """`info.splits` names splits after the window a card is judged on, and the cache
    hands out the instrument that carries it."""
    assert scan(f"x = self.{name}"), f"self.{name} was allowed"


@pytest.mark.parametrize("name", sorted(DENIED_HISTORY))
def test_the_history_requests_and_the_warmup_gate_are_out_of_reach(name: str) -> None:
    """History arrives only as the declared prefix; a request or the gate's own state would
    be a second route to it, or a way to trade before the open."""
    (problem,) = scan(f"x = self.{name}")

    assert f"attribute '.{name}' is denied" in problem
    assert "declare `warmup: {sessions: N}`" in problem


@pytest.mark.parametrize("name", sorted(DENIED_BOOK))
def test_the_book_policy_s_own_state_is_out_of_reach(name: str) -> None:
    """The policy's anchor is where the window opens and its cushion what the book made
    before this month; the book it leaves is read from `balance` alone."""
    (problem,) = scan(f"x = self.{name}")

    assert f"attribute '.{name}' is denied" in problem
    assert "read the book the policy left from `balance`" in problem


def test_everything_the_runner_hands_the_harness_for_a_book_policy_is_denied() -> None:
    """A new piece of policy state the runner sets is out of reach the moment it is added."""
    from types import SimpleNamespace

    from kanso.nautilus.costs import BookPolicy
    from kanso.nautilus.cross_section import book

    sleeve = SimpleNamespace()
    book(sleeve, BookPolicy(reset="monthly"), 0, 1, cushion=1.0, settled_ns=0)

    assert set(vars(sleeve)) <= DENIED_BOOK


@pytest.mark.parametrize("name", sorted(DENIED_DEPTH))
def test_the_harness_s_own_book_under_depth_is_out_of_reach(name: str) -> None:
    """The harness keeps every change of a `depth` book; the author is handed the view."""
    (problem,) = scan(f"x = self.{name}")

    assert f"attribute '.{name}' is denied" in problem
    assert "read them there" in problem


def test_everything_the_harness_keeps_of_a_depth_book_is_denied() -> None:
    """A new piece of the harness's book, or a new way of showing it, is out of reach the
    moment it is added."""
    from tests.nautilus.strategy.test_sleeve import config

    sleeve = KansoStrategy(config(depth=(100_000_000, 3)))
    kept = {name for name in vars(sleeve) if name.startswith(("_depth", "_top"))}
    shown = {name for name in dir(KansoStrategy) if name.startswith(("_show", "_take_depth"))}

    assert kept
    assert kept | shown <= DENIED_DEPTH


@pytest.mark.parametrize("name", sorted(DENIED_UNDER_DEPTH))
def test_the_book_subscriptions_are_refused_under_depth_alone(name: str) -> None:
    """Under `depth` they reach the book around the view; without it they are ordinary."""
    (problem,) = scan(f"self.{name}", depth=True)

    assert f"attribute '.{name}' is denied under depth" in problem
    assert "on_order_book_deltas and on_quote_tick" in problem
    assert scan(f"self.{name}") == []


def test_every_route_the_engine_gives_to_the_bus_or_a_book_is_refused_under_depth() -> None:
    """Read off the engine rather than off the set, so a member dropped from it fails here:
    every name a strategy or an attached construct holds the bus by, and every subscription
    to a book. Measured on nautilus_trader 1.231.0: `msgbus` and `_msgbus`, and three
    subscriptions, on `Strategy` and `Actor` alike."""
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.trading.strategy import Strategy

    routes = {
        name
        for cls in (Strategy, Actor)
        for name in dir(cls)
        if "msgbus" in name or name.startswith("subscribe_order_book")
    }

    assert routes >= {
        "msgbus",
        "_msgbus",
        "subscribe_order_book_deltas",
        "subscribe_order_book_at_interval",
        "subscribe_order_book_depth",
    }
    for name in sorted(routes):
        assert scan(f"self.{name}", depth=True), f".{name} reaches the book under depth"


def test_every_request_and_every_clock_the_engine_gives_an_actor_is_refused() -> None:
    """Read off the engine rather than off the sets, so a request or a clock it adds is
    refused the day it is added: every `request_*` hands the author history, a book the
    account is not served — a snapshot, the deltas or the depth of a book — or an instrument
    with its splits, and the clock, under either name, the instant the engine is at, which
    under `depth` is every change of the book. Measured on nautilus_trader 1.231.0: twelve
    requests and `clock` and `_clock`, on `Strategy` and `Actor` alike."""
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.trading.strategy import Strategy

    routes = {
        name
        for cls in (Strategy, Actor)
        for name in dir(cls)
        if name.startswith("request_") or "clock" in name
    }

    assert routes >= {
        "clock",
        "_clock",
        "request_order_book_snapshot",
        "request_order_book_deltas",
        "request_order_book_depth",
        "request_instrument",
    }
    for name in sorted(routes):
        for depth in (False, True):
            assert scan(f"self.{name}", depth=depth), f".{name} was allowed"


@pytest.mark.parametrize(
    "line",
    [
        "at = self._clock.timestamp_ns()",
        "self.request_order_book_snapshot(InstrumentId.from_str('DEMO.XNAS'), limit=10)",
        "self.request_order_book_depth(InstrumentId.from_str('DEMO.XNAS'), depth=10)",
        'shown = "{0._clock}".format(self)',
    ],
)
def test_a_depth_sleeve_is_refused_the_clock_and_a_book_of_its_own_asking(line: str) -> None:
    """Two routes past the view a `depth` sleeve is handed: the engine's instant, which there
    is the instant of every change, and a book asked for rather than subscribed."""
    source = (
        "from nautilus_trader.model.identifiers import InstrumentId\n"
        "class Strategy:\n"
        "    def on_order_book_deltas(self, deltas):\n"
        f"        {line}\n"
    )

    assert scan(source, depth=True)
    assert scan(source)


def test_an_attached_construct_under_depth_may_not_subscribe_the_book() -> None:
    source = (
        "from nautilus_trader.model.identifiers import InstrumentId\n"
        "class Modifier:\n"
        "    def on_start(self):\n"
        "        self.subscribe_order_book_deltas(InstrumentId.from_str('DEMO.XNAS'))\n"
    )

    assert scan(source, construct="filter") == []
    (problem,) = scan(source, depth=True, construct="filter")
    assert "'.subscribe_order_book_deltas' is denied under depth" in problem


@pytest.mark.parametrize(
    "source",
    [
        'books = self.__getattribute__("_depth_books")',
        'cache = object.__getattribute__(self, "cache")',
        "kept = self.__getstate__()",
        "kept = self.__reduce__()",
        "kept = self.__reduce_ex__(2)",
        'opened = __builtins__["open"]',
        "opened = len.__self__.open",
        "up = (x for x in ()).gi_frame.f_back",
        "try:\n    1 / 0\nexcept ZeroDivisionError as exc:\n    up = exc.__traceback__",
        "up = frame.tb_frame.f_locals",
        "up = frame.f_globals",
        "up = frame.f_builtins",
        "up = coroutine.cr_frame",
        "up = generator.ag_frame",
        'shown = "{0._depth_books[DEMO.XNAS][1].keys}".format(self)',
        'shown = "{0.price:{1.cache}}".format(tick, self)',
        'shown = "{0.__dict__}".format(self)',
    ],
)
def test_a_denied_name_reached_by_another_spelling_is_refused(source: str) -> None:
    """The attribute rules match a spelling, so every spelling that reaches the same object
    by a name held in a string, by an instance's attributes all at once, by the builtins
    module or by a frame is refused as well."""
    assert scan(source), f"{source!r} was allowed"


def test_the_routes_refused_reach_what_they_are_refused_for() -> None:
    """Not a hypothetical: run as a strategy file is run, each spelling hands over the
    builtin `open`, an attribute by its name, or an instance's attributes."""
    import builtins

    from kanso.nautilus.backtest import _module

    module = _module(
        b"OPEN = len.__self__.open\n"
        b"HELD = __builtins__\n"
        b"class Kept:\n"
        b"    pass\n"
        b"KEPT = Kept()\n"
        b"KEPT.cache = 1\n"
        b"BY_NAME = object.__getattribute__(KEPT, 'cache')\n"
        b"ALL = KEPT.__getstate__()\n",
        "integrity_probe",
    )

    assert module.OPEN is open
    assert module.HELD in (builtins, vars(builtins))
    assert (module.BY_NAME, module.ALL) == (1, {"cache": 1})


@pytest.mark.parametrize(
    "source",
    [
        'shown = "{0.price:>10}".format(tick)',
        'shown = "{0[cache]}".format(held)',
        'shown = "{0.close} {1}".format(bar, 2)',
        'shown = "{ not a field"',
        'shown = "{0[a]b.cache}"',
        'shown = "{0.subscribe_order_book_depth}".format(self)',
    ],
)
def test_a_format_field_naming_no_denied_attribute_passes(source: str) -> None:
    """An item key is not an attribute, a malformed string or field formats nothing — after
    an item only `.` or `[` may follow — and a book subscription is an ordinary attribute
    without `depth`."""
    assert scan(source) == []


@pytest.mark.parametrize("depth", [False, True])
def test_a_format_field_naming_the_bus_is_refused_with_depth_or_without(depth: bool) -> None:
    (problem,) = scan('shown = "{0._msgbus}".format(self)', depth=depth)

    assert "format field '{0._msgbus}'" in problem
    assert "the engine's message bus" in problem


def test_a_format_field_naming_a_book_subscription_is_refused_under_depth() -> None:
    (problem,) = scan('shown = "{0.subscribe_order_book_depth}".format(self)', depth=True)

    assert "format field '{0.subscribe_order_book_depth}'" in problem
    assert "on_order_book_deltas and on_quote_tick" in problem


def test_a_lane_under_depth_is_checked_for_what_depth_refuses(lane: Path) -> None:
    (lane / "strategy.py").write_text("x = self.subscribe_order_book_depth\n", encoding="utf-8")

    assert check(lane, {}) == []
    (problem,) = check(lane, {}, depth=True)
    assert "'.subscribe_order_book_depth' is denied under depth" in problem


def test_a_strategy_reaching_for_the_schedule_is_refused_by_the_route_it_took() -> None:
    """The lookahead a schedule would otherwise open, and the only spelling that reaches it."""
    source = (
        "class Strategy:\n"
        "    def on_bar(self, bar):\n"
        "        held = self.cache.instrument(bar.bar_type.instrument_id)\n"
        "        return held.info\n"
    )

    problems = scan(source)

    assert len(problems) == 1
    assert "attribute '.cache' is denied" in problems[0]
    assert "every split of its life" in problems[0]


def test_the_logging_call_every_strategy_writes_is_not_a_denial() -> None:
    """`.info` is the engine's own logger method, so the word is not what is denied."""
    assert (
        scan('class Strategy:\n    def on_bar(self, bar):\n        self.log.info("hello")\n') == []
    )


def test_a_sleeve_holds_no_split_schedule_to_deny() -> None:
    """The schedule lives in the venue's simulation module, which `strategy.py` cannot name.

    A private attribute on the base class would not have closed the channel — `self._x` is
    spellable and so is `self._KansoStrategy__x` — so the sleeve holds none at all.
    """
    from kanso.nautilus.strategy import KansoStrategy

    assert not [name for name in vars(KansoStrategy) if "split" in name.lower()]


@pytest.mark.parametrize("name", sorted(DENIED_STALE_BASIS))
def test_every_quantity_the_engine_derives_from_a_stale_basis_is_refused(name: str) -> None:
    """`avg_px_open` survives a split unrescaled, so nothing computed from it may be read."""
    assert scan(f"x = self.portfolio.{name}"), f".{name} was allowed"


def test_a_sleeve_sizing_off_the_account_balance_is_refused() -> None:
    """Measured under kanso's own venue: a $100,000 account read $109,000 once a position
    that had spanned a one-for-ten reverse split closed."""
    (problem,) = scan("qty = self.portfolio.account(venue).balance_free()")

    assert "attribute '.account' is denied" in problem
    assert "size from `last_price`" in problem


def test_an_alias_cannot_smuggle_a_denied_name_in() -> None:
    assert scan("import numpy as load")
    assert scan("from numpy import array as open")
    assert scan("from datetime import time")


def test_bar_open_is_not_the_builtin() -> None:
    assert (
        scan("def on_bar(self, bar):\n    return bar.open + bar.high - bar.low + bar.close") == []
    )
    assert scan("x = Bar.open") == []


def test_the_builtin_open_is_still_refused() -> None:
    (problem,) = scan("open('/etc/passwd')")
    assert "'open' is a denied identifier" in problem


def test_a_file_that_does_not_parse_is_a_single_problem() -> None:
    (problem,) = scan("def broken(:\n")
    assert "does not parse" in problem


def test_every_problem_carries_the_line_it_is_on() -> None:
    problems = scan("import os\n\nimport sys\n")
    assert any("line 1" in p for p in problems)
    assert any("line 3" in p for p in problems)


def test_the_same_problem_on_one_line_is_reported_once() -> None:
    assert scan("x = os + os") == ["line 1: 'os' is a denied identifier, used as a name"]


def test_the_same_problem_on_two_lines_is_reported_twice() -> None:
    assert len(scan("import os\nimport os\n")) == 2


def test_a_clean_lane_directory_has_nothing_to_report(lane: Path) -> None:
    assert check(lane, {}) == []


def test_a_fourth_file_in_the_lane_directory_is_out_of_scope(lane: Path) -> None:
    (lane / "notes.md").write_text("a learnings file", encoding="utf-8")
    assert check(lane, {}) == ["'notes.md' is not one of the three scoped files"]


def test_transient_artefacts_are_not_the_researcher_s(lane: Path) -> None:
    (lane / "__pycache__").mkdir()
    (lane / ".DS_Store").write_text("", encoding="utf-8")
    assert check(lane, {}) == []


def test_a_missing_scoped_file_is_reported(lane: Path) -> None:
    (lane / "program.md").unlink()
    assert check(lane, {}) == ["'program.md' is missing from the lane directory"]


def test_a_lane_without_a_strategy_reports_only_its_scope(lane: Path) -> None:
    (lane / "strategy.py").unlink()
    assert check(lane, {}) == ["'strategy.py' is missing from the lane directory"]


def test_a_pinned_file_that_changed_is_reported(lane: Path) -> None:
    pinned = {
        name: sha256((lane / name).read_bytes()).hexdigest()
        for name in ("hypothesis.yaml", "program.md")
    }
    assert check(lane, pinned) == []
    (lane / "hypothesis.yaml").write_text("schema: 1\nid: other\n", encoding="utf-8")
    assert check(lane, pinned) == ["'hypothesis.yaml' no longer equals the blob this run pinned"]


def test_a_pin_for_an_absent_file_is_not_a_second_complaint(lane: Path) -> None:
    (lane / "program.md").unlink()
    problems = check(lane, {"program.md": "0" * 64})
    assert problems == ["'program.md' is missing from the lane directory"]


def test_a_lane_directory_that_is_not_there_is_reported(tmp_path: Path) -> None:
    (problem,) = scope(tmp_path / "absent", {})
    assert "cannot be read" in problem


def test_a_strategy_that_is_not_text_is_reported(lane: Path) -> None:
    (lane / "strategy.py").write_bytes(b"\xff\xfe\x00 not utf-8")
    (problem,) = check(lane, {})
    assert "cannot be read as text" in problem


@pytest.mark.parametrize("stub", ["strategy_sleeve.py", "strategy_modifier.py"])
def test_the_shipped_strategy_stubs_pass(stub: str) -> None:
    source = (PACKAGE_ROOT / "templates" / stub).read_text(encoding="utf-8")
    assert scan(source, stub) == []


# --- under a sizing rule ----------------------------------------------------------


SIZED_SLEEVE = "\n".join(
    [
        "class Strategy:",
        "    def on_bar(self, bar):",
        "        self.submit_entry(bar.bar_type.instrument_id, 'BUY', notional=self.notional)",
        "        self.submit_entry(bar.bar_type.instrument_id, 'BUY', qty=10)",
        "        self.submit_exit(bar.bar_type.instrument_id, price=9.5)",
        "        self.submit_entry(bar.bar_type.instrument_id, 'BUY', **self.kw)",
        "        net = self.portfolio.net_position(bar.bar_type.instrument_id)",
        "        self.submit_order("
        "self.order_factory.market(bar.bar_type.instrument_id, 'BUY', 1))",
        "        self.modify_order(net, quantity=2)",
        "        self.close_all_positions(bar.bar_type.instrument_id)",
    ]
)


def test_the_size_knobs_are_refused_under_a_sized_hypothesis() -> None:
    problems = scan(SIZED_SLEEVE, sized=True)

    assert "line 3: keyword 'notional=' is denied under sizing" in "\n".join(problems)
    assert any("line 4: keyword 'qty='" in p for p in problems)
    assert any(
        "line 5: keyword 'price='" in p and "submit_exit(instrument_id)" in p for p in problems
    )
    assert any("line 6: '**' on submit_entry is denied" in p for p in problems)
    assert any(
        "line 7: attribute '.portfolio' is denied" in p and "self.held(" in p for p in problems
    )
    assert any("'.submit_order' is denied" in p for p in problems)
    assert any("'.order_factory' is denied" in p for p in problems)
    assert any("'.modify_order' is denied" in p for p in problems)
    assert any("'.close_all_positions' is denied" in p for p in problems)
    assert all("because the harness sizes every order" in p or "self.held(" in p for p in problems)


def test_the_size_knobs_are_allowed_under_free_sizing() -> None:
    assert scan(SIZED_SLEEVE) == []
    assert scan(SIZED_SLEEVE, sized=False, construct="sleeve") == []


def test_an_override_of_a_harness_method_is_refused_sized_or_not() -> None:
    """Under a sizing rule an override is a size knob no attribute scan can see; without one
    it is still the harness's method, so the refusal is the same either way."""
    source = f"{SLEEVE_HEAD}    def _quantise(self, instrument, raw):\n        return raw * 2\n"

    for sized in (True, False):
        (problem,) = scan(source, sized=sized)
        assert problem.startswith("line 3: '_quantise' belongs to KansoStrategy")
    assert scan(f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        pass\n", sized=True) == []
    assert scan(f"{SLEEVE_HEAD}    def _signal(self):\n        pass\n", sized=True) == []


def test_hedge_is_refused_for_a_sized_overlay_and_clip_for_a_free_one() -> None:
    hedging = (
        "from kanso.nautilus.strategy import Decision, Hedge\n"
        "class Modifier:\n"
        "    def on_data(self, ctx):\n"
        "        return Decision(scale=1.0, hedges=(Hedge('X', 1.0),))\n"
    )
    clipping = (
        "from kanso.nautilus.strategy import Clip, Decision\n"
        "class Modifier:\n"
        "    def on_data(self, ctx):\n"
        "        return Decision(clips=(Clip('X', 'BUY'),))\n"
    )

    sized_problems = scan(hedging, sized=True, construct="overlay")
    assert any("name 'Hedge' is denied for this overlay" in p for p in sized_problems)
    assert any("keyword 'hedges='" in p for p in sized_problems)
    assert any("keyword 'scale='" in p for p in sized_problems)
    assert all("Decision(clips=(Clip(instrument, side),))" in p for p in sized_problems)
    assert scan(clipping, sized=True, construct="overlay") == []

    free_problems = scan(clipping, sized=False, construct="overlay")
    assert any("name 'Clip' is denied for this overlay" in p for p in free_problems)
    assert any("keyword 'clips='" in p for p in free_problems)
    assert scan(hedging, sized=False, construct="overlay") == []


def test_a_sized_overlay_may_write_its_two_hooks_and_nothing_of_the_harness() -> None:
    hooks = (
        f"{MODIFIER_HEAD}    def evaluate(self, ctx):\n        pass\n"
        "    def on_data(self, ctx):\n        pass\n"
    )
    assert scan(hooks, sized=True, construct="overlay") == []
    (problem,) = scan(
        f"{MODIFIER_HEAD}    def _start(self):\n        pass\n", sized=True, construct="overlay"
    )
    assert problem.startswith("line 3: '_start' belongs to KansoModifier")


def test_a_filter_is_held_to_no_sizing_vocabulary() -> None:
    assert scan("x = Clip\ny = Hedge\n", sized=True, construct="filter") == []


def test_the_overlay_vocabulary_is_refused_as_an_attribute_and_a_bare_call_is_seen() -> None:
    (problem,) = scan(
        "import kanso.nautilus.strategy as s\nx = s.Hedge", sized=True, construct="overlay"
    )
    assert "name 'Hedge' is denied for this overlay" in problem
    (bare,) = scan("submit_entry(x, 'BUY', qty=1)", sized=True)
    assert (
        "keyword 'qty=' is denied under sizing" in bare
        and "submit_entry(instrument_id, side)" in bare
    )


# --- the names the base class owns ----------------------------------------------------


SLEEVE_HEAD = "from kanso.nautilus.strategy import KansoStrategy\nclass Strategy(KansoStrategy):\n"
MODIFIER_HEAD = (
    "from kanso.nautilus.strategy import KansoModifier\nclass Modifier(KansoModifier):\n"
)

SHADOWING = """from kanso.nautilus.strategy import KansoConfig, KansoStrategy

CLOSE = 3


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self._close = CLOSE
"""
"""The shape that crashed at its first exit: the column index of a close, stored under the
name of the harness's own exit method."""


def test_assigning_a_name_the_sleeve_base_owns_names_the_name_the_line_and_the_base() -> None:
    (problem,) = scan(SHADOWING)

    assert problem.startswith(
        "line 14: '_close' belongs to KansoStrategy, and binding 'self._close' replaces it"
    )
    assert "rename yours" in problem
    assert "underscored or not" in problem


@pytest.mark.parametrize(
    ("body", "line", "name", "how"),
    [
        ("    def _close(self, instrument_id):\n        pass\n", 3, "_close", "'def _close'"),
        ("    _fund = None\n", 3, "_fund", "binding '_fund' on the class"),
        ("    _refund: int = 0\n", 3, "_refund", "binding '_refund' on the class"),
        ("    def on_start(self):\n        self._last_price = {}\n", 4, "_last_price", None),
        ("    def on_start(self):\n        self._cfg: dict = {}\n", 4, "_cfg", None),
        ("    def on_start(self):\n        self.size = 10\n", 4, "size", None),
        ("    def on_bar(self, bar):\n        self._refund += 1\n", 4, "_refund", None),
        ("    def on_start(self):\n        self.a, self._held = 1, 2\n", 4, "_held", None),
        ("    def on_start(me):\n        me._fund = 0\n", 4, "_fund", "binding 'me._fund'"),
        (
            "    def on_start(self):\n        def later():\n            self._close = 0\n",
            5,
            "_close",
            None,
        ),
        ("    @property\n    def balance(self):\n        return 1.0\n", 4, "balance", None),
    ],
)
def test_every_way_of_binding_a_sleeve_name_is_refused(
    body: str, line: int, name: str, how: str | None
) -> None:
    (problem,) = scan(SLEEVE_HEAD + body)

    assert problem.startswith(f"line {line}: '{name}' belongs to KansoStrategy")
    if how is not None:
        assert f"and {how} replaces it" in problem


def test_what_the_base_leaves_to_its_author_is_not_a_clash() -> None:
    sleeve = (
        "from kanso.nautilus.strategy import KansoConfig, KansoStrategy\n"
        "class Config(KansoConfig):\n"
        "    lookback: int = 20\n"
        "class Strategy(KansoStrategy):\n"
        "    config_cls = Config\n"
        "    def __init__(self, config=None):\n"
        "        super().__init__(config)\n"
        "        self.closes = []\n"
        "    def on_start(self):\n"
        "        self.bought = False\n"
        "    def on_order_filled(self, event):\n"
        "        self._signal = None\n"
        "    def __repr__(self):\n"
        "        return 'mine'\n"
        "    def _enter(self):\n"
        "        return self.submit_entry(self.universe[0], 'BUY')\n"
    )
    modifier = (
        f"{MODIFIER_HEAD}"
        "    construct = 'filter'\n"
        "    config_cls = None\n"
        "    def evaluate(self, ctx):\n"
        "        self._close = 1\n"
        "    def on_data(self, ctx):\n"
        "        pass\n"
    )

    assert scan(sleeve) == []
    assert scan(modifier) == []


def test_a_modifier_is_held_to_the_names_its_own_base_owns() -> None:
    (problem,) = scan(f"{MODIFIER_HEAD}    def on_start(self):\n        self._cfg = 1\n")

    assert problem.startswith("line 4: '_cfg' belongs to KansoModifier")


def test_a_class_that_is_not_the_harness_s_binds_what_it_likes() -> None:
    source = (
        "class Leg:\n"
        "    def __init__(self):\n"
        "        self._close = 0.0\n"
        "    def _fund(self):\n"
        "        pass\n"
        f"{SLEEVE_HEAD}"
        "    def on_start(self):\n"
        "        self.leg = Leg()\n"
        "        self.leg._close = 1.0\n"
    )

    assert scan(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "from kanso.nautilus.strategy import KansoStrategy as Base\n"
        "class Strategy(Base):\n    _close = 1\n",
        "import kanso.nautilus.strategy as ks\nclass Strategy(ks.KansoStrategy):\n    _close = 1\n",
        "from kanso.nautilus.strategy import KansoStrategy\n"
        "class Common(KansoStrategy):\n    pass\n"
        "class Strategy(Common):\n    _close = 1\n",
        "from kanso.nautilus.strategy import KansoStrategy\n"
        "class Mixin:\n    _close = 1\n"
        "class Strategy(Mixin, KansoStrategy):\n    pass\n",
    ],
    ids=["alias", "module attribute", "a base of its own", "a mixin"],
)
def test_a_class_reaching_the_base_by_any_route_is_held_to_it(source: str) -> None:
    (problem,) = scan(source)

    assert "'_close' belongs to KansoStrategy" in problem


@pytest.mark.parametrize(
    ("declared", "line"),
    [
        ('bar_resolution: str = "5m"', 4),
        ("bar_resolution: str | None", 4),
        ('bar_resolution = "5m"', 4),
        ('bar_resolution, every = "5m", 6', 4),
        ('[every, bar_resolution] = [6, "5m"]', 4),
        ('*every, bar_resolution = 6, "5m"', 4),
        ('if True:\n        bar_resolution: str = "5m"', 5),
        ('for bar_resolution in ("5m",):\n        pass', 4),
        ('try:\n        bar_resolution = "5m"\n    except ValueError:\n        pass', 5),
        ('every = (bar_resolution := "5m")', 4),
        ('@property\n    def bar_resolution(self) -> str:\n        return "5m"', 5),
        ("from math import pi as bar_resolution", 4),
        ('match "5m":\n        case bar_resolution:\n            pass', 5),
        ('match {"k": 5}:\n        case {**bar_resolution}:\n            pass', 5),
    ],
    ids=[
        "annotated",
        "annotation alone",
        "assigned",
        "tuple assign",
        "list assign",
        "starred",
        "nested if",
        "for target",
        "nested try",
        "walrus",
        "property",
        "import",
        "match capture",
        "match mapping rest",
    ],
)
def test_a_config_declaring_the_bar_size_kanso_sets_is_refused(declared: str, line: int) -> None:
    """A version's manifest carries `bar_resolution` only where the hypothesis states one, so a
    Config's own default would set a stage's bar size and not a card's, and a binding that
    hides the field would set both: a binding anywhere in the class's own scope is refused."""
    source = (
        "from kanso.nautilus.strategy import KansoConfig as Base, KansoStrategy\n"
        "class Config(Base):\n"
        "    lookback: int = 60\n"
        f"    {declared}\n"
        "class Strategy(KansoStrategy):\n"
        "    config_cls = Config\n"
    )

    assert scan(source) == [
        f"line {line}: 'bar_resolution' is a field kanso sets on KansoConfig from the "
        "hypothesis, and a Config may not bind it: a value bound there would stand in for the "
        "file's on a stage, or on every path where it hides the field; rename yours"
    ]


def test_a_config_may_declare_fields_of_its_own_and_another_class_any_name() -> None:
    """A function's, a lambda's, a nested class's and a comprehension's names bind in a scope
    of their own, never on the Config."""
    source = (
        "from kanso.nautilus.strategy import KansoConfig, KansoStrategy\n"
        "class Leg:\n"
        "    bar_resolution = '5m'\n"
        "class Config(KansoConfig):\n"
        "    lookback: int = 60\n"
        "    fixed_params: tuple[str, ...] = ('lookback',)\n"
        "    sizes = [bar_resolution for bar_resolution in ('5m',)]\n"
        "    pick = lambda self, bar_resolution='5m': bar_resolution\n"
        "    class Inner:\n"
        "        bar_resolution = '5m'\n"
        "    def size(self) -> str:\n"
        "        bar_resolution = '5m'\n"
        "        return bar_resolution\n"
        "class Strategy(KansoStrategy):\n"
        "    config_cls = Config\n"
    )

    assert scan(source) == []


def test_a_file_that_does_not_parse_has_no_clash_to_name_and_scan_says_why() -> None:
    source = f"{SLEEVE_HEAD}    def on_start(self:\n        self._close = 3\n"

    assert clashes(source) == []
    (problem,) = scan(source)
    assert "does not parse" in problem


def test_a_bare_annotation_binds_nothing_and_a_static_helper_has_no_self() -> None:
    source = (
        f"{SLEEVE_HEAD}    _close: int\n"
        "    @staticmethod\n    def helper(leg):\n        leg._close = 1\n"
        "    @staticmethod\n    def nothing():\n        pass\n"
    )

    assert scan(source) == []


def test_the_owned_names_are_read_from_the_installed_class(monkeypatch) -> None:
    """Nobody lists them: a name the harness gains is owned the moment it exists."""
    instance = KansoStrategy()
    assert set(vars(instance)) <= owned(KansoStrategy)
    assert {"_close", "_fund", "_refund", "_last_price", "submit_exit"} <= owned(KansoStrategy)
    assert "_close" not in owned(KansoModifier)

    monkeypatch.setattr(KansoStrategy, "_brand_new", lambda self: None, raising=False)
    owned.cache_clear()
    try:
        (problem,) = scan(f"{SLEEVE_HEAD}    def on_start(self):\n        self._brand_new = 1\n")
    finally:
        monkeypatch.undo()
        owned.cache_clear()
    assert "'_brand_new' belongs to KansoStrategy" in problem


def test_the_harness_keeps_its_own_machinery_off_the_names_an_author_writes() -> None:
    """The engine's `on_*` handlers are the author's because the harness hooks the engine
    through `handle_*` and names of its own; a handler it defined itself would be one an
    author could silently replace."""
    for base in (KansoStrategy, KansoModifier):
        defined = {name for name in vars(base) if name.startswith("on_")}
        assert defined <= AUTHORED, defined
        assert not (owned(base) & AUTHORED)


def test_the_program_template_names_what_the_rule_refuses_and_what_it_leaves() -> None:
    """`program.md` tells a proposer which names are the base's by example; each must be one
    the scan refuses, and the name it offers instead one the scan leaves alone."""
    program = (PACKAGE_ROOT / "templates" / "program.md").read_text(encoding="utf-8")
    (bullet,) = (line for line in program.splitlines() if "Names the base class owns" in line)
    examples = re.findall(r"`(\w+)`", bullet.split("underscored or not — ")[1].split(" among")[0])

    assert len(examples) >= 5
    assert set(examples) <= owned(KansoStrategy)
    assert "`self._close_col`, not `self._close`" in bullet
    assert "_close_col" not in owned(KansoStrategy)


# --- the harness's own sends, and the methods it overrides --------------------------


def test_the_harness_s_own_sends_are_read_from_the_installed_class() -> None:
    """Nobody lists them: every private method of the base that hands the engine an order
    itself is one, every private method that calls one is one, and no public verb is."""
    sent = sends(KansoStrategy)
    direct = {
        name
        for name, value in vars(KansoStrategy).items()
        if name.startswith("_")
        and not name.startswith("__")
        and inspect.isfunction(value)
        and re.search(
            r"(self|super\(\))\.(submit_order|submit_order_list|modify_order)\(",
            inspect.getsource(value),
        )
    }

    assert direct and direct <= sent
    assert {"_submitted", "_booked", "_close", "_sized_entry", "_sized_exit"} <= sent
    assert {"_place_hedges", "_submit_leg", "_submit_own"} <= sent
    assert sent <= owned(KansoStrategy)
    assert not sent & {"submit_entry", "submit_exit", "submit_order", "_pay_owed"}
    assert sends(KansoModifier) == frozenset()


@pytest.mark.parametrize("name", sorted(sends(KansoStrategy) | SEND_MARKS))
def test_a_sleeve_reaching_the_harness_s_own_send_is_refused_sized_or_not(name: str) -> None:
    """Called by a strategy, a send hands the engine whatever it is given as an order kanso
    sized, which a book lets through as kanso's own; the mark and the ledger are what the
    book recognises kanso's order by."""
    source = f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        x = self.{name}\n"

    for sized in (False, True):
        (problem,) = (p for p in scan(source, sized=sized) if f"'.{name}'" in p)
        assert problem.startswith(f"line 4: attribute '.{name}' is denied, because it is the ")
        assert "submit_entry(instrument_id, side) or submit_exit(instrument_id)" in problem


@pytest.mark.parametrize(
    "source",
    [
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        KansoStrategy._submitted(self, bar)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        me = self\n        me._close(bar)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        super()._booked(bar)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        go(self, bar)\n"
        "def go(strategy, order):\n    strategy._submitted(order)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        (lambda: self._submitted(bar))()\n",
    ],
    ids=["the base's class", "a second name", "super()", "a module function", "a lambda"],
)
def test_the_harness_s_own_send_is_refused_off_any_receiver_but_a_class_of_its_own(
    source: str,
) -> None:
    (problem,) = scan(source)

    assert "is denied, because it is the harness's own send" in problem


def test_a_class_that_is_not_the_harness_s_reads_its_own_names() -> None:
    """A name kanso sends by is the author's own on a class the base does not hold, a
    modifier included, whose base sends nothing."""
    source = (
        "class Leg:\n"
        "    def __init__(self):\n"
        "        self._close = 0.0\n"
        "    def price(self):\n"
        "        return self._close + self._submitted\n"
        f"{MODIFIER_HEAD}"
        "    def on_start(self):\n"
        "        self._close = 1.0\n"
        "    def evaluate(self, ctx):\n"
        "        return self._close\n"
    )

    assert scan(source) == []


def test_the_methods_the_harness_overrides_are_read_from_the_installed_class() -> None:
    names = overridden(KansoStrategy)

    assert {"submit_order", "submit_order_list", "modify_order", "cancel_order"} <= names
    assert {"cancel_orders", "cancel_all_orders", "handle_event", "handle_bar"} <= names
    assert names == {
        name for name in vars(KansoStrategy) if hasattr(Strategy, name) and name[:2] != "__"
    }
    assert {"subscribe_order_book_deltas", "_stop"} <= overridden(KansoModifier)
    assert not (overridden(KansoModifier) | names) & AUTHORED


@pytest.mark.parametrize(
    "source",
    [
        "from nautilus_trader.trading.strategy import Strategy as Engine\n"
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        Engine.submit_order(self, bar)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n"
        "        super(KansoStrategy, self).submit_order(bar)\n",
        "import nautilus_trader.trading.strategy as engine\n"
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n"
        "        engine.Strategy.submit_order_list(self, bar)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n"
        "        type(self).mro()[2].modify_order(self, bar)\n",
        f"{SLEEVE_HEAD}    def on_bar(self, bar):\n        go(self, bar)\n"
        "def go(strategy, order):\n    strategy.cancel_order(order)\n",
        f"{SLEEVE_HEAD}    @classmethod\n    def send(cls, me, order):\n"
        "        cls.submit_order(me, order)\n",
    ],
    ids=[
        "the engine's class",
        "a two-argument super",
        "the engine's module",
        "the class's lineage",
        "a module function",
        "a classmethod",
    ],
)
def test_a_method_the_harness_overrides_reached_past_it_is_refused(source: str) -> None:
    """Read off anything but the instance or `super()`, `submit_order` can be the engine's
    own, which records no intent, sizes nothing and, in a book, is past the book's check."""
    (problem,) = scan(source)

    assert re.match(r"line \d+: '\w+' is read off '.+', where it can be the engine's own", problem)
    assert "call self." in problem


def test_a_method_the_harness_overrides_is_read_off_the_instance_or_super() -> None:
    source = (
        f"{SLEEVE_HEAD}    def on_bar(this, bar):\n"
        "        this.submit_order(bar)\n"
        "        super().submit_order(bar)\n"
        "        (lambda: this.cancel_order(bar))()\n"
    )

    assert scan(source) == []


def test_every_name_the_engine_gives_the_bus_is_refused_to_every_strategy() -> None:
    """Read off the engine rather than off the set, so a name it adds is refused the day it
    is added: on the bus a strategy hands the execution engine a command or a fill past its
    own methods and a book's check, whatever its hypothesis declares. Measured on
    nautilus_trader 1.231.0: `msgbus` and `_msgbus`, on `Strategy` and `Actor` alike."""
    from nautilus_trader.common.actor import Actor

    routes = {name for cls in (Strategy, Actor) for name in dir(cls) if "msgbus" in name}

    assert routes >= {"msgbus", "_msgbus"}
    for name in sorted(routes):
        for sized in (False, True):
            for depth in (False, True):
                (problem,) = scan(f"x = self.{name}", sized=sized, depth=depth)
                assert f"attribute '.{name}' is denied, because it is the engine's message bus" in (
                    problem
                )


def test_an_event_applied_by_hand_is_refused_on_any_receiver() -> None:
    """`Order.apply` and `Position.apply` take an event as if the engine had applied it, and
    which receiver is an order the scan cannot tell."""
    from nautilus_trader.model.orders import Order
    from nautilus_trader.model.position import Position

    assert callable(Order.apply) and callable(Position.apply)
    for source in ("order.apply(event)", "self.rest.apply(update)", "held[0].apply(fill)"):
        (problem,) = scan(source)
        assert "attribute '.apply' is denied, because it applies an event" in problem


ROUTE_HEAD = (
    "from nautilus_trader.core.uuid import UUID4\n"
    "from nautilus_trader.model.events import OrderFilled, OrderUpdated\n"
    "from nautilus_trader.model.objects import Price, Quantity\n"
    "from nautilus_trader.trading.strategy import Strategy as Engine\n"
    "from kanso.nautilus.strategy import KansoStrategy\n"
    "class Strategy(KansoStrategy):\n"
    "    def on_bar(self, bar):\n"
)


@pytest.mark.parametrize(
    ("body", "refused"),
    [
        (
            '        self.msgbus.send(endpoint="ExecEngine.execute", msg=submit)\n',
            "'.msgbus' is denied",
        ),
        (
            '        self.msgbus.send(endpoint="RiskEngine.execute", msg=submit)\n',
            "'.msgbus' is denied",
        ),
        (
            '        self._msgbus.send(endpoint="RiskEngine.execute", msg=modify)\n',
            "'._msgbus' is denied",
        ),
        (
            '        self.msgbus.send(endpoint="ExecEngine.execute", msg=in_another_name)\n',
            "'.msgbus' is denied",
        ),
        (
            '        for held in self.msgbus.subscriptions("events.order.*"):\n'
            "            self.msgbus.unsubscribe(topic=held.topic, handler=held.handler)\n",
            "'.msgbus' is denied",
        ),
        (
            '        self.msgbus.send(endpoint="ExecEngine.process", msg=OrderFilled(*fill))\n',
            "'.msgbus' is denied",
        ),
        (
            "        rest = self.submit_exit(bar.bar_type.instrument_id)\n"
            "        rest.apply(OrderUpdated(*update))\n",
            "'.apply' is denied",
        ),
    ],
    ids=[
        "a submit to the execution engine",
        "a submit to the risk engine",
        "a modify on the bus",
        "a submit in another strategy's name",
        "the book's handler unsubscribed",
        "a fill of the strategy's own making",
        "an event applied to kanso's own exit",
    ],
)
@pytest.mark.parametrize("sized", [False, True])
def test_every_route_past_the_strategy_s_own_methods_is_refused_sized_or_not(
    body: str, refused: str, sized: bool
) -> None:
    """Each sent an order, a modify or a fill to the venue in a book at no latency and at
    20 ms, where the book did not refuse it, and alone (`docs/backlog.md` rows 209 and 210);
    the scan refuses each by the name it is spelt with, whatever the hypothesis declares."""
    for depth in (False, True):
        problems = scan(ROUTE_HEAD + body, sized=sized, depth=depth)
        assert any(refused in problem for problem in problems), problems


ROW_211 = "docs/backlog.md row 211: a route the scan does not follow yet"
"""Why each case below is an expected failure: a route the scan does not see yet. Each is strict,
so the day the scan refuses one it passes, the suite fails, and the marker comes off."""


@pytest.mark.xfail(strict=True, reason=ROW_211)
@pytest.mark.parametrize(
    "source",
    [
        "from typing import operator",
        "from dataclasses import inspect",
        "from nautilus_trader.model.data import pickle",
        "from kanso.nautilus.strategy import actions",
        "from nautilus_trader.model.book import itemgetter",
        "from nautilus_trader.trading.config import resolve_path",
        "import typing\ncall = typing.operator.methodcaller('_submitted', order)",
        "import nautilus_trader.model\nsubmit = nautilus_trader.execution.messages.SubmitOrder",
        "import nautilus_trader.model\nbindings = nautilus_trader.core.rust",
        "import nautilus_trader.model\ncore = nautilus_trader.core",
        "from nautilus_trader import model\nthaw = model.data.pickle.loads",
        "import numpy as np\nopened = np.lib._datasource._open('strategy.py')",
        "import typing\nheld = typing\nreached = held.operator",
        "import nautilus_trader.model\nroot = nautilus_trader\nreached = root.execution",
    ],
    ids=[
        "a module an allowed leaf holds",
        "the inspect module through dataclasses",
        "pickle through the engine's model",
        "a kanso module through the strategy base",
        "a function an engine leaf takes from the standard library",
        "the engine's importer through its trading config",
        "a module read off an allowed one",
        "the package an engine leaf's import binds",
        "the Rust bindings through the package root",
        "a package root read off the import",
        "pickle through a leaf imported from its package",
        "the builtin open through numpy",
        "an allowed module read under another name",
        "the engine's package root read under another name",
    ],
)
def test_what_an_allowed_import_hands_out_is_refused_when_it_is_out_of_reach(source: str) -> None:
    """The allow-list matches a path, and an allowed leaf can hold what is not allowed — a
    module, or a class or a function defined in one — while `import a.b` binds the package
    `a`, whose other submodules are not allowed. Each of these passes the scan today
    (`docs/backlog.md` row 211); resolving them without importing anything is a follow-up."""
    assert scan(source) != []


@pytest.mark.xfail(strict=True, reason=ROW_211)
@pytest.mark.parametrize(
    "source",
    [
        "import numpy as np\nopened = np.lib.npyio.DataSource(None).open('strategy.py')",
        "import numpy as np\nmapped = np.lib.format.open_memmap('window.npy')",
        "import numpy as np\nlibrary = np.ctypeslib.load_library('libc', '/usr/lib')",
    ],
    ids=["a path or a URL opened", "a file mapped", "a shared library loaded"],
)
def test_numpy_s_other_file_functions_are_refused(source: str) -> None:
    """numpy is allowed whole, and `DENIED_NUMPY_FILE` names only some of what in it reaches a
    file; these pass the scan today (`docs/backlog.md` row 211)."""
    assert scan(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from nautilus_trader.model.enums import OrderSide",
        "from kanso.nautilus.strategy import Decision, Hedge",
        "from nautilus_trader.core.datetime import millis_to_nanos",
        "import nautilus_trader.model.data\nkind = nautilus_trader.model.data.Bar",
        "import kanso.nautilus.strategy\nbase = kanso.nautilus.strategy.KansoStrategy",
        "import numpy as np\ndrawn = np.random.default_rng(1).normal()",
        "import numpy\nreal = numpy.float64(1.0).real",
        "from nautilus_trader.model import data\ndef on_data(data):\n    return data",
        "import math\nroot = math.sqrt(2.0) * math.pi",
    ],
    ids=[
        "an enum the engine's model takes from its Rust bindings",
        "what the strategy base takes from kanso's hooks",
        "a function the engine compiled",
        "a dotted read through the package root to a leaf",
        "a dotted read to kanso's strategy base",
        "a dotted read through numpy",
        "data read off what a module holds",
        "a module's name bound again as a parameter",
        "an ordinary dotted read",
    ],
)
def test_what_an_allowed_leaf_hands_out_of_its_own_package_passes(source: str) -> None:
    """The engine's and kanso's allowed leaves hand out what they define elsewhere in their
    own package by design — the engine's enums come from its Rust bindings, `Decision` from
    kanso's hooks — and a name the file binds again is the file's own: whatever closes row
    211 must still pass each of these."""
    assert scan(source) == []


@pytest.mark.parametrize(
    "source", ["import numpy.f2py.__main__", "from numpy.f2py import __main__"]
)
def test_the_scan_runs_no_module_a_strategy_names(
    source: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`numpy.f2py.__main__` is allowed, as all of numpy is, and importing it runs f2py's
    command line on the process's own arguments and raises `SystemExit`. A scan that imported
    what a strategy names to judge it ran that in the process gating the card: the card
    command's arguments were read as f2py's, the command exited with no card recorded, and a
    lane would have died. The scan reads the source alone, so it returns, prints nothing and
    leaves the module unimported; the import is the card's own, in its child process."""
    monkeypatch.setattr(sys, "argv", ["kanso", "research", "card", "demo_mr", "--desc", "x"])
    imported = "numpy.f2py.__main__" in sys.modules
    body = "\nfrom kanso.nautilus.strategy import KansoStrategy\nclass Strategy(KansoStrategy):\n"
    try:
        problems = scan(source + body + "    pass\n")
    except BaseException as error:  # SystemExit is what the import raised
        pytest.fail(f"the scan raised {type(error).__name__}: {error!r}")

    assert isinstance(problems, list)
    assert capsys.readouterr() == ("", "")
    assert ("numpy.f2py.__main__" in sys.modules) is imported
