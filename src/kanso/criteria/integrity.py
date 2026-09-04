"""The anti-cheat boundary: what a researched `strategy.py` may import, name and touch.

This is a guardrail against the loop's own proposer, not a sandbox against a hostile
actor. The loop rewards a number, and the cheapest ways to move that number are not
research: reading the certification window off the catalog, reading a file the run did not
pin, reaching wall-clock time so a replay cannot reproduce the fills, or quietly editing
something other than `strategy.py`. Each of those is denied here, statically, before
anything runs — a card that fails this check is discarded with no backtest at all, so code
that violates the embargo never executes.

Three rules, in the order they are checked:

**Imports** are matched by their full dotted path against an allow-list of exact leaves,
never package roots. The engine's model, trading and indicator packages are open; of its
core only the pure leaves are, because the rest of `nautilus_trader.core` reaches the
Rust bridge, and the bridge, the persistence layer and the backtest package each reach the
data catalog and therefore the certification window. `kanso.nautilus.strategy` is the only
kanso module a strategy may see. Beyond those, numpy and a short standard-library set of
pure computation. A star import binds names nobody declared, so it is refused whatever it
comes from.

**Identifiers** are refused wherever they appear — as a name, as an attribute or as an
import alias — so that a denied capability cannot be reached by a second route. That
covers the builtins that open files, evaluate strings or reach attributes by name; the
modules that reach the filesystem, the process or the network; the introspection dunders
that walk from any object to any other; the Rust bridge names; numpy's file functions; and
the component clock with its timer API, since a strategy reads data time and wall-clock
logic cannot survive replay parity. The builtins are refused as names only: `Bar.open` and
the other OHLC attributes are ordinary data, and the ban is on the builtin, not the word.

**Scope**: the lane directory holds exactly `hypothesis.yaml`, `program.md` and
`strategy.py`, and the first two still equal the blobs the run pinned. Transient artefacts
— dot-files and `__pycache__` — are ignored, because the interpreter writes them and the
researcher did not.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Final

ALLOWED_IMPORTS: Final = frozenset(
    {
        "nautilus_trader.model",
        "nautilus_trader.trading",
        "nautilus_trader.indicators",
        "nautilus_trader.core.datetime",
        "nautilus_trader.core.uuid",
        "nautilus_trader.core.message",
        "nautilus_trader.core.data",
        "nautilus_trader.core.math",
        "nautilus_trader.core.stats",
        "nautilus_trader.core.correctness",
        "nautilus_trader.core.fsm",
        "kanso.nautilus.strategy",
        "numpy",
        "math",
        "statistics",
        "collections",
        "dataclasses",
        "typing",
        "decimal",
        "datetime",
    }
)
"""Exact leaves. A path is allowed when it is one of these or lies under one."""

NAMED_DENIALS: Final = {
    "nautilus_trader.core.nautilus_pyo3": "the Rust bridge reaches the data catalog",
    "nautilus_trader.core.rust": "the Rust bindings reach the data catalog",
    "nautilus_trader.persistence": "the persistence layer is the data catalog",
    "nautilus_trader.backtest": "the backtest package reaches the certification window",
}
"""Denied paths worth naming in the message; the allow-list already refuses them."""

DENIED_BUILTINS: Final = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "vars",
        "globals",
        "locals",
        "input",
        "breakpoint",
    }
)
"""Refused as a name or an alias; an attribute of the same spelling is ordinary data."""

DENIED_MODULES: Final = frozenset(
    {"os", "sys", "subprocess", "socket", "pathlib", "importlib", "builtins", "ctypes", "time"}
)
DENIED_DUNDERS: Final = frozenset(
    {"__subclasses__", "__globals__", "__code__", "__class__", "__mro__", "__bases__", "__dict__"}
)
DENIED_BRIDGE: Final = frozenset({"nautilus_pyo3", "capsule_to_list"})
DENIED_NUMPY_FILE: Final = frozenset(
    {"load", "save", "savez", "fromfile", "tofile", "loadtxt", "genfromtxt", "savetxt", "memmap"}
)
DENIED_CLOCK: Final = frozenset(
    {
        "clock",
        "set_timer",
        "set_timer_ns",
        "set_time_alert",
        "set_time_alert_ns",
        "cancel_timer",
        "cancel_timers",
        "timestamp",
        "timestamp_ns",
        "timestamp_ms",
        "timestamp_us",
        "utc_now",
        "local_now",
    }
)
"""The component clock and its timer API, reachable only as an attribute."""

DENIED_IDENTIFIERS: Final = DENIED_MODULES | DENIED_DUNDERS | DENIED_BRIDGE | DENIED_NUMPY_FILE
"""Refused as a name, an attribute or an import alias alike."""

DENIED_ATTRIBUTES: Final = DENIED_IDENTIFIERS | DENIED_CLOCK

SCOPED_FILES: Final = ("hypothesis.yaml", "program.md", "strategy.py")
"""Exactly what a lane directory holds."""

PINNED_FILES: Final = ("hypothesis.yaml", "program.md")
"""The scoped files a run pins as blobs; a card may not change either."""

IGNORED_ENTRIES: Final = frozenset({"__pycache__"})
"""Transient artefacts the interpreter writes, which the researcher did not."""

STRATEGY = "strategy.py"


def import_allowed(path: str) -> bool:
    """Whether a full dotted import path lies at or under an allowed leaf."""
    return any(path == leaf or path.startswith(f"{leaf}.") for leaf in ALLOWED_IMPORTS)


def _import_problem(path: str, line: int) -> str | None:
    if import_allowed(path):
        return None
    for denied, reason in NAMED_DENIALS.items():
        if path == denied or path.startswith(f"{denied}."):
            return f"line {line}: import of '{path}' is denied — {reason}"
    return f"line {line}: import of '{path}' is not on the allow-list"


def _identifier_problem(name: str, line: int, what: str) -> str | None:
    if name in DENIED_IDENTIFIERS or name in DENIED_BUILTINS:
        return f"line {line}: '{name}' is a denied identifier, used as {what}"
    return None


def _visit_import(node: ast.Import) -> Iterable[str]:
    for alias in node.names:
        problem = _import_problem(alias.name, node.lineno)
        if problem is not None:
            yield problem
        if alias.asname is not None:
            named = _identifier_problem(alias.asname, node.lineno, "an import alias")
            if named is not None:
                yield named


def _visit_import_from(node: ast.ImportFrom) -> Iterable[str]:
    if node.level:
        yield f"line {node.lineno}: a relative import has no allow-listed path"
        return
    module = node.module or ""
    for alias in node.names:
        if alias.name == "*":
            yield f"line {node.lineno}: 'from {module} import *' binds undeclared names"
            continue
        problem = _import_problem(f"{module}.{alias.name}", node.lineno)
        if problem is not None:
            yield problem
        for used, what in ((alias.name, "an imported name"), (alias.asname, "an import alias")):
            if used is not None:
                named = _identifier_problem(used, node.lineno, what)
                if named is not None:
                    yield named


def scan(source: str, origin: str = STRATEGY) -> list[str]:
    """Every import, identifier and attribute rule this source breaks, in file order."""
    try:
        tree = ast.parse(source, filename=origin)
    except SyntaxError as exc:
        return [f"{origin}: does not parse: {exc.msg} at line {exc.lineno}"]
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            problems.extend(_visit_import(node))
        elif isinstance(node, ast.ImportFrom):
            problems.extend(_visit_import_from(node))
        elif isinstance(node, ast.Name):
            problem = _identifier_problem(node.id, node.lineno, "a name")
            if problem is not None:
                problems.append(problem)
        elif isinstance(node, ast.Attribute) and node.attr in DENIED_ATTRIBUTES:
            problems.append(f"line {node.lineno}: attribute '.{node.attr}' is denied")
    return sorted(set(problems), key=problems.index)


def scope(lane_dir: Path, pinned: Mapping[str, str]) -> list[str]:
    """Every way this lane directory departs from the three scoped files and their pins."""
    problems: list[str] = []
    try:
        entries = sorted(
            child.name
            for child in lane_dir.iterdir()
            if not child.name.startswith(".") and child.name not in IGNORED_ENTRIES
        )
    except OSError as exc:
        return [f"the lane directory cannot be read: {exc.strerror}"]
    for extra in (name for name in entries if name not in SCOPED_FILES):
        problems.append(f"'{extra}' is not one of the three scoped files")
    for missing in (name for name in SCOPED_FILES if name not in entries):
        problems.append(f"'{missing}' is missing from the lane directory")
    for name in PINNED_FILES:
        expected = pinned.get(name)
        path = lane_dir / name
        if expected is None or not path.is_file():
            continue
        if sha256(path.read_bytes()).hexdigest() != expected:
            problems.append(f"'{name}' no longer equals the blob this run pinned")
    return problems


def check(lane_dir: Path, pinned: Mapping[str, str]) -> list[str]:
    """The whole static half: the directory's scope, then the strategy's own source."""
    problems = scope(lane_dir, pinned)
    source = lane_dir / STRATEGY
    if not source.is_file():
        return problems
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [*problems, f"{STRATEGY} cannot be read as text: {exc}"]
    return [*problems, *scan(text)]
