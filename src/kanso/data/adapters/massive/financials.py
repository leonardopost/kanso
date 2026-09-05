"""Financial statements, stamped at the instant the filing was accepted.

A statement is the one data class where the two engine timestamps are furthest apart and
where confusing them is most expensive. `ts_event` is the end of the period the figures
cover; `ts_init` is the instant the filing carrying them was accepted, weeks later and on
no fixed offset. A backtest stamped at the period end would buy on a quarter's revenue
the day the quarter closed and would look extraordinary for a reason that has nothing to
do with the strategy.

**The acceptance instant comes from the source or the point is refused.** kanso's
publication rule for this data class derives no lag and says so: the source must supply
the filing instant. So there are exactly two admissible origins, and both are the
source's own — the row's acceptance field, and the filings index joined on the accession
the row names, asked for the row's own filing day. A row with neither is refused. It is
not stamped from the period end, not from the filing date, and not from the moment of the
fetch: each of those would be an availability kanso invented, and the whole point of this
module is that it does not.

**Vendor-computed ratios are not read.** Only the statement sections are taken from a
row; anything else it carries is ignored on purpose. A served ratio has no derivable
publication instant of its own — it is computed from figures of different vintages and is
typically served only as a current snapshot — so it cannot be evaluated point in time.
kanso computes ratios in a strategy from the figures below and the prices beside them,
where the availability of every input is known.

**A figure keeps its unit.** `items` holds the numbers and `units` holds what each is
counted in, under the same key, because revenue in dollars over shares outstanding is a
ratio and revenue in dollars over shares in thousands is a wrong answer. Keys are
`<statement>.<tag>`, the source's own tags under the section they came from.

**Restatements are further points, never overwrites.** A revised statement arrives as
another point for the same period with a later acceptance instant, so "the figure known
at time t" is the latest point at or before t — which is exactly how the engine delivers
them, since it orders by `ts_init`.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
A custom data type is a `nautilus_trader.core.data.Data` subclass decorated with
`@customdataclass` (`nautilus_trader.model.custom`), which synthesises the constructor,
the dict, bytes and Arrow conversions and the schema, and registers the class for msgspec
and Arrow — which is what lets the catalog persist it. Field annotations are restricted to
`InstrumentId`, `str`, `bool`, `float`, `int`, `bytes`, `ndarray` and `dict`; a `dict`
field is stored as its JSON text and comes back as a mapping.

**This module must not use `from __future__ import annotations`.** The decorator reads
`cls.__annotations__` verbatim and resolves nothing, so postponed annotations reach it as
strings and it raises `TypeError: Unsupported custom data annotation: 'str'`.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, Final, Literal

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId
from pydantic import Field, model_validator

from kanso.data.adapters.massive.client import MassiveClient
from kanso.data.adapters.massive.entitlement import Endpoint, Entitlements
from kanso.data.adapters.massive.filings import (
    ACCEPTANCE_FIELD,
    VENDOR,
    AcceptanceIndex,
    accession_of,
    day_end_ns,
    decoded,
    encoded,
    instant_ns,
    listing,
    parse_day,
    require,
    ticker_of,
)
from kanso.data.loader import DatasetRef, arrow_batches, checked, manifest_for, utc_day
from kanso.data.loaders.points import instrument_id
from kanso.data.manifest import Manifest, dataset_id
from kanso.data.types import register_custom_type, resolve_type
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty

__all__ = [
    "ACCEPTANCE",
    "FINANCIALS",
    "FINANCIALS_PAGE_LIMIT",
    "INDEXED",
    "PUBLICATION_RULE",
    "STATEMENTS",
    "TYPE_ID",
    "FinancialStatement",
    "FinancialsLoader",
    "FinancialsSpec",
    "statement_of",
]

TYPE_ID: Final = "financial_statement"
"""The id this type is registered under and named by in `data_requirements`."""

PUBLICATION_RULE: Final = "fundamental"
"""The data class whose publication rule these points are stamped under. It derives no
lag and requires the source to supply the filing instant, which is what this module does
and why a row without one is refused."""

STATEMENTS: Final = (
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
    "comprehensive_income",
)
"""The sections read out of a row. Anything else the source carries beside them is left
where it is: a derived quantity has no publication instant of its own."""

FIGURES_FIELD: Final = "financials"
PERIOD_FIELD: Final = "period_of_report_date"
FILED_FIELD: Final = "filing_date"
TIMEFRAME_FIELD: Final = "timeframe"
FISCAL_PERIOD_FIELD: Final = "fiscal_period"
FISCAL_YEAR_FIELD: Final = "fiscal_year"
SOURCE_FILING_FIELD: Final = "source_filing_url"
VALUE_KEY: Final = "value"
UNIT_KEY: Final = "unit"
"""The row fields this listing carries. Named once each so that confirming the vendor's
spelling against a live answer is one edit rather than a search."""

ACCEPTANCE: Final = "acceptance"
INDEXED: Final = "filings_index"
"""Where a point's availability came from: the row's own acceptance instant, or the
filings index joined on the accession. Both are the source's; there is no third."""

FINANCIALS_PAGE_LIMIT: Final = "100"
"""Rows per page of the financials listing, and this one endpoint's own ceiling.

Measured, not assumed: the listing serves a hundred rows a page and rejects a request for
a thousand outright, with a client error rather than an empty page — so a limit borrowed
from a neighbouring listing does not fetch fewer statements, it fetches none. The number
therefore lives here, beside the endpoint that has it, rather than in a shared page size
the next endpoint added would inherit without measuring its own.

A smaller page is not a smaller answer: the cursor carries the rest, and a walk over it
returns every row a wider page would have. What the ceiling costs is requests, not data.
"""

FINANCIALS: Final = Endpoint(
    dataset="financials",
    template="/vX/reference/financials",
    params=(
        ("ticker", "{ticker}"),
        (PERIOD_FIELD + ".gte", "{start}"),
        (PERIOD_FIELD + ".lte", "{end}"),
        ("order", "asc"),
        ("sort", PERIOD_FIELD),
        ("limit", FINANCIALS_PAGE_LIMIT),
    ),
)
"""Periodic statements, asked over the period they report on — which is the date these
points' `ts_event` and therefore the dataset's span are stated in.

Ordering and sorting are the source's own and are served as asked; only the page size is
this listing's to cap, which `FINANCIALS_PAGE_LIMIT` records. The version prefix is this
listing's own too — the source versions each listing on its own schedule, so a neighbour
served under a different one is no evidence about this path either way."""


@customdataclass
class FinancialStatement(Data):  # type: ignore[misc]
    """One issuer's figures for one reporting period, as one filing stated them.

    `ts_event` is the last instant of the period the figures cover and `ts_init` the
    instant the filing was accepted. `items` maps `<statement>.<tag>` to a number and
    `units` maps the same keys to what each number counts; `accession` names the document
    the figures came from and `stamped_from` says which of the two admissible origins the
    availability instant was read from.
    """

    instrument_id: InstrumentId
    timeframe: str
    fiscal_period: str
    fiscal_year: str
    accession: str
    stamped_from: str
    items: dict[str, float]
    units: dict[str, str]


class FinancialsSpec(KansoModel):
    """A financials spec: which issuers, which reporting grain, over which period ends."""

    loader: Literal["massive_financials"] = "massive_financials"
    instruments: list[NonEmpty] = Field(min_length=1)
    venue: NonEmpty
    start: date
    end: date
    timeframe: Literal["annual", "quarterly", "ttm"] = "quarterly"

    @model_validator(mode="after")
    def _validate(self) -> "FinancialsSpec":
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instruments: repeats a ticker")
        return self

    @property
    def span(self) -> tuple[date, date]:
        return (self.start, self.end)


def statement_of(
    row: Mapping[str, Any],
    spec: FinancialsSpec,
    instrument: InstrumentId,
    index: AcceptanceIndex,
) -> FinancialStatement:
    """One row as a statement, or a refusal naming what stopped it becoming one.

    Every refusal here is a refusal to invent an availability. A row is a statement when
    the source says which period it covers, when the filing carrying it was accepted, and
    what the figures are; a row missing any of the three is not one.
    """
    period = parse_day(row.get(PERIOD_FIELD), f"financials.{PERIOD_FIELD}")
    ts_event = day_end_ns(period)
    accession = accession_of(row, SOURCE_FILING_FIELD)
    accepted, stamped_from = _accepted(row, accession, index)
    if accepted <= ts_event:
        raise ValidationError(
            f"massive: a statement for the period ending {period} was accepted at or before "
            "the end of that period, which is not a publication instant",
            remedy="the vendor's acceptance field is wrong for this row; report it upstream",
        )
    items, units = _figures(row)
    if not items:
        raise ValidationError(
            f"massive: the statement for the period ending {period} carries no figures under "
            f"{', '.join(STATEMENTS)}",
            remedy="narrow the range to periods the source holds statements for",
        )
    return FinancialStatement(
        ts_event=ts_event,
        ts_init=accepted,
        instrument_id=instrument,
        timeframe=str(row.get(TIMEFRAME_FIELD, spec.timeframe)),
        fiscal_period=str(row.get(FISCAL_PERIOD_FIELD, "")),
        fiscal_year=str(row.get(FISCAL_YEAR_FIELD, "")),
        accession=accession,
        stamped_from=stamped_from,
        items=items,
        units=units,
    )


@dataclass(frozen=True)
class FinancialsLoader:
    """The financials loader: one dataset per issuer, stamped from the filing.

    Holds a client rather than a workspace, because a loader is stateless with respect to
    the workspace and the credential is resolved where the client is built. `as_of` fixes
    the day every probe runs on, so one long run sees one consistent set of answers.
    """

    id: ClassVar[str] = "massive_financials"

    client: MassiveClient
    as_of: date | None = None

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per issuer, after establishing that the plan includes the listing."""
        parsed = FinancialsSpec.model_validate(dict(spec))
        entitlements = Entitlements(self.client, as_of=self.as_of)
        found: list[DatasetRef] = []
        for ticker in parsed.instruments:
            require(entitlements, FINANCIALS, ticker)
            instrument = str(instrument_id(ticker, parsed.venue))
            found.append(
                DatasetRef(
                    dataset_id=dataset_id(instrument, TYPE_ID, None, False, parsed.end),
                    instrument=instrument,
                    type=TYPE_ID,
                    resolution=None,
                    span=parsed.span,
                    adjusted=False,
                    publication="delayed",
                    publication_rule=PUBLICATION_RULE,
                    vendor=VENDOR,
                    vendor_dataset=FINANCIALS.dataset,
                    request_params=encoded(parsed),
                )
            )
        return found

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The issuer's statements whose period ends in `window`, in availability order."""
        spec = _spec_of(ref)
        return checked(self._points(spec, ref, window), f"{self.id} dataset {ref.dataset_id}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the listing actually served over the dataset's whole span."""
        return manifest_for(ref, self.id, self.load(ref, ref.span))

    def _points(
        self, spec: FinancialsSpec, ref: DatasetRef, window: tuple[date, date]
    ) -> Iterator[object]:
        ticker = ticker_of(ref, spec.instruments)
        instrument = instrument_id(ticker, spec.venue)
        index = AcceptanceIndex(self.client, ticker, as_of=self.as_of)
        found: list[FinancialStatement] = []
        for row in listing(
            self.client,
            FINANCIALS,
            ticker,
            window,
            extra={TIMEFRAME_FIELD: spec.timeframe},
            as_of=self.as_of,
        ):
            statement = statement_of(row, spec, instrument, index)
            if window[0] <= utc_day(statement.ts_event) <= window[1]:
                found.append(statement)
        yield from sorted(found, key=lambda point: (point.ts_init, point.ts_event))


def _accepted(row: Mapping[str, Any], accession: str, index: AcceptanceIndex) -> tuple[int, str]:
    """The instant this filing was accepted, and which of the two origins it came from."""
    served = row.get(ACCEPTANCE_FIELD)
    if served is not None:
        return instant_ns(served, f"financials.{ACCEPTANCE_FIELD}"), ACCEPTANCE
    filed = row.get(FILED_FIELD)
    if filed is None:
        raise ValidationError(
            f"massive: a statement for the period ending {row.get(PERIOD_FIELD)!r} carries "
            f"neither {ACCEPTANCE_FIELD} nor {FILED_FIELD}, so nothing says when it became "
            "public and there is no day to ask the filings index about",
            remedy=(
                "narrow the range to periods the source stamps; a statement dated from its "
                "period end would hand a strategy figures weeks before they existed"
            ),
        )
    joined = index.of(accession, parse_day(filed, f"financials.{FILED_FIELD}"))
    if joined is None:
        raise ValidationError(
            f"massive: a statement for the period ending {row.get(PERIOD_FIELD)!r} carries no "
            f"{ACCEPTANCE_FIELD}, and the filings index holds no acceptance instant for "
            f"accession {accession or '(none named)'} on {filed}",
            remedy=(
                "narrow the range to periods the source stamps; a statement dated from its "
                "filing day would claim an availability the source did not serve"
            ),
        )
    return joined, INDEXED


def _figures(row: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    """The numbers and their units, section by named section and nothing else."""
    figures = row.get(FIGURES_FIELD)
    items: dict[str, float] = {}
    units: dict[str, str] = {}
    if not isinstance(figures, Mapping):
        return items, units
    for statement in STATEMENTS:
        section = figures.get(statement)
        if not isinstance(section, Mapping):
            continue
        for tag, figure in section.items():
            if not isinstance(figure, Mapping):
                continue
            value = figure.get(VALUE_KEY)
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            key = f"{statement}.{tag}"
            items[key] = float(value)
            unit = figure.get(UNIT_KEY)
            if isinstance(unit, str) and unit:
                units[key] = unit
    return items, units


def _spec_of(ref: DatasetRef) -> FinancialsSpec:
    """The spec a ref carries, so `load` needs nothing but the ref it was given."""
    params = ref.request_params
    if not params:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} carries no financials spec; refs come from "
            "FinancialsLoader.discover and are not built by hand"
        )
    return FinancialsSpec.model_validate(decoded(params, ("instruments",)))


register_custom_type(TYPE_ID, FinancialStatement)
