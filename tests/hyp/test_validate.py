"""Validation: one hypothesis per rule, each breaking exactly that rule.

Every failure is a validation failure (exit 3) whose message names the field and says why,
so the test asserts both rather than only that something went wrong.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from kanso import hyp
from kanso.config import CONFIG_NAME, load_config
from kanso.data.instruments import resolve_universe
from kanso.errors import Exit, KansoError, ValidationError
from kanso.hyp.validate import venue_models
from kanso.workspace import Workspace
from tests.hyp.conftest import (
    DEMO_ENTRY,
    DOCUMENT,
    FILTER_CLASSIFICATION,
    HOST_ID,
    PERP_ENTRY,
    SLEEVE_CLASSIFICATION,
    document,
    write_hypothesis,
    write_instruments,
    write_portfolio,
    write_strategy,
)


def refused(ws: Workspace, doc: dict[str, Any], hyp_id: str | None = None) -> ValidationError:
    """Validate a document that must be refused, and hand back the failure."""
    path = write_hypothesis(ws, doc, hyp_id)
    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, path)
    assert failure.value.code is Exit.VALIDATION
    assert isinstance(failure.value, ValidationError)
    return failure.value


def accepted(ws: Workspace, doc: dict[str, Any]) -> Any:
    return hyp.validate(ws, write_hypothesis(ws, doc))


# -- the admissible one --------------------------------------------------------


def test_the_demo_hypothesis_is_admissible(ws: Workspace) -> None:
    assert accepted(ws, DOCUMENT).id == "demo_mr"


def test_a_parent_the_workspace_has_not_registered_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(derived_from=["demo_parent"]))

    assert failure.message.startswith("derived_from: demo_parent is not a registered hypothesis")
    assert "kanso hyp add" in (failure.remedy or "")


def test_a_registered_parent_is_admissible(ws: Workspace) -> None:
    from kanso.state import StateStore

    with StateStore(ws.path("state.db")) as store:
        store.migrate()
        hyp.add(ws, store, write_hypothesis(ws, {**DOCUMENT, "id": "demo_parent"}))

    assert accepted(ws, document(derived_from=["demo_parent"])).derived_from == ["demo_parent"]


def test_a_classified_sleeve_is_admissible(ws: Workspace) -> None:
    parsed = accepted(ws, document(**SLEEVE_CLASSIFICATION))

    assert parsed.construct is not None
    assert parsed.construct.id == "sleeve"


def test_a_construct_attached_to_a_certified_host_is_admissible(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID)

    parsed = accepted(ws, document(**FILTER_CLASSIFICATION))

    assert parsed.construct is not None
    assert parsed.construct.host == HOST_ID


# -- the duration grammar and the windows --------------------------------------


def test_a_horizon_outside_the_duration_grammar_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(horizon="30x"))

    assert "horizon" in failure.message


def test_a_certification_window_inside_the_embargo_is_refused(ws: Workspace) -> None:
    # A one-day horizon embargoes five days; research closes 2024-12-31.
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2025-01-02", "end": "2025-05-30"},
    }

    failure = refused(ws, document(horizon="1d", windows=windows))

    assert "windows.certification.start" in failure.message
    assert "embargo" in failure.message


def test_the_embargo_is_five_horizons_or_a_day_whichever_is_longer(ws: Workspace) -> None:
    # 30m x 5 is under a day, so the floor applies and the day after research closes is legal.
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2025-01-01", "end": "2025-05-30"},
    }

    assert accepted(ws, document(windows=windows, horizon="1m")) is not None


def test_a_long_horizon_pushes_certification_further_out(ws: Workspace) -> None:
    # 10d x 5 is 50 days, so a certification window opening two days later is refused.
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2025-01-02", "end": "2025-05-30"},
    }

    failure = refused(ws, document(windows=windows, horizon="10d"))

    assert "50d" in failure.message


def test_a_certification_window_overlapping_research_is_refused(ws: Workspace) -> None:
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2024-12-01", "end": "2025-05-30"},
    }

    failure = refused(ws, document(windows=windows))

    assert "certification.start" in failure.message
    assert "overlaps the research window" in failure.message


def test_a_forward_window_overlapping_certification_is_refused(ws: Workspace) -> None:
    windows = {**DOCUMENT["windows"], "forward": {"start": "2025-05-01"}}

    failure = refused(ws, document(windows=windows))

    assert "forward.start" in failure.message
    assert "overlaps the certification window" in failure.message


def test_a_window_that_ends_before_it_starts_is_refused(ws: Workspace) -> None:
    windows = {
        **DOCUMENT["windows"],
        "research": {"start": "2024-12-31", "end": "2024-01-02"},
    }

    failure = refused(ws, document(windows=windows))

    assert "end" in failure.message


# -- resolution, data requirements and costs -----------------------------------


def test_a_bar_resolution_without_bars_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(resolution="1m", data_requirements=["quote"]))

    assert "data_requirements" in failure.message
    assert "bar" in failure.message


def test_a_tick_resolution_without_ticks_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(resolution="tick", data_requirements=["bar"]))

    assert "data_requirements" in failure.message
    assert "'trade', 'quote' or 'book'" in failure.message


def test_a_grain_resolution_absent_from_the_requirements_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(resolution="quote", data_requirements=["trade"]))

    assert "data_requirements" in failure.message
    assert "'quote'" in failure.message


def test_a_data_requirement_naming_no_known_type_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(data_requirements=["bar", "orderbook"]))

    assert failure.message.startswith("data_requirements:")
    assert "orderbook" in failure.message


def test_a_repeated_data_requirement_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(data_requirements=["bar", "bar"]))

    assert "data_requirements" in failure.message
    assert "repeats" in failure.message


def test_depth_without_the_book_it_views_is_refused(ws: Workspace) -> None:
    depth = {"every_ms": 100, "levels": 3}
    failure = refused(ws, document(resolution="tick", data_requirements=["trade"], depth=depth))

    assert "depth: needs 'book' in data_requirements" in failure.message


def test_depth_beside_a_quote_series_is_refused(ws: Workspace) -> None:
    depth = {"every_ms": 100, "levels": 3}
    failure = refused(
        ws, document(resolution="tick", data_requirements=["book", "quote"], depth=depth)
    )

    assert "depth: refuses 'quote' in data_requirements" in failure.message


@pytest.mark.parametrize("levels", [0, 401])
def test_depth_levels_outside_one_to_four_hundred_are_refused(ws: Workspace, levels: int) -> None:
    depth = {"every_ms": 100, "levels": levels}
    failure = refused(
        ws, document(resolution="tick", data_requirements=["book", "trade"], depth=depth)
    )

    assert "depth.levels" in failure.message


def test_depth_over_a_book_is_admissible(ws: Workspace) -> None:
    depth = {"every_ms": 100, "levels": 3}
    parsed = accepted(
        ws,
        document(
            resolution="tick",
            data_requirements=["book", "trade"],
            depth=depth,
            costs={**DOCUMENT["costs"], "latency_ms": 20},
        ),
    )

    assert parsed.depth is not None and parsed.depth.every_ms == 100


def test_a_spread_read_from_quotes_needs_quotes(ws: Workspace) -> None:
    costs = {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "quotes"}

    failure = refused(ws, document(costs=costs))

    assert "costs.spread:" in failure.message
    assert "quote" in failure.message


def test_a_fixed_spread_needs_its_width(ws: Workspace) -> None:
    costs = {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "fixed_bps"}

    failure = refused(ws, document(costs=costs))

    assert "costs.fixed_bps: required when spread is fixed_bps" in failure.message


def test_a_cost_model_that_cannot_be_completed_is_refused(ws: Workspace) -> None:
    # No override, no broker declaration and no quotes to take a spread from.
    failure = refused(ws, document(costs=None))

    assert failure.message.startswith("costs.fixed_bps:")
    assert "no quotes" in failure.message


def test_quotes_complete_the_cost_model_without_a_width(ws: Workspace) -> None:
    doc = document(costs={"spread": "quotes"}, data_requirements=["bar", "quote"], resolution="1m")

    assert accepted(ws, doc) is not None


def test_a_quoted_spread_with_no_width_over_a_name_without_quotes_is_named(
    ws: Workspace,
) -> None:
    """A fill in a name read as bars alone is struck at a close, which carries no spread, and
    has no quote before it to read one from: under `spread: quotes` with no width it is charged
    none. The file cannot say whether the strategy trades the name, and a signal is charged
    nothing either way, so it is admitted and named, with the width as the remedy."""
    doc = document(
        resolution="1m",
        universe=["DEMO", "EURO"],
        data_requirements=["bar", "quote"],
        data_by_instrument={"DEMO": ["quote"], "EURO": ["bar"]},
        costs={"commission_bps": 0.0, "slippage_bps": 1.0, "spread": "quotes"},
    )

    (warning,) = hyp.admit(ws, write_hypothesis(ws, doc)).warnings

    assert warning == (
        "costs.spread: quotes on XETR reads a fill's half-spread from the quote before it, and "
        "EURO is asked for no quote or book, so a fill there is charged no spread; if the "
        "strategy trades it, state fixed_bps in the hypothesis costs or under "
        "venues.XETR.costs in portfolio.yaml — a fill with no quote before it pays half of it "
        "— or ask it for quote under data_by_instrument"
    )


def test_a_trigger_read_as_prints_beside_a_follower_read_as_quotes_is_admitted(
    ws: Workspace,
) -> None:
    """The shape `kanso screen draft` writes for a cell whose trigger is one name's prints and
    whose follower is another's quotes, under the default spread from quotes: the trigger has
    no quote before a fill and the file cannot say it is never traded. It is admitted, named
    twice — no spread there, and the tape kept on touch — where a refusal once held the signal
    to a width it is never charged."""
    doc = document(
        resolution="trade",
        universe=["DEMO", "EURO"],
        data_requirements=["quote", "trade"],
        data_by_instrument={"DEMO": ["trade"], "EURO": ["quote"]},
        costs={"latency_ms": 5.0},
    )

    admitted = hyp.admit(ws, write_hypothesis(ws, doc))

    assert {model["costs"]["spread"] for model in resolved(ws, doc).values()} == {"quotes"}
    touch, spread = admitted.warnings
    assert touch.startswith("costs.limit_fill: touch on SIM, XETR,")
    assert ": DEMO is asked for trade; EURO is asked for quote. The print rules" in touch
    assert spread.startswith("costs.spread: quotes on SIM reads a fill's half-spread")
    assert "and DEMO is asked for no quote or book, so a fill there is charged no spread" in spread


def test_a_stated_width_prices_a_name_without_quotes_under_a_quoted_spread(ws: Workspace) -> None:
    parsed = accepted(
        ws,
        document(
            resolution="1m",
            universe=["DEMO", "EURO"],
            data_requirements=["bar", "quote"],
            data_by_instrument={"DEMO": ["quote"], "EURO": ["bar"]},
            costs={"slippage_bps": 1.0, "spread": "quotes", "fixed_bps": 4.0},
        ),
    )

    assert parsed.costs is not None and parsed.costs.fixed_bps == 4.0


# -- the universe --------------------------------------------------------------


def test_a_repeated_universe_id_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["DEMO", "DEMO"]))

    assert "universe" in failure.message
    assert "repeats" in failure.message


def test_an_unknown_universe_id_is_refused_naming_it(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["NOPE"]))

    assert failure.message.startswith("NOPE:")
    assert "no entry in instruments.yaml" in failure.message


def test_an_instrument_delisted_before_the_research_window_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["GONE"]))

    assert failure.message.startswith("GONE:")
    assert "delisted 2023-06-30, before 2024-01-02" in failure.message


def test_an_instrument_listed_after_the_research_window_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["LATE"]))

    assert failure.message.startswith("LATE:")
    assert "listed after 2024-01-02" in failure.message


def test_every_failing_universe_id_is_reported_together(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["GONE", "LATE"]))

    assert "GONE" in failure.message
    assert "LATE" in failure.message


def test_a_universe_spanning_two_account_currencies_is_refused(ws: Workspace) -> None:
    write_portfolio(ws, {"XETR": {"currency": "EUR"}})

    failure = refused(ws, document(universe=["DEMO", "EURO"]))

    assert failure.message.startswith("universe:")
    assert "more than one account currency" in failure.message


@pytest.mark.parametrize(
    ("book", "moved"),
    [
        ({"reset": "monthly"}, "a monthly reset"),
        ({"financing_rate_bps": 25}, "a financing carry"),
        ({"reset": "monthly", "financing_rate_bps": 25}, "a monthly reset and a financing carry"),
    ],
)
def test_a_policy_that_moves_money_is_refused_on_a_cash_account(
    ws: Workspace, book: dict[str, Any], moved: str
) -> None:
    """A cash venue cannot fund a restore from the cushion or hold a borrowed notional."""
    write_portfolio(ws, {"SIM": {"account": "cash"}})

    failure = refused(ws, document(book=book))

    assert failure.message == (
        f"book: {moved} needs a margin account, and SIM resolves to a cash account, which "
        "cannot fund a restore or hold a borrowed notional"
    )
    assert failure.remedy == (
        "set venues.SIM.account to margin in portfolio.yaml, or set book.reset to none and "
        "book.financing_rate_bps to 0"
    )


def test_a_maintenance_floor_alone_is_admissible_on_a_cash_account(ws: Workspace) -> None:
    """A floor moves no money: it is a gate on what the book held."""
    write_portfolio(ws, {"SIM": {"account": "cash"}})

    parsed = accepted(ws, document(book={"maintenance_pct": 40}))

    assert parsed.book is not None and parsed.book.maintenance_pct == 40.0


def test_a_policy_that_moves_money_is_admissible_on_a_margin_account(ws: Workspace) -> None:
    parsed = accepted(ws, document(book={"reset": "monthly", "financing_rate_bps": 25}))

    assert parsed.book is not None and parsed.book.funded


PRINT_COSTS = {
    "commission_bps": 0.5,
    "slippage_bps": 1.0,
    "spread": "fixed_bps",
    "fixed_bps": 2,
    "latency_ms": 20,
}


@pytest.mark.parametrize("rule", ["print_through", "print_through_whole"])
@pytest.mark.parametrize(
    ("resolution", "requirements", "missing"),
    [("1d", ["bar"], "quote or trade"), ("tick", ["quote"], "trade")],
)
def test_a_print_rule_without_quotes_and_prints_is_refused(
    ws: Workspace, rule: str, resolution: str, requirements: list[str], missing: str
) -> None:
    """The rule fills a resting limit on prints and a taker on quotes, so it needs both."""
    failure = refused(
        ws,
        document(
            resolution=resolution,
            data_requirements=requirements,
            costs={**PRINT_COSTS, "limit_fill": rule},
        ),
    )

    assert failure.message.startswith(f"costs.limit_fill: {rule} on SIM fills a resting limit")
    assert failure.message.endswith(f"data_requirements has no {missing}")
    assert failure.remedy == (
        "add quote and trade to data_requirements, or state limit_fill: touch or through"
    )


def test_a_print_rule_over_a_level_two_book_is_refused(ws: Workspace) -> None:
    failure = refused(
        ws,
        document(
            resolution="tick",
            data_requirements=["quote", "trade", "book"],
            costs={**PRINT_COSTS, "limit_fill": "print_through"},
        ),
    )

    assert "is a rule for the top-of-book venue" in failure.message
    assert failure.remedy.startswith("drop book from data_requirements")


def test_a_print_rule_stated_for_the_venue_alone_is_refused_the_same_way(ws: Workspace) -> None:
    write_portfolio(ws, {"SIM": {"costs": {"limit_fill": "print_through"}}})

    failure = refused(ws, document(data_requirements=["bar"]))

    assert failure.message.startswith("costs.limit_fill: print_through on SIM")


def test_a_print_rule_over_quotes_and_prints_is_admissible_beside_a_print_only_signal(
    ws: Workspace,
) -> None:
    """An instrument asked only for its prints is a signal the sleeve never trades; were it
    traded, a market order on it would be refused for want of a quote."""
    parsed = accepted(
        ws,
        document(
            resolution="tick",
            universe=["DEMO", "EURO"],
            data_requirements=["quote", "trade"],
            data_by_instrument={"EURO": ["trade"]},
            costs={**PRINT_COSTS, "limit_fill": "print_through"},
        ),
    )

    assert parsed.costs is not None and parsed.costs.limit_fill == "print_through"


def test_a_print_rule_over_bars_beside_ticks_is_told_to_drop_their_size_with_them(
    ws: Workspace,
) -> None:
    """At tick resolution the bars are read at `bar_resolution`, so dropping them leaves a size
    nothing is read at: the remedy drops both, and stays at the resolution the file has."""
    failure = refused(
        ws,
        document(
            resolution="tick",
            bar_resolution="1m",
            data_requirements=["bar", "quote", "trade"],
            costs={**PRINT_COSTS, "limit_fill": "print_through", "latency_ms": 20},
        ),
    )

    assert "DEMO is asked for bar beside quote" in failure.message
    assert failure.remedy == (
        "ask each instrument that carries quotes for quote and trade alone under "
        "data_by_instrument, keeping bar for an instrument asked for bar alone; with none "
        "left, drop bar from data_requirements, and bar_resolution with it; or state "
        "limit_fill: touch or through"
    )


@pytest.mark.parametrize("rule", ["print_through", "print_through_whole"])
def test_a_print_rule_over_bars_beside_quotes_is_refused(ws: Workspace, rule: str) -> None:
    """A bar walks the engine's own bid and ask through its prices, past the quote a taker is
    filled on, so a market order sent on one would fill on a quote the market has left and a
    limit that quote makes marketable would rest for a later print."""
    failure = refused(
        ws,
        document(
            resolution="1m",
            data_requirements=["bar", "quote", "trade"],
            costs={**PRINT_COSTS, "limit_fill": rule},
        ),
    )

    assert failure.message == (
        f"costs.limit_fill: {rule} on SIM fills a taker on the quote in force, and DEMO is asked "
        "for bar beside quote: a bar moves the book the venue judges a taker marketable from "
        "past that quote"
    )
    assert failure.remedy == (
        "ask each instrument that carries quotes for quote and trade alone under "
        "data_by_instrument, keeping bar for an instrument asked for bar alone; with none "
        "left, drop bar from data_requirements and research at resolution: tick, since a "
        "bar size requires bar; or state limit_fill: touch or through"
    )


@pytest.mark.parametrize("rule", ["print_through", "print_through_whole"])
def test_the_remedy_for_bars_beside_quotes_admits_a_one_name_minute_hypothesis(
    ws: Workspace, rule: str
) -> None:
    """A one-name hypothesis at a minute's resolution, asking its name for bars, quotes and
    prints, is refused under a print rule. Asking the name for quote and trade alone leaves
    bar asked of nothing, which is refused, and dropping bar leaves a bar-size resolution
    without bars, which is refused too; the remedy names the way that is admitted, the same
    feeds at resolution tick."""
    costs = {**PRINT_COSTS, "limit_fill": rule}
    minute = {"resolution": "1m", "costs": costs}
    beside = refused(ws, document(**minute, data_requirements=["bar", "quote", "trade"]))
    unasked = refused(
        ws,
        document(
            **minute,
            data_requirements=["bar", "quote", "trade"],
            data_by_instrument={"DEMO": ["quote", "trade"]},
        ),
    )
    barless = refused(ws, document(**minute, data_requirements=["quote", "trade"]))

    parsed = accepted(
        ws, document(resolution="tick", data_requirements=["quote", "trade"], costs=costs)
    )

    assert "DEMO is asked for bar beside quote" in beside.message
    assert "resolution: tick" in beside.remedy
    assert "no instrument is asked for bar" in unasked.message
    assert "resolution 1m is a bar size" in barless.message
    assert parsed.resolution == "tick" and parsed.costs is not None
    assert parsed.costs.limit_fill == rule


def test_a_print_rule_is_admissible_beside_a_bar_only_signal(ws: Workspace) -> None:
    """Bars of an instrument that carries no quotes move no quote in force: a market order on
    it is refused for want of one, and no bar fills a resting limit."""
    parsed = accepted(
        ws,
        document(
            resolution="1m",
            universe=["DEMO", "EURO"],
            data_requirements=["bar", "quote", "trade"],
            data_by_instrument={"DEMO": ["quote", "trade"], "EURO": ["bar"]},
            costs={**PRINT_COSTS, "limit_fill": "print_through"},
        ),
    )

    assert parsed.costs is not None and parsed.costs.limit_fill == "print_through"


# -- tick resolution: the fill rule, the latency and a maker schedule ---------------

TICK_COSTS = {"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "quotes", "latency_ms": 20}


def tick(**changes: Any) -> dict[str, Any]:
    """A hypothesis over DEMO's quotes and prints at tick resolution, every cost but those
    `changes` names stated; a route of 20 ms, and bars, where they are required, read a minute
    at a time."""
    costs = {**TICK_COSTS, **changes.pop("costs", {})}
    fields = {"resolution": "tick", "data_requirements": ["quote", "trade"], **changes}
    if "bar" in fields["data_requirements"]:
        fields.setdefault("bar_resolution", "1m")
    return document(**fields, costs=costs)


BOTH_FEEDS = (
    "ask every name, traded or not, for quote and trade under data_by_instrument, keeping every "
    "other type it is asked for"
)
"""The way to the print rule the names leave open where `bar` is not required."""

BOTH_FEEDS_BARLESS = (
    "ask every name, traded or not, for quote and trade and not for bar under "
    "data_by_instrument, keeping every other type it is asked for, and drop bar from "
    "data_requirements, and bar_resolution with it"
)
"""The same way where `bar` is required, by a name beside its quotes or by a signal alone."""


def both_feeds(doc: dict[str, Any]) -> dict[str, Any]:
    """`doc` with the way to the print rule taken as it is worded: every name asked for quote
    and trade, each keeping every other type it was asked for but bar, and bar dropped from
    data_requirements."""
    required = doc["data_requirements"]
    narrowed = doc.get("data_by_instrument", {})
    feeds = ["quote", "trade"]
    asked = {
        name: feeds + [kind for kind in narrowed.get(name, required) if kind not in {*feeds, "bar"}]
        for name in doc["universe"]
    }
    kept = [kind for kind in required if kind != "bar"]
    rest = {key: value for key, value in doc.items() if key != "bar_resolution"}
    return {**rest, "data_requirements": kept, "data_by_instrument": asked}


@pytest.mark.parametrize(
    ("doc", "rule"),
    [
        (tick(), "print_through"),
        (tick(resolution="quote"), "print_through"),
        (tick(data_requirements=["quote"]), "touch"),
        (tick(data_requirements=["quote", "trade", "bar"]), "touch"),
        (
            tick(
                data_requirements=["book", "trade"], costs={"spread": "fixed_bps", "fixed_bps": 2}
            ),
            "touch",
        ),
        (document(), "touch"),
        (tick(costs={"limit_fill": "touch"}), "touch"),
        (tick(costs={"limit_fill": "through"}), "through"),
    ],
    ids=[
        "tick",
        "quote",
        "no prints",
        "bars beside quotes",
        "book",
        "bars",
        "touch stated",
        "through stated",
    ],
)
def test_the_fill_rule_no_layer_states_follows_the_resolution(
    ws: Workspace, doc: dict[str, Any], rule: str
) -> None:
    """Quotes and prints at tick resolution resolve to the print rule; bars, a book and a tape
    with no prints resolve to touch; a rule stated at a layer stands."""
    (model,) = resolved(ws, doc).values()

    assert model["costs"]["limit_fill"] == rule


def test_the_print_rule_default_leaves_the_costs_origin_where_the_layers_leave_it(
    ws: Workspace,
) -> None:
    (model,) = resolved(ws, tick()).values()

    assert model["origins"]["costs"] == "hypothesis"


@pytest.mark.parametrize(
    ("narrowed", "asked"),
    [
        ({"EURO": ["trade"]}, "EURO is asked for trade"),
        ({"EURO": ["quote"]}, "EURO is asked for quote"),
    ],
    ids=["prints alone", "quotes alone"],
)
def test_a_name_asked_for_one_feed_keeps_every_venue_on_touch_and_is_named(
    ws: Workspace, narrowed: dict[str, list[str]], asked: str
) -> None:
    """Under the print rules a taker fills only on a quote in force and a resting limit only on
    a print, so a name asked for prints alone never takes and one asked for quotes alone never
    rests to a fill. The default stays touch, on which the same file fills, and the warning
    names the name that kept it there. Until the default read each name, both resolved to
    print_through on both venues with no word."""
    doc = tick(
        universe=["DEMO", "EURO"],
        data_by_instrument={"DEMO": ["quote", "trade"], **narrowed},
        costs={"spread": "fixed_bps", "fixed_bps": 0},
    )

    models = resolved(ws, doc)
    admitted = hyp.admit(ws, write_hypothesis(ws, doc))

    assert {venue: model["costs"]["limit_fill"] for venue, model in models.items()} == {
        "SIM": "touch",
        "XETR": "touch",
    }
    (warning,) = admitted.warnings
    assert warning.startswith("costs.limit_fill: touch on SIM, XETR, which credits a resting")
    assert f": {asked}. The print rules fill a taker only on a quote in force" in warning
    assert "bar beside quote" not in warning
    assert warning.endswith(
        "; to move to print_through, ask every name, traded or not, for quote and trade under "
        "data_by_instrument, keeping every other type it is asked for, or, if each name short "
        "of a feed is a signal the strategy never trades, state limit_fill: print_through, "
        "under which a market order in a name asked for no quote, and a resting limit in one "
        "asked for no trade, never fills; to stay on touch without this warning, state "
        "limit_fill: touch"
    )


def test_asking_the_traded_names_alone_for_both_feeds_keeps_the_default_on_touch(
    ws: Workspace,
) -> None:
    """The default reads every name, traded or not: a signal asked for prints alone beside a
    traded name asked for both keeps the tape on touch, and the warning names the signal.
    Its remedy once told the operator to ask each name the strategy trades for both feeds,
    which this file already does."""
    doc = tick(
        universe=["DEMO", "EURO"],
        data_by_instrument={"DEMO": ["trade"], "EURO": ["quote", "trade"]},
        costs={"spread": "fixed_bps", "fixed_bps": 0},
    )

    (warning,) = hyp.admit(ws, write_hypothesis(ws, doc)).warnings

    assert {model["costs"]["limit_fill"] for model in resolved(ws, doc).values()} == {"touch"}
    assert ": DEMO is asked for trade. The print rules" in warning
    assert f"to move to print_through, {BOTH_FEEDS}" in warning


@pytest.mark.parametrize(
    ("way", "rule", "warned"),
    [
        (
            {"data_by_instrument": {"DEMO": ["quote", "trade"], "EURO": ["quote", "trade"]}},
            "print_through",
            1,
        ),
        ({"costs": {"limit_fill": "print_through"}}, "print_through", 1),
        ({"costs": {"limit_fill": "touch"}}, "touch", 0),
    ],
    ids=["both feeds", "print rule stated", "touch stated"],
)
def test_each_way_the_touch_warning_offers_ends_it(
    ws: Workspace, way: dict[str, Any], rule: str, warned: int
) -> None:
    """A signal asked for prints alone: asking it for both feeds, or stating the print rule
    over it, moves both venues to print_through, where the only warning left is the one for
    a print rule with no maker schedule; stating touch keeps the rule and draws none."""
    changes: dict[str, Any] = {
        "universe": ["DEMO", "EURO"],
        "data_by_instrument": {"DEMO": ["trade"], "EURO": ["quote", "trade"]},
        **way,
    }
    changes["costs"] = {"spread": "fixed_bps", "fixed_bps": 0, **way.get("costs", {})}
    doc = tick(**changes)

    admitted = hyp.admit(ws, write_hypothesis(ws, doc))

    assert {model["costs"]["limit_fill"] for model in resolved(ws, doc).values()} == {rule}
    assert len(admitted.warnings) == warned
    assert not any(warning.startswith("costs.limit_fill") for warning in admitted.warnings)


def test_every_name_asked_for_quotes_and_prints_resolves_to_the_print_rule(ws: Workspace) -> None:
    doc = tick(
        universe=["DEMO", "EURO"],
        data_by_instrument={"DEMO": ["quote", "trade"], "EURO": ["trade", "quote"]},
    )

    assert {model["costs"]["limit_fill"] for model in resolved(ws, doc).values()} == {
        "print_through"
    }


def test_a_bar_only_name_at_tick_resolution_keeps_the_default_on_touch(ws: Workspace) -> None:
    """No bar fills a resting limit under the print rules and a bar carries no quote for a
    taker to fill on, so a name read as bars alone keeps the tape on touch as well."""
    doc = tick(
        universe=["DEMO", "EURO"],
        data_requirements=["quote", "trade", "bar"],
        data_by_instrument={"DEMO": ["quote", "trade"], "EURO": ["bar"]},
        costs={"spread": "fixed_bps", "fixed_bps": 0},
    )

    admitted = hyp.admit(ws, write_hypothesis(ws, doc))

    assert {model["costs"]["limit_fill"] for model in resolved(ws, doc).values()} == {"touch"}
    assert ": EURO is asked for bar. The print rules" in admitted.warnings[0]


def test_a_name_asked_for_bars_beside_quotes_and_prints_is_named_for_its_bars(
    ws: Workspace,
) -> None:
    """A name asked for bars beside its quotes and prints holds both feeds, so the print rule
    would not starve it: a bar walks the engine's bid and ask past the quote in force, and a
    print rule over it is refused. The warning says that, and not that a feed is missing. Its
    way to the print rule drops bar from data_requirements too, since no name is left asking
    for it: it once stopped at asking the name for both feeds alone, and the file that did
    only that was refused for a type no name is asked for."""
    admitted = hyp.admit(
        ws, write_hypothesis(ws, tick(data_requirements=["quote", "trade", "bar"]))
    )
    kept = refused(
        ws,
        tick(
            data_requirements=["quote", "trade", "bar"],
            data_by_instrument={"DEMO": ["quote", "trade"]},
        ),
    )
    alone = tick(data_by_instrument={"DEMO": ["quote", "trade"]})

    (warning,) = admitted.warnings
    assert warning == (
        "costs.limit_fill: touch on SIM, which credits a resting limit with no queue ahead of "
        "it, and not print_through, which a hypothesis holding quotes and prints at tick "
        "resolution resolves to when no layer states a rule only if every name is asked for "
        "quote and trade and none for bar: DEMO is asked for quote, trade, bar. A bar walks "
        "the engine's bid and ask past the quote in force, on which the print rules fill a "
        "taker, so a print rule over a name asked for bar beside quote is refused; to move to "
        f"print_through, {BOTH_FEEDS_BARLESS}; to stay on touch without this warning, state "
        "limit_fill: touch"
    )
    assert "data_requirements: no instrument is asked for bar once" in kept.message
    assert {model["costs"]["limit_fill"] for model in resolved(ws, alone).values()} == {
        "print_through"
    }


def test_names_short_of_a_feed_and_barred_beside_quotes_give_both_reasons(
    ws: Workspace,
) -> None:
    doc = tick(
        universe=["DEMO", "EURO"],
        data_requirements=["quote", "trade", "bar"],
        data_by_instrument={"DEMO": ["quote", "trade", "bar"], "EURO": ["trade"]},
        costs={"spread": "fixed_bps", "fixed_bps": 0},
    )

    (warning,) = hyp.admit(ws, write_hypothesis(ws, doc)).warnings

    assert ": DEMO is asked for quote, trade, bar; EURO is asked for trade. The print " in warning
    assert "could not fill as it does under touch, and a bar walks the engine's bid" in warning


@pytest.mark.parametrize("layer", ["hypothesis", "portfolio"])
def test_a_stated_touch_over_a_one_feed_name_draws_no_warning(ws: Workspace, layer: str) -> None:
    """A rule a layer states is the operator's: the warning says why the default stayed on
    touch, and its remedy, asking the names for both feeds, cannot move a stated one."""
    stated = {"limit_fill": "touch"}
    if layer == "portfolio":
        write_portfolio(ws, {"SIM": {"costs": stated}, "XETR": {"costs": stated}})
    doc = tick(
        universe=["DEMO", "EURO"],
        data_by_instrument={"DEMO": ["quote", "trade"], "EURO": ["trade"]},
        costs={"spread": "fixed_bps", "fixed_bps": 0, **(stated if layer == "hypothesis" else {})},
    )

    admitted = hyp.admit(ws, write_hypothesis(ws, doc))

    assert {model["costs"]["limit_fill"] for model in resolved(ws, doc).values()} == {"touch"}
    assert admitted.warnings == ()


def test_a_venue_a_layer_states_a_rule_for_is_left_out_of_the_warning(ws: Workspace) -> None:
    """XETR's touch is stated and SIM's is the default the names chose: only SIM is named.
    Stated `through`, XETR was left out by its rule alone, and a warning that named every
    venue on touch passed."""
    write_portfolio(ws, {"XETR": {"costs": {"limit_fill": "touch"}}})
    doc = tick(
        universe=["DEMO", "EURO"],
        data_by_instrument={"DEMO": ["quote", "trade"], "EURO": ["quote"]},
        costs={"spread": "fixed_bps", "fixed_bps": 0},
    )

    (warning,) = hyp.admit(ws, write_hypothesis(ws, doc)).warnings

    assert warning.startswith("costs.limit_fill: touch on SIM, which credits")


def test_a_tape_without_prints_is_told_nothing_about_the_print_rule(ws: Workspace) -> None:
    """A hypothesis that holds no prints was never resolved to the print rule, so no name kept
    it from it."""
    admitted = hyp.admit(ws, write_hypothesis(ws, tick(data_requirements=["quote"])))

    assert admitted.warnings == ()


@pytest.mark.parametrize("resolution", ["tick", "quote", "trade"])
def test_a_latency_of_zero_at_tick_resolution_is_refused(ws: Workspace, resolution: str) -> None:
    """At zero the venue lands an order on the point it was sent from. No latency is named:
    the remedy asks for the measured one."""
    costs = {key: value for key, value in TICK_COSTS.items() if key != "latency_ms"}
    failure = refused(
        ws, document(resolution=resolution, data_requirements=["quote", "trade"], costs=costs)
    )

    assert failure.message == (
        f"costs.latency_ms: 0 on SIM at resolution {resolution} lands every order on the point "
        "it was sent from, before anything later on the tape, which no route to a venue does"
    )
    assert failure.remedy == (
        "state costs.latency_ms — the round trip measured on the account and route you trade "
        "through — in the hypothesis costs or under venues.SIM.costs in portfolio.yaml"
    )


def test_a_latency_stated_for_the_venue_admits_a_tick_hypothesis(ws: Workspace) -> None:
    write_portfolio(ws, {"SIM": {"costs": {"latency_ms": 5}}})
    costs = {key: value for key, value in TICK_COSTS.items() if key != "latency_ms"}

    parsed = accepted(ws, tick(costs=costs | {"latency_ms": None}))

    assert parsed.resolution == "tick"


def test_a_latency_of_zero_on_bars_is_admissible(ws: Workspace) -> None:
    (model,) = resolved(ws, DOCUMENT).values()

    assert model["costs"]["latency_ms"] == 0.0


@pytest.mark.parametrize("rule", ["touch", "through"])
@pytest.mark.parametrize(
    ("schedule", "key"),
    [({"maker_bps": 0.0}, "maker_bps"), ({"maker_per_share": 0.004}, "maker_per_share")],
)
def test_a_maker_schedule_under_a_queue_free_rule_at_tick_resolution_is_refused(
    ws: Workspace, rule: str, schedule: dict[str, float], key: str
) -> None:
    """`touch` and `through` credit a resting limit with no queue ahead of it, so a maker's
    schedule there would price fills the tape did not give."""
    failure = refused(ws, tick(costs={"limit_fill": rule, **schedule}))

    assert failure.message.startswith(
        f"costs.{key}: a maker schedule on SIM under limit_fill: {rule} prices as a maker's"
    )
    assert failure.remedy is not None
    assert failure.remedy.startswith("state limit_fill: print_through or print_through_whole")
    assert "venues.SIM.costs" in failure.remedy


def test_a_maker_schedule_stated_for_the_venue_is_refused_the_same_way(ws: Workspace) -> None:
    write_portfolio(ws, {"SIM": {"costs": {"maker_bps": -0.2}}})

    failure = refused(ws, tick(costs={"limit_fill": "through"}))

    assert failure.message.startswith("costs.maker_bps: a maker schedule on SIM")


@pytest.mark.parametrize(("layer", "venues"), [("hypothesis", "SIM, XETR"), ("portfolio", "SIM")])
def test_a_maker_schedule_on_a_venue_a_one_feed_name_kept_on_touch_names_the_name(
    ws: Workspace, layer: str, venues: str
) -> None:
    """No layer states a rule, and EURO, asked for prints alone, keeps the tape on touch. The
    refusal names it, and its remedy leads with asking every name for both feeds, which admits
    the same schedule under the print rule. It once named neither the name nor that way out,
    and the warning that names the name was never reached."""
    schedule = {"maker_bps": 0.0}
    if layer == "portfolio":
        write_portfolio(ws, {"SIM": {"costs": schedule}})
    costs = {"spread": "fixed_bps", "fixed_bps": 0, **(schedule if layer == "hypothesis" else {})}
    universe = ["DEMO", "EURO"]
    both = ["quote", "trade"]

    failure = refused(
        ws,
        tick(universe=universe, data_by_instrument={"DEMO": both, "EURO": ["trade"]}, costs=costs),
    )
    asked = tick(universe=universe, data_by_instrument={"DEMO": both, "EURO": both}, costs=costs)
    admitted = hyp.admit(ws, write_hypothesis(ws, asked))

    assert failure.message.startswith(f"costs.maker_bps: a maker schedule on {venues} under ")
    assert failure.message.endswith(
        f"; no layer states a rule for {venues}, which a hypothesis holding quotes and prints at "
        "tick resolution resolves to print_through only if every name is asked for quote and "
        "trade and none for bar: EURO is asked for trade"
    )
    assert failure.remedy is not None
    assert failure.remedy.startswith(
        f"{BOTH_FEEDS}, which moves a venue no layer states a rule for to print_through; or "
        "state limit_fill: print_through or print_through_whole"
    )
    assert {model["costs"]["limit_fill"] for model in resolved(ws, asked).values()} == {
        "print_through"
    }
    assert admitted.hyp.id == "demo_mr"


def test_a_maker_schedule_under_a_stated_touch_names_no_name(ws: Workspace) -> None:
    """A stated touch is the operator's, and asking the names for both feeds would not move it,
    so the refusal neither names them nor offers it."""
    doc = tick(
        universe=["DEMO", "EURO"],
        data_by_instrument={"DEMO": ["quote", "trade"], "EURO": ["trade"]},
        costs={"spread": "fixed_bps", "fixed_bps": 0, "maker_bps": 0.0, "limit_fill": "touch"},
    )

    failure = refused(ws, doc)

    assert "EURO" not in failure.message
    assert failure.remedy is not None
    assert failure.remedy.startswith("state limit_fill: print_through or print_through_whole")


def test_a_maker_schedule_over_a_name_asked_for_bar_beside_quote_is_offered_no_print_rule(
    ws: Workspace,
) -> None:
    """A print rule over a name asked for bar beside quote is refused, so the maker refusal
    does not offer one, and asking every name for both feeds and none for bar leaves bar asked
    of none, so its remedy drops bar from data_requirements as well. Followed as written, the
    remedy admits the same schedule under the print rule. It once offered both ways as they
    stood, and each was refused in turn: the stated print rule over DEMO's bars, and the
    narrowed file for a type no name was asked for."""
    schedule = {"maker_bps": 0.0}
    barred = tick(data_requirements=["quote", "trade", "bar"], costs=schedule)

    failure = refused(ws, barred)
    stating = refused(ws, {**barred, "costs": {**barred["costs"], "limit_fill": "print_through"}})
    followed = both_feeds(barred)
    admitted = hyp.admit(ws, write_hypothesis(ws, followed))

    assert failure.message.endswith(": DEMO is asked for quote, trade, bar")
    assert failure.remedy == (
        f"{BOTH_FEEDS_BARLESS}, which moves a venue no layer states a rule for to print_through; "
        "or remove maker_bps and maker_per_share from the hypothesis costs and from "
        "venues.SIM.costs in portfolio.yaml"
    )
    assert "DEMO is asked for bar beside quote" in stating.message
    assert {model["costs"]["limit_fill"] for model in resolved(ws, followed).values()} == {
        "print_through"
    }
    assert admitted.warnings == ()


@pytest.mark.parametrize("rule", ["touch", "through"])
def test_a_maker_schedule_under_a_stated_rule_over_bars_beside_quotes_is_offered_both_ways(
    ws: Workspace, rule: str
) -> None:
    """Asking the names for both feeds cannot move a stated rule, and a print rule is refused
    while DEMO is asked for bar beside quote, so the remedy offers stating one only beside
    asking every name for both feeds and none for bar, which keeps the schedule, or the
    schedule's removal. Each, followed as written, admits the file. The remedy once offered
    the removal alone, and an operator who meant the schedule had no way to it."""
    schedule = {"maker_bps": 0.0}
    barred = tick(data_requirements=["quote", "trade", "bar"], costs={"limit_fill": rule})

    failure = refused(ws, {**barred, "costs": {**barred["costs"], **schedule}})
    followed = both_feeds(barred)
    followed["costs"] = {**followed["costs"], **schedule, "limit_fill": "print_through"}
    kept = hyp.admit(ws, write_hypothesis(ws, followed))
    removed = hyp.admit(ws, write_hypothesis(ws, barred))

    assert failure.remedy == (
        f"{BOTH_FEEDS_BARLESS}, and state limit_fill: print_through or print_through_whole, "
        "which fills a resting limit only on a later print through it and needs every name the "
        "strategy trades asked for quote and trade, and no book; or remove maker_bps and "
        "maker_per_share from the hypothesis costs and from venues.SIM.costs in portfolio.yaml"
    )
    assert {model["costs"]["limit_fill"] for model in resolved(ws, followed).values()} == {
        "print_through"
    }
    assert kept.warnings == ()
    assert removed.warnings == ()


def test_a_maker_schedule_under_a_stated_rule_over_bars_beside_quotes_alone_is_offered_removal(
    ws: Workspace,
) -> None:
    """With trade not required no name can be asked for prints, so the way to the print rule
    is not open and the remedy offers the schedule's removal alone, which admits the file."""
    barred = tick(data_requirements=["quote", "bar"], costs={"limit_fill": "touch"})

    failure = refused(ws, {**barred, "costs": {**barred["costs"], "maker_bps": 0.0}})
    removed = hyp.admit(ws, write_hypothesis(ws, barred))

    assert failure.remedy == (
        "remove maker_bps and maker_per_share from the hypothesis costs and from "
        "venues.SIM.costs in portfolio.yaml"
    )
    assert removed.warnings == ()


IN_PLAY = """
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.types import register_custom_type

PROVIDES = {"data_types": ["kanso_in_play"]}


@customdataclass
class KansoInPlay(Data):
    instrument_id: InstrumentId
    in_play: int


register_custom_type("kanso_in_play", KansoInPlay)
"""
"""An extension registering the custom series a session scope reads its flag off; its own
module name and type id, since a type id names one class for the life of the process."""

SCOPE: dict[str, Any] = {"series": "kanso_in_play", "flag": "in_play", "always": ["DEMO"]}
"""A session scope reading its flag off that series."""


@pytest.mark.parametrize(
    "shape",
    [
        {"data_requirements": ["quote", "trade", "bar", "corporate_action"]},
        {"universe": ["PERP"], "data_requirements": ["quote", "trade", "bar", "funding"]},
        {
            "universe": ["DEMO", "EURO"],
            "data_requirements": ["quote", "trade", "bar", "kanso_in_play"],
            "session_scope": SCOPE,
        },
        {
            "universe": ["DEMO", "EURO"],
            "data_requirements": ["quote", "trade", "corporate_action"],
            "data_by_instrument": {
                "DEMO": ["quote", "trade", "corporate_action"],
                "EURO": ["trade"],
            },
        },
        {
            "universe": ["DEMO", "EURO"],
            "data_requirements": ["quote", "trade", "bar"],
            "data_by_instrument": {"DEMO": ["quote", "trade"], "EURO": ["bar"]},
        },
    ],
    ids=[
        "corporate actions beside bars",
        "a perpetual's funding beside bars",
        "a session scope's series beside bars",
        "corporate actions beside a name short of a feed",
        "a signal asked for bars alone",
    ],
)
def test_the_way_to_the_print_rule_followed_as_worded_admits_any_other_required_type(
    ws: Workspace, shape: dict[str, Any]
) -> None:
    """The warning's way to the print rule and the maker refusal's lead with the same words, and
    followed as worded each admits the file under the print rule with the schedule kept, whatever
    else the names are asked for: the funding every perpetual requires, the series a session
    scope reads, corporate actions. The words once asked every name for quote and trade alone,
    which left such a type asked of no name and was refused for it, and dropped bar only where a
    name was asked for it beside quote, which a signal asked for bars alone is not."""
    if "PERP" in shape.get("universe", []):
        write_instruments(ws, "DEMO", "PERP")
        ws = configured(ws, currency="USDT")
    if "session_scope" in shape:
        extension = ws.root / "kanso_ext"
        extension.mkdir()
        (extension / "kanso_in_play.py").write_text(IN_PLAY, encoding="utf-8")
    way = BOTH_FEEDS_BARLESS if "bar" in shape["data_requirements"] else BOTH_FEEDS
    costs = {"spread": "fixed_bps", "fixed_bps": 0}
    schedule = {"maker_bps": 0.0}
    doc = tick(**shape, costs=costs)

    warnings = hyp.admit(ws, write_hypothesis(ws, doc)).warnings
    failure = refused(ws, tick(**shape, costs={**costs, **schedule}))
    followed = both_feeds(tick(**shape, costs={**costs, **schedule}))
    admitted = hyp.admit(ws, write_hypothesis(ws, followed))

    (warning,) = [warning for warning in warnings if warning.startswith("costs.limit_fill")]
    assert f"; to move to print_through, {way}" in warning
    assert failure.remedy is not None
    assert failure.remedy.startswith(
        f"{way}, which moves a venue no layer states a rule for to print_through; or "
    )
    assert {model["costs"]["limit_fill"] for model in resolved(ws, followed).values()} == {
        "print_through"
    }
    assert admitted.warnings == ()


@pytest.mark.parametrize(
    "doc",
    [
        tick(costs={"maker_bps": 0.0}),
        tick(costs={"maker_bps": 0.0, "limit_fill": "print_through_whole"}),
        tick(
            data_requirements=["book", "trade"],
            costs={"spread": "fixed_bps", "fixed_bps": 2, "maker_bps": 0.0},
        ),
        document(costs={**DOCUMENT["costs"], "maker_bps": 0.0}),
    ],
    ids=["print rule by default", "print rule stated", "level-two book", "bars under touch"],
)
def test_a_maker_schedule_is_admissible_where_resting_fills_are_credited_from_the_tape(
    ws: Workspace, doc: dict[str, Any]
) -> None:
    """The print rules credit a resting limit from a print through it, a level-two book keeps
    a queue, and on bars there is no tape rule to choose instead."""
    admitted = hyp.admit(ws, write_hypothesis(ws, doc))

    assert admitted.warnings == ()


def test_a_print_rule_with_no_maker_schedule_is_admitted_with_a_warning(ws: Workspace) -> None:
    admitted = hyp.admit(ws, write_hypothesis(ws, tick()))

    assert admitted.warnings == (
        "costs: no maker_bps or maker_per_share on SIM under limit_fill: print_through, so a "
        "fill that rested on the book is charged as any fill at its price — slippage_bps and "
        "half the spread on top of the commission; state the account's maker schedule, 0 for "
        "none and negative for a rebate",
    )
    assert hyp.validate(ws, write_hypothesis(ws, tick())).id == admitted.hyp.id


def test_one_account_currency_across_two_venues_is_admissible(ws: Workspace) -> None:
    assert accepted(ws, document(universe=["DEMO", "EURO"])) is not None


# -- the currency each instrument settles in ---------------------------------------


def test_an_equity_quoted_in_another_currency_than_its_venue_s_account_is_refused(
    ws: Workspace,
) -> None:
    """The row the backlog kept open: a EUR leg on a USD account fills in EUR, and the
    engine's conversion error is lost to a logger kanso builds bypassed."""
    write_instruments(ws, "DEMO", "BUND")

    failure = refused(ws, document(universe=["DEMO", "BUND"]))

    assert failure.message == (
        "universe: BUND.XETR settles in EUR, and XETR's account currency is USD; a "
        "hypothesis's fills settle and are booked in the account's own currency"
    )
    assert failure.remedy == (
        "set venues.XETR.currency to EUR in portfolio.yaml, or [research] currency to EUR in "
        "kanso.toml"
    )


@pytest.mark.parametrize("account", ["USDT", "USDC"])
def test_a_perpetual_settled_in_another_stablecoin_than_its_quote_fits_no_account(
    ws: Workspace, account: str
) -> None:
    """USDC and USDT are two codes: the engine calls the pair no quanto, settles the
    contract in USDC and books its positions, PnL and margin in USDT, the quote currency.
    A USDT account would be paid in USDC; a USDC account would be booked in USDT and find no
    USDT/USDC rate to convert at, so the balance update is deferred. No account holds both."""
    write_instruments(ws, "DEMO", "USDC_PERP")

    failure = refused(configured(ws, currency=account), document(universe=["USDC_PERP"]))

    assert failure.message == (
        "universe: BTC-USDT-USDC.SIM settles in USDC and is booked in USDT, and SIM's "
        f"account currency is {account}; a hypothesis's fills settle and are booked in the "
        "account's own currency"
    )
    assert failure.remedy == (
        "remove BTC-USDT-USDC.SIM from `universe` in hypothesis.yaml: no one account currency "
        "is both its settlement currency USDC and its booked currency USDT"
    )


def test_a_perpetual_settled_in_the_account_s_currency_is_admissible(ws: Workspace) -> None:
    write_instruments(ws, "DEMO", "PERP")

    parsed = accepted(
        configured(ws, currency="USDT"),
        document(universe=["PERP"], data_requirements=["bar", "funding"]),
    )

    assert parsed.universe == ["PERP"]


# -- a perpetual's funding ----------------------------------------------------------


def test_a_perpetual_without_its_funding_is_refused(ws: Workspace) -> None:
    """A held perpetual pays or is paid funding every settlement; a card that is not handed
    it measures a P&L the contract never had."""
    write_instruments(ws, "DEMO", "PERP")

    failure = refused(configured(ws, currency="USDT"), document(universe=["PERP"]))

    assert failure.message == (
        "data_requirements: BTCUSDT-PERP.SIM is a perpetual and funding is not required; "
        "a perpetual's P&L is not honest without the funding it paid and was paid"
    )
    assert failure.remedy == (
        "add funding to data_requirements and load its realised funding history"
    )


def test_every_perpetual_missing_its_funding_is_named_together(ws: Workspace) -> None:
    ws.path("instruments.yaml").write_text(
        yaml.safe_dump(
            {"PERP": PERP_ENTRY, "ETH": {**PERP_ENTRY, "nautilus_id": "ETHUSDT-PERP.SIM"}}
        ),
        encoding="utf-8",
    )

    failure = refused(configured(ws, currency="USDT"), document(universe=["PERP", "ETH"]))

    assert failure.message.startswith(
        "data_requirements: BTCUSDT-PERP.SIM, ETHUSDT-PERP.SIM are perpetuals and funding is "
        "not required"
    )


def test_a_perpetual_is_known_by_its_definition_not_its_name(ws: Workspace) -> None:
    """An id that reads like a perpetual is an equity when its definition is one, and a
    perpetual filed under a plain id is still a perpetual."""
    ws.path("instruments.yaml").write_text(
        yaml.safe_dump(
            {
                "DEMO-PERP": {**DEMO_ENTRY, "nautilus_id": "DEMO-PERP.SIM"},
                "COIN": PERP_ENTRY,
            }
        ),
        encoding="utf-8",
    )

    assert accepted(ws, document(universe=["DEMO-PERP"])).universe == ["DEMO-PERP"]
    failure = refused(configured(ws, currency="USDT"), document(universe=["COIN"]))
    assert failure.message.startswith("data_requirements: BTCUSDT-PERP.SIM is a perpetual")


def test_a_usd_instrument_on_a_usdt_account_is_refused(ws: Workspace) -> None:
    """The demo's instrument under an account the workspace moved to USDT."""
    failure = refused(configured(ws, currency="USDT"), DOCUMENT)

    assert failure.message.startswith("universe: DEMO.SIM settles in USD, and SIM's account ")


# -- the account and currency `[research]` states -------------------------------------


def configured(ws: Workspace, **research: str) -> Workspace:
    """The same workspace reopened with these `[research]` keys rewritten in `kanso.toml`."""
    path = ws.path(CONFIG_NAME)
    text = path.read_text(encoding="utf-8")
    for key, value in research.items():
        text, count = re.subn(rf"^{key} = .*$", f'{key} = "{value}"', text, flags=re.M)
        assert count == 1, key
    path.write_text(text, encoding="utf-8")
    return Workspace(root=ws.root, config=load_config(path))


def resolved(ws: Workspace, doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What the card of an accepted hypothesis would be measured under, per venue."""
    parsed = accepted(ws, doc)
    held = resolve_universe(ws, parsed.universe, parsed.windows.research.start, record=False)
    return {venue: m.model_dump(mode="json") for venue, m in venue_models(ws, parsed, held).items()}


TEMPLATE_VENUE_MODEL: dict[str, Any] = {
    "venue": "SIM",
    "account": "margin",
    "default_leverage": 1.0,
    "currency": "USD",
    "costs": {
        "commission_bps": 0.5,
        "commission_per_share": 0.0,
        "slippage_bps": 1.0,
        "slippage_ticks": 0.0,
        "spread": "fixed_bps",
        "fixed_bps": 2.0,
        "maker_bps": None,
        "maker_per_share": None,
        "sell_fee_bps": 0.0,
        "sell_fee_per_share": 0.0,
        "limit_fill": "touch",
        "latency_ms": 0.0,
    },
    "origins": {"account": "default", "currency": "default", "costs": "hypothesis"},
}
"""The demo hypothesis's venue model as a template workspace resolved it on 0.13.0, before
`[research]` became a layer, less the broker the template names."""


def test_a_template_workspace_resolves_the_model_it_did_before_the_configuration_layer(
    ws: Workspace,
) -> None:
    """`account = "margin"` and `currency = "USD"` restate the defaults, so they are not a
    layer: the origins still read `default` and no card anchor moves."""
    (model,) = resolved(ws, DOCUMENT).values()

    assert model == {**TEMPLATE_VENUE_MODEL, "broker": ws.config.research.broker}


def test_a_configured_currency_the_engine_registers_reaches_the_card(ws: Workspace) -> None:
    write_instruments(ws, "DEMO", "PERP")
    (model,) = resolved(
        configured(ws, currency="USDT"),
        document(universe=["PERP"], data_requirements=["bar", "funding"]),
    ).values()

    assert model["currency"] == "USDT"
    assert model["origins"]["currency"] == "config"
    assert model["origins"]["account"] == "default"


def test_a_configured_cash_account_reaches_the_card(ws: Workspace) -> None:
    (model,) = resolved(configured(ws, account="cash"), DOCUMENT).values()

    assert (model["account"], model["default_leverage"]) == ("cash", None)
    assert model["origins"]["account"] == "config"


def test_a_configured_currency_the_engine_does_not_register_is_refused(ws: Workspace) -> None:
    """The grammar admits USTD, and only the engine knows it registers nothing by that name."""
    failure = refused(configured(ws, currency="USTD"), DOCUMENT)

    assert (
        failure.message
        == "currency: 'USTD' is not a code the engine registers, as fiat or as crypto"
    )
    assert failure.remedy is not None
    assert "[research] currency" in failure.remedy
    assert "kanso.toml" in failure.remedy
    assert "venues.<MIC>.currency" in failure.remedy


def test_validation_writes_neither_the_store_nor_the_cache(ws: Workspace) -> None:
    """A validation that left something behind would not be one.

    The universe is resolved to be checked, and afterwards the catalog's instrument
    store — the registry of record, which `kanso data instruments resolve` alone writes —
    and the cache are exactly as they were.
    """
    from kanso.data.instruments import read_store

    before = ws.path("instruments.yaml").read_bytes()

    accepted(ws, DOCUMENT)

    assert not ws.path("catalog", "data").exists()
    assert read_store(ws) == {}
    assert ws.path("instruments.yaml").read_bytes() == before


# -- where the file lives ------------------------------------------------------


def test_a_hypothesis_lives_in_the_directory_its_id_names(ws: Workspace) -> None:
    failure = refused(ws, DOCUMENT, hyp_id="somewhere_else")

    assert failure.message.startswith("id:")
    assert "somewhere_else" in failure.message


def test_a_hypothesis_may_not_be_called_portfolio(ws: Workspace) -> None:
    # The id grammar admits the word, but it is how `construct.host` names the book, and a
    # certified sleeve composes a strategy named after its hypothesis.
    failure = refused(ws, document(id="portfolio"))

    assert failure.message.startswith("id:")
    assert "names its host" in failure.message
    assert failure.remedy is not None
    assert "hypotheses/portfolio/" in failure.remedy


def test_a_file_outside_the_hypotheses_tree_is_judged_on_its_content_alone(
    ws: Workspace,
) -> None:
    import yaml

    path = ws.path("elsewhere.yaml")
    path.write_text(yaml.safe_dump(DOCUMENT), encoding="utf-8")

    assert hyp.validate(ws, path).id == "demo_mr"


def test_a_file_that_cannot_be_read_is_a_validation_failure(ws: Workspace) -> None:
    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, ws.path("hypotheses", "absent", "hypothesis.yaml"))

    assert failure.value.code is Exit.VALIDATION
    assert "cannot be read" in failure.value.message


def test_a_file_that_is_not_utf8_is_a_validation_failure(ws: Workspace) -> None:
    path = ws.path("hypotheses", "demo_mr", "hypothesis.yaml")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"schema: 1\nid: \xff\xfe\n")

    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, path)

    assert failure.value.code is Exit.VALIDATION
    assert "not UTF-8" in failure.value.message


# -- the classification --------------------------------------------------------


def test_a_construct_outside_the_catalogue_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "construct": {"id": "invented"},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct:")
    assert "not in the catalogue" in failure.message


def test_a_sleeve_that_names_a_host_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "construct": {"id": "sleeve", "host": HOST_ID},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.host:")
    assert "attaches to nothing" in failure.message


def test_an_attached_construct_with_no_host_is_refused(ws: Workspace) -> None:
    classification = {
        **FILTER_CLASSIFICATION,
        "construct": {"id": "filter", "params": {"scope": "time"}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.host:")
    assert "attaches to a sleeve" in failure.message


def test_a_host_that_is_not_a_certified_strategy_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert failure.message.startswith("construct.host:")
    assert "not a certified strategy" in failure.message


def test_a_host_file_declaring_another_strategy_is_refused(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID, declared_id="other_sleeve")

    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert failure.message.startswith("construct.host:")
    assert "other_sleeve" in failure.message


def test_a_construct_parameter_outside_its_declared_set_is_refused(ws: Workspace) -> None:
    # The host is certified and the objective applies: the value is the only thing wrong.
    write_strategy(ws, HOST_ID)
    classification = {
        **FILTER_CLASSIFICATION,
        "construct": {"id": "filter", "host": HOST_ID, "params": {"scope": "sideways"}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message == "construct.params.scope: 'sideways' is not one of time, instrument"


def test_a_construct_parameter_the_construct_does_not_declare_is_refused(
    ws: Workspace,
) -> None:
    write_strategy(ws, HOST_ID)
    classification = {
        **FILTER_CLASSIFICATION,
        "construct": {"id": "filter", "host": HOST_ID, "params": {"window": "time"}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.params:")
    assert "has no parameter 'window'" in failure.message


def test_a_portfolio_construct_attaches_to_the_book(ws: Workspace) -> None:
    classification = {
        "construct": {"id": "allocation", "host": "portfolio"},
        "objective": {"id": "marginal_net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
        "constraints": [{"id": "strategy_integrity"}],
    }

    assert accepted(ws, document(**classification)) is not None


def test_a_portfolio_construct_named_onto_a_sleeve_is_refused(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID)
    classification = {
        "construct": {"id": "allocation", "host": HOST_ID},
        "objective": {"id": "marginal_net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
        "constraints": [{"id": "strategy_integrity"}],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.host:")
    assert "attaches to the book" in failure.message


def test_an_objective_outside_the_toolbox_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {"id": "invented", "params": {"min_delta": 0.5, "k_se": 1.0}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("objective.id:")
    assert "not an objective in the toolbox" in failure.message


def test_an_objective_that_does_not_apply_is_refused(ws: Workspace) -> None:
    # wf_sharpe_net wants a holding period of a day or more; this one holds 30 minutes.
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.5, "k_se": 1.0}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("objective.id:")
    assert "does not apply" in failure.message
    assert "net_edge_bps" in failure.message
    assert (failure.remedy or "").startswith("name one of the applicable objectives")


def test_a_relative_objective_on_a_sleeve_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {
            "id": "marginal_net_edge_bps",
            "params": {"min_delta": 0.5, "k_se": 1.0},
        },
    }

    failure = refused(ws, document(**classification))

    assert "does not apply" in failure.message


DAILY_SLEEVE: dict[str, Any] = {
    "horizon": "1d",
    "resolution": "1d",
    "construct": {"id": "sleeve", "rationale": "a strategy of its own"},
    "constraints": [{"id": "strategy_integrity"}],
}


def test_a_daily_sleeve_measured_against_a_hold_is_admissible(ws: Workspace) -> None:
    parsed = accepted(
        ws,
        document(
            benchmark={"hold": "first_leg"},
            objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 1.0}},
            **DAILY_SLEEVE,
        ),
    )

    assert parsed.benchmark is not None and parsed.benchmark.hold == "first_leg"


def test_a_benchmark_takes_the_absolute_sharpe_s_place(ws: Workspace) -> None:
    """A declared benchmark and an objective that ignores it are two answers to one question."""
    failure = refused(
        ws,
        document(
            benchmark={"hold": "first_leg"},
            objective={"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 1.0}},
            **DAILY_SLEEVE,
        ),
    )

    assert failure.message.startswith("objective.id: 'wf_sharpe_net' does not apply")
    assert "wf_sharpe_vs_hold" in failure.message
    assert failure.remedy == "set objective.id to wf_sharpe_vs_hold, or remove benchmark"


def test_removing_a_benchmark_moves_the_objective_back(ws: Workspace) -> None:
    failure = refused(
        ws,
        document(
            objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 1.0}},
            **DAILY_SLEEVE,
        ),
    )

    assert failure.message.startswith("objective.id: 'wf_sharpe_vs_hold' does not apply")
    assert failure.remedy == (
        "declare `benchmark: {hold: first_leg}`, or set objective.id to wf_sharpe_net"
    )


def test_a_benchmark_below_a_day_is_refused_before_classification(ws: Workspace) -> None:
    """A sub-daily hypothesis is measured per trade, and a hold has no trades to pair with.

    Refused on the draft, so `kanso classify` never asks a model to classify it.
    """
    draft = refused(ws, document(benchmark={"hold": "first_leg"}))
    classified = refused(ws, document(benchmark={"hold": "first_leg"}, **SLEEVE_CLASSIFICATION))

    assert draft.message.startswith("benchmark: declared on a 30m horizon")
    assert (
        draft.remedy == "remove benchmark from this file, or lengthen the horizon to a day or more"
    )
    assert (classified.message, classified.remedy) == (draft.message, draft.remedy)


def test_an_objective_parameter_outside_its_range_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {"id": "net_edge_bps", "params": {"min_delta": 0.5, "k_se": 0.1}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("objective.params.k_se:")
    assert "outside the range" in failure.message


def test_a_constraint_that_is_not_a_gate_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "strategy_integrity"}, {"id": "net_edge_bps"}],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("constraints.net_edge_bps:")
    assert "not a gate" in failure.message


def test_a_leg_edge_leg_the_universe_does_not_hold_is_refused(ws: Workspace) -> None:
    edge = {"id": "leg_edge", "params": {"leg": "EURO", "min_sharpe": 0.0}}

    failure = refused(ws, document(**SLEEVE_CLASSIFICATION, required_constraints=[edge]))

    assert failure.message == (
        "required_constraints.leg_edge.leg: 'EURO' is not in the universe (DEMO)"
    )


def test_a_leg_edge_leg_the_universe_does_not_hold_is_refused_from_constraints_too(
    ws: Workspace,
) -> None:
    edge = {"id": "leg_edge", "params": {"leg": "EURO", "min_sharpe": 0.0}}
    classification = {**SLEEVE_CLASSIFICATION, "constraints": [{"id": "strategy_integrity"}, edge]}

    failure = refused(ws, document(**classification))

    assert failure.message == "constraints.leg_edge.leg: 'EURO' is not in the universe (DEMO)"


def test_a_leg_edge_leg_the_universe_holds_is_admissible(ws: Workspace) -> None:
    edge = {"id": "leg_edge", "params": {"leg": "DEMO", "min_sharpe": 0.0}}

    parsed = accepted(ws, document(**SLEEVE_CLASSIFICATION, required_constraints=[edge]))

    assert [ref.id for ref in parsed.required_constraints or []] == ["leg_edge"]


def test_a_trading_session_is_admissible_on_a_draft(ws: Workspace) -> None:
    hours = {"id": "trading_hours", "params": {"session": "09:30-16:00", "tz": "America/New_York"}}

    parsed = accepted(ws, document(required_constraints=[hours]))

    assert parsed.required_constraints is not None
    assert parsed.required_constraints[0].params == hours["params"]


def test_a_trading_session_the_gate_cannot_read_is_refused_before_any_card_runs(
    ws: Workspace,
) -> None:
    hours = {"id": "trading_hours", "params": {"session": "9:30-16:00", "tz": "US/Gotham"}}

    failure = refused(ws, document(required_constraints=[hours]))

    assert failure.message == (
        "required_constraints.trading_hours.session: '9:30-16:00' is not a span of clock "
        "times on one day, HH:MM-HH:MM; required_constraints.trading_hours.tz: 'US/Gotham' "
        "is not a time zone this host knows"
    )


def test_a_required_constraint_is_held_to_the_same_rules_and_says_which_list(
    ws: Workspace,
) -> None:
    failure = refused(ws, document(required_constraints=[{"id": "net_edge_bps"}]))

    assert failure.message.startswith("required_constraints.net_edge_bps:")
    assert "not a gate" in failure.message


def test_a_required_constraint_from_another_stage_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(required_constraints=[{"id": "bootstrap", "params": {"n": 10}}]))

    assert "required_constraints.bootstrap" in failure.message
    assert "cert stage" in failure.message


def test_a_required_constraint_parameter_outside_its_range_is_refused(ws: Workspace) -> None:
    failure = refused(
        ws, document(required_constraints=[{"id": "min_trades", "params": {"min": 0}}])
    )

    assert "required_constraints.min_trades.min" in failure.message


CONTRIBUTION = {
    **SLEEVE_CLASSIFICATION,
    "objective": {"id": "wf_contribution_bps", "params": {"min_delta": 0.5, "k_se": 1.0}},
}
"""A sub-daily sleeve its operator scores on the return per period of the capital."""

GUARD = {"id": "edge_per_trade", "params": {"min_bps": 0.0, "k_se": 1.0}}


def test_a_draft_may_state_its_objective_alone(ws: Workspace) -> None:
    """The operator names the return target; `kanso classify` decides the rest around it."""
    held = accepted(ws, document(objective=CONTRIBUTION["objective"]))

    assert held.objective is not None and held.objective.id == "wf_contribution_bps"
    assert held.construct is None and held.constraints is None


@pytest.mark.parametrize(
    ("objective", "problem"),
    [
        (
            {"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 1.0}},
            "objective.id: 'wf_sharpe_net' does not apply to this hypothesis in either "
            "objective mode; the applicable objectives are marginal_net_edge_bps, "
            "marginal_wf_contribution_bps, net_edge_bps, wf_contribution_bps",
        ),
        (
            {"id": "min_trades", "params": {"min_delta": 0.0, "k_se": 1.0}},
            "objective.id: 'min_trades' is not an objective in the toolbox",
        ),
        (
            {"id": "net_edge_bps", "params": {"min_delta": 0.0, "k_se": 9.0}},
            "objective.params.k_se: 9.0 is outside the range",
        ),
    ],
    ids=["inapplicable", "a-gate", "out-of-range"],
)
def test_an_objective_a_draft_states_is_held_to_the_toolbox(
    ws: Workspace, objective: dict[str, Any], problem: str
) -> None:
    assert problem in refused(ws, document(objective=objective)).message


def test_a_draft_stating_a_construct_without_an_objective_is_still_half_classified(
    ws: Workspace,
) -> None:
    failure = refused(ws, document(construct=SLEEVE_CLASSIFICATION["construct"]))

    assert "objective: missing" in failure.message


def test_a_contribution_without_edge_per_trade_is_refused(ws: Workspace) -> None:
    """A contribution moves toward zero as losing trades are dropped, so a lane scored on one
    without the gate keeps a card that trades less at a worse loss per trade."""
    failure = refused(ws, document(**CONTRIBUTION))

    assert failure.message == (
        "constraints: edge_per_trade is required of a hypothesis scored on "
        "wf_contribution_bps, which rewards trading less once every trade loses"
    )
    assert failure.remedy is not None
    assert "{id: edge_per_trade, params: {min_bps: <0.0..50.0>, k_se: <0.0..3.0>}}" in (
        failure.remedy
    )


@pytest.mark.parametrize("where", ["required_constraints", "constraints"])
def test_a_contribution_carrying_edge_per_trade_in_either_list_is_admitted(
    ws: Workspace, where: str
) -> None:
    constraints = [*CONTRIBUTION["constraints"], GUARD] if where == "constraints" else None
    doc = document(**CONTRIBUTION)
    if constraints is None:
        doc["required_constraints"] = [GUARD]
    else:
        doc["constraints"] = constraints

    held = accepted(ws, doc)

    assert {ref.id: ref.params for ref in held.card_gates}["edge_per_trade"] == GUARD["params"]


@pytest.mark.parametrize("where", ["required_constraints", "constraints"])
@pytest.mark.parametrize(
    ("given", "missing"), [({"min_bps": 0.0}, "k_se"), ({"k_se": 1.0}, "min_bps")]
)
def test_edge_per_trade_with_one_threshold_is_refused_wherever_it_is_named(
    ws: Workspace, where: str, given: dict[str, float], missing: str
) -> None:
    """Row 189: half the rule used to register and then skip on every card, which passes."""
    half = {"id": "edge_per_trade", "params": given}
    doc = document(**SLEEVE_CLASSIFICATION)
    if where == "constraints":
        doc["constraints"] = [*SLEEVE_CLASSIFICATION["constraints"], half]
    else:
        doc["required_constraints"] = [half]

    failure = refused(ws, doc)

    assert f"{where}.edge_per_trade.{missing}: is not set" in failure.message


def test_edge_per_trade_with_one_threshold_is_refused_on_a_draft(ws: Workspace) -> None:
    failure = refused(
        ws, document(required_constraints=[{"id": "edge_per_trade", "params": {"k_se": 1.0}}])
    )

    assert "required_constraints.edge_per_trade.min_bps: is not set" in failure.message


def test_a_gate_required_twice_is_refused_because_the_two_would_disagree(
    ws: Workspace,
) -> None:
    failure = refused(
        ws,
        document(
            required_constraints=[
                {"id": "min_trades", "params": {"min": 10}},
                {"id": "min_trades", "params": {"min": 20}},
            ]
        ),
    )

    assert "named more than once" in failure.message
    assert "min_trades" in failure.message


def test_an_unclassified_hypothesis_may_require_gates(ws: Workspace) -> None:
    """They are the operator's, so they do not wait on a classification to be checked."""
    doc = {
        key: value
        for key, value in document().items()
        if key not in {"construct", "objective", "constraints"}
    }
    doc["required_constraints"] = [{"id": "min_trades", "params": {"min": 20}}]

    parsed = accepted(ws, doc)

    assert parsed.required_constraints is not None
    assert parsed.required_constraints[0].params == {"min": 20}
    assert parsed.constraints is None


def test_a_constraint_from_another_stage_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [
            {"id": "strategy_integrity"},
            {"id": "embargoed_window", "params": {"min_fraction": 0.5}},
        ],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("constraints.embargoed_window:")
    assert "runs at the cert stage" in failure.message


def test_a_constraint_parameter_outside_its_range_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 0}}],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("constraints.min_trades.min:")
    assert "outside the range" in failure.message


def test_constraints_without_the_integrity_gate_are_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "min_trades", "params": {"min": 30}}],
    }

    failure = refused(ws, document(**classification))

    assert "constraints" in failure.message
    assert "strategy_integrity is required" in failure.message


def test_a_repeated_constraint_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "strategy_integrity"}, {"id": "strategy_integrity"}],
    }

    failure = refused(ws, document(**classification))

    assert "constraints" in failure.message
    assert "repeats" in failure.message


@pytest.mark.parametrize("dropped", ["construct", "objective", "constraints"])
def test_a_half_written_classification_is_refused(ws: Workspace, dropped: str) -> None:
    classification = {k: v for k, v in SLEEVE_CLASSIFICATION.items() if k != dropped}

    failure = refused(ws, document(**classification))

    assert failure.message.startswith(f"{dropped}:")
    assert "together" in failure.message


def test_an_unclassified_hypothesis_needs_no_classification(ws: Workspace) -> None:
    parsed = accepted(ws, DOCUMENT)

    assert parsed.construct is None
    assert parsed.objective is None
    assert parsed.constraints is None


# -- extensions ----------------------------------------------------------------


def test_a_custom_type_an_extension_registers_is_a_usable_requirement(ws: Workspace) -> None:
    extension = ws.root / "kanso_ext"
    extension.mkdir()
    (extension / "sentiment.py").write_text(
        "from nautilus_trader.core.data import Data\n"
        "from nautilus_trader.model.custom import customdataclass\n"
        "from kanso.data import register_custom_type\n\n"
        "PROVIDES = {'data_types': ('sentiment',)}\n\n\n"
        "@customdataclass\n"
        "class Sentiment(Data):\n"
        "    score: float = 0.0\n\n\n"
        "register_custom_type('sentiment', Sentiment)\n",
        encoding="utf-8",
    )
    write_instruments(ws, "DEMO")

    parsed = accepted(ws, document(data_requirements=["bar", "sentiment"]))

    assert "sentiment" in parsed.data_requirements


def test_a_workspace_without_a_portfolio_has_no_venue_overrides(ws: Workspace) -> None:
    ws.path("portfolio.yaml").unlink()

    assert accepted(ws, document(universe=["DEMO", "EURO"])) is not None


# -- sizing: a budget the ceilings can fund, on a construct that places orders --------


FULL_BOOK = {"mode": "full_book", "budget": 20_000}


def test_a_budget_the_ceilings_fund_is_admissible(ws: Workspace) -> None:
    parsed = accepted(ws, document(capital=100_000, sizing=FULL_BOOK))

    assert parsed.sizing is not None
    assert parsed.sizing.budget == 20_000.0


def test_a_budget_above_the_position_ceiling_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(capital=50_000, sizing=FULL_BOOK))

    assert "sizing.budget: 20000 is more than max_position_pct admits" in failure.message
    assert "20% of the 50000 capital is 10000" in failure.message
    assert "max_position_pct" in (failure.remedy or "")


def test_a_budget_above_the_leverage_ceiling_is_refused_naming_the_capital_it_used(
    ws: Workspace,
) -> None:
    limits = {"max_position_pct": 500, "max_drawdown_pct": 15, "max_leverage": 1}
    failure = refused(ws, document(capital=10_000, risk_limits=limits, sizing=FULL_BOOK))

    assert "sizing.budget: 20000 is more than max_leverage funds: 1 x 10000 capital is 10000" in (
        failure.message
    )
    assert "kanso.toml" not in failure.message

    defaulted = refused(
        ws, document(risk_limits=limits, sizing={"mode": "full_book", "budget": 1e9})
    )

    assert "(from kanso.toml research.capital)" in defaulted.message


def test_sizing_on_a_filter_or_exit_is_refused(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID)

    failure = refused(ws, document(capital=100_000, sizing=FULL_BOOK, **FILTER_CLASSIFICATION))

    assert "sizing: a filter places no order of its own" in failure.message
    assert "host sleeve" in (failure.remedy or "")


# -- sizing: what an attached construct must agree with its host on ----------------------


OVERLAY_CLASSIFICATION: dict[str, Any] = {
    "construct": {"id": "overlay", "host": HOST_ID},
    "objective": {"id": "marginal_net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
    "constraints": [{"id": "strategy_integrity"}],
}
HOST_BUDGET = {"mode": "full_book", "budget": 30_000}
OWN_BUDGET = {"mode": "full_book", "budget": 20_000}
BOOK = {"max_position_pct": 100, "max_drawdown_pct": 15, "max_leverage": 1}


def host_with(ws: Workspace, **changes: Any) -> None:
    """A composed host whose sleeve hypothesis file the workspace holds."""
    write_strategy(ws, HOST_ID)
    write_hypothesis(ws, document(id=HOST_ID, **changes), HOST_ID)


def test_a_budgeted_overlay_on_a_budgeted_host_is_admissible(ws: Workspace) -> None:
    host_with(ws, capital=30_000, sizing=HOST_BUDGET, risk_limits=BOOK)

    parsed = accepted(
        ws, document(capital=50_000, sizing=OWN_BUDGET, risk_limits=BOOK, **OVERLAY_CLASSIFICATION)
    )

    assert parsed.sizing is not None and parsed.sizing.budget == 20_000.0


def test_a_budgeted_overlay_on_an_unbudgeted_host_is_refused(ws: Workspace) -> None:
    host_with(ws)

    failure = refused(
        ws, document(capital=50_000, sizing=OWN_BUDGET, risk_limits=BOOK, **OVERLAY_CLASSIFICATION)
    )

    assert "sizing: host_sleeve@1 was composed without a sizing rule" in failure.message
    assert "certify and compose it again" in (failure.remedy or "")


def test_an_unbudgeted_overlay_on_a_budgeted_host_is_refused(ws: Workspace) -> None:
    host_with(ws, capital=30_000, sizing=HOST_BUDGET, risk_limits=BOOK)

    failure = refused(ws, document(capital=50_000, **OVERLAY_CLASSIFICATION))

    assert "sizing: host_sleeve@1 sizes to a budget of 30000" in failure.message
    assert failure.remedy == "add sizing to this file"


def test_an_overlay_s_budget_and_its_host_s_must_fit_the_leverage_ceiling(ws: Workspace) -> None:
    host_with(ws, capital=30_000, sizing=HOST_BUDGET, risk_limits=BOOK)

    failure = refused(
        ws, document(capital=40_000, sizing=OWN_BUDGET, risk_limits=BOOK, **OVERLAY_CLASSIFICATION)
    )

    assert "40000 capital x 1 leverage is 40000" in failure.message
    assert "less than the host's 30000 budget and this one together" in failure.message
    assert "set capital to at least 50000" in (failure.remedy or "")


def test_an_overlay_s_host_budget_must_fit_the_position_ceiling(ws: Workspace) -> None:
    host_with(ws, capital=30_000, sizing=HOST_BUDGET, risk_limits=BOOK)
    limits = {**BOOK, "max_position_pct": 50}

    failure = refused(
        ws,
        document(capital=50_000, sizing=OWN_BUDGET, risk_limits=limits, **OVERLAY_CLASSIFICATION),
    )

    assert "risk_limits.max_position_pct: 50% of the 50000 book is 25000" in failure.message
    assert "set max_position_pct to at least 60" in (failure.remedy or "")


def test_a_filter_declares_its_host_s_resolution(ws: Workspace) -> None:
    host_with(ws, resolution="1d", horizon="1d")

    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert "resolution: 1m is not the host's 1d; a filter is consulted on the host's grain" in (
        failure.message
    )
    assert failure.remedy == "set resolution to 1d, or attach as an overlay"


def test_an_attached_construct_declares_its_host_s_warmup(ws: Workspace) -> None:
    """The construct's card runs the host underneath it, warmed as the construct's file says."""
    host_with(ws, warmup={"sessions": 20})

    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert "warmup: host_sleeve@1 warms on 20 session(s) before its window" in failure.message
    assert "this file declares 0" in failure.message
    assert "set warmup to {sessions: 20} to match" in (failure.remedy or "")


def test_a_construct_warmed_under_a_cold_host_is_told_to_drop_it(ws: Workspace) -> None:
    """`sessions` is greater than zero, so the remedy for a host without a warmup is to drop
    the key, never to set it to zero."""
    host_with(ws)

    failure = refused(ws, document(warmup={"sessions": 5}, **FILTER_CLASSIFICATION))

    assert "warms on 0 session(s)" in failure.message and "this file declares 5" in failure.message
    assert (failure.remedy or "").startswith("drop warmup to match ")


def test_a_construct_warmed_as_its_host_is_admissible(ws: Workspace) -> None:
    host_with(ws, warmup={"sessions": 20})

    parsed = accepted(ws, document(warmup={"sessions": 20}, **FILTER_CLASSIFICATION))

    assert parsed.warmup is not None and parsed.warmup.sessions == 20


def test_an_attached_construct_declares_its_host_s_book_policy(ws: Workspace) -> None:
    """The construct's cards run under its own file's policy; its version under the host's."""
    host_with(ws, book={"reset": "monthly", "financing_rate_bps": 50})

    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert (
        "book: host_sleeve@1 runs under a book policy of {reset: monthly, "
        "financing_rate_bps: 50.0} and this file declares none" in failure.message
    )
    assert failure.remedy is not None
    assert failure.remedy.startswith("set book to {reset: monthly, financing_rate_bps: 50.0} to")


def test_a_construct_booked_under_an_unbooked_host_is_told_to_drop_it(ws: Workspace) -> None:
    host_with(ws)

    failure = refused(ws, document(book={"maintenance_pct": 30}, **FILTER_CLASSIFICATION))

    assert "runs under a book policy of none" in failure.message
    assert "this file declares {reset: none, financing_rate_bps: 0.0, maintenance_pct: 30.0}" in (
        failure.message
    )
    assert (failure.remedy or "").startswith("drop book to match ")


def test_a_construct_booked_as_its_host_is_admissible(ws: Workspace) -> None:
    host_with(ws, book={"reset": "monthly"})

    parsed = accepted(ws, document(book={"reset": "monthly"}, **FILTER_CLASSIFICATION))

    assert parsed.book is not None and parsed.book.reset == "monthly"


TICK_BARS: dict[str, Any] = {
    "universe": ["DEMO", "EURO"],
    "resolution": "tick",
    "data_requirements": ["bar", "trade"],
    "data_by_instrument": {"DEMO": ["trade"], "EURO": ["bar"]},
    "costs": {**DOCUMENT["costs"], "latency_ms": 20},
}
"""One name's prints beside another's bars: a tick sleeve reading a signal that carries
nothing but bars."""


def test_a_tick_hypothesis_reading_bars_at_a_stated_size_is_admissible(ws: Workspace) -> None:
    parsed = accepted(ws, document(**TICK_BARS, bar_resolution="1m"))

    assert (parsed.resolution, parsed.bar_size) == ("tick", "1m")


def test_a_tick_hypothesis_reading_bars_at_no_size_is_refused_with_its_remedy(
    ws: Workspace,
) -> None:
    """Admitted before the key existed, and refused at `research begin` for want of a snapshot
    at `tick` (exit 2), so no card of it ran."""
    failure = refused(ws, document(**TICK_BARS))

    assert "bar_resolution: resolution 'tick' is no bar size and 'bar' is required" in (
        failure.message
    )
    assert (failure.remedy or "").startswith("state bar_resolution")


def test_an_attached_construct_reads_its_host_s_bars_at_the_host_s_size(ws: Workspace) -> None:
    """The construct's card runs the host underneath it, its bars read at the size the
    construct's file states, so a host certified on one size is not handed another."""
    host_with(ws, **TICK_BARS, bar_resolution="1m")

    failure = refused(ws, document(**TICK_BARS, bar_resolution="5m", **FILTER_CLASSIFICATION))

    assert "bar_resolution: host_sleeve@1 reads its bars at 1m and this file declares 5m" in (
        failure.message
    )
    assert (failure.remedy or "").startswith("set bar_resolution to 1m to match ")


READING_NO_BARS: dict[str, Any] = {
    **TICK_BARS,
    "data_requirements": ["trade"],
    "data_by_instrument": {},
}
"""A tick host whose file requires no `bar`, so it reads none."""

DROP_THE_BARS = (
    "drop bar_resolution, and bar from data_requirements and from every data_by_instrument "
    "entry, an entry that asked for bar alone dropped with its name from the universe, to match "
)
"""The remedy over such a host, up to the host's path."""


def without_bars(doc: dict[str, Any]) -> dict[str, Any]:
    """`doc` with `DROP_THE_BARS` done as worded and nothing else."""
    alone = {name for name, kinds in doc.get("data_by_instrument", {}).items() if kinds == ["bar"]}
    return {
        **{key: value for key, value in doc.items() if key != "bar_resolution"},
        "universe": [name for name in doc["universe"] if name not in alone],
        "data_requirements": [kind for kind in doc["data_requirements"] if kind != "bar"],
        "data_by_instrument": {
            name: [kind for kind in kinds if kind != "bar"]
            for name, kinds in doc.get("data_by_instrument", {}).items()
            if name not in alone
        },
    }


def test_a_construct_reading_bars_over_a_tick_host_that_reads_none_drops_the_bars_too(
    ws: Workspace,
) -> None:
    """A tick host that requires no bar reads none, so a construct's cards would hand it bars
    it never read; dropping the size alone would leave `bar` required at no size, which the
    schema refuses, and dropping `bar` alone would leave the signal that asked for nothing else
    asking for nothing, which it refuses too, with no remedy. The remedy drops the bars, and a
    name asked for bars alone with them, and the file that follows it as worded is admitted."""
    host_with(ws, **READING_NO_BARS)
    stating = document(**TICK_BARS, bar_resolution="1m", **FILTER_CLASSIFICATION)

    failure = refused(ws, stating)
    followed = accepted(ws, without_bars(stating))

    assert failure.message.startswith(
        "bar_resolution: host_sleeve@1 reads no bars beside its tick grain and this file "
        "declares 1m"
    )
    assert (failure.remedy or "").startswith(DROP_THE_BARS)
    assert (followed.universe, followed.bar_resolution, followed.data_requirements) == (
        ["DEMO"],
        None,
        ["trade"],
    )
    assert followed.data_by_instrument == {"DEMO": ["trade"]}


@pytest.mark.parametrize("size", ["5m", "1m"])
def test_a_construct_reading_bars_beside_ticks_over_a_bar_size_host_takes_its_grain(
    ws: Workspace, size: str
) -> None:
    """Only an overlay states a size over a bar-size host, and the overlay's card would hand the
    host the overlay's tick clock as a bar grain beside its own, at which no bar can be
    subscribed — at the host's very size as much as at another, so the refusal says that rather
    than compare the two. Dropping the size alone leaves `bar` required at no size. The remedy
    is the host's resolution with the key dropped, and the file that follows it is admitted."""
    host_with(ws)
    tick = {
        "resolution": "tick",
        "data_requirements": ["bar", "trade"],
        "costs": TICK_BARS["costs"],
    }

    failure = refused(ws, document(**tick, bar_resolution=size, **OVERLAY_CLASSIFICATION))
    followed = accepted(ws, document(**{**tick, "resolution": "1m"}, **OVERLAY_CLASSIFICATION))

    assert failure.message.endswith(
        "bar_resolution: host_sleeve@1 states none, reading its bars at its own resolution, 1m, "
        f"and this file declares {size}; an overlay's card hands its host this file's tick clock "
        "as a bar grain beside its own, at which no bar can be subscribed"
    )
    assert (failure.remedy or "").startswith(
        "set resolution to 1m and drop bar_resolution, to match "
    )
    assert (followed.resolution, followed.bar_resolution) == ("1m", None)


@pytest.mark.parametrize("construct", ["filter", "exit"])
def test_a_rule_stating_a_bar_size_off_a_bar_size_host_s_grain_is_given_one_remedy_that_clears(
    ws: Workspace, construct: str
) -> None:
    """A filter or an exit is refused on its grain first. Setting the host's resolution alone
    would leave a size beside a bar-size resolution, which the schema refuses, and attaching as
    an overlay meets the refusal above; so the one remedy takes the host's resolution and drops
    the key, and the file that follows it as worded is admitted."""
    host_with(ws)
    classification = {**FILTER_CLASSIFICATION, "construct": {"id": construct, "host": HOST_ID}}
    stating = document(**TICK_BARS, bar_resolution="5m", **classification)

    failure = refused(ws, stating)
    followed = accepted(
        ws, {**{k: v for k, v in stating.items() if k != "bar_resolution"}, "resolution": "1m"}
    )

    assert f"resolution: tick is not the host's 1m; a {construct} is consulted on the host's" in (
        failure.message
    )
    assert (failure.remedy or "").startswith(
        "set resolution to 1m and drop bar_resolution, to match "
    )
    assert (followed.resolution, followed.bar_resolution) == ("1m", None)


def test_a_rule_stating_a_bar_size_off_a_tick_host_reading_none_takes_its_grain_and_drops_bars(
    ws: Workspace,
) -> None:
    """Over a tick host reading no bars, the grain refusal's one remedy is the host's resolution
    with the bars dropped as `DROP_THE_BARS` says, and the file that follows it is admitted."""
    host_with(ws, **READING_NO_BARS)
    stating = document(
        **{
            **TICK_BARS,
            "resolution": "quote",
            "data_requirements": ["bar", "quote"],
            "data_by_instrument": {"DEMO": ["quote"], "EURO": ["bar"]},
        },
        bar_resolution="1m",
        **FILTER_CLASSIFICATION,
    )

    failure = refused(ws, stating)
    followed = accepted(ws, {**without_bars(stating), "resolution": "tick"})

    assert "resolution: quote is not the host's tick" in failure.message
    assert (failure.remedy or "").startswith(f"set resolution to tick, {DROP_THE_BARS}")
    assert (followed.resolution, followed.universe, followed.data_requirements) == (
        "tick",
        ["DEMO"],
        ["quote"],
    )


def test_a_filter_off_a_host_reading_bars_beside_ticks_is_given_its_grain_and_size(
    ws: Workspace,
) -> None:
    """Over such a host an overlay may not keep a bar clock of its own, so attaching as one is
    no way off: the one remedy is the host's grain and size, and the file that follows it with
    the host's types and latency is admitted."""
    host_with(ws, **TICK_BARS, bar_resolution="1m")

    failure = refused(ws, document(resolution="5m", **FILTER_CLASSIFICATION))
    followed = accepted(ws, document(**TICK_BARS, bar_resolution="1m", **FILTER_CLASSIFICATION))

    assert "resolution: 5m is not the host's tick; a filter is consulted on the host's grain" in (
        failure.message
    )
    assert failure.remedy == "set resolution to tick and bar_resolution to 1m, requiring bar"
    assert (followed.resolution, followed.bar_resolution) == ("tick", "1m")


def test_a_construct_reading_bars_as_its_host_does_is_admissible(ws: Workspace) -> None:
    host_with(ws, **TICK_BARS, bar_resolution="1m")

    parsed = accepted(ws, document(**TICK_BARS, bar_resolution="1m", **FILTER_CLASSIFICATION))

    assert parsed.bar_resolution == "1m"


def test_an_overlay_on_a_clock_of_its_own_over_a_host_reading_bars_beside_ticks_is_refused_once(
    ws: Workspace,
) -> None:
    """A file at a bar-size resolution states no `bar_resolution`, so its cards would hand the
    host bars of no size. One refusal, whose remedy — the host's grain and size — is admitted."""
    host_with(ws, **TICK_BARS, bar_resolution="1m")

    failure = refused(ws, document(resolution="5m", **OVERLAY_CLASSIFICATION))
    followed = accepted(ws, document(**TICK_BARS, bar_resolution="1m", **OVERLAY_CLASSIFICATION))

    assert failure.message.startswith(
        "resolution: 5m is a bar clock of this overlay's own, and host_sleeve@1 reads its bars "
        "beside its tick grain at 1m"
    )
    assert (failure.remedy or "").startswith(
        "set resolution to tick and bar_resolution to 1m, requiring bar, to match "
    )
    assert (followed.resolution, followed.bar_resolution) == ("tick", "1m")


def test_an_overlay_may_keep_a_grain_of_its_own(ws: Workspace) -> None:
    host_with(ws, resolution="1d", horizon="1d")

    assert accepted(ws, document(**OVERLAY_CLASSIFICATION)).resolution == "1m"


def test_a_host_without_a_hypothesis_file_is_paired_with_nothing(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID)

    assert accepted(ws, document(sizing=OWN_BUDGET, capital=100_000, **OVERLAY_CLASSIFICATION))


def test_a_benchmark_on_a_construct_measured_against_its_host_is_refused(ws: Workspace) -> None:
    host_with(ws, horizon="1d", resolution="1d")

    failure = refused(
        ws,
        document(
            benchmark={"hold": "first_leg"},
            horizon="1d",
            resolution="1d",
            **{
                **OVERLAY_CLASSIFICATION,
                "objective": {
                    "id": "marginal_wf_sharpe",
                    "params": {"min_delta": 0.0, "k_se": 1.0},
                },
            },
        ),
    )

    assert failure.message.startswith(
        "benchmark: declared, and marginal_wf_sharpe measures nothing against it"
    )
    assert (
        failure.remedy
        == "remove benchmark from this file; the construct is measured against its host"
    )
