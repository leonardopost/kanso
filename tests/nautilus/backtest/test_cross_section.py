"""A coincident grain is one book; a fill against the other leg is at this close."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from kanso.nautilus.backtest import _load_stream, checked, execute, run
from kanso.nautilus.cross_section import with_cross_section
from kanso.nautilus.session import run_node
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


def test_released_counts_catalog_points_not_flush_markers(request_for) -> None:
    """Session `released` is a market-point count; slicing the marked feed by it drops bars."""
    demo, other = bars(DAYS), bars(DAYS, OTHER)
    request = request_for(source=CROSS_SLEEVE, hypothesis_=_hyp())
    instruments, groups = tuple(_instruments()), tuple(_groups())
    node = _execute(request, instruments, groups, mode=NODE, speed=0.0)
    engine = _execute(request, instruments, groups, mode=ENGINE, speed=0.0)

    assert node.released == engine.released == len(demo) + len(other)
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
    stream = checked(request_for(), [instrument()], [tuple(demo), (marker,)])

    assert [row[1] for row in stream] == [INSTRUMENT] * len(demo)


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
