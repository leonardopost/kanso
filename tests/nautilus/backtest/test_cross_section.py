"""A coincident grain is one book; a fill against the other leg is at this close."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from kanso.nautilus.backtest import _load_stream, checked, execute, run
from kanso.nautilus.cross_section import batched, coincident, is_marker, ordered, with_cross_section
from kanso.nautilus.session import run_node
from kanso.replay.record import Spool
from kanso.replay.run import ENGINE, NODE, _execute
from tests.nautilus.backtest.conftest import (
    INSTRUMENT,
    RESEARCH,
    bars,
    catalog,
    hypothesis,
    instrument,
    second_bars,
)
from tests.replay.test_session import both

OTHER = "OTHR"
OTHER_ID = f"{OTHER}.XNAS"
DAYS = (date(2024, 1, 1), date(2024, 1, 4))

CROSS_SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        names = [str(i) for i in self.universe]
        if str(bar.bar_type.instrument_id) != names[0]:
            return
        self.submit_entry(names[1], "BUY", notional=1_000.0)
"""

DOUBLE_SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        names = [str(i) for i in self.universe]
        held = self.universe[1]
        key = str(bar.bar_type.instrument_id)
        if key == names[0]:
            self.submit_entry(held, "BUY", notional=1_000.0)
        elif key == names[1] and float(self.portfolio.net_position(held)) == 0:
            self.submit_entry(held, "BUY", notional=1_000.0)
"""


def _hyp():
    return hypothesis(universe=[INSTRUMENT, OTHER_ID])


def _groups():
    return [tuple(bars(DAYS)), tuple(bars(DAYS, OTHER))]


def _instruments():
    return [instrument(), instrument(OTHER)]


def test_a_cross_instrument_market_fills_at_this_instant_close(tmp_path, request_for) -> None:
    """Submitting into the second name from the first on_bar fills at today's close."""
    hyp = _hyp()
    other = bars(DAYS, OTHER)
    store = catalog(tmp_path / "catalog", [*bars(DAYS), *other], _instruments())
    card = run(request_for(source=CROSS_SLEEVE, hypothesis_=hyp), store).run

    assert [fill.instrument_id for fill in card.fills] == [OTHER_ID] * len(other)
    assert [fill.px for fill in card.fills] == [float(bar.close) for bar in other]


def test_both_paths_emit_one_intent_when_the_second_leg_would_also_buy(
    request_for,
) -> None:
    """A settle between flushed handlers is what keeps the two code paths at one B intent."""
    request = request_for(source=DOUBLE_SLEEVE, hypothesis_=_hyp())
    node, engine = both(request, _instruments(), _groups())
    other = bars(DAYS, OTHER)

    assert node.intents == engine.intents
    bought = [row for row in engine.intents if row[1] == OTHER_ID and row[2] == "BUY"]
    assert len(bought) == len(other)
    assert [fill.px for fill in engine.run.fills] == [float(bar.close) for bar in other]


def test_an_incomplete_instant_still_trades_the_silent_leg_at_last_public_close(
    request_for,
) -> None:
    """A day the second name does not print is not lookahead and is not a stuck buffer."""
    demo = bars(DAYS)
    other = bars((DAYS[0], date(2024, 1, 3)), OTHER)
    request = request_for(source=CROSS_SLEEVE, hypothesis_=_hyp())
    card = execute(request, _instruments(), [tuple(demo), tuple(other)]).run

    assert len(card.fills) == len(demo)
    assert card.fills[-1].px == float(other[-1].close)
    assert [fill.px for fill in card.fills[:-1]] == [float(bar.close) for bar in other]


def test_released_counts_catalog_points_not_flush_markers(request_for, tmp_path: Path) -> None:
    """Session `released` is a market-point count; slicing the marked feed by it drops bars."""
    demo, other = bars(DAYS), bars(DAYS, OTHER)
    request = request_for(source=CROSS_SLEEVE, hypothesis_=_hyp())
    instruments, groups = tuple(_instruments()), tuple(_groups())
    node = _execute(
        request, instruments, iter([groups]), mode=NODE, speed=0.0, sink=Spool(tmp_path / "node")
    )
    engine = _execute(
        request,
        instruments,
        iter([groups]),
        mode=ENGINE,
        speed=0.0,
        sink=Spool(tmp_path / "engine"),
    )

    assert node.released == engine.released == len(demo) + len(other)
    assert node.intents == engine.intents


def test_a_book_instant_is_released_as_one_point_on_both_paths(request_for, tmp_path: Path) -> None:
    """A session counts what it delivered: the changes one book made at one instant are one
    batch, and so one point of the count, on the node as on the engine."""
    from tests.nautilus.backtest.conftest import POSTER, TICK_DAYS, tick_groups, tick_hypothesis

    request = request_for(source=POSTER, hypothesis_=tick_hypothesis())
    groups = tuple(tick_groups())
    instruments = (instrument(),)
    node = _execute(
        request, instruments, iter([groups]), mode=NODE, speed=0.0, sink=Spool(tmp_path / "node")
    )
    engine = _execute(
        request,
        instruments,
        iter([groups]),
        mode=ENGINE,
        speed=0.0,
        sink=Spool(tmp_path / "engine"),
    )

    book, prints = groups
    instants = {int(change.ts_init) for change in book}  # type: ignore[attr-defined]
    assert node.released == engine.released == len(instants) + len(prints)
    assert len(instants) < len(book) and len(TICK_DAYS) == 2
    assert node.intents == engine.intents


def test_a_single_instrument_stream_is_unchanged(request_for) -> None:
    """One name is still the per-point dispatcher; no markers, no extra fill delay."""
    demo = bars(DAYS)
    request = request_for()
    engine = execute(request, [instrument()], [tuple(demo)])
    node = run_node(request, [instrument()], [tuple(demo)])

    assert engine.intents == node.intents
    assert engine.intents


def test_a_marker_in_a_loaded_group_is_not_a_clock_tick(request_for) -> None:
    """Markers are a feed signal; they must not become period marks."""
    demo = bars(DAYS)
    other = bars(DAYS, OTHER)
    marker = with_cross_section((demo[0], other[0]))[2]
    marks = checked(request_for(), [instrument()], [tuple(demo), (marker,)])

    assert [set(fold.marks) for fold in marks.periods()] == [{INSTRUMENT}] * len(demo)


def test_an_empty_feed_is_not_added_to_the_engine() -> None:
    engine = MagicMock()
    _load_stream(engine, ())
    engine.add_data.assert_not_called()
    engine.sort_data.assert_not_called()


CLIPPER = b"""
from kanso.nautilus.strategy import Decision, Hedge, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    construct = "overlay"
    config_cls = Config

    def on_data(self, ctx):
        if ctx.book.get(ctx.instrument_id, 0.0) > 0:
            return Decision(hedges=(Hedge(ctx.instrument_id, -1.0),))
        return Decision.neutral(self.construct)
"""


def test_a_finer_overlay_clips_on_its_own_grain_while_the_host_keeps_the_daily(
    request_for,
) -> None:
    """The runner loads both grains: the host's on_bar sees only days, on_data only seconds."""
    overlay = hypothesis().model_copy(update={"resolution": "1s"})
    daily, seconds = bars(RESEARCH), second_bars(RESEARCH)
    request = request_for(
        RESEARCH,
        hypothesis_=overlay,
        grains=("1d", "1s"),
        modifiers=(("overlay", CLIPPER, {}),),
    )
    with_overlay = execute(request, [instrument()], [tuple(daily), tuple(seconds)])
    host_alone = execute(request_for(RESEARCH), [instrument()], [tuple(daily)])

    hosts = [row for row in with_overlay.intents if row[3] != 1.0]
    clips = [row for row in with_overlay.intents if row[3] == 1.0]
    assert [row[:3] for row in hosts] == [row[:3] for row in host_alone.intents]
    assert clips and {row[2] for row in clips} == {"SELL"}
    assert {row[0] for row in clips} <= {int(bar.ts_event) for bar in seconds}


THIRD = "THRD"
THIRD_ID = f"{THIRD}.XNAS"

HOLDING_SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        if str(bar.bar_type.instrument_id) != str(self.universe[0]):
            return
        self.n += 1
        if self.n == 2:
            self.submit_entry(self.universe[0], "BUY", notional=1_000.0)
        elif self.n == 4:
            self.submit_exit(self.universe[0])
"""

ACCUMULATING = b"""
from kanso.nautilus.strategy import Decision, Hedge, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    construct = "overlay"
    config_cls = Config

    def on_data(self, ctx):
        if ctx.book.get("DEMO.XNAS", 0.0) > 0:
            return Decision(hedges=(Hedge("THRD.XNAS", 1.0),))
        return Decision.neutral(self.construct)
"""


def test_an_overlay_on_the_host_grain_fires_identically_on_both_paths(request_for) -> None:
    """The in-flight guard reads the sleeve's own orders, so a node and the engine agree."""
    hyp = hypothesis(universe=[INSTRUMENT, OTHER_ID, THIRD_ID])
    request = request_for(
        source=HOLDING_SLEEVE, hypothesis_=hyp, modifiers=(("overlay", ACCUMULATING, {}),)
    )
    instruments = [instrument(), instrument(OTHER), instrument(THIRD)]
    groups = [tuple(bars(DAYS)), tuple(bars(DAYS, OTHER)), tuple(bars(DAYS, THIRD))]
    node, engine = both(request, instruments, groups)

    assert node.intents == engine.intents
    assert [row[1] for row in engine.intents].count(THIRD_ID) >= 1


SIZED_HOST = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        if str(bar.bar_type.instrument_id) != str(self.universe[0]):
            return
        self.n += 1
        if self.n == 1:
            self.submit_entry(self.universe[0], "BUY")
        elif self.n == 4:
            self.submit_exit(self.universe[0])
"""

SAME_NAME_CLIPPER = b"""
from kanso.nautilus.strategy import Clip, Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    construct = "overlay"
    config_cls = Config

    def on_data(self, ctx):
        if ctx.book.get("DEMO.XNAS", 0.0) > 0 and not ctx.clips.get("DEMO.XNAS"):
            return Decision(clips=(Clip("DEMO.XNAS", "BUY"),))
        return Decision.neutral(self.construct)
"""


def test_the_host_s_own_share_excludes_the_clip_in_the_same_name_on_both_paths(
    request_for,
) -> None:
    """The ledger reads the same on the engine and on a node: the host exits its own share."""
    request = request_for(
        source=SIZED_HOST,
        hypothesis_=_hyp(),
        sleeve_budget=10_000.0,
        modifiers=(("overlay", SAME_NAME_CLIPPER, {"sizing_budget": 5_000.0}),),
    )
    node, engine = both(request, _instruments(), _groups())

    assert node.intents == engine.intents
    buys = [row for row in engine.intents if row[2] == "BUY"]
    sells = [row for row in engine.intents if row[2] == "SELL"]
    assert len(buys) == 2 and len(sells) == 1, engine.intents
    assert sells[0][3] == buys[0][3], "the host sells what it bought, never the clip"
    assert buys[1][3] < buys[0][3], "the clip is the overlay's smaller budget"


def _delta(name: str, ts: int, price: int) -> object:
    from nautilus_trader.model.enums import BookAction, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId

    from kanso.data.loaders.points import make_delta

    ident = InstrumentId.from_str(name)
    return make_delta(ident, BookAction.UPDATE, OrderSide.BUY, price, 10, 0, 2, 0, ts, ts)


def test_the_changes_of_one_instrument_at_one_instant_are_one_batch() -> None:
    """Two names' books changing at one instant are two batches, each in the order its
    changes were loaded; a change at another instant, or a point of another kind between
    two changes, starts another."""
    from nautilus_trader.model.data import OrderBookDeltas

    demo = bars(DAYS)[0]
    changes = [
        _delta(INSTRUMENT, 5, 1_000),
        _delta(INSTRUMENT, 5, 1_001),
        _delta(OTHER_ID, 5, 2_000),
        _delta(OTHER_ID, 5, 2_001),
        _delta(OTHER_ID, 6, 2_002),
        demo,
        _delta(OTHER_ID, 6, 2_003),
    ]

    made = batched(changes)

    assert [type(point).__name__ for point in made] == [
        "OrderBookDeltas",
        "OrderBookDeltas",
        "OrderBookDeltas",
        "Bar",
        "OrderBookDeltas",
    ]
    batches = [point for point in made if isinstance(point, OrderBookDeltas)]
    assert [str(batch.instrument_id) for batch in batches] == [INSTRUMENT, OTHER_ID] + [
        OTHER_ID
    ] * 2
    assert (
        [[int(d.order.price.raw) for d in batch.deltas] for batch in batches]
        == [
            [int(c.order.price.raw) for c in changes[0:2]],  # type: ignore[attr-defined]
            [int(c.order.price.raw) for c in changes[2:4]],  # type: ignore[attr-defined]
            [int(changes[4].order.price.raw)],  # type: ignore[attr-defined]
            [int(changes[6].order.price.raw)],  # type: ignore[attr-defined]
        ]
    )
    assert [int(batch.ts_init) for batch in batches] == [5, 5, 6, 6]
    assert batched(bars(DAYS)) == tuple(bars(DAYS)), "a series with no book passes as it is"


def test_no_marker_follows_a_book_batch() -> None:
    """A book handler is never held, so a marker after a batch would flush nothing: the
    prints sharing the batch's instant are marked, the batch is not."""
    from tests.nautilus.backtest.conftest import TICK_DAYS, ticking

    book, prints = ticking(TICK_DAYS[0])
    stream = ordered([tuple(book), tuple(prints)], coincident=True)

    followed = [
        type(stream[index - 1]).__name__
        for index, point in enumerate(stream)
        if is_marker(point) and not is_marker(stream[index - 1])
    ]
    assert followed and set(followed) == {"TradeTick"}
    assert sum(map(is_marker, stream)) == len(prints)


def test_a_tick_feed_is_marked_however_its_instants_fall() -> None:
    """Prints that never share an instant are still marked when the hypothesis is a tick
    feed, and so is any feed of several names; a feed of one name's bars is marked only
    when its points share an instant."""
    from tests.nautilus.backtest.conftest import TICK_DAYS, tick_hypothesis, ticking

    _, prints = ticking(TICK_DAYS[0])
    alone = tuple(prints[1::3])
    assert len({int(p.ts_init) for p in alone}) == len(alone)

    assert coincident(tick_hypothesis())
    assert coincident(hypothesis(universe=[INSTRUMENT, OTHER_ID]))
    assert not coincident(hypothesis())
    assert sum(map(is_marker, with_cross_section(alone, coincident=True))) == len(alone)
    assert with_cross_section(alone) == alone
    demo = tuple(bars(DAYS))
    assert with_cross_section(demo, coincident=coincident(hypothesis())) == demo


def test_whether_a_feed_is_marked_does_not_depend_on_the_chunk(request_for) -> None:
    """The same tick window run whole and run a few points a chunk, in this process: one
    card, because every chunk of a coincident feed is marked — whereas a chunk read off its
    own points goes unmarked whenever no instant in it holds two points of a kind. Measured
    on these chunks with the marking read off each one instead: 27 fills against the whole
    window's 29 at 20 ms, and other intents at 0 ms."""
    from kanso.nautilus import backtest as runner
    from tests.nautilus.backtest.conftest import POSTER, tick_groups, tick_hypothesis

    request = request_for(source=POSTER, hypothesis_=tick_hypothesis(20.0))
    groups = tick_groups()
    chunks = list(runner._cut(groups, 7))
    assert any(not any(map(is_marker, ordered(chunk))) for chunk in chunks), (
        "some chunk holds no instant two points of a kind share"
    )

    whole = execute(request, [instrument()], groups)
    cut = runner.execute_chunked(request, [instrument()], chunks)

    assert whole.run.fills
    assert (cut.run, cut.intents) == (whole.run, whole.intents)


STAMPS = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy

SEEN = []


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Records the instant of every bar and settlement it is handed, and trades nothing."""

    config_cls = Config

    def on_bar(self, bar):
        SEEN.append(("bar", int(bar.ts_init)))

    def on_data(self, data):
        SEEN.append(("data", int(data.ts_init)))
'''


def test_a_sleeve_started_on_an_unmarked_chunk_is_flushed_on_a_marked_one(request_for) -> None:
    """One name's bars and custom points are not coincident by rule, so a chunk is marked
    only when two points of one series share an instant — as a split and a dividend on one
    ex-date do; here two settlements of nothing. Chunked by day, the first day holds no such
    instant and the sleeve starts unmarked; the second does, and the sleeve, held for
    markers from then on, subscribes them then, or every point of the second day waits for
    a flush that never reaches it. It is handed what one chunk of both days hands."""
    import sys
    from hashlib import sha256

    from kanso.nautilus.backtest import execute_chunked
    from tests.nautilus.backtest.test_depth import _bars, _settlements

    first, second = date(2024, 1, 2), date(2024, 1, 3)
    hyp = hypothesis(data_requirements=("bar", "funding"), resolution="1s")
    assert not coincident(hyp)
    days = [
        (tuple(_bars(first, (1_000, 2_000, 3_000))), tuple(_settlements(first, (1_500,)))),
        (tuple(_bars(second, (1_000, 2_000, 3_000))), tuple(_settlements(second, (2_000, 2_000)))),
    ]
    assert not any(map(is_marker, ordered(days[0])))
    assert any(map(is_marker, ordered(days[1])))

    def handed(chunks: list[tuple[tuple[object, ...], ...]], tag: str) -> list[tuple[str, int]]:
        source = STAMPS + f"# {tag}\n".encode()
        result = execute_chunked(
            request_for(RESEARCH, source=source, hypothesis_=hyp), [instrument()], chunks
        )
        assert not result.crashed, result.traceback_tail
        return list(sys.modules[f"kanso_sleeve_{sha256(source).hexdigest()[:12]}"].SEEN)

    whole = handed([(days[0][0] + days[1][0], days[0][1] + days[1][1])], "one")
    by_day = handed(days, "two")

    assert by_day == whole
    assert [kind for kind, _ in by_day] == [
        *("bar", "data", "bar", "bar"),
        *("bar", "bar", "data", "data", "bar"),
    ]
