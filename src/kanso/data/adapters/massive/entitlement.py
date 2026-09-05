"""Telling four conditions apart when the vendor states them with one sentence.

Massive answers "this data isn't included in your current plan" to a dataset the plan
excludes, to a range older than the plan's history window, to a ticker carrying the wrong
asset-class prefix and to a key shape it does not recognise. The sentence is byte
identical in all four cases, so this module never reads it. It establishes meaning the
only way that is sound: by asking a second question whose answer separates the cases.

**A refusal is evidence about the plan; an empty page is evidence about the window.** The
two are never read as the same thing, and collapsing them is how this module fails: a
quarterly statement and an episodic filing hold nothing in an ordinary fortnight, so a
recent window that comes back empty says the fortnight was quiet and says nothing whatever
about the subscription. Measured: the statements and filings listings answer a fourteen-day
window with an empty page and the identical request with the dates taken off with a full
one, while a series the plan really excludes is refused outright at every date.

**The protocol.** One question is asked of a *recent* window, where a plan's rolling
history window cannot be the reason for a refusal, and one of a *control* endpoint that
is not gated the same way, which says whether the vendor recognises the key at all. An
empty page earns a third question — the same one asked as widely as the endpoint admits —
before it is allowed to mean anything.

* rows for a recent window — the plan includes the series. If a range was asked for and
  it too serves rows, the answer is `OK`; otherwise the floor decides, below.
* a refusal for a recent window, and the control recognises the key — the key is real and
  the plan excludes it: `NOT_ENTITLED`.
* a refusal for a recent window, and the control does not recognise the key — the request
  itself is wrong: `MALFORMED`.
* no rows for a recent window, and the widest question the endpoint admits serves rows —
  the plan includes the series and the window is what was empty.
* no rows for a recent window and none for the widest question either — the source never
  refused, so this is a series that holds nothing (`EMPTY`) or a key the vendor does not
  carry (`MALFORMED`), and the control says which.
* no rows for a recent window and a refusal of the same question dated back to the epoch —
  the plan includes the series and the *range* is what was refused. A series the plan
  excludes is refused at every date, this fortnight included; this one was not.
* the request shape rejected outright — `MALFORMED`, with no second question needed.

**Which answers carry to the next key.** An answer about a plan carries — that is what
makes a universe cost one probe rather than one per name — and an answer about a key does
not. "This key holds nothing" and "this key does not exist" are established for every name
that asks, because reusing one of them makes the first silent ticker in a spec the verdict
for every ticker behind it, reported without the vendor being asked about any of them.

**The control question is asked where the vendor keeps the key.** Whether a key is one
the vendor recognises can only be asked of the endpoint that holds keys of its kind.
Option contracts are not in the generic ticker reference — it rejects an option key
outright, with a client error — so asking it about one answers "unrecognised" for a
contract the vendor defines perfectly well, and a plan that genuinely excludes option
ticks is then reported as a wrong prefix, sending an operator to correct a ticker that
was already right. The control endpoint is therefore chosen by asset class, the way the
source itself gates; every other class this adapter serves is keyed in the generic
reference, prefixed forex and index keys among them.

**The floor is a measurement, not a constant.** Where a recent window serves rows and the
requested range does not, the difference is the source's history floor, so the floor is
probed and compared: a range ending before it is `BELOW_FLOOR`, and a range above it that
still holds nothing is `EMPTY`. The floor is found from the data itself — an ascending
request spanning all of history returns rows beginning at the floor, so its first row
*is* the floor, at the cost of one request — and by bisection on the start date where the
source refuses such a range instead of truncating it. A bisection needs a start date known
to serve, so where none is known the floor is reported unmeasured rather than searched for:
an invented floor of a fortnight ago silently truncates a backfill to a fortnight and calls
it a success. Nothing here assumes a floor per class: two classes share a floor only by
coincidence, and where a plan's window rolls, today's floor is not yesterday's, which is
why a `Floor` records the day it was probed.

**The grain is the source's, not the taxonomy's.** Entitlement is decided per ticker and
cached at the grain the source actually gates on. For index tickers that grain is the
source feed behind the ticker, not the asset class: one index returns bars and another
does not, on the same endpoint over the same range, so an answer for one index says
nothing about another. Two shortcuts are therefore refused here. A feed allowlist is
never consulted — some feeds are entitled in part, and a filter on the feed name silently
drops entitled tickers. A universe is never enumerated from one page, because the index
universe runs to several thousand tickers behind a cursor. One ticker of a class is probed
and its *plan* answer serves the rest, which is the saving the coarse grain buys; a class
the source gates per ticker buys none of it and is probed per ticker.

A silently truncated range is not this module's to catch. A range straddling the floor is
answered with a success status and a short series, and no probe can see that: the loader
compares what was served against what was asked and records the served span.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from kanso.data.adapters.massive.client import Call, MassiveClient, Signal
from kanso.data.adapters.massive.errors import (
    ERROR_FOR,
    BelowFloorError,
    EmptyResultError,
    MalformedRequestError,
    MassiveError,
    NotEntitledError,
    Outcome,
)
from kanso.errors import ValidationError

__all__ = [
    "BARS",
    "CONTROLS",
    "EARLIEST",
    "OPTION_CONTRACTS",
    "OPTION_REFERENCE",
    "PER_TICKER",
    "PLAN_VERDICTS",
    "PROBE_SPAN",
    "QUOTES",
    "REFERENCE",
    "TRADES",
    "Endpoint",
    "Entitlements",
    "Floor",
    "Probe",
    "Step",
    "control_for",
    "grain",
    "history_floor",
    "key",
    "probe",
    "raise_if_blocked",
    "recent_window",
    "today_utc",
]

PROBE_SPAN: Final = timedelta(days=14)
"""How wide a probe's recent window is.

Two weeks always contains trading days, whatever the holidays, so a *refusal* of it is
about the plan rather than the calendar. An empty answer to it is not, and never was: two
weeks of a quarterly statement or an episodic filing hold nothing in the ordinary case,
which is why an empty page is put to a wider question before it is allowed to mean
anything."""

SETTLE: Final = timedelta(days=1)
"""A probe never asks about today: a session in progress has no complete day bar, and a
window that can only ever be empty is a window that establishes nothing."""

EARLIEST: Final = date(1970, 1, 1)
"""Where the search for a floor starts. No vendor serves anything older, and the epoch is
the one date that needs no justification of its own."""

STALE_AFTER: Final = timedelta(days=1)
"""How long a probed floor may be trusted. Where a plan grants a rolling window the floor
moves every day, so a floor is a fact about a day, not about a source."""

PER_TICKER: Final[frozenset[str]] = frozenset({"indices"})
"""The classes whose entitlement is decided per ticker rather than per endpoint.

Indices are gated by the source feed behind each ticker, and kanso cannot see that feed's
entitlement except by asking about the ticker. Caching one index's answer for the class
would drop entitled tickers on the floor, so the cache is keyed per ticker for them."""

FIRST_ROW: Final = "first-row"
BISECTED: Final = "bisected"
UNMEASURED: Final = "unmeasured"
"""How a floor was established: read off the oldest row of a straddling request, found by
halving the start date until the source stops refusing, or not established at all.

`UNMEASURED` reports the epoch, which is a floor that clamps nothing. It is the answer
where the source draws no boundary this method can find — it served rows but dated none of
them, or it never served rows at any start date — and it exists because the alternative is
worse than useless: a bisection run without a bracket returns whichever end it started
from, which is a floor of a fortnight ago on a series with twenty years of history, and a
backfill clamped to that fetches a fortnight and reports success."""

UNITS: Final[dict[str, int]] = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}
"""Sub-second parts per second, for the epochs the vendor times rows in: aggregate bars
carry milliseconds and the tick endpoints carry nanoseconds."""


def _dated(text: str) -> bool:
    """Whether a path template or a parameter value carries a date window."""
    return "{start}" in text or "{end}" in text


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One vendor path a probe can ask for rows over a date window.

    A template rather than a function so an endpoint is a value: comparable, printable
    and safe to put in a table. `{ticker}`, `{start}` and `{end}` are substituted in the
    path and in every parameter value, which covers both shapes the vendor uses — the
    range in the path for aggregates, the range in the parameters for ticks.

    `timestamp_field` and `timestamp_unit` are how a row's day is read, which is what
    makes the one-request floor probe possible.

    No `limit` is set on a probe. The vendor applies `limit` to the *base* aggregates
    before rolling them up, so a small limit with a multiplier above one returns an empty
    series — an empty answer that means nothing about entitlement, which is exactly the
    confusion this module exists to prevent.
    """

    dataset: str
    template: str
    params: tuple[tuple[str, str], ...] = ()
    timestamp_field: str = "t"
    timestamp_unit: str = "ms"

    def __post_init__(self) -> None:
        if self.timestamp_unit not in UNITS:
            raise ValidationError(
                f"massive endpoint {self.dataset!r}: {self.timestamp_unit!r} is not one of "
                f"{', '.join(UNITS)}"
            )

    def request(
        self, ticker: str | None = None, window: tuple[date, date] | None = None
    ) -> tuple[str, dict[str, str]]:
        """The path and parameters that ask this endpoint for `ticker` over `window`.

        A ticker is optional because one request genuinely has none: a listing asked of
        the whole market, where dropping the filter is exactly what makes an empty answer
        evidence about the plan rather than about one issuer. An endpoint that names a
        ticker in its own template or parameters cannot be asked that way, and says so as
        a refusal an operator can read rather than as a substitution failure.
        """
        return (
            self._fill(self.template, ticker, window),
            {name: self._fill(value, ticker, window) for name, value in self.params},
        )

    @property
    def dated_path(self) -> bool:
        """Whether this endpoint's date window is part of its address.

        It decides how wide a question the endpoint can be asked. A range that is part of
        the path cannot be left off, so the widest form of such a question is the whole of
        history; a range that lives in the parameters can simply be dropped.
        """
        return _dated(self.template)

    @property
    def windowed(self) -> bool:
        """Whether this endpoint carries a date window at all, in its path or parameters.

        A question asked of an endpoint that carries none is not a question about a window,
        and must never be recorded or described as one: the evidence a probe prints is the
        only account of itself an operator gets, and a window in it for a request that
        carried no dates is evidence of something that did not happen.
        """
        return self.dated_path or any(_dated(value) for _, value in self.params)

    def unwindowed(self) -> Endpoint:
        """The same question with its date window dropped from the parameters.

        This is the widest a listing can be asked, and the request that makes an empty page
        mean something: an event series answers a fortnight with nothing in the ordinary
        case and the whole series with rows. Returns the endpoint unchanged where there is
        no window in the parameters to drop, which includes every endpoint whose range is
        in the path.
        """
        return replace(self, params=tuple(item for item in self.params if not _dated(item[1])))

    def day(self, row: Mapping[str, Any]) -> date | None:
        """The UTC day a row belongs to, or `None` when it carries no usable timestamp."""
        value = row.get(self.timestamp_field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return datetime.fromtimestamp(int(value) // UNITS[self.timestamp_unit], UTC).date()

    def _fill(self, text: str, ticker: str | None, window: tuple[date, date] | None) -> str:
        if "{ticker}" in text:
            if ticker is None:
                raise MalformedRequestError(
                    f"massive: the {self.dataset} endpoint names a ticker in {text!r} and "
                    "none was given",
                    remedy=(
                        "pass the ticker to ask about; only a listing whose ticker filter "
                        "has been dropped is asked of the whole market"
                    ),
                )
            text = text.replace("{ticker}", ticker)
        if "{start}" not in text and "{end}" not in text:
            return text
        if window is None:
            raise MalformedRequestError(
                f"massive: the {self.dataset} endpoint is asked for a date window and none "
                "was given",
                remedy="pass the window the dataset covers",
            )
        return text.replace("{start}", window[0].isoformat()).replace(
            "{end}", window[1].isoformat()
        )


BARS: Final = Endpoint(
    dataset="bars",
    template="/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
    params=(("adjusted", "false"), ("sort", "asc")),
)
"""Daily aggregates. The probe's default: every class the adapter serves has them, the
multiplier is one so no roll-up is involved, and ascending order puts the floor first."""

TRADES: Final = Endpoint(
    dataset="trades",
    template="/v3/trades/{ticker}",
    params=(("timestamp.gte", "{start}"), ("timestamp.lte", "{end}"), ("order", "asc")),
    timestamp_field="sip_timestamp",
    timestamp_unit="ns",
)

QUOTES: Final = Endpoint(
    dataset="quotes",
    template="/v3/quotes/{ticker}",
    params=(("timestamp.gte", "{start}"), ("timestamp.lte", "{end}"), ("order", "asc")),
    timestamp_field="sip_timestamp",
    timestamp_unit="ns",
)
"""Ticks are separately entitled from aggregates in several classes, so a probe of the
bars endpoint says nothing about them: each is probed on its own endpoint."""

REFERENCE: Final = Endpoint(dataset="reference", template="/v3/reference/tickers/{ticker}")
"""The control question for every class the vendor keys here: does it recognise this key
at all? Reference has no history window of its own, so an answer is about the key and
never about the range. Stocks, futures and the prefixed forex and index keys all answer
here; an option key does not, and is asked of `OPTION_REFERENCE` instead."""

OPTION_CONTRACTS: Final = "/v3/reference/options/contracts"
"""Where an option contract's definition lives — the one class the vendor keys outside its
generic ticker reference. A chain is walked from this same path, so it is named here, in
the module every other one imports, rather than in two places that can drift apart."""

OPTION_REFERENCE: Final = Endpoint(dataset="reference", template=f"{OPTION_CONTRACTS}/{{ticker}}")
"""The control question for an option key, which the generic reference cannot answer.

Measured: the generic ticker reference rejects an option key outright with a client error
while this endpoint returns the contract. A probe that asked the generic one would read
that rejection as a key the vendor does not know, and would report options ticks — which
this plan really does exclude, at every date — as a malformed request. That tells an
operator their request is broken when the truth is that their plan excludes the series,
and it is the most expensive wrong answer this adapter can give."""

CONTROLS: Final[dict[str, Endpoint]] = {"options": OPTION_REFERENCE}
"""The classes whose keys the generic control does not answer for.

Kept as a table rather than a branch so the exception is visible: the control endpoint is
chosen the way entitlement is decided, by asking the source the question it can answer."""


def control_for(asset_class: str, generic: Endpoint = REFERENCE) -> Endpoint:
    """The endpoint that says whether the vendor recognises a key of this class."""
    return CONTROLS.get(asset_class, generic)


@dataclass(frozen=True, slots=True)
class Step:
    """One request a probe made and what it signalled, as the evidence for its answer."""

    dataset: str
    path: str
    window: tuple[date, date] | None
    status: int
    signal: Signal
    rows: int

    def payload(self) -> dict[str, object]:
        """The step as a plain record, carrying no credential and no vendor prose."""
        return {
            "dataset": self.dataset,
            "path": self.path,
            "window": [self.window[0].isoformat(), self.window[1].isoformat()]
            if self.window
            else None,
            "status": self.status,
            "signal": str(self.signal),
            "rows": self.rows,
        }


@dataclass(frozen=True, slots=True)
class Probe:
    """What was established about one ticker and one dataset, and how.

    `grain` names the grain the answer is cached at, `detail` says in words what the
    steps showed, and `steps` is the evidence, so an operator reading a refusal can see
    which requests were made and what each returned rather than a vendor sentence that
    means four things.
    """

    outcome: Outcome
    dataset: str
    asset_class: str
    ticker: str
    grain: str
    probed_on: date
    detail: str
    floor: date | None = None
    steps: tuple[Step, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the source will serve what was asked for."""
        return self.outcome is Outcome.OK

    def payload(self) -> dict[str, object]:
        """The probe as a plain record, for `--json`, `doctor` and a manifest note."""
        return {
            "outcome": str(self.outcome),
            "dataset": self.dataset,
            "asset_class": self.asset_class,
            "ticker": self.ticker,
            "grain": self.grain,
            "probed_on": self.probed_on.isoformat(),
            "detail": self.detail,
            "floor": self.floor.isoformat() if self.floor else None,
            "steps": [step.payload() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class Floor:
    """The earliest date a source serves for one ticker and dataset, and when it was found.

    `probed_on` is load bearing: a plan that grants a rolling window has a floor that
    moves with the calendar, so a floor carried from yesterday into today is wrong by a
    day, and one carried from last month is wrong by a month.

    It is measured per series rather than per class, which for an instrument listed after
    the plan's window opens is the listing date rather than the plan's own edge. That is
    the number a backfill wants either way — where to stop asking — and it is a floor in
    the only sense that matters here: the source serves nothing before it.
    """

    asset_class: str
    dataset: str
    ticker: str
    floor: date
    probed_on: date
    method: str
    steps: tuple[Step, ...] = ()

    def stale(self, on: date) -> bool:
        """True when this floor is too old to be trusted on `on`."""
        return on - self.probed_on > STALE_AFTER

    def payload(self) -> dict[str, object]:
        return {
            "asset_class": self.asset_class,
            "dataset": self.dataset,
            "ticker": self.ticker,
            "floor": self.floor.isoformat(),
            "probed_on": self.probed_on.isoformat(),
            "method": self.method,
            "steps": [step.payload() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class _Plan:
    """What the entitlement questions established, and the widest answer they got.

    `outcome` is `None` where the plan includes the series and one of the four otherwise.
    `straddle` is the answer to a whole-of-history request where one was made, so a floor
    measured afterwards reads it off rather than asking the identical question twice.
    `serving` is whether the recent window came back with rows, which is the one thing a
    bisection needs and cannot establish for itself: halving a start date only converges
    where one end of the bracket is known to serve.
    """

    outcome: Outcome | None
    detail: str
    straddle: Call | None = None
    serving: bool = False


PLAN_VERDICTS: Final[frozenset[Outcome]] = frozenset({Outcome.OK, Outcome.NOT_ENTITLED})
"""The outcomes that are statements about a plan, and may therefore answer for another key.

A plan covers a dataset for a class, so "the plan includes this" and "the plan excludes
this" carry from the key that established them to the next key of the same grain, which is
what makes a universe cost one probe instead of one per name. `EMPTY` and `MALFORMED` do
not: they say this key holds no rows and this key is not one the vendor carries, which are
facts about a key and about nothing else. Reusing one of those is how a silent ticker
becomes the verdict for a whole class — the survey's own warning that an empty answer for
one issuer is a fact about that issuer, enforced here rather than only written down."""


def today_utc() -> date:
    """Today in UTC, the basis every span in kanso is stated in."""
    return datetime.now(UTC).date()


def settled_end(today: date) -> date:
    """The most recent day a probe asks about."""
    return today - SETTLE


def recent_window(today: date) -> tuple[date, date]:
    """The window a probe uses to ask whether the plan includes a series at all."""
    end = settled_end(today)
    return end - PROBE_SPAN, end


def grain(asset_class: str) -> str:
    """The grain this class's entitlement is decided at: `ticker` or `endpoint`."""
    return "ticker" if asset_class in PER_TICKER else "endpoint"


def key(asset_class: str, dataset: str, ticker: str) -> str:
    """The cache key an answer about this series may be reused under.

    Coarse where the source gates coarsely and per ticker where it does not. It decides
    only *where* an answer may be reused; `PLAN_VERDICTS` decides *which* answers may be,
    and an answer about a key never is, whatever the grain.
    """
    if asset_class in PER_TICKER:
        return f"{asset_class}:{dataset}:{ticker}"
    return f"{asset_class}:{dataset}"


def probe(
    client: MassiveClient,
    ticker: str,
    asset_class: str,
    *,
    dataset: Endpoint = BARS,
    window: tuple[date, date] | None = None,
    as_of: date | None = None,
    control: Endpoint | None = None,
    earliest: date = EARLIEST,
) -> Probe:
    """Establish which of the five outcomes this series and window are in.

    Asks a recent window first, because a refusal there cannot be about the range; where
    that window comes back empty rather than refused, asks the widest question the
    endpoint admits, because an empty page is about a window until there is no window left
    to blame. Then the requested window, if there is one; then the control endpoint or the
    floor, whichever separates the remaining cases. The control defaults to the endpoint
    that holds keys of this class, which for an option contract is not the generic ticker
    reference; passing one names it explicitly. Entitlement alone costs one request where
    the plan serves the fortnight, two where it refuses one or where a quiet fortnight is
    settled by the wider question, and three where that wider question settles nothing
    either. Only a range that came back short pays for a floor besides: one further request
    where the source truncates, none where the floor cannot be measured, and about fifteen
    where the source refuses a straddling range and a start date is known to serve — so
    `Entitlements` measures a floor once and reuses it.
    """
    today = as_of or today_utc()
    steps: list[Step] = []

    def settled(outcome: Outcome, detail: str, floor: date | None = None) -> Probe:
        return _verdict(dataset, ticker, asset_class, today, outcome, detail, steps, floor)

    plan = _included(client, dataset, ticker, asset_class, today, earliest, control, steps)
    if plan.outcome is not None:
        return settled(plan.outcome, plan.detail)
    if window is None:
        return settled(Outcome.OK, plan.detail)

    asked = _ask(client, dataset, ticker, window, steps).signal
    if asked is Signal.ROWS:
        return settled(Outcome.OK, "the source served rows over the requested window")
    if asked is Signal.BAD_REQUEST:
        return settled(Outcome.MALFORMED, "the vendor rejected the requested window outright")

    found = _floor(
        client, dataset, ticker, asset_class, today, earliest, steps, plan.straddle, plan.serving
    )
    if window[1] < found.floor:
        return settled(
            Outcome.BELOW_FLOOR,
            f"the source serves this series from {found.floor} and the range ends "
            f"{window[1]}, so the whole of it is older than the source",
            found.floor,
        )
    if asked is Signal.REFUSED:
        return settled(
            Outcome.NOT_ENTITLED,
            f"the plan includes this series and the source refused "
            f"{window[0]}..{window[1]}, which is above its floor of {found.floor}, so the "
            "plan gates the range itself",
            found.floor,
        )
    return settled(
        Outcome.EMPTY,
        f"the plan includes this series and its floor is {found.floor}, and "
        f"{window[0]}..{window[1]} holds nothing",
        found.floor,
    )


def history_floor(
    client: MassiveClient,
    ticker: str,
    asset_class: str,
    *,
    dataset: Endpoint = BARS,
    as_of: date | None = None,
    control: Endpoint | None = None,
    earliest: date = EARLIEST,
) -> Floor:
    """The earliest date the source serves for this series, measured.

    Refuses (precondition) a series the plan does not include, because the earliest date
    of something that is never served is not a floor: it is an entitlement failure, and
    reporting it as a floor is the confusion this adapter exists to avoid. Entitlement is
    established here exactly as a probe establishes it, so a series whose recent fortnight
    happens to be quiet is floored rather than refused, and the failure a caller sees names
    which of the outcomes stopped it.
    """
    today = as_of or today_utc()
    steps: list[Step] = []
    plan = _included(client, dataset, ticker, asset_class, today, earliest, control, steps)
    if plan.outcome is not None:
        raise _no_floor(dataset, ticker, asset_class, plan.outcome, plan.detail)
    return _floor(
        client, dataset, ticker, asset_class, today, earliest, steps, plan.straddle, plan.serving
    )


def _no_floor(
    endpoint: Endpoint, ticker: str, asset_class: str, outcome: Outcome, detail: str
) -> MassiveError:
    """The failure a floor request gets when the plan does not include the series.

    It names the outcome that stopped it rather than the measurement that could not happen,
    because a plan to change, a key to correct and a series that holds nothing are three
    different things for an operator to do about the same missing number.
    """
    return ERROR_FOR[outcome](
        f"massive: {endpoint.dataset} for {ticker} ({asset_class}) has no history floor "
        f"to measure: {detail}",
        remedy="probe entitlement first; a floor is only meaningful for a served series",
    )


def raise_if_blocked(found: Probe) -> None:
    """Turn a probe that is not `OK` into the failure that names what it found.

    Each failure carries its own exit code and its own remedy, so the operator agent
    branches on the outcome rather than on prose: a plan to change, a range to clamp, a
    window to widen and a request to correct are four different things to do.
    """
    if found.ok:
        return
    where = f"massive: {found.dataset} for {found.ticker} ({found.asset_class})"
    match found.outcome:
        case Outcome.NOT_ENTITLED:
            raise NotEntitledError(
                f"{where} is not included in this plan: {found.detail}",
                remedy=(
                    "drop the series from the spec, or add the dataset to the plan; this is "
                    "not a history-floor failure, which is probed separately"
                ),
            )
        case Outcome.BELOW_FLOOR:
            raise BelowFloorError(
                f"{where} is served from {found.floor}: {found.detail}",
                remedy=f"ask for a range at or after {found.floor}; `data backfill` clamps to it",
                floor=found.floor,
            )
        case Outcome.EMPTY:
            raise EmptyResultError(
                f"{where} served nothing: {found.detail}",
                remedy="widen the range, or check that the instrument traded over it",
            )
        case _:
            raise MalformedRequestError(
                f"{where} is not a request the vendor honours: {found.detail}",
                remedy="check the ticker and the prefix its asset class carries",
            )


class Entitlements:
    """A memo over the probes, so a universe is asked about once rather than per chunk.

    A backfill walks years in chunks and would otherwise re-probe every chunk; the
    answers are cached at the grain the source gates on, and the floor is cached per
    series, so a range is checked against a measured floor with no further requests. The
    day is fixed at construction: one long run sees one consistent set of floors, which
    matters where the plan's window rolls at midnight.

    Two rules keep the saving honest. Only a **plan verdict** answers for another key —
    "the plan includes this dataset" and "the plan excludes it" are facts about a
    subscription, while "this key holds nothing" and "this key does not exist" are facts
    about one name and are established for every name that asks. And a question this memo
    asks is asked **once**: establishing the plan and measuring a floor want the same
    recent window, so the second reads the first's answer instead of paying for it again.

    Nothing here raises what a walk is meant to survive. `check` returns the outcome it
    finds, including one the floor measurement was the first to discover, because a caller
    branching on an outcome cannot branch on an exception thrown past it.
    """

    def __init__(
        self,
        client: MassiveClient,
        *,
        as_of: date | None = None,
        control: Endpoint | None = None,
        earliest: date = EARLIEST,
    ) -> None:
        self._client = client
        self.as_of = as_of or today_utc()
        self._control = control
        self._earliest = earliest
        self._probes: dict[str, Probe] = {}
        self._plans: dict[str, tuple[_Plan, tuple[Step, ...]]] = {}
        self._floors: dict[str, Floor] = {}
        self._requests = 0

    @property
    def requests(self) -> int:
        """How many requests this memo has spent, counted as they were made.

        Counted here rather than from the answers, because an answer that failed carries
        its steps into an exception and an answer reused at the source's grain appears on
        several lines: a report summing either would be short exactly where a survey ran
        into trouble.
        """
        return self._requests

    def check(
        self,
        ticker: str,
        asset_class: str,
        *,
        dataset: Endpoint = BARS,
        window: tuple[date, date] | None = None,
    ) -> Probe:
        """Whether this series, and optionally this window, can be asked for.

        Entitlement is probed once per grain; a window is then judged against the measured
        floor rather than by asking again. A window above the floor is reported `OK` even
        though it may hold nothing: whether a served range is short is coverage, which
        the loader measures from what arrived.

        Measuring that floor asks about *this* key, and so can be the first to learn that
        the plan reused from another key does not cover it. That is an answer, not an
        accident, and it comes back as this probe's outcome.
        """
        found = self._settled(ticker, asset_class, dataset)
        if not found.ok or window is None:
            return found
        plan, steps = self._planned(ticker, asset_class, dataset)
        if plan.outcome is not None:
            return _verdict(
                dataset, ticker, asset_class, self.as_of, plan.outcome, plan.detail, steps
            )
        floor = self._floored(ticker, asset_class, dataset, plan, steps)
        if window[1] < floor.floor:
            return replace(
                found,
                outcome=Outcome.BELOW_FLOOR,
                detail=(
                    f"the source serves this series from {floor.floor} and the range ends "
                    f"{window[1]}"
                ),
                floor=floor.floor,
            )
        return replace(
            found,
            detail=f"the range begins at or after the floor of {floor.floor}",
            floor=floor.floor,
        )

    def floor(self, ticker: str, asset_class: str, *, dataset: Endpoint = BARS) -> Floor:
        """The measured floor of this series, probed once and reused for the run.

        Raises the outcome that stopped it where the plan does not include the series: the
        earliest date of something never served is not a floor. A caller that would rather
        have the outcome as a value asks `check`.
        """
        plan, steps = self._planned(ticker, asset_class, dataset)
        if plan.outcome is not None:
            raise _no_floor(dataset, ticker, asset_class, plan.outcome, plan.detail)
        return self._floored(ticker, asset_class, dataset, plan, steps)

    def probes(self) -> tuple[Probe, ...]:
        """Every entitlement answer established here, in the order it was reached."""
        return tuple(self._probes.values())

    def floors(self) -> tuple[Floor, ...]:
        """Every floor measured here."""
        return tuple(self._floors.values())

    def _settled(self, ticker: str, asset_class: str, dataset: Endpoint) -> Probe:
        """This key's entitlement answer, reusing another key's only where it is a plan."""
        cached = self._probes.get(key(asset_class, dataset.dataset, ticker))
        if cached is not None and cached.outcome in PLAN_VERDICTS:
            return replace(cached, ticker=ticker)
        return self._probe(ticker, asset_class, dataset)

    def _probe(self, ticker: str, asset_class: str, dataset: Endpoint) -> Probe:
        plan, steps = self._planned(ticker, asset_class, dataset)
        found = _verdict(
            dataset,
            ticker,
            asset_class,
            self.as_of,
            plan.outcome or Outcome.OK,
            plan.detail,
            steps,
        )
        self._probes[key(asset_class, dataset.dataset, ticker)] = found
        return found

    def _planned(
        self, ticker: str, asset_class: str, dataset: Endpoint
    ) -> tuple[_Plan, tuple[Step, ...]]:
        """What the entitlement questions establish about this key, asked at most once."""
        memo = self._series(asset_class, dataset, ticker)
        cached = self._plans.get(memo)
        if cached is not None:
            return cached
        steps: list[Step] = []
        plan = _included(
            self._client,
            dataset,
            ticker,
            asset_class,
            self.as_of,
            self._earliest,
            self._control,
            steps,
        )
        self._requests += len(steps)
        self._plans[memo] = (plan, tuple(steps))
        return self._plans[memo]

    def _floored(
        self,
        ticker: str,
        asset_class: str,
        dataset: Endpoint,
        plan: _Plan,
        steps: Sequence[Step],
    ) -> Floor:
        memo = self._series(asset_class, dataset, ticker)
        cached = self._floors.get(memo)
        if cached is not None:
            return cached
        trail = list(steps)
        found = _floor(
            self._client,
            dataset,
            ticker,
            asset_class,
            self.as_of,
            self._earliest,
            trail,
            plan.straddle,
            plan.serving,
        )
        self._requests += len(trail) - len(steps)
        self._floors[memo] = found
        return found

    @staticmethod
    def _series(asset_class: str, dataset: Endpoint, ticker: str) -> str:
        """The key a plan and a floor are memoised under: always the series itself."""
        return f"{asset_class}:{dataset.dataset}:{ticker}"


def _ask(
    client: MassiveClient,
    endpoint: Endpoint,
    ticker: str,
    window: tuple[date, date] | None,
    steps: list[Step],
) -> Call:
    """One probe request, recorded as evidence before anything can go wrong with it.

    The window is recorded only where the endpoint carries one. A request that named no
    dates is not evidence about a window, and a step claiming otherwise would put a
    fortnight in the record of a question asked over the whole of a market's history.
    """
    path, params = endpoint.request(ticker, window)
    call = client.call(path, params)
    steps.append(
        Step(
            dataset=endpoint.dataset,
            path=call.path,
            window=window if endpoint.windowed else None,
            status=call.status,
            signal=call.signal,
            rows=len(call.rows),
        )
    )
    call.raise_for_transport()
    return call


def _included(
    client: MassiveClient,
    endpoint: Endpoint,
    ticker: str,
    asset_class: str,
    today: date,
    earliest: date,
    control: Endpoint | None,
    steps: list[Step],
) -> _Plan:
    """Whether the plan includes this series at all, asked so that no empty page can lie.

    A refusal and an empty page are different kinds of evidence and are treated as such. A
    refusal is the source declining to serve, so it goes straight to the control question,
    which separates a plan that excludes the series from a key the vendor never carried. An
    empty page is a statement about the window it was asked over, so the window is taken
    away and the question asked again as widely as the endpoint admits; a series that
    answers *that* with rows is included, whatever the fortnight held. Reading the fortnight
    as the plan is what tells an operator to buy a subscription they already hold, and for a
    quarterly statement or an episodic filing it is the ordinary case rather than the rare
    one.

    A refusal of the *widest* question is the one refusal that is not read as a plan, and
    only where the widest question was widened by dating it back to the epoch. Measured, a
    series the plan excludes is refused at every date, this fortnight included; this one was
    not, so what the source declined was a range starting below every plan's history window,
    which is the range's problem and not the subscription's. The floor is what settles it,
    and calling it an entitlement failure would be the expensive answer again with a longer
    range attached.
    """
    asking = control_for(asset_class) if control is None else control
    question = _question(endpoint)
    live = _ask(client, endpoint, ticker, recent_window(today), steps).signal
    if live is Signal.BAD_REQUEST:
        return _Plan(Outcome.MALFORMED, "the vendor rejected the request outright")
    if live is Signal.ROWS:
        return _Plan(None, f"the source served {question} for this series", serving=True)
    if live is Signal.REFUSED:
        return _Plan(*_refused(client, ticker, asking, steps, question))
    wide = _widest(client, endpoint, ticker, today, earliest, steps)
    if wide is None:
        return _Plan(*_nowhere(client, ticker, asking, steps, question))
    straddle = wide if endpoint.dated_path else None
    if wide.signal is Signal.BAD_REQUEST:
        return _Plan(
            Outcome.MALFORMED,
            f"{question} held nothing and the vendor rejected the same question asked "
            "as widely as this endpoint admits",
        )
    if wide.signal is Signal.REFUSED:
        if not endpoint.dated_path:
            return _Plan(*_refused(client, ticker, asking, steps, question))
        return _Plan(
            None,
            f"{question} held nothing and was not refused, and the source refused the "
            f"same question dated back to {earliest}; a plan that excluded this series "
            "would have refused the recent window too, so what was refused is the range",
            straddle,
        )
    if wide.signal is Signal.ROWS:
        return _Plan(
            None,
            f"{question} held nothing and the series serves rows for the widest question "
            "this endpoint admits, so the plan includes it and the window is what was empty",
            straddle,
        )
    return _Plan(*_nowhere(client, ticker, asking, steps, question), straddle)


def _question(endpoint: Endpoint) -> str:
    """What a probe's first question was, in the words an operator's report prints.

    An endpoint whose window lives in its parameters is asked with them taken off by the
    callers that want a market's answer rather than an issuer's, and describing that as a
    recent window would report a fortnight nobody asked about.
    """
    return "a recent window" if endpoint.windowed else "this question with no date window"


def _widest(
    client: MassiveClient,
    endpoint: Endpoint,
    ticker: str,
    today: date,
    earliest: date,
    steps: list[Step],
) -> Call | None:
    """The same question asked as widely as the endpoint admits, or `None` where the
    question already was that wide.

    A listing keeps its window in the parameters and answers without them — measured:
    quarterly statements and episodic filings answer a fourteen-day window with an empty
    page and the identical request with the dates taken off with a full one. An aggregate
    keeps its window in the path, where the widest question is the whole of history, which
    is also the request a floor is read off, so the answer is handed on rather than asked
    for twice. A question that carried no window in the first place was already the widest
    one there is, and repeating it would only cost a request.
    """
    if endpoint.dated_path:
        return _ask(client, endpoint, ticker, (earliest, settled_end(today)), steps)
    wide = endpoint.unwindowed()
    return None if wide == endpoint else _ask(client, wide, ticker, None, steps)


def _refused(
    client: MassiveClient,
    ticker: str,
    control: Endpoint,
    steps: list[Step],
    question: str,
) -> tuple[Outcome, str]:
    """What a refusal means: a plan that excludes the series, or a key the vendor lacks.

    A refusal is a statement about the plan unless the vendor does not carry the key at
    all, so the one question left is whether it carries it, asked where keys of this class
    are kept. `question` names what was refused, because the detail is the whole account of
    itself a probe gives and an account of the wrong request is worse than none.
    """
    seen = _ask(client, control, ticker, None, steps).signal
    if seen is Signal.ROWS:
        return (
            Outcome.NOT_ENTITLED,
            f"the vendor recognises this key and refused {question}, so the plan "
            "excludes the series rather than the request being wrong",
        )
    if seen is Signal.REFUSED:
        return (
            Outcome.NOT_ENTITLED,
            f"the source refused {question} and the reference endpoint is refused "
            "too, so the key shape could not be checked and the plan is the likelier cause",
        )
    return (
        Outcome.MALFORMED,
        f"the source refused {question} and the vendor does not recognise the key, "
        "so the ticker or its asset-class prefix is wrong",
    )


def _nowhere(
    client: MassiveClient,
    ticker: str,
    control: Endpoint,
    steps: list[Step],
    question: str,
) -> tuple[Outcome, str]:
    """What an empty page means once there is no window left to blame it on.

    The source answered every question about this series and refused none of them, so
    nothing here is evidence about the plan. Either the vendor does not carry the key,
    which is a request to correct, or it carries it and holds no rows for it at any date,
    which is an empty series. Calling that second case an entitlement failure is precisely
    the answer that sends an operator to buy a subscription they already hold.
    """
    seen = _ask(client, control, ticker, None, steps).signal
    if seen in (Signal.ROWS, Signal.REFUSED):
        return (
            Outcome.EMPTY,
            f"the source refused nothing about this series — not {question} and not the "
            "widest question this endpoint admits — and served no rows for either, so it "
            "holds nothing rather than the plan excluding it",
        )
    return (
        Outcome.MALFORMED,
        "the source served nothing and the vendor does not recognise the key, so the "
        "ticker or its asset-class prefix is wrong",
    )


def _floor(
    client: MassiveClient,
    endpoint: Endpoint,
    ticker: str,
    asset_class: str,
    today: date,
    earliest: date,
    steps: list[Step],
    asked: Call | None = None,
    serving: bool = True,
) -> Floor:
    """The floor, read off a straddling request where possible and bisected where not.

    A range that begins below the floor and ends above it is answered with a success
    status and a series that starts at the floor, so one ascending request over all of
    history names the floor exactly. Where the source refuses such a range instead of
    truncating it, the start date is halved until it stops refusing, which costs about
    fifteen requests for fifty years.

    Halving needs a bracket, and two things can leave it without one. A straddling request
    that *served* draws no refusal boundary to find, so rows it dated nowhere leave nothing
    to search for. And a recent window that came back empty is not a start date known to
    serve, so the predicate the search halves on — does this start date return rows — is
    not even monotonic: a delisted issuer answers an old start with rows and a recent one
    with none. Searching either shape returns whichever end the search began at, which is a
    floor of a fortnight ago on a series with twenty years behind it. So neither is
    searched: the floor is reported unmeasured, at the epoch, where it clamps nothing and a
    loader records the span the source actually served.

    `asked` is that whole-of-history answer where establishing entitlement already had to
    ask for it, which is the case for a series whose recent window came back empty.
    `serving` is whether that recent window returned rows.
    """
    end = settled_end(today)
    straddle = asked or _ask(client, endpoint, ticker, (earliest, end), steps)
    days = [day for row in straddle.rows if (day := endpoint.day(row)) is not None]
    if straddle.signal is Signal.ROWS and days:
        return _found(asset_class, endpoint, ticker, min(days), today, FIRST_ROW, steps)
    if straddle.signal is Signal.ROWS or not serving:
        return _found(asset_class, endpoint, ticker, earliest, today, UNMEASURED, steps)
    edge = _bisect(client, endpoint, ticker, earliest, end, steps)
    return _found(asset_class, endpoint, ticker, edge, today, BISECTED, steps)


def _bisect(
    client: MassiveClient,
    endpoint: Endpoint,
    ticker: str,
    earliest: date,
    end: date,
    steps: list[Step],
) -> date:
    """The earliest start date the source answers with rows, to the day.

    The invariant is that `low` is known not to serve and `high` is known to serve — the
    recent window that established entitlement is exactly the request at `high` — so the
    loop halves a known bracket rather than searching an unknown one. `_floor` is what
    holds it: it calls this only where that recent window returned rows and the widest
    request was refused, which is the one shape in which the boundary exists and the
    predicate is monotonic. Called without that, the loop returns its own starting point
    dressed as a measurement, which is why the guard lives in the caller rather than here.
    """
    high = end - PROBE_SPAN
    low = min(earliest, high)
    while (high - low).days > 1:
        middle = low + timedelta(days=(high - low).days // 2)
        if _ask(client, endpoint, ticker, (middle, end), steps).signal is Signal.ROWS:
            high = middle
        else:
            low = middle
    return high


def _verdict(
    endpoint: Endpoint,
    ticker: str,
    asset_class: str,
    today: date,
    outcome: Outcome,
    detail: str,
    steps: Sequence[Step],
    floor: date | None = None,
) -> Probe:
    """One probe answer, built the same way wherever it is reached."""
    return Probe(
        outcome=outcome,
        dataset=endpoint.dataset,
        asset_class=asset_class,
        ticker=ticker,
        grain=grain(asset_class),
        probed_on=today,
        detail=detail,
        floor=floor,
        steps=tuple(steps),
    )


def _found(
    asset_class: str,
    endpoint: Endpoint,
    ticker: str,
    floor: date,
    today: date,
    method: str,
    steps: Sequence[Step],
) -> Floor:
    return Floor(
        asset_class=asset_class,
        dataset=endpoint.dataset,
        ticker=ticker,
        floor=floor,
        probed_on=today,
        method=method,
        steps=tuple(steps),
    )
