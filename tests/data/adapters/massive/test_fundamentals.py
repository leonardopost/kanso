"""Corporate actions, financial statements and the filings indexes that stamp them.

Three properties are what these tests exist for, and every case here is one of them.

**An empty listing is not a refusal.** A reference listing filtered to one issuer answers
with no rows both when the plan excludes it and when the issuer did nothing, so the
fixture vendor holds rows for other issuers too: a market-wide probe sees them, a
per-issuer fetch does not, and the four outcomes come apart under the identical sentence.

**An availability is read, never invented.** A statement is stamped at the instant the
filing its row names was accepted, or it is not a point; a corporate action is stamped at
the day it was announced where the source serves one and at the day it took effect where
it does not, and the dataset says which by its publication class. No test here accepts a
point stamped from a period end, a fetch time, or a date the source did not serve.

**Coverage is what was served.** The vendor holds fewer days than the spec asks for, and
the manifest records the days it held.

**The statements and both filings indexes answer as they were recorded answering.** Every
answer the financials tests read is a response recorded from the live source on 2026-10-15
(`fixtures/fundamentals/`, with `provenance.json` beside it), served only for exactly the
request it was recorded for: a request nothing was recorded for fails the test rather than
being answered by a guess. The fixtures they replace recorded what their author expected —
a filings row naming its issuer, an acceptance instant with a zone, an index that honoured
an issuer filter — and the live source does none of the three.

Nothing here opens a socket, reads a credential or carries a recorded secret.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
from kanso.data.adapters.massive.errors import (
    EmptyResultError,
    MalformedRequestError,
    NotEntitledError,
)
from kanso.data.adapters.massive.filings import (
    FILINGS,
    FILINGS_INDEX,
    PERIODIC_FORMS,
    AcceptanceIndex,
    accession_of,
    day_end_ns,
    day_start_ns,
    decoded,
    edgar_instant_ns,
    encoded,
    listing,
    offered,
    parse_day,
    ticker_of,
)
from kanso.data.adapters.massive.financials import (
    BALANCE_SHEETS,
    CASH_FLOW_STATEMENTS,
    ENDPOINTS,
    FINANCIALS_PAGE_LIMIT,
    INCOME_STATEMENTS,
    INDEXED,
    STATEMENTS,
    FinancialsLoader,
    FinancialsSpec,
    FinancialStatement,
    statement_of,
)
from kanso.data.catalog import write
from kanso.data.loader import DatasetRef, to_ns, utc_day
from kanso.data.loaders.points import instrument_id
from kanso.data.manifest import shortfall
from kanso.data.publication import check_delayed
from kanso.data.types import CorporateAction, resolve_type
from kanso.errors import Exit, ValidationError

from . import Replay, body, definition, missing, nothing, refused, served

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

LISTINGS = frozenset({"splits", "dividends"})

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
}
"""Where each corporate-actions listing lives, version prefix and all. The fixture matches
the path whole, so a path the source does not serve is a 404 here as there."""

CONTROL = "/v3/reference/tickers/"
"""The control endpoint, which is asked per ticker and so matched by prefix."""


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
    """A plan the corporate-actions fixture serves under, in the terms the source gates on.

    `rows` holds every issuer's rows for each listing; a request naming a ticker is
    filtered to it and a market-wide probe is not, which is exactly the asymmetry the
    probe protocol rests on. `granted` is what the plan includes, `known` is whether the
    reference endpoint recognises the key, and `sentence` is the vendor's prose — a knob
    only so a test can change it and prove no outcome moved. A path the source does not
    serve is a 404 with no envelope, its own measured behaviour. `paged` and `page_size`
    say how many rows a page holds while the cursor carries the rest.
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
        found = self.rows.get(name, [])
        if "ticker" in params:
            found = [row for row in found if row.get("ticker") == params["ticker"]]
        if not self.paged:
            return served(found)
        if "/next/" in url:
            return served(found[self.page_size :])
        return served(found[: self.page_size], next_url=CURSOR.format(name=name))


def transport(vendor: Any) -> tuple[MassiveClient, Replay]:
    """A client over one frozen vendor, and the record of what it was asked."""
    replay = Replay(vendor)
    return MassiveClient("test-key-not-a-secret", transport=replay), replay


def actions_loader(vendor: Vendor) -> tuple[CorporateActionsLoader, Replay]:
    client, replay = transport(vendor)
    return CorporateActionsLoader(client=client, as_of=TODAY), replay


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


ELSEWHERE: dict[str, dict[str, Any]] = {
    "splits": split_row(date(2019, 1, 2), ticker=OTHER),
    "dividends": dividend_row(date(2019, 1, 3), ticker=OTHER, declared=date(2019, 1, 2)),
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


# --- the recorded source: statements and both filings indexes -------------------------

FUNDAMENTALS = Path(__file__).parent / "fixtures" / "fundamentals"

PROVENANCE: dict[str, Any] = json.loads((FUNDAMENTALS / "provenance.json").read_text("utf-8"))

ISSUER_PARAMS = frozenset({"tickers", "cik", "ticker"})
"""The parameters that narrow a listing to one issuer. A request carrying none is a probe."""


def request_key(url: str, params: Mapping[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    """A request as a path and its parameters, wherever the parameters were spelled."""
    parts = urlsplit(url)
    asked = {**dict(parse_qsl(parts.query)), **params}
    return parts.path, tuple(sorted(asked.items()))


RECORDED: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {
    request_key(meta["url"], {}): name for name, meta in PROVENANCE["files"].items()
}
"""Which recording answers which request: exactly the one it was recorded for."""

MARKET = {
    INCOME_STATEMENTS.template: "market_income_statements.json",
    BALANCE_SHEETS.template: "market_balance_sheets.json",
    CASH_FLOW_STATEMENTS.template: "market_cash_flow_statements.json",
    FILINGS_INDEX.template: "market_filings_index.json",
    FILINGS.template: "market_filings.json",
}
"""What a market-wide probe of each listing was answered with, whatever window it names:
the probe asks a fortnight and then nothing, and either way the market holds rows."""


def fundamental(name: str) -> Response:
    """One recorded answer: its body as served and its status as recorded."""
    return Response(
        status=int(PROVENANCE["files"][name]["status"]), body=(FUNDAMENTALS / name).read_bytes()
    )


def rows_of(name: str) -> list[dict[str, Any]]:
    """The rows a recorded listing served, in the order served."""
    found: list[dict[str, Any]] = json.loads(fundamental(name).body)["results"]
    return found


def recorded(*, instead: Mapping[str, str] | None = None) -> Any:
    """The source as recorded, answering each request with exactly what it was answered.

    `instead` swaps one recording for another by file name, which is how a test shows what
    a load does with an answer the source gave elsewhere — a 404 in place of a report it
    holds — without writing an answer the source never gave.
    """
    swaps = dict(instead or {})

    def answer(url: str, params: Mapping[str, str]) -> Response:
        path, asked = request_key(url, params)
        if path.startswith(CONTROL):
            return served([definition(TICKER)])
        name = RECORDED.get((path, asked))
        if name is None and not ISSUER_PARAMS & dict(asked).keys():
            name = MARKET.get(path)
        if name is None:
            pytest.fail(f"asked {path} with {dict(asked)}, which nothing was recorded for")
        return fundamental(swaps.get(name, name))

    return answer


def financials_loader(answer: Any = None) -> tuple[FinancialsLoader, Replay]:
    client, replay = transport(answer or recorded())
    return FinancialsLoader(client=client, as_of=TODAY), replay


def financials_spec(**overrides: Any) -> dict[str, Any]:
    """AAPL's quarters ending in the first half of 2026: what the statements were recorded for."""
    spec: dict[str, Any] = {
        "loader": "massive_financials",
        "instruments": [TICKER],
        "venue": VENUE,
        "start": "2026-01-01",
        "end": "2026-06-30",
    }
    spec.update(overrides)
    return spec


FIRST_HALF = (date(2026, 1, 1), date(2026, 6, 30))

Q2_ACCEPTED = to_ns(datetime(2026, 5, 1, 10, 1, 0, tzinfo=UTC))
Q3_ACCEPTED = to_ns(datetime(2026, 7, 31, 10, 1, 2, tzinfo=UTC))
"""AAPL's two 10-Qs, accepted at 06:01:00 and 06:01:02 Eastern daylight time — the
recorded `20260501060100` and `20260731060102`, four hours behind UTC."""


def statements_of(spec: dict[str, Any] | None = None, answer: Any = None) -> list[Any]:
    loader, _ = financials_loader(answer)
    ref = loader.discover(spec or financials_spec())[0]
    return list(loader.load(ref, ref.span))


# --- the acceptance clock -------------------------------------------------------------


@pytest.mark.parametrize(
    ("clock", "utc"),
    [
        ("20260731060102", datetime(2026, 7, 31, 10, 1, 2, tzinfo=UTC)),
        ("20260213160000", datetime(2026, 2, 13, 21, 0, 0, tzinfo=UTC)),
        ("20200730204446", datetime(2020, 7, 31, 0, 44, 46, tzinfo=UTC)),
    ],
)
def test_the_acceptance_clock_is_read_as_the_regulator_s_eastern_time(
    clock: str, utc: datetime
) -> None:
    """Four hours behind UTC in summer and five in winter, as the regulator's clock is."""
    assert edgar_instant_ns(clock, "filings.acceptance_datetime") == to_ns(utc)


def test_a_clock_the_fall_back_repeats_is_read_as_its_later_instant() -> None:
    """01:30 happens twice on 2026-11-01; the later is the bound that cannot leak."""
    assert edgar_instant_ns("20261101013000", "x") == to_ns(
        datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    )


@pytest.mark.parametrize("value", ["2026-07-31T06:01:02Z", "2026073106010", "2026073106010x", ""])
def test_an_acceptance_clock_of_any_other_shape_is_refused(value: str) -> None:
    with pytest.raises(ValidationError) as failure:
        edgar_instant_ns(value, "filings.acceptance_datetime")
    assert "YYYYMMDDHHMMSS" in failure.value.message
    assert "filings.acceptance_datetime" in failure.value.message


# --- the two filings indexes ----------------------------------------------------------


def test_the_acceptance_index_ignores_an_issuer_and_reads_a_date_as_digits() -> None:
    """Why it is asked one report at a time: recorded, a listing for AAPL by ticker and by
    CIK served three other issuers' reports, and an ISO filing date matched nothing."""
    issuers = {
        row["entities"][0]["company_data"]["ticker"]
        for row in rows_of("filings_ignores_issuer.json")
    }
    assert issuers and TICKER not in issuers
    assert rows_of("filings_iso_date.json") == []
    assert (
        "acceptance_datetime"
        not in json.loads(fundamental("acceptance_0000320193-17-000009.json").body)["results"]
    )


def acceptance_index(
    ticker: str, cik: str, *days: date, answer: Any = None
) -> tuple[AcceptanceIndex, Replay]:
    client, replay = transport(answer or recorded())
    return AcceptanceIndex(client, ticker, cik, set(days)), replay


def test_a_day_is_stamped_at_its_periodic_report_s_acceptance() -> None:
    index, replay = acceptance_index(TICKER, "0000320193", date(2026, 5, 1), date(2026, 7, 31))
    assert index.of(date(2026, 7, 31)) == (Q3_ACCEPTED, "0000320193-26-000020")
    assert index.of(date(2026, 5, 1)) == (Q2_ACCEPTED, "0000320193-26-000013")
    assert index.of(date(2026, 6, 1)) is None
    assert index.of(date(2026, 7, 31)) == (Q3_ACCEPTED, "0000320193-26-000020")
    walks = [item for item in replay.asked if FILINGS_INDEX.template in item.url]
    lookups = [item for item in replay.asked if FILINGS.template + "/" in item.url]
    assert len(walks) == 1, "one walk of the filings index over the days the rows name"
    assert walks[0].params["cik"] == "0000320193"
    assert len(lookups) == 2, "each report's instant is asked for once and kept"
    assert all(item.params == {} for item in lookups)


def test_a_report_accepted_after_the_cut_off_stamps_the_day_it_was_dated() -> None:
    """MSFT's 10-K was accepted at 20:44 Eastern on 07-30 and dated 07-31; a statement
    naming 07-31 is stamped at that acceptance, which is the 31st in UTC."""
    index, _ = acceptance_index(OTHER, "0000789019", date(2020, 7, 30), date(2020, 7, 31))
    expected = (to_ns(datetime(2020, 7, 31, 0, 44, 46, tzinfo=UTC)), "0001564590-20-034944")
    assert index.of(date(2020, 7, 31)) == expected
    assert index.of(date(2020, 7, 30)) == expected


@pytest.mark.parametrize(
    ("ticker", "cik", "day"),
    [
        (TICKER, "0000320193", date(2017, 8, 2)),
        (OTHER, "0000789019", date(2025, 1, 29)),
    ],
)
def test_a_report_the_acceptance_index_holds_no_instant_for_stamps_nothing(
    ticker: str, cik: str, day: date
) -> None:
    """Held without the field (AAPL's 10-Q of 2017-08-02) or not held at all (MSFT's 10-Q of
    2025-01-29, a 404): either way there is no instant, and the day is not stamped."""
    index, _ = acceptance_index(ticker, cik, day)
    assert index.of(day) is None


def test_a_row_for_another_issuer_or_another_form_stamps_nothing() -> None:
    """The filings index honours its filters, measured; a version that stopped would cost a
    stamp here, never stamp a statement from someone else's report or a press release."""
    day = "2026-07-31"
    rows = [
        {
            "cik": "0000000001",
            "form_type": "10-Q",
            "filing_date": day,
            "accession_number": "0000000001-26-000001",
        },
        {
            "cik": "0000320193",
            "form_type": "8-K",
            "filing_date": day,
            "accession_number": "0000320193-26-000019",
        },
    ]

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if FILINGS_INDEX.template in url:
            return served(rows)
        pytest.fail(f"asked {url}: no report that day is the issuer's periodic one")

    index, _ = acceptance_index(TICKER, "0000320193", date(2026, 7, 31), answer=answer)
    assert index.of(date(2026, 7, 31)) is None


def test_a_day_with_two_reports_is_stamped_at_the_later_and_at_none_if_either_has_none() -> None:
    day = date(2026, 7, 31)
    early, late = "0000320193-26-000020", "0000320193-26-000021"
    accepted = {early: "20260731060102", late: "20260731163000"}

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if FILINGS_INDEX.template in url:
            return served(
                [
                    {
                        "cik": "0000320193",
                        "form_type": "10-Q",
                        "filing_date": "2026-07-31",
                        "accession_number": early,
                    },
                    {
                        "cik": "0000320193",
                        "form_type": "10-Q/A",
                        "filing_date": "2026-07-31",
                        "accession_number": late,
                    },
                ]
            )
        accession = url.rsplit("/", 1)[1]
        if accepted.get(accession) is None:
            return body({"status": "NOT_FOUND", "message": "Filing not found."}, 404)
        return body(
            {
                "status": "OK",
                "results": {
                    "accession_number": accession,
                    "acceptance_datetime": accepted[accession],
                },
            }
        )

    index, _ = acceptance_index(TICKER, "0000320193", day, answer=answer)
    assert index.of(day) == (to_ns(datetime(2026, 7, 31, 20, 30, tzinfo=UTC)), late)

    accepted[late] = None  # type: ignore[assignment]
    index, _ = acceptance_index(TICKER, "0000320193", day, answer=answer)
    assert index.of(day) is None


def test_an_index_of_no_days_asks_nothing() -> None:
    index, replay = acceptance_index(TICKER, "0000320193")
    assert index.of(date(2026, 7, 31)) is None
    assert replay.asked == []


def test_a_refused_acceptance_index_is_the_plan_and_not_a_missing_instant() -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        if CONTROL in url:
            return served([definition(TICKER)])
        if FILINGS_INDEX.template in url:
            return fundamental("filings_index_AAPL.json")
        return refused()

    index, _ = acceptance_index(TICKER, "0000320193", date(2026, 7, 31), answer=answer)
    with pytest.raises(NotEntitledError):
        index.of(date(2026, 7, 31))


def test_the_filings_index_is_asked_by_cik_and_by_periodic_form() -> None:
    path, params = FILINGS_INDEX.request(TICKER, (date(2026, 5, 1), date(2026, 7, 31)))
    assert path == "/stocks/filings/vX/index"
    assert params["form_type.any_of"].split(",") == list(PERIODIC_FORMS)
    assert (params["filing_date.gte"], params["filing_date.lte"]) == ("2026-05-01", "2026-07-31")
    assert "{ticker}" not in json.dumps(params), "the CIK travels beside the endpoint"
    assert offered(FILINGS) == FILINGS


# --- financials: what a statement is --------------------------------------------------


def test_a_statement_is_stamped_at_the_acceptance_of_the_filing_its_row_names() -> None:
    points = statements_of()
    assert all(isinstance(point, FinancialStatement) for point in points)
    assert len(points) == 6, "three statements for each of two quarters"
    by_quarter = {point.fiscal_period: point for point in points}
    q2, q3 = by_quarter["Q2"], by_quarter["Q3"]
    assert (q2.ts_event, q2.ts_init) == (day_end_ns(date(2026, 3, 28)), Q2_ACCEPTED)
    assert (q3.ts_event, q3.ts_init) == (day_end_ns(date(2026, 6, 27)), Q3_ACCEPTED)
    assert (q3.accession, q3.stamped_from) == ("0000320193-26-000020", INDEXED)
    assert (q3.fiscal_year, q3.timeframe) == ("2026", "quarterly")
    assert [point.ts_init for point in points] == sorted(point.ts_init for point in points)
    assert all(point.ts_init > point.ts_event for point in points)


def test_a_statement_keeps_every_figure_under_its_statement_and_states_no_unit() -> None:
    """The source names no unit, so none is written; a zero it serves is kept as served."""
    points = statements_of()
    income = next(
        p for p in points if p.fiscal_period == "Q3" and "income_statement.revenue" in p.items
    )
    served_row = rows_of("income_statements_AAPL.json")[-1]
    assert income.items["income_statement.revenue"] == served_row["revenue"] == 109_417_000_000.0
    assert income.items["income_statement.interest_expense"] == 0.0
    assert income.items["income_statement.basic_earnings_per_share"] == 2.03
    assert not {"income_statement.fiscal_year", "income_statement.fiscal_quarter"} & set(
        income.items
    )
    assert income.units == {}
    assert {key.split(".")[0] for point in points for key in point.items} == set(STATEMENTS)
    assert all(len({key.split(".")[0] for key in point.items}) == 1 for point in points)


def test_a_figure_that_is_not_a_number_is_not_a_figure() -> None:
    row = {
        "period_end": "2026-06-27",
        "revenue": 1.0,
        "flagged": True,
        "worded": "n/a",
        "tickers": ["AAPL"],
        "fiscal_year": 2026,
    }
    spec = FinancialsSpec.model_validate(financials_spec())
    point = statement_of(
        row, "income_statement", spec, instrument_id(TICKER, VENUE), (Q3_ACCEPTED, "x")
    )
    assert point.items == {"income_statement.revenue": 1.0}


def test_a_row_with_no_figures_is_not_a_statement() -> None:
    row = {
        "period_end": "2026-06-27",
        "fiscal_year": 2026,
        "fiscal_quarter": 3,
        "tickers": ["AAPL"],
    }
    spec = FinancialsSpec.model_validate(financials_spec())
    with pytest.raises(ValidationError) as failure:
        statement_of(
            row, "income_statement", spec, instrument_id(TICKER, VENUE), (Q3_ACCEPTED, "x")
        )
    assert "no figures" in failure.value.message


def test_a_statement_accepted_before_its_period_ended_is_refused() -> None:
    row = {"period_end": "2026-06-27", "revenue": 1.0}
    spec = FinancialsSpec.model_validate(financials_spec())
    early = (day_end_ns(date(2026, 6, 27)), "x")
    with pytest.raises(ValidationError) as failure:
        statement_of(row, "income_statement", spec, instrument_id(TICKER, VENUE), early)
    assert "not a publication instant" in failure.value.message


@pytest.mark.parametrize(("timeframe", "named"), [("annual", "FY"), ("ttm", "TTM")])
def test_a_point_names_a_grain_that_is_not_a_quarter_as_the_retired_listing_did(
    timeframe: str, named: str
) -> None:
    row = {"period_end": "2026-06-27", "revenue": 1.0, "fiscal_quarter": 4, "fiscal_year": 2026}
    spec = FinancialsSpec.model_validate(
        financials_spec(timeframe=timeframe, statements=["income_statement"])
    )
    point = statement_of(
        row, "income_statement", spec, instrument_id(TICKER, VENUE), (Q3_ACCEPTED, "x")
    )
    assert (point.fiscal_period, point.timeframe) == (named, timeframe)


# --- financials: what can and cannot be stamped ---------------------------------------


def test_a_row_naming_a_report_without_an_instant_is_dropped_and_the_rest_kept() -> None:
    """The source holding no instant for AAPL's Q2 report, as it holds none for its 10-Q of
    2017-08-02: the Q2 statements are not points and the Q3 ones still are."""
    answer = recorded(
        instead={"acceptance_0000320193-26-000013.json": "acceptance_0000320193-17-000009.json"}
    )
    points = statements_of(answer=answer)
    assert {point.fiscal_period for point in points} == {"Q3"}
    assert len(points) == 3


def test_a_series_of_which_nothing_can_be_stamped_is_refused_naming_why() -> None:
    """BABA files 20-Fs, which the acceptance index does not hold: recorded, its 20-F of
    2026-05-20 is listed by the filings index and answers 404 under its accession."""
    spec = financials_spec(
        instruments=["BABA"],
        start="2025-01-01",
        end="2026-06-30",
        timeframe="annual",
        statements=["income_statement"],
    )
    with pytest.raises(ValidationError) as failure:
        statements_of(spec)
    assert "BABA" in failure.value.message
    assert "none of them" in failure.value.message
    assert "20-F" in str(failure.value.remedy)


def test_a_row_with_no_filing_day_or_no_issuer_is_dropped() -> None:
    rows = rows_of("income_statements_AAPL.json")
    rows[0] = {key: value for key, value in rows[0].items() if key != "filing_date"}
    rows[1] = {key: value for key, value in rows[1].items() if key != "cik"}

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if INCOME_STATEMENTS.template in url and "tickers" in params:
            return served(rows)
        return recorded()(url, params)

    with pytest.raises(ValidationError) as failure:
        statements_of(financials_spec(statements=["income_statement"]), answer=answer)
    assert "none of them" in failure.value.message


# --- financials: the spec, the request and the dataset --------------------------------


def test_the_financials_spec_refuses_what_it_cannot_mean() -> None:
    for bad in (
        financials_spec(start="2024-01-01", end="2023-01-01"),
        financials_spec(instruments=[TICKER, TICKER]),
        financials_spec(statements=[]),
        financials_spec(statements=["income_statement", "income_statement"]),
        financials_spec(statements=["ratios"]),
    ):
        with pytest.raises(ValidationError):
            FinancialsSpec.model_validate(bad)


def test_a_balance_sheet_has_no_trailing_twelve_months() -> None:
    """Measured: the balance sheets listing answers a trailing-twelve-month request with no
    rows, so a spec asking for one is refused and one left to default does not ask it."""
    with pytest.raises(ValidationError) as failure:
        FinancialsSpec.model_validate(
            financials_spec(timeframe="ttm", statements=["balance_sheet"])
        )
    assert "balance at an instant" in str(failure.value)
    assert FinancialsSpec.model_validate(financials_spec(timeframe="ttm")).asked == (
        "income_statement",
        "cash_flow_statement",
    )
    assert FinancialsSpec.model_validate(financials_spec()).asked == STATEMENTS


def test_a_spec_with_its_statements_left_out_round_trips_through_its_ref() -> None:
    for spec in (
        financials_spec(),
        financials_spec(statements=["cash_flow_statement", "income_statement"]),
    ):
        parsed = FinancialsSpec.model_validate(spec)
        again = FinancialsSpec.model_validate(
            decoded(encoded(parsed), ("instruments", "statements"))
        )
        assert again == parsed


@pytest.mark.parametrize(
    ("timeframe", "asked"),
    [("quarterly", "quarterly"), ("annual", "annual"), ("ttm", "trailing_twelve_months")],
)
def test_each_statement_is_asked_over_its_period_at_the_grain_the_source_spells(
    timeframe: str, asked: str
) -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "tickers" in params:
            return nothing()
        return recorded()(url, params)

    loader, replay = financials_loader(answer)
    (ref,) = loader.discover(financials_spec(timeframe=timeframe))
    assert list(loader.load(ref, ref.span)) == []
    fetched = [item for item in replay.asked if "tickers" in item.params]
    assert [item.url.split("api.massive.com")[1] for item in fetched] == [
        ENDPOINTS[name].template
        for name in FinancialsSpec.model_validate(financials_spec(timeframe=timeframe)).asked
    ]
    for item in fetched:
        assert item.params["tickers"] == TICKER
        assert item.params["timeframe"] == asked
        assert (item.params["period_end.gte"], item.params["period_end.lte"]) == (
            "2026-01-01",
            "2026-06-30",
        )
        assert item.params["sort"] == "period_end.asc"
        assert item.params["limit"] == FINANCIALS_PAGE_LIMIT


def test_discovery_establishes_the_statements_and_both_indexes_once_for_the_universe() -> None:
    loader, replay = financials_loader()
    loader.discover(financials_spec(instruments=[TICKER, OTHER, "IBM"]))
    probed = [item.url.split("api.massive.com")[1].split("?")[0] for item in replay.asked]
    assert sorted(probed) == sorted(
        [INCOME_STATEMENTS.template, FILINGS_INDEX.template, FILINGS.template]
    )
    assert not any(ISSUER_PARAMS & item.params.keys() for item in replay.asked)


def test_an_unentitled_financials_listing_is_a_plan_failure_not_a_floor() -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        if CONTROL in url:
            return served([definition(TICKER)])
        if INCOME_STATEMENTS.template in url:
            return refused()
        return recorded()(url, params)

    loader, _ = financials_loader(answer)
    with pytest.raises(NotEntitledError) as failure:
        loader.discover(financials_spec())
    assert failure.value.code is Exit.PRECONDITION
    assert failure.value.fatal is False


def test_a_financials_dataset_declares_a_delayed_publication_it_can_prove() -> None:
    loader, _ = financials_loader()
    (ref,) = loader.discover(financials_spec())
    assert (ref.publication, ref.publication_rule) == ("delayed", "fundamental")
    assert (ref.type, ref.vendor_dataset) == ("financial_statement", "financials")
    points = list(loader.load(ref, ref.span))
    assert check_delayed(points, ref.publication_rule).id == "fundamental"


def test_a_financials_window_is_applied_to_what_came_back() -> None:
    """Asked for the second quarter alone and answered with the half year it was recorded
    over, the load keeps the quarter it asked for and nothing else."""
    by_path = {
        INCOME_STATEMENTS.template: "income_statements_AAPL.json",
        BALANCE_SHEETS.template: "balance_sheets_AAPL.json",
        CASH_FLOW_STATEMENTS.template: "cash_flow_statements_AAPL.json",
    }

    def answer(url: str, params: Mapping[str, str]) -> Response:
        path = urlsplit(url).path
        if "tickers" in params and path in by_path:
            return fundamental(by_path[path])
        return recorded()(url, params)

    loader, _ = financials_loader(answer)
    (ref,) = loader.discover(financials_spec())
    points = list(loader.load(ref, (date(2026, 4, 1), date(2026, 6, 30))))
    assert {utc_day(point.ts_event) for point in points} == {date(2026, 6, 27)}


def test_the_financials_manifest_records_the_periods_that_were_served() -> None:
    loader, _ = financials_loader()
    (ref,) = loader.discover(financials_spec())
    manifest = loader.manifest(ref)
    assert manifest.span == (date(2026, 3, 28), date(2026, 6, 27))
    assert manifest.row_count == 6
    assert shortfall(ref.span, manifest.span) is not None
    tables = loader.load_arrow(ref, ref.span)
    assert tables is not None
    assert sum(table.num_rows for table in tables) == 6


def test_a_financials_ref_built_by_hand_carries_no_spec() -> None:
    loader, _ = financials_loader()
    ref = DatasetRef(
        dataset_id="x",
        instrument=INSTRUMENT,
        type="financial_statement",
        resolution=None,
        span=FIRST_HALF,
        adjusted=False,
        publication="delayed",
        publication_rule="fundamental",
    )
    with pytest.raises(ValidationError) as failure:
        list(loader.load(ref, FIRST_HALF))
    assert "carries no financials spec" in failure.value.message


# --- the paths the source serves, and the one it retired ------------------------------


def test_the_retired_statements_listing_is_named_as_retired() -> None:
    """Recorded after its sunset: HTTP 410 and `GONE`. Read as a malformed request it would
    send an operator to check parameters that were never wrong."""
    retired = replace(INCOME_STATEMENTS, template="/vX/reference/financials")
    client, _ = transport(lambda url, params: fundamental("financials_retired.json"))
    with pytest.raises(MalformedRequestError) as failure:
        list(listing(client, retired, TICKER, FIRST_HALF))
    assert "retired /vX/reference/financials" in failure.value.message
    assert "410" in failure.value.message
    assert "uv lock --upgrade-package kanso" in str(failure.value.remedy)


def test_a_listing_asked_for_under_the_wrong_version_is_a_404_and_not_a_refusal() -> None:
    """A wrong prefix is a rejected request, which is a different thing to say than a plan:
    the source answers it with a bare 404 — no entitlement sentence, no empty page."""
    stale = replace(SPLITS, template="/vX/reference/splits")
    client, _ = transport(Vendor(rows={"splits": [split_row(date(2021, 8, 31))]}))
    with pytest.raises(MalformedRequestError) as failure:
        list(listing(client, stale, TICKER, SPAN))
    assert "rejected the request" in failure.value.message
    assert "404" in failure.value.message


# --- into the store -------------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    """The one attribute the catalog reads off a workspace."""

    root: Path


def test_a_financials_dataset_is_one_the_store_accepts(tmp_path: Path) -> None:
    """The delayed publication it declares is one its own timestamps carry."""
    loader, _ = financials_loader()
    (ref,) = loader.discover(financials_spec())
    written = write(Store(root=tmp_path), loader.load(ref, ref.span), ref=ref, source=loader.id)
    assert written.manifest.publication == "delayed"
    assert written.manifest.publication_rule == "fundamental"
    assert written.manifest.span == (date(2026, 3, 28), date(2026, 6, 27))
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
