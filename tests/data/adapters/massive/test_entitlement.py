"""Four conditions, one sentence: the probing that keeps them four.

The vendor answers a dataset the plan excludes, a range older than the plan's window, a
wrong prefix and an unknown key with the identical sentence, so every fixture here returns
that identical sentence. What separates the outcomes is the *second question* each probe
asks, and the tests assert on the requests as much as on the answers: an outcome reached
without the second question would be an outcome read off the prose.

The fixture vendor is a plan, not a script. It has a floor, an entitlement, a way of
presenting a refusal — a client error for some classes, an empty success for others — and
a choice of truncating or refusing a range that straddles its floor. Every measured
behaviour of the real source is one setting of those knobs, so a test says which source it
is imitating rather than which bytes it expects.

**A refusal and an empty page are different evidence, and the tests hold them apart.** A
refusal is the source declining to serve and is about the plan; an empty page is about the
window it was asked over. The sparse listings make the difference visible: statements are
quarterly and filings episodic, so a fourteen-day window of either holds nothing in the
ordinary case, and the identical request with the dates taken off holds a full page. A
suite whose fixtures answered that fortnight with rows could not see the difference, which
is exactly how two entitled listings came to be reported as `not_entitled`; the listing
fixture here answers as the source was measured answering, so the case bites.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from kanso.data.adapters.massive.client import MassiveClient, Response, Signal
from kanso.data.adapters.massive.entitlement import (
    BARS,
    BISECTED,
    CONTROLS,
    EARLIEST,
    FIRST_ROW,
    OPTION_CONTRACTS,
    OPTION_REFERENCE,
    PER_TICKER,
    PLAN_VERDICTS,
    PROBE_SPAN,
    QUOTES,
    REFERENCE,
    TRADES,
    UNMEASURED,
    Endpoint,
    Entitlements,
    Floor,
    Probe,
    Step,
    control_for,
    grain,
    history_floor,
    key,
    probe,
    raise_if_blocked,
    recent_window,
    settled_end,
    today_utc,
)
from kanso.data.adapters.massive.errors import (
    ERROR_FOR,
    NON_FATAL,
    BelowFloorError,
    EmptyResultError,
    MalformedRequestError,
    MassiveError,
    NotEntitledError,
    Outcome,
    TransportError,
)
from kanso.errors import Exit, ValidationError

from . import Answer, Replay, bar, definition, nothing, refused, rejected, served, window_of

TODAY = date(2026, 9, 4)
"""The day every probe here runs on. Frozen, because a rolling window makes a floor a
fact about a day and a test that drifted with the calendar would prove nothing."""

STOCK_FLOOR = date(2003, 9, 10)
ROLLING_FLOOR = TODAY - timedelta(days=730)

GENERIC_REFERENCE = "/v3/reference/tickers/"
"""Where the vendor keys everything but an option contract."""


def key_of(url: str) -> str:
    """The key a control lookup names: the last segment of its path."""
    return url.rsplit("/", 1)[-1]


def is_option(url: str) -> bool:
    """Whether a control lookup names an option key. The generic reference rejects one."""
    return key_of(url).startswith("O:")


@dataclass
class Vendor:
    """A plan the fixture serves under, in the terms the real source gates on.

    `looks` is how this source presents an absence — a client error for the classes whose
    refusals arrive as errors, an empty success for the index feeds that answer with no
    rows at all. `truncates` is whether a range straddling the floor comes back short and
    successful, which is the behaviour that makes a one-request floor probe possible.

    The two reference endpoints answer as the live source was measured answering, and
    that is a correction: this fixture used to answer the generic ticker reference for
    every key, an option's included. The real one rejects `O:…` outright with a client
    error and the option-contracts endpoint returns the contract, so under the old shape
    an option's genuine refusal came back `MALFORMED` — "the ticker or its asset-class
    prefix is wrong" about a ticker that was right — and no test in this file could see
    it. `reference` is the knob for whichever of the two a probe actually asks.
    """

    floor: date = STOCK_FLOOR
    entitled: bool = True
    looks: str = "refused"
    truncates: bool = True
    reference: str = "rows"
    reject_bars: bool = False
    holes: tuple[tuple[date, date], ...] = ()
    timestamps: bool = True
    asked: list[str] = field(default_factory=list)

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        self.asked.append(url)
        if OPTION_CONTRACTS in url:
            return self._reference(url)
        if GENERIC_REFERENCE in url:
            return rejected() if is_option(url) else self._reference(url)
        if self.reject_bars:
            return rejected()
        if not self.entitled:
            return self._absent()
        start, end = window_of(url)
        if end < self.floor:
            return self._absent()
        if start < self.floor and not self.truncates:
            return refused()
        first = max(start, self.floor)
        days = [first + timedelta(days=step) for step in range(3)]
        inside = [day for day in days if day <= end and not self._holed(day)]
        if not inside:
            return nothing()
        return served([bar(day) if self.timestamps else {"c": 1.0} for day in inside])

    def _reference(self, url: str) -> Response:
        """What a control lookup gets back, whichever reference endpoint asked it."""
        return {
            "rows": served([definition(key_of(url), source_feed="TestFeed")]),
            "empty": nothing(),
            "refused": refused(),
            "rejected": rejected(),
        }[self.reference]

    def _absent(self) -> Response:
        return refused() if self.looks == "refused" else nothing()

    def _holed(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.holes)


def reader(vendor: Vendor) -> tuple[MassiveClient, Replay]:
    replay = Replay(vendor)
    return MassiveClient("test-key-not-a-secret", transport=replay), replay


def reader_of(answer: Answer) -> tuple[MassiveClient, Replay]:
    """A client over any frozen vendor, for the fixtures that are not the `Vendor` plan."""
    replay = Replay(answer)
    return MassiveClient("test-key-not-a-secret", transport=replay), replay


# --- the outcomes, and that they are four --------------------------------------


def test_a_rejected_request_is_malformed_and_asks_nothing_further() -> None:
    """A rejected shape needs no second question: every later request would fail the same."""
    client, replay = reader(Vendor(reject_bars=True))

    found = probe(client, "AAPL", "stocks", as_of=TODAY)

    assert found.outcome is Outcome.MALFORMED
    assert len(replay.asked) == 1


def test_a_key_the_vendor_knows_and_the_plan_excludes_is_not_entitled() -> None:
    client, replay = reader(Vendor(entitled=False))

    found = probe(client, "O:AAPL260116C00150000", "options", as_of=TODAY)

    assert found.outcome is Outcome.NOT_ENTITLED
    assert [step.dataset for step in found.steps] == ["bars", "reference"]
    assert len(replay.asked) == 2


def test_an_index_feed_the_plan_excludes_refuses_and_is_not_entitled() -> None:
    """The measured index case, and a correction. `I:SPX` answers a recent window with a
    `403` and the vendor's sentence while `I:NDX` on the same endpoint and range answers
    with bars, so entitlement is per source feed and the refusal is what says so.

    This case used to assert that an *empty* answer for `I:SPX` was an entitlement
    failure, which the live source does not do and which is the reasoning the defect was
    built on: an empty page is what a quiet window looks like for every dataset the
    adapter serves, so reading one as a plan cannot be right for any of them.
    """
    client, _ = reader(Vendor(entitled=False, looks="refused"))

    found = probe(client, "I:SPX", "indices", as_of=TODAY)

    assert found.outcome is Outcome.NOT_ENTITLED
    assert found.grain == "ticker"
    assert "refused a recent window" in found.detail


def test_a_key_the_vendor_does_not_know_is_a_malformed_request_not_a_plan_problem() -> None:
    """Sending an operator to buy a plan they hold is the expensive wrong answer here."""
    client, _ = reader(Vendor(entitled=False, reference="empty"))

    found = probe(client, "O:NOTATICKER", "options", as_of=TODAY)

    assert found.outcome is Outcome.MALFORMED
    assert "does not recognise the key" in found.detail
    assert found.steps[1].path == f"{OPTION_CONTRACTS}/O:NOTATICKER"


def test_a_control_that_rejects_the_key_shape_is_also_a_malformed_request() -> None:
    client, _ = reader(Vendor(entitled=False, reference="rejected"))

    assert probe(client, "C:NOPE", "forex", as_of=TODAY).outcome is Outcome.MALFORMED


def test_a_control_that_is_itself_refused_leaves_the_plan_as_the_likelier_cause() -> None:
    """With the shape uncheckable, the answer says so rather than inventing certainty."""
    client, _ = reader(Vendor(entitled=False, reference="refused"))

    found = probe(client, "C:EURUSD", "forex", as_of=TODAY)

    assert found.outcome is Outcome.NOT_ENTITLED
    assert "could not be checked" in found.detail


def test_a_served_recent_window_with_nothing_asked_of_it_is_entitlement_alone() -> None:
    client, replay = reader(Vendor())

    found = probe(client, "AAPL", "stocks", as_of=TODAY)

    assert found.outcome is Outcome.OK
    assert found.floor is None
    assert len(replay.asked) == 1


def test_a_range_the_source_serves_is_ok() -> None:
    client, _ = reader(Vendor())

    found = probe(
        client, "AAPL", "stocks", window=(date(2024, 1, 2), date(2024, 3, 1)), as_of=TODAY
    )

    assert found.outcome is Outcome.OK


def test_a_range_older_than_the_source_is_the_floor_and_never_the_plan() -> None:
    """The single most expensive wrong answer this adapter can give, under test."""
    client, _ = reader(Vendor(floor=STOCK_FLOOR, looks="empty"))

    found = probe(
        client, "AAPL", "stocks", window=(date(1994, 1, 3), date(1995, 1, 3)), as_of=TODAY
    )

    assert found.outcome is Outcome.BELOW_FLOOR
    assert found.floor == STOCK_FLOOR
    assert str(STOCK_FLOOR) in found.detail


def test_the_same_range_under_a_source_that_refuses_instead_of_emptying_is_still_the_floor() -> (
    None
):
    """Options and forex present a below-floor range as a refusal; it is the same fact."""
    client, _ = reader(Vendor(floor=ROLLING_FLOOR, looks="refused"))

    found = probe(
        client, "C:EURUSD", "forex", window=(date(2021, 1, 4), date(2021, 6, 1)), as_of=TODAY
    )

    assert found.outcome is Outcome.BELOW_FLOOR
    assert found.floor == ROLLING_FLOOR


def test_an_entitled_range_above_the_floor_that_holds_nothing_is_empty() -> None:
    """Not a refusal, not a floor: the fourth outcome, and it needs the floor to be sure."""
    hole = (date(2010, 6, 1), date(2010, 7, 30))
    client, _ = reader(Vendor(holes=(hole,), looks="empty"))

    found = probe(
        client, "AAPL", "stocks", window=(date(2010, 6, 2), date(2010, 6, 20)), as_of=TODAY
    )

    assert found.outcome is Outcome.EMPTY
    assert found.floor == STOCK_FLOOR


def test_a_window_rejected_outright_is_malformed_even_on_an_entitled_series() -> None:
    def vendor(url: str, params: Mapping[str, str]) -> Response:
        if "1900-01-01" in url:
            return rejected()
        return served([bar(window_of(url)[0])])

    client, _ = reader(Vendor())
    client = MassiveClient("k", transport=Replay(vendor))

    found = probe(
        client, "AAPL", "stocks", window=(date(1900, 1, 1), date(1901, 1, 1)), as_of=TODAY
    )

    assert found.outcome is Outcome.MALFORMED


def test_a_refusal_of_a_range_above_the_measured_floor_is_the_plan_gating_the_range() -> None:
    """A contradiction the probe reports as what it is, rather than as a floor."""

    def vendor(url: str, params: Mapping[str, str]) -> Response:
        start, end = window_of(url)
        if start == date(2015, 1, 5):
            return refused()
        first = max(start, STOCK_FLOOR)
        return served([bar(first)])

    client = MassiveClient("k", transport=Replay(vendor))

    found = probe(
        client, "AAPL", "stocks", window=(date(2015, 1, 5), date(2015, 2, 5)), as_of=TODAY
    )

    assert found.outcome is Outcome.NOT_ENTITLED
    assert found.floor == STOCK_FLOOR
    assert "above its floor" in found.detail


def test_one_sentence_yields_four_different_outcomes() -> None:
    """The milestone's headline: identical prose, four answers, none of them read off it."""
    outcomes = {
        probe(reader(Vendor(entitled=False))[0], "O:X", "options", as_of=TODAY).outcome,
        probe(
            reader(Vendor(floor=ROLLING_FLOOR))[0],
            "C:EURUSD",
            "forex",
            window=(date(2015, 1, 5), date(2015, 2, 5)),
            as_of=TODAY,
        ).outcome,
        probe(
            reader(Vendor(holes=((date(2010, 1, 1), date(2011, 1, 1)),)))[0],
            "AAPL",
            "stocks",
            window=(date(2010, 2, 1), date(2010, 3, 1)),
            as_of=TODAY,
        ).outcome,
        probe(reader(Vendor(reject_bars=True))[0], "AAPL", "stocks", as_of=TODAY).outcome,
    }

    assert outcomes == {
        Outcome.NOT_ENTITLED,
        Outcome.BELOW_FLOOR,
        Outcome.EMPTY,
        Outcome.MALFORMED,
    }


def test_a_probe_that_cannot_reach_the_vendor_fails_rather_than_concluding() -> None:
    """No answer is not an answer: an outcome invented from a timeout would be a lie."""
    client = MassiveClient("k", transport=Replay(lambda url, params: Response(503, b"{}")))

    with pytest.raises(TransportError):
        probe(client, "AAPL", "stocks", as_of=TODAY)


# --- an empty page is about the window, a refusal is about the plan ------------

SPARSE = Endpoint(
    dataset="financials",
    template="/vX/reference/financials",
    params=(
        ("ticker", "{ticker}"),
        ("period_of_report_date.gte", "{start}"),
        ("period_of_report_date.lte", "{end}"),
        ("limit", "100"),
    ),
)
"""A quarterly listing, shaped as the source serves it: the key and the window are both
parameters, so the window can be taken off and the key stays. Filings are the same shape
over `filing_date`, and splits and dividends over their own dates."""


@dataclass
class Listing:
    """A sparse listing that answers a dated request and an undated one differently.

    Measured, and the reason this fixture exists: `/vX/reference/financials` and
    `/v1/reference/sec/filings` answer a fourteen-day window with `200` and no rows, and
    the identical request with the dates taken off with `200` and a full page. `/v3/
    reference/splits` does the same. A fixture answering the fortnight with rows encodes a
    hope — statements are quarterly and filings episodic, so a fortnight holds nothing in
    the ordinary case — and it is what let a probe ask the wrong question and look right.

    `dated` and `undated` are the two answers, and `reference` is the control endpoint's,
    so a test sets the three knobs the protocol actually turns on.
    """

    dated: str = "empty"
    undated: str = "rows"
    reference: str = "rows"
    asked: list[str] = field(default_factory=list)

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        self.asked.append(url)
        if GENERIC_REFERENCE in url:
            return self._answer(self.reference, definition(key_of(url)))
        windowed = any(name.endswith((".gte", ".lte")) for name in params)
        return self._answer(self.dated if windowed else self.undated, {"ticker": "AAPL"})

    def _answer(self, how: str, row: dict[str, Any]) -> Response:
        return {"rows": served([row]), "empty": nothing(), "refused": refused()}[how]


def test_a_sparse_listing_whose_fortnight_is_empty_is_never_an_entitlement_failure() -> None:
    """The defect, under test. Statements are quarterly, so a fourteen-day window of them
    holds nothing in the ordinary case; reporting that as a plan tells an operator to buy
    a subscription they already hold, which is the most expensive wrong answer here."""
    listing = Listing(dated="empty", undated="rows")
    client, _ = reader_of(listing)

    found = probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY)

    assert found.outcome is Outcome.OK
    assert found.outcome is not Outcome.NOT_ENTITLED
    assert raise_if_blocked(found) is None


def test_the_question_that_settles_it_is_the_same_one_with_the_dates_taken_off() -> None:
    """An empty page can only be told from a refusal by asking something wider, and the
    measured wider question is the identical request carrying no window at all."""
    listing = Listing(dated="empty", undated="rows")
    client, replay = reader_of(listing)

    probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY)

    assert len(replay.asked) == 2
    first, second = replay.asked
    assert any(name.endswith(".gte") for name in first.params)
    assert not any(name.endswith((".gte", ".lte")) for name in second.params)
    assert second.params["ticker"] == "AAPL"
    assert second.params["limit"] == "100"


def test_the_same_listing_refused_is_still_an_entitlement_failure() -> None:
    """The other half of the distinction: a `403` is evidence about the plan, and taking
    an empty page seriously must not cost the answer that a refusal is one."""
    client, _ = reader_of(Listing(dated="refused"))

    found = probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY)

    assert found.outcome is Outcome.NOT_ENTITLED
    with pytest.raises(NotEntitledError):
        raise_if_blocked(found)


def test_a_listing_that_is_refused_only_without_its_window_is_the_plan_too() -> None:
    """The refusal can arrive at either question; whichever it arrives at, it is one."""
    client, _ = reader_of(Listing(dated="empty", undated="refused"))

    assert probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY).outcome is (
        Outcome.NOT_ENTITLED
    )


def test_a_series_that_holds_nothing_at_any_date_is_empty_and_not_a_plan() -> None:
    """No window left to blame and no refusal anywhere: the source simply has no rows for
    this key. That is the fourth outcome, and calling it the first is the defect."""
    client, _ = reader_of(Listing(dated="empty", undated="empty", reference="rows"))

    found = probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY)

    assert found.outcome is Outcome.EMPTY
    with pytest.raises(EmptyResultError):
        raise_if_blocked(found)


def test_an_uncheckable_key_that_was_never_refused_is_still_not_a_plan() -> None:
    """The control is refused, so the key shape cannot be checked — but the series itself
    was never refused, and inventing an entitlement failure out of that is the same error
    in a smaller place."""
    client, _ = reader_of(Listing(dated="empty", undated="empty", reference="refused"))

    assert probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY).outcome is Outcome.EMPTY


def test_nothing_anywhere_for_a_key_the_vendor_lacks_is_a_malformed_request() -> None:
    client, _ = reader_of(Listing(dated="empty", undated="empty", reference="empty"))

    found = probe(client, "NOTATICKER", "stocks", dataset=SPARSE, as_of=TODAY)

    assert found.outcome is Outcome.MALFORMED
    assert "does not recognise the key" in found.detail


def test_a_listing_that_rejects_the_undated_shape_is_a_malformed_request() -> None:
    def vendor(url: str, params: Mapping[str, str]) -> Response:
        if any(name.endswith(".gte") for name in params):
            return nothing()
        return rejected()

    client, _ = reader_of(vendor)

    assert probe(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY).outcome is (
        Outcome.MALFORMED
    )


def test_a_question_that_carried_no_window_is_not_asked_a_second_time() -> None:
    """A survey asks a listing with the dates already off. There is nothing wider left to
    ask, so the probe goes straight to the control rather than repeating itself."""
    client, replay = reader_of(Listing(dated="empty", undated="empty", reference="rows"))

    found = probe(client, "AAPL", "stocks", dataset=SPARSE.unwindowed(), as_of=TODAY)

    assert found.outcome is Outcome.EMPTY
    assert [step.dataset for step in found.steps] == ["financials", "reference"]
    assert len(replay.asked) == 2


def test_a_price_series_whose_fortnight_is_quiet_is_asked_the_whole_of_history() -> None:
    """The same rule for a continuous series. A delisted issuer's last fortnight holds no
    bars, and that is a fact about the issuer; the plan is what serves the history."""
    quiet = (TODAY - timedelta(days=40), TODAY)

    client, replay = reader(Vendor(floor=STOCK_FLOOR, holes=(quiet,), looks="empty"))

    found = probe(client, "AAPL", "stocks", as_of=TODAY)

    assert found.outcome is Outcome.OK
    assert window_of(replay.paths[1]) == (EARLIEST, settled_end(TODAY))


def test_the_whole_of_history_is_asked_for_once_and_the_floor_reads_it_off() -> None:
    """Establishing entitlement and measuring a floor want the identical request, so the
    answer is handed on rather than paid for twice."""
    quiet = (TODAY - timedelta(days=40), TODAY)
    client, replay = reader(Vendor(floor=STOCK_FLOOR, holes=(quiet,), looks="empty"))

    found = probe(
        client,
        "AAPL",
        "stocks",
        window=(date(1994, 1, 3), date(1995, 1, 3)),
        as_of=TODAY,
        earliest=EARLIEST,
    )

    assert found.outcome is Outcome.BELOW_FLOOR
    assert found.floor == STOCK_FLOOR
    straddles = [path for path in replay.paths if window_of(path) == (EARLIEST, settled_end(TODAY))]
    assert len(straddles) == 1


def test_a_floor_is_measured_for_a_series_whose_recent_fortnight_is_quiet() -> None:
    """A floor is only meaningless for a series the plan excludes. One that is simply
    quiet this fortnight has a floor like any other, and refusing to measure it would be
    the same conflation wearing a different hat."""
    quiet = (TODAY - timedelta(days=40), TODAY)
    client, _ = reader(Vendor(floor=STOCK_FLOOR, holes=(quiet,), looks="empty"))

    assert history_floor(client, "AAPL", "stocks", as_of=TODAY).floor == STOCK_FLOOR


def test_a_floor_asked_for_on_an_empty_series_fails_as_empty_and_not_as_a_plan() -> None:
    client, _ = reader_of(Listing(dated="empty", undated="empty", reference="rows"))

    with pytest.raises(EmptyResultError, match="no history floor"):
        history_floor(client, "AAPL", "stocks", dataset=SPARSE, as_of=TODAY)


def test_a_refusal_of_the_epoch_is_about_the_range_and_never_about_the_plan() -> None:
    """A quiet fortnight plus a source that refuses rather than truncates.

    The recent window answered `200` and no rows, and the request refused was the identical
    question dated back to the epoch — a start below every plan's history window. Measured,
    a series the plan excludes is refused at *every* date, this fortnight included, so a
    refusal that spares the fortnight cannot be the subscription. Reporting it as one sends
    an operator to buy forex bars they already hold, and the remedy printed beside it says
    in terms that this is not a history-floor failure, which is precisely what it is.
    """
    quiet = (TODAY - timedelta(days=40), TODAY)
    client, _ = reader(Vendor(floor=ROLLING_FLOOR, truncates=False, holes=(quiet,)))

    found = probe(client, "C:EURUSD", "forex", as_of=TODAY)

    assert found.outcome is Outcome.OK
    assert found.outcome is not Outcome.NOT_ENTITLED
    assert "was not refused" in found.detail
    assert "refused a recent window" not in found.detail
    assert [(step.status, str(step.signal)) for step in found.steps] == [
        (200, "no_rows"),
        (403, "refused"),
    ]


def test_a_series_with_no_start_date_known_to_serve_has_no_floor_to_bisect() -> None:
    """The bracket a halving needs is a start date known to serve, and there is none here.

    The predicate is not even monotonic: this series answers an old start with rows and a
    recent one with none, so halving converges on whichever end it began at — a floor of a
    fortnight ago on a series with two years behind it, which a backfill then clamps to and
    reports as a success. So nothing is halved and nothing is invented.
    """
    quiet = (TODAY - timedelta(days=40), TODAY)
    client, replay = reader(Vendor(floor=ROLLING_FLOOR, truncates=False, holes=(quiet,)))

    found = history_floor(client, "C:EURUSD", "forex", as_of=TODAY)

    assert (found.method, found.floor) == (UNMEASURED, EARLIEST)
    assert found.floor < ROLLING_FLOOR, "a floor that clamps nothing, not one a fortnight old"
    assert len(replay.asked) == 2, "the recent window and the straddle, and no search"


def test_taking_the_window_off_leaves_the_key_and_the_page_size_alone() -> None:
    wide = SPARSE.unwindowed()

    assert wide.request("AAPL") == (
        "/vX/reference/financials",
        {"ticker": "AAPL", "limit": "100"},
    )
    assert wide.dataset == SPARSE.dataset


def test_an_endpoint_whose_range_is_in_its_address_has_no_undated_form() -> None:
    """A range that is part of the path cannot be left off, so the widest question such an
    endpoint admits is the whole of history rather than no window at all."""
    assert BARS.dated_path is True
    assert SPARSE.dated_path is False
    assert BARS.unwindowed() == BARS


# --- the control question, asked where the vendor keeps the key ----------------


def test_an_option_key_is_checked_against_the_contracts_endpoint() -> None:
    """Measured: `/v3/reference/tickers/O:…` is a client error and
    `/v3/reference/options/contracts/O:…` returns the contract. Asking the generic one
    answers "unrecognised" about a contract the vendor defines perfectly well."""
    client, replay = reader(Vendor(entitled=False))

    found = probe(client, "O:AAPL260909C00205000", "options", as_of=TODAY)

    assert found.steps[1].path == f"{OPTION_CONTRACTS}/O:AAPL260909C00205000"
    assert found.outcome is Outcome.NOT_ENTITLED
    assert len(replay.asked) == 2


def test_option_ticks_the_plan_excludes_are_the_plan_and_never_the_prefix() -> None:
    """The wrong answer the whole design exists to prevent. This plan really does exclude
    option ticks, at every date; reporting that as a bad ticker sends an operator to fix
    a key that is already correct, and leaves the subscription they lack unbought."""
    client, _ = reader(Vendor(entitled=False))

    found = probe(client, "O:AAPL260909C00205000", "options", dataset=TRADES, as_of=TODAY)

    assert found.outcome is Outcome.NOT_ENTITLED
    assert "prefix" not in found.detail
    with pytest.raises(NotEntitledError):
        raise_if_blocked(found)


@pytest.mark.parametrize(
    ("ticker", "asset_class"),
    [("AAPL", "stocks"), ("C:EURUSD", "forex"), ("I:SPX", "indices"), ("ESZ6", "futures")],
)
def test_every_other_class_is_checked_against_the_generic_reference(
    ticker: str, asset_class: str
) -> None:
    """`AAPL` and `C:EURUSD` were both measured answering there; only options are keyed
    elsewhere, and moving the rest with them would break four classes to fix one."""
    client, _ = reader(Vendor(entitled=False))

    found = probe(client, ticker, asset_class, as_of=TODAY)

    assert found.steps[1].path == f"{GENERIC_REFERENCE}{ticker}"
    assert found.outcome is Outcome.NOT_ENTITLED


def test_the_control_endpoint_is_chosen_by_asset_class() -> None:
    assert control_for("options") is OPTION_REFERENCE
    assert control_for("stocks") is REFERENCE
    assert control_for("indices") is REFERENCE
    assert set(CONTROLS) == {"options"}
    assert OPTION_REFERENCE.request("O:X") == (f"{OPTION_CONTRACTS}/O:X", {})


def test_a_control_named_explicitly_wins_over_the_class_default() -> None:
    """The injection seam, and a demonstration of what it costs to point it wrong: the
    generic reference asked about an option key produces exactly the defect."""
    client, _ = reader(Vendor(entitled=False))

    found = probe(client, "O:X", "options", control=REFERENCE, as_of=TODAY)

    assert found.steps[1].path == f"{GENERIC_REFERENCE}O:X"
    assert found.outcome is Outcome.MALFORMED


# --- the floor, measured -------------------------------------------------------


def test_a_straddling_request_names_the_floor_in_one_request() -> None:
    """A range beginning below the floor comes back beginning at it, so the first row is it."""
    client, replay = reader(Vendor(floor=STOCK_FLOOR))

    found = history_floor(client, "AAPL", "stocks", as_of=TODAY)

    assert (found.floor, found.method) == (STOCK_FLOOR, FIRST_ROW)
    assert len(replay.asked) == 2
    assert found.probed_on == TODAY


def test_a_source_that_refuses_a_straddling_range_is_bisected_instead() -> None:
    client, replay = reader(Vendor(floor=ROLLING_FLOOR, truncates=False))

    found = history_floor(client, "C:EURUSD", "forex", as_of=TODAY)

    assert found.method == BISECTED
    assert abs((found.floor - ROLLING_FLOOR).days) <= 1
    assert len(replay.asked) < 20


def test_rows_without_a_usable_timestamp_leave_the_floor_unmeasured() -> None:
    """A straddling request that *served* draws no boundary, so there is none to search for.

    Bisecting anyway searches for a refusal that cannot happen and converges on whichever
    end it began at. The honest answer is that no floor was measured, reported at the epoch
    so it clamps nothing and a loader records the span the source actually served.
    """
    client, replay = reader(Vendor(floor=ROLLING_FLOOR, timestamps=False))

    found = history_floor(client, "AAPL", "stocks", as_of=TODAY)

    assert (found.method, found.floor) == (UNMEASURED, EARLIEST)
    assert len(replay.asked) == 2, "the recent window and the straddle, and nothing halved"


def test_a_series_the_plan_excludes_has_no_floor_to_measure() -> None:
    """A floor for something never served would be an entitlement failure in disguise."""
    client, _ = reader(Vendor(entitled=False))

    with pytest.raises(NotEntitledError) as caught:
        history_floor(client, "O:X", "options", as_of=TODAY)

    assert caught.value.code == Exit.PRECONDITION
    assert caught.value.fatal is False


def test_a_floor_is_a_fact_about_the_day_it_was_probed() -> None:
    found = Floor("forex", "bars", "C:EURUSD", ROLLING_FLOOR, TODAY, FIRST_ROW)

    assert found.stale(TODAY) is False
    assert found.stale(TODAY + timedelta(days=1)) is False
    assert found.stale(TODAY + timedelta(days=2)) is True
    assert found.payload()["method"] == FIRST_ROW


def test_the_search_starts_at_the_epoch_and_ends_at_the_last_settled_day() -> None:
    client, replay = reader(Vendor())

    history_floor(client, "AAPL", "stocks", as_of=TODAY)

    assert window_of(replay.paths[0]) == recent_window(TODAY)
    assert window_of(replay.paths[1]) == (EARLIEST, settled_end(TODAY))


# --- the grain the source gates on ---------------------------------------------


def test_indices_are_probed_and_cached_per_ticker() -> None:
    """One index serves bars and another does not; a class-wide answer would drop the
    entitled one on the floor."""
    served_tickers = {"I:NDX"}

    def vendor(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/tickers/" in url:
            return served([definition("I:SPX", source_feed="CboeGlobalIndicesMain")])
        if any(ticker in url for ticker in served_tickers):
            return served([bar(window_of(url)[0])])
        return nothing()

    client = MassiveClient("k", transport=Replay(vendor))
    entitlements = Entitlements(client, as_of=TODAY)

    assert entitlements.check("I:NDX", "indices").ok is True
    assert entitlements.check("I:SPX", "indices").ok is False
    assert len(entitlements.probes()) == 2


def test_a_class_the_source_gates_as_a_whole_is_asked_once() -> None:
    client, replay = reader(Vendor())
    entitlements = Entitlements(client, as_of=TODAY)

    entitlements.check("AAPL", "stocks")
    reused = entitlements.check("MSFT", "stocks")

    assert len(replay.asked) == 1
    assert len(entitlements.probes()) == 1
    assert reused.ticker == "MSFT"


def test_the_grain_is_named_on_every_answer() -> None:
    assert grain("indices") == "ticker"
    assert grain("stocks") == "endpoint"
    assert key("indices", "bars", "I:SPX") == "indices:bars:I:SPX"
    assert key("stocks", "bars", "AAPL") == "stocks:bars"
    assert set(PER_TICKER) == {"indices"}


def test_ticks_are_probed_on_their_own_endpoint() -> None:
    """A class whose aggregates are included and whose ticks are not is one class with
    two answers."""

    def vendor(url: str, params: Mapping[str, str]) -> Response:
        if OPTION_CONTRACTS in url:
            return served([definition("O:X")])
        if url.startswith("https://api.massive.com/v3/trades"):
            return refused()
        return served([bar(window_of(url)[0])])

    client = MassiveClient("k", transport=Replay(vendor))
    entitlements = Entitlements(client, as_of=TODAY)

    assert entitlements.check("O:X", "options", dataset=BARS).ok is True
    assert entitlements.check("O:X", "options", dataset=TRADES).ok is False


# --- the memo ------------------------------------------------------------------


def test_a_range_is_judged_against_the_cached_floor_rather_than_by_asking_again() -> None:
    """A backfill walks years in chunks; re-probing each chunk would double the traffic."""
    client, replay = reader(Vendor(floor=STOCK_FLOOR))
    entitlements = Entitlements(client, as_of=TODAY)

    below = entitlements.check("AAPL", "stocks", window=(date(1994, 1, 3), date(1995, 1, 3)))
    above = entitlements.check("AAPL", "stocks", window=(date(2010, 1, 4), date(2010, 2, 4)))
    again = entitlements.check("AAPL", "stocks", window=(date(2011, 1, 4), date(2011, 2, 4)))

    assert below.outcome is Outcome.BELOW_FLOOR
    assert below.floor == STOCK_FLOOR
    assert above.ok is True and again.ok is True
    assert len(replay.asked) == 2, "the recent window once, the straddle once, and no repeats"
    assert len(entitlements.floors()) == 1
    assert entitlements.requests == 2


def test_a_series_the_plan_excludes_is_answered_without_measuring_a_floor() -> None:
    client, replay = reader(Vendor(entitled=False))
    entitlements = Entitlements(client, as_of=TODAY)

    found = entitlements.check("O:X", "options", window=(date(2024, 1, 2), date(2024, 2, 2)))

    assert found.outcome is Outcome.NOT_ENTITLED
    assert entitlements.floors() == ()
    assert len(replay.asked) == 2


def quiet_key(silent: str) -> Answer:
    """A source that serves every key but one, and holds nothing at all for that one.

    The shape of a name the vendor recognises and carries nothing for: a contract that
    never traded, a listing from yesterday, a ticker that was delisted. Ordinary in a
    universe of any size, and the case a memo must not let stand for the class.
    """

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if GENERIC_REFERENCE in url:
            return served([definition(key_of(url))])
        if f"/{silent}/" in url:
            return nothing()
        return served([bar(window_of(url)[1])])

    return answer


def test_a_key_that_holds_nothing_never_answers_for_another_key() -> None:
    """The reported defect one level up, and the survey's own warning made true in code.

    "This key holds nothing" is a fact about that key. Letting it stand for the class makes
    the first silent ticker in a spec the verdict for every ticker behind it — reported, in
    the coarse classes, without the vendor being asked about any of them.
    """
    client, replay = reader_of(quiet_key("HALTED"))
    entitlements = Entitlements(client, as_of=TODAY)

    silent = entitlements.check("HALTED", "stocks")
    served_key = entitlements.check("AAPL", "stocks")

    assert silent.outcome is Outcome.EMPTY
    assert served_key.ok is True
    assert any("AAPL" in item.url for item in replay.asked), "AAPL was actually asked about"


def test_a_key_the_plan_covers_still_answers_for_the_next_key_of_its_class() -> None:
    """The saving the coarse grain buys, kept: a plan is a fact about a class of keys.

    Measured, the source gates these classes per endpoint — option ticks are refused at
    every date and for every contract — so one answer is the class's answer, and probing a
    universe name by name would spend a request per name to learn the same thing.
    """
    client, replay = reader(Vendor(entitled=False))
    entitlements = Entitlements(client, as_of=TODAY)

    first = entitlements.check("O:AAPL260116C00150000", "options")
    second = entitlements.check("O:MSFT260116C00400000", "options")

    assert (first.outcome, second.outcome) == (Outcome.NOT_ENTITLED, Outcome.NOT_ENTITLED)
    assert second.ticker == "O:MSFT260116C00400000"
    assert len(replay.asked) == 2, "the plan was established once and answered twice"
    assert set(PLAN_VERDICTS) == {Outcome.OK, Outcome.NOT_ENTITLED}


def test_an_outcome_the_floor_pass_is_first_to_find_comes_back_as_an_answer() -> None:
    """A plan reused from another key is checked against this one when its floor is measured.

    That measurement asks about *this* key and can be the first to learn the plan does not
    cover it. It is an outcome like any other, and a caller written to skip a non-fatal one
    cannot skip an exception thrown past its branch: a walk over a universe would abort on
    the one name, discarding every name the source serves perfectly.
    """

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if GENERIC_REFERENCE in url:
            return served([definition(key_of(url))])
        if "/WEIRD/" in url:
            return refused()
        start, end = window_of(url)
        return served([bar(max(start, STOCK_FLOOR))])

    client, _ = reader_of(answer)
    entitlements = Entitlements(client, as_of=TODAY)
    window = (date(2025, 1, 2), date(2026, 1, 2))

    served_first = entitlements.check("AAPL", "stocks", window=window)
    odd = entitlements.check("WEIRD", "stocks", window=window)
    served_after = entitlements.check("MSFT", "stocks", window=window)

    assert served_first.ok is True
    assert odd.outcome is Outcome.NOT_ENTITLED
    assert odd.ticker == "WEIRD"
    assert served_after.ok is True, "the name after the odd one is still discovered"


def test_a_memo_asked_for_the_floor_of_an_excluded_series_still_refuses_to_invent_one() -> None:
    """`check` folds the outcome into its answer; `floor` returns a floor or it fails."""
    client, _ = reader(Vendor(entitled=False))
    entitlements = Entitlements(client, as_of=TODAY)

    with pytest.raises(NotEntitledError, match="no history floor"):
        entitlements.floor("O:X", "options")


def test_the_day_is_fixed_for_the_life_of_one_run() -> None:
    """A rolling window moves the floor at midnight; one backfill sees one set of floors."""
    client, _ = reader(Vendor())

    assert Entitlements(client).as_of == today_utc()
    assert Entitlements(client, as_of=TODAY).as_of == TODAY


# --- turning an outcome into a failure -----------------------------------------


def test_an_answer_that_is_ok_raises_nothing() -> None:
    assert (
        raise_if_blocked(Probe(Outcome.OK, "bars", "stocks", "AAPL", "endpoint", TODAY, "")) is None
    )


@pytest.mark.parametrize(
    ("outcome", "expected", "code"),
    [
        (Outcome.NOT_ENTITLED, NotEntitledError, Exit.PRECONDITION),
        (Outcome.BELOW_FLOOR, BelowFloorError, Exit.PRECONDITION),
        (Outcome.EMPTY, EmptyResultError, Exit.PRECONDITION),
        (Outcome.MALFORMED, MalformedRequestError, Exit.VALIDATION),
    ],
)
def test_each_outcome_fails_as_its_own_thing_to_do(
    outcome: Outcome, expected: type[MassiveError], code: Exit
) -> None:
    """The operator agent branches on the code, so the four must not collapse into one."""
    found = Probe(outcome, "bars", "forex", "C:EURUSD", "endpoint", TODAY, "detail", ROLLING_FLOOR)

    with pytest.raises(expected) as caught:
        raise_if_blocked(found)

    assert caught.value.code == code
    assert ERROR_FOR[outcome] is expected
    assert caught.value.remedy


def test_a_floor_failure_names_the_floor_to_clamp_to() -> None:
    found = Probe(
        Outcome.BELOW_FLOOR, "bars", "forex", "C:EURUSD", "endpoint", TODAY, "d", ROLLING_FLOOR
    )

    with pytest.raises(BelowFloorError) as caught:
        raise_if_blocked(found)

    assert caught.value.floor == ROLLING_FLOOR
    assert str(ROLLING_FLOOR) in str(caught.value)


def test_the_three_a_history_walk_survives_are_the_three_about_one_series() -> None:
    """What a walk survives is exactly what is true of one name and not of the next.

    A plan that excludes the series, a source younger than the range, and a key the source
    holds nothing for all end one dataset. A malformed request does not: it is a statement
    about a request shape, and every name behind it would be asked the same broken way.
    """
    assert set(NON_FATAL) == {Outcome.NOT_ENTITLED, Outcome.BELOW_FLOOR, Outcome.EMPTY}
    assert NotEntitledError("x").fatal is False
    assert BelowFloorError("x").fatal is False
    assert EmptyResultError("x").fatal is False
    assert MalformedRequestError("x").fatal is True


def test_a_transport_failure_carries_no_outcome_because_it_established_none() -> None:
    assert TransportError("x").outcome is None
    assert TransportError("x", status=503).status == 503
    assert TransportError("x").code == Exit.ERROR


# --- the endpoint as a value ---------------------------------------------------


def test_an_aggregate_endpoint_puts_the_range_in_the_path() -> None:
    path, params = BARS.request("AAPL", (date(2024, 1, 2), date(2024, 3, 1)))

    assert path == "/v2/aggs/ticker/AAPL/range/1/day/2024-01-02/2024-03-01"
    assert params == {"adjusted": "false", "sort": "asc"}
    assert "limit" not in params


def test_a_tick_endpoint_puts_the_range_in_the_parameters() -> None:
    path, params = QUOTES.request("O:X", (date(2024, 1, 2), date(2024, 1, 3)))

    assert path == "/v3/quotes/O:X"
    assert params["timestamp.gte"] == "2024-01-02"
    assert params["timestamp.lte"] == "2024-01-03"


def test_the_control_endpoint_needs_no_range_at_all() -> None:
    """Reference has no history window, so its answer is about the key and nothing else."""
    assert REFERENCE.request("I:NDX") == ("/v3/reference/tickers/I:NDX", {})


def test_an_endpoint_asked_for_a_range_without_one_refuses_rather_than_guesses() -> None:
    with pytest.raises(MalformedRequestError, match="needs a date window|date window"):
        BARS.request("AAPL")


MARKET_WIDE = Endpoint(
    dataset="splits",
    template="/v3/reference/splits",
    params=(("execution_date.gte", "{start}"), ("execution_date.lte", "{end}")),
)
"""A listing asked of the whole market: the ticker filter is dropped, so nothing in it
names a ticker at all. Dropping it is what makes an empty answer evidence about the plan
rather than about one issuer, so this shape has to be askable."""


def test_a_listing_that_names_no_ticker_is_asked_without_one() -> None:
    """A market-wide probe has no ticker by definition. This shape used to raise
    `TypeError: replace() argument 2 must be str, not None` out of the substitution."""
    path, params = MARKET_WIDE.request(None, (date(2024, 1, 2), date(2024, 1, 16)))

    assert path == "/v3/reference/splits"
    assert params == {"execution_date.gte": "2024-01-02", "execution_date.lte": "2024-01-16"}


def test_an_endpoint_that_names_a_ticker_refuses_a_request_without_one() -> None:
    """A kanso error saying what is missing, not a `TypeError` from inside a substitution:
    a probe raising one would reach an operator as a crash, and a walk that catches the
    adapter's own failures would not catch it at all."""
    with pytest.raises(MalformedRequestError, match="names a ticker") as caught:
        BARS.request(None, (date(2024, 1, 2), date(2024, 1, 16)))

    assert caught.value.code == Exit.VALIDATION
    assert caught.value.remedy


def test_a_ticker_named_only_in_a_parameter_is_refused_the_same_way() -> None:
    """The filtered listing: its path names no ticker and its parameters do."""
    filtered = Endpoint(
        dataset="splits", template="/v3/reference/splits", params=(("ticker", "{ticker}"),)
    )

    with pytest.raises(MalformedRequestError, match="names a ticker"):
        filtered.request(None)


@pytest.mark.parametrize(
    ("endpoint", "row", "expected"),
    [
        (BARS, {"t": 1_078_704_000_000}, date(2004, 3, 8)),
        (TRADES, {"sip_timestamp": 1_078_704_000_000_000_000}, date(2004, 3, 8)),
        (BARS, {"t": 1_078_704_000_000.0}, date(2004, 3, 8)),
        (BARS, {"t": True}, None),
        (BARS, {"t": "2004-03-08"}, None),
        (BARS, {}, None),
    ],
)
def test_a_row_s_day_is_read_from_the_epoch_the_endpoint_uses(
    endpoint: Endpoint, row: dict[str, Any], expected: date | None
) -> None:
    """Aggregates are timed in milliseconds and ticks in nanoseconds; both are read here."""
    assert endpoint.day(row) == expected


def test_an_endpoint_declared_with_an_epoch_nobody_uses_is_refused() -> None:
    with pytest.raises(ValidationError, match="not one of"):
        Endpoint("bars", "/x/{ticker}", timestamp_unit="fortnights")


# --- the evidence --------------------------------------------------------------


def test_a_probe_records_every_request_it_made() -> None:
    """An operator reading a refusal sees the questions asked, not a sentence."""
    client, _ = reader(Vendor(entitled=False))

    payload = probe(client, "O:X", "options", as_of=TODAY).payload()

    steps = payload["steps"]
    assert isinstance(steps, list)
    assert [step["dataset"] for step in steps] == ["bars", "reference"]
    assert steps[0]["window"] is not None
    assert steps[1]["window"] is None
    assert payload["floor"] is None
    assert payload["grain"] == "endpoint"


def test_a_request_that_carried_no_dates_records_no_window_and_claims_none() -> None:
    """The evidence must be evidence of what happened, and so must the sentence beside it.

    A listing asked market-wide with the dates off carried no window at all. A step
    stamping the fortnight on it, and a detail calling it "a recent window", together tell
    an operator their key returns current quarterly statements when all that was
    established is that the vendor holds some statement for some issuer at some date.
    """
    client, _ = reader_of(Listing(dated="empty", undated="rows"))

    found = probe(client, "AAPL", "stocks", dataset=SPARSE.unwindowed(), as_of=TODAY)

    assert found.ok is True
    assert [step.window for step in found.steps] == [None]
    assert "recent window" not in found.detail
    assert "no date window" in found.detail


def test_a_step_is_a_plain_record() -> None:
    step = Step("bars", "/v2/x", (date(2024, 1, 1), date(2024, 2, 1)), 200, Signal.ROWS, 3)

    assert step.payload() == {
        "dataset": "bars",
        "path": "/v2/x",
        "window": ["2024-01-01", "2024-02-01"],
        "status": 200,
        "signal": "rows",
        "rows": 3,
    }


def test_the_recent_window_is_wide_enough_to_contain_trading_days() -> None:
    """A window of one day over a weekend would read as an absence of entitlement."""
    start, end = recent_window(TODAY)

    assert end == TODAY - timedelta(days=1)
    assert end - start == PROBE_SPAN


def test_the_loaders_package_holds_no_table_of_its_own() -> None:
    """A loader is reached by id through the registry, never by importing a module."""
    from kanso.data.adapters.massive import loaders

    assert not hasattr(loaders, "LOADERS")
