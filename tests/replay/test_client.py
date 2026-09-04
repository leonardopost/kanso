"""The replay data client: what it releases, when it releases it, and what it waits for.

The flow control is tested against fake engines rather than a node, because what is under
test is the rule — nothing is released while a queue holds work — and a fake queue is the
only way to hold work open long enough to prove the rule rather than observe its effect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

import pytest

from kanso.nautilus.replay_client import ReplayDataClient, outstanding


@dataclass
class FakeEngine:
    """An engine whose queue depths are whatever a test says they are."""

    depths: list[int] = field(default_factory=list)
    asked: int = 0

    def cmd_qsize(self) -> int:
        self.asked += 1
        return self.depths.pop(0) if self.depths else 0

    def evt_qsize(self) -> int:
        return 0


@dataclass
class Point:
    """The only thing the feed reads off a data object is its availability instant."""

    ts_init: int
    ts_event: int = 0


class Feeder(ReplayDataClient):
    """A client with the engine's construction removed, so a test can build one."""

    def __init__(self, **kwargs: Any) -> None:
        self.released_points: list[Point] = []
        self._points = tuple(kwargs.pop("points", ()))
        self._speed = float(kwargs.pop("speed", 0.0))
        self._settle_turns = int(kwargs.pop("settle_turns", 1))
        self._sleep = kwargs.pop("sleep", asyncio.sleep)
        self._alive = kwargs.pop("alive", lambda: True)
        self._engines = ()
        self._released = 0
        self._paced = 0.0
        self._last_ts = 0

    def _handle_data(self, point: Any) -> None:  # type: ignore[override]
        self.released_points.append(point)


def slept() -> tuple[list[float], Any]:
    """A sleep that records what it was asked for and yields control anyway."""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    return delays, sleep


def run(coro: Awaitable[Any]) -> Any:
    """Drive one coroutine to completion on a loop of its own."""
    return asyncio.new_event_loop().run_until_complete(coro)


# --- what counts as work in flight --------------------------------------------


def test_outstanding_sums_every_queue_an_engine_exposes() -> None:
    """Work moves between engines, so the wait is on all of them at once."""
    left, right = FakeEngine([3]), FakeEngine([4])

    assert outstanding([left, right]) == 7


def test_outstanding_ignores_a_queue_an_engine_does_not_have() -> None:
    """Each engine has some of the accessors and none has all of them."""

    class Bare:
        pass

    assert outstanding([Bare()]) == 0


def test_pending_is_zero_without_attached_engines() -> None:
    """A client nobody attached waits on nothing, which is what a unit test wants."""
    assert Feeder().pending() == 0


# --- the flow control ---------------------------------------------------------


def test_the_feed_waits_until_the_queues_are_quiet() -> None:
    """A point is not released while the previous one's work is still queued."""
    engine = FakeEngine([2, 1, 0, 0, 0, 0, 0, 0])
    client = Feeder(points=[Point(1), Point(2)], settle_turns=2)
    client.attach(engine)

    run(client.replay())

    assert client.released == 2
    assert engine.asked >= 4


def test_a_settled_feed_releases_every_point_in_order() -> None:
    """The window is released whole, in the order it was handed over."""
    client = Feeder(points=[Point(3), Point(7), Point(9)])

    assert run(client.replay()) == 3
    assert [point.ts_init for point in client.released_points] == [3, 7, 9]
    assert client.last_ts == 9


def test_a_feed_stops_when_the_node_it_feeds_stops() -> None:
    """A node that is shutting down will never drain, so the feed does not wait for it."""
    alive = [True]
    client = Feeder(points=[Point(1), Point(2), Point(3)], alive=lambda: alive[0])

    async def drive() -> int:
        released = 0
        for _ in range(1):
            released = await client.replay()
        return released

    alive[0] = False
    assert run(drive()) == 0
    assert client.released_points == []


def test_a_settle_ends_when_the_node_stops_mid_wait() -> None:
    """The wait itself is abandoned, not only the next release."""
    engine = FakeEngine([5, 5, 5, 5])
    alive = [True]
    client = Feeder(settle_turns=3, alive=lambda: alive[0])
    client.attach(engine)

    async def drive() -> None:
        alive[0] = False
        await client.settle()

    run(drive())

    assert engine.asked == 0


# --- pacing -------------------------------------------------------------------


def test_speed_zero_never_sleeps() -> None:
    """Speed zero is no wall-clock pacing at all, not "as fast as the socket allows"."""
    delays, sleep = slept()
    client = Feeder(points=[Point(0), Point(10**9)], speed=0.0, sleep=sleep)

    run(client.replay())

    assert client.paced_s == 0.0
    assert all(delay == 0 for delay in delays)


def test_a_speed_paces_by_the_gap_between_availability_instants() -> None:
    """A second of data at speed two is half a second of wall clock."""
    delays, sleep = slept()
    client = Feeder(points=[Point(0), Point(2 * 10**9)], speed=2.0, sleep=sleep)

    run(client.replay())

    assert client.paced_s == pytest.approx(1.0)
    assert pytest.approx(1.0) in delays


def test_two_points_at_one_instant_are_not_paced_apart() -> None:
    """A zero gap is no wait, at any speed."""
    delays, sleep = slept()
    client = Feeder(points=[Point(5), Point(5)], speed=1.0, sleep=sleep)

    run(client.replay())

    assert client.paced_s == 0.0
    assert all(delay == 0 for delay in delays)


def test_the_first_point_is_released_without_waiting() -> None:
    """There is no previous instant to pace against, so the feed starts at once."""
    delays, sleep = slept()
    client = Feeder(points=[Point(10**12)], speed=1.0, sleep=sleep)

    run(client.replay())

    assert client.paced_s == 0.0
    assert client.released == 1
