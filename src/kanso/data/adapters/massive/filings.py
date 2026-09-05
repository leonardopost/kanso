"""The filings index, and the reference-document vocabulary the fundamentals share.

A **filing** is the document a financial statement arrives in, and its *acceptance
instant* is the moment the regulator's system took it — which is the moment it became
public. That instant is the only timestamp a fundamental point may carry as `ts_init`, so
this module reads the index that holds it and joins on the accession the statement names.

The index registers no data type and writes no dataset. A filing record adds no series
over the statement it carries: what it adds is the instant, and an instant is a stamp
rather than a point. `filings` is nonetheless a product in its own right — an operator or
a command may list what an issuer filed over a window — so the record is a value with a
payload, not a private tuple.

It also holds what the three fundamental datasets share, because all three are read the
same way and the rules below must be stated once rather than three times.

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

**An instant carries its zone or it is refused.** A timestamp with no offset is ambiguous
by hours, and reading a wall clock as UTC moves a publication *earlier* by the offset —
the direction that hands a strategy information it did not have. Nothing here defaults a
zone, and a date is widened to the end of its day for the same reason: the later bound is
the one that cannot leak.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
Nothing here builds an engine object; `kanso.data.loader.to_ns` is used for the one
conversion, so an instant is an exact integer count of nanoseconds from the epoch rather
than a float of seconds, which is what the engine's `ts_event` and `ts_init` are.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NoReturn

from kanso.data.adapters.massive.client import MassiveClient, Signal
from kanso.data.adapters.massive.entitlement import (
    Endpoint,
    Entitlements,
    Probe,
    probe,
    raise_if_blocked,
)
from kanso.data.adapters.massive.errors import MalformedRequestError, NotEntitledError
from kanso.data.loader import to_ns
from kanso.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.data.loader import DatasetRef
    from kanso.schemas.base import KansoModel

__all__ = [
    "ACCEPTANCE_FIELD",
    "ASSET_CLASS",
    "FILINGS",
    "PAGE_LIMIT",
    "VENDOR",
    "AcceptanceIndex",
    "Filing",
    "accession_of",
    "day_end_ns",
    "day_start_ns",
    "decoded",
    "encoded",
    "explain",
    "filing_of",
    "filings",
    "instant_ns",
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
the number the splits, dividends and filings listings answer to, and the financials
listing rejects it outright with a client error and carries a smaller one of its own. An
endpoint added later measures its ceiling rather than inheriting a number that happened to
work elsewhere."""

TICKER_PARAM: Final = "ticker"
"""The parameter that narrows a listing to one issuer, and the one `offered` drops."""

ACCEPTANCE_FIELD: Final = "acceptance_datetime"
"""The row field carrying the instant the regulator accepted the document."""

FILED_FIELD: Final = "filing_date"
PERIOD_FIELD: Final = "period_of_report_date"
FORM_FIELD: Final = "type"
ACCESSION_FIELD: Final = "accession_number"
SOURCE_FIELD: Final = "source_url"
TICKER_FIELD: Final = "ticker"
"""The field names a filing row carries. Named here, once each, so that confirming the
vendor's spelling against a live answer is one edit rather than a search."""

FILINGS: Final = Endpoint(
    dataset="filings",
    template="/v1/reference/sec/filings",
    params=(
        (TICKER_PARAM, "{ticker}"),
        ("filing_date.gte", "{start}"),
        ("filing_date.lte", "{end}"),
        ("order", "asc"),
        ("sort", "filing_date"),
        ("limit", PAGE_LIMIT),
    ),
)
"""The filings listing: what an issuer filed over a window, and when each was accepted.

Asked over the *filing* date, because that is the date the vendor indexes a document by
and the one date a statement that lacks its own acceptance instant still carries — which
is what makes the join possible at all without guessing a window.

**The version prefix is measured, not shared.** This listing is served under `/v1` and
answers a plain HTTP 404 — no vendor envelope, no entitlement sentence — under any other,
which is neither a refusal nor an empty window and must not be read as one. The vendor
versions its listings one at a time, so the prefix beside a neighbouring endpoint says
nothing about this one."""

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


def instant_ns(value: object, field: str) -> int:
    """A vendor instant as UTC nanoseconds, refused unless it names its own zone."""
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"massive: {field} is {text!r}, which is not an ISO-8601 instant",
            remedy="the vendor changed this field's shape; the adapter must be updated to it",
        ) from None
    if moment.tzinfo is None:
        raise ValidationError(
            f"massive: {field} is {text!r}, which names no time zone; reading a wall clock as "
            "UTC moves a publication earlier by the offset, and earlier is the direction that "
            "hands a strategy information it did not have",
            remedy="the source must serve this instant with an offset for it to be usable",
        )
    return to_ns(moment)


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

    Dropping the ticker filter is what turns "no rows" from an ambiguity into evidence.
    Filtered to one issuer, an empty answer is either a plan that excludes the listing or
    an issuer that did nothing; unfiltered, an empty answer over a fortnight of the whole
    market is the plan, because somebody splits, declares and files every week.
    """
    return replace(
        endpoint, params=tuple(item for item in endpoint.params if item[0] != TICKER_PARAM)
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
    """
    path, params = endpoint.request(ticker, window)
    params.update(extra or {})
    for page in client.pages(path, params):
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
    """The inverse of `encoded`, leaving every coercion to the model."""
    out: dict[str, object] = {}
    for name, raw in params.items():
        if name in lists:
            out[name] = [part for part in raw.split(",") if part]
        else:
            out[name] = None if raw == "" else raw
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


# --- the index itself -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Filing:
    """One document an issuer filed, and the instant it became public."""

    accession: str
    form: str
    ticker: str
    filed_on: date
    accepted_ns: int
    source_url: str
    period_end: date | None = None

    def payload(self) -> dict[str, object]:
        """The filing as a plain record, for `--json` and for an operator to read."""
        return {
            "accession": self.accession,
            "form": self.form,
            "ticker": self.ticker,
            "filed_on": self.filed_on.isoformat(),
            "accepted_ns": self.accepted_ns,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "source_url": self.source_url,
        }


def filing_of(row: Mapping[str, Any]) -> Filing:
    """One filing row as a record, or a refusal naming what the row lacks.

    A row with no acceptance instant is refused rather than dated from its filing day:
    the filing day is a day and the acceptance is an instant, and inventing the second
    from the first is exactly the substitution this whole adapter refuses to make.
    """
    filed = parse_day(row.get(FILED_FIELD), f"filings.{FILED_FIELD}")
    accepted = instant_ns(row.get(ACCEPTANCE_FIELD), f"filings.{ACCEPTANCE_FIELD}")
    period = row.get(PERIOD_FIELD)
    return Filing(
        accession=accession_of(row),
        form=str(row.get(FORM_FIELD, "")),
        ticker=str(row.get(TICKER_FIELD, "")),
        filed_on=filed,
        accepted_ns=accepted,
        source_url=str(row.get(SOURCE_FIELD, "")),
        period_end=parse_day(period, f"filings.{PERIOD_FIELD}") if period is not None else None,
    )


def filings(
    client: MassiveClient,
    ticker: str,
    window: tuple[date, date],
    *,
    endpoint: Endpoint = FILINGS,
    as_of: date | None = None,
) -> Iterator[Filing]:
    """Every filing an issuer made over a window of filing days, in the order served."""
    for row in listing(client, endpoint, ticker, window, as_of=as_of):
        yield filing_of(row)


class AcceptanceIndex:
    """Acceptance instants by accession, fetched one filing day at a time.

    A statement that carries no acceptance instant of its own still carries the day it
    was filed, so the index is asked for exactly that day and joined on the accession.
    That is why nothing here guesses how long after a period end a filing lands: the day
    is read off the row rather than assumed, and one request settles every statement
    filed on it.

    A row the index cannot read — no accession, no readable instant — is skipped rather
    than refused, because this is a lookup: the caller that finds no answer raises the
    failure, and it can name the accession while this cannot. A *refused* listing is the
    other thing entirely and is not skipped: it leaves the index as the outcome a probe
    established, so "the plan excludes the filings index" never arrives as "no match".
    """

    def __init__(
        self,
        client: MassiveClient,
        ticker: str,
        *,
        endpoint: Endpoint = FILINGS,
        as_of: date | None = None,
    ) -> None:
        self._client = client
        self._ticker = ticker
        self._endpoint = endpoint
        self._as_of = as_of
        self._days: dict[date, dict[str, int]] = {}

    def of(self, accession: str, filed_on: date) -> int | None:
        """The instant that accession was accepted, or `None` when the day holds no such row."""
        if not accession:
            return None
        return self._day(filed_on).get(accession)

    def days(self) -> tuple[date, ...]:
        """The filing days this index has fetched, in the order it fetched them."""
        return tuple(self._days)

    def _day(self, filed_on: date) -> dict[str, int]:
        held = self._days.get(filed_on)
        if held is not None:
            return held
        found: dict[str, int] = {}
        for row in listing(
            self._client, self._endpoint, self._ticker, (filed_on, filed_on), as_of=self._as_of
        ):
            accession = accession_of(row)
            moment = row.get(ACCEPTANCE_FIELD)
            if accession and moment is not None:
                found[accession] = instant_ns(moment, f"filings.{ACCEPTANCE_FIELD}")
        self._days[filed_on] = found
        return found
