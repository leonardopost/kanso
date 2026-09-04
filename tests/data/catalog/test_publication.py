"""Publication rules: keyed by data class, conservative, and never optional for delayed data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from kanso.data import publication as pub
from kanso.errors import Exit, ValidationError
from tests.data.catalog.conftest import AAPL, bar, quote, trade

JAN = date(2024, 1, 2)
FIFTEEN_MINUTES = 15 * 60 * pub.NANOS_PER_SECOND


@dataclass
class Opaque:
    """A point with timestamps and no way to rebuild itself."""

    ts_event: int
    ts_init: int


def test_every_rule_is_keyed_by_the_data_class_it_names() -> None:
    for key, rule in pub.PUBLICATION_RULES.items():
        assert rule.id == key
        assert rule.description


def test_no_rule_names_a_vendor() -> None:
    """A rule is a fact about a data class; two vendors carrying it publish it once."""
    for rule in pub.PUBLICATION_RULES.values():
        assert "vendor" not in rule.id


def test_resolve_takes_an_id_or_a_rule() -> None:
    rule = pub.resolve("delayed_quote")
    assert pub.resolve(rule) is rule


def test_resolve_refuses_a_rule_nobody_declared() -> None:
    with pytest.raises(ValidationError) as raised:
        pub.resolve("whenever_it_arrives")
    assert raised.value.code is Exit.VALIDATION
    assert "delayed_quote" in raised.value.message


def test_a_derived_rule_computes_the_availability_instant() -> None:
    rule = pub.resolve("delayed_quote")
    assert rule.derived
    assert rule.lag == timedelta(minutes=15)
    assert rule.available_at(1_000) == 1_000 + FIFTEEN_MINUTES


def test_a_supplied_rule_derives_nothing() -> None:
    rule = pub.resolve("fundamental")
    assert not rule.derived
    with pytest.raises(ValidationError, match="must supply ts_init"):
        rule.available_at(1_000)


def test_the_series_that_change_only_on_publication_prime_their_windows() -> None:
    assert pub.primes("fundamental")
    assert pub.primes("corporate_action")
    assert pub.primes("economic_release")
    assert not pub.primes("delayed_quote")
    assert not pub.primes(None)


def test_availability_may_not_precede_the_reference_time() -> None:
    with pytest.raises(ValidationError) as raised:
        pub.check_availability([bar(AAPL, JAN, lag_ns=-1)])
    assert raised.value.code is Exit.VALIDATION
    assert "cannot precede" in raised.value.message


def test_availability_equal_to_the_reference_time_is_fine_for_realtime_data() -> None:
    pub.check_availability([bar(AAPL, JAN), quote(AAPL, JAN)])


def test_a_delayed_dataset_must_name_a_rule() -> None:
    with pytest.raises(ValidationError, match="must name the rule"):
        pub.check_delayed([quote(AAPL, JAN, lag_ns=FIFTEEN_MINUTES)], None)


def test_a_delayed_point_may_not_be_available_at_its_reference_time() -> None:
    with pytest.raises(ValidationError) as raised:
        pub.check_delayed([quote(AAPL, JAN)], "delayed_quote")
    assert "delay is not in its timestamps" in raised.value.message


def test_a_delayed_point_must_carry_the_instant_the_rule_derives() -> None:
    with pytest.raises(ValidationError, match="does not derive"):
        pub.check_delayed([quote(AAPL, JAN, lag_ns=60)], "delayed_quote")


def test_a_delayed_point_the_rule_derives_is_accepted() -> None:
    rule = pub.check_delayed([quote(AAPL, JAN, lag_ns=FIFTEEN_MINUTES)], "delayed_quote")
    assert rule.id == "delayed_quote"


def test_a_supplied_rule_only_asks_that_publication_came_later() -> None:
    rule = pub.check_delayed(
        [trade(AAPL, JAN, lag_ns=86_400 * pub.NANOS_PER_SECOND)], "fundamental"
    )
    assert not rule.derived


def test_stamp_sets_availability_from_the_rule_and_leaves_the_reference_alone() -> None:
    source = bar(AAPL, JAN)
    stamped = list(pub.stamp([source], "official_close"))[0]
    assert stamped.ts_event == source.ts_event
    assert stamped.ts_init == source.ts_event + FIFTEEN_MINUTES
    assert stamped.close == source.close


def test_stamp_leaves_a_point_that_already_carries_the_right_instant() -> None:
    source = bar(AAPL, JAN, lag_ns=FIFTEEN_MINUTES)
    assert list(pub.stamp([source], "official_close"))[0] is source


def test_stamp_passes_a_supplied_point_through_untouched() -> None:
    source = trade(AAPL, JAN, lag_ns=1_000)
    assert list(pub.stamp([source], "fundamental"))[0] is source


def test_stamp_refuses_a_supplied_point_with_no_availability_of_its_own() -> None:
    with pytest.raises(ValidationError, match="must supply ts_init"):
        list(pub.stamp([trade(AAPL, JAN)], "fundamental"))


def test_restamp_refuses_a_point_it_cannot_rebuild() -> None:
    with pytest.raises(ValidationError, match="no to_dict/from_dict"):
        pub.restamp(Opaque(ts_event=1, ts_init=1), 2)


def test_the_primer_is_the_last_point_published_before_the_window() -> None:
    early = trade(AAPL, date(2023, 12, 20), seq=1)
    later = trade(AAPL, date(2023, 12, 28), seq=2)
    inside = trade(AAPL, date(2024, 1, 3), seq=3)
    window_opens = inside.ts_event - 1
    assert pub.last_before([early, later, inside], window_opens) is later


def test_there_is_no_primer_when_nothing_was_published_first() -> None:
    inside = trade(AAPL, date(2024, 1, 3))
    assert pub.last_before([inside], inside.ts_init) is None
