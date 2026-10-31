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
comes from. A path names a module, not what the module holds, and the path is all this rule
reads: what an allowed module holds from elsewhere, and what a dotted read off an imported
module reaches, are not resolved (*What the scan does not see*, below). The scan imports no
module because a strategy names it — its only import is kanso's own strategy base — so a
module's import-time code runs in the card's child process, never in the process that gates
it.

**Identifiers** are refused wherever they appear — as a name, as an attribute or as an
import alias — so that a denied capability cannot be reached by a second route. That
covers the builtins that open files, evaluate strings or reach attributes by name; the
modules that reach the filesystem, the process or the network; the introspection dunders
that walk from any object to any other — to an attribute by a name held in a string, to an
instance's attributes all at once, to the builtins module — and the frame attributes that
walk up into the harness's own frames; the Rust bridge names; numpy's file functions; and
the component clock with its timer API, since a strategy reads data time and wall-clock
logic cannot survive replay parity. The builtins are refused as names only: `Bar.open` and
the other OHLC attributes are ordinary data, and the ban is on the builtin, not the word.
A denied attribute named in a string's format field is refused as well, since
`"{0.cache}".format(self)` reads it by that name.

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
`depth` so are the three subscriptions that would hand the author a book of its own: every
change, snapshots on an interval, and the ten-level depth an account on a sampled channel is
not served. They bind an attached construct too, which is an engine actor the harness hands
no view.

**The bus and the events are the engine's**, sized or not, with `depth` or without. On the
message bus, under both the names the engine gives it (`msgbus`, `_msgbus`), a strategy can
send the execution engine a command — an order, a modify — or a fill, past its own methods
and a book's check, and take a book's handler off; its data engine topics carry every change
of a book as well. `apply` hands an order or a position an event as if the engine had applied
it, and an order moved that way at the venue sends no modify; which receiver is an order the
scan cannot tell, so the word is denied on any. What these routes do is measured where the
sets are declared (`DENIED_BUS`, `DENIED_EVENTS`).

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

**The harness's sends are its own.** A sleeve hands the engine an order through the base's
`submit_order`, `submit_order_list` and `modify_order`, which record the intent, ask the
limits, size and refuse, and which a book reads to refuse what kanso did not send
(`kanso.nautilus.book`). Two kinds of read reach past them, and both are refused, sized or
not. The base's private helpers that send an order as the harness's own — `_submitted`,
`_booked`, `_close` and every private method of kanso's classes that calls `submit_order`,
`submit_order_list` or `modify_order` on the instance or on `super()`, or calls one that does
(`sends`) — and the mark and the ledger a book recognises kanso's order by (`_sending`,
`_book_ledger`) are refused when read off anything but the instance of a method of a class
the base does not hold, where the name is that class's own: called by a strategy, such a
helper sends whatever it is handed as an order kanso sized. A method the engine defines and
the base overrides — `submit_order`, `modify_order`, `cancel_order`, the `handle_*` handlers
and the rest (`overridden`) — is refused when read off anything but `self`, the instance a
method receives, or `super()` with no arguments: off the engine's class
(`Strategy.submit_order(self, order)`) or a two-argument `super(KansoStrategy, self)` it is
the engine's own method, which skips the base's. Both sets are read from the installed
classes when the scan runs, never kept as a list. The instance read through another name —
handed to a module-level function, or kept as `me = self` — is refused as well, which is the
price of seeing the rest.

**What the scan does not see** is a spelling it does not follow, and each of these reaches
what the rules above refuse when it is spelt directly (`docs/backlog.md` row 211):

* the engine's class held under the name `self` — the first parameter of a module-level
  function handed `Strategy` — whose `submit_order` is the engine's own, which in a book the
  book still refuses on the bus (`kanso.nautilus.book`) and a sleeve alone sends;
* a module an allowed leaf holds, which its path passes: `from typing import operator` is the
  `operator` module, `from dataclasses import inspect` the `inspect` module, `from
  nautilus_trader.model.data import pickle` the `pickle` module, and `from
  kanso.nautilus.strategy import actions` kanso's `actions`;
* a dotted read off an imported module, which is not followed: `import nautilus_trader.model`
  binds the package `nautilus_trader`, so `nautilus_trader.execution` and
  `nautilus_trader.core.rust` read packages the allow-list refuses to import, which the
  engine has imported by the time a strategy runs; `from nautilus_trader import model`
  reaches `pickle` as `model.data.pickle`; and a module kept under another name is read there
  unfollowed as well;
* a class or a function an allowed leaf takes from a module that is not allowed: the engine's
  `resolve_path`, which `nautilus_trader.trading.config` takes from
  `nautilus_trader.common.config` and which imports whatever path it is handed as a string;
  `itemgetter`, which `nautilus_trader.model.book` takes from `operator`; and
  `numpy.lib._datasource._open`, which is the builtin `open`;
* numpy, allowed whole, past its file functions (`DENIED_NUMPY_FILE`): the `open` of
  `numpy.lib.npyio.DataSource` opens a path, or downloads a URL and opens that,
  `numpy.lib.format.open_memmap` maps a file, and `numpy.ctypeslib.load_library` loads a
  shared library.

**Scope**: the lane directory holds exactly `hypothesis.yaml`, `program.md` and
`strategy.py`, and the first two still equal the blobs the run pinned. Transient artefacts
— dot-files and `__pycache__` — are ignored, because the interpreter writes them and the
researcher did not.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterable, Iterator, Mapping
from functools import cache
from hashlib import sha256
from pathlib import Path
from string import Formatter
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
    {
        "__subclasses__",
        "__globals__",
        "__code__",
        "__class__",
        "__mro__",
        "__bases__",
        "__dict__",
        "__getattribute__",
        "__getstate__",
        "__reduce__",
        "__reduce_ex__",
        "__builtins__",
        "__self__",
        "__traceback__",
    }
)
"""The dunders that walk from one object to another: to a class and its relatives, to a
function's globals, to an attribute by a name held as a string (`__getattribute__`, which
`object` and every instance carry), to an instance's attributes all at once (`__getstate__`
and the two reduce hooks hand back its `__dict__`), to the builtins themselves
(`__builtins__`, and a builtin function's `__self__`, which is the builtins module), and to a
frame (`__traceback__`)."""
DENIED_FRAMES: Final = frozenset(
    {
        "gi_frame",
        "cr_frame",
        "ag_frame",
        "tb_frame",
        "f_back",
        "f_globals",
        "f_locals",
        "f_builtins",
    }
)
"""A generator's, a coroutine's or a traceback's frame, and what a frame reaches: the frames
that called it, the harness's among them, with their locals, and the module's globals."""
DENIED_BRIDGE: Final = frozenset({"nautilus_pyo3", "capsule_to_list"})
DENIED_NUMPY_FILE: Final = frozenset(
    {"load", "save", "savez", "fromfile", "tofile", "loadtxt", "genfromtxt", "savetxt", "memmap"}
)
DENIED_CLOCK: Final = frozenset(
    {
        "clock",
        "_clock",
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
"""The component clock and its timer API, reachable only as an attribute — the clock under
both the names the engine gives it, `clock` and `_clock` (nautilus_trader 1.231.0). It reads
the instant the engine is at, which in a backtest is the point being matched and under
`depth` every change of the book, where the author is handed `data_time`."""

DENIED_SCHEDULE: Final = frozenset({"cache", "request_instrument", "request_instruments"})
"""The routes by which a `strategy.py` can hold an instrument, and so its `info.splits`:
every split of that instrument's life, which is the certification window and beyond — the
cache, and the engine's requests for an instrument, which the data engine answers."""

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
        "request_aggregated_bars",
        "request_quote_ticks",
        "request_trade_ticks",
        "request_order_book_snapshot",
        "request_order_book_deltas",
        "request_order_book_depth",
        "request_funding_rates",
        "request_data",
        "request_join",
        "_trading_from_ns",
        "_delivered_ns",
        "_fed_from_ns",
        "_warming",
        "_undelivered",
    }
)
"""The engine's history requests — every `request_*` an engine actor holds but the two for an
instrument (`DENIED_SCHEDULE`), the book's among them, which under `depth` would hand the
author a book the account is not served — and the harness's warmup gate: the three attributes it
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
        "_top_marked",
        "_take_depth",
        "_show_view",
        "_show_book",
        "_show_top",
        "_show_depth",
    }
)
"""The harness's own copy of a book a `depth` hypothesis holds, at every change, and the
methods that show the author its view of it."""

DENIED_BUS: Final = frozenset({"msgbus", "_msgbus"})
"""The engine's message bus, under both the names every `Strategy` and `Actor` holds it by
(nautilus_trader 1.231.0). Measured through the backtest runner in a book at no latency and
at 20 ms, and alone at no latency: a `SubmitOrder` or a `ModifyOrder` sent on it to
`ExecEngine.execute` or `RiskEngine.execute` reached the venue past the strategy's own
methods, and a book did not refuse it; an `OrderFilled` sent to `ExecEngine.process` was
recorded as a fill at a price no venue printed; and unsubscribing the bus's `events.order.*`
handlers took the book's check away. Its data engine topics carry every change of a book as
well."""

DENIED_EVENTS: Final = frozenset({"apply"})
"""`Order.apply` and `Position.apply`, which take an event — an `OrderUpdated`, an
`OrderFilled` — as if the engine had applied it. Measured on nautilus_trader 1.231.0
through the backtest runner, in a book at no latency and at 20 ms and alone at no latency:
an `OrderUpdated` to 5,000 at 5.00 that a strategy applied to its own resting sale of 1,000
at 100 sold 5,000 at 5.00, with no modify sent. Every event a strategy's order takes is the
engine's to apply, and which receiver is an order the scan cannot tell, so the word is denied
on any receiver."""

WHY_BUS: Final = (
    "it is the engine's message bus, which hands the execution engine a command or a fill "
    "past the strategy's own methods and a book's check; place orders through "
    "submit_entry(instrument_id, side) and submit_exit(instrument_id), and read data in the "
    "on_* handlers"
)

WHY_EVENTS: Final = (
    "it applies an event to an order or a position — a fill, a modify — as if the engine had, "
    "past the venue and kanso's own methods; the engine applies every event itself, so send "
    "orders through submit_entry(instrument_id, side) and submit_exit(instrument_id), and "
    "take one back with cancel_order"
)

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
    **dict.fromkeys(DENIED_BUS, WHY_BUS),
    **dict.fromkeys(DENIED_EVENTS, WHY_EVENTS),
}
"""Why each denial that carries a reason exists, said in the refusal so a proposer can act on it."""

DENIED_IDENTIFIERS: Final = DENIED_MODULES | DENIED_DUNDERS | DENIED_BRIDGE | DENIED_NUMPY_FILE
"""Refused as a name, an attribute or an import alias alike."""

DENIED_ATTRIBUTES: Final = (
    DENIED_IDENTIFIERS
    | DENIED_FRAMES
    | DENIED_CLOCK
    | DENIED_SCHEDULE
    | DENIED_STALE_BASIS
    | DENIED_HISTORY
    | DENIED_BOOK
    | DENIED_DEPTH
    | DENIED_BUS
    | DENIED_EVENTS
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
    {
        "subscribe_order_book_deltas",
        "subscribe_order_book_at_interval",
        "subscribe_order_book_depth",
    }
)
"""The subscriptions that hand a book at every change, or as snapshots the account is not
served, around the view the harness hands; denied only when the hypothesis declares
`depth`. The bus, which carries every change on the data engine's topics, is denied to
every strategy (`DENIED_BUS`). The harness subscribes a sleeve's book itself, so
`subscribe_order_book_deltas` is a second subscription only an attached construct, which
the harness does not gate, would be handed every change through."""

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

SEND_MARKS: Final = frozenset({"_sending", "_book_ledger"})
"""What a book lets kanso's own order through by: the mark on the order kanso is sending, and
the book's ledger; denied beside the base's private sends (`sends`)."""

ENGINE_SENDS: Final = frozenset({"submit_order", "submit_order_list", "modify_order"})
"""The engine's methods that hand it an order or move one; a private method of kanso's that
calls one on the instance or on `super()` is a send (`sends`)."""

WHY_SEND: Final = (
    "it is the harness's own send, which hands the engine whatever it is given as an order "
    "kanso sized, past the limits, the room and a book's check; call "
    "submit_entry(instrument_id, side) or submit_exit(instrument_id), and give a name of "
    "your own another spelling"
)

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
        *_receiver_problems(tree),
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
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            problems.extend(_format_problems(node.value, node.lineno, depth=depth))
    return sorted(set(problems), key=problems.index)


def _format_fields(text: str) -> Iterator[str]:
    """Every replacement field `str.format` would read in `text`, nested specs included."""
    try:
        parsed = list(Formatter().parse(text))
    except ValueError:
        return
    for _, field, spec, _ in parsed:
        if field:
            yield field
        if spec:
            yield from _format_fields(spec)


_FIELD_STEP: Final = re.compile(r"\.([^.\[]*)|\[([^\]]*)\]")


def _field_attributes(field: str) -> list[str]:
    """The attributes a format field's name reads, in the grammar `str.format` parses it by:
    an argument, then `.name` and `[key]` steps; a key is an item, never an attribute."""
    position = len(re.split(r"[.\[]", field, maxsplit=1)[0])
    names: list[str] = []
    while position < len(field):
        step = _FIELD_STEP.match(field, position)
        if step is None:
            break
        if step.group(1) is not None:
            names.append(step.group(1))
        position = step.end()
    return names


def _format_problems(text: str, line: int, *, depth: bool) -> Iterable[str]:
    """A denied attribute named in a format field, which `str.format` reaches by its name.

    `"{0.cache}".format(self)` is the attribute read the scan refuses when spelled as one,
    with the name held in a string; any string can be formatted, so every literal is read.
    """
    for field in _format_fields(text):
        for name in _field_attributes(field):
            if name in DENIED_ATTRIBUTES:
                why = WHY.get(name)
            elif depth and name in DENIED_UNDER_DEPTH:
                why = WHY_DEPTH
            else:
                continue
            yield (
                f"line {line}: attribute '.{name}' is denied, and naming it in the format "
                f"field '{{{field}}}' reaches it all the same"
                + ("" if why is None else f"; it is denied because {why}")
            )


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


def _bases() -> tuple[type, ...]:
    """The harness bases a researched file's classes reach, imported when first asked for."""
    from kanso.nautilus.strategy import KansoModifier, KansoStrategy

    return (KansoStrategy, KansoModifier)


def _kanso_classes(base: type) -> list[type]:
    return [klass for klass in base.__mro__ if klass.__module__.partition(".")[0] == "kanso"]


@cache
def sends(base: type) -> frozenset[str]:
    """Every private method of kanso's classes in `base`'s MRO that hands the engine an order:
    one whose body calls an `ENGINE_SENDS` method on `self` or on `super()`, or calls another
    such private method there; read from the installed classes' source. A public method —
    `submit_entry`, `submit_exit` and the engine's own — is not one: it asks what it is meant
    to ask before it sends.
    """
    calls: dict[str, set[str]] = {}
    for klass in _kanso_classes(base):
        for node in ast.parse(inspect.getsource(klass)).body[0].body:  # type: ignore[attr-defined]
            if (
                isinstance(node, ast.FunctionDef)
                and node.name.startswith("_")
                and not node.name.startswith("__")
            ):
                calls.setdefault(node.name, set()).update(_calls_on_self(node))
    found = {name for name, called in calls.items() if called & ENGINE_SENDS}
    while more := {name for name, called in calls.items() if called & found} - found:
        found |= more
    return frozenset(found)


def _calls_on_self(node: ast.FunctionDef) -> Iterator[str]:
    """The methods a function calls on `self` or on `super()`."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and (_is_name(child.func.value, "self") or _is_bare_super(child.func.value))
        ):
            yield child.func.attr


@cache
def overridden(base: type) -> frozenset[str]:
    """Every method the engine defines that a kanso class in `base`'s MRO defines again, read
    from the installed classes, less what the base leaves its author (`AUTHORED`, the
    dunders and the `on_*` handlers)."""
    engine = [klass for klass in base.__mro__ if klass.__module__.partition(".")[0] != "kanso"]
    return frozenset(
        name
        for klass in _kanso_classes(base)
        for name in vars(klass)
        if any(name in vars(beneath) for beneath in engine)
        and name not in AUTHORED
        and not name.startswith("on_")
        and not (name.startswith("__") and name.endswith("__"))
    )


def _is_name(node: ast.expr, name: str | None) -> bool:
    return name is not None and isinstance(node, ast.Name) and node.id == name


def _is_bare_super(node: ast.expr) -> bool:
    """Whether `node` is `super()` with no arguments, which resolves past the class the
    method is written in and so reaches the harness base's own methods."""
    return (
        isinstance(node, ast.Call)
        and _is_name(node.func, "super")
        and not node.args
        and not node.keywords
    )


def _receiver_problems(tree: ast.Module) -> list[str]:
    """Every read of the harness's own sends, and of a method it overrides, off a receiver
    that can reach past the harness base.

    A send (`sends`, `SEND_MARKS`) is refused when read off anything but the instance of a
    method of a class that is not held to the base that owns it, where the name is that
    class's own; a binding of one is the clash rule's (`_clashes`). An overridden method
    (`overridden`) is refused when read off anything but `self`, the instance a method
    receives, or `super()` with no arguments.
    """
    holding: dict[int, set[type]] = {}
    for node, base in _harness_classes(tree):
        holding.setdefault(id(node), set()).add(base)
    sent = {base: sends(base) | SEND_MARKS for base in _bases()}
    names = frozenset().union(*(overridden(base) for base in _bases()))
    found: list[str] = []

    def visit(
        node: ast.AST, scope: tuple[str | None, frozenset[type]], held: set[type] | None
    ) -> None:
        """`scope` is the instance in reach and the bases its class is held to; `held` the
        bases of the class whose body `node` is directly in, `None` outside one."""
        instance, owners = scope
        if isinstance(node, ast.ClassDef):
            held = holding.get(id(node), set())
            for child in ast.iter_child_nodes(node):
                visit(child, (None, frozenset()), held)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            positional = [*node.args.posonlyargs, *node.args.args]
            if held is not None and positional and not _static(node) and not _classwide(node):
                scope = (positional[0].arg, frozenset(held))
            for child in ast.iter_child_nodes(node):
                visit(child, scope, None)
            return
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            own = _is_name(node.value, instance) or _is_name(node.value, "self")
            if node.attr in names and not (own or _is_bare_super(node.value)):
                found.append(
                    f"line {node.lineno}: '{node.attr}' is read off "
                    f"'{ast.unparse(node.value)}', where it can be the engine's own "
                    f"{node.attr} past the one the harness base defines, which skips what the "
                    f"harness does there; call self.{node.attr}(...), or "
                    f"super().{node.attr}(...) with no arguments"
                )
            mine = _is_name(node.value, instance)
            for base, held_names in sent.items():
                if node.attr in held_names and not (mine and base not in owners):
                    found.append(
                        f"line {node.lineno}: attribute '.{node.attr}' is denied, because "
                        f"{WHY_SEND}"
                    )
                    break
        for child in ast.iter_child_nodes(node):
            visit(child, scope, held)

    visit(tree, (None, frozenset()), None)
    return found


def _classwide(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the method is a `classmethod`, whose first parameter is the class."""
    return any(
        isinstance(decorator, ast.Name) and decorator.id == "classmethod"
        for decorator in node.decorator_list
    )


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
    harness = {base.__name__: base for base in _bases()}
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
