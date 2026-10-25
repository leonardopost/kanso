"""The filings indexes, and the reference-document vocabulary the fundamentals share.

A **filing** is the document a financial statement arrives in, and its *acceptance
instant* is the moment the regulator's system took it — which is the moment it became
public. That instant is the only timestamp a fundamental point may carry as `ts_init`, so
this module finds it, and it takes two of the source's indexes to do so. A statement names
its issuer's CIK and the day of the filing that carried it and nothing else about that
filing; the *filings index* lists an issuer's periodic reports by the day they were filed,
and the *acceptance index* holds each report's instant under its accession. Neither holds
both, measured on 2026-10-15: the first serves no instant, and the second ignores every
issuer filter it is asked with.

The indexes register no data type and write no dataset. A filing adds no series over the
statement it carries: what it adds is the instant, and an instant is a stamp rather than a
point.

The module also holds what the fundamental datasets share, because they are read the same
way and the rules below must be stated once rather than for each.

**A listing is probed without the ticker.** A reference listing filtered to one issuer
answers with no rows for two quite different reasons — the plan excludes the dataset, and
the issuer did nothing in that window — and confusing those is the mistake this adapter
exists not to make. `offered` drops the ticker filter, so the probe asks whether *anybody*
split, declared a dividend or filed in the window. Somebody always did, so rows prove the
plan includes the listing, and a later empty answer for one ticker is then a fact about
the issuer rather than about the plan.

**No history floor is measured for these datasets.** The floor probe halves a bracket
whose invariant is that an earlier start serves nothing while a later one serves rows.
That holds for a continuous price series and fails for a sparse event series: an issuer
that filed in 1996 and in 2015 and nothing between would bisect to an arbitrary year and
the answer would be reported as the source's floor. So a range is asked for in full and
coverage is measured from what came back, which is the rule every loader follows anyway.

**A refusal mid-fetch is explained, never assumed.** A listing that answered a probe
minutes ago and refuses now is asked about again, at the grain the source gates on, and
the probe's outcome is what gets raised. Nothing here reads the vendor's sentence, which
is byte-identical for four different conditions.

**An instant is read in the zone it was measured in, or it is refused.** A timestamp with
no offset is ambiguous by hours, and reading a wall clock as UTC moves a publication
*earlier* by the offset — the direction that hands a strategy information it did not have.
The acceptance index serves its instant as a bare `YYYYMMDDHHMMSS` clock, and that clock was
measured to be the regulator's own Eastern time: over four filing days either side of a
daylight-saving change (2025-11-03, 2026-02-13, 2026-05-01, 2026-07-31; 626 filings) every
value fell between 06:00:59 and 21:50:45, which is the window the regulator's system
accepts filings in, Eastern, in winter and summer alike; read as UTC the window would have
moved by an hour between the seasons. So that clock is read as `America/New_York` and
nothing else is read without a zone. A date is widened to the end of its day for the same
reason: the later bound is the one that cannot leak.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
Nothing here builds an engine object; `kanso.data.loader.to_ns` is used for the one
conversion, so an instant is an exact integer count of nanoseconds from the epoch rather
than a float of seconds, which is what the engine's `ts_event` and `ts_init` are.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NoReturn
from zoneinfo import ZoneInfo

from kanso.data.adapters.massive.client import MassiveClient, Signal
from kanso.data.adapters.massive.entitlement import (
    Endpoint,
    Entitlements,
    Probe,
    probe,
    raise_if_blocked,
)
from kanso.data.adapters.massive.errors import (
    GONE,
    MalformedRequestError,
    NotEntitledError,
    retired,
)
from kanso.data.loader import to_ns
from kanso.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.data.loader import DatasetRef
    from kanso.schemas.base import KansoModel

__all__ = [
    "ACCEPTANCE_FIELD",
    "ASSET_CLASS",
    "EDGAR_ZONE",
    "FILINGS",
    "FILINGS_INDEX",
    "PAGE_LIMIT",
    "PERIODIC_FORMS",
    "UNHELD_FORMS",
    "VENDOR",
    "AcceptanceIndex",
    "accession_of",
    "day_end_ns",
    "day_start_ns",
    "decoded",
    "edgar_instant_ns",
    "encoded",
    "explain",
    "listing",
    "offered",
    "parse_day",
    "require",
    "ticker_of",
]

VENDOR: Final = "massive"
"""The vendor a manifest records these datasets against.

Spelled here rather than imported from the adapter package's own declaration: importing
it would make these modules depend on the package body being fully executed, and the day
the package exports them that dependency becomes a cycle."""

ASSET_CLASS: Final = "stocks"
"""The one class these datasets exist for. The adapter's capabilities declare corporate
actions, financials and filings for equities alone, so the class is a constant here and
not a spec field an operator could set to something the source has no listing for."""

PAGE_LIMIT: Final = "1000"
"""Rows per page of the splits, dividends and filings listings.

This is a page size, not a cap on an aggregation. The caution that forbids `limit` on a
rolled-up aggregate — where the source applies it to the base series *before* the roll-up
and answers with an empty series that means nothing — does not reach a cursor-paged
listing, where the parameter decides only how many rows arrive per request while the
cursor carries the rest.

**A ceiling is a property of one endpoint, never of the adapter.** Each listing has its
own, measured against the source and spelled beside the endpoint it belongs to: this is
the number the splits, dividends and acceptance listings answer to. An endpoint added
later measures its ceiling rather than inheriting a number that happened to work
elsewhere."""

ACCEPTANCE_FIELD: Final = "acceptance_datetime"
"""The acceptance index's field carrying the instant the regulator accepted the document,
as the Eastern `YYYYMMDDHHMMSS` clock `edgar_instant_ns` reads."""

ACCESSION_FIELD: Final = "accession_number"
SOURCE_FIELD: Final = "source_url"
FORM_FIELD: Final = "form_type"
CIK_PARAM: Final = "cik"
FILED_FIELD: Final = "filing_date"
"""The field names a filing row carries. Named here, once each, so that confirming the
vendor's spelling against a live answer is one edit rather than a search."""

EDGAR_ZONE: Final = "America/New_York"
"""The zone the acceptance index's clock is stated in: the regulator's own, measured."""

EDGAR_CLOCK: Final = "%Y%m%d%H%M%S"
"""How the acceptance index spells an instant: fourteen digits and no zone."""

PERIODIC_FORMS: Final = (
    "10-K",
    "10-K/A",
    "10-KT",
    "10-KT/A",
    "10-Q",
    "10-Q/A",
    "10-QT",
    "10-QT/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
)
"""The forms a periodic financial statement arrives in: the annual and quarterly reports,
their transition-period and amended forms, and the two annual reports of an issuer
outside the United States. A day's other filings — an 8-K's press release, an insider's
Form 4 — are not where a statement comes from, so they never stamp one."""

UNHELD_FORMS: Final = frozenset({"20-F", "20-F/A"})
"""The periodic forms the acceptance index was measured holding no instant for: the annual
report of an issuer outside the United States, and its amendment. On 2026-10-15 the filings
index listed BABA's 20-Fs on eleven days and SONY's on three, and the acceptance index held
none of them — BABA's of 2026-05-20 answers 404 under its own accession. An issuer whose
statements name only these has none this source can stamp. A 40-F, the annual report of a
Canadian issuer, was not measured, so it is not among them: a statement naming one is
stamped where the index holds its instant and dropped where it does not, as a domestic
report's is."""

NOT_HELD: Final = (404, "NOT_FOUND")
"""How the acceptance index answers an accession it does not hold: HTTP 404 under its own
`NOT_FOUND` status, measured for MSFT's 10-Q of 2025-01-29 and BABA's 20-F of 2026-05-20.
It is the one client error that is an answer about the filing — any other, a bare 404 for a
path the source does not serve among them, is about the request."""

INDEX_PAGE_LIMIT: Final = "1000"
"""Rows per page of the filings index. It serves up to ten thousand; an issuer's periodic
reports over sixteen years are seventy-odd rows, so the first page is the whole answer."""

FILINGS: Final = Endpoint(
    dataset="filings",
    template="/v1/reference/sec/filings",
    params=(("order", "desc"), ("sort", "filing_date"), ("limit", PAGE_LIMIT)),
)
"""The acceptance index: the one listing that holds a filing's acceptance instant.

It is asked for one document at a time, under its accession — `{template}/{accession}` —
and only probed as a listing, market-wide and with no window, because as a listing it
narrows by nothing a statement could name. Measured on 2026-10-15: it ignores `ticker` and
`cik` alike (asked for AAPL's filings it serves Worthington Steel's and Crane's), it
compares `filing_date` as a `YYYYMMDD` string so an ISO date matches nothing, it files a
report accepted after the evening cut-off under the day it was accepted rather than the day
the regulator dated it, and it does not hold every instant: it lists many reports with no
acceptance field at all (AAPL's 10-K of 2012-10-31 and 10-Q of 2017-08-02 among them), and
some not at all (MSFT's 10-Q of 2025-01-29 and NVDA's of 2024-11-20 answer 404 under their
own accessions). The source serves no other copy of the instant: its file listing for a
report holds the documents but not the submission header the regulator stamps it in.

**The version prefix is measured, not shared.** This listing is served under `/v1` and
answers a plain HTTP 404 — no vendor envelope, no entitlement sentence — under any other,
which is neither a refusal nor an empty window and must not be read as one."""

FILINGS_INDEX: Final = Endpoint(
    dataset="filings_index",
    template="/stocks/filings/vX/index",
    params=(
        ("form_type.any_of", ",".join(PERIODIC_FORMS)),
        ("filing_date.gte", "{start}"),
        ("filing_date.lte", "{end}"),
        ("sort", "filing_date.asc"),
        ("limit", INDEX_PAGE_LIMIT),
    ),
)
"""The filings index: an issuer's periodic reports, by the day the regulator dated them.

Asked by CIK — the issuer key a statement names, which outlives a change of ticker, and
which is passed beside the endpoint rather than in it, so the probe and a refusal's account
still name the instrument's own ticker — and by form, both of which it honours, measured:
MSFT's periodic reports since 2009 are 72 rows, every one under MSFT's own CIK. It serves
no acceptance instant, which is why the acceptance index is asked after it. A report
accepted after the evening cut-off is listed under both the day it was accepted and the day
it was dated (MSFT's 10-K of 2020-07-31), so the day a statement names finds it either way."""

_ACCESSION: Final = re.compile(r"(\d{10})-?(\d{2})-?(\d{6})")
"""An accession number, dashed or not. The regulator's own form is `0000320193-20-000096`
and the archive path spells the same number without dashes, so both are recognised and
the dashed form is what this module returns."""


# --- the vendor's spelling of a date, an instant and an accession ---------------------


def parse_day(value: object, field: str) -> date:
    """A vendor date as a calendar day, or a refusal naming the field it came from."""
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"massive: {field} is {text!r}, which is not an ISO-8601 date",
            remedy="the vendor changed this field's shape; the adapter must be updated to it",
        ) from None


def day_start_ns(day: date) -> int:
    """The first instant of a UTC day, in nanoseconds."""
    return to_ns(datetime(day.year, day.month, day.day, tzinfo=UTC))


def day_end_ns(day: date) -> int:
    """The last instant of a UTC day, in nanoseconds.

    The bound a dated fact is stamped at. A fact the source dates but does not time
    happened at some unknown hour of that day and is certainly public by its end, so the
    day's end is the availability that cannot be too early.
    """
    return day_start_ns(day + timedelta(days=1)) - 1


def edgar_instant_ns(value: object, field: str) -> int:
    """An acceptance instant as the acceptance index serves it, in UTC nanoseconds.

    The index spells it as fourteen digits with no zone, and the zone is the regulator's
    Eastern time, measured (see the module's account). Anything else is refused rather than
    read in some zone: a clock read in the wrong one moves a publication by hours, and half
    the wrong zones move it earlier. Where the clock falls in the hour a daylight-saving
    change repeats, the later of its two instants is taken — the bound that cannot leak;
    the regulator accepts nothing at that hour, so it decides nothing in practice.
    """
    text = str(value).strip()
    try:
        if len(text) != 14 or not text.isdigit():
            raise ValueError(text)
        clock = datetime.strptime(text, EDGAR_CLOCK)
    except ValueError:
        raise ValidationError(
            f"massive: {field} is {text!r}, which is not the YYYYMMDDHHMMSS clock the "
            "acceptance index was measured serving",
            remedy="the vendor changed this field's shape; the adapter must be updated to it",
        ) from None
    return to_ns(clock.replace(tzinfo=ZoneInfo(EDGAR_ZONE), fold=1))


def accession_of(row: Mapping[str, Any], *fields: str) -> str:
    """The accession a row names, or the empty string when it names none.

    Read from the row's own accession field where there is one, and otherwise recovered
    from a document URL, whose archive path spells the same number. That is a derivation
    from a fixed form rather than a guess — eighteen digits in one layout — and a value
    that does not hold one yields nothing rather than something. `fields` names any
    further URL fields to look in, because the listings spell that field differently.
    """
    for name in (ACCESSION_FIELD, SOURCE_FIELD, *fields):
        value = row.get(name)
        if isinstance(value, str) and (found := _ACCESSION.search(value)):
            return "-".join(found.groups())
    return ""


# --- how a listing is asked for ------------------------------------------------------


def offered(endpoint: Endpoint) -> Endpoint:
    """The same listing asked of the whole market: the request whose emptiness means something.

    Dropping the issuer filter is what turns "no rows" from an ambiguity into evidence.
    Filtered to one issuer, an empty answer is either a plan that excludes the listing or
    an issuer that did nothing; unfiltered, an empty answer over a fortnight of the whole
    market is the plan, because somebody splits, declares and files every week. The filter
    is whichever parameter carries the issuer — `ticker`, `tickers` or `cik`, as each
    listing spells it — so it is found by what it carries rather than by its name.
    """
    return replace(
        endpoint, params=tuple(item for item in endpoint.params if "{ticker}" not in item[1])
    )


def require(entitlements: Entitlements, endpoint: Endpoint, ticker: str) -> Probe:
    """Establish that the plan includes this listing, or raise the outcome that says why.

    The probe is market-wide and is therefore cached across the whole universe: one
    request settles a listing for every ticker in a spec, and the answer is an outcome
    rather than a sentence.
    """
    found = entitlements.check(ticker, ASSET_CLASS, dataset=offered(endpoint))
    raise_if_blocked(found)
    return found


def explain(
    client: MassiveClient, endpoint: Endpoint, ticker: str, *, as_of: date | None = None
) -> NoReturn:
    """Turn a refusal into the outcome a probe establishes, never into a guess.

    A listing that answered a probe at discovery and refuses during a fetch has changed
    under the run, and the vendor states that change with the same sentence it uses for
    three other conditions. So the question is asked again and the probe's own outcome is
    what gets raised. A probe that comes back `OK` is a contradiction rather than an
    answer, and is reported as one instead of being smoothed into an empty dataset.
    """
    found = probe(client, ticker, ASSET_CLASS, dataset=offered(endpoint), as_of=as_of)
    raise_if_blocked(found)
    raise NotEntitledError(
        f"massive: the {endpoint.dataset} listing refused {ticker} and a fresh probe of the "
        "same listing served rows, so what the plan includes changed during the run",
        remedy="re-run the command; if it repeats, check which datasets the plan grants",
    )


def listing(
    client: MassiveClient,
    endpoint: Endpoint,
    ticker: str,
    window: tuple[date, date],
    *,
    extra: Mapping[str, str] | None = None,
    as_of: date | None = None,
) -> Iterator[Mapping[str, Any]]:
    """Every row of a reference listing, failing loudly rather than emptily.

    A cursor walk that ends because the source refused a page is a walk that served part
    of a range, and yielding what it got would record a short span as a complete one. So
    a refusal is explained by probing and a rejected shape is refused outright; only rows
    and a genuinely empty answer come back as data.

    A path the source has retired is said to be one. It answers HTTP 410 (`GONE`), which is
    a client error like a malformed request, and the remedy for a malformed request — check
    the parameters — sends an operator after a mistake they did not make: measured, the
    statements listing kanso first read was retired on 2026-10-09 and answered every request
    that way.
    """
    path, params = endpoint.request(ticker, window)
    params.update(extra or {})
    for page in client.pages(path, params):
        if page.status == GONE:
            raise retired(path, f"the {endpoint.dataset} listing")
        if page.signal is Signal.BAD_REQUEST:
            raise MalformedRequestError(
                f"massive: the {endpoint.dataset} listing rejected the request for {ticker} "
                f"over {window[0]}..{window[1]} (HTTP {page.status})",
                remedy="check the ticker and the parameters this listing is asked with",
            )
        if page.signal is Signal.REFUSED:
            explain(client, endpoint, ticker, as_of=as_of)
        yield from page.rows


# --- what a ref carries, so a load needs nothing but the ref -------------------------


def encoded(spec: KansoModel) -> dict[str, str]:
    """A spec as the string map a dataset reference carries.

    A vendor spec holds no credential — the key is a header resolved at the moment of use
    — so recording it whole is what makes the request a manifest describes reproducible.
    Lists are comma-joined and an absent value is empty, so the map round-trips through
    the model's own field types.
    """
    out: dict[str, str] = {}
    for name, value in spec.model_dump(mode="json").items():
        if isinstance(value, list):
            out[name] = ",".join(str(item) for item in value)
        else:
            out[name] = "" if value is None else str(value)
    return out


def decoded(params: Mapping[str, str], lists: Sequence[str]) -> dict[str, object]:
    """The inverse of `encoded`, leaving every coercion to the model.

    An empty value is an absent one, a list's included: `encoded` writes a list left out
    and a list of nothing alike, and only the first is one a spec can hold.
    """
    out: dict[str, object] = {}
    for name, raw in params.items():
        if raw == "":
            out[name] = None
        elif name in lists:
            out[name] = [part for part in raw.split(",") if part]
        else:
            out[name] = raw
    return out


def ticker_of(ref: DatasetRef, instruments: Sequence[str]) -> str:
    """The vendor ticker a dataset reference is for, checked against its own spec."""
    symbol = ref.instrument.rpartition(".")[0]
    if symbol not in instruments:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} names instrument {ref.instrument!r}, which its own "
            f"spec does not ask for ({', '.join(instruments)})"
        )
    return symbol


# --- the two indexes, joined ------------------------------------------------------------


class AcceptanceIndex:
    """When an issuer's periodic reports were accepted, by the day the regulator dated them.

    A statement names its issuer's CIK and the day of the filing that carried it, and
    nothing else about that filing. So its instant is found in two steps, both the
    source's: the filings index lists the issuer's periodic reports by the day they were
    dated — one walk over the days the statements name, asked by CIK — and the acceptance
    index is then asked for each report the day holds, under its accession. One request
    settles every statement a report carried, because a report's instant is kept.

    A day is stamped at the latest acceptance among its periodic reports: whichever of them
    carried the statement, that bound is not early. A day that holds no periodic report, or
    holds one the acceptance index has no instant for, has no stamp, and `of` says so with
    `None` rather than reaching for the day itself, and `forms` says which forms the day's
    reports were, which is how a statement that cannot be stamped is told apart from an
    issuer none of whose statements can be (`UNHELD_FORMS`). Measured on 2026-10-15, the
    acceptance index held an instant for 56 of the 65 days AAPL's quarterly statements
    since 2009 name and 51 of NVDA's 66, and none of a 20-F.

    Each row is checked for the CIK and the form it was asked for, though the filings index
    was measured honouring both: the acceptance index ignores every issuer filter it is
    given, and a filter some later version ignores should cost a stamp, never a wrong one.
    Only an answer about the filing is a missing stamp. A *refused* index leaves as the
    outcome a probe establishes, so "the plan excludes the filings index" never arrives as
    "no instant"; a retired lookup, a rejected one and one that was not answered leave as
    what they are, because the one answer that means "no instant" is the 404 the index
    gives a filing it does not hold (`NOT_HELD`).
    """

    def __init__(
        self,
        client: MassiveClient,
        ticker: str,
        cik: str,
        filed: Collection[date],
        *,
        as_of: date | None = None,
    ) -> None:
        self._client = client
        self._ticker = ticker
        self._cik = cik
        self._window = (min(filed), max(filed)) if filed else None
        self._as_of = as_of
        self._reports: dict[date, tuple[str, ...]] | None = None
        self._forms: dict[str, str] = {}
        self._accepted: dict[str, int | None] = {}

    def of(self, filed_on: date) -> tuple[int, str] | None:
        """The latest acceptance among the periodic reports dated `filed_on`, and its
        accession, or `None` when the day holds none or holds one with no instant."""
        reports = self._listed().get(filed_on, ())
        found = [(self._instant(accession), accession) for accession in reports]
        stamped = [(instant, accession) for instant, accession in found if instant is not None]
        if not found or len(stamped) < len(found):
            return None
        return max(stamped)

    def forms(self, filed_on: date) -> frozenset[str]:
        """The forms of the periodic reports dated `filed_on`; none where it holds none."""
        return frozenset(self._forms[accession] for accession in self._listed().get(filed_on, ()))

    def _listed(self) -> dict[date, tuple[str, ...]]:
        if self._reports is not None:
            return self._reports
        days: dict[date, list[str]] = {}
        if self._window is not None:
            for row in listing(
                self._client,
                FILINGS_INDEX,
                self._ticker,
                self._window,
                extra={CIK_PARAM: self._cik},
                as_of=self._as_of,
            ):
                accession = accession_of(row)
                form = str(row.get(FORM_FIELD, ""))
                if not accession or row.get(CIK_PARAM) != self._cik:
                    continue
                if form not in PERIODIC_FORMS:
                    continue
                held = days.setdefault(
                    parse_day(row.get(FILED_FIELD), "filings_index.filing_date"), []
                )
                if accession not in held:
                    held.append(accession)
                self._forms[accession] = form
        self._reports = {day: tuple(found) for day, found in days.items()}
        return self._reports

    def _instant(self, accession: str) -> int | None:
        if accession in self._accepted:
            return self._accepted[accession]
        path = f"{FILINGS.template}/{accession}"
        page = next(self._client.pages(path))
        if page.signal is Signal.REFUSED:
            explain(self._client, FILINGS, self._ticker, as_of=self._as_of)
        if page.signal is Signal.BAD_REQUEST and (page.status, page.vendor_status) != NOT_HELD:
            if page.status == GONE:
                raise retired(path, "a filing's acceptance instant")
            raise MalformedRequestError(
                f"massive: the acceptance index rejected the lookup of {accession} "
                f"(HTTP {page.status}), which is not how it answers a filing it does not hold",
                remedy="the vendor changed this lookup; the adapter must be updated to it",
            )
        moment = page.rows[0].get(ACCEPTANCE_FIELD) if page.rows else None
        found = None if moment is None else edgar_instant_ns(moment, f"filings.{ACCEPTANCE_FIELD}")
        self._accepted[accession] = found
        return found
