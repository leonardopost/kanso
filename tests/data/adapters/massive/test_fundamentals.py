"""Corporate actions, financial statements and the filings index that stamps them.

Three properties are what these tests exist for, and every case here is one of them.

**An empty listing is not a refusal.** A reference listing filtered to one issuer answers
with no rows both when the plan excludes it and when the issuer did nothing, so the
fixture vendor holds rows for other issuers too: a market-wide probe sees them, a
per-issuer fetch does not, and the four outcomes come apart under the identical sentence.

**An availability is read, never invented.** A statement is stamped at the instant its
filing was accepted or it is refused; a corporate action is stamped at the day it was
announced where the source serves one and at the day it took effect where it does not,
and the dataset says which by its publication class. No test here accepts a point stamped
from a period end, a fetch time, or a date the source did not serve.

**Coverage is what was served.** The vendor holds fewer days than the spec asks for, and
the manifest records the days it held.

**The fixture answers where the source answers and no wider.** It routes on the whole
path, version prefix included, and rejects a page wider than a listing serves — because
both of those were once wrong in this adapter and neither was visible to a fixture that
matched on the tail of a URL and ignored `limit`. A frozen vendor that answers anything
asked of it proves only that the code is self-consistent.

Nothing here opens a socket, reads a credential or carries a recorded secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from kanso.data.adapters.massive.client import MassiveClient, Response
from kanso.data.adapters.massive.corporate_actions import (
    ANNOUNCED,
    DEFAULT_KINDS,
    DIVIDENDS_ANNOUNCED,
    DIVIDENDS_EFFECTIVE,
    EFFECTIVE,
    SPLITS,
    CorporateActionsLoader,
    CorporateActionsSpec,
    endpoints_for,
)
from kanso.data.adapters.massive.entitlement import Endpoint
from kanso.data.adapters.massive.errors import (
    EmptyResultError,
    MalformedRequestError,
    NotEntitledError,
)
from kanso.data.adapters.massive.filings import (
    ACCEPTANCE_FIELD,
    FILINGS,
    PAGE_LIMIT,
    AcceptanceIndex,
    Filing,
    accession_of,
    day_end_ns,
    day_start_ns,
    encoded,
    filing_of,
    filings,
    instant_ns,
    listing,
    offered,
    parse_day,
    ticker_of,
)
from kanso.data.adapters.massive.financials import (
    ACCEPTANCE,
    FINANCIALS,
    FINANCIALS_PAGE_LIMIT,
    INDEXED,
    STATEMENTS,
    FinancialsLoader,
    FinancialsSpec,
    FinancialStatement,
)
from kanso.data.catalog import write
from kanso.data.loader import DatasetRef, utc_day
from kanso.data.loaders.points import instrument_id
from kanso.data.manifest import shortfall
from kanso.data.publication import check_delayed
from kanso.data.types import CorporateAction, resolve_type
from kanso.errors import Exit, ValidationError

from . import Replay, body, definition, missing, nothing, over_limit, served

TODAY = date(2026, 9, 4)
"""The day every probe here runs on. Frozen, because a probe's recent window is measured
from today and a test that drifted with the calendar would prove nothing."""

TICKER = "AAPL"
OTHER = "MSFT"
"""The issuer under test, and the one whose rows make a market-wide answer non-empty —
which is the whole difference between "the plan excludes this" and "this issuer did
nothing that fortnight"."""

VENUE = "XNAS"
INSTRUMENT = f"{TICKER}.{VENUE}"

SPAN = (date(2020, 1, 1), date(2024, 12, 31))

LISTINGS = frozenset({"splits", "dividends", "financials", "filings"})

CURSOR = "https://api.massive.com/next/{name}"
"""Where a paged fixture's cursor points. The client follows an absolute URL verbatim, so
the fixture routes it by its last segment."""

ACCESSION = "0000320193-24-000081"
ARCHIVE = (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000081/"
    "0000320193-24-000081-index.htm"
)


PATHS = {
    "/v3/reference/splits": "splits",
    "/v3/reference/dividends": "dividends",
    "/vX/reference/financials": "financials",
    "/v1/reference/sec/filings": "filings",
}
"""Where each listing lives, version prefix and all.

Corrected against the live source: the filings index is served under `/v1` and answers a
plain 404 under anything else. The fixture used to route on the tail of a URL, which made
every prefix equally acceptable and let a wrong one pass the whole suite — so it now
matches the path whole, and a path the source does not serve is a 404 here as there.
"""

CONTROL = "/v3/reference/tickers/"
"""The control endpoint, which is asked per ticker and so matched by prefix."""

CEILING = {"financials": 100}
"""The most rows a listing serves in one page, measured.

The financials listing rejects a wider page outright rather than trimming it, and the
three reference listings serve a thousand. A fixture that ignored `limit` — as this one
did — cannot tell a page size that works from one that fails on the first live request.
"""


def listing_of(url: str) -> str | None:
    """Which listing a URL asks for, or `None` where the source serves nothing at all."""
    path = "/" + url.split("//", 1)[-1].partition("/")[2].split("?", 1)[0]
    if path.startswith("/next/"):
        return path.rsplit("/", 1)[1]
    if path.startswith(CONTROL):
        return "reference"
    return PATHS.get(path)


@dataclass
class Vendor:
    """A plan the fixture serves under, in the terms the source gates on.

    `rows` holds every issuer's rows for each listing; a request naming a ticker is
    filtered to it and a market-wide probe is not, which is exactly the asymmetry the
    probe protocol rests on. `granted` is what the plan includes, `known` is whether the
    reference endpoint recognises the key, and `sentence` is the vendor's prose — a knob
    only so a test can change it and prove no outcome moved.

    Two answers are refusals of the request rather than of the plan, and both are the
    source's own measured behaviour: a path it does not serve is a 404 with no envelope,
    and a `limit` above a listing's ceiling is a client error rather than a short page.
    `paged` and `page_size` say how many rows a page holds while the cursor carries the
    rest, so a walk can be proved at the page size a listing actually serves.
    """

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    granted: frozenset[str] = LISTINGS
    known: bool = True
    paged: bool = False
    page_size: int = 1
    sentence: str = "Warning [NOT_ENTITLED]: This data isn't included in your current plan."

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        name = listing_of(url)
        if name is None:
            return missing()
        if name == "reference":
            return served([definition(TICKER)]) if self.known else nothing()
        if name not in self.granted:
            return body(
                {"status": "NOT_AUTHORIZED", "request_id": "frozen", "message": self.sentence},
                403,
            )
        ceiling = CEILING.get(name)
        if ceiling is not None and int(params.get("limit", ceiling)) > ceiling:
            return over_limit(ceiling)
        found = self.rows.get(name, [])
        if "ticker" in params:
            found = [row for row in found if row.get("ticker") == params["ticker"]]
        if not self.paged:
            return served(found)
        if "/next/" in url:
            return served(found[self.page_size :])
        return served(found[: self.page_size], next_url=CURSOR.format(name=name))


def transport(vendor: Vendor) -> tuple[MassiveClient, Replay]:
    """A client over one frozen vendor, and the record of what it was asked."""
    replay = Replay(vendor)
    return MassiveClient("test-key-not-a-secret", transport=replay), replay


def actions_loader(vendor: Vendor) -> tuple[CorporateActionsLoader, Replay]:
    client, replay = transport(vendor)
    return CorporateActionsLoader(client=client, as_of=TODAY), replay


def financials_loader(vendor: Vendor) -> tuple[FinancialsLoader, Replay]:
    client, replay = transport(vendor)
    return FinancialsLoader(client=client, as_of=TODAY), replay


# --- rows the vendor holds ------------------------------------------------------------


def split_row(day: date, *, ticker: str = TICKER, frm: Any = 1, to: Any = 4) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "execution_date": day.isoformat(),
        "split_from": frm,
        "split_to": to,
        "id": f"S{day}",
    }


def dividend_row(
    ex: date,
    *,
    ticker: str = TICKER,
    declared: date | None = None,
    cash: Any = 0.24,
    currency: str | None = "USD",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": ticker,
        "ex_dividend_date": ex.isoformat(),
        "pay_date": (ex + timedelta(days=10)).isoformat(),
        "cash_amount": cash,
    }
    if declared is not None:
        row["declaration_date"] = declared.isoformat()
    if currency is not None:
        row["currency"] = currency
    return row


def figures(revenue: float = 94_930_000_000.0) -> dict[str, Any]:
    """One row's statement sections, plus a section this loader must not read."""
    return {
        "income_statement": {
            "revenues": {"value": revenue, "unit": "USD", "label": "Revenues", "order": 100},
            "basic_earnings_per_share": {"value": 1.4, "unit": "USD / shares", "order": 200},
        },
        "balance_sheet": {"assets": {"value": 331_612_000_000.0, "unit": "USD"}},
        "ratios": {"price_to_earnings": {"value": 31.2, "unit": "ratio"}},
    }


def financials_row(
    period: date,
    *,
    ticker: str = TICKER,
    accepted: str | None = "2024-08-02T20:41:20Z",
    filed: date | None = None,
    sections: Mapping[str, Any] | None = None,
    accession: str | None = ACCESSION,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": ticker,
        "period_of_report_date": period.isoformat(),
        "timeframe": "quarterly",
        "fiscal_period": "Q3",
        "fiscal_year": "2024",
        "financials": figures() if sections is None else sections,
    }
    if accepted is not None:
        row[ACCEPTANCE_FIELD] = accepted
    if filed is not None:
        row["filing_date"] = filed.isoformat()
    if accession is not None:
        row["source_filing_url"] = ARCHIVE
    return row


def filing_row(
    filed: date,
    *,
    ticker: str = TICKER,
    accession: str = ACCESSION,
    accepted: str | None = "2024-08-02T20:41:20Z",
    period: date | None = None,
    source: str = ARCHIVE,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": ticker,
        "filing_date": filed.isoformat(),
        "accession_number": accession,
        "type": "10-Q",
        "source_url": source,
    }
    if accepted is not None:
        row[ACCEPTANCE_FIELD] = accepted
    if period is not None:
        row["period_of_report_date"] = period.isoformat()
    return row


ELSEWHERE: dict[str, dict[str, Any]] = {
    "splits": split_row(date(2019, 1, 2), ticker=OTHER),
    "dividends": dividend_row(date(2019, 1, 3), ticker=OTHER, declared=date(2019, 1, 2)),
    "financials": financials_row(date(2019, 1, 1), ticker=OTHER),
    "filings": filing_row(date(2019, 1, 4), ticker=OTHER, accession="0000000001-19-000001"),
}
"""One row per listing from an issuer nobody is asking about.

They are what a market-wide probe sees and a per-issuer fetch does not, which is the whole
difference between "the plan excludes this listing" and "this issuer did nothing"."""


def held(**rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The vendor's rows, with every listing non-empty somewhere in the market."""
    return {name: [ELSEWHERE[name], *rows.get(name, [])] for name in sorted(LISTINGS)}


def actions_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "loader": "massive_corporate_actions",
        "instruments": [TICKER],
        "venue": VENUE,
        "start": SPAN[0].isoformat(),
        "end": SPAN[1].isoformat(),
    }
    spec.update(overrides)
    return spec


def financials_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "loader": "massive_financials",
        "instruments": [TICKER],
        "venue": VENUE,
        "start": SPAN[0].isoformat(),
        "end": SPAN[1].isoformat(),
    }
    spec.update(overrides)
    return spec


# --- the vendor's spelling of a date, an instant and an accession ---------------------


def test_a_date_is_read_or_named() -> None:
    assert parse_day("2024-08-02", "splits.execution_date") == date(2024, 8, 2)
    with pytest.raises(ValidationError) as failure:
        parse_day("08/02/2024", "splits.execution_date")
    assert "splits.execution_date" in failure.value.message
    assert failure.value.code is Exit.VALIDATION


def test_a_day_is_bounded_at_both_ends() -> None:
    day = date(2024, 8, 2)
    assert day_end_ns(day) == day_start_ns(day + timedelta(days=1)) - 1
    assert utc_day(day_start_ns(day)) == day
    assert utc_day(day_end_ns(day)) == day


def test_an_instant_carries_its_zone_or_is_refused() -> None:
    zulu = instant_ns("2024-08-02T20:41:20Z", "financials.acceptance_datetime")
    offset = instant_ns("2024-08-02T16:41:20-04:00", "financials.acceptance_datetime")
    assert zulu == offset
    with pytest.raises(ValidationError) as naive:
        instant_ns("2024-08-02T16:41:20", "financials.acceptance_datetime")
    assert "no time zone" in naive.value.message
    assert "earlier" in naive.value.message
    with pytest.raises(ValidationError) as nonsense:
        instant_ns("20240802164120", "financials.acceptance_datetime")
    assert "ISO-8601" in nonsense.value.message


def test_an_accession_is_read_or_recovered_but_never_guessed() -> None:
    assert accession_of({"accession_number": ACCESSION}) == ACCESSION
    assert accession_of({"source_url": ARCHIVE}) == ACCESSION
    assert accession_of({"source_filing_url": ARCHIVE}, "source_filing_url") == ACCESSION
    assert accession_of({"accession_number": "000032019324000081"}) == ACCESSION
    assert accession_of({"source_url": "https://example.test/none"}) == ""
    assert accession_of({}) == ""


# --- how a listing is asked for -------------------------------------------------------


def test_a_probe_drops_the_ticker_and_keeps_everything_else() -> None:
    probe = offered(SPLITS)
    assert "ticker" not in dict(probe.params)
    assert dict(probe.params)["sort"] == "execution_date"
    assert probe.template == SPLITS.template
    assert probe.dataset == SPLITS.dataset


def test_a_listing_is_probed_market_wide_and_fetched_per_issuer() -> None:
    vendor = Vendor(rows={"splits": [split_row(date(2021, 8, 31), ticker=OTHER)]})
    loader, replay = actions_loader(vendor)
    loader.discover(actions_spec(kinds=["split"]))
    assert [asked.params.get("ticker") for asked in replay.asked] == [None]


def test_the_four_outcomes_come_apart_under_one_sentence() -> None:
    """A refusal is about the plan and an empty page is about the window, and the two are
    never the same answer. A market that simply holds no splits is `EMPTY`; only the
    listing the plan excludes is `NOT_ENTITLED`. Reading the silent market as a plan is
    what tells an operator to buy a subscription they already hold, and this case used to
    assert exactly that.
    """
    known = Vendor(rows={}, granted=LISTINGS - {"splits"})
    with pytest.raises(NotEntitledError):
        actions_loader(known)[0].discover(actions_spec(kinds=["split"]))

    unknown = Vendor(rows={}, granted=LISTINGS - {"splits"}, known=False)
    with pytest.raises(MalformedRequestError):
        actions_loader(unknown)[0].discover(actions_spec(kinds=["split"]))

    silent = Vendor(rows={})
    with pytest.raises(EmptyResultError):
        actions_loader(silent)[0].discover(actions_spec(kinds=["split"]))

    elsewhere = Vendor(rows={"splits": [split_row(date(2021, 8, 31), ticker=OTHER)]})
    loader, _ = actions_loader(elsewhere)
    refs = loader.discover(actions_spec(kinds=["split"]))
    assert list(loader.load(refs[0], SPAN)) == []


def test_the_sentence_is_never_what_decides() -> None:
    for sentence in ("", "anything at all", "Warning [NOT_ENTITLED]: upgrade your plan."):
        vendor = Vendor(rows={}, granted=LISTINGS - {"splits"}, sentence=sentence)
        with pytest.raises(NotEntitledError):
            actions_loader(vendor)[0].discover(actions_spec(kinds=["split"]))


def test_one_probe_answers_for_the_whole_universe() -> None:
    rows = [split_row(date(2021, 8, 31), ticker=OTHER)]
    vendor = Vendor(rows={"splits": rows, "dividends": [dividend_row(date(2021, 5, 7))]})
    loader, replay = actions_loader(vendor)
    loader.discover(actions_spec(instruments=[TICKER, OTHER, "IBM"]))
    assert len(replay.asked) == 2


def test_a_listing_walks_its_cursor() -> None:
    days = [date(2021, 5, 7), date(2021, 8, 6)]
    vendor = Vendor(rows={"dividends": [dividend_row(day) for day in days]}, paged=True)
    client, replay = transport(vendor)
    rows = list(listing(client, DIVIDENDS_EFFECTIVE, TICKER, SPAN))
    assert [row["ex_dividend_date"] for row in rows] == [day.isoformat() for day in days]
    assert "/next/" in replay.paths[-1]


def test_a_rejected_shape_is_refused_outright() -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        return body({"status": "ERROR", "request_id": "frozen"}, 400)

    client, _ = transport(Vendor())
    client = MassiveClient("test-key-not-a-secret", transport=Replay(answer))
    with pytest.raises(MalformedRequestError) as failure:
        list(listing(client, SPLITS, TICKER, SPAN))
    assert "rejected the request" in failure.value.message


def test_a_refusal_mid_fetch_is_explained_by_probing() -> None:
    """The listing answers the probe, then refuses the issuer's own page."""
    seen: list[str] = []

    def answer(url: str, params: Mapping[str, str]) -> Response:
        seen.append(url)
        if listing_of(url) == "reference":
            return served([definition(TICKER)])
        if "ticker" in params:
            return body({"status": "NOT_AUTHORIZED", "message": "x"}, 403)
        return served([split_row(date(2021, 8, 31), ticker=OTHER)])

    client = MassiveClient("test-key-not-a-secret", transport=Replay(answer))
    with pytest.raises(NotEntitledError) as failure:
        list(listing(client, SPLITS, TICKER, SPAN))
    assert "changed during the run" in failure.value.message
    assert len(seen) == 2


def test_a_refusal_the_probe_reproduces_is_the_plan() -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        if listing_of(url) == "reference":
            return served([definition(TICKER)])
        return body({"status": "NOT_AUTHORIZED", "message": "x"}, 403)

    client = MassiveClient("test-key-not-a-secret", transport=Replay(answer))
    with pytest.raises(NotEntitledError) as failure:
        list(listing(client, SPLITS, TICKER, SPAN))
    assert "not included in this plan" in failure.value.message


def test_a_ref_names_an_instrument_its_own_spec_asks_for() -> None:
    loader, _ = actions_loader(Vendor(rows={"splits": [split_row(date(2021, 8, 31))]}))
    ref = loader.discover(actions_spec(kinds=["split"]))[0]
    assert ticker_of(ref, [TICKER]) == TICKER
    with pytest.raises(ValidationError) as failure:
        ticker_of(ref, [OTHER])
    assert INSTRUMENT in failure.value.message


def test_a_spec_round_trips_through_the_ref_it_is_carried_on() -> None:
    parsed = CorporateActionsSpec.model_validate(actions_spec(kinds=["dividend"]))
    assert encoded(parsed)["kinds"] == "dividend"
    assert encoded(parsed)["start"] == "2020-01-01"


# --- the filings index ----------------------------------------------------------------


def test_a_filing_is_a_record_of_a_document_and_an_instant() -> None:
    vendor = Vendor(rows={"filings": [filing_row(date(2024, 8, 2), period=date(2024, 6, 29))]})
    client, _ = transport(vendor)
    found = list(filings(client, TICKER, (date(2024, 8, 1), date(2024, 8, 3))))
    assert [filing.accession for filing in found] == [ACCESSION]
    assert found[0].accepted_ns == instant_ns("2024-08-02T20:41:20Z", "x")
    assert found[0].payload() == {
        "accession": ACCESSION,
        "form": "10-Q",
        "ticker": TICKER,
        "filed_on": "2024-08-02",
        "accepted_ns": found[0].accepted_ns,
        "period_end": "2024-06-29",
        "source_url": ARCHIVE,
    }


def test_a_filing_with_no_acceptance_instant_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        filing_of(filing_row(date(2024, 8, 2), accepted=None))
    assert ACCEPTANCE_FIELD in failure.value.message


def test_a_filing_without_a_period_carries_none() -> None:
    assert filing_of(filing_row(date(2024, 8, 2))).period_end is None


def test_the_index_asks_once_per_filing_day_and_skips_what_it_cannot_read() -> None:
    day = date(2024, 8, 2)
    vendor = Vendor(
        rows={
            "filings": [
                filing_row(day),
                filing_row(day, accession="", source="", accepted="2024-08-02T21:00:00Z"),
                filing_row(day, accession="0000320193-24-000099", accepted=None),
            ]
        }
    )
    client, replay = transport(vendor)
    index = AcceptanceIndex(client, TICKER)
    assert index.of(ACCESSION, day) == instant_ns("2024-08-02T20:41:20Z", "x")
    assert index.of("0000320193-24-000099", day) is None
    assert index.of("", day) is None
    assert index.days() == (day,)
    assert len(replay.asked) == 1


def test_the_index_reads_an_accession_from_a_row_that_names_none() -> None:
    day = date(2024, 8, 2)
    row = filing_row(day)
    del row["accession_number"]
    client, _ = transport(Vendor(rows={"filings": [row]}))
    assert AcceptanceIndex(client, TICKER).of(ACCESSION, day) is not None


# --- corporate actions: the spec ------------------------------------------------------


def test_a_spec_refuses_what_it_cannot_mean() -> None:
    with pytest.raises(ValidationError):
        CorporateActionsSpec.model_validate(actions_spec(start="2024-01-01", end="2023-01-01"))
    with pytest.raises(ValidationError):
        CorporateActionsSpec.model_validate(actions_spec(instruments=[TICKER, TICKER]))
    with pytest.raises(ValidationError):
        CorporateActionsSpec.model_validate(actions_spec(kinds=[]))
    with pytest.raises(ValidationError):
        CorporateActionsSpec.model_validate(actions_spec(kinds=["split", "split"]))


def test_the_kinds_decide_the_basis_and_the_publication_class() -> None:
    dividends = CorporateActionsSpec.model_validate(actions_spec(kinds=["dividend"]))
    assert dividends.basis == ANNOUNCED
    assert dividends.publication == "realtime"
    assert endpoints_for(dividends) == (DIVIDENDS_ANNOUNCED,)

    both = CorporateActionsSpec.model_validate(actions_spec())
    assert list(both.kinds) == list(DEFAULT_KINDS)
    assert both.basis == EFFECTIVE
    assert both.publication == "unknown"
    assert endpoints_for(both) == (SPLITS, DIVIDENDS_EFFECTIVE)

    splits = CorporateActionsSpec.model_validate(actions_spec(kinds=["split"]))
    assert splits.publication == "unknown"
    assert endpoints_for(splits) == (SPLITS,)


def test_each_shape_asks_by_the_date_it_stamps_by() -> None:
    assert dict(DIVIDENDS_ANNOUNCED.params)["declaration_date.gte"] == "{start}"
    assert dict(DIVIDENDS_EFFECTIVE.params)["ex_dividend_date.gte"] == "{start}"
    assert dict(SPLITS.params)["execution_date.gte"] == "{start}"


# --- corporate actions: the points ----------------------------------------------------


def points_of(vendor: Vendor, spec: dict[str, Any], window: tuple[date, date] = SPAN) -> list[Any]:
    loader, _ = actions_loader(vendor)
    ref = loader.discover(spec)[0]
    return list(loader.load(ref, window))


def test_a_split_is_stamped_at_the_end_of_the_day_it_took_effect() -> None:
    day = date(2020, 8, 31)
    vendor = Vendor(rows={"splits": [split_row(day)]})
    (action,) = points_of(vendor, actions_spec(kinds=["split"]))
    assert isinstance(action, CorporateAction)
    assert action.kind == "split"
    assert action.ratio == 4.0
    assert action.cash == 0.0
    assert action.currency == "USD"
    assert action.ts_event == day_end_ns(day)
    assert action.ts_init == action.ts_event
    assert action.ex_date_ns == day_start_ns(day)
    assert action.instrument_id == instrument_id(TICKER, VENUE)


def test_a_reverse_split_is_the_same_ratio_the_other_way() -> None:
    vendor = Vendor(rows={"splits": [split_row(date(2021, 3, 1), frm=4, to=1)]})
    (action,) = points_of(vendor, actions_spec(kinds=["split"]))
    assert action.ratio == 0.25


@pytest.mark.parametrize("bad", [0, -1, None, "4", True])
def test_a_split_ratio_has_two_positive_sides(bad: Any) -> None:
    vendor = Vendor(rows={"splits": [split_row(date(2021, 3, 1), to=bad)]})
    with pytest.raises(ValidationError) as failure:
        points_of(vendor, actions_spec(kinds=["split"]))
    assert "split_to" in failure.value.message


def test_a_dividend_in_the_effective_shape_is_stamped_at_its_ex_date() -> None:
    ex = date(2021, 5, 7)
    vendor = Vendor(rows=held(dividends=[dividend_row(ex, declared=date(2021, 4, 28))]))
    (action,) = points_of(vendor, actions_spec(kinds=["dividend", "split"]))
    assert action.kind == "dividend"
    assert action.ratio == 1.0
    assert action.cash == 0.24
    assert action.ts_event == day_end_ns(ex)
    assert action.ex_date_ns == day_start_ns(ex)


def test_a_dividend_in_the_announced_shape_is_knowable_before_it_goes_ex() -> None:
    ex, declared = date(2021, 5, 7), date(2021, 4, 28)
    vendor = Vendor(rows={"dividends": [dividend_row(ex, declared=declared)]})
    (action,) = points_of(vendor, actions_spec(kinds=["dividend"]))
    assert action.ts_event == day_end_ns(declared)
    assert action.ts_init == action.ts_event
    assert action.ex_date_ns == day_start_ns(ex)
    assert action.ts_event < action.ex_date_ns


def test_an_undeclared_dividend_is_refused_rather_than_back_dated() -> None:
    vendor = Vendor(rows={"dividends": [dividend_row(date(2021, 5, 7))]})
    with pytest.raises(ValidationError) as failure:
        points_of(vendor, actions_spec(kinds=["dividend"]))
    assert "declaration_date" in failure.value.message
    assert "kinds: [dividend, split]" in str(failure.value.remedy)


def test_a_dividend_keeps_its_own_currency_and_survives_a_missing_amount() -> None:
    ex = date(2021, 5, 7)
    rows = [
        dividend_row(ex, currency="EUR", cash=None),
        dividend_row(ex + timedelta(days=1), currency=None),
    ]
    vendor = Vendor(rows=held(dividends=rows))
    first, second = points_of(vendor, actions_spec(kinds=["dividend", "split"]))
    assert (first.currency, first.cash) == ("EUR", 0.0)
    assert (second.currency, second.cash) == ("USD", 0.24)


def test_the_two_listings_are_one_dataset_in_availability_order() -> None:
    vendor = Vendor(
        rows={
            "splits": [split_row(date(2020, 8, 31))],
            "dividends": [dividend_row(date(2020, 2, 7)), dividend_row(date(2021, 5, 7))],
        }
    )
    loader, _ = actions_loader(vendor)
    (ref,) = loader.discover(actions_spec())
    points = list(loader.load(ref, SPAN))
    assert [point.kind for point in points] == ["dividend", "split", "dividend"]
    assert [point.ts_init for point in points] == sorted(point.ts_init for point in points)
    assert ref.vendor_dataset == "splits+dividends"


def test_a_window_is_applied_to_what_came_back() -> None:
    """The source's own filter is never trusted: the loader filters what it was given."""
    vendor = Vendor(
        rows=held(dividends=[dividend_row(date(2020, 2, 7)), dividend_row(date(2021, 5, 7))])
    )
    points = points_of(
        vendor, actions_spec(kinds=["dividend", "split"]), (date(2021, 1, 1), date(2021, 12, 31))
    )
    assert [utc_day(point.ts_event) for point in points] == [date(2021, 5, 7)]


def test_a_discovered_dataset_says_where_it_came_from() -> None:
    vendor = Vendor(rows={"dividends": [dividend_row(date(2021, 5, 7), declared=date(2021, 4, 1))]})
    loader, _ = actions_loader(vendor)
    (ref,) = loader.discover(actions_spec(kinds=["dividend"]))
    assert ref.instrument == INSTRUMENT
    assert ref.type == "corporate_action"
    assert ref.resolution is None
    assert ref.adjusted is False
    assert ref.publication == "realtime"
    assert ref.publication_rule == "corporate_action"
    assert ref.vendor == "massive"
    assert ref.span == SPAN
    assert (ref.request_params or {})["venue"] == VENUE


def test_a_dataset_of_splits_declares_its_availability_unknown() -> None:
    vendor = Vendor(rows={"splits": [split_row(date(2020, 8, 31))]})
    loader, _ = actions_loader(vendor)
    (ref,) = loader.discover(actions_spec(kinds=["split"]))
    assert ref.publication == "unknown"


def test_the_manifest_records_the_days_that_were_served() -> None:
    served_days = [date(2021, 5, 7), date(2021, 8, 6)]
    vendor = Vendor(rows=held(dividends=[dividend_row(day) for day in served_days]))
    loader, _ = actions_loader(vendor)
    (ref,) = loader.discover(actions_spec(kinds=["dividend", "split"]))
    manifest = loader.manifest(ref)
    assert manifest.span == (served_days[0], served_days[-1])
    assert manifest.row_count == 2
    assert manifest.publication == "unknown"
    assert shortfall(ref.span, manifest.span) is not None


def test_the_points_reach_the_catalog_schema() -> None:
    vendor = Vendor(rows={"splits": [split_row(date(2020, 8, 31))]})
    loader, _ = actions_loader(vendor)
    (ref,) = loader.discover(actions_spec(kinds=["split"]))
    tables = loader.load_arrow(ref, SPAN)
    assert tables is not None
    assert sum(table.num_rows for table in tables) == 1
    assert resolve_type(ref.type) is CorporateAction


def test_a_ref_built_by_hand_carries_no_spec() -> None:
    loader, _ = actions_loader(Vendor())
    ref = DatasetRef(
        dataset_id="x",
        instrument=INSTRUMENT,
        type="corporate_action",
        resolution=None,
        span=SPAN,
        adjusted=False,
        publication="unknown",
    )
    with pytest.raises(ValidationError) as failure:
        list(loader.load(ref, SPAN))
    assert "carries no corporate-actions spec" in failure.value.message


# --- financials -----------------------------------------------------------------------


def statements_of(
    vendor: Vendor, spec: dict[str, Any] | None = None, window: tuple[date, date] = SPAN
) -> list[Any]:
    loader, _ = financials_loader(vendor)
    ref = loader.discover(spec or financials_spec())[0]
    return list(loader.load(ref, window))


def test_a_statement_is_stamped_at_the_instant_its_filing_was_accepted() -> None:
    period = date(2024, 6, 29)
    vendor = Vendor(rows={"financials": [financials_row(period)]})
    (point,) = statements_of(vendor)
    assert isinstance(point, FinancialStatement)
    assert point.ts_event == day_end_ns(period)
    assert point.ts_init == instant_ns("2024-08-02T20:41:20Z", "x")
    assert point.ts_init > point.ts_event
    assert point.stamped_from == ACCEPTANCE
    assert point.accession == ACCESSION
    assert (point.fiscal_period, point.fiscal_year, point.timeframe) == ("Q3", "2024", "quarterly")


def test_a_statement_keeps_its_figures_and_their_units_and_drops_the_ratios() -> None:
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29))]})
    (point,) = statements_of(vendor)
    assert point.items["income_statement.revenues"] == 94_930_000_000.0
    assert point.units["income_statement.revenues"] == "USD"
    assert point.items["balance_sheet.assets"] == 331_612_000_000.0
    assert not any(key.startswith("ratios.") for key in point.items)
    assert set(key.split(".")[0] for key in point.items) <= set(STATEMENTS)


def test_a_figure_that_is_not_a_number_is_not_a_figure() -> None:
    sections = {
        "income_statement": {
            "revenues": {"value": 1.0, "unit": ""},
            "flagged": {"value": True},
            "worded": {"value": "n/a"},
            "shapeless": 3,
        },
        "balance_sheet": ["not a section"],
    }
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29), sections=sections)]})
    (point,) = statements_of(vendor)
    assert list(point.items) == ["income_statement.revenues"]
    assert point.units == {}


@pytest.mark.parametrize("sections", [{}, {"ratios": {"pe": {"value": 3.0}}}, "nothing"])
def test_a_row_with_no_figures_is_not_a_statement(sections: Any) -> None:
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29), sections=sections)]})
    with pytest.raises(ValidationError) as failure:
        statements_of(vendor)
    assert "no figures" in failure.value.message


def test_a_statement_accepted_before_its_period_ended_is_refused() -> None:
    vendor = Vendor(
        rows={"financials": [financials_row(date(2024, 6, 29), accepted="2024-06-29T00:00:01Z")]}
    )
    with pytest.raises(ValidationError) as failure:
        statements_of(vendor)
    assert "not a publication instant" in failure.value.message


def test_a_statement_with_no_acceptance_is_joined_on_its_own_filing_day() -> None:
    period, filed = date(2024, 6, 29), date(2024, 8, 2)
    vendor = Vendor(
        rows={
            "financials": [financials_row(period, accepted=None, filed=filed)],
            "filings": [filing_row(filed)],
        }
    )
    loader, replay = financials_loader(vendor)
    (ref,) = loader.discover(financials_spec())
    (point,) = list(loader.load(ref, SPAN))
    assert point.stamped_from == INDEXED
    assert point.ts_init == instant_ns("2024-08-02T20:41:20Z", "x")
    asked = [item for item in replay.asked if "sec/filings" in item.url]
    assert [item.params["filing_date.gte"] for item in asked] == ["2024-08-02"]


def test_a_statement_the_index_cannot_answer_for_is_refused() -> None:
    period, filed = date(2024, 6, 29), date(2024, 8, 2)
    vendor = Vendor(
        rows={
            "financials": [financials_row(period, accepted=None, filed=filed)],
            "filings": [filing_row(filed, accession="0000320193-24-000099")],
        }
    )
    with pytest.raises(ValidationError) as failure:
        statements_of(vendor)
    assert ACCESSION in failure.value.message
    assert "filings index" in failure.value.message


def test_a_statement_with_no_day_to_ask_about_is_refused() -> None:
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29), accepted=None)]})
    with pytest.raises(ValidationError) as failure:
        statements_of(vendor)
    assert "filing_date" in failure.value.message
    assert "period end" in str(failure.value.remedy)


def test_a_restatement_is_another_point_with_a_later_availability() -> None:
    period = date(2024, 6, 29)
    vendor = Vendor(
        rows={
            "financials": [
                financials_row(period),
                financials_row(period, accepted="2024-11-01T18:00:00Z"),
            ]
        }
    )
    first, second = statements_of(vendor)
    assert first.ts_event == second.ts_event
    assert first.ts_init < second.ts_init


def test_a_financials_dataset_declares_a_delayed_publication_it_can_prove() -> None:
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29))]})
    loader, replay = financials_loader(vendor)
    (ref,) = loader.discover(financials_spec(timeframe="annual"))
    assert ref.publication == "delayed"
    assert ref.publication_rule == "fundamental"
    assert ref.type == "financial_statement"
    assert ref.vendor_dataset == "financials"
    points = list(loader.load(ref, SPAN))
    assert check_delayed(points, ref.publication_rule).id == "fundamental"
    fetch = [item for item in replay.asked if item.params.get("ticker") == TICKER]
    assert fetch[0].params["timeframe"] == "annual"


def test_a_financials_window_is_applied_to_what_came_back() -> None:
    vendor = Vendor(
        rows={
            "financials": [
                financials_row(date(2020, 6, 27)),
                financials_row(date(2024, 6, 29)),
            ]
        }
    )
    points = statements_of(vendor, None, (date(2024, 1, 1), date(2024, 12, 31)))
    assert [utc_day(point.ts_event) for point in points] == [date(2024, 6, 29)]


def test_the_financials_manifest_records_the_periods_that_were_served() -> None:
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29))]})
    loader, _ = financials_loader(vendor)
    (ref,) = loader.discover(financials_spec())
    manifest = loader.manifest(ref)
    assert manifest.span == (date(2024, 6, 29), date(2024, 6, 29))
    assert manifest.publication == "delayed"
    assert shortfall(ref.span, manifest.span) is not None
    tables = loader.load_arrow(ref, SPAN)
    assert tables is not None
    assert sum(table.num_rows for table in tables) == 1


def test_the_financials_spec_refuses_what_it_cannot_mean() -> None:
    with pytest.raises(ValidationError):
        FinancialsSpec.model_validate(financials_spec(start="2024-01-01", end="2023-01-01"))
    with pytest.raises(ValidationError):
        FinancialsSpec.model_validate(financials_spec(instruments=[TICKER, TICKER]))


def test_a_financials_ref_built_by_hand_carries_no_spec() -> None:
    loader, _ = financials_loader(Vendor())
    ref = DatasetRef(
        dataset_id="x",
        instrument=INSTRUMENT,
        type="financial_statement",
        resolution=None,
        span=SPAN,
        adjusted=False,
        publication="delayed",
        publication_rule="fundamental",
    )
    with pytest.raises(ValidationError) as failure:
        list(loader.load(ref, SPAN))
    assert "carries no financials spec" in failure.value.message


def test_an_unentitled_financials_listing_is_a_plan_failure_not_a_floor() -> None:
    vendor = Vendor(rows={}, granted=LISTINGS - {"financials"})
    loader, _ = financials_loader(vendor)
    with pytest.raises(NotEntitledError) as failure:
        loader.discover(financials_spec())
    assert failure.value.code is Exit.PRECONDITION
    assert failure.value.fatal is False


def test_the_filings_endpoint_is_asked_over_the_filing_date() -> None:
    assert dict(FILINGS.params)["filing_date.gte"] == "{start}"
    assert isinstance(FINANCIALS, Endpoint)
    assert dict(FINANCIALS.params)["period_of_report_date.gte"] == "{start}"


# --- the paths and the page sizes the source actually serves --------------------------


def test_the_filings_index_is_asked_for_under_the_version_that_serves_it() -> None:
    """Measured: `/v1/reference/sec/filings` answers and every other prefix does not."""
    day = date(2024, 8, 2)
    client, replay = transport(Vendor(rows={"filings": [filing_row(day)]}))
    found = list(filings(client, TICKER, (day, day)))
    assert [filing.accession for filing in found] == [ACCESSION]
    assert replay.asked[0].path.split("?")[0] == "v1/reference/sec/filings"


def test_a_listing_asked_for_under_the_wrong_version_is_a_404_and_not_a_refusal() -> None:
    """A wrong prefix is a rejected request, which is a different thing to say than a plan.

    The prefix this endpoint was first built with is the one under test here: the source
    answers it with a bare 404 — no entitlement sentence, no empty page — so nothing can
    mistake a mistyped path for a dataset the plan excludes or a window that held nothing.
    """
    stale = replace(FILINGS, template="/vX/reference/sec/filings")
    client, _ = transport(Vendor(rows={"filings": [filing_row(date(2024, 8, 2))]}))
    with pytest.raises(MalformedRequestError) as failure:
        list(filings(client, TICKER, (date(2024, 8, 1), date(2024, 8, 3)), endpoint=stale))
    assert "rejected the request" in failure.value.message
    assert "404" in failure.value.message


def test_the_financials_listing_asks_for_a_page_the_source_will_serve() -> None:
    """The ceiling is this endpoint's own, and the reference listings' is ten times it."""
    assert dict(FINANCIALS.params)["limit"] == FINANCIALS_PAGE_LIMIT
    assert int(FINANCIALS_PAGE_LIMIT) <= CEILING["financials"]
    assert dict(FILINGS.params)["limit"] == PAGE_LIMIT
    assert int(PAGE_LIMIT) > CEILING["financials"]

    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29))]})
    loader, replay = financials_loader(vendor)
    (ref,) = loader.discover(financials_spec())
    (point,) = list(loader.load(ref, SPAN))
    assert point.ts_event == day_end_ns(date(2024, 6, 29))
    assert {item.params["limit"] for item in replay.asked if "limit" in item.params} == {
        FINANCIALS_PAGE_LIMIT
    }


def test_a_financials_page_wider_than_the_source_serves_is_rejected_outright() -> None:
    """The limit this endpoint was first built with, and what the source does with it.

    Not a short page and not an empty one: the request itself is refused, so a listing
    asked for a thousand rows returns no statements at all rather than fewer of them.
    """
    wide = replace(
        FINANCIALS,
        params=tuple(
            (name, PAGE_LIMIT if name == "limit" else value) for name, value in FINANCIALS.params
        ),
    )
    client, _ = transport(Vendor(rows={"financials": [financials_row(date(2024, 6, 29))]}))
    with pytest.raises(MalformedRequestError) as failure:
        list(listing(client, wide, TICKER, SPAN))
    assert "rejected the request" in failure.value.message


def test_the_financials_listing_walks_its_cursor_at_the_page_the_source_serves() -> None:
    """A hundred rows a page is not a hundred-row answer: the cursor carries the rest."""
    periods = [date(2020, 1, 1) + timedelta(days=10 * step) for step in range(150)]
    vendor = Vendor(
        rows={"financials": [financials_row(period) for period in periods]},
        paged=True,
        page_size=int(FINANCIALS_PAGE_LIMIT),
    )
    loader, replay = financials_loader(vendor)
    (ref,) = loader.discover(financials_spec())
    points = list(loader.load(ref, SPAN))
    assert [utc_day(point.ts_event) for point in points] == periods

    walk = [
        item for item in replay.asked if item.params.get("ticker") == TICKER or "/next/" in item.url
    ]
    assert len(walk) == 2
    assert walk[0].params["limit"] == FINANCIALS_PAGE_LIMIT
    assert walk[1].url == CURSOR.format(name="financials")
    assert loader.manifest(ref).row_count == len(periods)


def test_a_filing_record_is_a_value() -> None:
    filing = Filing(
        accession=ACCESSION,
        form="10-Q",
        ticker=TICKER,
        filed_on=date(2024, 8, 2),
        accepted_ns=1,
        source_url=ARCHIVE,
    )
    assert filing.period_end is None
    assert filing.payload()["accession"] == ACCESSION


# --- into the store -------------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    """The one attribute the catalog reads off a workspace."""

    root: Path


def test_a_financials_dataset_is_one_the_store_accepts(tmp_path: Path) -> None:
    """The delayed publication it declares is one its own timestamps carry."""
    vendor = Vendor(rows={"financials": [financials_row(date(2024, 6, 29))]})
    loader, _ = financials_loader(vendor)
    (ref,) = loader.discover(financials_spec())
    written = write(Store(root=tmp_path), loader.load(ref, SPAN), ref=ref, source=loader.id)
    assert written.manifest.publication == "delayed"
    assert written.manifest.publication_rule == "fundamental"
    assert written.manifest.span == (date(2024, 6, 29), date(2024, 6, 29))
    assert written.manifest.vendor == "massive"
    assert written.truncated
    assert "missing" in str(written.shortfall)


def test_a_corporate_actions_dataset_is_one_the_store_accepts(tmp_path: Path) -> None:
    """Including the shape whose availability is a bound, which says so and is written."""
    served_days = [date(2021, 5, 7), date(2021, 8, 6)]
    vendor = Vendor(
        rows=held(
            splits=[split_row(date(2021, 6, 1))],
            dividends=[dividend_row(day) for day in served_days],
        )
    )
    loader, _ = actions_loader(vendor)
    (ref,) = loader.discover(actions_spec())
    written = write(Store(root=tmp_path), loader.load(ref, SPAN), ref=ref, source=loader.id)
    assert written.manifest.publication == "unknown"
    assert written.manifest.row_count == 3
    assert written.manifest.span == (served_days[0], served_days[-1])
    assert written.shortfall is not None
