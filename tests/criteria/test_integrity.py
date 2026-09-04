"""The anti-cheat boundary: every denial the toolbox states, and everything it must allow."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from kanso.criteria.integrity import (
    DENIED_BUILTINS,
    DENIED_CLOCK,
    DENIED_DUNDERS,
    DENIED_MODULES,
    DENIED_NUMPY_FILE,
    check,
    import_allowed,
    scan,
    scope,
)
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
