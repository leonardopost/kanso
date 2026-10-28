"""The token bucket an account's rate builds, and the tally every run keeps, on the data clock.

Both are arithmetic on instants a sleeve hands them, so they are tested as arithmetic: a burst
the size of the rate, a refill of exactly one token every `60 s / rate`, never past full, and
an instant that runs backwards refilling nothing. What the sleeve does with a command the
bucket refuses is tested where it does it (`tests/replay/test_order_rate.py`).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.nautilus.rate import (
    CANCEL,
    MODIFY,
    NS_PER_MINUTE,
    SUBMIT,
    Bucket,
    Tally,
)
from kanso.nautilus.strategy import KansoConfig, KansoStrategy
from kanso.schemas import Commands

SECOND_NS = 1_000_000_000


def test_a_bucket_starts_full_and_admits_a_burst_of_its_rate() -> None:
    bucket = Bucket(5)

    assert [bucket.take(0) for _ in range(6)] == [True] * 5 + [False]
    assert bucket.holds(0) == 0


def test_a_bucket_refills_one_token_every_sixtieth_of_a_minute_over_its_rate() -> None:
    """At 6 a minute a token comes back every 10 s of the data clock, and not a nanosecond
    before."""
    bucket = Bucket(6)
    for _ in range(6):
        assert bucket.take(0)

    assert not bucket.take(10 * SECOND_NS - 1)
    assert bucket.take(10 * SECOND_NS)
    assert not bucket.take(10 * SECOND_NS)
    assert bucket.holds(40 * SECOND_NS) == 3


def test_a_bucket_never_refills_past_full() -> None:
    bucket = Bucket(3)
    assert bucket.take(0)

    assert bucket.holds(10 * NS_PER_MINUTE) == 3
    assert [bucket.take(10 * NS_PER_MINUTE) for _ in range(4)] == [True, True, True, False]


def test_an_instant_before_the_last_refills_nothing() -> None:
    """The sleeve hands the bucket a clock that never runs backwards; an earlier instant
    handed anyway moves nothing, so a late point cannot buy a token back."""
    bucket = Bucket(60)
    for _ in range(60):
        bucket.take(5 * SECOND_NS)

    assert not bucket.take(4 * SECOND_NS)
    assert bucket.take(6 * SECOND_NS), "a second on from the last instant refills one"


def test_a_bucket_takes_all_of_a_count_or_none_of_it() -> None:
    bucket = Bucket(4)

    assert not bucket.take(0, 5)
    assert bucket.holds(0) == 4, "a count it could not take took nothing"
    assert bucket.take(0, 3)
    assert bucket.holds(0) == 1


def test_a_bucket_refuses_a_rate_that_admits_nothing() -> None:
    with pytest.raises(ValueError, match="admits no command"):
        Bucket(0)


@given(
    rate=st.integers(min_value=1, max_value=200),
    gaps=st.lists(st.integers(min_value=0, max_value=2 * NS_PER_MINUTE), max_size=30),
)
def test_a_bucket_admits_no_more_than_its_burst_and_its_refill(rate: int, gaps: list[int]) -> None:
    """Over any span from the start, what it admitted is at most the burst plus a token for
    every whole `60 s / rate` the span holds."""
    bucket = Bucket(rate)
    now, taken = 0, 0
    for gap in gaps:
        now += gap
        while bucket.take(now):
            taken += 1
        assert taken <= rate + rate * now // NS_PER_MINUTE


def test_a_tally_counts_each_kind_and_the_busiest_sixty_seconds() -> None:
    tally = Tally()
    for second in (0, 1, 2):
        tally.add(second * SECOND_NS, SUBMIT)
    tally.add(3 * SECOND_NS, CANCEL, 2)
    tally.add(70 * SECOND_NS, MODIFY)

    assert tally.summary() == Commands(submits=3, modifies=1, cancels=2, peak_60s=5, p99_60s=5)


def test_the_window_ending_at_a_command_holds_what_was_sent_in_the_sixty_seconds_up_to_it() -> None:
    """A command exactly 60 s before another is out of its window; one a nanosecond later is
    in it."""
    tally = Tally()
    tally.add(0, SUBMIT)
    tally.add(NS_PER_MINUTE, SUBMIT)
    assert tally.summary().peak_60s == 1

    tally = Tally()
    tally.add(1, SUBMIT)
    tally.add(NS_PER_MINUTE, SUBMIT)
    assert tally.summary().peak_60s == 2
    assert tally.within(NS_PER_MINUTE) == 2
    assert tally.within(NS_PER_MINUTE + 1) == 1


def test_the_99th_percentile_is_the_nearest_rank_over_every_command() -> None:
    """A hundred commands each alone in its minute, then a burst of five at one instant, read
    one, two, three, four and five: of the 105 counts the 104th smallest, the nearest rank of
    the 99th percentile, is the burst's four."""
    tally = Tally()
    for minute in range(100):
        tally.add(minute * 2 * NS_PER_MINUTE, SUBMIT)
    for _ in range(5):
        tally.add(500 * NS_PER_MINUTE, CANCEL)

    sent = tally.summary()
    assert (sent.peak_60s, sent.p99_60s) == (5, 4)

    calm = Tally()
    calm.add(0, SUBMIT)
    calm.add(1, SUBMIT)
    for minute in range(1, 200):
        calm.add(minute * 2 * NS_PER_MINUTE, SUBMIT)
    assert (calm.summary().peak_60s, calm.summary().p99_60s) == (2, 1), (
        "one pair in 201 commands moves the peak and not the 99th percentile"
    )


def test_a_tally_with_nothing_sent_reads_zero_throughout() -> None:
    assert Tally().summary() == Commands()
    assert Tally().within(0) == 0


def sleeve(per_minute: int | None) -> KansoStrategy:
    """A sleeve under a venue model stating `per_minute`, outside any engine: what it spends
    reads `data_time` alone."""
    costs = {} if per_minute is None else {"orders_per_minute": per_minute}
    return KansoStrategy(KansoConfig(venue_model={"costs": costs}))


def test_the_sleeve_s_data_clock_never_runs_backwards_for_its_bucket_or_its_tally() -> None:
    """A point published late carries an earlier reference time than the one before it; the
    sleeve counts a command sent on it at the later instant, so the tally's window and the
    bucket's refill read one clock that only moves forward."""
    held = sleeve(1)
    held._data_time = 120 * SECOND_NS
    assert held._command_spend(SUBMIT)
    held._data_time = 60 * SECOND_NS
    assert not held._command_spend(CANCEL), "an earlier reference time buys no token back"
    held._data_time = 180 * SECOND_NS
    assert held._command_spend(CANCEL)

    assert held._command_tally.summary() == Commands(submits=1, cancels=1, peak_60s=1, p99_60s=1)


def test_a_sleeve_under_no_stated_rate_spends_freely_and_counts_what_it_sends() -> None:
    free = sleeve(None)
    assert free._command_bucket is None
    assert all(free._command_spend(SUBMIT) for _ in range(1_000))
    assert free._command_spend(CANCEL, 0), "nothing to send is nothing to count"

    assert free._command_tally.summary() == Commands(submits=1_000, peak_60s=1_000, p99_60s=990)
