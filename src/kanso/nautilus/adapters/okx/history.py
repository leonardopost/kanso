"""The exchange's public history as catalog data: what the three loaders share.

`okx_bars` (`bars.py`), `okx_trades` (`trades.py`) and `okx_funding` (`funding.py`) are
`kanso.data.Loader`s over the public API of the host `[adapters.okx]` names, returned by the
adapter's `loaders(ws)` as factories. None sends a credential — the same User-Agent-only
client the public reference uses, on the table's quota — so, like the reference, they are
enabled by the table and not by a variable.

**A spec names the exchange's swaps and a range of UTC days.**

```yaml
loader: okx_bars
instruments: [BTC-USDT-SWAP]     # the exchange's instId, or BTC-USDT-SWAP.OKX
start: 2026-09-28
end: 2026-09-28
resolution: 1m                   # okx_bars only; the others refuse one
```

The venue is the exchange's own, so no spec states one, and an id on another venue is
refused. Days are UTC days of `ts_event`, as every kanso span is.

**Precision is the resolved definition's.** Prices and sizes are read at the precision of
the definition the catalog holds for the id — `kanso data instruments resolve` with
`[data] reference = "okx"` puts it there — so an unresolved id is refused before any
request is made, and the precisions are recorded in the dataset's request parameters,
where a later `data sync` reads them. A served number that precision cannot hold exactly is
refused rather than rounded: the exchange re-sets a contract's tick from time to time, and a
price rounded onto today's tick is a price it never printed. Sizes are in contracts, the
unit the definition's lot is stated in and the one kanso's notional (`qty x px x
multiplier`) reads.

**A range is served in full or refused by name.** `discover` never narrows what a spec
asks for. A range reaching before what an endpoint serves is refused naming the endpoint's
horizon — never loaded as an empty market — and one reaching into a UTC day that has not
ended is refused naming the last complete day. A `load` asked past what the source holds
yet — `data sync` extends a series to today — serves what the source holds and stops, and
the manifest records the span actually served.

**A throttle is waited out; nothing else is.** An answer of HTTP 429 or code `50011` is
asked again after a growing pause, up to `RETRIES` times — the archive listing draws one now
and then even on its own quota (`reference.KEYED_QUOTAS`). Any other answer that is not the
API's success stops the call, because nothing about the data was established by it.

Every dataset is `realtime`: a bar is public at its close, a print when it prints and a
funding payment when it settles, so `ts_init` equals `ts_event` and no publication rule is
involved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Final

from pydantic import Field, model_validator

from kanso.data.loader import DatasetRef, arrow_batches, checked, manifest_for
from kanso.data.manifest import Manifest, dataset_id
from kanso.data.types import resolve_type
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus.adapters.okx.reference import ADAPTER, PublicClient, Transport
from kanso.nautilus.adapters.okx.venue import VENUE, instrument_id
from kanso.schemas.base import KansoModel, NonEmpty
from kanso.schemas.duration import Duration

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.data.loader import Loader
    from kanso.workspace import Workspace

__all__ = [
    "DAY",
    "MS_PER_DAY",
    "NS_PER_MS",
    "RETRIES",
    "HistoryLoader",
    "HistorySpec",
    "Series",
    "answered",
    "day_ms",
    "days",
    "loaders",
    "runs",
    "units",
]

DAY: Final = timedelta(days=1)
MS_PER_DAY: Final = 86_400_000
NS_PER_MS: Final = 1_000_000
"""The API times every row in milliseconds since the epoch; the engine in nanoseconds."""

RETRIES: Final = 5
PAUSE_S: Final = 2.0
"""Attempts at a throttled request, and the pause before the next, times the attempt."""

THROTTLED: Final = "50011"
"""The exchange's code for a request over its rate limit, answered under HTTP 429."""

_EPOCH: Final = date(1970, 1, 1)


def day_ms(day: date) -> int:
    """UTC midnight opening `day`, in the API's milliseconds."""
    return (day - _EPOCH).days * MS_PER_DAY


def days(span: tuple[date, date]) -> list[date]:
    """Every day of `span`, both ends included."""
    return [span[0] + DAY * offset for offset in range((span[1] - span[0]).days + 1)]


def runs(dates: Sequence[date]) -> str:
    """Days as runs — `2023-01-01..2023-01-03, 2023-01-07` — so a refusal stays one line."""
    runs: list[list[date]] = []
    for day in sorted(set(dates)):
        if runs and day - runs[-1][-1] == DAY:
            runs[-1].append(day)
        else:
            runs.append([day])
    return ", ".join(str(run[0]) if len(run) == 1 else f"{run[0]}..{run[-1]}" for run in runs)


def units(text: object, precision: int) -> int | None:
    """`text` as a whole number of `10 ** -precision`, or `None` when it is not exactly one.

    Read as a decimal from the exchange's own spelling, so nothing is lost to a float, and
    never rounded: a number the precision cannot hold is the caller's to refuse by name.
    """
    try:
        value = Decimal(str(text))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    scaled = value.scaleb(precision)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def answered(
    client: PublicClient,
    path: str,
    params: Mapping[str, str],
    pause: Callable[[float], None],
) -> tuple[Any, ...]:
    """The `data` of one successful answer, waiting out a throttle and stopping on the rest."""
    for attempt in range(1, RETRIES + 1):
        answer = client.get(path, params)
        if answer.listed:
            return answer.data
        if answer.status != 429 and answer.code != THROTTLED:
            break
        if attempt < RETRIES:
            pause(PAUSE_S * attempt)
    raise KansoError(
        f"okx: {path} did not answer as the exchange's API does ({answer.said()})",
        Exit.ERROR,
        remedy=(
            "re-run the command; if it repeats, lower `rate_per_second` in [adapters.okx] or "
            "check the exchange's status page"
        ),
    )


# --- the spec and the series it discovers ---------------------------------------


class HistorySpec(KansoModel):
    """A public-history spec: the exchange's swaps, a range of UTC days, a bar size."""

    loader: str | None = None
    instruments: list[NonEmpty] = Field(min_length=1)
    start: date
    end: date
    resolution: Duration | None = None

    @model_validator(mode="after")
    def _ordered(self) -> HistorySpec:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        return self


def inst_id_of(name: str) -> str:
    """The exchange's `instId` a spec's name means: bare, or with the venue appended."""
    inst, dot, venue = name.rpartition(".")
    if not dot:
        return name
    if venue != VENUE:
        raise ValidationError(
            f"instruments: {name!r} names the venue {venue}, and these loaders read the "
            f"exchange's own swaps, on {VENUE}",
            remedy=f"name the swap as the exchange does, BTC-USDT-SWAP or BTC-USDT-SWAP.{VENUE}",
        )
    return inst


@dataclass(frozen=True, slots=True)
class Series:
    """One dataset's request, as a ref carries it and a later `load` rebuilds it.

    Strings only and no credential — the manifest records them verbatim. `host` is where
    the series was first read from, for the record; a load reads from the host the
    workspace names, since every regional host serves the same public history.
    """

    inst_id: str
    price_precision: int
    size_precision: int
    host: str
    resolution: str | None = None

    @property
    def instrument(self) -> str:
        """The kanso instrument id: the `instId` with the venue appended."""
        return instrument_id(self.inst_id)

    def params(self) -> dict[str, str]:
        return {
            "inst_id": self.inst_id,
            "price_precision": str(self.price_precision),
            "size_precision": str(self.size_precision),
            "host": self.host,
        }

    @classmethod
    def of(cls, ref: DatasetRef, loader_id: str) -> Series:
        """The series a ref describes, refusing one that was not discovered by a loader here."""
        params = ref.request_params or {}
        wanted = ("inst_id", "price_precision", "size_precision")
        if any(name not in params for name in wanted):
            raise ValidationError(
                f"dataset {ref.dataset_id!r} carries no {loader_id} request; refs come from "
                "the loader's own discover and are not built by hand",
                remedy="run the loader through `kanso data load`, which discovers them",
            )
        return cls(
            inst_id=params["inst_id"],
            price_precision=int(params["price_precision"]),
            size_precision=int(params["size_precision"]),
            host=params.get("host", ""),
            resolution=ref.resolution,
        )


# --- the loader -----------------------------------------------------------------


@dataclass
class HistoryLoader:
    """One public-history loader, opened for the workspace whose table names the host.

    Stateless as the interface requires: the only thing kept between calls is the client,
    whose quota must outlive a single request. `transport`, `as_of` and `pause` are how the
    suite serves recorded bodies on a fixed day without waiting; nothing else passes them.
    """

    id: ClassVar[str]
    type: ClassVar[str]
    vendor_dataset: ClassVar[str]
    aggregated: ClassVar[bool] = False

    workspace: Workspace
    transport: Transport | None = None
    as_of: date | None = None
    pause: Callable[[float], None] = time.sleep
    cache: Path | None = None
    _client: PublicClient | None = field(default=None, init=False, repr=False, compare=False)

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per swap, over exactly the range asked for, or a refusal naming why."""
        parsed = HistorySpec.model_validate(dict(spec))
        self._shape(parsed)
        latest = self.latest()
        if parsed.end > latest:
            raise ValidationError(
                f"end: {parsed.end} is not a UTC day that has ended; the last complete day is "
                f"{latest}",
                remedy=f"set `end: {latest}` or earlier",
            )
        window = (parsed.start, parsed.end)
        client = self.client()
        found: list[DatasetRef] = []
        for name in dict.fromkeys(parsed.instruments):
            series = self._series(inst_id_of(name), parsed.resolution, client)
            self.measure(client, series, window)
            found.append(self._ref(series, window))
        return found

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[Any]:
        """The dataset's points over `window`, up to the last day that has ended."""
        series = Series.of(ref, self.id)
        span = ref.window(window)
        if span is None or span[0] > self.latest():
            return iter(())
        span = (span[0], min(span[1], self.latest()))
        return checked(self.points(self.client(), series, span), f"{self.id} {series.inst_id}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the source served over the dataset's span, measured from the points."""
        return manifest_for(ref, self.id, self.load(ref, ref.span))

    def latest(self) -> date:
        """The last UTC day that has ended, as of the day this loader runs on."""
        return (self.as_of or datetime.now(tz=UTC).date()) - DAY

    def client(self) -> PublicClient:
        """The public client on the table's host, built once and kept for its quota."""
        if self._client is None:
            self._client = ADAPTER.client(self.workspace, transport=self.transport)
        return self._client

    # --- what each loader supplies ------------------------------------------------

    def measure(self, client: PublicClient, series: Series, window: tuple[date, date]) -> None:
        """Refuse a window the endpoint cannot serve in full, naming what it does serve."""
        raise NotImplementedError  # pragma: no cover - every loader supplies its own

    def points(
        self, client: PublicClient, series: Series, span: tuple[date, date]
    ) -> Iterator[Any]:
        """The points of `span`, in the order the engine wants them."""
        raise NotImplementedError  # pragma: no cover - every loader supplies its own

    # --- internals ------------------------------------------------------------

    def _shape(self, spec: HistorySpec) -> None:
        if self.aggregated and spec.resolution is None:
            raise ValidationError(
                f"resolution: {self.id} loads bars and the spec names no bar size",
                remedy="add `resolution: 1m` (or another size docs/adapters.md lists)",
            )
        if not self.aggregated and spec.resolution is not None:
            raise ValidationError(
                f"resolution: {self.id} loads {self.type} points, which have no bar size, and "
                f"the spec names {spec.resolution!r}",
                remedy="drop `resolution`, or load bars with okx_bars",
            )

    def _series(self, inst_id: str, resolution: str | None, client: PublicClient) -> Series:
        """The series of one swap, at the precision of the definition the catalog holds."""
        from kanso.data.instruments import current_definitions

        instrument = instrument_id(inst_id)
        held = current_definitions(self.workspace).get(instrument)
        if held is None:
            raise PreconditionError(
                f"{instrument}: the catalog holds no definition of it, and its prices and "
                "sizes are read at the definition's precision",
                remedy=f"run `kanso data instruments resolve {instrument}` with "
                '`[data] reference = "okx"`, then load again',
            )
        definition: Any = held
        return Series(
            inst_id=inst_id,
            price_precision=int(definition.price_precision),
            size_precision=int(definition.size_precision),
            host=client.base_url,
            resolution=resolution,
        )

    def _ref(self, series: Series, span: tuple[date, date]) -> DatasetRef:
        return DatasetRef(
            dataset_id=dataset_id(series.instrument, self.type, series.resolution, False, span[1]),
            instrument=series.instrument,
            type=self.type,
            resolution=series.resolution,
            span=span,
            adjusted=False,
            publication="realtime",
            vendor=self.id,
            vendor_dataset=self.vendor_dataset,
            request_params=series.params(),
        )


def loaders(ws: Workspace) -> dict[str, Callable[[], Loader]]:
    """Every public-history loader for `ws`, as a factory per id; nothing is built here."""
    from kanso.nautilus.adapters.okx.bars import OkxBarsLoader
    from kanso.nautilus.adapters.okx.funding import OkxFundingLoader
    from kanso.nautilus.adapters.okx.trades import OkxTradesLoader

    return {
        OkxBarsLoader.id: lambda: OkxBarsLoader(workspace=ws),
        OkxTradesLoader.id: lambda: OkxTradesLoader(workspace=ws),
        OkxFundingLoader.id: lambda: OkxFundingLoader(workspace=ws),
    }
