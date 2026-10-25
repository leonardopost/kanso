"""Financial statements, stamped at the instant the filing carrying them was accepted.

A statement is the one data class where the two engine timestamps are furthest apart and
where confusing them is most expensive. `ts_event` is the end of the period the figures
cover; `ts_init` is the instant the filing carrying them was accepted, weeks later and on
no fixed offset. A backtest stamped at the period end would buy on a quarter's revenue
the day the quarter closed and would look extraordinary for a reason that has nothing to
do with the strategy.

**The source serves each period once, as its latest filing stated it.** Three listings —
income statements, balance sheets, cash flow statements — answer one row per period, and
the row's `filing_date` is "the most recent SEC filing that included this period's data"
in the source's own words: a figure restated by a later filing has replaced the original,
and the original and the day it was first public are not served at all. So a period is
stamped at the acceptance of that most recent filing, which is the one instant at which
the figures served were certainly public; stamping them at the first filing instead would
hand a strategy a restatement before it was made. It is late on purpose, and measured
late: a quarter's income and cash flow figures are carried again by the next year's
quarterly report, so on 2026-10-15 the median AAPL quarter's income statement named a
filing 398 days after the quarter ended (its balance sheet, which the next year's report
does not repeat, one 34 days after); the latest quarter is stamped at its own report until
the next one repeats it, so a later load stamps it later.

**The acceptance instant comes from the source or the point is not made.** The rows carry
no instant and no accession, only the CIK and that filing day, so the instant is the
source's filings indexes joined on both (`filings.AcceptanceIndex`): the latest acceptance
among the periodic reports the issuer filed that day. A row the indexes cannot stamp is
dropped, never stamped from its filing day, its period end or the moment of the fetch.
Measured on 2026-10-15 over quarterly statements since 2009, the indexes stamp 173 of
AAPL's 200 rows, 171 of MSFT's 203, 159 of JPM's 184, 162 of NVDA's 207 and 165 of BRK.B's
198 — the rest name a report the source lists without its instant, or does not hold. It
holds none of BABA's or SONY's 20-F reports, so neither issuer's statements stamp at all.

**Dropping is per row, in every window.** `data backfill` and `data sync` ask thirty days
at a time, which is one quarter at most, so a window whose rows are all dropped is an
ordinary window, answered with nothing as a quiet one is, and the walk goes on past it. The
one refusal is for a window whose rows name only reports of the forms the acceptance index
holds no instant for (`filings.UNHELD_FORMS`): there the cause is the issuer's filings and
not one report, so no window of them would stamp either, and the refusal says so.

**Vendor-computed ratios are not read.** The statements listings serve figures, not
ratios; the source's ratios listing is not asked. A ratio has no publication instant of
its own — it is computed from figures of different vintages and prices — so it cannot be
evaluated point in time. kanso computes ratios in a strategy from the figures below and
the prices beside them, where the availability of every input is known.

**A figure is what the source served, under its own name.** `items` maps
`<statement>.<field>` — `income_statement.revenue`, `balance_sheet.total_assets` — to every
number a row carries, the fiscal year and quarter aside. The source states no unit for any
of them, so `units` is empty rather than filled in: its documentation calls each one a
number, the per-share and share-count fields say so by name, and the rest are amounts in a
currency it does not name (USD for every domestic issuer measured). It spells a concept a
filing did not report separately as `0.0` — AAPL's quarterly `interest_expense`, which its
filings fold into other income — so a zero is the source's figure and not proof of none.

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
    FILINGS,
    FILINGS_INDEX,
    UNHELD_FORMS,
    VENDOR,
    AcceptanceIndex,
    day_end_ns,
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
from kanso.data.types import register_custom_type, resolve_type
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty

__all__ = [
    "BALANCE_SHEETS",
    "CASH_FLOW_STATEMENTS",
    "DATASET",
    "ENDPOINTS",
    "FINANCIALS_PAGE_LIMIT",
    "INCOME_STATEMENTS",
    "INDEXED",
    "PUBLICATION_RULE",
    "STATEMENTS",
    "TIMEFRAMES",
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
and why a row without one is not made into a point."""

DATASET: Final = "financials"
"""What the three statements listings are, together: one product the plan includes or
does not, probed once, and the vendor dataset every financials ref records."""

Statement = Literal["income_statement", "balance_sheet", "cash_flow_statement"]
Timeframe = Literal["annual", "quarterly", "ttm"]

STATEMENTS: Final[tuple[Statement, ...]] = (
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
)
"""The statements the source serves, each from its own listing. The comprehensive-income
section the retired listing carried has no successor, so it is no longer among them."""

TIMEFRAMES: Final[dict[Timeframe, str]] = {
    "quarterly": "quarterly",
    "annual": "annual",
    "ttm": "trailing_twelve_months",
}
"""A spec's reporting grain as the source spells it."""

FISCAL_PERIODS: Final[dict[Timeframe, str]] = {"annual": "FY", "ttm": "TTM"}
"""How a point names its fiscal period where the grain is not a quarter. A quarterly
point is `Q` and the quarter the source serves; these are the spellings the retired
listing used, kept so a strategy that read them reads them still."""

UNSTAMPED: Final[dict[Timeframe, frozenset[Statement]]] = {"ttm": frozenset({"balance_sheet"})}
"""The statements a grain has none of. A balance sheet is a balance at an instant, so it
has no trailing twelve months: measured, the source answers that request with no rows."""

PERIOD_FIELD: Final = "period_end"
FILED_FIELD: Final = "filing_date"
CIK_FIELD: Final = "cik"
FISCAL_YEAR_FIELD: Final = "fiscal_year"
FISCAL_QUARTER_FIELD: Final = "fiscal_quarter"
"""The row fields this module reads by name. Named once each so that confirming the
vendor's spelling against a live answer is one edit rather than a search."""

IDENTITY: Final = frozenset({FISCAL_YEAR_FIELD, FISCAL_QUARTER_FIELD})
"""The numbers a row carries that are not figures: they say which period it is."""

INDEXED: Final = "filings_index"
"""Where every point's availability came from: the filings indexes, joined on the
issuer and the day of the filing that carried the period."""

FINANCIALS_PAGE_LIMIT: Final = "1000"
"""Rows per page of each statements listing. The source documents fifty thousand and
answered every page asked of it up to that, measured; one issuer's history at one grain is
under seventy rows, so the first page is the whole answer and the cursor is a formality."""


def _statements(path: str) -> Endpoint:
    return Endpoint(
        dataset=DATASET,
        template=path,
        params=(
            ("tickers", "{ticker}"),
            (PERIOD_FIELD + ".gte", "{start}"),
            (PERIOD_FIELD + ".lte", "{end}"),
            ("sort", PERIOD_FIELD + ".asc"),
            ("limit", FINANCIALS_PAGE_LIMIT),
        ),
    )


INCOME_STATEMENTS: Final = _statements("/stocks/financials/v1/income-statements")
BALANCE_SHEETS: Final = _statements("/stocks/financials/v1/balance-sheets")
CASH_FLOW_STATEMENTS: Final = _statements("/stocks/financials/v1/cash-flow-statements")
"""The three statements listings, asked over the period they report on — which is the date
these points' `ts_event` and therefore the dataset's span are stated in.

They replaced `/vX/reference/financials`, which the source deprecated with a sunset of
2026-10-09 and now answers with HTTP 410. Each narrows to an issuer by `tickers`, matching
a row whose ticker list holds the one asked for, and each answers every period of that
issuer once."""

ENDPOINTS: Final[dict[Statement, Endpoint]] = {
    "income_statement": INCOME_STATEMENTS,
    "balance_sheet": BALANCE_SHEETS,
    "cash_flow_statement": CASH_FLOW_STATEMENTS,
}


@customdataclass
class FinancialStatement(Data):  # type: ignore[misc]
    """One issuer's figures for one statement and reporting period, as a filing stated them.

    `ts_event` is the last instant of the period the figures cover and `ts_init` the
    instant the filing was accepted. `items` maps `<statement>.<field>` to a number and
    `units` maps the same keys to what each counts, where the source states it; `accession`
    names the filing whose acceptance stamped the point and `stamped_from` where that
    instant was read from.
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
    """A financials spec: which issuers, which statements, which grain, over which period ends.

    `statements` names the listings asked; left out, it is every statement the grain has —
    all three quarterly or annually, and the income and cash flow statements for a trailing
    twelve months, since a balance sheet has none.
    """

    loader: Literal["massive_financials"] = "massive_financials"
    instruments: list[NonEmpty] = Field(min_length=1)
    venue: NonEmpty
    start: date
    end: date
    timeframe: Timeframe = "quarterly"
    statements: list[Statement] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> "FinancialsSpec":
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instruments: repeats a ticker")
        if self.statements is not None:
            if len(set(self.statements)) != len(self.statements):
                raise ValueError("statements: repeats a statement")
            none = sorted(set(self.statements) & UNSTAMPED.get(self.timeframe, frozenset()))
            if none:
                raise ValueError(
                    f"statements: {', '.join(none)} has no {self.timeframe} figures — a "
                    "balance sheet is a balance at an instant, not a sum over a year; ask it "
                    "quarterly or annually"
                )
        return self

    @property
    def span(self) -> tuple[date, date]:
        return (self.start, self.end)

    @property
    def asked(self) -> tuple[Statement, ...]:
        """The statements this spec asks for, in the source's order."""
        if self.statements is not None:
            return tuple(name for name in STATEMENTS if name in self.statements)
        absent = UNSTAMPED.get(self.timeframe, frozenset())
        return tuple(name for name in STATEMENTS if name not in absent)


def statement_of(
    row: Mapping[str, Any],
    statement: Statement,
    spec: FinancialsSpec,
    instrument: InstrumentId,
    stamp: tuple[int, str],
) -> FinancialStatement:
    """One row of one statement as a point, stamped at `stamp`, or a refusal naming why not.

    `stamp` is the acceptance instant and the accession of the filing the row names. What
    is refused here is a row that contradicts itself — a filing accepted before the period
    it reports on ended, or a statement with no figures — which no join could repair.
    """
    period = parse_day(row.get(PERIOD_FIELD), f"{statement}.{PERIOD_FIELD}")
    ts_event = day_end_ns(period)
    accepted, accession = stamp
    if accepted <= ts_event:
        raise ValidationError(
            f"massive: a {statement} for the period ending {period} was accepted at or before "
            "the end of that period, which is not a publication instant",
            remedy="the source's filings indexes disagree with its statements; report it upstream",
        )
    items = _figures(row, statement)
    if not items:
        raise ValidationError(
            f"massive: the {statement} for the period ending {period} carries no figures",
            remedy="the vendor changed this listing's shape; the adapter must be updated to it",
        )
    quarter = row.get(FISCAL_QUARTER_FIELD)
    return FinancialStatement(
        ts_event=ts_event,
        ts_init=accepted,
        instrument_id=instrument,
        timeframe=spec.timeframe,
        fiscal_period=FISCAL_PERIODS.get(spec.timeframe, f"Q{quarter}" if quarter else ""),
        fiscal_year=str(row.get(FISCAL_YEAR_FIELD, "")),
        accession=accession,
        stamped_from=INDEXED,
        items=items,
        units={},
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
        """One dataset per issuer, once the plan is shown to include what stamping needs.

        That is the statements and both filings indexes: a statement with no instant is
        not a point, so a plan without either index could serve rows and stamp none.
        """
        parsed = FinancialsSpec.model_validate(dict(spec))
        entitlements = Entitlements(self.client, as_of=self.as_of)
        found: list[DatasetRef] = []
        for ticker in parsed.instruments:
            for name in parsed.asked:
                require(entitlements, ENDPOINTS[name], ticker)
            require(entitlements, FILINGS_INDEX, ticker)
            require(entitlements, FILINGS, ticker)
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
                    vendor_dataset=DATASET,
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
        """What the listings actually served, and could be stamped, over the whole span."""
        return manifest_for(ref, self.id, self.load(ref, ref.span))

    def _points(
        self, spec: FinancialsSpec, ref: DatasetRef, window: tuple[date, date]
    ) -> Iterator[object]:
        ticker = ticker_of(ref, spec.instruments)
        instrument = instrument_id(ticker, spec.venue)
        served: list[tuple[Statement, Mapping[str, Any]]] = [
            (name, row)
            for name in spec.asked
            for row in listing(
                self.client,
                ENDPOINTS[name],
                ticker,
                window,
                extra={"timeframe": TIMEFRAMES[spec.timeframe]},
                as_of=self.as_of,
            )
        ]
        indexes = _indexes(self.client, ticker, served, self.as_of)
        found: list[FinancialStatement] = []
        dropped: list[frozenset[str]] = []
        for name, row in served:
            index = indexes.get(str(row.get(CIK_FIELD, "")))
            filed = row.get(FILED_FIELD)
            if index is None or filed is None:
                dropped.append(frozenset())
                continue
            day = parse_day(filed, f"{name}.{FILED_FIELD}")
            stamp = index.of(day)
            if stamp is None:
                dropped.append(index.forms(day))
                continue
            statement = statement_of(row, name, spec, instrument, stamp)
            if window[0] <= utc_day(statement.ts_event) <= window[1]:
                found.append(statement)
        if dropped and len(dropped) == len(served) and all(_unheld(forms) for forms in dropped):
            forms = ", ".join(sorted(frozenset[str]().union(*dropped)))
            raise ValidationError(
                f"massive: {ticker}'s statements over {window[0]}..{window[1]} name only "
                f"{forms} reports, and the source's acceptance index holds the instant of "
                "none of them",
                remedy=(
                    f"drop {ticker} from the spec: the acceptance index holds no 20-F's "
                    "instant, so an issuer filing its annual report on that form has no "
                    "statement this source can stamp; if it has since moved to 10-K and "
                    "10-Q reports, a range after its last 20-F loads"
                ),
            )
        yield from sorted(found, key=lambda point: (point.ts_init, point.ts_event))


def _indexes(
    client: MassiveClient,
    ticker: str,
    served: list[tuple[Statement, Mapping[str, Any]]],
    as_of: date | None,
) -> dict[str, AcceptanceIndex]:
    """One acceptance index per issuer key the rows name, over the filing days they name.

    One issuer has one CIK, measured; a ticker the source has carried for two issuers would
    name two, and each row is then stamped by its own issuer's filings.
    """
    days: dict[str, set[date]] = {}
    for name, row in served:
        cik, filed = row.get(CIK_FIELD), row.get(FILED_FIELD)
        if cik and filed is not None:
            days.setdefault(str(cik), set()).add(parse_day(filed, f"{name}.{FILED_FIELD}"))
    return {
        cik: AcceptanceIndex(client, ticker, cik, filed, as_of=as_of) for cik, filed in days.items()
    }


def _unheld(forms: frozenset[str]) -> bool:
    """Whether a dropped row named a day holding only reports of forms never held."""
    return bool(forms) and forms <= UNHELD_FORMS


def _figures(row: Mapping[str, Any], statement: Statement) -> dict[str, float]:
    """Every number a row carries, under its statement, the fiscal year and quarter aside."""
    return {
        f"{statement}.{field}": float(value)
        for field, value in row.items()
        if field not in IDENTITY and not isinstance(value, bool) and isinstance(value, int | float)
    }


def _spec_of(ref: DatasetRef) -> FinancialsSpec:
    """The spec a ref carries, so `load` needs nothing but the ref it was given."""
    params = ref.request_params
    if not params:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} carries no financials spec; refs come from "
            "FinancialsLoader.discover and are not built by hand"
        )
    return FinancialsSpec.model_validate(decoded(params, ("instruments", "statements")))


register_custom_type(TYPE_ID, FinancialStatement)
