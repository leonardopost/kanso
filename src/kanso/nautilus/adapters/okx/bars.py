"""`okx_bars`: the exchange's candles, stamped at their close.

Read from `GET /api/v5/market/history-candles?instId=&bar=&after=&before=&limit=`, with no
credential. Every fact below was measured against `us.okx.com` on 2026-09-30 and the
answers the suite replays were recorded then (`tests/nautilus/adapters/okx/fixtures/`).

**A row is an array, opened at its first field.** `[ts, o, h, l, c, vol, volCcy,
volCcyQuote, confirm]`: `ts` is the millisecond instant the candle opened, `vol` is in
contracts (`volCcy` in the coin, `volCcyQuote` in the quote currency — on `BTC-USDT-SWAP`
`vol` 1439.97 beside `volCcy` 14.3997 at a contract of 0.01 BTC), and `confirm` is `"1"` for
a candle that has closed and `"0"` for the one still forming, which the endpoint serves as
its newest row. So a bar is stamped at its **close**, `ts + size`, as both `ts_event` and
`ts_init` — the first instant its high, low and close were known — its volume is `vol` at
the definition's size precision, and a row whose `confirm` is not `"1"` is dropped.

**Rows come newest first, and both bounds are exclusive.** `after=X` answers the candles
that opened strictly before `X`, `before=Y` those that opened strictly after `Y`, and the two
together the newest `limit` of the candles between them. `limit` is honoured to 300 and a
larger one answers 300. So a UTC day's bars — closes in `[D, D+1)`, opens in
`[D - size, D+1 - size)` — are asked for within `after = D+1 - size`, `before = D - size - 1`.

**A day is asked for in page-sized slices, several at once.** Measured on 2026-09-30, a page
of 300 answered in 0.4 to 0.8 s, so one page after another ran at about 1.3 requests a
second, well under the table's quota. Pages are addressed by time, so the day's opens are cut
into slices of 300 candles back from its end — `after = D+1 - size - k x 300 x size` — and
every slice is asked for under the day's own `before`, with up to `rate_per_second` of them
in flight (never more than `IN_FLIGHT`) and the client's quota holding the rate. A slice's
page is its 300 candles when none is missing and, when some are, every candle of the slice
and older ones of the day besides. A page that holds nothing as old as the slice's oldest
open is walked on from its own oldest open, as a whole day once was, until a page reaches
the slice's oldest open or comes back empty; so the day holds every candle the endpoint
serves between its bounds, however the slices fall and whatever a page holds. The candles
are yielded oldest first, one day at a time, with no more than one slice beyond those in
flight fetched ahead of the day being yielded.

**The bar sizes are the endpoint's, spelled its way.** It answers code `51000`, "Parameter
bar error", for a size it does not serve (`2s`, `10s`, `4m`, `1h` in lower case, `3H`, `8H`,
`2W`), and its `6H`, `12H`, `1D` and `1W` candles open on Hong Kong time (`1D` at 16:00
UTC); the `utc` spellings open on UTC. `BAR_SIZES` maps each kanso size the endpoint serves
to the spelling whose candles open on UTC, and any other size is refused before a request.

**Its horizon depends on the size.** `1m` candles of `BTC-USDT-SWAP` reached back past
2021-01-01; `1s` candles reached 2026-03-14 on 2026-09-30 and not 2026-03-01, a window that
moves with the calendar. A window reaching before the horizon is refused naming it: day `D`
is served when a candle closing at or before `D 00:00` exists — one request, `after = D -
size + 1`, `limit = 1` — and when the window's first day is not, the first day that is is
found by bisection between it and today, a handful of requests, and named.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from itertools import groupby, islice
from typing import Any, ClassVar, Final

from kanso.data.loaders.points import bar_type, instrument_id, make_bar
from kanso.errors import ValidationError
from kanso.nautilus.adapters.okx.config import table
from kanso.nautilus.adapters.okx.history import (
    DAY,
    NS_PER_MS,
    HistoryLoader,
    HistorySpec,
    Series,
    answered,
    day_ms,
    days,
    units,
)
from kanso.nautilus.adapters.okx.reference import PublicClient
from kanso.nautilus.adapters.okx.venue import VENUE
from kanso.schemas.duration import parse_duration

__all__ = ["BAR_SIZES", "CANDLES", "IN_FLIGHT", "PAGE", "OkxBarsLoader", "build_bar"]

CANDLES: Final = "/api/v5/market/history-candles"

PAGE: Final = 300
"""Candles per page: the most the endpoint answers, whatever `limit` asks for."""

IN_FLIGHT: Final = 20
"""The most page requests in flight at once, whatever the table's rate: the exchange documents
this endpoint as admitting 20 requests in two seconds from one address, so more than that in
flight could only be throttled."""

BAR_SIZES: Final[dict[str, str]] = {
    "1s": "1s",
    "5s": "5s",
    "15s": "15s",
    "30s": "30s",
    "1m": "1m",
    "2m": "2m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6Hutc",
    "12h": "12Hutc",
    "1d": "1Dutc",
    "1w": "1Wutc",
}
"""kanso's bar size, as the endpoint's `bar` whose candles open on UTC."""

FIELDS: Final = 9
TS, OPEN, HIGH, LOW, CLOSE, VOLUME = range(6)
CONFIRM: Final = 8
CLOSED: Final = "1"


def build_bar(series: Series, row: Sequence[Any]) -> Any:
    """One closed candle as a bar stamped at its close; `None` for one still forming."""
    if len(row) != FIELDS:
        raise ValidationError(
            f"okx bars {series.inst_id}: a candle of {len(row)} fields where the endpoint "
            f"serves {FIELDS}: {list(row)[:FIELDS]}",
            remedy="the endpoint changed its row; the map here has to be measured again",
        )
    if row[CONFIRM] != CLOSED:
        return None
    size = parse_duration(str(series.resolution), "resolution")
    opened = int(row[TS])
    closed = (opened + int(size.total_seconds() * 1000)) * NS_PER_MS
    prices = [units(row[at], series.price_precision) for at in (OPEN, HIGH, LOW, CLOSE)]
    volume = units(row[VOLUME], series.size_precision)
    if volume is None or any(price is None for price in prices):
        raise ValidationError(
            f"okx bars {series.inst_id}: the candle opened at {opened} ms is "
            f"{list(row[: VOLUME + 1])}, which the definition's precision "
            f"(price {series.price_precision}, size {series.size_precision}) cannot hold "
            "exactly; kanso does not round a served price",
            remedy=(
                "the contract's tick or lot was different then; state the precision it had in "
                "the entry's `override` in instruments.yaml and resolve again"
            ),
        )
    o, h, low, c = (int(price or 0) for price in prices)
    try:
        return make_bar(
            bar_type(instrument_id(series.inst_id, VENUE), str(series.resolution)),
            (o, h, low, c),
            volume,
            series.price_precision,
            series.size_precision,
            closed,
            closed,
        )
    except ValueError as exc:
        raise ValidationError(
            f"okx bars {series.inst_id}: the candle opened at {opened} ms is not a bar the "
            f"engine accepts ({exc})",
            remedy="kanso will not repair a served price; leave the day out of the range",
        ) from None


@dataclass
class OkxBarsLoader(HistoryLoader):
    """The exchange's candles for the listed swaps, at the sizes its endpoint serves."""

    id: ClassVar[str] = "okx_bars"
    type: ClassVar[str] = "bar"
    vendor_dataset: ClassVar[str] = CANDLES
    aggregated: ClassVar[bool] = True

    def discover(self, spec: Any) -> Any:
        """As every loader here discovers, once the bar size is one the endpoint serves."""
        size = HistorySpec.model_validate(dict(spec)).resolution
        if size is not None and size not in BAR_SIZES:
            raise ValidationError(
                f"resolution: {size!r} is not a bar size the exchange's candle endpoint serves "
                "on UTC; it serves " + ", ".join(BAR_SIZES),
                remedy="name one of those sizes, or aggregate a smaller one yourself",
            )
        return super().discover(spec)

    def measure(self, client: PublicClient, series: Series, window: tuple[date, date]) -> None:
        """Refuse a window reaching before the endpoint's horizon for this size, naming it."""
        if self._served(client, series, window[0]):
            return
        today = self.latest() + DAY
        if not self._served(client, series, today):
            raise ValidationError(
                f"okx bars {series.inst_id}: the endpoint serves no {series.resolution} candles "
                f"of it at all, as of {today}",
                remedy="check the swap trades on the exchange, or ask for another bar size",
            )
        low, high = window[0], today
        while (high - low).days > 1:
            middle = low + DAY * ((high - low).days // 2)
            if self._served(client, series, middle):
                high = middle
            else:
                low = middle
        raise ValidationError(
            f"okx bars {series.inst_id}: the endpoint's {series.resolution} candles begin on "
            f"{high} (as of {today}), and the spec asks from {window[0]}; days before its "
            "horizon are not an empty market, they are not served",
            remedy=f"set `start: {high}` or later",
        )

    def points(
        self, client: PublicClient, series: Series, span: tuple[date, date]
    ) -> Iterator[Any]:
        """Each day's closed candles, oldest first, one day at a time, its slices asked for
        up to `rate_per_second` at once and never more than `IN_FLIGHT`; a candle that does not
        close inside its day is dropped, whatever the bounds should have kept out."""
        step = self._step(series)
        slices = (
            (day, after)
            for day in days(span)
            for after in range(day_ms(day + DAY) - step, day_ms(day) - step, -PAGE * step)
        )

        def fetched(one: tuple[date, int]) -> tuple[date, list[tuple[int, Sequence[Any]]]]:
            return one[0], self._slice(client, series, *one)

        width = min(table(self.workspace).rate_per_second, IN_FLIGHT)
        with closing(_ahead(fetched, slices, width)) as pages:
            for day, parts in groupby(pages, key=lambda page: page[0]):
                lower, after_day = day_ms(day) - step, day_ms(day + DAY) - step
                found = {key: row for _, rows in parts for key, row in rows}
                for key in sorted(found):
                    point = build_bar(series, found[key]) if lower <= key < after_day else None
                    if point is not None:
                        yield point

    # --- internals ------------------------------------------------------------

    def _step(self, series: Series) -> int:
        return int(parse_duration(str(series.resolution), "resolution").total_seconds() * 1000)

    def _slice(
        self, client: PublicClient, series: Series, day: date, after: int
    ) -> list[tuple[int, Sequence[Any]]]:
        """The candles of the day opened in `[after - PAGE x size, after)`, keyed by their open,
        and whatever older ones of the day the pages that held them carried.

        One page, unless it holds nothing as old as the slice's oldest open: then the slice is
        walked back from the page's own oldest open until a page reaches the slice's oldest
        open, comes back empty, or holds nothing older than it was asked for.
        """
        step = self._step(series)
        lower = day_ms(day) - step
        floor = max(lower, after - PAGE * step)
        found: list[tuple[int, Sequence[Any]]] = []
        while True:
            rows = self._page(client, series, after=after, before=lower - 1)
            opened = [int(row[TS]) for row in rows]
            if not rows or min(opened) >= after:
                return found
            found.extend(zip(opened, rows, strict=True))
            if min(opened) <= floor:
                return found
            after = min(opened)

    def _served(self, client: PublicClient, series: Series, day: date) -> bool:
        """Whether a candle closing at or before `day` 00:00 UTC is served."""
        return bool(self._page(client, series, after=day_ms(day) - self._step(series) + 1, limit=1))

    def _page(
        self,
        client: PublicClient,
        series: Series,
        *,
        after: int,
        before: int | None = None,
        limit: int = PAGE,
    ) -> tuple[Any, ...]:
        params = {
            "instId": series.inst_id,
            "bar": BAR_SIZES[str(series.resolution)],
            "after": str(after),
            "limit": str(limit),
        }
        if before is not None:
            params["before"] = str(before)
        rows = answered(client, CANDLES, params, self.pause)
        return tuple(row for row in rows if isinstance(row, list))


def _ahead[T, R](fetch: Callable[[T], R], items: Iterable[T], width: int) -> Generator[R]:
    """`fetch` of each item, in the items' order, with up to `width` of them running at once.

    One more item is handed to the pool as each result is taken, so a thread is never idle
    while the caller works on a result, and nothing is fetched further ahead than that. A
    fetch that raises raises here, in its turn; closing the iterator early cancels what has
    not started and waits for what has, so no request outlives the load that asked for it.
    """
    queue = iter(items)
    with ThreadPoolExecutor(max_workers=width, thread_name_prefix="okx-bars") as pool:
        pending: deque[Future[R]] = deque(pool.submit(fetch, item) for item in islice(queue, width))
        try:
            while pending:
                oldest = pending.popleft()
                pending.extend(pool.submit(fetch, item) for item in islice(queue, 1))
                yield oldest.result()
        finally:
            for future in pending:
                future.cancel()
