"""The live market data feed, and the fact that a feed is a choice of tape.

This is the default data client of a stage whose execution client runs on a wall clock: it
serves the bars a deployed version trades on, from the same broker the orders go to.

**The tape is the whole risk here.** The broker serves two of them. `sip` is the
consolidated tape — every venue's prints — and `iex` is one venue's slice of it, and they
are different series for the same instrument on the same session: measured on one AAPL day,
the consolidated tape opened at 309.58 and closed at 303.42 on 75.3 million shares while
the single venue opened at 309.765 and closed at 303.41 on 3.2 million. A strategy
researched on one and traded on the other is not the same strategy, and nothing about the
numbers themselves would say so — both are plausible prices for that day. So the tape is
never defaulted, never inferred and never silent:

* the configuration has no default and refuses to guess one, so a client cannot open on an
  undeclared feed at all;
* the tape is part of this client's engine id, so every log line, every data response and
  the node's own client registry name the tape the points came off;
* the tape a stage ran on is reported by `provenance` for the caller to record, and
  `check_feed` refuses a run whose declared tape contradicts the recorded one — a stage
  does not silently change series between one restart and the next.

**A bar is stamped at its close, consulting no zone.** The row's `t` is the instant the
aggregation window opened — for a daily bar, midnight of the exchange's own calendar day,
which is the same anchor the historical vendor in this build uses — and this build's rule,
settled once for every source, is that a bar's `ts_event` is that instant plus one
resolution step. So a daily bar from this feed and a daily bar of the same session from the
history the strategy was researched on land at the same nanosecond, and nothing compares
them through a calendar. The rule itself lives in this adapter's parser; this module only
decides which windows to ask for.

**A window that has not closed is not served.** The broker will happily return the bar of
the session in progress, and delivering it would hand a strategy a close that is not the
close and then a second, different bar carrying the same reference time. Only bars whose
close instant has passed are published, and each series remembers the last close it
delivered, so a repeated sweep over an overlapping window publishes nothing twice.

**A feed that has gone blind stops the stage.** After three consecutive failed sweeps the
client asks the system to shut down, naming the tape and the endpoint. A live stage that
keeps running on stale prices is a stage placing orders against a market it can no longer
see, and this milestone is the first one where those orders can be real.

**Quotes and prints are refused rather than approximated.** The broker publishes a schema
for both and neither was measured, and the streaming transport that carries them was not
measured either. A subscription for one is refused by name, which is visible, rather than
accepted and never delivered, which is not.

The credential travels in two headers and nowhere else — never a query parameter, never a
log line, never this client's `repr`, never the payload `provenance` returns.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
A custom live data client subclasses `LiveMarketDataClient`, constructed with `loop`,
`client_id`, `venue`, `msgbus`, `cache`, `clock`, `instrument_provider` and `config`; the
provider is type-checked against the engine's own `InstrumentProvider`. `venue=None`
declares a multi-venue client, which this is, because the venue of an instrument is the
venue it is listed on and this broker routes to several. The base class's public
`subscribe_bars` asserts the bar type is externally aggregated and records the
subscription itself, then drives the `_subscribe_bars` coroutine through `create_task`,
which logs an exception rather than letting it reach the loop — so a refusal raised there
is reported and does not kill the node. `_handle_data` sends a point to the
`DataEngine.process` endpoint, which on a `LiveDataEngine` enqueues it; `_handle_bars`
sends a `DataResponse` to `DataEngine.response`, which is how a historical request is
answered. `Clock.set_timer(name, interval, callback=...)` on a `LiveClock` fires without a
running event loop and `cancel_timer(name)` removes it, and `timestamp_ns` is wall time in
nanoseconds. `BarType` carries a `BarSpecification` of a step, a `BarAggregation` and a
`PriceType`, which is what a resolution is read back out of. `ShutdownSystem` is the
command the kernel acts on to stop a node.

The transport this adapter shares is synchronous and drives its own event loop, so it
cannot be called on the node's loop; every request here is therefore made on a worker
thread.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from nautilus_trader.common.messages import ShutdownSystem
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import BarAggregation, PriceType
from nautilus_trader.model.identifiers import ClientId, InstrumentId

from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus.adapters.alpaca import ID
from kanso.nautilus.adapters.alpaca.config import (
    DATA_HOST,
    Credentials,
    Feed,
    account,
    credential_names,
    endpoint,
    resolve,
)
from kanso.nautilus.adapters.alpaca.parsing import bar as bar_of
from kanso.nautilus.adapters.alpaca.parsing import rfc3339, symbol_of
from kanso.nautilus.adapters.alpaca.provider import provider as instrument_provider_for
from kanso.nautilus.adapters.alpaca.venue import serves
from kanso.nautilus.session import SHUTDOWN_TOPIC
from kanso.schemas import parse_duration

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Iterable, Mapping, Sequence

    from nautilus_trader.data.messages import (
        RequestBars,
        SubscribeBars,
        SubscribeInstrument,
        SubscribeInstruments,
        SubscribeQuoteTicks,
        SubscribeTradeTicks,
        UnsubscribeBars,
    )

    from kanso.nautilus.adapters.alpaca.config import Transport
    from kanso.workspace import Workspace

__all__ = [
    "BARS_KEY",
    "BARS_PATH",
    "DATA_CLIENT_FACTORY",
    "END_PARAM",
    "FEED_PARAM",
    "LIMIT_PARAM",
    "MAX_FAILURES",
    "MAX_PAGES",
    "PAGE_PARAM",
    "PAGE_TOKEN",
    "POLL_INTERVAL_S",
    "START_PARAM",
    "TIMEFRAMES",
    "TIMEFRAME_PARAM",
    "UNITS",
    "AlpacaDataClient",
    "AlpacaLiveDataClientFactory",
    "bars_of",
    "check_feed",
    "client_id_of",
    "data_client",
    "engine_client_id",
    "page",
    "resolution_of",
    "timeframe_of",
]

BARS_PATH: Final = "/v2/stocks/{symbol}/bars"
"""The one market data endpoint this client reads, measured on both tapes."""

BARS_KEY: Final = "bars"
PAGE_TOKEN: Final = "next_page_token"
"""The envelope keys the broker publishes around the rows. Only the *row* was measured, so
both published envelope shapes are read — a list for one symbol, a map of symbol to list
for several — and anything else yields no bars rather than a guessed reading."""

FEED_PARAM: Final = "feed"
TIMEFRAME_PARAM: Final = "timeframe"
START_PARAM: Final = "start"
END_PARAM: Final = "end"
LIMIT_PARAM: Final = "limit"
PAGE_PARAM: Final = "page_token"
"""The query parameters sent. `feed` is measured and always sent; the broker also offers an
`adjustment` parameter which was not measured and is therefore not sent, so its default
stands — which affects a historical request and not a bar that has just closed, since no
corporate action has been applied to a session that ended a moment ago."""

TIMEFRAMES: Final[dict[str, str]] = {"m": "Min", "h": "Hour", "d": "Day", "w": "Week"}
"""kanso's duration units as the broker's timeframe suffixes. There is no spelling for a
second, so a sub-minute bar is refused by name rather than rounded up to a minute."""

UNITS: Final[dict[BarAggregation, str]] = {
    BarAggregation.SECOND: "s",
    BarAggregation.MINUTE: "m",
    BarAggregation.HOUR: "h",
    BarAggregation.DAY: "d",
    BarAggregation.WEEK: "w",
}
"""The engine's bar aggregations as kanso's duration units, which is how a bar type is read
back into the resolution the parser stamps a bar with."""

MAX_PAGES: Final = 100
"""Pages one window may be walked over. Far past any legitimate answer for a live sweep,
and a bound on a cursor that does not end."""

POLL_INTERVAL_S: Final = 15.0
"""Seconds between sweeps. One request per subscribed series per sweep, so this and the
account's quota together bound how many series a stage may run — which is why a
subscription past that bound is refused rather than silently throttled into missing bars.
A caller that knows its resolution may widen it; a daily stage has no use for fifteen
seconds, and a minute stage has no use for more."""

SECONDS_PER_MINUTE: Final = 60.0

MAX_SERIES: Final = 1_000_000
"""The bound on subscribed series when no quota was stated: none worth speaking of. A
caller that passes the account's own rate gets the real one, which is what the builder does."""

MAX_FAILURES: Final = 3
"""Consecutive failed sweeps before the stage is asked to stop. One is a hiccup; three in a
row is a feed that is not coming back, and a stage that cannot see the market must not go
on placing orders into it."""

NS_PER_SECOND: Final = 1_000_000_000


# --- the tape -----------------------------------------------------------------


def check_feed(declared: Feed, recorded: str | None, *, subject: str) -> None:
    """Refuse a run whose declared tape contradicts the one already recorded.

    `None` is not a contradiction: nothing was recorded, so there is nothing to disagree
    with, and the caller records the declared tape instead. Anything else must match
    exactly — including a value that names no tape this broker serves, which is a record
    written by something that did not mean what this reader assumes.
    """
    if recorded is None:
        return
    text = recorded.strip().lower()
    if text == declared.value:
        return
    known = ", ".join(sorted(member.value for member in Feed))
    raise PreconditionError(
        f"{subject}: was recorded on the {recorded!r} tape and is configured for "
        f"{declared.value!r}; the two are different series for the same session, so this "
        "would not be the same strategy",
        remedy=(
            "set `feed` in the [adapters.alpaca] table to the tape this deployment was "
            f"recorded on, or deploy afresh on {declared.value!r} (the tapes are {known})"
        ),
    )


# --- bar types and windows ----------------------------------------------------


def resolution_of(bar_type: BarType) -> str:
    """The kanso resolution one bar type names, refusing a grain the broker has not got.

    The broker aggregates by time alone, so a bar type aggregated by volume, by value or by
    tick count has no request that would produce it and is refused by name — approximating
    one with a time bar would serve a strategy something other than what it subscribed to.
    """
    spec = bar_type.spec
    if spec.price_type != PriceType.LAST:
        raise ValidationError(
            f"bar_type: {bar_type} is priced on {spec.price_type!s}, and the broker's bars "
            "aggregate traded prices"
        )
    unit = UNITS.get(spec.aggregation)
    if unit is None:
        raise ValidationError(
            f"bar_type: {bar_type} is aggregated by {spec.aggregation!s}, which the broker "
            f"does not aggregate by; it aggregates by {', '.join(sorted(TIMEFRAMES.values()))}"
        )
    return f"{spec.step}{unit}"


def timeframe_of(resolution: str) -> str:
    """One kanso resolution as the broker's `timeframe` parameter.

    The broker's shortest bar is a minute, so a second-grained resolution is refused here
    rather than sent and silently answered at another grain, and a multiplier that is not a
    whole number above zero is refused rather than sent as text the broker would read as
    something else.
    """
    step, unit = resolution[:-1], resolution[-1:]
    suffix = TIMEFRAMES.get(unit)
    if suffix is None or not step.isdigit() or int(step) <= 0:
        raise ValidationError(
            f"resolution: {resolution!r} is not a bar size this broker serves; it serves "
            f"{', '.join(sorted(TIMEFRAMES.values()))}"
        )
    return f"{step}{suffix}"


def step_ns(resolution: str) -> int:
    """One resolution as nanoseconds, which is the distance from a window's open to close."""
    return int(parse_duration(resolution, "resolution").total_seconds()) * NS_PER_SECOND


# --- reading the wire ---------------------------------------------------------


def page(
    payload: Mapping[str, Any], symbol: str
) -> tuple[tuple[Mapping[str, Any], ...], str | None]:
    """One response's rows for `symbol`, and the token of the page after it.

    Both published envelope shapes are read — the single-symbol endpoint answers with a
    list under `bars` and the multi-symbol one with a map of symbol to list — because only
    the row itself was measured. An envelope of neither shape yields no rows, which is
    visible as a series that stopped, rather than an invented reading of an unknown shape.
    """
    held = payload.get(BARS_KEY)
    rows: Iterable[Any] = ()
    if isinstance(held, list):
        rows = held
    elif isinstance(held, dict):
        found = held.get(symbol)
        rows = found if isinstance(found, list) else ()
    token = payload.get(PAGE_TOKEN)
    following = token if isinstance(token, str) and token else None
    return tuple(row for row in rows if isinstance(row, dict)), following


def bars_of(
    rows: Sequence[Mapping[str, Any]],
    *,
    instrument: InstrumentId,
    resolution: str,
    price_precision: int,
    size_precision: int,
) -> tuple[Bar, ...]:
    """Every row that is a bar, stamped at the close of its own window.

    A row missing a field a bar needs yields nothing rather than a bar carrying a number
    nobody served, and the count of what was kept is what a caller sees.
    """
    made = []
    for row in rows:
        point = bar_of(
            row,
            instrument=instrument,
            resolution=resolution,
            price_precision=price_precision,
            size_precision=size_precision,
        )
        if point is not None:
            made.append(point)
    return tuple(made)


# --- identity -----------------------------------------------------------------


def engine_client_id(client_id: str, feed: Feed) -> ClientId:
    """The id this client is registered and logged under: the account, then the tape.

    Both halves are there deliberately. The account is what decides whose money an order
    beside this feed moves, and the tape is what decides whether the series is the one the
    strategy was researched on — so neither can be read off a log line by inference.
    """
    return ClientId(f"{client_id.upper()}-{feed.value.upper()}")


def client_id_of(engine_id: ClientId | str) -> str:
    """The kanso client id an engine client id was built from."""
    return str(engine_id).rsplit("-", 1)[0].lower()


def _keys(path: str) -> list[str]:
    """The rate-limit buckets a request counts against, coarsest last."""
    parts = [part for part in path.split("/") if part][:2]
    return ["/".join(parts), parts[0]] if len(parts) > 1 else parts


# --- the client ---------------------------------------------------------------


class AlpacaDataClient(LiveMarketDataClient):
    """The broker's live bar feed for one account, on one declared tape.

    Multi-venue, because the venue of an instrument is the venue it is listed on and this
    broker routes to several. The credential is held for the life of the client and travels
    only in the two request headers: it is in no message, no log line and no `repr`.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: Any,
        cache: Any,
        clock: Any,
        *,
        credentials: Credentials,
        feed: Feed,
        transport: Transport,
        instrument_provider: Any,
        data_url: str = DATA_HOST,
        poll_interval_s: float = POLL_INTERVAL_S,
        requests_per_minute: int | None = None,
        max_failures: int = MAX_FAILURES,
        config: LiveDataClientConfig | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=engine_client_id(credentials.account.client_id, feed),
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config or LiveDataClientConfig(),
        )
        self._credentials = credentials
        self._feed = feed
        self._transport = transport
        self._data_url = data_url
        self._interval = float(poll_interval_s)
        self._max_failures = int(max_failures)
        self._capacity = _capacity(requests_per_minute, self._interval)
        self._since: dict[BarType, int] = {}
        self._polling = False
        self._sweeps = 0
        self._skipped = 0
        self._failures = 0
        self._timer = f"{self.id}-bars"

    def __repr__(self) -> str:
        """The account and the tape, and never a credential."""
        return f"{type(self).__name__}({self.id}, feed={self._feed.value})"

    # --- what a caller reads ------------------------------------------------

    @property
    def feed(self) -> Feed:
        """The tape this client reads, which is a declaration and never a default."""
        return self._feed

    @property
    def client_id(self) -> str:
        """The kanso client id whose account this feed is opened with."""
        return self._credentials.account.client_id

    @property
    def since(self) -> dict[BarType, int]:
        """Per subscribed series, the close instant delivered up to."""
        return dict(self._since)

    @property
    def sweeps(self) -> int:
        """How many sweeps have run. `skipped` is how many were dropped as overlapping."""
        return self._sweeps

    @property
    def skipped(self) -> int:
        """Sweeps that found the previous one still in flight and stood down."""
        return self._skipped

    @property
    def failures(self) -> int:
        """Consecutive failed sweeps; reset by the first that succeeds."""
        return self._failures

    def provenance(self) -> dict[str, object]:
        """What this feed was, for the caller to record beside a stage's run.

        Carries no credential and no value: the account's id, the tape, the host and the
        cadence are what a later run is checked against by `check_feed`.
        """
        return {
            "broker": ID,
            "client": self.client_id,
            "feed": self._feed.value,
            "host": self._data_url,
            "poll_interval_s": self._interval,
        }

    # --- lifecycle ----------------------------------------------------------

    async def _connect(self) -> None:
        """Publish the instruments this broker can route for, then start the sweep.

        The definitions are the workspace's own, so connecting reads a local store and
        opens no socket: the first request this client makes is the first sweep, and a
        credential that the broker refuses is reported there.
        """
        await self._instrument_provider.load_all_async()
        for definition in self._instrument_provider.list_all():
            self._handle_data(definition)
        self._clock.set_timer(
            name=self._timer,
            interval=timedelta(seconds=self._interval),
            callback=self._on_timer,
        )

    async def _disconnect(self) -> None:
        """Stop sweeping. Nothing else is held open, because nothing else was opened."""
        if self._timer in self._clock.timer_names:
            self._clock.cancel_timer(self._timer)

    def _on_timer(self, event: Any) -> None:
        """The clock's callback: run one sweep as a task on the node's own loop."""
        self.create_task(self.poll(), log_msg=f"poll: {self.id}")

    # --- subscriptions ------------------------------------------------------

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        """Take a series on, from now forward.

        From now rather than from the beginning of the day: a live stage that replayed the
        session it started in the middle of would trade bars it could not have traded, and
        a strategy that wants history asks for it, which is a request and not a
        subscription.
        """
        bar_type = command.bar_type
        self._admit(bar_type)
        self._since.setdefault(bar_type, int(self._clock.timestamp_ns()))

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        """Drop a series, and the close instant it had been delivered up to."""
        self._since.pop(command.bar_type, None)

    async def _subscribe_instrument(self, command: SubscribeInstrument) -> None:
        """Publish one definition, which is served from the workspace's own store."""
        self._handle_data(self._definition(command.instrument_id))

    async def _subscribe_instruments(self, command: SubscribeInstruments) -> None:
        """Publish every definition this broker can route for."""
        await self._instrument_provider.load_all_async()
        for definition in self._instrument_provider.list_all():
            self._handle_data(definition)

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        """Refused: the quote wire and the stream that carries it were never measured."""
        raise self._unmeasured("quotes", command.instrument_id)

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        """Refused: the print wire and the stream that carries it were never measured."""
        raise self._unmeasured("trades", command.instrument_id)

    def _unmeasured(self, kind: str, instrument_id: InstrumentId) -> ValidationError:
        return ValidationError(
            f"{kind}: this broker's {kind} are not served by this adapter, so {instrument_id} "
            "would be subscribed and never delivered",
            remedy="subscribe to bars, which is the grain this feed serves",
        )

    def _servable(self, bar_type: BarType) -> None:
        """Refuse a series this feed cannot serve, before it is silently never delivered."""
        timeframe_of(resolution_of(bar_type))
        instrument_id = bar_type.instrument_id
        if not serves(instrument_id.venue.value):
            raise ValidationError(
                f"bar_type: {instrument_id} is listed on a venue this broker does not trade, "
                "so no order beside this feed could be routed for it"
            )
        self._definition(instrument_id)

    def _admit(self, bar_type: BarType) -> None:
        """The same, and the quota's bound on how many series one sweep may cover.

        A bound rather than a throttle: a sweep the quota cannot finish drops bars from the
        series at the end of it, and a stage that silently stopped seeing half its universe
        is worse than one that refused to start.
        """
        self._servable(bar_type)
        if bar_type not in self._since and len(self._since) >= self._capacity:
            raise PreconditionError(
                f"bar_type: this account's quota sweeps {self._capacity} series every "
                f"{self._interval:g}s, and {bar_type} would be one more",
                remedy=(
                    "raise `[adapters.alpaca] requests_per_minute` to the account's own "
                    "ceiling, widen the poll interval, or deploy fewer instruments"
                ),
            )

    # --- the sweep ----------------------------------------------------------

    async def poll(self) -> int:
        """One sweep of every subscribed series; the number of bars published.

        A sweep that finds the previous one still in flight stands down rather than queueing
        behind it: two sweeps over the same window would ask the broker twice for the same
        rows and spend the quota on an answer already in hand.
        """
        if self._polling:
            self._skipped += 1
            return 0
        self._polling = True
        try:
            self._sweeps += 1
            now = int(self._clock.timestamp_ns())
            published = 0
            for bar_type in tuple(self._since):
                published += await self._advance(bar_type, now)
            return published
        finally:
            self._polling = False

    async def _advance(self, bar_type: BarType, now: int) -> int:
        """Publish whatever has closed on one series since it was last delivered.

        A request is in flight across an await, and a strategy may unsubscribe while it is,
        so the series is looked for rather than assumed both before the request and after
        it: a series dropped mid-sweep is neither delivered nor left with a mark that would
        put it back in the next sweep.
        """
        since = self._since.get(bar_type)
        if since is None:
            return 0
        resolution = resolution_of(bar_type)
        try:
            served = await asyncio.to_thread(self.fetch, bar_type, since - step_ns(resolution), now)
        except KansoError as failure:
            self._failed(bar_type, failure)
            return 0
        self._failures = 0
        fresh = sorted(
            (point for point in served if since < point.ts_event <= now),
            key=lambda point: point.ts_event,
        )
        if bar_type not in self._since:
            return 0
        for point in fresh:
            self._handle_data(point)
        if fresh:
            self._since[bar_type] = fresh[-1].ts_event
        return len(fresh)

    def _failed(self, bar_type: BarType, failure: KansoError) -> None:
        """Count a failed sweep, and stop the stage once the feed is plainly gone."""
        self._failures += 1
        self._log.error(f"{bar_type} on the {self._feed.value} tape: {failure.message}")
        if self._failures < self._max_failures:
            return
        reason = (
            f"alpaca: the {self._feed.value} feed failed {self._failures} sweeps in a row "
            f"({failure.message})"
        )
        self._msgbus.publish(
            topic=SHUTDOWN_TOPIC,
            msg=ShutdownSystem(
                trader_id=self.trader_id,
                component_id=self.id,
                command_id=UUID4(),
                ts_init=int(self._clock.timestamp_ns()),
                reason=reason,
            ),
        )

    # --- requests -----------------------------------------------------------

    async def _request_bars(self, request: RequestBars) -> None:
        """Answer a historical bar request off the same tape the subscription reads.

        The same tape deliberately: a strategy that warmed its indicators on one series and
        traded another would decide on numbers it never traded against.
        """
        bar_type = request.bar_type
        self._servable(bar_type)
        now = int(self._clock.timestamp_ns())
        start = now if request.start is None else int(dt_to_unix_nanos(request.start))
        end = now if request.end is None else int(dt_to_unix_nanos(request.end))
        served = await asyncio.to_thread(self.fetch, bar_type, start, end, request.limit or None)
        closed = [point for point in served if point.ts_event <= end]
        self._handle_bars(
            bar_type,
            closed,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    # --- the wire -----------------------------------------------------------

    def fetch(
        self,
        bar_type: BarType,
        start_ns: int,
        end_ns: int,
        limit: int | None = None,
    ) -> tuple[Bar, ...]:
        """Every bar the broker serves for one series over one window, walked to the end.

        Synchronous, because the transport is: it drives its own event loop and cannot be
        called on the node's. Callers on the loop reach it through a worker thread.
        """
        instrument_id = bar_type.instrument_id
        definition = self._definition(instrument_id)
        resolution = resolution_of(bar_type)
        symbol = symbol_of(instrument_id)
        path = BARS_PATH.format(symbol=symbol)
        asked: dict[str, str] = {
            TIMEFRAME_PARAM: timeframe_of(resolution),
            FEED_PARAM: self._feed.value,
            START_PARAM: rfc3339(max(start_ns, 0)),
            END_PARAM: rfc3339(max(end_ns, 0)),
        }
        if limit is not None:
            asked[LIMIT_PARAM] = str(limit)
        rows: list[Mapping[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = asked if token is None else {**asked, PAGE_PARAM: token}
            served, token = page(self._get(path, params), symbol)
            rows.extend(served)
            if token is None:
                return bars_of(
                    rows,
                    instrument=instrument_id,
                    resolution=resolution,
                    price_precision=definition.price_precision,
                    size_precision=definition.size_precision,
                )
        raise KansoError(
            f"alpaca: {path} served more than {MAX_PAGES} pages of {resolution} bars on the "
            f"{self._feed.value} tape, which is a cursor that does not end",
            Exit.ERROR,
            remedy="narrow the window the request covers",
        )

    def _get(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        """One request, and its body as an object, or a failure naming what came back."""
        try:
            answer = self._transport(
                "GET",
                endpoint(self._data_url, path),
                params,
                self._credentials.headers(),
                _keys(path),
            )
        except KansoError:
            raise
        except Exception as exc:  # every fault below an answer is one outcome
            raise KansoError(
                f"alpaca: {path} could not be reached ({type(exc).__name__})",
                Exit.ERROR,
                remedy="check the network and the broker's status page, then re-run",
            ) from exc
        if answer.status in (401, 403):
            key_variable, secret_variable = credential_names(self.client_id)
            raise PreconditionError(
                f"alpaca: the market data host refused this account's key (HTTP "
                f"{answer.status}) for {path}",
                remedy=(
                    f"check that {key_variable} and {secret_variable} hold the "
                    f"{account(self.client_id).environment.value} account's own credentials"
                ),
            )
        if answer.status != 200:
            raise KansoError(
                f"alpaca: {path} answered HTTP {answer.status} for {params[TIMEFRAME_PARAM]} "
                f"bars on the {self._feed.value} tape",
                Exit.ERROR,
                remedy="re-run the command; if it repeats, check the broker's status page",
            )
        try:
            parsed = json.loads(answer.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise KansoError(
                f"alpaca: {path} answered with a body that is not JSON",
                Exit.ERROR,
                remedy="re-run the command; if it repeats, check the broker's status page",
            ) from None
        if not isinstance(parsed, dict):
            raise KansoError(
                f"alpaca: {path} answered with {type(parsed).__name__}, not an object",
                Exit.ERROR,
            )
        return parsed

    def _definition(self, instrument_id: InstrumentId) -> Any:
        """The instrument this feed's prices are quantised to, or a refusal naming it.

        The provider first, because it holds this broker's own view of what the workspace
        resolved, and the engine's cache after it, because a node adds what it is going to
        trade before it connects its clients.
        """
        found = self._instrument_provider.find(instrument_id)
        if found is None:
            found = self._cache.instrument(instrument_id)
        if found is None:
            raise ValidationError(
                f"instrument: {instrument_id} has no definition in this workspace, so a bar "
                "of it could not be quantised to the precisions it was certified at",
                remedy="resolve the universe before deploying it",
            )
        return found


def _capacity(requests_per_minute: int | None, interval_s: float) -> int:
    """How many series one quota can sweep at this cadence, never fewer than one."""
    if requests_per_minute is None:
        return MAX_SERIES
    return max(1, int(requests_per_minute * interval_s / SECONDS_PER_MINUTE))


def data_client(
    ws: Workspace,
    client_id: str,
    *,
    loop: asyncio.AbstractEventLoop,
    msgbus: Any,
    cache: Any,
    clock: Any,
    transport: Transport | None = None,
    instrument_provider: Any = None,
    recorded_feed: str | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    config: LiveDataClientConfig | None = None,
) -> AlpacaDataClient:
    """This broker's live feed for one workspace and one of its accounts.

    The tape is read from the workspace's `[adapters.alpaca]` table, which has no default,
    so a workspace that has not declared one is refused here rather than opened on a guess;
    `recorded_feed` is the tape a previous run of the same stage ran on, and a run that
    contradicts it is refused. The credential is resolved at this moment and the key is
    checked against the host it would address before anything is opened.
    """
    from kanso.nautilus.adapters.alpaca import BROKER

    settings = BROKER.config(ws)
    feed = settings.require_feed()
    check_feed(feed, recorded_feed, subject=f"stages: the {client_id} feed")
    return AlpacaDataClient(
        loop,
        msgbus,
        cache,
        clock,
        credentials=resolve(ws.root, client_id),
        feed=feed,
        transport=transport if transport is not None else BROKER.transport(ws),
        instrument_provider=(
            instrument_provider if instrument_provider is not None else instrument_provider_for(ws)
        ),
        data_url=settings.data_url,
        poll_interval_s=poll_interval_s,
        requests_per_minute=settings.requests_per_minute,
        config=config,
    )


class AlpacaLiveDataClientFactory(LiveDataClientFactory):
    """What the engine calls to build this feed, once a node has been told to.

    The configuration carries the workspace's path and the client's id and no value, so it
    is safe to write into a node's configuration and into a recorded session; the
    credential is resolved here, at the moment the client is opened. The id decides which
    account's key is read, and the workspace's own table decides the tape.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: Any,
        cache: Any,
        clock: Any,
    ) -> AlpacaDataClient:
        """One live feed for the account the configuration names, or the name it was given."""
        from pathlib import Path

        from kanso.nautilus.adapters.alpaca.factory import AlpacaDataClientConfig
        from kanso.workspace import find

        if not isinstance(config, AlpacaDataClientConfig):
            raise PreconditionError(
                f"alpaca: {type(config).__name__} is not this adapter's market data client "
                "configuration",
                remedy=f"configure the {name!r} client with an AlpacaDataClientConfig",
            )
        return data_client(
            find(Path(config.workspace)),
            account(config.client_id or name).client_id,
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )


DATA_CLIENT_FACTORY: Final = AlpacaLiveDataClientFactory
"""What this adapter's factory module delegates a market data client to."""
