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
`[D - size, D+1 - size)` — are asked for as `after = D+1 - size`, `before = D - size - 1`,
and walked back page by page, each page's oldest open becoming the next `after`, until a
page comes back empty; they are yielded oldest first, one day in memory at a time.

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

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, Final

from kanso.data.loaders.points import bar_type, instrument_id, make_bar
from kanso.errors import ValidationError
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

__all__ = ["BAR_SIZES", "CANDLES", "PAGE", "OkxBarsLoader", "build_bar"]

CANDLES: Final = "/api/v5/market/history-candles"

PAGE: Final = 300
"""Candles per page: the most the endpoint answers, whatever `limit` asks for."""

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
        """Each day's closed candles, oldest first, one day in memory at a time."""
        step = self._step(series)
        for day in days(span):
            lower = day_ms(day) - step
            found: dict[int, Sequence[Any]] = {}
            after = day_ms(day + DAY) - step
            while True:
                rows = self._page(client, series, after=after, before=lower - 1)
                opened = [int(row[TS]) for row in rows]
                if not rows or min(opened) >= after:
                    break
                found.update(zip(opened, rows, strict=True))
                after = min(opened)
            for key in sorted(found):
                point = build_bar(series, found[key])
                if point is not None:
                    yield point

    # --- internals ------------------------------------------------------------

    def _step(self, series: Series) -> int:
        return int(parse_duration(str(series.resolution), "resolution").total_seconds() * 1000)

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
