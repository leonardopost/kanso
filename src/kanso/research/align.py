"""Alignment, deterministic half: does this `strategy.py` still test the stated idea?

A research loop optimises a number, and the cheapest way to move a number is often to
stop testing the hypothesis — to trade an instrument that is not in the universe, to
subscribe to a finer bar than the one the idea is about, or to read a data type the
hypothesis never asked for. None of that is cheating in the `strategy_integrity` sense:
the code is well-behaved, it simply answers a different question. So it is checked
separately, and checked here first, because three of the ways to drift are visible in the
source's own syntax tree and need no model at all:

* **universe** — an instrument literal naming something outside `universe`;
* **resolution** — a bar specification whose step or aggregation differs from
  `resolution`, whether it is spelled as a `BarSpecification` call or as a bar-type
  string;
* **data types** — a subscription or a handler for a type outside `data_requirements`.

Each is a syntactic fact, so a violation is stated rather than judged: this half never
returns "probably". Only a source that passes it is worth a model's opinion, which is the
other half of the check and costs a call. A literal the checks cannot read — a symbol
built at runtime, a step read from the config — is not evidence of drift and is passed
over: this is a detector of stated intent, not a proof of its absence.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Final

from kanso.schemas import Hypothesis, is_duration

__all__ = ["BAR_TYPE_PATTERN", "HANDLERS", "REASON_LIMIT", "align_static", "problems"]

REASON_LIMIT: Final = 200
"""What `align_check` allows a reason to be, so both halves report the same shape."""

AGGREGATION: Final[dict[str, str]] = {
    "s": "SECOND",
    "m": "MINUTE",
    "h": "HOUR",
    "d": "DAY",
    "w": "WEEK",
}
"""The engine's bar aggregation for each duration unit, as the strategy API spells it."""

BAR_TYPE_PATTERN: Final = (
    r"^(?P<instrument>[A-Za-z0-9._/:]+)-(?P<step>\d+)-(?P<aggregation>[A-Z_]+)"
    r"-(?P<price>[A-Z_]+)-(?P<source>INTERNAL|EXTERNAL)$"
)
"""A bar type written as a string, which is how the engine parses one."""

_BAR_TYPE: Final = re.compile(BAR_TYPE_PATTERN)

_INSTRUMENT: Final = re.compile(r"^[A-Z0-9][A-Z0-9._]*\.[A-Z][A-Z0-9_]*$")
"""An instrument literal: a qualified `SYMBOL.VENUE`, upper case as the engine writes it."""

_SPECIFICATION: Final = "BarSpecification"
_AGGREGATION_ENUM: Final = "BarAggregation"

HANDLERS: Final[dict[str, str]] = {
    "subscribe_bars": "bar",
    "request_bars": "bar",
    "on_bar": "bar",
    "handle_bar": "bar",
    "subscribe_quote_ticks": "quote",
    "request_quote_ticks": "quote",
    "on_quote_tick": "quote",
    "handle_quote_tick": "quote",
    "subscribe_trade_ticks": "trade",
    "request_trade_ticks": "trade",
    "on_trade_tick": "trade",
    "handle_trade_tick": "trade",
}
"""Every subscription and handler name that names one market data type outright."""


@dataclass(frozen=True)
class _Stated:
    """What the hypothesis states, in the shapes the syntax tree is compared against."""

    universe: tuple[str, ...]
    known: frozenset[str]
    resolution: str
    bar: tuple[int, str] | None
    required: frozenset[str]

    @classmethod
    def of(cls, hyp: Hypothesis) -> _Stated:
        names = set(hyp.universe)
        names.update(name.split(".")[0] for name in hyp.universe)
        bar = (
            (int(hyp.resolution[:-1]), AGGREGATION[hyp.resolution[-1]])
            if is_duration(hyp.resolution)
            else None
        )
        return cls(
            universe=tuple(sorted(hyp.universe)),
            known=frozenset(names),
            resolution=hyp.resolution,
            bar=bar,
            required=frozenset(hyp.data_requirements),
        )

    def instrument(self, name: str, line: int) -> str | None:
        """A literal instrument the universe does not hold."""
        if name in self.known:
            return None
        return f"line {line}: {name!r} is not in the universe ({', '.join(self.universe)})"

    def specification(self, step: int, aggregation: str, line: int) -> str | None:
        """A bar whose step or aggregation is not the one the resolution names."""
        if self.bar is None:
            return (
                f"line {line}: a {step}-{aggregation} bar, but the resolution is "
                f"{self.resolution!r}, which is not a bar size"
            )
        if (step, aggregation) == self.bar:
            return None
        return (
            f"line {line}: a {step}-{aggregation} bar, but the resolution is "
            f"{self.resolution!r} ({self.bar[0]}-{self.bar[1]})"
        )

    def data_type(self, name: str, line: int) -> str | None:
        """A subscription or handler for a type the hypothesis does not require."""
        wanted = HANDLERS.get(name)
        if wanted is None or wanted in self.required:
            return None
        return (
            f"line {line}: {name} reads {wanted} data, which is not required "
            f"({', '.join(sorted(self.required))})"
        )


def _constant(stated: _Stated, node: ast.Constant) -> list[str]:
    """What one string literal says about the universe and about the resolution."""
    if not isinstance(node.value, str):
        return []
    matched = _BAR_TYPE.match(node.value)
    if matched is not None:
        found = (
            stated.instrument(matched["instrument"], node.lineno),
            stated.specification(int(matched["step"]), matched["aggregation"], node.lineno),
        )
        return [problem for problem in found if problem is not None]
    if _INSTRUMENT.match(node.value):
        problem = stated.instrument(node.value, node.lineno)
        return [] if problem is None else [problem]
    return []


def _call(stated: _Stated, node: ast.Call) -> list[str]:
    """What a `BarSpecification(step, BarAggregation.UNIT, ...)` call says."""
    called = node.func
    name = (
        called.attr
        if isinstance(called, ast.Attribute)
        else called.id
        if isinstance(called, ast.Name)
        else None
    )
    if name != _SPECIFICATION or len(node.args) < 2:
        return []
    step, aggregation = node.args[0], node.args[1]
    if not isinstance(step, ast.Constant) or not isinstance(step.value, int):
        return []
    if not isinstance(aggregation, ast.Attribute):
        return []
    if not isinstance(aggregation.value, ast.Name) or aggregation.value.id != _AGGREGATION_ENUM:
        return []
    problem = stated.specification(step.value, aggregation.attr, node.lineno)
    return [] if problem is None else [problem]


def problems(hyp: Hypothesis, source: bytes) -> list[str]:
    """Every way this source departs from the hypothesis it claims to test, in file order."""
    try:
        tree = ast.parse(source.decode("utf-8", errors="replace"), filename="strategy.py")
    except SyntaxError as exc:
        return [f"strategy.py does not parse: {exc.msg} at line {exc.lineno}"]
    stated = _Stated.of(hyp)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            found.extend(_constant(stated, node))
        elif isinstance(node, ast.Call):
            found.extend(_call(stated, node))
        elif isinstance(node, ast.Attribute):
            problem = stated.data_type(node.attr, node.lineno)
            if problem is not None:
                found.append(problem)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            problem = stated.data_type(node.name, node.lineno)
            if problem is not None:
                found.append(problem)
    return sorted(set(found), key=found.index)


def align_static(hyp: Hypothesis, source: bytes) -> tuple[bool, str | None]:
    """Whether this source still tests `hyp`, and the reasons it does not.

    The reason is capped at the length the model half is allowed, so a caller records one
    shape whichever half produced it.
    """
    found = problems(hyp, source)
    if not found:
        return True, None
    reason = "; ".join(found)
    if len(reason) > REASON_LIMIT:
        reason = reason[: REASON_LIMIT - 1].rstrip() + "…"
    return False, reason
