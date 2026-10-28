"""How fast a sleeve sends orders, and the bucket that holds it to its account's rate.

A broker counts what a strategy sends it — every order, every modify, every cancel — and
answers past a rate with a refusal or a wait. The simulated venue counts nothing, so a rule
that re-posts on every change of the touch would trade on a card at a rate no account takes
and be throttled once it trades through one. Two things close that gap, and both run in the
sleeve on both code paths, on the data clock, so a card, a replay and a node count the same
commands at the same instants.

**The tally** (`Tally`) counts every command the sleeve sends, by kind — a submit per order,
a modify per modify, a cancel per order cancelled — at the data-clock instant it is sent, and
reads off them how busy the busiest minute was: for each command, how many the sleeve sent in
the 60 s ending at it, that one included; the largest of those counts and their 99th
percentile over every command, by nearest rank. It is a reading of the run and changes
nothing the run does, so every card carries it whether or not a rate is stated
(`kanso.schemas.Commands`).

**The bucket** (`Bucket`) is what a stated `costs.orders_per_minute` builds: a token bucket of
that many tokens, full at the start, refilled at that many a minute on the data clock and
never past full, from which every command takes one as the sleeve sends it. It is the shape
of the quota kanso's own broker clients throttle themselves with — in nautilus_trader 1.231.0
`nautilus_pyo3.Quota.rate_per_minute(max_burst)` names the burst it admits and the per-minute
rate as one number (`core/nautilus_pyo3.pyi`) — so a sleeve whose bucket never empties is one
that quota never waits on. What the sleeve does with a command the bucket has no token for
is the strategy base's (`kanso.nautilus.strategy.KansoStrategy`): an entry or a modify is
refused, an exit is owed, a cancel is deferred.

The data clock is `data_time`, held so it never runs backwards: a point published late with
an earlier reference time refills nothing and moves no window. Every count is an integer and
every refill is integer arithmetic in nanoseconds, so the same commands at the same instants
give the same answers on every host and both paths.
"""

from __future__ import annotations

from collections import deque
from typing import Final

from kanso.schemas import Commands

__all__ = ["CANCEL", "KINDS", "MODIFY", "NS_PER_MINUTE", "SUBMIT", "Bucket", "Tally"]

NS_PER_MINUTE: Final = 60_000_000_000
"""A minute on the data clock: the window a rate is struck over and the span a bucket refills
its whole capacity in."""

SUBMIT: Final = "submit"
MODIFY: Final = "modify"
CANCEL: Final = "cancel"
KINDS: Final = (SUBMIT, MODIFY, CANCEL)
"""The three commands a sleeve sends a venue."""

PERCENTILE: Final = 99
"""The percentile of the per-command 60 s counts a tally reports beside their peak."""


class Bucket:
    """`per_minute` tokens, full at the start, refilled at `per_minute` a minute on the data
    clock and never past full; a command takes one.

    The level is kept in token-nanoseconds — a token is `NS_PER_MINUTE` of them and a
    nanosecond refills `per_minute` — so a refill is exact at any rate and any gap.
    """

    def __init__(self, per_minute: int) -> None:
        if per_minute < 1:
            raise ValueError(f"per_minute: {per_minute} admits no command at all")
        self.per_minute = per_minute
        self._full = per_minute * NS_PER_MINUTE
        self._level = self._full
        self._at: int | None = None

    def holds(self, now_ns: int) -> int:
        """The whole tokens the bucket holds at `now_ns`, refilled to it."""
        self._refill(now_ns)
        return self._level // NS_PER_MINUTE

    def take(self, now_ns: int, count: int = 1) -> bool:
        """Take `count` tokens at `now_ns`, or none when it holds fewer; whether it took them."""
        self._refill(now_ns)
        cost = count * NS_PER_MINUTE
        if self._level < cost:
            return False
        self._level -= cost
        return True

    def _refill(self, now_ns: int) -> None:
        at = self._at
        if at is None or now_ns > at:
            if at is not None:
                self._level = min(self._full, self._level + self.per_minute * (now_ns - at))
            self._at = now_ns


class Tally:
    """Every command a sleeve sent, counted by kind, and how many it sent in the 60 s ending
    at each.

    It holds the instants of the last minute's commands and how often each count was seen,
    never the commands themselves, so a sleeve that sends millions holds as much as its
    busiest minute sent.
    """

    def __init__(self) -> None:
        self._counts = dict.fromkeys(KINDS, 0)
        self._recent: deque[int] = deque()
        self._seen: dict[int, int] = {}

    def add(self, ts_ns: int, kind: str, count: int = 1) -> None:
        """Count `count` commands of `kind` sent at `ts_ns`, which never precedes the last."""
        self._counts[kind] += count
        recent = self._recent
        edge = ts_ns - NS_PER_MINUTE
        while recent and recent[0] <= edge:
            recent.popleft()
        seen = self._seen
        for _ in range(count):
            recent.append(ts_ns)
            held = len(recent)
            seen[held] = seen.get(held, 0) + 1

    def within(self, now_ns: int) -> int:
        """How many commands were sent in the 60 s ending at `now_ns`."""
        edge = now_ns - NS_PER_MINUTE
        return sum(1 for ts_ns in self._recent if ts_ns > edge)

    def summary(self) -> Commands:
        """The counts, the busiest 60 s and its 99th percentile over every command."""
        total = sum(self._seen.values())
        rank = (PERCENTILE * total + 99) // 100  # the nearest rank, ceil(0.99 x total)
        p99 = 0
        reached = 0
        for held in sorted(self._seen):
            reached += self._seen[held]
            if reached >= rank:
                p99 = held
                break
        return Commands(
            submits=self._counts[SUBMIT],
            modifies=self._counts[MODIFY],
            cancels=self._counts[CANCEL],
            peak_60s=max(self._seen, default=0),
            p99_60s=p99,
        )
