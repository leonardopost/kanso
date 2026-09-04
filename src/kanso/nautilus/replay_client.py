"""The replay data client: a live data client fed by the catalog, flow-controlled at every speed.

A live node is asynchronous. Its data engine, risk engine and execution engine each consume
their own queue in their own task, so publishing a point returns long before that point has
reached a strategy, before the order the strategy wrote has reached the venue and before the
fill it produced has reached the position the next decision reads. A feed that releases
points as fast as the loop accepts them therefore fills orders several bars after the
research path fills them — and in the extreme releases the whole window before the first bar
is handled, so nothing trades at all.

**So the feed waits.** After every point this client waits for the engines it was attached
to to go quiet — every queue empty, and stably empty across consecutive turns of the loop —
before releasing the next. That is the whole of the flow control, it applies at every speed,
and it is what makes the live code path produce the order intents the research code path
produces over the same data. `speed` is wall-clock pacing on top of it: `speed` seconds of
data per second of wall clock, and **speed zero means no pacing at all** — no sleeping
between points — rather than "as fast as the socket allows", which is the free-running feed
this client exists to refuse.

Points carry the timestamps they were stored with. The client re-stamps nothing: `ts_event`
stays the economic reference time and `ts_init` the instant the information became
available, so a strategy that reads its own data time sees in replay exactly what it saw in
research. The node's clock is a live clock and the engine's is a test clock, which is why
neither an intent nor a point may ever be stamped from a clock here.

Engine facts this module relies on (nautilus_trader 1.231.0): a custom live data client is a
subclass of `LiveMarketDataClient` constructed with `loop`, `client_id`, `venue`, `msgbus`,
`cache`, `clock`, `instrument_provider` and `config`; its `_handle_data` sends the point to
the `DataEngine.process` endpoint, which on a `LiveDataEngine` enqueues it rather than
handling it; the `_subscribe_*` and `_unsubscribe_*` coroutines are the client's half of a
subscription, while the base class does the bookkeeping and the data engine does the routing,
so a client that holds the whole window releases every point and lets the engine route it;
`LiveDataEngine` exposes `cmd_qsize`, `req_qsize`, `res_qsize` and `data_qsize`, and
`LiveRiskEngine` and `LiveExecutionEngine` expose `cmd_qsize` and `evt_qsize` — together the
whole of the work in flight inside a node; each queue is consumed by a task that dequeues and
handles in one uninterrupted step, so a queue observed empty from another task holds no work
that has been taken but not yet done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.identifiers import ClientId

__all__ = [
    "NS_PER_SECOND",
    "QUEUE_SIZES",
    "REPLAY_CLIENT",
    "SETTLE_TURNS",
    "ReplayDataClient",
    "outstanding",
]

REPLAY_CLIENT: Final = "REPLAY"
"""The client id a replayed node's data arrives under."""

SETTLE_TURNS: Final = 3
"""Consecutive quiet turns of the event loop that count as drained.

One would do: a queue is filled before the feed yields, and the task that consumes it is
woken before the feed is resumed, so a queue seen empty after a yield is a queue whose work
is done. Three is the margin for a handler that hands work on through a queue this client
cannot see, and costs three turns of an idle loop.
"""

NS_PER_SECOND: Final = 1_000_000_000.0

QUEUE_SIZES: Final = ("cmd_qsize", "data_qsize", "evt_qsize", "req_qsize", "res_qsize")
"""Every queue-depth accessor a live engine may expose; each engine has some of them."""


def outstanding(engines: Sequence[object]) -> int:
    """How many messages are queued inside these engines, over every queue they expose.

    Asked of the engines rather than of one of them, because work moves between them: a
    point handled by the data engine becomes an order command on the risk engine, then on
    the execution engine, then a fill event coming back. Zero means the node has finished
    with everything it was given.
    """
    total = 0
    for engine in engines:
        for name in QUEUE_SIZES:
            sizer = getattr(engine, name, None)
            if sizer is not None:
                total += int(sizer())
    return total


class ReplayDataClient(LiveMarketDataClient):
    """A live data client that releases a held window of points, one settled step at a time.

    The points are given whole at construction, already in the order the engine would put
    them in, and `replay` releases them. Subscriptions are the engine's business: the client
    records them through the base class and releases every point regardless, exactly as a
    backtest hands its whole stream to the data engine and lets it route.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: Any,
        cache: Any,
        clock: Any,
        *,
        points: Sequence[Any] = (),
        speed: float = 0.0,
        settle_turns: int = SETTLE_TURNS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        alive: Callable[[], bool] = lambda: True,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(REPLAY_CLIENT),
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            config=LiveDataClientConfig(),
        )
        self._points = tuple(points)
        self._speed = float(speed)
        self._settle_turns = int(settle_turns)
        self._sleep = sleep
        self._alive = alive
        self._engines: tuple[object, ...] = ()
        self._released = 0
        self._paced = 0.0
        self._last_ts = 0

    # --- what the session sets and reads ------------------------------------

    def attach(self, *engines: object) -> None:
        """Name the engines whose queues the feed waits on between points."""
        self._engines = tuple(engines)

    @property
    def released(self) -> int:
        """How many points have been released so far."""
        return self._released

    @property
    def last_ts(self) -> int:
        """The `ts_init` of the last point released; zero before the first."""
        return self._last_ts

    @property
    def paced_s(self) -> float:
        """The wall-clock seconds this feed has asked to sleep for; zero at speed zero."""
        return self._paced

    # --- the feed ------------------------------------------------------------

    def pending(self) -> int:
        """The work still in flight inside the attached engines."""
        return outstanding(self._engines)

    async def settle(self) -> None:
        """Yield until the attached engines have been quiet for `settle_turns` turns.

        A node that has begun shutting down will never drain, because nothing is left to
        consume its queues; the wait ends there rather than spinning forever.
        """
        quiet = 0
        while quiet < self._settle_turns:
            await self._sleep(0)
            if not self._alive():
                return
            quiet = quiet + 1 if self.pending() == 0 else 0

    async def replay(self) -> int:
        """Release every held point, paced and settled, and return how many were released.

        The pace is the gap between two points' availability instants divided by `speed`;
        at speed zero there is no pace and the feed advances as soon as the node is quiet.
        A feed stops short when the node it is feeding stops, and the count it returns is
        how the caller learns that the window was not replayed whole.
        """
        previous: int | None = None
        for point in self._points:
            if not self._alive():
                break
            ts = int(point.ts_init)
            await self._pace(previous, ts)
            self._handle_data(point)
            self._released += 1
            self._last_ts = ts
            previous = ts
            await self.settle()
        return self._released

    async def _pace(self, previous: int | None, ts: int) -> None:
        if self._speed <= 0 or previous is None:
            return
        delay = (ts - previous) / NS_PER_SECOND / self._speed
        if delay <= 0:
            return
        self._paced += delay
        await self._sleep(delay)

    # --- the client's half of a subscription --------------------------------
    #
    # Every one is a no-op: the window is held in full and released in full, and which of
    # its points reach which component is the data engine's routing, not this client's.

    async def _connect(self) -> None: ...

    async def _disconnect(self) -> None: ...

    async def _subscribe(self, command: Any) -> None: ...

    async def _subscribe_instrument(self, command: Any) -> None: ...

    async def _subscribe_instruments(self, command: Any) -> None: ...

    async def _subscribe_quote_ticks(self, command: Any) -> None: ...

    async def _subscribe_trade_ticks(self, command: Any) -> None: ...

    async def _subscribe_bars(self, command: Any) -> None: ...

    async def _unsubscribe(self, command: Any) -> None: ...

    async def _unsubscribe_instrument(self, command: Any) -> None: ...

    async def _unsubscribe_instruments(self, command: Any) -> None: ...

    async def _unsubscribe_quote_ticks(self, command: Any) -> None: ...

    async def _unsubscribe_trade_ticks(self, command: Any) -> None: ...

    async def _unsubscribe_bars(self, command: Any) -> None: ...
