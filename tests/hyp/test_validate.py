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
        ws, document(resolution="tick", data_requirements=["book", "trade"], depth=depth)
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


PRINT_COSTS = {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "fixed_bps", "fixed_bps": 2}


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
