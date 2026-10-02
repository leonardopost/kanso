"""Same-instant delivery: one book for a grain, then one author handler at a time.

The engine delivers by `ts_init` and, at a tie, keeps insertion order. A sleeve that
trades several instruments therefore used to handle the first name against a book the
later names had not yet moved, and a market submitted into the second filled at its
previous close. This module is the repair: after the existing stable `ts_init` sort,
consecutive points that share an availability instant *and* a grain are a cohort, and
every cohort of a held kind — a bar, a quote, a trade, a custom point — is followed by one
marker per point. The venue sees the whole cohort first; each marker then flushes one
buffered handler and the engine settles, so the next handler sees the fills.

**A book's changes of one instant are one batch.** The changes one instrument's book made
at one instant are delivered as one `OrderBookDeltas` (`batched`): the venue applies the
batch whole and matches once, the data engine publishes it whole after its book has taken
all of it, and the author's `on_order_book_deltas` is called once with every change of the
instant — never with a book that has lost its best ask and not yet been handed the next
one. A book handler is never held, so no marker follows a book cohort: the venue was
settled after the batch itself, and a marker after it would flush nothing.

**Whether a feed is marked is a property of the hypothesis** (`coincident`), so it cannot
depend on where a card's window was cut into chunks: a feed of several names, or one that
holds prints, quotes or a book, is marked in every chunk whether or not that chunk holds an
instant two points share. Any other stream is marked when some cohort in it holds two or
more points, and a stream of single-point cohorts that is not coincident is left unmarked —
direct `add_data` tests stay on the old dispatcher. A single-point cohort is dispatched the
same marked or not at zero latency; under a latency a command that came due at the point
lands before the author's handler for it when it is marked and after it when it is not,
which is why the choice cannot be left to the chunk.

Engine facts this module relies on (nautilus_trader 1.231.0): `DataEngine._handle_data`
publishes only `CustomData` among custom types and logs `unrecognized type` for a
bare `Data` subclass; `_handle_custom_data` publishes the inner `data.data` on the
custom-data topic, so a strategy's `handle_data` receives `KansoCrossSection` and
never the wrapper. `subscribe_data` binds that topic before it sends a `Subscribe`
command; on the replay path that command routes to the replay client, registered as
the node's default, whose subscribe is a no-op, and the topic is already bound.
`sort_data` is a stable sort on `ts_init`, so markers
inserted last at an instant stay last when the engine concatenates homogeneous
`add_data` runs. The name is `KansoCrossSection` because the serializable-type
registry is keyed by bare class name across the process. `OrderBookDeltas(instrument_id,
deltas)` takes its `ts_init`, `ts_event` and `flags` from its last delta; the backtest
engine hands it to `SimulatedExchange.process_order_book_deltas`, which applies every delta
and then matches once, and a data engine that does not buffer deltas publishes it whole on
the instrument's deltas topic, after the book updater it subscribed at priority 10 has
applied it (both re-checked by `kanso.nautilus.facts`).
"""

from collections.abc import Sequence
from itertools import chain
from typing import TYPE_CHECKING

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import (
    Bar,
    CustomData,
    DataType,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)

from kanso.nautilus.costs import BookPolicy

if TYPE_CHECKING:
    from kanso.schemas import Hypothesis

__all__ = [
    "KansoCrossSection",
    "MARKER_TYPE",
    "arm",
    "batched",
    "book",
    "coincident",
    "is_marker",
    "ordered",
    "warm",
    "without_markers",
    "with_cross_section",
]


@customdataclass
class KansoCrossSection(Data):  # type: ignore[misc]
    """The flush signal for one buffered handler of a coincident grain."""

    n: int


MARKER_TYPE = DataType(KansoCrossSection)

BAR = "bar"
QUOTE = "quote"
TRADE = "trade"
BOOK = "book"

TICK_KINDS = frozenset({QUOTE, TRADE, BOOK})
"""The data requirements whose points share instants as a rule rather than by accident."""


def coincident(hyp: "Hypothesis") -> bool:
    """Whether a hypothesis's feed is marked however its instants fall.

    A universe of more than one name, or a requirement of prints, quotes or a book: a feed
    where several points of one instant are the rule. Measured on two perpetual swaps over 82 days,
    no hour of either held no millisecond that two prints shared, and 88-89 % of prints
    shared theirs. Deciding it here rather than from the points is what keeps a card the
    same however its window was cut into chunks.
    """
    return len(hyp.universe) > 1 or bool(TICK_KINDS.intersection(hyp.data_requirements))


def is_marker(point: object) -> bool:
    """Whether this point is a cross-section flush, wrapped or already unwrapped."""
    return isinstance(_inner(point), KansoCrossSection)


def without_markers(points: Sequence[object]) -> tuple[object, ...]:
    """The catalog points of a stream: everything that is not a flush.

    A session records this, and a stage's `released` count is the length of a prefix
    of it, because a marker is a feed signal and not a clock tick.
    """
    return tuple(point for point in points if not is_marker(point))


def arm(strategy: object, points: Sequence[object]) -> None:
    """Hold author handlers until a marker flushes them, when this feed has markers.

    A strategy that never sees a marker keeps the engine's per-point dispatch, which
    is what a single-instrument card and a test that calls `add_data` directly are.
    """
    if any(is_marker(point) for point in points):
        strategy._hold_until_cross_section = True  # type: ignore[attr-defined]


def warm(strategy: object, opens_ns: int) -> None:
    """Drop every order the strategy submits before the feed reaches `opens_ns`.

    Set by the runner exactly as `arm` is — a private attribute the harness reads, never
    a configuration field an author could read back or a manifest would record. The
    harness keys the gate on the availability instant of the point it is handling, which
    is the window's own order, so an in-window point published after midnight is never
    refused for having a reference time before it. Everything else runs: handlers, the
    last-price readers, an overlay's own clock. What is dropped is dropped silently, as a
    refused filter is, so both code paths record the same intents.
    """
    strategy._trading_from_ns = opens_ns  # type: ignore[attr-defined]


def book(
    strategy: object,
    policy: BookPolicy,
    anchor_ns: int,
    period_ns: int,
    cushion: float = 0.0,
    settled_ns: int | None = None,
    carried_from_ns: int | None = None,
) -> None:
    """Have the harness apply a book policy on the periods the runner's extraction cuts.

    Set by the runner exactly as `arm` and `warm` are — private attributes an author cannot
    read back and no manifest records. The harness needs three things the extraction has
    and it does not: the instant the return periods are cut from, their length, and where
    the book stood before this run — what earlier windows set aside in the cushion and the
    last period end they settled, both zero and none except on a stage restart — and the
    earliest instant a carry runs from, the anchor unless a restart resumed later.
    """
    strategy._policy = policy  # type: ignore[attr-defined]
    strategy._anchor_ns = anchor_ns  # type: ignore[attr-defined]
    strategy._period_ns = period_ns  # type: ignore[attr-defined]
    strategy._cushion = cushion  # type: ignore[attr-defined]
    strategy._settled_ns = settled_ns  # type: ignore[attr-defined]
    strategy._carried_from_ns = (  # type: ignore[attr-defined]
        anchor_ns if carried_from_ns is None else carried_from_ns
    )


def deliver_from(strategy: object, from_ns: int) -> None:
    """Hand the strategy nothing of a shared feed that precedes `from_ns`.

    A stage feeds one series once to every version subscribed to it, cut at the deepest
    warmup among them, so the feed can reach further back than what one version's own
    request delivers. The harness drops every point before this instant before it records
    or dispatches anything, so a version on a shared feed handles exactly the points a run
    of it alone would: a cold version beside a warmed one sees nothing of the prefix, and
    a shallower warmup sees nothing of a deeper one's. Set by the node for every version
    as `arm` and `warm` are, and never by a run whose feed is the request's own span.
    """
    strategy._fed_from_ns = from_ns  # type: ignore[attr-defined]


def batched(group: Sequence[object]) -> tuple[object, ...]:
    """One series with each instrument's book changes of one instant as one batch.

    A run of consecutive `OrderBookDelta` points of one instrument at one `ts_init` becomes
    one `OrderBookDeltas`, in the order the run held them; every other point passes as it
    is. The flags are left as loaded: nothing downstream buffers on `F_LAST`.
    """
    if not any(isinstance(point, OrderBookDelta) for point in group):
        return tuple(group)
    out: list[object] = []
    run: list[OrderBookDelta] = []
    for point in group:
        if isinstance(point, OrderBookDelta):
            if run and (
                point.ts_init != run[0].ts_init or point.instrument_id != run[0].instrument_id
            ):
                out.append(OrderBookDeltas(run[0].instrument_id, run))
                run = []
            run.append(point)
            continue
        if run:
            out.append(OrderBookDeltas(run[0].instrument_id, run))
            run = []
        out.append(point)
    if run:
        out.append(OrderBookDeltas(run[0].instrument_id, run))
    return tuple(out)


def ordered(groups: Sequence[Sequence[object]], *, coincident: bool = False) -> tuple[object, ...]:
    """Every point of every group in the order an engine would deliver them, marked.

    Each group's book changes are batched per instrument and instant first (`batched`).
    The engine sorts its accumulated stream by `ts_init` with a stable sort, so points
    sharing an instant keep the order their groups were added in. Sorting the
    concatenation the same way reproduces that exactly; `with_cross_section` then
    inserts the flush markers that make a coincident grain one book.
    """
    return with_cross_section(
        tuple(
            sorted(
                chain.from_iterable(batched(group) for group in groups),
                key=lambda point: int(point.ts_init),  # type: ignore[attr-defined]
            )
        ),
        coincident=coincident,
    )


def with_cross_section(points: Sequence[object], *, coincident: bool = False) -> tuple[object, ...]:
    """Insert one flush marker per point of every held cohort, when the stream is marked.

    Markers already in the input are dropped first, so applying this twice is applying
    it once. A stream is marked when it is `coincident` or when any cohort has two or more
    points, and then size-one cohorts get a marker too: an incomplete instant in an
    otherwise multi-instrument feed still has to flush, or its handler waits for a marker
    that never comes. A book cohort gets none, because its handler is never held. An
    unmarked stream is returned unchanged, object identity included.
    """
    plain = tuple(point for point in points if not is_marker(point))
    if not plain:
        return ()
    cohorts = tuple(_cohorts(plain))
    if not coincident and all(len(cohort) == 1 for cohort in cohorts):
        return plain
    out: list[object] = []
    for cohort in cohorts:
        out.extend(cohort)
        if isinstance(cohort[0], (OrderBookDelta, OrderBookDeltas)):
            continue
        ts = int(cohort[0].ts_init)  # type: ignore[attr-defined]
        out.extend(_marker(ts) for _ in cohort)
    return tuple(out)


def _cohorts(points: Sequence[object]) -> list[tuple[object, ...]]:
    """Consecutive runs sharing `(ts_init, kind)` after a stable `ts_init` sort."""
    found: list[tuple[object, ...]] = []
    start = 0
    while start < len(points):
        head = points[start]
        ts = int(head.ts_init)  # type: ignore[attr-defined]
        kind = _kind(head)
        end = start + 1
        while (
            end < len(points)
            and int(points[end].ts_init) == ts  # type: ignore[attr-defined]
            and _kind(points[end]) == kind
        ):
            end += 1
        found.append(tuple(points[start:end]))
        start = end
    return found


def _kind(point: object) -> str:
    inner = _inner(point)
    if isinstance(inner, Bar):
        spec = inner.bar_type.spec
        return f"{BAR}:{spec.step}-{spec.aggregation}-{spec.price_type}"
    if isinstance(inner, QuoteTick):
        return QUOTE
    if isinstance(inner, TradeTick):
        return TRADE
    return type(inner).__name__


def _inner(point: object) -> object:
    if isinstance(point, CustomData):
        return point.data
    return point


def _marker(ts: int) -> CustomData:
    inner = KansoCrossSection(n=1, ts_event=ts, ts_init=ts)
    return CustomData(MARKER_TYPE, inner)
