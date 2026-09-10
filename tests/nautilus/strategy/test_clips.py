"""A sized overlay names clips and the harness sizes them to the overlay's own budget."""

from __future__ import annotations

import pytest

from kanso.errors import ValidationError
from kanso.nautilus.hooks import OVERLAY, Clip, Hedge
from kanso.nautilus.sizing import (
    BUDGET_BELOW_LOT,
    CLIP_DIRECTION,
    CLIP_WITHOUT_BUDGET,
    HEDGE_UNDER_SIZING,
    ONE_CLIP,
    SizingError,
)
from kanso.nautilus.strategy import Decision, HookContext, KansoModifier, KansoModifierConfig

from .conftest import DEMO, HEDGE
from .test_sizing import DEMO_SHARES, HEDGE_SHARES, Enters, intents, sized, two_names

CLIP_BUDGET = 20_000.0
HEDGE_CLIP = 999.0
"""floor(20,000 / 20.01)."""
DEMO_CLIP = 1_998.0
"""floor(20,000 / 10.01)."""


class Clipper(KansoModifier):
    """Answers the data clock from a script: {bar count of the host: clips}."""

    construct = OVERLAY
    config_cls = KansoModifierConfig
    script: dict[int, tuple[Clip, ...]] = {}

    def on_start(self) -> None:
        self.ticks = 0
        self.books: list[tuple[dict[str, float], dict[str, float]]] = []

    def on_data(self, ctx: HookContext) -> Decision:
        self.ticks += 1
        self.books.append((dict(ctx.book), dict(ctx.clips)))
        clips = self.script.get(self.ticks)
        if clips is None:
            return Decision.neutral(self.construct)
        return Decision(clips=clips)


def clipper(cls: type[KansoModifier] = Clipper, budget: float = CLIP_BUDGET, host: str = "Enters"):
    return cls(KansoModifierConfig(host_strategy_id=host, hyp_id="attached", sizing_budget=budget))


def positions(run) -> dict[str, float]:
    return {str(p.instrument_id): float(p.signed_qty) for p in run.engine.cache.positions_open()}


def test_a_clip_is_an_instrument_and_a_side() -> None:
    assert Clip("HEDGE.XNAS", "FLAT").side == "FLAT"
    with pytest.raises(ValidationError, match="clip.side: 'LONG' is none of BUY, SELL, FLAT"):
        Clip("HEDGE.XNAS", "LONG")


def test_a_clip_is_sized_to_the_overlay_s_budget_not_the_host_s(backtest) -> None:
    class Once(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),)}

    overlay = clipper(Once)
    run = backtest(Enters(sized()), [overlay], data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES), ("HEDGE.XNAS", "BUY", HEDGE_CLIP)]
    assert positions(run) == {"DEMO.XNAS": DEMO_SHARES, "HEDGE.XNAS": HEDGE_CLIP}


def test_a_clip_on_the_side_already_held_is_a_no_op(backtest) -> None:
    class Twice(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),), 12: (Clip("HEDGE.XNAS", "BUY"),)}

    run = backtest(Enters(sized()), [clipper(Twice)], data=two_names(), instruments=(DEMO, HEDGE))

    assert [row for row in intents(run) if row[0] == "HEDGE.XNAS"] == [
        ("HEDGE.XNAS", "BUY", HEDGE_CLIP)
    ]


def test_a_second_clip_while_one_is_held_is_refused(backtest) -> None:
    class Greedy(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),), 12: (Clip("DEMO.XNAS", "BUY"),)}

    with pytest.raises(SizingError) as failure:
        backtest(Enters(sized()), [clipper(Greedy)], data=two_names(), instruments=(DEMO, HEDGE))

    refused = failure.value.refusal
    assert refused.rule == ONE_CLIP
    assert "a clip of 999 is held in HEDGE.XNAS" in refused.why
    assert "Clip('HEDGE.XNAS', 'FLAT') in the same answer" in refused.why


def test_the_other_side_of_a_held_clip_is_refused_until_flat(backtest) -> None:
    class Reverses(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),), 12: (Clip("HEDGE.XNAS", "SELL"),)}

    with pytest.raises(SizingError) as failure:
        backtest(Enters(sized()), [clipper(Reverses)], data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == CLIP_DIRECTION
    assert "Clip('HEDGE.XNAS', 'FLAT') before opening the other side" in failure.value.refusal.why


def test_a_flat_clip_takes_back_exactly_the_clip(backtest) -> None:
    class RoundTrip(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),), 12: (Clip("HEDGE.XNAS", "FLAT"),)}

    overlay = clipper(RoundTrip)
    run = backtest(Enters(sized()), [overlay], data=two_names(), instruments=(DEMO, HEDGE))

    assert [row for row in intents(run) if row[0] == "HEDGE.XNAS"] == [
        ("HEDGE.XNAS", "BUY", HEDGE_CLIP),
        ("HEDGE.XNAS", "SELL", HEDGE_CLIP),
    ]
    assert positions(run) == {"DEMO.XNAS": DEMO_SHARES}
    assert overlay.books[-1][1] == {"DEMO.XNAS": 0.0, "HEDGE.XNAS": 0.0}


def test_a_flat_with_nothing_held_places_nothing(backtest) -> None:
    class Idle(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "FLAT"),)}

    run = backtest(Enters(sized()), [clipper(Idle)], data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]


def test_a_clip_switches_names_with_a_flat_in_the_same_answer(backtest) -> None:
    class Switches(Clipper):
        script = {
            8: (Clip("HEDGE.XNAS", "BUY"),),
            12: (Clip("HEDGE.XNAS", "FLAT"), Clip("DEMO.XNAS", "BUY")),
        }

    run = backtest(
        Enters(sized()), [clipper(Switches)], data=two_names(), instruments=(DEMO, HEDGE)
    )

    assert intents(run)[-2:] == [
        ("HEDGE.XNAS", "SELL", HEDGE_CLIP),
        ("DEMO.XNAS", "BUY", DEMO_CLIP),
    ]
    assert positions(run) == {"DEMO.XNAS": DEMO_SHARES + DEMO_CLIP}


def test_a_hedge_from_a_sized_overlay_is_refused(backtest) -> None:
    class Old(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

        def on_data(self, ctx: HookContext) -> Decision:
            return Decision(hedges=(Hedge("HEDGE.XNAS", 10.0),))

    with pytest.raises(SizingError) as failure:
        backtest(Enters(sized()), [clipper(Old)], data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == HEDGE_UNDER_SIZING
    assert "Decision(clips=(Clip(instrument, side),))" in failure.value.refusal.why


def test_a_clip_from_an_overlay_without_a_budget_is_refused(backtest) -> None:
    class New(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

        def on_data(self, ctx: HookContext) -> Decision:
            return Decision(clips=(Clip("HEDGE.XNAS", "BUY"),))

    with pytest.raises(SizingError) as failure:
        backtest(
            Enters(sized()), [clipper(New, budget=0.0)], data=two_names(), instruments=(DEMO, HEDGE)
        )

    assert failure.value.refusal.rule == CLIP_WITHOUT_BUDGET


def test_the_neutral_answer_is_refused_on_neither_kind(backtest) -> None:
    class Quiet(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

        def on_data(self, ctx: HookContext) -> Decision:
            return Decision.neutral(self.construct)

    for budget in (CLIP_BUDGET, 0.0):
        run = backtest(
            Enters(sized()),
            [clipper(Quiet, budget=budget)],
            data=two_names(),
            instruments=(DEMO, HEDGE),
        )
        assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]


def test_the_host_s_own_share_excludes_the_clip_in_the_same_name(backtest) -> None:
    class SameName(Clipper):
        script = {8: (Clip("DEMO.XNAS", "BUY"),)}

    class Reads(Enters):
        def after(self, bars: int) -> None:
            if bars == 12:
                self.seen.append(self.held(DEMO))

    overlay = clipper(SameName, host="Reads")
    run = backtest(Reads(sized()), [overlay], data=two_names(), instruments=(DEMO, HEDGE))

    assert positions(run) == {"DEMO.XNAS": DEMO_SHARES + DEMO_CLIP}
    assert run.strategy.seen[-1] == DEMO_SHARES
    assert overlay.books[-1] == (
        {"DEMO.XNAS": DEMO_SHARES, "HEDGE.XNAS": 0.0},
        {"DEMO.XNAS": DEMO_CLIP, "HEDGE.XNAS": 0.0},
    )


def test_the_host_s_exit_does_not_touch_the_clip(backtest) -> None:
    class SameName(Clipper):
        script = {8: (Clip("DEMO.XNAS", "BUY"),)}

    class Leaves(Enters):
        def after(self, bars: int) -> None:
            if bars == 12:
                self.submit_exit(DEMO)

    run = backtest(
        Leaves(sized()),
        [clipper(SameName, host="Leaves")],
        data=two_names(),
        instruments=(DEMO, HEDGE),
    )

    assert intents(run)[-1] == ("DEMO.XNAS", "SELL", DEMO_SHARES)
    assert positions(run) == {"DEMO.XNAS": DEMO_CLIP}, "the clip stays"


def test_a_clip_flattened_from_evaluate_fills_in_the_flip_s_settle(backtest) -> None:
    """At a flip the overlay is asked inside the host's entry and its legs settle with it."""

    class Inverse(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),)}

        def on_start(self) -> None:
            super().on_start()
            self.asked: list[tuple[str, str, dict[str, float], dict[str, float]]] = []

        def evaluate(self, ctx: HookContext) -> Decision:
            self.asked.append((ctx.instrument_id, ctx.side, dict(ctx.book), dict(ctx.clips)))
            if ctx.side == "BUY" and ctx.clips.get(ctx.instrument_id):
                other = "DEMO.XNAS" if ctx.instrument_id == "HEDGE.XNAS" else "HEDGE.XNAS"
                return Decision(clips=(Clip(ctx.instrument_id, "FLAT"), Clip(other, "BUY")))
            return Decision.neutral(self.construct)

    class Flips(Enters):
        def after(self, bars: int) -> None:
            if bars == 12:
                self.submit_exit(DEMO)
                self.submit_entry(HEDGE, "BUY")

    overlay = clipper(Inverse, host="Flips")
    run = backtest(Flips(sized()), [overlay], data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run)[-4:] == [
        ("DEMO.XNAS", "SELL", DEMO_SHARES),
        ("HEDGE.XNAS", "BUY", HEDGE_SHARES),
        ("HEDGE.XNAS", "SELL", HEDGE_CLIP),
        ("DEMO.XNAS", "BUY", DEMO_CLIP),
    ]
    assert len({i.ts_event for i in run.strategy.intents[-4:]}) == 1
    assert positions(run) == {"HEDGE.XNAS": HEDGE_SHARES, "DEMO.XNAS": DEMO_CLIP}
    assert overlay.asked[-1] == (
        "HEDGE.XNAS",
        "BUY",
        {"DEMO.XNAS": 0.0, "HEDGE.XNAS": 0.0},
        {"DEMO.XNAS": 0.0, "HEDGE.XNAS": HEDGE_CLIP},
    ), "the exit in flight is netted out of the book; the clip is the overlay's"


def test_a_clip_into_an_unknown_instrument_is_refused(backtest) -> None:
    class Elsewhere(Clipper):
        script = {8: (Clip("NOPE.XNAS", "BUY"),)}

    with pytest.raises(ValidationError, match="clip: NOPE.XNAS is not in the cache"):
        backtest(Enters(sized()), [clipper(Elsewhere)], data=two_names(), instruments=(DEMO, HEDGE))


def test_a_clip_before_any_print_of_its_name_places_nothing(backtest) -> None:
    class Early(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),), 12: (Clip("HEDGE.XNAS", "BUY"),)}

    from .conftest import flat

    run = backtest(
        Enters(sized()), [clipper(Early)], data=list(flat(DEMO)), instruments=(DEMO, HEDGE)
    )

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]


def test_a_clip_budget_below_one_lot_is_refused(backtest) -> None:
    class Tiny(Clipper):
        script = {8: (Clip("HEDGE.XNAS", "BUY"),)}

    with pytest.raises(SizingError) as failure:
        backtest(
            Enters(sized()),
            [clipper(Tiny, budget=5.0)],
            data=two_names(),
            instruments=(DEMO, HEDGE),
        )

    assert failure.value.refusal.rule == BUDGET_BELOW_LOT
    assert "a budget of 5 at 20 floors to no whole lot" in failure.value.refusal.why
