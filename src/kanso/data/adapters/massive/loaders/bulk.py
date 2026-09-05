"""`massive_bulk`: a day of aggregates per object, which is what a backfill should ask for.

The vendor serves the same aggregates twice. Over the REST API a decade of daily bars for
a thousand tickers is tens of thousands of cursor-paged requests; over the flat-file store
it is one gzipped CSV per trading day covering every ticker at once. So this transport is
worth reaching for over long history, and the request path serves the classes, types and
resolutions the store does not lay out. Nothing chooses between them on the operator's
behalf: the two ids never collide, and a spec picks a transport by naming one.

**Entitlement is measured with a GET and nothing else.** The store's listing is not scoped
by plan: a prefix the plan excludes lists cleanly back a decade and refuses every read
inside it. `discover` therefore lists the prefix to learn which days exist, reads one byte
of the newest object to learn whether the plan includes the class at all, and — only when
it does — bisects the listed days for the earliest one that can actually be read. That
earliest day is the history floor, and it is a measurement taken today rather than a
constant: where a plan grants a rolling window, the floor moves with the calendar.

The two questions are kept apart for the reason the whole adapter exists. A refusal at an
old date under a plan that serves the newest object is a **history floor**, not an
entitlement failure, and telling an operator to buy a plan they already hold is the most
expensive wrong answer here. The newest object answers "does the plan include this class",
the floor answers "how far back", and neither answer is ever read off a refusal's prose.

**Coverage is what was served.** A day the store holds no object for contributes no rows
and no coverage; the span in a manifest is measured from the points, so a request for a
range the store only partly holds is recorded as partly held rather than as complete.

**One aggregate, two spellings.** The store's `window_start` and the REST answer's `t` are
the same instant in different units: measured on one session, `1785729600000000000`
nanoseconds here and `1785729600000` milliseconds there both name 2026-08-03 04:00Z. That
instant is the start of the calendar day the vendor aggregates over and not the moment the
session opened — for `us_stocks_sip` it is midnight America/New_York, which is why it was
04:00Z on that August day and is 05:00Z in winter, and for a class the vendor anchors
elsewhere it is that class's own day. Nothing here reads the anchor, which is the point:
whatever the vendor opened the window at is what the bar closes a resolution after.
Reading one epoch with the other's unit puts every bar in 1970 or in the year 57000, and
neither is an error a later check catches, so the unit is stated once, here.

**A bar is timestamped at the close of its own window, by the request path's rule.** Since
the two transports serve one aggregate, the row is translated into the spelling the request
path reads and handed to that path's own builder: the close, the quantisation, the bar
type, the availability stamp and the refusal of a row the engine will not accept are then
one implementation rather than several that agree until one is edited. A backfill over the
store followed by a sync over the API therefore extends one series instead of interleaving
two conventions in it.

**What the store serves is unadjusted, and says so.** A flat-file object carries the prices
of the day it was written, with no split or dividend applied after it, so every dataset
discovered here is `adjusted=False`. The catalog files an adjusted and an unadjusted series
of the same bars in one place, so extending a bulk-loaded series with an `adjusted: true`
request-path spec would join two price bases into one series with nothing to mark the
seam — the two transports agree about *when* a bar is, and a spec that disagrees about
what its prices mean is the operator's to keep straight.

Because a bar is stamped at its close, a daily bar's reference time falls on the UTC day
after the one its window opened in, and spans here — as everywhere in kanso — are stated in
UTC days of `ts_event`. A window is therefore turned into the vendor window-start days that
serve it by the same function the request path widens its own requests with, and the bars
are kept by the day their close falls in, so a chunked backfill loses nothing at its seams
and both transports answer one operator range with one set of days.

Only the two aggregate types are read. The store also carries ticks, whose column layout
this adapter has not measured; inventing one would produce points that look right and are
not, so trades and quotes are served over the request path and a spec asking for them here
is refused by name. What *is* known about the aggregate layout is checked: the header is
compared against the columns this loader maps, and a file that does not match is refused
rather than read positionally, so a reordered column cannot swap open and close in silence.

Objects are fetched into the catalog's adapter cache, so a second ticker over the same day
re-reads from disk and an interrupted backfill re-reads what it already downloaded. A
download lands on a scratch name beside its object and is renamed onto it only once it has
finished, because the cache is consulted by whether the file exists: a run interrupted
mid-stream would otherwise leave a truncated object that every later run reads instead of
re-fetching, and it would never repair itself. Every ref is self-describing: what a load
needs is carried in the ref's request parameters and therefore in the manifest, which is
how `data sync` extends a dataset in a later process without the spec that first wrote it.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`Bar` is constructed from `Price` and `Quantity` at a declared precision and validates its
own OHLC ordering, and `BarType` renders as `SYMBOL.VENUE-step-AGGREGATION-PRICE-SOURCE`
for an `EXTERNAL`, `LAST`-price series — which is what data loaded from outside the engine
is. Both timestamps are nanosecond integers and the engine orders by `ts_init` alone.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, ClassVar, Final

from pydantic import Field, model_validator

from kanso.data.adapters.massive.client import Transport
from kanso.data.adapters.massive.errors import NotEntitledError
from kanso.data.adapters.massive.loaders.bars import (
    BARS_KIND,
    NS_PER_UNIT,
    Request,
    built,
    shift,
    vendor_ticker,
    vendor_window,
)
from kanso.data.adapters.massive.objectstore import Access, ObjectStore, Reply
from kanso.data.loader import DatasetRef, arrow_batches, checked, manifest_for, utc_day
from kanso.data.loaders.points import instrument_id
from kanso.data.manifest import PUBLICATIONS, Manifest, dataset_id
from kanso.data.publication import resolve as resolve_rule
from kanso.data.publication import stamp
from kanso.data.types import resolve_type
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty
from kanso.schemas.duration import Duration

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = [
    "BISECTED",
    "CLASSES",
    "COLUMNS",
    "FIRST_OBJECT",
    "RESOLUTIONS",
    "Bulk",
    "BulkLoader",
    "BulkSpec",
    "Coverage",
    "Series",
    "day_of",
    "key_for",
    "loader",
    "prefix_for",
]

CLASSES: Final[dict[str, str]] = {
    "stocks": "us_stocks_sip",
    "options": "us_options_opra",
    "forex": "global_forex",
    "indices": "us_indices",
}
"""The store's own top-level prefix for each asset class this adapter names.

The store lays out more than this — `global_crypto` among them — and what is listed here
is the intersection with the classes the adapter has a vendor key convention for. A class
in one and not the other would be accepted by a spec, spend a listing and an entitlement
read, and then fail on its own ticker, which is a contradiction rather than a refusal.

Which of these a plan can actually read is deliberately not here: it is measured with a
GET, per prefix, on the day it is asked."""

RESOLUTIONS: Final[dict[str, str]] = {"1d": "day_aggs_v1", "1m": "minute_aggs_v1"}
"""The two aggregate types the store carries, by the resolution kanso spells them with."""

SUFFIX: Final = ".csv.gz"
"""What an object's name ends in, and the reason nothing here uses a filesystem layer that
decompresses by extension: the bytes that are parsed must be the bytes that were fetched."""

COLUMNS: Final[tuple[str, ...]] = (
    "ticker",
    "volume",
    "open",
    "close",
    "high",
    "low",
    "window_start",
    "transactions",
)
"""The aggregate file's header, as the store writes it."""

FIELDS: Final[dict[str, str]] = {
    "open": "o",
    "high": "h",
    "low": "l",
    "close": "c",
    "volume": "v",
}
"""The file's column for each field the vendor's REST aggregate row spells with a letter.

The store and the API serve one aggregate under two spellings. Naming the correspondence
here is what lets a single row builder serve both transports."""

FIRST_OBJECT: Final = "first-object"
BISECTED: Final = "bisected"
"""How a floor was established: the oldest object listed was readable, or the earliest
readable object was found by halving the listed days."""

PARTIAL: Final = ".partial"
"""What a download in progress is named, beside the object it becomes. A file under this
name is never read: the cache holds an object only once its bytes are all there."""

PROBE_BYTES: Final = 1
"""How much of an object a probe reads. One byte answers the only question it asks, and a
ranged request comes back `206` rather than dragging a multi-gigabyte day across."""


def prefix_for(asset_class: str, resolution: str) -> str:
    """The store prefix holding one class's aggregates at one resolution."""
    if asset_class not in CLASSES:
        raise ValidationError(
            f"asset_class: {asset_class!r} is not a class this loader reads out of the "
            f"flat-file store; it reads {', '.join(sorted(CLASSES))}",
            remedy="the request-path loaders serve every other class this adapter names",
        )
    if resolution not in RESOLUTIONS:
        raise ValidationError(
            f"resolution: {resolution!r} is not an aggregate the flat-file store carries; it "
            f"carries {', '.join(sorted(RESOLUTIONS))}",
            remedy="the request-path loaders serve every other resolution and type",
        )
    return f"{CLASSES[asset_class]}/{RESOLUTIONS[resolution]}/"


def key_for(asset_class: str, resolution: str, day: date) -> str:
    """The object key holding one class's aggregates for one day."""
    return (
        f"{prefix_for(asset_class, resolution)}{day.year:04d}/{day.month:02d}/"
        f"{day.isoformat()}{SUFFIX}"
    )


def day_of(key: str) -> date | None:
    """The day an object key names, or `None` when the key is not one of this layout."""
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(SUFFIX):
        return None
    try:
        return date.fromisoformat(name[: -len(SUFFIX)])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Series:
    """Everything one dataset's load depends on, beyond the window it is asked over.

    Carried in the ref's request parameters and therefore in the manifest, so `data sync`
    extends a dataset in a later process from what the dataset itself records rather than
    from the spec that first wrote it. Nothing here is a credential.
    """

    asset_class: str
    resolution: str
    venue: str
    symbol: str
    ticker: str
    price_precision: int = 2
    size_precision: int = 0
    publication: str = "realtime"
    publication_rule: str | None = None

    @property
    def instrument(self) -> str:
        """The workspace instrument id this series is written under.

        The vendor key and the instrument id are two different names for one thing and the
        difference is load bearing: the key carries a market prefix, the id carries a
        venue, and putting the prefix in the id would say the vendor's word for a market
        where a venue belongs. The object's `ticker` column holds the key; every manifest,
        card and order is keyed by the id.
        """
        return str(instrument_id(self.symbol, self.venue))

    def request(self) -> Request:
        """This series as the vendor request the request path builds a bar from.

        Nothing is sent from it — the object was already read — and it exists so the bar
        itself is built by the one builder both transports share: the close rule, the
        quantisation, the bar type and the availability stamp are then one implementation
        rather than two that agree until one of them is edited.
        """
        return Request(
            symbol=self.symbol,
            venue=self.venue,
            ticker=self.ticker,
            asset_class=self.asset_class,
            dataset=BARS_KIND.dataset,
            resolution=self.resolution,
            adjusted=False,
            publication=self.publication,
            publication_rule=self.publication_rule,
            price_precision=self.price_precision,
            size_precision=self.size_precision,
        )

    def params(self) -> dict[str, str]:
        """The ref's request parameters: strings, and never a credential."""
        return {
            "asset_class": self.asset_class,
            "resolution": self.resolution,
            "venue": self.venue,
            "symbol": self.symbol,
            "ticker": self.ticker,
            "price_precision": str(self.price_precision),
            "size_precision": str(self.size_precision),
        }

    @classmethod
    def of(cls, ref: DatasetRef) -> Series:
        """The series a ref describes, refusing one that was not discovered here.

        The availability class is read off the ref rather than out of its parameters,
        because that is where the catalog keeps it and a manifest a later process rebuilds
        this from carries the ref's copy and not a second one.
        """
        params = ref.request_params or {}
        wanted = ("asset_class", "resolution", "venue", "symbol", "ticker")
        missing = [name for name in wanted if name not in params]
        if missing:
            raise ValidationError(
                f"dataset {ref.dataset_id!r} carries no bulk series ({', '.join(missing)} "
                "absent); refs come from BulkLoader.discover and are not built by hand"
            )
        return cls(
            asset_class=params["asset_class"],
            resolution=params["resolution"],
            venue=params["venue"],
            symbol=params["symbol"],
            ticker=params["ticker"],
            price_precision=int(params.get("price_precision", 2)),
            size_precision=int(params.get("size_precision", 0)),
            publication=ref.publication,
            publication_rule=ref.publication_rule,
        )


class BulkSpec(KansoModel):
    """A `massive_bulk` spec: one class, one resolution, one range, and its tickers.

    `start` and `end` are UTC days of a bar's reference time, which is its close, exactly
    as the request path reads the same two fields — so one range means one set of days
    whichever transport serves it. The spec names no session and no zone: the vendor's
    window start and the resolution settle the close between them, so a field for either
    would be a required place to state the vendor's own timestamp wrongly — worse than no
    field, because an operator has to supply it and it changes the answer.

    `publication` is the request path's field under the request path's rules, because it
    describes the plan rather than the transport: a delayed tier is delayed whichever way
    the day is fetched, and a spec that could declare it on one transport and not the other
    would file two datasets of one series under one key with two availability conventions
    in them. There is no adjustment field: the store's objects are unadjusted.
    """

    loader: str = "massive_bulk"
    asset_class: NonEmpty
    venue: NonEmpty
    instruments: list[NonEmpty] = Field(min_length=1)
    tickers: dict[str, NonEmpty] = Field(default_factory=dict)
    resolution: Duration = "1d"
    start: date
    end: date
    price_precision: int = Field(default=2, ge=0, le=9)
    size_precision: int = Field(default=0, ge=0, le=9)
    publication: str = "realtime"
    publication_rule: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> BulkSpec:
        prefix_for(self.asset_class, self.resolution)
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        if self.publication not in PUBLICATIONS:
            raise ValueError(
                f"publication: {self.publication!r} is not a publication class; expected one "
                f"of {', '.join(PUBLICATIONS)}"
            )
        if self.publication == "delayed":
            if not self.publication_rule:
                raise ValueError(
                    "publication_rule: a delayed dataset must name the rule its availability "
                    "timestamps are derived from"
                )
            resolve_rule(self.publication_rule)
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError(
                "instruments: one instrument is named twice, which is one dataset twice"
            )
        unknown = sorted(set(self.tickers) - set(self.instruments))
        if unknown:
            raise ValueError(
                f"tickers: {', '.join(unknown)} are not in this spec's instruments, so the "
                "override would never be used"
            )
        return self

    def series(self, symbol: str, on: date) -> Series:
        """The per-dataset record for one of this spec's instruments.

        The vendor key is derived from the class the way the request path derives it, so
        one spelling of that fact serves both transports, and an operator may override it
        for a symbol the convention does not reach.
        """
        return Series(
            asset_class=self.asset_class,
            resolution=self.resolution,
            venue=self.venue,
            symbol=symbol,
            ticker=self.tickers.get(symbol) or vendor_ticker(symbol, self.asset_class, on),
            price_precision=self.price_precision,
            size_precision=self.size_precision,
            publication=self.publication,
            publication_rule=self.publication_rule,
        )


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the store holds for one class and resolution, and how far back it serves.

    `days` is every day the store has an object for, ascending, which listing establishes
    without a plan. `floor` is the earliest of them that can actually be read, which only
    a GET establishes. `method` records which of the two ways the floor was found, and
    `probed_on` the day it was found, because a rolling window moves it.
    """

    asset_class: str
    resolution: str
    days: tuple[date, ...]
    floor: date
    probed_on: date
    method: str
    reads: tuple[Mapping[str, object], ...] = ()

    @property
    def newest(self) -> date:
        """The most recent day the store holds an object for."""
        return self.days[-1]

    def within(self, window: tuple[date, date]) -> tuple[date, ...]:
        """The days of `window` the store both holds and serves, ascending.

        `window` is stated in the days the vendor's windows *open* in, which is what an
        object is named by, and never in the reference days a caller asks a dataset over;
        turning one into the other is the loader's job and is done in one place.
        """
        low = max(window[0], self.floor)
        return tuple(day for day in self.days if low <= day <= window[1])

    def payload(self) -> dict[str, object]:
        """The measurement as a plain record, carrying no credential and no prose."""
        return {
            "asset_class": self.asset_class,
            "resolution": self.resolution,
            "objects": len(self.days),
            "floor": self.floor.isoformat(),
            "newest": self.newest.isoformat(),
            "probed_on": self.probed_on.isoformat(),
            "method": self.method,
            "reads": [dict(item) for item in self.reads],
        }


class Bulk:
    """The store's answer about one class and resolution, measured once and reused.

    A backfill walks years in chunks and would otherwise re-list and re-probe for each of
    them. Listing is one walk, entitlement is one read, and the floor costs about a dozen
    more; every chunk after that is judged against the cached answer with no request. The
    day is fixed at construction, so one long run sees one consistent set of floors.
    """

    def __init__(self, store: ObjectStore, *, as_of: date | None = None) -> None:
        self._store = store
        self.as_of = as_of or datetime.now(tz=UTC).date()
        self._coverage: dict[str, Coverage] = {}

    def coverage(self, asset_class: str, resolution: str) -> Coverage:
        """What the store holds and serves for this class, listed once and probed once."""
        cached = self._coverage.get(f"{asset_class}:{resolution}")
        if cached is not None:
            return cached
        found = self._measure(asset_class, resolution)
        self._coverage[f"{asset_class}:{resolution}"] = found
        return found

    def measured(self) -> tuple[Coverage, ...]:
        """Every answer established here, in the order it was reached."""
        return tuple(self._coverage.values())

    def _measure(self, asset_class: str, resolution: str) -> Coverage:
        prefix = prefix_for(asset_class, resolution)
        days = sorted(
            {day for entry in self._store.listing(prefix) if (day := day_of(entry.key)) is not None}
        )
        if not days:
            raise NotEntitledError(
                f"massive files: {prefix} holds no object of this layout, so there is no bulk "
                "history to read",
                remedy=(
                    "check the asset class and `[adapters.massive]`; the request-path loaders "
                    "serve the same series without the object store"
                ),
            )
        reads: list[Mapping[str, object]] = []
        newest = self._read(asset_class, resolution, days[-1], reads)
        if newest.access is not Access.SERVED:
            raise NotEntitledError(
                f"massive files: the newest object under {prefix} ({days[-1]}) is "
                f"{newest.access}, so this plan does not include the class at all",
                remedy=(
                    "this is not a history-floor failure — the store's newest object is "
                    "refused, so no date under this prefix would serve; drop the class from "
                    "the spec, or add it to the plan"
                ),
            )
        if self._read(asset_class, resolution, days[0], reads).access is Access.SERVED:
            return Coverage(
                asset_class,
                resolution,
                tuple(days),
                days[0],
                self.as_of,
                FIRST_OBJECT,
                tuple(reads),
            )
        floor = self._bisect(asset_class, resolution, days, reads)
        return Coverage(
            asset_class, resolution, tuple(days), floor, self.as_of, BISECTED, tuple(reads)
        )

    def _bisect(
        self,
        asset_class: str,
        resolution: str,
        days: Sequence[date],
        reads: list[Mapping[str, object]],
    ) -> date:
        """The earliest listed day the store serves, over a bracket already established.

        `low` is known not to serve and `high` is known to serve — the two reads above are
        exactly those bounds — so this halves a known bracket rather than searching an
        unknown one, at about a dozen reads for a decade of trading days.
        """
        low, high = 0, len(days) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if self._read(asset_class, resolution, days[middle], reads).access is Access.SERVED:
                high = middle
            else:
                low = middle
        return days[high]

    def _read(
        self,
        asset_class: str,
        resolution: str,
        day: date,
        reads: list[Mapping[str, object]],
    ) -> Reply:
        """One byte of one object, recorded as evidence before anything else happens."""
        reply = self._store.read(
            key_for(asset_class, resolution, day), byte_range=(0, PROBE_BYTES - 1)
        )
        reads.append(reply.evidence())
        reply.raise_for_transport()
        return reply


@dataclass(frozen=True)
class BulkLoader:
    """The flat-file loader: one object per day, filtered to the ticker of each dataset.

    Stateless in the sense the loader protocol requires — the same ref and window always
    produce the same points — while holding the store it reads through and the day its
    floors were measured on, so one command sees one consistent set of them.
    """

    id: ClassVar[str] = "massive_bulk"

    store: ObjectStore
    bulk: Bulk
    cache: Path | None = None

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per instrument, spanning what the store both holds and serves.

        The span's start is the measured floor rather than the date the spec asked for, so
        `backfill` clamps to a floor it was told rather than to one it guessed; its end is
        the newest object rather than today, so a run made before the day's file lands does
        not record a span the store cannot fill.

        Both are shifted by the resolution before they are stated, because a span is in
        reference days and the store's floor and newest object are in window-start days —
        the oldest daily object the plan serves is the oldest *session*, and that session's
        bar belongs to the following day. It is the same shift the request path applies to
        the floor it measures, so one range asked of both transports means one set of days.
        """
        parsed = BulkSpec.model_validate(dict(spec))
        found = self.bulk.coverage(parsed.asset_class, parsed.resolution)
        step = shift(parsed.resolution)
        served = (found.floor + step, found.newest + step)
        window = (max(parsed.start, served[0]), min(parsed.end, served[1]))
        if window[1] < window[0]:
            raise ValidationError(
                f"start/end: {parsed.start}..{parsed.end} and the range the store serves "
                f"({served[0]}..{served[1]}) do not meet",
                remedy=f"ask for a range inside {served[0]}..{served[1]}",
            )
        return [
            self._ref(parsed.series(symbol, window[1]), window) for symbol in parsed.instruments
        ]

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The ticker's bars over `window`: every object whose windows can close inside it.

        A delayed dataset is re-stamped from its rule here, exactly as the request path
        re-stamps the one it serves: availability belongs to the plan and not to the way a
        day was fetched, and a series a backfill filled over the store and a sync extended
        over the API would otherwise carry two conventions for when its bars became known.
        """
        series = Series.of(ref)
        points: Iterable[object] = _bars(self._rows(series, window), series, window)
        if series.publication == "delayed" and series.publication_rule:
            points = stamp(points, series.publication_rule)
        return checked(points, f"{self.id} {series.ticker} {ref.type}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the store actually served over the whole dataset span."""
        return manifest_for(ref, self.id, self.load(ref, ref.span))

    def _rows(self, series: Series, window: tuple[date, date]) -> Iterator[Mapping[str, str]]:
        """This ticker's rows from every object covering `window`, oldest day first.

        A day's object is fetched once and read per ticker, so a universe of a hundred
        names costs one download per day rather than a hundred.
        """
        if self.cache is not None:
            yield from self._under(self.cache, series, window)
            return
        with TemporaryDirectory() as scratch:
            yield from self._under(Path(scratch), series, window)

    def _under(
        self, root: Path, series: Series, window: tuple[date, date]
    ) -> Iterator[Mapping[str, str]]:
        """The same rows, out of objects held under `root`.

        `window` is in reference days and objects are named by the day their windows open
        in, so it is widened into window-start days by the function the request path widens
        its own requests with. The surplus rows are dropped rather than written, which is
        what makes a chunked backfill lose nothing where two chunks meet.
        """
        found = self.bulk.coverage(series.asset_class, series.resolution)
        for day in found.within(vendor_window(window, series.resolution)):
            key = key_for(series.asset_class, series.resolution, day)
            path = root / key
            if not path.is_file():
                _fetch(self.store, key, path)
            mine = [row for row in _read_object(path) if row.get("ticker") == series.ticker]
            yield from sorted(mine, key=lambda row: _epoch(row, series))

    def _ref(self, series: Series, window: tuple[date, date]) -> DatasetRef:
        return DatasetRef(
            dataset_id=dataset_id(series.instrument, "bar", series.resolution, False, window[1]),
            instrument=series.instrument,
            type="bar",
            resolution=series.resolution,
            span=window,
            adjusted=False,
            publication=series.publication,
            publication_rule=series.publication_rule,
            vendor="massive",
            vendor_dataset=prefix_for(series.asset_class, series.resolution),
            request_params=series.params(),
        )


def loader(
    ws: Workspace,
    *,
    transport: Transport | None = None,
    as_of: date | None = None,
    cache: Path | None = None,
) -> BulkLoader:
    """The bulk loader for one workspace, with its two object-store credentials resolved.

    The access key and the secret are resolved on their own, at the moment of use. The
    REST client's transport is borrowed so that the signed requests whose *answer* matters
    count against the same quota as every other request this adapter makes. The cache is
    the catalog's own adapter scratch space, so an interrupted backfill re-reads a day it
    already downloaded from disk rather than from the network.
    """
    from kanso.data.adapters.massive import ADAPTER
    from kanso.data.manifest import cache_path

    access_key_id, secret_key = ADAPTER.object_store_keys(ws)
    settings = ADAPTER.config(ws)
    store = ObjectStore(
        access_key_id,
        secret_key,
        transport=transport or ADAPTER.client(ws).transport,
        timeout_s=settings.timeout_s,
    )
    return BulkLoader(store=store, bulk=Bulk(store, as_of=as_of), cache=cache or cache_path(ws))


def _fetch(store: ObjectStore, key: str, path: Path) -> None:
    """Download one object into the cache, or leave the cache exactly as it was.

    The cache is consulted by whether a file is there, so a download that stops half way
    through — a signal, a dropped connection, a full disk — must not leave a file. It lands
    on a scratch name beside the object and is renamed onto it only once the stream has
    finished, which is one operation on every filesystem kanso runs on; a scratch file left
    by a killed process is overwritten by the next run rather than read. Without this a
    single interruption poisons the cache for good: every later run finds the truncated
    object, reads it, and fails on a decompression that no re-run repairs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}{PARTIAL}")
    try:
        store.fetch(key, scratch)
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def _read_object(path: Path) -> Iterator[Mapping[str, str]]:
    """The rows of one gzipped aggregate file, refusing a header it does not recognise.

    The file is decompressed here rather than by a filesystem layer inferring it from the
    extension, so what is parsed is exactly the bytes that were fetched. Every cell is read
    by column name, so a store that reorders its columns is read correctly and one that
    changes the set of them is refused — which is the case a positional reader would get
    wrong while producing bars that still validate.

    A file that is not readable gzip at all names its own path, because the only thing to
    do with one is delete it and an operator cannot delete a file nothing named.
    """
    try:
        with gzip.open(path, "rb") as handle:
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
            header = tuple(reader.fieldnames or ())
            if sorted(header) != sorted(COLUMNS):
                raise ValidationError(
                    f"{path.name}: the aggregate header is {', '.join(header) or '(empty)'} "
                    f"and this loader maps {', '.join(COLUMNS)}",
                    remedy=(
                        "the store changed its layout; the column map here has to be measured "
                        "again rather than guessed around"
                    ),
                )
            yield from reader
    except (OSError, EOFError) as exc:
        raise ValidationError(
            f"{path}: this cached object is not readable gzip ({type(exc).__name__}: {exc})",
            remedy=f"delete {path} and run the command again, which re-downloads the day",
        ) from None


def _bars(
    rows: Iterable[Mapping[str, str]], series: Series, window: tuple[date, date]
) -> Iterator[object]:
    """One bar per row, built by the request path's builder and kept by its own close day.

    Nothing about a bar is decided here. The row is translated out of the file's spelling
    and the request path's builder does the rest — including refusing a row the engine will
    not accept, in the words the request path refuses it in — so the two transports cannot
    stamp, quantise, name or reject one session two ways; what is left is dropping the
    surplus the widened read brought back.
    """
    request = series.request()
    for row in rows:
        point = built(BARS_KIND, request, _vendor_row(row, series))
        if point is not None and window[0] <= utc_day(point.ts_event) <= window[1]:
            yield point


def _vendor_row(row: Mapping[str, str], series: Series) -> dict[str, Any]:
    """One file row in the spelling the request path's aggregate row is written in.

    The file names a column per field and times the window in nanoseconds; the API names a
    letter per field and times the same window in milliseconds. Dividing is exact: an
    aggregate window opens on a whole second in both, which is the measurement the two
    epochs were established by.
    """
    found: dict[str, Any] = {"t": _epoch(row, series) // NS_PER_UNIT["ms"]}
    for column, letter in FIELDS.items():
        found[letter] = _number(row, column)
    return found


def _epoch(row: Mapping[str, str], series: Series) -> int:
    """`window_start` as UTC nanoseconds, which is the unit the store writes it in."""
    raw = row.get("window_start", "")
    try:
        return int(raw)
    except ValueError:
        raise ValidationError(
            f"window_start: {raw!r} is not a whole number of nanoseconds "
            f"({series.asset_class} {series.resolution})",
            remedy=(
                "the flat files time rows in nanoseconds where the REST aggregates use "
                "milliseconds; a value in another unit is a different file than this reads"
            ),
        ) from None


def _number(row: Mapping[str, str], name: str) -> float:
    """One decimal cell as a number, refused rather than repaired when it is not one.

    The file writes every cell as text and the API answers with numbers, so this is where
    the two spellings meet; the quantisation that follows is the request path's own, which
    is why one price cannot round two ways depending on the transport that served it.
    """
    raw = row.get(name, "")
    try:
        return float(raw)
    except ValueError:
        raise ValidationError(
            f"{name}: {raw!r} is not a number in a flat-file aggregate row"
        ) from None
