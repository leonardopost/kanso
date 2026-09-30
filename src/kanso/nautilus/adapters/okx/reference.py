"""The exchange's public reference: a listed perpetual swap resolved into an instrument.

This is the data adapter the OKX package exposes beside its broker, as `ADAPTER`, and the
instrument provider `[data] reference = "okx"` names. As a provider it reads one public
endpoint, the instruments listing, with no credential at all; the adapter also hands out the
public-history loaders (`history.py`), which read through the same client, and this module
holds the wire they share.

**Public, so enabled by its table.** The listing needs no key, so this adapter has no
credential to be enabled by; it is enabled by `[adapters.okx]` instead, which is also where
the regional host is stated. A workspace that never named the exchange makes no request of
it — `configured` is false, `data adapters --check` and `doctor --check-adapters` pass it
by — and a workspace that names it as its reference without stating a region is refused by
the table's own rule, before anything is sent. No header but the User-Agent is ever sent, so
a variable exported for another tool (`OKX_API_KEY` among them) reaches nothing here.

**One id, one request.** Measured on 2026-09-30 against `us.okx.com`:
`GET /api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP` answers the one row,
where the unnarrowed page carries all 494 swaps in half a megabyte; `instType` is required
(HTTP 400, code `50014`, without it); a comma-separated `instId` is refused as a parameter
error (HTTP 400, code `51000`), as is a lower-case one; and an id the exchange does not list
answers HTTP 200 with code `51001` and no rows. So an id is asked for on its own, an unknown
id is that id's failure and never the endpoint's, and a malformed one is refused per id too.
Every other answer — a throttle, a gateway error, a body that is not the API's envelope —
stops the call: nothing about the id was established by it. That includes `51000` under HTTP
200, which the exchange was seen to answer once, transiently, for a well-formed id in review
of this adapter (seven repeats answered code `0`): only the 400 marks an id malformed. A fault
below any answer — a refused connection, a timeout — stops the call too, with a network
remedy.

**The fields, as the recorded rows carry them.** A linear swap's row leaves `baseCcy` and
`quoteCcy` empty; the contract's own currency is `ctValCcy` (`BTC`) and the quote is the
second half of `uly` (`BTC-USDT`). So:

| row | definition |
|---|---|
| `ctValCcy` | `base_currency` |
| `uly`'s second half | `quote_currency` |
| `settleCcy` | `settlement_currency` |
| `ctVal` x `ctMult` | `multiplier`, the contract's size in the base currency |
| `tickSz` | `price_increment`, and the precision derived from it |
| `lotSz` | `size_increment` and `lot_size` |
| `minSz` | `min_quantity` |
| `listTime` | the day it listed: an id asked for as of an earlier day is not yet listed |

`ctMult` was `1` on every one of the 494 swaps the page carried, so the multiplier is
`ctVal` in practice; the product is taken so that a contract that states another is still
sized right. `ctType` must be `linear`: an inverse contract (`BTC-USD-SWAP`, margined and
settled in the coin) is refused by name, because kanso's notional is `qty x px x multiplier`
in the quote currency. `state` must be `live`: a suspended or not-yet-open contract is
refused by name. The listing is today's: a contract the exchange has delisted is not in it
and is unknown, and a definition resolved as of an earlier day carries the terms the
exchange lists today.

**Fees are zeroed here, on purpose.** The listing carries no fee rate, and the definition
states `maker_fee` and `taker_fee` as zero anyway rather than leaving them to a default: the
runner charges commission once, from the venue model the broker declares, and a definition
carrying a rate would be charged it again by the simulated venue on every fill.

**The measured fields are the provider's, the operator's override is the operator's.** They
reach `build` the way every reference provider's measured tick and lot do, as the fields
kanso supplies, and the core applies the entry's `override` over them — so a correction written in
`instruments.yaml` wins over what the exchange lists. A definition is dated the day it was
resolved as of, both `ts_event` and `ts_init`, as every reference definition is.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`nautilus_pyo3.get_okx_http_base_url(OKXRegion)` maps a region to its REST host (`config.py`
records the map and `kanso doctor` re-checks it), and the request is sent through
`nautilus_pyo3.HttpClient`, built once per client with a `User-Agent` default header and the
table's rate as its `default_quota`; its `request` is a coroutine that must be created inside
a running loop, and resolves to a response carrying `status` and `body`. The exchange's edge
refuses a request whose User-Agent is the standard library's default before the API answers:
HTTP 403, body `error code: 1010`, recorded on 2026-09-30.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol
from urllib.parse import urlsplit

from kanso import __version__
from kanso.data.instruments import (
    LINEAR,
    NOT_YET_LISTED,
    UNKNOWN,
    InstrumentProvider,
    ResolveError,
    build,
)
from kanso.data.registry import Reach, Survey
from kanso.errors import Exit, KansoError, ValidationError
from kanso.nautilus.adapters.okx.config import ID, Region, table
from kanso.nautilus.adapters.okx.venue import VENUE, instrument_id
from kanso.schemas import InstrumentEntry

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Callable

    from kanso.data.loader import Loader
    from kanso.workspace import Workspace

__all__ = [
    "ADAPTER",
    "INSTRUMENTS",
    "USER_AGENT",
    "Answer",
    "OkxReference",
    "PublicClient",
    "ReferenceAdapter",
    "Response",
    "Transport",
    "base_url",
    "definition",
    "pyo3_transport",
]

INSTRUMENTS: Final = "/api/v5/public/instruments"
"""The public listing. It needs no key and is asked for with none."""

SWAP: Final = "SWAP"
"""The listing's `instType` for a perpetual swap, the only kind this package trades."""

OK: Final = "0"
NOT_LISTED: Final = "51001"
BAD_PARAMETER: Final = "51000"
"""The exchange's own codes, measured: success, an id it does not list (under HTTP 200),
and a parameter it rejects (under HTTP 400)."""

LIVE: Final = "live"
LINEAR_TYPE: Final = "linear"

USER_AGENT: Final = f"kanso/{__version__}"
"""Sent with every request: the exchange's edge refuses the standard library's default."""

TIMEOUT_S: Final = 30
DOWNLOAD_TIMEOUT_S: Final = 600
"""An API answer is small; a day's trade archive of a liquid swap is tens of megabytes."""

API: Final = "/api/"
"""Every path of the exchange's REST API begins here; anything else is its file host."""

ARCHIVES: Final = "/api/v5/public/market-data-history"
"""The listing of the exchange's daily history archives (`history.py`, `trades.py`)."""

KEYED_QUOTAS: Final[dict[str, int]] = {ARCHIVES: 1}
"""Paths metered on a quota of their own, in requests per second, below the table's rate.

Measured on 2026-09-30 against `us.okx.com`: the archive listing answered HTTP 429, code
`50011`, to every second request sent half a second apart, to three of eight sent a second
apart through this client on that quota, and to none of six sent two seconds apart. The
engine's quota admits a burst as large as its rate, so one a second — with a burst of one —
is the slowest it can state, and `okx_trades` also pauses two seconds before every listing
request it sends, which the quota cannot say."""

ASSET_CLASS: Final = "perpetuals"
DATASET: Final = "reference"
DATASETS: Final = (DATASET, "bars", "trades", "funding")
"""The reference, then one dataset per public-history loader (`history.py`)."""


# --- the wire -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Response:
    """One HTTP response, reduced to what this adapter reads."""

    status: int
    body: bytes = b""


class Transport(Protocol):
    """How a GET is sent: injectable, so the suite serves recorded bodies and no socket."""

    def __call__(self, url: str, params: Mapping[str, str]) -> Response: ...


def pyo3_transport(rate_per_second: int, *, factory: Any = None) -> Transport:
    """A transport over one rate-limited engine HTTP client, built once and closed over.

    The quota lives in the client, so a client per request would be no quota at all.
    `factory` exists so the suite drives the same coroutine plumbing without a socket.

    A request to a path `KEYED_QUOTAS` names is sent under that path's own key as well, so
    the endpoint that throttles hardest is metered on its own; a request outside the API —
    an archive on the exchange's file host, tens of megabytes — is given `DOWNLOAD_TIMEOUT_S`
    rather than the API's `TIMEOUT_S`.
    """
    client = (factory or _http_client)(rate_per_second)

    def send(url: str, params: Mapping[str, str]) -> Response:
        from nautilus_trader.core import nautilus_pyo3

        path = urlsplit(url).path
        keys = [path] if path in KEYED_QUOTAS else None
        timeout = TIMEOUT_S if path.startswith(API) else DOWNLOAD_TIMEOUT_S

        async def once() -> Any:
            return await client.request(
                nautilus_pyo3.HttpMethod.GET,
                url,
                params=dict(params),
                keys=keys,
                timeout_secs=timeout,
            )

        answer = asyncio.run(once())
        return Response(status=int(answer.status), body=bytes(answer.body or b""))

    return send


def _http_client(rate_per_second: int) -> Any:
    """The engine's client: a User-Agent and a quota, and no other header of any kind."""
    from nautilus_trader.core import nautilus_pyo3

    return nautilus_pyo3.HttpClient(
        default_headers={"User-Agent": USER_AGENT},
        header_keys=[],
        keyed_quotas=[
            (path, nautilus_pyo3.Quota.rate_per_second(rate)) for path, rate in KEYED_QUOTAS.items()
        ],
        default_quota=nautilus_pyo3.Quota.rate_per_second(rate_per_second),
        timeout_secs=TIMEOUT_S,
    )


def base_url(region: Region) -> str:
    """The REST host the engine maps `region` to."""
    from nautilus_trader.core import nautilus_pyo3

    regions: Any = nautilus_pyo3.OKXRegion  # its stub omits `from_str`
    return str(nautilus_pyo3.get_okx_http_base_url(regions.from_str(region.value)))


@dataclass(frozen=True, slots=True)
class Answer:
    """One answer of the listing: the HTTP status, the exchange's code and message, rows."""

    status: int
    code: str | None
    message: str
    rows: tuple[Mapping[str, Any], ...] = ()
    data: tuple[Any, ...] = ()
    """Every element of the envelope's `data`, as served: an endpoint whose rows are arrays
    rather than objects — the candles — is read from here, and `rows` keeps the objects."""

    @property
    def listed(self) -> bool:
        """The listing answered, with rows or without."""
        return self.status == 200 and self.code == OK

    @property
    def not_listed(self) -> bool:
        """The exchange lists no instrument under the id asked for."""
        return self.code == NOT_LISTED

    @property
    def malformed(self) -> bool:
        """The exchange rejected the id asked for as a parameter."""
        return self.status == 400 and self.code == BAD_PARAMETER

    def said(self) -> str:
        """What came back, in one phrase and without the body."""
        code = "" if self.code is None else f", code {self.code}"
        message = f": {self.message[:120]}" if self.message else ""
        return f"HTTP {self.status}{code}{message}"


def _answer(response: Response) -> Answer:
    """The response read as the API's envelope, or as an answer that is not one."""
    try:
        parsed = json.loads(response.body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, Mapping) or "code" not in parsed:
        text = response.body.decode("utf-8", "replace").strip()
        return Answer(response.status, None, text)
    data = tuple(parsed["data"]) if isinstance(parsed.get("data"), list) else ()
    rows = tuple(row for row in data if isinstance(row, Mapping))
    return Answer(response.status, str(parsed["code"]), str(parsed.get("msg") or ""), rows, data)


@dataclass(frozen=True, slots=True)
class PublicClient:
    """The public API on one regional host, through one transport, with no credential."""

    base_url: str
    transport: Transport

    def swaps(self, inst_id: str | None = None) -> Answer:
        """The swap listing, narrowed to one `instId` when one is given."""
        params = {"instType": SWAP}
        if inst_id is not None:
            params["instId"] = inst_id
        return self.get(INSTRUMENTS, params)

    def get(self, path: str, params: Mapping[str, str]) -> Answer:
        """One public endpoint's answer, read as the API's envelope."""
        return _answer(self.fetch(f"{self.base_url}{path}", params, name=path))

    def fetch(
        self, url: str, params: Mapping[str, str] | None = None, *, name: str = ""
    ) -> Response:
        """One GET of `url` as it came back, or a stop when nothing came back at all."""
        try:
            return self.transport(url, dict(params or {}))
        except Exception as exc:  # every fault below the answer is one outcome
            raise KansoError(
                f"okx: {name or url} could not be reached ({type(exc).__name__})",
                Exit.ERROR,
                remedy="check the network and the exchange's status page, then re-run",
            ) from exc


def _unanswered(answer: Answer) -> KansoError:
    return KansoError(
        f"okx: {INSTRUMENTS} did not answer as the exchange's API does ({answer.said()})",
        Exit.ERROR,
        remedy=(
            "re-run the command; if it repeats, lower `rate_per_second` in [adapters.okx] or "
            "check the exchange's status page"
        ),
    )


# --- one row ------------------------------------------------------------------


def definition(row: Mapping[str, Any], as_of: date) -> object | str:
    """The instrument one listing row describes as of `as_of`, or why it describes none."""
    inst = str(row.get("instId", ""))
    kind = row.get("ctType")
    if kind != LINEAR_TYPE:
        return (
            f"{inst} is an {kind or 'unstated'} contract (ctType {kind!r}, settled in "
            f"{row.get('settleCcy') or 'an unstated currency'}); {LINEAR} — trade the "
            "linear contract instead"
        )
    state = row.get("state")
    if state != LIVE:
        return (
            f"{inst} is {state or 'in no stated state'} on the exchange, not live; a "
            "definition is resolved only for a contract that trades"
        )
    listed = _day(row.get("listTime"))
    if listed is not None and listed > as_of:
        return f"{NOT_YET_LISTED} {as_of}: it was listed {listed}"
    fields = _fields(row)
    if isinstance(fields, str):
        return f"{UNKNOWN}: {fields}"
    entry = InstrumentEntry(
        nautilus_id=instrument_id(inst),
        asset_class="CRYPTOCURRENCY",
        corporate_actions="none",
        override={"instrument_class": "swap"},
    )
    try:
        return build(entry, {**fields, "ts_event": as_of, "ts_init": as_of})
    except ValidationError as exc:
        return f"{UNKNOWN}: {exc.message}"


def _fields(row: Mapping[str, Any]) -> dict[str, object] | str:
    """The constructor fields a linear row states, or the first one it leaves out."""
    stated = {
        name: row.get(name)
        for name in ("ctValCcy", "uly", "settleCcy", "ctVal", "ctMult", "tickSz", "lotSz")
    }
    missing = [name for name, value in stated.items() if not value]
    if missing:
        return f"the exchange's row states no {missing[0]}"
    _, _, quote = str(stated["uly"]).partition("-")
    if not quote:
        return f"the exchange's row names no quote currency in uly {stated['uly']!r}"
    try:
        multiplier = Decimal(str(stated["ctVal"])) * Decimal(str(stated["ctMult"]))
    except InvalidOperation:
        return f"ctVal {stated['ctVal']!r} x ctMult {stated['ctMult']!r} is not a number"
    fields: dict[str, object] = {
        "base_currency": stated["ctValCcy"],
        "quote_currency": quote,
        "settlement_currency": stated["settleCcy"],
        "multiplier": multiplier,
        "price_increment": stated["tickSz"],
        "size_increment": stated["lotSz"],
        "lot_size": stated["lotSz"],
        "maker_fee": "0",
        "taker_fee": "0",
    }
    if row.get("minSz"):
        fields["min_quantity"] = row["minSz"]
    return fields


def _day(millis: object) -> date | None:
    """A listing time — milliseconds since the epoch, as text — as a UTC day.

    One that is not a number, or that no calendar holds, is no listing time at all.
    """
    try:
        return datetime.fromtimestamp(int(str(millis)) / 1000, tz=UTC).date()
    except (ValueError, OverflowError, OSError):
        return None


# --- the provider ---------------------------------------------------------------


class OkxReference(InstrumentProvider):
    """Resolves `<instId>.OKX`, or a bare `instId`, one public request per id."""

    id: ClassVar[str] = ID

    def __init__(self, client: PublicClient) -> None:
        self._client = client
        self._seen: dict[str, str] = {}

    def resolve(self, ids: Sequence[str], as_of: date) -> dict[str, object]:
        """A definition or a `ResolveError` for every id, keyed as it was asked."""
        return {wanted: self._one(wanted, as_of) for wanted in dict.fromkeys(ids)}

    def sources(self, instrument_id: str) -> dict[str, str]:
        """The exchange's own `instId` for an instrument this provider resolved."""
        found = self._seen.get(instrument_id)
        return {} if found is None else {self.id: found}

    def _one(self, wanted: str, as_of: date) -> object:
        inst, dot, venue = wanted.rpartition(".")
        if not dot:
            inst = wanted
        elif venue != VENUE:
            return ResolveError(
                wanted,
                f"{UNKNOWN}: it names the venue {venue}, and this provider resolves the "
                f"exchange's own instruments, on {VENUE}",
            )
        answer = self._client.swaps(inst)
        if answer.malformed:
            return ResolveError(
                wanted,
                f"{UNKNOWN}: the exchange rejected {inst!r} as an instrument id; its swaps "
                "are named like BTC-USDT-SWAP, in capitals",
            )
        if not (answer.listed or answer.not_listed):
            raise _unanswered(answer)
        row = next((one for one in answer.rows if one.get("instId") == inst), None)
        if row is None:
            return ResolveError(wanted, f"{UNKNOWN}: the exchange lists no perpetual swap {inst}")
        found = definition(row, as_of)
        if isinstance(found, str):
            return ResolveError(wanted, found)
        self._seen[wanted] = inst
        self._seen[instrument_id(inst)] = inst
        return found


# --- the adapter ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the exchange's public data offers: definitions of the listed perpetual swaps,
    and their bars, trade prints and realised funding."""

    def names(self) -> tuple[str, ...]:
        return DATASETS

    def payload(self) -> dict[str, object]:
        return {
            "classes": [
                {"asset_class": ASSET_CLASS, "datasets": list(DATASETS), "grain": "endpoint"}
            ],
            "datasets": list(DATASETS),
            "credential": "none: the listing and the history are public",
        }


@dataclass(frozen=True, slots=True)
class ReferenceAdapter:
    """The registry's entry point for the exchange's public data: no credential, one table."""

    id: str = ID
    kind: str = "data"
    capabilities: Capabilities = Capabilities()
    credentials: tuple[str, ...] = ()

    def client(self, ws: Workspace, *, transport: Transport | None = None) -> PublicClient:
        """The listing on the table's regional host, refusing a table that states none.

        `transport` is how the suite serves recorded bodies: nothing else passes one.
        """
        settings = table(ws)
        return PublicClient(
            base_url=base_url(settings.require_region()),
            transport=transport or pyo3_transport(settings.rate_per_second),
        )

    def configured(self, ws: Workspace) -> bool:
        """Whether `[adapters.okx]` is present and names a host: the listing needs no key.

        The broker's model accepts the table with no region, so a table can be present and
        still give this adapter nowhere to send a request; a probe passes it by as
        unconfigured rather than stopping on it, and a resolution through `[data] reference`
        still refuses it by name in `client`.
        """
        return self.id in ws.config.adapters and table(ws).region is not None

    def credential_origins(self, ws: Workspace) -> dict[str, str | None]:
        """None to report: nothing this adapter sends is a credential."""
        return {}

    def quota(self, ws: Workspace) -> str:
        """The rate the table allows kanso's own public requests."""
        return f"{table(ws).rate_per_second}/s"

    def loaders(self, ws: Workspace) -> dict[str, Callable[[], Loader]]:
        """The public-history loaders, as factories: listing them builds none and sends nothing."""
        from kanso.nautilus.adapters.okx.history import loaders

        return loaders(ws)

    def provider(self, ws: Workspace, *, transport: Transport | None = None) -> OkxReference:
        """The instrument provider `[data] reference = "okx"` names."""
        return OkxReference(self.client(ws, transport=transport))

    def survey(self, ws: Workspace, *, transport: Transport | None = None) -> Survey:
        """One public request, the swap listing: does the host answer, and what it lists.

        A survey's `reachable` is a credential's verdict, and this adapter sends none, so a
        host that does not answer — the edge's 403, a throttle, a gateway error — is never
        reported as `reachable: false`: it raises, as `resolve` does, and the probe that
        asked reports the call's failure with a network remedy instead of blaming a key.
        """
        answer = self.client(ws, transport=transport).swaps()
        if not answer.listed:
            raise _unanswered(answer)
        linear = [
            row
            for row in answer.rows
            if row.get("ctType") == LINEAR_TYPE and row.get("state") == LIVE
        ]
        reach = Reach(
            asset_class=ASSET_CLASS,
            dataset=DATASET,
            grain="endpoint",
            outcome="ok",
            detail=f"{len(linear)} live linear swaps of {len(answer.rows)} listed",
        )
        return Survey(
            adapter=self.id,
            reachable=True,
            detail="the public listing answered; no credential was sent",
            requests=1,
            reach=(reach,),
        )


ADAPTER: Final = ReferenceAdapter()
"""The registered instance. Building one costs nothing and sends nothing."""
