"""Splits and dividends as `CorporateAction` points, stamped when they became knowable.

kanso loads unadjusted prices and carries the actions beside them, so an adjustment is a
function of the two rather than something a vendor baked into a price series. This module
is where that second half comes from: two vendor listings, one dataset, one point per
event.

**Splits and dividends are one dataset.** A dataset is one instrument, one type and one
resolution, and both listings produce `corporate_action` points for the same instrument.
They are therefore fetched together, merged and written once; two datasets would be two
files claiming the same identity, and the store holds one series per instrument and type.

**When a corporate action became knowable is what decides its stamps, and the source
decides which of two answers is available.** A `CorporateAction` records the announcement:
`ts_event` and `ts_init` are both the instant the fact existed and was knowable, and the
effective date travels as `ex_date_ns`, a field a strategy can read the day the action is
announced. So the dataset has exactly two honest shapes and the spec's `kinds` chooses
between them:

* **announced** — dividends alone. The source serves a declaration date for a dividend,
  which is the day the issuer announced it, so every point is stamped at the end of that
  day and the dataset is `realtime`: the reference time and the availability coincide,
  because an announcement is public at the moment it is made. A dividend row that carries
  no declaration date is refused rather than back-dated.
* **effective** — anything including splits. The source serves no announcement date for a
  split, only the day it takes effect, and the `corporate_action` publication rule is
  explicit that the announcement instant must come from the source rather than be derived
  from an effective date. So every point in this shape is stamped at the end of the day it
  took effect — the conservative bound, since an action is certainly public by then, and
  late can only cost an opportunity while early leaks the future — and the dataset says so
  by declaring `publication: unknown`. It may be loaded and used for adjustment; a
  hypothesis that requires corporate actions is refused a snapshot pinned to it, which is
  the correct consequence of an availability nobody observed.

Stamping at the *end* of a day rather than its start is the same conservatism one level
down: a fact the source dates but does not time happened at an unknown hour and is public
by the day's end.

**A window is a window of stamps.** The listing is asked over the same date the stamps
come from — the declaration date in the announced shape, the effective date in the other
— so the span a dataset claims, the range the spec asked for and the instants the engine
delivers on are all one calendar. Asking by one date and stamping by another would drop
every action whose two dates fall on opposite sides of a chunk boundary.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`CorporateAction` is kanso's own registered custom type, a `@customdataclass` over the
engine's `Data`; its constructor takes the two timestamps first and its fields by keyword.
Its `ratio` is shares held after the event per share held before and its `cash` is cash
per share held before, which is what makes a split and a dividend one type.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, Final, Literal

from pydantic import Field, model_validator

from kanso.data.adapters.massive.client import MassiveClient
from kanso.data.adapters.massive.entitlement import Endpoint, Entitlements
from kanso.data.adapters.massive.filings import (
    PAGE_LIMIT,
    VENDOR,
    day_end_ns,
    day_start_ns,
    decoded,
    encoded,
    listing,
    parse_day,
    require,
    ticker_of,
)
from kanso.data.loader import DatasetRef, arrow_batches, checked, manifest_for, utc_day
from kanso.data.loaders.points import instrument_id
from kanso.data.manifest import Manifest, dataset_id
from kanso.data.types import CorporateAction, resolve_type
from kanso.data.types.corporate_action import TYPE_ID
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty

__all__ = [
    "ANNOUNCED",
    "DEFAULT_KINDS",
    "PUBLICATION_RULE",
    "DIVIDENDS_ANNOUNCED",
    "DIVIDENDS_EFFECTIVE",
    "EFFECTIVE",
    "SPLITS",
    "CorporateActionsLoader",
    "CorporateActionsSpec",
    "action_of",
    "endpoints_for",
]

Kind = Literal["split", "dividend"]

DEFAULT_KINDS: Final[tuple[Kind, ...]] = ("dividend", "split")
"""What a spec asks for when it names no kinds: everything the two listings hold, which
is the shape stamped at each action's effective date."""

PUBLICATION_RULE: Final = "corporate_action"
"""The data class whose publication rule these points are stamped under. It shares its
name with the type they are, which is a coincidence of both being named after the thing
they describe rather than one being derived from the other."""

ANNOUNCED: Final = "announced"
EFFECTIVE: Final = "effective"
"""The two shapes a corporate-action dataset can honestly take: stamped at the day the
issuer announced the action, or at the day it took effect."""

EXECUTION_FIELD: Final = "execution_date"
SPLIT_FROM_FIELD: Final = "split_from"
SPLIT_TO_FIELD: Final = "split_to"
EX_FIELD: Final = "ex_dividend_date"
DECLARED_FIELD: Final = "declaration_date"
CASH_FIELD: Final = "cash_amount"
CURRENCY_FIELD: Final = "currency"
"""The row fields each listing carries. Named once each so that confirming the vendor's
spelling against a live answer is one edit rather than a search."""

SPLITS: Final = Endpoint(
    dataset="splits",
    template="/v3/reference/splits",
    params=(
        ("ticker", "{ticker}"),
        (f"{EXECUTION_FIELD}.gte", "{start}"),
        (f"{EXECUTION_FIELD}.lte", "{end}"),
        ("order", "asc"),
        ("sort", EXECUTION_FIELD),
        ("limit", PAGE_LIMIT),
    ),
)
"""Splits, over the day each took effect — the only date this listing serves."""

DIVIDENDS_EFFECTIVE: Final = Endpoint(
    dataset="dividends",
    template="/v3/reference/dividends",
    params=(
        ("ticker", "{ticker}"),
        (f"{EX_FIELD}.gte", "{start}"),
        (f"{EX_FIELD}.lte", "{end}"),
        ("order", "asc"),
        ("sort", EX_FIELD),
        ("limit", PAGE_LIMIT),
    ),
)

DIVIDENDS_ANNOUNCED: Final = Endpoint(
    dataset="dividends",
    template="/v3/reference/dividends",
    params=(
        ("ticker", "{ticker}"),
        (f"{DECLARED_FIELD}.gte", "{start}"),
        (f"{DECLARED_FIELD}.lte", "{end}"),
        ("order", "asc"),
        ("sort", DECLARED_FIELD),
        ("limit", PAGE_LIMIT),
    ),
)
"""The same listing over two different dates: a dataset is asked by the date it stamps
by, so a chunk boundary can never fall between an action's two dates and lose it."""


class CorporateActionsSpec(KansoModel):
    """A corporate-actions spec: which issuers, which kinds, over which days.

    `kinds` decides more than a filter. Dividends alone can be stamped at the day they
    were announced, which is what the data class means by availability; a split cannot,
    because the source serves no announcement date for one. So a spec naming only
    dividends produces a `realtime` dataset and any other spec produces an `unknown` one,
    and the difference is stated rather than smoothed over.
    """

    loader: Literal["massive_corporate_actions"] = "massive_corporate_actions"
    instruments: list[NonEmpty] = Field(min_length=1)
    venue: NonEmpty
    start: date
    end: date
    kinds: list[Kind] = Field(default_factory=lambda: list(DEFAULT_KINDS))
    currency: NonEmpty = "USD"
    """The currency a cash amount is stated in when the row does not name its own."""

    @model_validator(mode="after")
    def _validate(self) -> CorporateActionsSpec:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instruments: repeats a ticker")
        if not self.kinds:
            raise ValueError("kinds: name at least one of split, dividend")
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("kinds: repeats a kind")
        return self

    @property
    def basis(self) -> str:
        """Which of the two shapes this spec asks for."""
        return ANNOUNCED if set(self.kinds) == {"dividend"} else EFFECTIVE

    @property
    def publication(self) -> str:
        """How the points became public: at once for an announcement, unknown for a bound."""
        return "realtime" if self.basis == ANNOUNCED else "unknown"

    @property
    def span(self) -> tuple[date, date]:
        return (self.start, self.end)


def endpoints_for(spec: CorporateActionsSpec) -> tuple[Endpoint, ...]:
    """The listings this spec reads, in a stable order."""
    found: list[Endpoint] = []
    if "split" in spec.kinds:
        found.append(SPLITS)
    if "dividend" in spec.kinds:
        found.append(DIVIDENDS_ANNOUNCED if spec.basis == ANNOUNCED else DIVIDENDS_EFFECTIVE)
    return tuple(found)


def action_of(
    row: Mapping[str, Any], endpoint: Endpoint, spec: CorporateActionsSpec, instrument: object
) -> CorporateAction:
    """One listing row as a corporate action, stamped by the spec's own basis."""
    if endpoint.dataset == SPLITS.dataset:
        return _split(row, spec, instrument)
    return _dividend(row, spec, instrument)


@dataclass(frozen=True)
class CorporateActionsLoader:
    """The corporate-actions loader: two listings, one dataset per issuer.

    Holds a client rather than a workspace, because a loader is stateless with respect to
    the workspace and the credential is resolved where the client is built. `as_of` fixes
    the day every probe runs on, so one long run sees one consistent set of answers.
    """

    id: ClassVar[str] = "massive_corporate_actions"

    client: MassiveClient
    as_of: date | None = None

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per issuer, after establishing that the plan includes each listing.

        Entitlement is probed market-wide and once per listing, so a universe of any size
        costs one request per listing rather than one per issuer, and an answer of "no
        rows for this issuer" later on is a fact about the issuer.
        """
        parsed = CorporateActionsSpec.model_validate(dict(spec))
        entitlements = Entitlements(self.client, as_of=self.as_of)
        vendor_dataset = "+".join(endpoint.dataset for endpoint in endpoints_for(parsed))
        found: list[DatasetRef] = []
        for ticker in parsed.instruments:
            for endpoint in endpoints_for(parsed):
                require(entitlements, endpoint, ticker)
            instrument = str(instrument_id(ticker, parsed.venue))
            found.append(
                DatasetRef(
                    dataset_id=dataset_id(instrument, TYPE_ID, None, False, parsed.end),
                    instrument=instrument,
                    type=TYPE_ID,
                    resolution=None,
                    span=parsed.span,
                    adjusted=False,
                    publication=parsed.publication,
                    publication_rule=PUBLICATION_RULE,
                    vendor=VENDOR,
                    vendor_dataset=vendor_dataset,
                    request_params=encoded(parsed),
                )
            )
        return found

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The issuer's actions whose stamp falls in `window`, in availability order."""
        spec = _spec_of(ref)
        return checked(self._points(spec, ref, window), f"{self.id} dataset {ref.dataset_id}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the listings actually served over the dataset's whole span."""
        return manifest_for(ref, self.id, self.load(ref, ref.span))

    def _points(
        self, spec: CorporateActionsSpec, ref: DatasetRef, window: tuple[date, date]
    ) -> Iterator[object]:
        ticker = ticker_of(ref, spec.instruments)
        instrument = instrument_id(ticker, spec.venue)
        found: list[CorporateAction] = []
        for endpoint in endpoints_for(spec):
            for row in listing(self.client, endpoint, ticker, window, as_of=self.as_of):
                action = action_of(row, endpoint, spec, instrument)
                if window[0] <= utc_day(action.ts_event) <= window[1]:
                    found.append(action)
        yield from sorted(found, key=lambda action: (action.ts_init, action.kind))


def _split(
    row: Mapping[str, Any], spec: CorporateActionsSpec, instrument: object
) -> CorporateAction:
    """A split row, stamped at the end of the day it took effect."""
    day = parse_day(row.get(EXECUTION_FIELD), f"splits.{EXECUTION_FIELD}")
    before = _positive(row.get(SPLIT_FROM_FIELD), f"splits.{SPLIT_FROM_FIELD}")
    after = _positive(row.get(SPLIT_TO_FIELD), f"splits.{SPLIT_TO_FIELD}")
    return CorporateAction(
        ts_event=day_end_ns(day),
        ts_init=day_end_ns(day),
        instrument_id=instrument,
        kind="split",
        ratio=after / before,
        cash=0.0,
        currency=spec.currency,
        ex_date_ns=day_start_ns(day),
    )


def _dividend(
    row: Mapping[str, Any], spec: CorporateActionsSpec, instrument: object
) -> CorporateAction:
    """A dividend row, stamped at the day it was announced or the day it went ex."""
    ex_day = parse_day(row.get(EX_FIELD), f"dividends.{EX_FIELD}")
    stamp = _announced(row) if spec.basis == ANNOUNCED else ex_day
    currency = row.get(CURRENCY_FIELD)
    return CorporateAction(
        ts_event=day_end_ns(stamp),
        ts_init=day_end_ns(stamp),
        instrument_id=instrument,
        kind="dividend",
        ratio=1.0,
        cash=_cash(row.get(CASH_FIELD)),
        currency=currency if isinstance(currency, str) and currency else spec.currency,
        ex_date_ns=day_start_ns(ex_day),
    )


def _announced(row: Mapping[str, Any]) -> date:
    """The day a dividend was declared, refused rather than replaced when absent."""
    declared = row.get(DECLARED_FIELD)
    if declared is None:
        raise ValidationError(
            f"massive: a dividend with ex-date {row.get(EX_FIELD)!r} carries no "
            f"{DECLARED_FIELD}, so the day it was announced is not known and stamping it at "
            "its ex-date would claim an availability the source did not serve",
            remedy=(
                "ask for `kinds: [dividend, split]`, which stamps every action at the day it "
                "took effect and declares the dataset's publication unknown"
            ),
        )
    return parse_day(declared, f"dividends.{DECLARED_FIELD}")


def _positive(value: object, field: str) -> float:
    """One side of a split ratio, which is a positive number or nothing usable."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValidationError(
            f"massive: {field} is {value!r}, and a split ratio has two positive sides",
            remedy="the vendor changed this field's shape; the adapter must be updated to it",
        )
    return float(value)


def _cash(value: object) -> float:
    """A dividend's cash per share, which may be absent on a pure share event."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _spec_of(ref: DatasetRef) -> CorporateActionsSpec:
    """The spec a ref carries, so `load` needs nothing but the ref it was given."""
    params = ref.request_params
    if not params:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} carries no corporate-actions spec; refs come from "
            "CorporateActionsLoader.discover and are not built by hand"
        )
    return CorporateActionsSpec.model_validate(decoded(params, ("instruments", "kinds")))
