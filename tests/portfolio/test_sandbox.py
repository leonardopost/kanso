"""The sandbox binding: the subscription the engine does not make, and the one kanso does."""

from __future__ import annotations

from nautilus_trader.common.component import is_matching_py

from kanso.nautilus import sandbox
from tests.replay.conftest import VENUE, bars, quotes, trades

WINDOW = (__import__("datetime").date(2024, 3, 1), __import__("datetime").date(2024, 3, 3))

SHIPPED = f"data.*.{VENUE}.*"
"""What `SandboxExecutionClient.connect` subscribes its `on_data` handler to."""


def test_the_shipped_subscription_reaches_a_quote_and_a_trade() -> None:
    assert is_matching_py(f"data.quotes.{VENUE}.DEMO", SHIPPED)
    assert is_matching_py(f"data.trades.{VENUE}.DEMO", SHIPPED)


def test_the_shipped_subscription_never_reaches_a_bar() -> None:
    # A bar is published to `data.bars.{bar_type}` and a bar type begins with the instrument
    # id, so the venue lands inside the last segment where the pattern needs a separator.
    # Without kanso's own subscription the exchange sees no market on a bar-only universe.
    topic = f"data.bars.DEMO.{VENUE}-1-DAY-LAST-EXTERNAL"
    assert not is_matching_py(topic, SHIPPED)


def test_bar_topics_names_every_grain_the_window_holds_by_venue() -> None:
    found = sandbox.bar_topics(bars(WINDOW))

    assert found == {VENUE: (f"data.bars.DEMO.{VENUE}-1-DAY-LAST-EXTERNAL",)}
    assert is_matching_py(found[VENUE][0], "data.bars.*")


def test_bar_topics_is_read_off_the_points_not_off_a_universe() -> None:
    assert sandbox.bar_topics(quotes(WINDOW)) == {}
    assert sandbox.bar_topics(trades(WINDOW)) == {}
    assert sandbox.bar_topics(()) == {}


def test_bar_topics_separates_two_instruments_of_one_venue() -> None:
    found = sandbox.bar_topics([*bars(WINDOW, "DEMO"), *bars(WINDOW, "OTHER")])

    assert set(found) == {VENUE}
    assert len(found[VENUE]) == 2
    assert found[VENUE] == tuple(sorted(found[VENUE])), "topics are ordered, so a node is"


def test_a_client_configuration_carries_the_venue_model_unchanged() -> None:
    from kanso.nautilus.venue import venue_configs
    from tests.replay.conftest import hypothesis, venue_model

    venue = venue_configs(hypothesis(), venue_model(), 100_000.0)[0]

    config = sandbox.client_config(venue)

    assert config.venue == venue.name
    assert config.account_type == venue.account_type
    assert config.base_currency == venue.base_currency
    assert list(config.starting_balances) == list(venue.starting_balances)
    assert float(config.default_leverage) == venue.default_leverage
    assert config.bar_execution is True
