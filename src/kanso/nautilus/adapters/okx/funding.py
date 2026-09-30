"""`okx_funding`: every funding settlement of a swap, at the rate it was actually paid.

Read from `GET /api/v5/public/funding-rate-history?instId=&after=&before=&limit=`, with no
credential. Measured against `us.okx.com` on 2026-09-30, and the answers the suite replays
were recorded then (`tests/nautilus/adapters/okx/fixtures/`).

**The realised rate, never the published one.** A row is `{fundingRate, realizedRate,
fundingTime, method, formulaType, instId, instType}`. `realizedRate` is the rate the
settlement at `fundingTime` actually paid — the payment — and `fundingRate` the rate the
exchange published for that period. The two agreed on all 281 rows of `BTC-USDT-SWAP`
served that day, but where they differ the payment is what a held position was charged or
paid, so `realizedRate` is what becomes `kanso.data.types.Funding.rate` and `fundingRate`
is never read. A row with no finite `realizedRate` is refused rather than filled in from it.

**Stamped at the settlement.** `fundingTime` is the millisecond settlement instant; a
realised rate is known when it settles and not before, so `ts_event` = `ts_init` = that
instant. The endpoint lists settled periods only: asked at 16:22:30 UTC, its newest row
was the settlement at 16:00.

**Rows newest first, both bounds exclusive, 400 to a page.** `after=X` answers the
settlements strictly before `X`, `before=Y` those strictly after `Y`; `limit=400` answered
all 281 rows held — 8-hourly settlements from 2026-06-29 08:00 UTC — and `after` the oldest
of them answered none. So the endpoint's horizon is about three months, and moving: a
window is served newest page first, walking `after` back to its first day.

**The horizon is measured, and a window before it refused.** `discover` walks the history
from the newest settlement until a page comes back empty — two requests for a swap settling
every eight hours — and the first UTC day served is the day of the oldest settlement when
that settlement is at midnight, and the day after it otherwise, since a day served from its
second settlement is not a day served. A window reaching before it is refused naming it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Final

from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.loader import utc_day
from kanso.data.types import Funding
from kanso.errors import ValidationError
from kanso.nautilus.adapters.okx.history import (
    DAY,
    MS_PER_DAY,
    NS_PER_MS,
    HistoryLoader,
    Series,
    answered,
    day_ms,
)
from kanso.nautilus.adapters.okx.reference import PublicClient

__all__ = ["FUNDING", "PAGE", "OkxFundingLoader", "build_funding"]

FUNDING: Final = "/api/v5/public/funding-rate-history"

PAGE: Final = 400
"""Settlements per page: `limit=400` answered every row the endpoint held."""

REALISED: Final = "realizedRate"
SETTLED: Final = "fundingTime"


def build_funding(series: Series, row: Mapping[str, Any]) -> Funding:
    """One settlement as a funding point, at its realised rate and its settlement instant."""
    try:
        rate = Decimal(str(row.get(REALISED, "")))
    except InvalidOperation:
        rate = Decimal("NaN")
    if not rate.is_finite():
        raise ValidationError(
            f"okx funding {series.inst_id}: the settlement at {row.get(SETTLED)} ms states no "
            f"realised rate ({REALISED} {row.get(REALISED)!r}); the published rate is not the "
            "payment, so it is not read in its place",
            remedy="leave the day out of the range; report the row to the exchange",
        )
    instant = int(row[SETTLED]) * NS_PER_MS
    return Funding(
        instrument_id=InstrumentId.from_str(series.instrument),
        rate=float(rate),
        ts_event=instant,
        ts_init=instant,
    )


@dataclass
class OkxFundingLoader(HistoryLoader):
    """The realised funding of the listed swaps, back to the endpoint's horizon."""

    id: ClassVar[str] = "okx_funding"
    type: ClassVar[str] = "funding"
    vendor_dataset: ClassVar[str] = FUNDING

    def measure(self, client: PublicClient, series: Series, window: tuple[date, date]) -> None:
        """Refuse a window reaching before the oldest settlement served, naming the horizon."""
        oldest: int | None = None
        while True:
            rows = self._page(client, series, after=oldest)
            settled = [int(row[SETTLED]) for row in rows]
            if not rows or (oldest is not None and min(settled) >= oldest):
                break
            oldest = min(settled)
        if oldest is None:
            raise ValidationError(
                f"okx funding {series.inst_id}: the endpoint lists no settlement of it at all",
                remedy="check the swap is listed and has settled once, then load again",
            )
        first = utc_day(oldest * NS_PER_MS)
        horizon = first if oldest % MS_PER_DAY == 0 else first + DAY
        if window[0] < horizon:
            began = datetime.fromtimestamp(oldest / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
            raise ValidationError(
                f"okx funding {series.inst_id}: the endpoint's settlements begin at {began}, "
                f"so it serves whole UTC days from {horizon}, and the spec asks from "
                f"{window[0]}; days before its horizon are not served",
                remedy=f"set `start: {horizon}` or later",
            )

    def points(
        self, client: PublicClient, series: Series, span: tuple[date, date]
    ) -> Iterator[Any]:
        """The settlements of `span`, oldest first; a row outside it is dropped, whatever the
        endpoint's exclusive bounds should have kept out."""
        found: dict[int, Mapping[str, Any]] = {}
        after = day_ms(span[1] + DAY)
        while True:
            rows = self._page(client, series, after=after, before=day_ms(span[0]) - 1)
            settled = [int(row[SETTLED]) for row in rows]
            if not rows or min(settled) >= after:
                break
            found.update(zip(settled, rows, strict=True))
            after = min(settled)
        for key in sorted(found):
            if day_ms(span[0]) <= key < day_ms(span[1] + DAY):
                yield build_funding(series, found[key])

    def _page(
        self,
        client: PublicClient,
        series: Series,
        *,
        after: int | None,
        before: int | None = None,
    ) -> list[Mapping[str, Any]]:
        params = {"instId": series.inst_id, "limit": str(PAGE)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        data = answered(client, FUNDING, params, self.pause)
        return [row for row in data if isinstance(row, Mapping) and SETTLED in row]
