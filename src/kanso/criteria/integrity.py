"""The anti-cheat boundary: what a researched `strategy.py` may import, name and touch.

This is a guardrail against the loop's own proposer, not a sandbox against a hostile
actor. The loop rewards a number, and the cheapest ways to move that number are not
research: reading the certification window off the catalog, reading a file the run did not
pin, reaching wall-clock time so a replay cannot reproduce the fills, or quietly editing
something other than `strategy.py`. Each of those is denied here, statically, before
anything runs — a card that fails this check is discarded with no backtest at all, so code
that violates the embargo never executes.

Four rules, in the order they are checked:

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

Two attribute sets are refused for reasons that are about corporate actions rather than
about capability, and both carry the reason in the refusal, because a discarded card whose
author is a model is worth nothing unless the model is told what it did.

**The cache.** An instrument's `info` carries every split of its life, including ones dated
after the window a card is judged on (`kanso.nautilus.splits`), and `cache.instrument(id)`
is the only route by which a `strategy.py` can hold an instrument at all — a subscription
delivers none in a backtest, and `HookContext` carries prices rather than definitions. So
the cache is denied and the schedule is out of reach with it. The word `info` is *not*
denied: `self.log.info(...)` is the engine's own logging call, and refusing it would refuse
the most ordinary line a strategy writes.

**Anything the engine derives from a position's opening basis.** `Position.avg_px_open` is
`cdef readonly` and `apply_adjustment` does not rescale it, so after a split it is a price
per share that no longer exists; `peak_qty` is a count of shares that no longer exist; and
`realized_pnl`, `realized_return`, the portfolio's P&L views and every account balance
credited from them are computed against those two. Measured on a one-for-ten reverse split
under kanso's own venue: a position that lost 50 reported a profit of 9,000, and a $100,000
account read $109,000 from the closing fill onwards. Those are reachable through
`self.portfolio` and through a position event, which is why they are named rather than left
to the cache's denial. kanso's own extraction reads none of them, computing a trade from
its fills and the split ledger instead; a researched strategy may not read them either.

**Under a sizing rule**, a fourth set. When the hypothesis declares `sizing`, the harness
sizes every order and a proposal that names a size is refused before any backtest rather
than capped after one: the `notional`, `qty` and `price` keywords of `submit_entry` and
`submit_exit` (and a `**` that could carry them), the attributes that build, change or
cancel an order by hand — `submit_order`, `order_factory`, `close_position`, `modify_order`
and their kin — and `portfolio`, whose net position counts an attached overlay's clips as
the host's; `self.held(id)` is the reader. A sized overlay may name `Clip` and not
`Hedge`, `hedges=` or `scale=`; an overlay without a budget may name `Hedge` and not `Clip`
or `clips=`. Every denial says why and what to write instead, because the proposer is a
model and a refusal it cannot act on is a loop.

**Under `depth`**, a fifth set. The harness keeps every change of the book for the venue
and hands the author the book on the hypothesis's grid and level one on every change, so
the harness's own copy and the methods that show it are denied to every strategy, and under
`depth` so are the message bus, which carries every change on the data engine's topics, and
the two subscriptions that would hand the author a book of its own: snapshots on an
interval, and the ten-level depth an account on a sampled channel is not served.

**The base's names** belong to the base. A class whose instances are a `KansoStrategy` or a
`KansoModifier` — it names one among its bases, through an alias, a module or a class of the
file that does, or it is a class of the file such a class names — may not bind a name that
base owns: not by `def`, not by assigning it in the class body, and not by assigning it on
the instance a method receives (`self._close = 3`). Python raises nothing for any of them;
the harness simply reaches the author's value where it expects its own, and the card crashes
at the first exit, or sizes, gates or books differently, with a traceback that names the
harness rather than the line. Under a sizing rule an override is also a size knob no
attribute scan can see. What the base owns is read from the installed classes when the scan
runs, never kept as a list: everything `dir` shows of it, the engine's names beneath
included, and every attribute kanso's own classes set on `self`. What it leaves to its
author is `AUTHORED`, the dunders and the engine's `on_*` handlers.

**Scope**: the lane directory holds exactly `hypothesis.yaml`, `program.md` and
`strategy.py`, and the first two still equal the blobs the run pinned. Transient artefacts
— dot-files and `__pycache__` — are ignored, because the interpreter writes them and the
researcher did not.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable, Iterator, Mapping
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Final, NamedTuple

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

DENIED_SCHEDULE: Final = frozenset({"cache"})
"""The one route by which a `strategy.py` can hold an instrument, and so its `info.splits`:
every split of that instrument's life, which is the certification window and beyond."""

DENIED_STALE_BASIS: Final = frozenset(
    {
        "account",
        "account_for_venue",
        "accounts",
        "analyzer",
        "avg_px_close",
        "avg_px_open",
        "equity",
        "peak_qty",
        "realized_pnl",
        "realized_pnls",
        "realized_return",
        "total_pnl",
        "total_pnls",
        "unrealized_pnl",
        "unrealized_pnls",
    }
)
"""Every quantity the engine computes against a position's opening basis, which a split
leaves in the share count the position opened in and which nothing can rewrite."""

DENIED_HISTORY: Final = frozenset(
    {
        "request_bars",
        "request_quote_ticks",
        "request_trade_ticks",
        "request_data",
        "_trading_from_ns",
        "_delivered_ns",
        "_fed_from_ns",
        "_warming",
        "_undelivered",
    }
)
"""The engine's history requests, and the harness's warmup gate: the three attributes it
reads and the two methods that combine them. History reaches a strategy only as the prefix
its hypothesis declares, delivered like the window's own points; a request would be a
second route to the catalog, and the gate's own state — or its answer — a clock that says
where the window opens, which is the counter `program.md` tells a proposer not to keep."""

DENIED_BOOK: Final = frozenset(
    {
        "_policy",
        "_anchor_ns",
        "_period_ns",
        "_cushion",
        "_settled_ns",
        "_carried_from_ns",
        "_period_index",
        "_period_last_ns",
        "_turn",
        "_close_period",
        "_book_at",
    }
)
"""The harness's book policy: the state it settles each return period with, and the three
methods that settle it. The anchor and the period index are a clock that says where the
window opens and how far into it a card is, and the cushion is what the book has made
before this month; a strategy reads the book the policy left through `balance`, which is
the number the card is struck on, and nothing else of it."""

DENIED_DEPTH: Final = frozenset(
    {
        "_depth_books",
        "_depth_shown",
        "_depth_seen_ns",
        "_top_shown",
        "_top_due",
        "_take_depth",
        "_show_view",
        "_show_book",
        "_show_top",
        "_show_depth",
    }
)
"""The harness's own copy of a book a `depth` hypothesis holds, at every change, and the
methods that show the author its view of it."""

WHY_DEPTH: Final = (
    "the account sees a book's depth only on the grid `depth.every_ms` sets and level one on "
    "every change, which the harness hands to on_order_book_deltas and on_quote_tick; read "
    "them there"
)

WHY: Final = {
    **dict.fromkeys(
        DENIED_SCHEDULE,
        "it hands out the instrument whose `info.splits` names every split of its life, "
        "including ones after the window this card is judged on",
    ),
    **dict.fromkeys(
        DENIED_STALE_BASIS,
        "the engine computes it against a position's opening basis, which a corporate "
        "action leaves in a share count that no longer exists; size from `last_price`, "
        "`held` and `balance` instead; kanso keeps the last two from the sleeve's own fills",
    ),
    **dict.fromkeys(
        DENIED_HISTORY,
        "history reaches a strategy only as the `warmup` sessions its hypothesis declares, "
        "fed before the window with every order dropped; declare `warmup: {sessions: N}` "
        "in hypothesis.yaml instead of asking the engine for it",
    ),
    **dict.fromkeys(
        DENIED_BOOK,
        "it is the harness's own book-policy state, a clock of where the window opens and "
        "what the book set aside; read the book the policy left from `balance`",
    ),
    **dict.fromkeys(DENIED_DEPTH, f"it is the harness's book at every change, and {WHY_DEPTH}"),
}
"""Why each denial that carries a reason exists, said in the refusal so a proposer can act on it."""

DENIED_IDENTIFIERS: Final = DENIED_MODULES | DENIED_DUNDERS | DENIED_BRIDGE | DENIED_NUMPY_FILE
"""Refused as a name, an attribute or an import alias alike."""

DENIED_ATTRIBUTES: Final = (
    DENIED_IDENTIFIERS
    | DENIED_CLOCK
    | DENIED_SCHEDULE
    | DENIED_STALE_BASIS
    | DENIED_HISTORY
    | DENIED_BOOK
    | DENIED_DEPTH
)

SIZE_HELPERS: Final = frozenset({"submit_entry", "submit_exit"})
"""The two verbs a sized strategy places orders with, which take no size."""

SIZE_KEYWORDS: Final = frozenset({"notional", "qty", "price"})
"""The keywords of those verbs that name a size or a price, denied under a sizing rule."""

DENIED_UNDER_SIZING: Final = frozenset(
    {
        "submit_order",
        "submit_order_list",
        "order_factory",
        "close_position",
        "close_all_positions",
        "modify_order",
        "cancel_order",
        "cancel_orders",
        "cancel_all_orders",
        "portfolio",
    }
)
"""Attributes that build, change or cancel an order by hand, or read a net that is not the
sleeve's own; denied only when the hypothesis declares `sizing`."""

WHY_UNDER_SIZING: Final = "because the harness sizes every order to the budget"

DENIED_UNDER_DEPTH: Final = frozenset(
    {"msgbus", "subscribe_order_book_at_interval", "subscribe_order_book_depth"}
)
"""What reaches a book at every change, or as snapshots the account is not served, around
the view the harness hands; denied only when the hypothesis declares `depth`. The bus
carries the data engine's own topics, every change of the book among them."""

OVERLAY_CONSTRUCT: Final = "overlay"
SIZED_OVERLAY_NAMES: Final = frozenset({"Hedge"})
SIZED_OVERLAY_KEYWORDS: Final = frozenset({"hedges", "scale"})
FREE_OVERLAY_NAMES: Final = frozenset({"Clip"})
FREE_OVERLAY_KEYWORDS: Final = frozenset({"clips"})
OWN_HOOKS: Final = frozenset({"evaluate", "on_data"})
"""The modifier hooks an author writes; every other harness name is the harness's."""

AUTHORED: Final = frozenset({"config_cls", "construct", *OWN_HOOKS})
"""What a base class leaves its subclass to bind: the config class kanso builds it with,
the construct a modifier is, and a modifier's two hooks. The dunders and the engine's
`on_*` handlers are the author's too, because the harness hooks the engine through
`handle_*` and names of its own."""

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


def scan(
    source: str,
    origin: str = STRATEGY,
    *,
    sized: bool = False,
    construct: str | None = None,
    depth: bool = False,
) -> list[str]:
    """Every import, identifier and attribute rule this source breaks, in file order.

    `sized` says the hypothesis declares a sizing rule and `construct` which construct the
    source is, so the rules of the sizing rule apply to the right vocabulary; `depth` says
    it declares `depth`, so what reaches the book around the harness's view is refused.
    """
    try:
        tree = ast.parse(source, filename=origin)
    except SyntaxError as exc:
        return [f"{origin}: does not parse: {exc.msg} at line {exc.lineno}"]
    problems: list[str] = [
        *_sizing_problems(tree, sized=sized, construct=construct),
        *(clash.problem for clash in _clashes(tree)),
    ]
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
            why = WHY.get(node.attr)
            problems.append(
                f"line {node.lineno}: attribute '.{node.attr}' is denied"
                + ("" if why is None else f", because {why}")
            )
        elif isinstance(node, ast.Attribute) and depth and node.attr in DENIED_UNDER_DEPTH:
            problems.append(
                f"line {node.lineno}: attribute '.{node.attr}' is denied under depth, "
                f"because {WHY_DEPTH}"
            )
    return sorted(set(problems), key=problems.index)


def _sizing_problems(tree: ast.AST, *, sized: bool, construct: str | None) -> Iterable[str]:
    """The rules a sizing rule adds, for the construct this source is."""
    overlay = construct == OVERLAY_CONSTRUCT
    sleeve = construct in (None, "sleeve")
    if overlay:
        names, keywords, why = (
            (
                SIZED_OVERLAY_NAMES,
                SIZED_OVERLAY_KEYWORDS,
                "a sized overlay names clips, and a "
                "clip carries no quantity; return Decision(clips=(Clip(instrument, side),))",
            )
            if sized
            else (
                FREE_OVERLAY_NAMES,
                FREE_OVERLAY_KEYWORDS,
                "an overlay without a budget has "
                "nothing to size a clip to; return Decision(hedges=(Hedge(instrument, qty),)), "
                "or declare `sizing` on its hypothesis",
            )
        )
        yield from _vocabulary_problems(tree, names, keywords, why)
    if not sized:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and sleeve and node.attr in DENIED_UNDER_SIZING:
            yield f"line {node.lineno}: attribute '.{node.attr}' is denied under sizing, " + (
                "because the net position counts an attached overlay's clips as the host's; "
                "read self.held(instrument_id)"
                if node.attr == "portfolio"
                else f"{WHY_UNDER_SIZING} and every order is the harness's; call "
                "submit_entry(instrument_id, side) or submit_exit(instrument_id)"
            )
        elif isinstance(node, ast.Call) and sleeve and _names_helper(node.func):
            for keyword in node.keywords:
                if keyword.arg is None:
                    yield (
                        f"line {node.lineno}: '**' on {_names_helper(node.func)} is denied under "
                        f"sizing, {WHY_UNDER_SIZING}; pass the instrument and the side alone"
                    )
                elif keyword.arg in SIZE_KEYWORDS:
                    yield (
                        f"line {node.lineno}: keyword '{keyword.arg}=' is denied under sizing, "
                        f"{WHY_UNDER_SIZING} and places it at market; call "
                        f"{_names_helper(node.func)}(instrument_id"
                        + (", side)" if _names_helper(node.func) == "submit_entry" else ")")
                    )


def _vocabulary_problems(
    tree: ast.AST, names: frozenset[str], keywords: frozenset[str], why: str
) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            yield f"line {node.lineno}: name '{node.id}' is denied for this overlay, because {why}"
        elif isinstance(node, ast.Attribute) and node.attr in names:
            yield (
                f"line {node.lineno}: name '{node.attr}' is denied for this overlay, because {why}"
            )
        elif isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.name in names or alias.asname in names:
                    yield (
                        f"line {node.lineno}: name '{alias.asname or alias.name}' is denied for "
                        f"this overlay, because {why}"
                    )
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in keywords:
                    yield (
                        f"line {node.lineno}: keyword '{keyword.arg}=' is denied for this "
                        f"overlay, because {why}"
                    )


def _names_helper(func: ast.expr) -> str | None:
    """`submit_entry` or `submit_exit` when the call is one of them, else `None`."""
    if isinstance(func, ast.Attribute) and func.attr in SIZE_HELPERS:
        return func.attr
    if isinstance(func, ast.Name) and func.id in SIZE_HELPERS:
        return func.id
    return None


class Clash(NamedTuple):
    """One binding of a name a harness base owns, where it is and how it was made."""

    line: int
    name: str
    base: str
    how: str

    @property
    def problem(self) -> str:
        """The refusal, as the proposer reads it."""
        return (
            f"line {self.line}: '{self.name}' belongs to {self.base}, and {self.how} replaces "
            "it, so the harness would reach yours where it expects its own; rename yours — "
            f"{self.base} owns every name it defines or sets, underscored or not"
        )


def clashes(source: str) -> list[Clash]:
    """Every binding in `source` of a name its harness base owns, in file order.

    A file that does not parse binds nothing this can find; `scan` is what reports it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return _clashes(tree)


def _clashes(tree: ast.Module) -> list[Clash]:
    found = [
        Clash(line, name, base.__name__, how)
        for node, base in _harness_classes(tree)
        for line, name, how in _bindings(node)
        if name in owned(base)
    ]
    return sorted(set(found), key=lambda clash: (clash.line, found.index(clash)))


@cache
def owned(base: type) -> frozenset[str]:
    """Every name `base` owns, read from the installed class rather than kept as a list.

    What `dir` shows of it — its methods, properties and class attributes, and every name
    the engine's classes beneath it expose — and every attribute a kanso class of its MRO
    sets on `self`, read from that class's source, because an instance attribute is in no
    class's `dir`. Less what the base leaves its author: `AUTHORED`, the dunders and the
    engine's `on_*` handlers.
    """
    names = set(dir(base))
    for klass in base.__mro__:
        if klass.__module__.partition(".")[0] == "kanso":
            tree = ast.parse(inspect.getsource(klass))
            names.update(store.attr for store in _stores(tree, "self"))
    return frozenset(
        name
        for name in names
        if name not in AUTHORED
        and not name.startswith("on_")
        and not (name.startswith("__") and name.endswith("__"))
    )


def _stores(node: ast.AST, owner: str) -> Iterator[ast.Attribute]:
    """Every attribute bound on the object `owner` names, anywhere under `node`."""
    return (
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.ctx, ast.Store)
        and isinstance(child.value, ast.Name)
        and child.value.id == owner
    )


def _harness_classes(tree: ast.Module) -> list[tuple[ast.ClassDef, type]]:
    """Every class of the file whose methods end up on a harness base's subclass, with that base.

    A class reaches a base by naming it among its bases — as itself, through an import
    alias or as a module's attribute — or by naming a class of the file that reaches one;
    every class of the file in that line, a mixin included, is held to the same base.
    """
    from kanso.nautilus.strategy import KansoModifier, KansoStrategy

    harness = {base.__name__: base for base in (KansoStrategy, KansoModifier)}
    aliases = {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname is not None
    }
    local = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def named(node: ast.ClassDef) -> list[str]:
        return [
            aliases.get(expr.id, expr.id) if isinstance(expr, ast.Name) else expr.attr
            for expr in node.bases
            if isinstance(expr, ast.Name | ast.Attribute)
        ]

    def lineage(name: str, seen: frozenset[str]) -> set[str]:
        found = {name}
        for base in named(local[name]):
            if base in local and base not in seen:
                found |= lineage(base, seen | {name})
        return found

    held: dict[str, set[type]] = {name: set() for name in local}
    for name in local:
        members = lineage(name, frozenset())
        bases = {
            harness[base] for member in members for base in named(local[member]) if base in harness
        }
        for member in members:
            held[member] |= bases
    return [
        (node, base)
        for name, node in local.items()
        for base in sorted(held[name], key=lambda base: base.__name__)
    ]


def _bindings(node: ast.ClassDef) -> Iterator[tuple[int, str, str]]:
    """`(line, name, how)` for every name the class binds on itself or on its instances."""
    for statement in node.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            yield statement.lineno, statement.name, f"'def {statement.name}'"
            positional = [*statement.args.posonlyargs, *statement.args.args]
            if positional and not _static(statement):
                owner = positional[0].arg
                for store in _stores(statement, owner):
                    yield store.lineno, store.attr, f"binding '{owner}.{store.attr}'"
            continue
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AugAssign) or (
            isinstance(statement, ast.AnnAssign) and statement.value is not None
        ):
            targets = [statement.target]
        else:
            continue
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    yield name.lineno, name.id, f"binding '{name.id}' on the class"


def _static(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the method is a `staticmethod`, whose first parameter is not the instance."""
    return any(
        isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
        for decorator in node.decorator_list
    )


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


def check(
    lane_dir: Path,
    pinned: Mapping[str, str],
    *,
    sized: bool = False,
    construct: str | None = None,
    depth: bool = False,
) -> list[str]:
    """The whole static half: the directory's scope, then the strategy's own source."""
    problems = scope(lane_dir, pinned)
    source = lane_dir / STRATEGY
    if not source.is_file():
        return problems
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [*problems, f"{STRATEGY} cannot be read as text: {exc}"]
    return [*problems, *scan(text, sized=sized, construct=construct, depth=depth)]
