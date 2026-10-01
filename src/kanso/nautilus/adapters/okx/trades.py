"""`okx_trades`: every print of a swap, from the exchange's daily trade archives.

The REST endpoint for past trades, `/api/v5/market/history-trades`, answers 100 prints a
request and about 80 days back — `BTC-USDT-SWAP` printed 3.56 million times on 2026-09-28 —
so this loader reads the archives the exchange publishes instead. Every fact below was measured
against `us.okx.com` and its file host on 2026-09-30, with no credential, and the answers
the suite replays were recorded then (`tests/nautilus/adapters/okx/fixtures/`).

**The listing.** `GET /api/v5/public/market-data-history?module=1&instType=SWAP&
instFamilyList=BTC-USDT&dateAggrType=daily&begin=&end=` lists one zip per day, each with
its `dateTs`, `filename`, `sizeMB` and `url` — on the exchange's file host, with a query
string of its own. It answers HTTP 400, code `50076`, for a range over ten days, so the
listing is asked in ranges of ten. It throttles hard, so every listing request is sent
`LISTING_GAP_S` after a pause of its own, on top of its quota (`reference.KEYED_QUOTAS`). `begin`
and `end` are read as the exchange's days, both included, and sent here at the exchange's
midnight, which is each archive's `dateTs`: a `begin` of 00:00 UTC on the newest archive's
day — eight hours into it — listed nothing, where the same offset lists an older day, so
every range is also asked from the day before the first archive it needs. BTC's archives
reach back through 2022; none is listed for 2021.

**An archive's day is the exchange's day, UTC+8.** `dateTs` is midnight in Hong Kong, and
the archive named `2023-01-01` holds the prints from 2022-12-31 15:59:41 UTC to 2023-01-01
15:59:51 UTC — the cut falls a few seconds either side of 16:00 UTC, and consecutive
archives continue each other's trade ids with none repeated. A UTC day `D` is therefore
served by two archives, the ones named `D` and `D+1`, and only when both are listed: a
day with one of them is a day with a third of its prints missing. An archive is not listed
as its day ends, and when it is was not measured: the archive of 2026-09-30, whose day ended
at 16:00 UTC, was still unlisted at 16:01, 16:52 and 17:03 UTC that day, when the newest
listed was 2026-09-29's, so the last UTC day served was 2026-09-28, two behind.

**The file.** One CSV per zip, named as the zip is, oldest print first. Its header is
`instrument_name,trade_id,side,price,size,created_time` through 2023 and gained a seventh
column, `source`, by 2026 — so columns are read by name, the six are required and any other
is ignored. `created_time` is the print's millisecond instant, and `size` is in
**contracts**, not the coin: on `USDC-USDT-SWAP`, a contract of 10 USDC, trade 3031605 is
`9.0` in the archive and `sz` 9 from the REST endpoint, whose sizes are contracts. `side`
is the taker's, as REST's is — the same trade reads `sell` in both.

**What a print becomes.** A `TradeTick` at the definition's price and size precision, its
aggressor the taker's side (`buy` a buyer, `sell` a seller), its trade id the exchange's,
and `ts_event` = `ts_init` = the print's instant: a print is public when it prints. Every
print is kept, whatever its `source`.

**Nothing is skipped in silence.** A range whose UTC days are not all served by listed
archives is refused at `discover`, naming those days and the archives they need; the newest
archives are two days behind, and the remedy names the last day served. A `load` that
`data sync` asks past the newest archive stops there, and one that meets a day missing
between two listed archives stops with the day's name. The zips are kept in the catalog's
adapter cache — a day is read from two of them and a backfill reads each twice — written
under a scratch name and renamed only once complete, and only once every member passes the
zip's own CRC-32: the file host sends a `Content-MD5`, but the engine's client hands back no
headers, so the archive's own check is the one there is. A file whose prints go back in
time is refused rather than sorted: the loader promises the engine non-decreasing
`ts_init`, one archive in memory at a time.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`TradeTick` requires a strictly positive size and a non-empty trade id of at most 36
characters, and carries an `AggressorSide`; `BUYER` and `SELLER` name the side that took
liquidity. `nautilus_pyo3.HttpResponse.headers` was measured empty for the file host's
answer, which carries `Content-MD5` and `Content-Length` when fetched with curl.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

from nautilus_trader.model.enums import AggressorSide

from kanso.data.loader import utc_day
from kanso.data.loaders.points import instrument_id, make_trade
from kanso.errors import Exit, KansoError, ValidationError
from kanso.nautilus.adapters.okx.config import ID
from kanso.nautilus.adapters.okx.history import (
    DAY,
    NS_PER_MS,
    HistoryLoader,
    Series,
    answered,
    day_ms,
    days,
    runs,
    units,
)
from kanso.nautilus.adapters.okx.reference import ARCHIVES, PublicClient
from kanso.nautilus.adapters.okx.venue import VENUE

__all__ = [
    "COLUMNS",
    "EXCHANGE_DAY",
    "LISTING_DAYS",
    "LISTING_GAP_S",
    "Archive",
    "OkxTradesLoader",
    "listed",
]

TRADES_MODULE: Final = "1"
"""The listing's `module` for daily trade archives; `2` lists candles, `4`-`6` books."""

LISTING_DAYS: Final = 10
"""The widest range the listing answers; eleven days answered HTTP 400, code `50076`."""

LISTING_GAP_S: Final = 2.0
"""The pause before every listing request, so no two are sent closer than this: measured on
2026-09-30, requests a second apart drew HTTP 429 three times in eight and requests two
seconds apart none in six. The quota cannot say it — the engine's admits a burst as large as
its rate — so a `discover` followed by its `load` would otherwise send two at once."""

EXCHANGE_DAY: Final = timedelta(hours=8)
"""How far the exchange's day, which names an archive, runs ahead of UTC."""

SWAP_SUFFIX: Final = "-SWAP"

COLUMNS: Final = ("instrument_name", "trade_id", "side", "price", "size", "created_time")
"""The columns every archive was measured to carry; `source` joined them by 2026."""

SIDES: Final[dict[str, AggressorSide]] = {
    "buy": AggressorSide.BUYER,
    "sell": AggressorSide.SELLER,
}

PARTIAL: Final = ".partial"


@dataclass(frozen=True, slots=True)
class Archive:
    """One listed archive: the exchange's day it is named for, its file and where it is."""

    day: date
    filename: str
    url: str


def listed(
    client: PublicClient,
    inst_id: str,
    first: date,
    last: date,
    pause: Any,
) -> dict[date, Archive]:
    """Every trade archive of `inst_id` the exchange lists for its days `first`..`last`."""
    family = inst_id.removesuffix(SWAP_SUFFIX)
    found: dict[date, Archive] = {}
    begin = first - DAY
    while begin < last:
        end = min(begin + DAY * (LISTING_DAYS - 1), last)
        pause(LISTING_GAP_S)
        data = answered(
            client,
            ARCHIVES,
            {
                "module": TRADES_MODULE,
                "instType": "SWAP",
                "instFamilyList": family,
                "dateAggrType": "daily",
                "begin": str(day_ms(begin) - _offset_ms()),
                "end": str(day_ms(end) - _offset_ms()),
            },
        )
        for archive in _archives(data):
            if first <= archive.day <= last:
                found[archive.day] = archive
        begin = end
    return found


def _offset_ms() -> int:
    return int(EXCHANGE_DAY.total_seconds() * 1000)


def _archives(data: tuple[Any, ...]) -> Iterator[Archive]:
    """The archives a listing names: one swap's, since a family holds one swap. A file of
    another instrument would be refused row by row, by the name every row carries. A file
    name is joined onto the cache directory and a URL is fetched, so an entry whose name is
    not a bare `.zip` name, or whose URL is not `https`, is refused rather than followed."""
    blocks = [block for block in data if isinstance(block, Mapping)]
    for block in blocks:
        for detail in block.get("details") or ():
            for entry in detail.get("groupDetails") or ():
                opened = datetime.fromtimestamp(int(entry["dateTs"]) / 1000, tz=UTC)
                name, url = str(entry["filename"]), str(entry["url"])
                if Path(name).name != name or name.startswith(".") or not name.endswith(".zip"):
                    raise ValidationError(
                        f"okx: the archive listing names a file {name!r}, which is not a bare "
                        "zip name",
                        remedy="the exchange changed its listing; measure it again",
                    )
                if urlsplit(url).scheme != "https":
                    raise ValidationError(
                        f"okx: the archive listing serves {name} from a URL that is not https",
                        remedy="the exchange changed its listing; measure it again",
                    )
                yield Archive((opened + EXCHANGE_DAY).date(), name, url)


@dataclass
class OkxTradesLoader(HistoryLoader):
    """Every print of the listed swaps, from the exchange's daily archives."""

    id: ClassVar[str] = "okx_trades"
    type: ClassVar[str] = "trade"
    vendor_dataset: ClassVar[str] = f"{ARCHIVES}?module={TRADES_MODULE}"

    def measure(self, client: PublicClient, series: Series, window: tuple[date, date]) -> None:
        """Refuse a window some UTC day of which the listed archives do not serve in full."""
        archives = listed(client, series.inst_id, window[0], window[1] + DAY, self.pause)
        served = _served(archives, window)
        missing = [day for day in days(window) if day not in served]
        if not missing:
            return
        needed = sorted({day for one in missing for day in (one, one + DAY)} - set(archives))
        within = (
            f"archives serve the UTC days {runs(sorted(served))} of that range"
            if served
            else "no UTC day of that range is served"
        )
        raise ValidationError(
            f"okx trades {series.inst_id}: UTC days {runs(missing)} are not served — they need "
            f"the exchange's archives of {runs(needed)} (named by its UTC+8 day, a UTC day "
            f"needs its own and the next), which it does not list; {within}",
            remedy=(
                "ask for days whose two archives are both listed; the newest archive is listed "
                f"two days behind, so the last UTC day served is at most {self.latest() - DAY}"
            ),
        )

    def points(
        self, client: PublicClient, series: Series, span: tuple[date, date]
    ) -> Iterator[Any]:
        """The prints of every served day of `span`, oldest first, one archive at a time."""
        archives = listed(client, series.inst_id, span[0], span[1] + DAY, self.pause)
        served = _served(archives, span)
        newest = max(archives, default=None)
        gaps = [
            day
            for day in days(span)
            if day not in served and newest is not None and day + DAY <= newest
        ]
        if gaps:
            raise ValidationError(
                f"okx trades {series.inst_id}: the exchange lists no archive for part of the UTC "
                f"days {runs(gaps)}, between archives it does list",
                remedy="leave those days out of the range; kanso does not load a day in part",
            )
        last = 0
        for day in sorted({day for one in served for day in (one, one + DAY)}):
            for point in self._read(self._fetched(client, archives[day]), series):
                if utc_day(point.ts_event) not in served:
                    continue
                if point.ts_event < last:
                    raise ValidationError(
                        f"okx trades {series.inst_id}: {archives[day].filename} goes back in "
                        "time; the loader promises the engine prints in order",
                        remedy="delete the cached archive and load again; report it if it repeats",
                    )
                last = point.ts_event
                yield point

    # --- internals ------------------------------------------------------------

    def _fetched(self, client: PublicClient, archive: Archive) -> Path:
        """The archive on disk, downloaded into the cache unless it is already whole there."""
        from kanso.data.manifest import cache_path

        root = self.cache or cache_path(self.workspace) / ID / "trades"
        path = root / archive.filename
        if path.is_file():
            return path
        response = client.fetch(archive.url, name=archive.filename)
        if response.status != 200:
            raise KansoError(
                f"okx: the archive {archive.filename} answered HTTP {response.status}",
                Exit.ERROR,
                remedy="re-run the command; if it repeats, check the exchange's status page",
            )
        try:
            damaged = zipfile.ZipFile(io.BytesIO(response.body)).testzip()
        except zipfile.BadZipFile:
            damaged = archive.filename
        if damaged is not None:
            raise KansoError(
                f"okx: the archive {archive.filename} arrived as {len(response.body)} bytes that "
                f"are not a whole zip ({damaged} fails its check)",
                Exit.ERROR,
                remedy="re-run the command, which downloads it again",
            )
        root.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(f"{path.name}{PARTIAL}")
        try:
            scratch.write_bytes(response.body)
            scratch.replace(path)
        finally:
            scratch.unlink(missing_ok=True)
        return path

    def _read(self, path: Path, series: Series) -> Iterator[Any]:
        """The prints of one archive, read by column name, streamed from the zip."""
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.namelist()
                if len(members) != 1:
                    raise ValidationError(
                        f"{path.name}: {len(members)} files where every archive measured holds "
                        "one CSV",
                        remedy="the exchange changed its archives; measure them again",
                    )
                with archive.open(members[0]) as handle:
                    rows = csv.reader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                    header = next(rows, [])
                    absent = [name for name in COLUMNS if name not in header]
                    if absent:
                        raise ValidationError(
                            f"{path.name}: its header is {', '.join(header) or '(empty)'} and "
                            f"lacks {', '.join(absent)}",
                            remedy="the exchange changed its archives; measure them again",
                        )
                    at = [header.index(name) for name in COLUMNS]
                    for line, row in enumerate(rows, start=2):
                        if len(row) != len(header):
                            raise ValidationError(
                                f"{path.name}: line {line} has {len(row)} fields and the header "
                                f"{len(header)}",
                                remedy="delete the cached archive and load again",
                            )
                        yield _print(
                            dict(zip(COLUMNS, [row[i] for i in at], strict=True)), series, path.name
                        )
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise ValidationError(
                f"{path}: this cached archive is not a readable zip ({type(exc).__name__})",
                remedy=f"delete {path} and run the command again, which downloads it again",
            ) from None


def _served(archives: Mapping[date, Archive], span: tuple[date, date]) -> set[date]:
    """The UTC days of `span` whose two archives are both listed."""
    return {day for day in days(span) if day in archives and day + DAY in archives}


def _print(row: Mapping[str, str], series: Series, name: str) -> Any:
    """One archive row as the engine's trade print, or a refusal naming the row."""
    price = units(row["price"], series.price_precision)
    size = units(row["size"], series.size_precision)
    side = SIDES.get(row["side"])
    if row["instrument_name"] != series.inst_id or None in (price, size, side):
        raise ValidationError(
            f"{name}: trade {row['trade_id']} is {dict(row)}, which is not a print of "
            f"{series.inst_id} at the definition's precision (price {series.price_precision}, "
            f"size {series.size_precision}) with a taker side of buy or sell",
            remedy=(
                "if the contract's tick or lot was different then, state the precision it had "
                "in the entry's `override` in instruments.yaml and resolve again"
            ),
        )
    instant = int(row["created_time"]) * NS_PER_MS
    try:
        return make_trade(
            instrument_id(series.inst_id, VENUE),
            int(price or 0),
            int(size or 0),
            SIDES[row["side"]],
            row["trade_id"],
            series.price_precision,
            series.size_precision,
            instant,
            instant,
        )
    except ValueError as exc:
        raise ValidationError(
            f"{name}: trade {row['trade_id']} is not a print the engine accepts ({exc})",
            remedy="kanso will not repair a served print; leave the day out of the range",
        ) from None
