"""Same-instant delivery: one book for a grain, then one author handler at a time.

The engine delivers by `ts_init` and, at a tie, keeps insertion order. A sleeve that
trades several instruments therefore used to handle the first name against a book the
later names had not yet moved, and a market submitted into the second filled at its
previous close. This module is the repair: after the existing stable `ts_init` sort,
consecutive points that share an availability instant *and* a grain are a cohort.
When any cohort has more than one point, every cohort — including an incomplete
instant of one name — is followed by one marker per point. The venue sees the whole
cohort first; each marker then flushes one buffered handler and the engine settles,
so the next handler sees the fills.

A stream whose every cohort is a single point is left unmarked. Direct `add_data`
tests stay on the old dispatcher.

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
registry is keyed by bare class name across the process.
"""

from collections.abc import Sequence
from itertools import chain

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import Bar, CustomData, DataType, QuoteTick, TradeTick

__all__ = [
    "KansoCrossSection",
    "MARKER_TYPE",
    "arm",
    "is_marker",
    "ordered",
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


def ordered(groups: Sequence[Sequence[object]]) -> tuple[object, ...]:
    """Every point of every group in the order an engine would deliver them, marked.

    The engine sorts its accumulated stream by `ts_init` with a stable sort, so points
    sharing an instant keep the order their groups were added in. Sorting the
    concatenation the same way reproduces that exactly; `with_cross_section` then
    inserts the flush markers that make a coincident grain one book.
    """
    return with_cross_section(
        tuple(sorted(chain.from_iterable(groups), key=lambda point: int(point.ts_init)))  # type: ignore[attr-defined]
    )


def with_cross_section(points: Sequence[object]) -> tuple[object, ...]:
    """Insert one flush marker per point of every cohort, when any cohort is a cross-section.

    Markers already in the input are dropped first, so applying this twice is applying
    it once. A stream of only single-point cohorts is returned unchanged, object identity
    included. When any cohort has two or more points, size-one cohorts get a marker too:
    an incomplete instant in an otherwise multi-instrument feed still has to flush, or
    its handler waits for a marker that never comes.
    """
    plain = tuple(point for point in points if not is_marker(point))
    if not plain:
        return ()
    cohorts = tuple(_cohorts(plain))
    if all(len(cohort) == 1 for cohort in cohorts):
        return plain
    out: list[object] = []
    for cohort in cohorts:
        out.extend(cohort)
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
