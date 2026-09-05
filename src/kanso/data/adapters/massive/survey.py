"""What this key actually reaches, measured — the answer `data adapters` and `doctor` print.

An operator's first question about a vendor is not "what does the adapter offer" but "what
does *my* key get". The offer is a constant and lives in the package's capabilities; the
answer to the second question is a fact about a plan on a day, and the only sound way to
get it is to ask the source. This module asks, once per dataset, at the grain the source
gates on, and reports the outcome and the measured history floor side by side.

**Reachability is established before entitlement.** A key the vendor does not accept
refuses everything, and reading those refusals as "your plan excludes these datasets" is
the one wrong answer this adapter exists to avoid. So the control lookup runs first, and a
key that fails it ends the survey: nothing further would mean anything.

**A sparse series is asked a question it can answer.** The price series carry a continuous
history: a fortnight of them holds bars, and the oldest row of a straddling request is the
floor. The reference listings — splits, dividends, financials, filings — are event series
that are silent most of the time. Statements are quarterly and filings episodic, so a
fortnight of either holds nothing in the ordinary case, and a fortnight asked of one issuer
holds nothing nearly always. So a listing is asked **market-wide and with no date window at
all**, which is the only form of the question an event series can answer with rows; and no
floor is measured for it, because the search that finds a floor in a continuous series
returns an arbitrary year in a sparse one. Only a listing the source **refuses** is
reported as a plan that excludes it.

That is not a nicety. A listing probed over a fortnight comes back empty in the ordinary
case, and an empty page reported as `not_entitled` tells an operator to buy a subscription
they already hold — the most expensive wrong answer this adapter can give, printed on the
one screen an operator reads before buying.

Prices are the other way round: they are asked of a key, and for indices the answer is
about that key alone, because the source gates them by the feed behind each ticker.

**Two keys cannot be constants.** An option contract and a futures contract both expire,
so a hard-coded one would go stale and come back refused, reporting a plan failure that is
really a dead key. Both are discovered from the reference endpoints, which carry no
history window, at one request each; where the discovery finds nothing, that class is
reported as unprobed rather than as unentitled.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from kanso.data.adapters.massive import CHECK_TICKER, MassiveAdapter
from kanso.data.adapters.massive.client import MassiveClient, Signal
from kanso.data.adapters.massive.conventions import (
    FOREX,
    FUTURES,
    INDICES,
    MARKET_OF,
    OPTIONS,
    STOCKS,
)
from kanso.data.adapters.massive.corporate_actions import DIVIDENDS_EFFECTIVE, SPLITS
from kanso.data.adapters.massive.entitlement import (
    BARS,
    QUOTES,
    TRADES,
    Endpoint,
    Entitlements,
    grain,
)
from kanso.data.adapters.massive.errors import MassiveError
from kanso.data.adapters.massive.filings import FILINGS, offered
from kanso.data.adapters.massive.financials import FINANCIALS
from kanso.data.adapters.massive.reference import CONTRACTS, LISTING
from kanso.data.registry import Reach, Survey

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.data.adapters.massive.client import Transport
    from kanso.workspace import Workspace

__all__ = ["KEYS", "OPTIONS_UNDERLYING", "TARGETS", "Target", "survey"]

KEYS: Final[dict[str, str]] = {
    STOCKS: CHECK_TICKER,
    FOREX: "C:EURUSD",
    INDICES: "I:NDX",
}
"""The key each of these classes is probed with.

Each is permanent: a listed equity, the most-quoted currency pair and a headline index.
An option or a futures contract cannot be on this list, because both expire and a stale
key comes back refused — which would be reported as a plan that excludes the class when
the truth is a key that no longer exists.
"""

OPTIONS_UNDERLYING: Final = CHECK_TICKER
"""Whose option chain a probe key is taken from: an underlying that always has one."""

UNPROBED: Final = "unprobed"
"""The outcome of a class no key could be found for. It is deliberately not one of the
four the vendor's sentence conflates: nothing was asked, so nothing was established."""


@dataclass(frozen=True, slots=True)
class Target:
    """One dataset of one class to probe, and how the source has to be asked about it.

    `sparse` says this dataset is an event series rather than a continuous one, and it
    settles three things at once: the listing is asked of the whole market rather than of
    one issuer, it is asked with no date window on it, and no history floor is measured for
    it. All three follow from the same fact — an event series is silent most of the time,
    so every narrow question about it comes back empty in the ordinary case, and an empty
    page read as a plan is the wrong answer this survey exists to prevent.
    """

    asset_class: str
    endpoint: Endpoint
    sparse: bool = False

    @property
    def dataset(self) -> str:
        return self.endpoint.dataset

    @property
    def question(self) -> Endpoint:
        """The endpoint as this target is actually asked.

        A sparse listing has its date window taken off, so that the only empty answer it
        can give is one about the plan. Measured: the statements and filings listings
        answer a fourteen-day window with an empty page and the identical request without
        dates with a full one.
        """
        return self.endpoint.unwindowed() if self.sparse else self.endpoint


TARGETS: Final[tuple[Target, ...]] = (
    Target(STOCKS, BARS),
    Target(STOCKS, TRADES),
    Target(STOCKS, QUOTES),
    Target(OPTIONS, BARS),
    Target(OPTIONS, TRADES),
    Target(OPTIONS, QUOTES),
    Target(FUTURES, BARS),
    Target(FOREX, BARS),
    Target(FOREX, QUOTES),
    Target(INDICES, BARS),
    Target(STOCKS, offered(SPLITS), sparse=True),
    Target(STOCKS, offered(DIVIDENDS_EFFECTIVE), sparse=True),
    Target(STOCKS, offered(FINANCIALS), sparse=True),
    Target(STOCKS, offered(FILINGS), sparse=True),
)
"""Every question the survey asks, in report order.

The price datasets are listed per class *and per dataset* because that is where the plans
differ: a class whose aggregates are included and whose ticks are not is one class with
two answers, and a survey reporting one line per class would have to pick the wrong one.
"""

TICKER_GRAIN_NOTE: Final = (
    "indices are gated by the source feed behind each ticker, so this answer is about "
    "{ticker} and about no other index"
)
MARKET_NOTE: Final = (
    "the reference listings are probed over the whole market and with no date window: "
    "statements are quarterly and filings episodic, so a fortnight of one issuer holds "
    "nothing in the ordinary case, and only a listing that is refused is a plan"
)
NO_FLOOR_NOTE: Final = (
    "no history floor is measured for a reference listing: it is a sparse event series, "
    "where a year holding nothing is the issuer's silence rather than the source's edge"
)


def survey(
    adapter: MassiveAdapter,
    ws: Workspace,
    *,
    transport: Transport | None = None,
    as_of: date | None = None,
) -> Survey:
    """Probe what this workspace's key reaches, and how far back, dataset by dataset.

    One rate-limited connection serves the whole survey, including the reachability
    lookup, so every request in it counts against one quota. Nothing here writes to the
    workspace and nothing here reads a credential into a message.
    """
    client = adapter.client(ws, transport=transport)
    check = adapter.check(ws, transport=client.transport)
    if not check.ok:
        return Survey(
            adapter=adapter.id,
            reachable=False,
            detail=check.detail,
            requests=1,
            notes=(
                "nothing else was probed: a key the vendor does not accept refuses every "
                "dataset, and reading that as a plan would be the wrong answer",
            ),
        )
    memo = Entitlements(client, as_of=as_of)
    keys, discovery = _keys(client)
    reach = tuple(_reach(memo, target, keys) for target in TARGETS)
    return Survey(
        adapter=adapter.id,
        reachable=True,
        detail=check.detail,
        requests=1 + discovery + memo.requests,
        reach=reach,
        notes=tuple(_notes(reach, keys)),
    )


def _keys(client: MassiveClient) -> tuple[dict[str, str], int]:
    """A probe key per class, and how many requests finding them cost.

    The permanent keys cost nothing. The two that expire are read from the reference
    endpoints, which have no history window of their own, so a chain that expired before
    the aggregate floor still lists and a key found here is a key that exists today.
    """
    found = dict(KEYS)
    spent = 0
    for asset_class, path, params in (
        (OPTIONS, CONTRACTS, {"underlying_ticker": OPTIONS_UNDERLYING, "limit": "1"}),
        (FUTURES, LISTING, {"market": MARKET_OF[FUTURES], "active": "true", "limit": "1"}),
    ):
        spent += 1
        key = _first_key(client, path, params)
        if key is not None:
            found[asset_class] = key
    return found, spent


def _first_key(client: MassiveClient, path: str, params: Mapping[str, str]) -> str | None:
    """The first key one page of a reference listing names, or `None` when it names none.

    One page, never a walk: this wants an example, and an example is the first row. A
    refusal here is not read as an entitlement answer — it simply leaves the class without
    a key, which the survey reports as unprobed.
    """
    call = client.call(path, dict(params))
    if call.signal is not Signal.ROWS:
        return None
    for row in call.rows:
        ticker = row.get("ticker")
        if isinstance(ticker, str) and ticker:
            return ticker
    return None


def _reach(memo: Entitlements, target: Target, keys: Mapping[str, str]) -> Reach:
    """Probe one target, and measure its floor where a floor means anything.

    A vendor failure ends this target and no other. A survey that stopped at the first
    refusal would report a plan by its first gap, which is the opposite of what it is for.
    """
    ticker = keys.get(target.asset_class)
    if ticker is None:
        return Reach(
            asset_class=target.asset_class,
            dataset=target.dataset,
            grain=grain(target.asset_class),
            outcome=UNPROBED,
            detail="no key of this class could be found to probe with, so nothing was asked",
        )
    try:
        found = memo.check(ticker, target.asset_class, dataset=target.question)
        floor = (
            memo.floor(ticker, target.asset_class, dataset=target.question).floor
            if found.ok and not target.sparse
            else None
        )
    except MassiveError as error:
        return Reach(
            asset_class=target.asset_class,
            dataset=target.dataset,
            grain=grain(target.asset_class),
            ticker=None if target.sparse else ticker,
            outcome=str(error.outcome) if error.outcome is not None else "unavailable",
            detail=error.message,
            probed_on=memo.as_of,
        )
    return Reach(
        asset_class=target.asset_class,
        dataset=target.dataset,
        grain=found.grain,
        ticker=None if target.sparse else ticker,
        outcome=str(found.outcome),
        detail=found.detail,
        floor=floor,
        probed_on=found.probed_on,
    )


def _notes(reach: tuple[Reach, ...], keys: Mapping[str, str]) -> Iterator[str]:
    """What an operator has to know to read these lines correctly."""
    if any(item.grain == "ticker" for item in reach) and INDICES in keys:
        yield TICKER_GRAIN_NOTE.format(ticker=keys[INDICES])
    yield MARKET_NOTE
    yield NO_FLOOR_NOTE
    for asset_class in sorted({item.asset_class for item in reach if item.outcome == UNPROBED}):
        yield (
            f"{asset_class}: no contract was listed to probe with, so this class was not "
            "asked about; it is neither entitled nor refused here"
        )
