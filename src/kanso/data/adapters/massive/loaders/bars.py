"""Aggregate bars over the request path, and the shape the three request loaders share.

Bars are the endpoint every class this adapter serves has, so the machinery all three
request loaders need lives here and the two tick loaders specialise it: the vendor ticker
a class's symbol maps to, the spec an operator writes, the entitlement gate `discover`
runs, the request `load` sends and the served span the manifest records are all the same
work, and only the endpoint, the row and the point built from it differ.

Four vendor facts shape this module, each measured rather than assumed.

**The aggregate timestamp is the window's start, in milliseconds.** The REST aggregate
row carries `t`, the Unix millisecond instant the aggregation window opened, while the
tick endpoints carry nanoseconds. A bar's `ts_event` in kanso is its **close**, so the
resolution is added to the window start and the point lands at the instant its period
ended — which is also the first instant it could be known, so a real-time bar has
`ts_init == ts_event` and no look-ahead. A loader that stamped a bar at its open would
hand a strategy the whole session's high, low and close at that session's first tick.

Because the close is the reference time, a daily bar's close falls on the UTC day after
the one its window opened in, and spans in kanso are stated in UTC days of `ts_event`.
The vendor is therefore asked over a window widened by the resolution at both ends, and
the rows are kept by the day their close falls in — so the days a spec asks for are the
days it gets, and no bar is lost at a chunk boundary.

**`limit` caps base aggregates before roll-up.** A small `limit` with a multiplier above
one returns an empty series, which would read as an absent history rather than as a
parameter mistake. So a limit is set only where the multiplier is one and no roll-up
happens; above one the request carries none and the cursor is walked instead.

**A range straddling the history floor succeeds, silently truncated.** The source answers
HTTP 200 with a series that begins at the floor and says nothing about the days it
dropped. Nothing here reads a short answer as a complete one: `discover` clamps the
dataset's span to the floor it measured, and the manifest records the span the points
actually covered, so the difference between asked and served is visible rather than
smoothed away.

**A futures year may be one digit.** `ESZ4` names a contract in some decade, and which
decade is only decidable against the range being asked for — resolving it against today
would read a 2014 contract as a 2024 one the moment the calendar rolled. The year is
resolved against the requested window and rendered in the vendor's two-digit form.

Entitlement is never concluded from the vendor's message, which is byte-identical for
four different conditions. `discover` asks `Entitlements`, which probes; a `load` that
meets a refusal mid-walk probes the same window before it says what happened.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`Bar` validates its own OHLC ordering and raises `ValueError` on a high below the low, so
a row whose fields contradict each other fails at construction rather than reaching the
catalog; that `ValueError` becomes a kanso validation failure naming the row's day.
`Price` and `Quantity` are fixed point, so vendor floats are quantised to the precisions
the spec declares. Every `Data` carries `ts_event` and `ts_init` as nanosecond integers,
and the engine orders, filters and clocks by `ts_init` alone.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from pydantic import Field, model_validator

from kanso.data.adapters.massive import CAPABILITIES, MassiveClient
from kanso.data.adapters.massive.client import Signal
from kanso.data.adapters.massive.entitlement import (
    UNITS,
    Endpoint,
    Entitlements,
    Probe,
    probe,
    raise_if_blocked,
    settled_end,
)
from kanso.data.adapters.massive.errors import NON_FATAL, MalformedRequestError, TransportError
from kanso.data.loader import DatasetRef, arrow_batches, checked, manifest_for, utc_day
from kanso.data.loaders.points import bar_type, instrument_id, make_bar
from kanso.data.manifest import PUBLICATIONS, Manifest, dataset_id
from kanso.data.publication import resolve as resolve_rule
from kanso.data.publication import stamp
from kanso.data.types import resolve_type
from kanso.errors import PreconditionError, ValidationError
from kanso.schemas.base import KansoModel, NonEmpty
from kanso.schemas.duration import Duration, parse_duration

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = [
    "AGGREGATE_LIMIT",
    "BARS_KIND",
    "FUTURES_MONTHS",
    "NS_PER_UNIT",
    "PREFIXES",
    "TICK_LIMIT",
    "Kind",
    "MassiveBarsLoader",
    "MassiveSpec",
    "Request",
    "RequestLoader",
    "aggregate",
    "bars_endpoint",
    "build_bar",
    "built",
    "futures_year",
    "instant",
    "numbers",
    "shift",
    "ticks",
    "vendor_ticker",
    "vendor_window",
]

NS_PER_UNIT: Final[dict[str, int]] = {name: 1_000_000_000 // parts for name, parts in UNITS.items()}
"""Nanoseconds per unit of the epochs the vendor times rows in, derived from the adapter's
own unit table so the REST millisecond epoch and the tick nanosecond epoch cannot drift."""

TIMESPANS: Final[dict[str, str]] = {
    "s": "second",
    "m": "minute",
    "h": "hour",
    "d": "day",
    "w": "week",
}
"""kanso's duration units as the vendor's aggregate timespans."""

AGGREGATE_LIMIT: Final = 50_000
"""Rows per aggregate page, sent **only** where the multiplier is one.

The vendor applies `limit` to the base aggregates it rolls up rather than to the
rolled-up series, so the same number with a multiplier above one caps the inputs and can
return an empty answer — an emptiness that says nothing about the history and everything
about the request. Above one no limit is sent and the cursor is walked instead."""

TICK_LIMIT: Final = 50_000
"""Rows per tick page. The tick endpoints roll nothing up, so their limit caps exactly the
rows that were asked for and is safe to set."""

PREFIXES: Final[dict[str, str]] = {
    "stocks": "",
    "options": "O:",
    "forex": "C:",
    "indices": "I:",
    "futures": "",
}
"""The prefix each asset class's tickers carry at this vendor. Futures carry none and are
spelled with a delivery month and a year instead."""

FUTURES_MONTHS: Final = "FGHJKMNQUVXZ"
"""The delivery-month codes, January through December, in the industry's own order."""

CENTURY: Final = 100
"""Years per century, for rendering a contract year in the vendor's two-digit form."""

PIVOT: Final = 70
"""Where a two-digit contract year is read as the last century rather than this one."""

OHLC: Final = ("o", "h", "l", "c")

_FUTURES: Final = re.compile(rf"^([A-Z0-9]{{1,4}}?)([{FUTURES_MONTHS}])([0-9]{{1,4}})$")

_OFFERED: Final[dict[str, tuple[str, ...]]] = {
    item.asset_class: item.datasets for item in CAPABILITIES.classes
}
"""What the adapter can ask for per class, taken from its own declaration so a spec naming
a class and a dataset that do not meet is refused before a request is made."""


# --- the vendor's spellings ---------------------------------------------------


def futures_year(digits: str, on: date) -> int:
    """The four-digit delivery year `digits` names, read against the range being asked for.

    A contract code carries one, two or four year digits and only the four-digit form is
    unambiguous. Two digits pivot at 1970, the convention every exchange's own listings
    use. One digit names a year in some decade, and the decade is decided by the window
    the operator asked for rather than by today: the same code means a different contract
    every ten years, so resolving it against the calendar would silently re-point a 2014
    backfill at 2024 the moment the decade turned.
    """
    if len(digits) == 4:
        return int(digits)
    if len(digits) == 3:
        raise MalformedRequestError(
            f"massive: {digits!r} is not a contract year; a futures code carries one, two "
            "or four year digits",
            remedy="write the contract as ESZ4, ESZ24 or ESZ2024",
        )
    value = int(digits)
    if len(digits) == 2:
        return 2000 + value if value < PIVOT else 1900 + value
    base = on.year - 5
    return base + (value - base) % 10


def vendor_ticker(symbol: str, asset_class: str, on: date) -> str:
    """The vendor's key for a workspace symbol in `asset_class`, on the range's own year.

    Every class but futures is the symbol behind a fixed prefix, and a symbol that already
    carries its prefix is left alone so an operator may write either. Futures are the
    exception: their key is a root, a delivery month and a year the vendor spells with two
    digits, and `on` is what decides which decade a one-digit year names.
    """
    prefix = PREFIXES.get(asset_class)
    if prefix is None:
        raise MalformedRequestError(
            f"massive: {asset_class!r} is not an asset class this adapter serves; it serves "
            f"{', '.join(sorted(PREFIXES))}"
        )
    if asset_class != "futures":
        return symbol if not prefix or symbol.startswith(prefix) else f"{prefix}{symbol}"
    found = _FUTURES.match(symbol.upper())
    if found is None:
        raise MalformedRequestError(
            f"massive: {symbol!r} is not a futures contract; expected a root, a delivery "
            f"month from {FUTURES_MONTHS} and a year, as in ESZ4",
            remedy="name the contract, not the product; a continuous series is not a contract",
        )
    root, month, digits = found.groups()
    return f"{root}{month}{futures_year(digits, on) % CENTURY:02d}"


def aggregate(resolution: str) -> tuple[int, str]:
    """A kanso bar size as the vendor's multiplier and timespan.

    The multiplier is what decides whether a limit may be sent at all, so the two are read
    together and never separately.
    """
    step = int(resolution[:-1])
    unit = resolution[-1]
    if step <= 0 or unit not in TIMESPANS:
        raise MalformedRequestError(
            f"massive: {resolution!r} is not a bar size this vendor aggregates; its "
            f"timespans are {', '.join(TIMESPANS.values())}"
        )
    return step, TIMESPANS[unit]


def shift(resolution: str | None) -> timedelta:
    """How far a point's reference time lies beyond the day its vendor window opened.

    Zero for a tick and for any bar shorter than a day; one day for a daily bar, seven for
    a weekly one. It is what turns a floor stated in the vendor's window-start days into a
    floor stated in the `ts_event` days kanso's spans are counted in.
    """
    if resolution is None:
        return timedelta(0)
    return timedelta(days=parse_duration(resolution, "resolution").days)


def vendor_window(window: tuple[date, date], resolution: str | None) -> tuple[date, date]:
    """The window to ask the vendor for, to serve `window` in `ts_event` days.

    Widened by the resolution — and by at least a day — at both ends: a bar is stamped at
    its close, so the point that belongs to the first day of the window opened before it,
    and the point that opened on the last day closes after it. Asking wide and keeping by
    the close is what makes a chunked backfill lose nothing at its seams; the surplus rows
    are dropped rather than written.
    """
    margin = max(shift(resolution), timedelta(days=1))
    return (window[0] - margin, window[1] + margin)


# --- reading a row ------------------------------------------------------------


def instant(row: Mapping[str, Any], name: str, unit: str) -> int | None:
    """A row's timestamp field as UTC nanoseconds, or `None` when it carries none.

    The unit is the endpoint's, never guessed: the aggregate rows are milliseconds since
    the epoch and the tick rows are nanoseconds, and the two are indistinguishable in a
    column of integers for any epoch a workspace cares about.
    """
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) * NS_PER_UNIT[unit]


def numbers(row: Mapping[str, Any], names: Sequence[str]) -> list[float] | None:
    """Every named field as a number, or `None` when any of them is absent or not one.

    A row missing a field the point needs is dropped rather than filled in: a bar with no
    close is not a bar, and inventing one would put a price in the catalog that the source
    never served. The drop is visible, because the row count and the span are measured
    from what was kept.
    """
    found: list[float] = []
    for name in names:
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        found.append(float(value))
    return found


def ticks(value: float, precision: int) -> int:
    """A vendor float as a whole number of increments at the declared precision."""
    return int(math.floor(value * 10**precision + 0.5))


# --- the spec and the request it discovers ------------------------------------


class MassiveSpec(KansoModel):
    """A request-path spec: one asset class, one type, and the instruments it names.

    The venue is the operator's, not the vendor's: kanso instrument ids are `SYMBOL.VENUE`
    and a vendor is not a venue, so nothing here invents one. `tickers` overrides the
    derived vendor key for a symbol whose spelling the convention does not reach.

    `publication` defaults to `realtime`, which is what a real-time entitlement serves. A
    plan on a delayed tier declares `delayed` and names the rule its availability comes
    from, and the loader then stamps every point from that rule.
    """

    loader: str | None = None
    asset_class: NonEmpty
    instruments: list[NonEmpty] = Field(min_length=1)
    venue: NonEmpty
    tickers: dict[str, NonEmpty] = Field(default_factory=dict)
    start: date
    end: date
    resolution: Duration | None = None
    adjusted: bool = False
    adjustment_basis: str | None = None
    price_precision: int = Field(default=2, ge=0, le=9)
    size_precision: int = Field(default=0, ge=0, le=9)
    publication: str = "realtime"
    publication_rule: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> MassiveSpec:
        if self.asset_class not in _OFFERED:
            raise ValueError(
                f"asset_class: {self.asset_class!r} is not a class this adapter serves; it "
                f"serves {', '.join(sorted(_OFFERED))}"
            )
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
        unknown = sorted(set(self.tickers) - set(self.instruments))
        if unknown:
            raise ValueError(
                f"tickers: {', '.join(unknown)} are not in this spec's instruments, so the "
                "override would never be used"
            )
        return self


@dataclass(frozen=True, slots=True)
class Request:
    """One dataset's vendor request, as a `DatasetRef` carries it and `load` rebuilds it.

    Everything a request needs and nothing a credential touches: the key travels in a
    header and never reaches these parameters, which a manifest records verbatim.

    `floor` is the day the source was measured to serve this series from, so a chunk near
    the beginning of history never asks below it — the source truncates such a range
    silently, and asking for days that cannot exist wastes a request per chunk.
    """

    symbol: str
    venue: str
    ticker: str
    asset_class: str
    dataset: str
    resolution: str | None
    adjusted: bool
    publication: str
    publication_rule: str | None
    price_precision: int
    size_precision: int
    adjustment_basis: str | None = None
    floor: date | None = None
    probed_on: date | None = None

    @property
    def instrument(self) -> str:
        """The workspace instrument id this dataset belongs to."""
        return str(instrument_id(self.symbol, self.venue))

    def params(self) -> dict[str, str]:
        """The request parameters a ref and a manifest carry: strings, and no credential."""
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "dataset": self.dataset,
            "price_precision": str(self.price_precision),
            "size_precision": str(self.size_precision),
            "adjustment_basis": self.adjustment_basis or "",
            "floor": self.floor.isoformat() if self.floor else "",
            "probed_on": self.probed_on.isoformat() if self.probed_on else "",
        }

    @classmethod
    def of(cls, ref: DatasetRef) -> Request:
        """The request a ref carries, or a refusal when the ref did not come from here."""
        params = ref.request_params or {}
        if "ticker" not in params:
            raise MalformedRequestError(
                f"dataset {ref.dataset_id!r} carries no vendor request; refs come from this "
                "loader's own discover and are not built by hand",
                remedy="run the loader through `kanso data load`, which discovers them",
            )
        return cls(
            symbol=params["symbol"],
            venue=params["venue"],
            ticker=params["ticker"],
            asset_class=params["asset_class"],
            dataset=params["dataset"],
            resolution=ref.resolution,
            adjusted=ref.adjusted,
            publication=ref.publication,
            publication_rule=ref.publication_rule,
            price_precision=int(params["price_precision"]),
            size_precision=int(params["size_precision"]),
            adjustment_basis=params.get("adjustment_basis") or None,
            floor=_day(params.get("floor")),
            probed_on=_day(params.get("probed_on")),
        )


def _day(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


# --- the loader -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Kind:
    """What separates one request loader from the next: an endpoint and a row.

    Composition rather than inheritance because that is the whole of the difference: the
    spec, the entitlement gate, the windowing and the manifest are identical for bars,
    trades and quotes, and a subclass that overrode only two members would hide that.
    """

    type: str
    dataset: str
    endpoint: Callable[[Request], Endpoint]
    build: Callable[[Request, Mapping[str, Any]], Any]
    aggregated: bool = False
    """Whether this type has a bar size. It is what makes a resolution required of a bar
    spec and refused of a tick one, and neither can be left to the endpoint to discover:
    a bar with no size has no timespan to ask for, and a tick with one is a spec whose
    author expected an aggregation that will not happen."""


def built(kind: Kind, request: Request, row: Mapping[str, Any]) -> Any:
    """One vendor row as a point, turning the engine's own row-level refusal into kanso's.

    A function beside the kinds rather than a method on the loader, because the flat-file
    transport builds the same points from the same builder and has to fail the same way: a
    row the engine refuses is one vendor answer, and reporting it as a bare `ValueError`
    over one transport and as a named validation failure over the other would make an
    operator's remedy depend on which of the two happened to serve the day.
    """
    try:
        return kind.build(request, row)
    except ValueError as exc:
        raise ValidationError(
            f"massive {kind.dataset} {request.ticker}: a row the source served is not a "
            f"{kind.type} the engine accepts ({exc})",
            remedy="report the row to the vendor; kanso will not repair a served price",
        ) from None


@dataclass
class RequestLoader:
    """The request path behind kanso's loader interface, for one data type.

    Opened by the adapter registry with the workspace whose credential and quota it uses;
    a client may be passed instead, which is how the suite replays frozen bodies with no
    network and no credential. The connection is memoised because the quota lives in it: a
    fresh client per chunk would be a fresh quota per chunk, which is no quota at all.

    Stateless in the sense the interface requires — the same spec discovers the same
    datasets and the same ref and window load the same points — since the only thing kept
    between calls is the connection.
    """

    id: ClassVar[str]
    kind: ClassVar[Kind]

    workspace: Workspace | None = None
    client: MassiveClient | None = None
    as_of: date | None = None
    _opened: MassiveClient | None = field(default=None, init=False, repr=False, compare=False)

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per instrument, spanning what this plan will actually serve.

        Entitlement is probed once per grain and the history floor measured once per
        series, both by `Entitlements`; the span is then clamped to the floor and to the
        last settled day, so `backfill` walks from a floor that was measured rather than
        assumed and nothing asks for a session that has not happened.

        A series the plan excludes, and one whose whole range is older than the source
        holds, are non-fatal: they end that dataset and leave the rest of the spec alone,
        because a universe rarely entitles every class it names. When every series is
        blocked the first refusal is raised, since a spec that discovered nothing would
        otherwise look like a spec that named nothing.
        """
        parsed = MassiveSpec.model_validate(dict(spec))
        self._shape(parsed)
        client = self._open()
        entitlements = Entitlements(client, as_of=self.as_of)
        window = (parsed.start, parsed.end)
        asked = vendor_window(window, parsed.resolution)
        step = shift(parsed.resolution)
        latest = settled_end(entitlements.as_of) + step
        found: list[DatasetRef] = []
        blocked: list[Probe] = []
        for symbol in parsed.instruments:
            ticker = parsed.tickers.get(symbol) or vendor_ticker(
                symbol, parsed.asset_class, window[1]
            )
            request = self._request(parsed, symbol, ticker, entitlements.as_of)
            endpoint = self.kind.endpoint(request)
            answer = entitlements.check(ticker, parsed.asset_class, dataset=endpoint, window=asked)
            if answer.outcome in NON_FATAL:
                blocked.append(answer)
                continue
            raise_if_blocked(answer)
            floor = (
                answer.floor
                or entitlements.floor(ticker, parsed.asset_class, dataset=endpoint).floor
            )
            span = (max(window[0], floor + step), min(window[1], latest))
            if span[0] > span[1]:
                raise MalformedRequestError(
                    f"massive: {self.kind.dataset} for {ticker} would serve nothing over "
                    f"{window[0]}..{window[1]}; the source serves this series from {floor} "
                    f"and no later than {latest}",
                    remedy=f"ask for a range inside {floor + step}..{latest}",
                )
            found.append(self._ref(parsed, replace(request, floor=floor), span))
        if not found and blocked:
            raise_if_blocked(blocked[0])
        return found

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[Any]:
        """The dataset's points over `window`, in the availability order the engine wants.

        Nothing is re-probed here: `discover` established the plan and the floor, and a
        backfill would otherwise pay for both on every chunk. A refusal that arrives
        anyway is not read as an absence — it is probed, and the outcome it establishes is
        what fails.
        """
        request = Request.of(ref)
        span = ref.window(window)
        if span is None:
            return iter(())
        points = self._points(request, span)
        if request.publication == "delayed" and request.publication_rule:
            points = stamp(points, request.publication_rule)
        return checked(points, f"massive {self.kind.dataset} {request.ticker}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the source served over the whole dataset span, measured from the points.

        It fetches the span again, which is what makes the answer honest: the span, the
        row count and the checksum come from the stream rather than from the request, so a
        range the vendor truncated at HTTP 200 is recorded as the shorter one it was.
        """
        request = Request.of(ref)
        return manifest_for(ref, self.id, self.load(ref, ref.span), request.adjustment_basis)

    # --- internals ------------------------------------------------------------

    def _shape(self, spec: MassiveSpec) -> None:
        """Refuse what cannot be asked for at all, before a request is spent finding out.

        Two shapes never reach the vendor: a dataset the class does not carry here, and a
        resolution that disagrees with the type — a bar with no size has no timespan to
        ask for, and a tick with one names an aggregation this endpoint does not perform.
        """
        offered = _OFFERED.get(spec.asset_class, ())
        if self.kind.dataset not in offered:
            raise MalformedRequestError(
                f"massive: {spec.asset_class} has no {self.kind.dataset} at this vendor; it "
                f"offers {', '.join(offered)}",
                remedy="drop the dataset from the spec, or name the class that carries it",
            )
        if self.kind.aggregated and spec.resolution is None:
            raise MalformedRequestError(
                f"resolution: {self.kind.dataset} are aggregated and the spec names no bar size",
                remedy="add `resolution: 1d` (or another <n>(s|m|h|d|w)) to the spec",
            )
        if not self.kind.aggregated and spec.resolution is not None:
            raise MalformedRequestError(
                f"resolution: {self.kind.dataset} are not aggregated, and the spec names the "
                f"bar size {spec.resolution!r}",
                remedy="drop `resolution`, or load bars instead",
            )

    def _open(self) -> MassiveClient:
        """The rate-limited client this loader reads through, built once and kept."""
        if self._opened is not None:
            return self._opened
        if self.client is None and self.workspace is None:
            raise PreconditionError(
                f"{self.id}: neither a workspace nor a client; a vendor loader is opened by "
                "the adapter registry, which resolves the credential and the quota from the "
                "workspace it is opened for",
                remedy="run the loader through `kanso data load`, which opens it",
            )
        # Imported here rather than at module scope so the package above may hold a table
        # of its own loaders without this module and that one importing each other.
        from kanso.data.adapters.massive import ADAPTER

        self._opened = self.client or ADAPTER.client(cast("Workspace", self.workspace))
        return self._opened

    def _request(self, spec: MassiveSpec, symbol: str, ticker: str, on: date) -> Request:
        return Request(
            symbol=symbol,
            venue=spec.venue,
            ticker=ticker,
            asset_class=spec.asset_class,
            dataset=self.kind.dataset,
            resolution=spec.resolution,
            adjusted=spec.adjusted,
            publication=spec.publication,
            publication_rule=spec.publication_rule,
            price_precision=spec.price_precision,
            size_precision=spec.size_precision,
            adjustment_basis=spec.adjustment_basis or (on.isoformat() if spec.adjusted else None),
            probed_on=on,
        )

    def _ref(self, spec: MassiveSpec, request: Request, span: tuple[date, date]) -> DatasetRef:
        instrument = request.instrument
        identity = dataset_id(instrument, self.kind.type, spec.resolution, spec.adjusted, span[1])
        return DatasetRef(
            dataset_id=identity,
            instrument=instrument,
            type=self.kind.type,
            resolution=spec.resolution,
            span=span,
            adjusted=spec.adjusted,
            publication=spec.publication,
            publication_rule=spec.publication_rule,
            vendor=self.id,
            vendor_dataset=self.kind.dataset,
            request_params=request.params(),
        )

    def _points(self, request: Request, span: tuple[date, date]) -> Iterator[Any]:
        """Every row the source serves for the widened window, kept by its own reference day."""
        client = self._open()
        endpoint = self.kind.endpoint(request)
        asked = vendor_window(span, request.resolution)
        if request.floor is not None:
            asked = (max(asked[0], request.floor), asked[1])
        path, params = endpoint.request(request.ticker, asked)
        for page in client.pages(path, params):
            if page.signal is not Signal.ROWS and page.signal is not Signal.NO_ROWS:
                self._explain(client, request, endpoint, asked)
            for row in page.rows:
                point = built(self.kind, request, row)
                if point is not None and span[0] <= utc_day(point.ts_event) <= span[1]:
                    yield point

    def _explain(
        self,
        client: MassiveClient,
        request: Request,
        endpoint: Endpoint,
        window: tuple[date, date],
    ) -> None:
        """Say what a mid-walk refusal was, by probing rather than by reading its message."""
        raise_if_blocked(
            probe(
                client,
                request.ticker,
                request.asset_class,
                dataset=endpoint,
                window=window,
                as_of=self.as_of,
            )
        )
        raise TransportError(
            f"massive: {self.kind.dataset} for {request.ticker} was refused over "
            f"{window[0]}..{window[1]}, and a probe of that same window came back served; "
            "the source contradicted itself, so nothing was established",
            remedy="re-run the command; if it repeats, check the vendor's status page",
        )


# --- bars -----------------------------------------------------------------------


def bars_endpoint(request: Request) -> Endpoint:
    """The aggregates endpoint for one dataset's resolution and adjustment basis.

    The multiplier decides whether a limit is sent at all, which is the whole reason the
    two are read together: a limit above a multiplier of one caps the base aggregates the
    vendor rolls up, and the rolled-up answer comes back empty.
    """
    step, span = aggregate(str(request.resolution))
    params = [("adjusted", "true" if request.adjusted else "false"), ("sort", "asc")]
    if step == 1:
        params.append(("limit", str(AGGREGATE_LIMIT)))
    return Endpoint(
        dataset="bars",
        template=f"/v2/aggs/ticker/{{ticker}}/range/{step}/{span}/{{start}}/{{end}}",
        params=tuple(params),
        timestamp_field="t",
        timestamp_unit="ms",
    )


def build_bar(request: Request, row: Mapping[str, Any]) -> Any:
    """One aggregate row as a bar timestamped at the close of its own window.

    The row's `t` is the millisecond instant the window opened, so the resolution is added
    to it: a bar is known at its close and at no earlier instant, and `ts_init` equals that
    close because a real-time aggregate is public as soon as it is complete.
    """
    opened = instant(row, "t", "ms")
    prices = numbers(row, OHLC)
    if opened is None or prices is None:
        return None
    volume = numbers(row, ("v",)) or [0.0]
    step = parse_duration(str(request.resolution), "resolution")
    ts_event = opened + int(step.total_seconds()) * 1_000_000_000
    quoted = [ticks(price, request.price_precision) for price in prices]
    return make_bar(
        bar_type(instrument_id(request.symbol, request.venue), str(request.resolution)),
        (quoted[0], quoted[1], quoted[2], quoted[3]),
        ticks(volume[0], request.size_precision),
        request.price_precision,
        request.size_precision,
        ts_event,
        ts_event,
    )


BARS_KIND: Final = Kind(
    type="bar", dataset="bars", endpoint=bars_endpoint, build=build_bar, aggregated=True
)


class MassiveBarsLoader(RequestLoader):
    """Aggregate bars for every class this adapter serves."""

    id: ClassVar[str] = "massive_bars"
    kind: ClassVar[Kind] = BARS_KIND
