"""The stream a card hands its engine: in order, each kind of point validated once.

`_load_stream` adds the first run of each kind of point — a type, and the instrument or bar
type its first point is of, which is all the engine's validation reads — in a validated
`add_data` call of its own, and everything between those runs in unvalidated calls of several
types, then sorts once. The engine runs such a call as the runs it holds
(`kanso.nautilus.facts`); what is held here is what it is handed.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from nautilus_trader.model.data import Bar, CustomData

from kanso.nautilus.backtest import CLIENT_ID, _load_stream
from kanso.nautilus.cross_section import with_cross_section
from tests.nautilus.strategy.conftest import DEMO, HEDGE, bar, quote, second_bar, trade


class _Engine:
    """Records what `_load_stream` hands an engine."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[Any], Any, bool, bool]] = []
        self.sorted = 0

    def add_data(
        self, data: list[Any], client_id: Any = None, validate: bool = True, sort: bool = True
    ) -> None:
        self.calls.append((list(data), client_id, validate, sort))

    def sort_data(self) -> None:
        self.sorted += 1


def _kind(point: Any) -> tuple[type, object]:
    if isinstance(point, Bar):
        return Bar, point.bar_type
    return type(point), getattr(point, "instrument_id", None)


def _made(kind: str, name: Any, index: int) -> object:
    if kind == "bar":
        return bar(name, index, 10.0)
    if kind == "second":
        return second_bar(name, 0, 10.0, ts_init=int(bar(DEMO, index, 1.0).ts_init))
    if kind == "quote":
        return quote(name, index)
    return trade(name, index)


@settings(max_examples=80, deadline=None)
@given(
    drawn=st.lists(
        st.tuples(
            st.sampled_from(("bar", "second", "quote", "trade")),
            st.sampled_from((DEMO, HEDGE)),
            st.integers(min_value=0, max_value=3),
        ),
        max_size=30,
    ),
    book=st.booleans(),
)
def test_each_kind_is_validated_once_and_the_stream_kept_in_order(
    drawn: list[tuple[str, Any, int]], book: bool
) -> None:
    points = with_cross_section(
        tuple(sorted((_made(*each) for each in drawn), key=lambda p: int(p.ts_init))),
        coincident=True,
    )
    engine = _Engine()

    _load_stream(engine, points, book=book)

    handed = [point for data, *_ in engine.calls for point in data]
    assert len(handed) == len(points)
    assert all(left is right for left, right in zip(handed, points, strict=True))
    assert engine.sorted == (1 if points else 0)
    assert all(not sort for *_, sort in engine.calls)
    validated = [(data, client) for data, client, validate, _ in engine.calls if validate]
    kinds = [_kind(data[0]) for data, _ in validated]
    assert len(kinds) == len(set(kinds))
    for data, client in validated:
        assert len({type(point) for point in data}) == 1
        custom = type(data[0]) is CustomData
        assert (client is not None and str(client) == CLIENT_ID) if custom else client is None
        assert not (book and not custom)
    firsts: dict[tuple[type, object], object] = {}
    for at, point in enumerate(points):
        starts_a_run = at == 0 or type(points[at - 1]) is not type(point)
        if starts_a_run and not (book and type(point) is not CustomData):
            firsts.setdefault(_kind(point), point)
    assert [id(data[0]) for data, _ in validated] == [id(point) for point in firsts.values()]
