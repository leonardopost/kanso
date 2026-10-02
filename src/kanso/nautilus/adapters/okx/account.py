"""The real account's own terms, read with its key: its fee tier, its mode, its position mode.

`kanso doctor --check-adapters` is the one caller, and this module is the only place kanso
sends a credential of this exchange. It reads the account the real client `okx` opens and
sets what the account pays against what the workspace charges a fill on `OKX`.

**Why the tier is read.** The declaration (`venue.py`) is the exchange's global Regular
tier, and an account belongs to a regional entity whose own Regular tier can differ. Measured
with the operator's read-only key on `us.okx.com`: on 2026-09-30, the account in spot mode
(`acctLv` 1), `GET /api/v5/account/trade-fee?instType=SWAP` answered level `Lv1`, maker
`-0.0002`, taker `-0.0005` — the global figures; on 2026-10-01, the account in futures mode
(`acctLv` 2), it answered maker `-0.0005` and taker `-0.0007` on `maker`/`taker`,
`makerU`/`takerU` and `makerUSDC`/`takerUSDC` alike, level `Lv1`, and the Australian entity's
fee page showed Futures 0.0500 % maker and 0.0700 % taker. A workspace costed on the
declaration charged that account 3 bp too little on a fill that rested and 2 bp on one that
took. Nothing in a card can see that, so the account is asked.

**What is read.** Two signed GETs on the host `[adapters.okx]` names. The fee tier,
`/api/v5/account/trade-fee?instType=SWAP`: the USDT-margined `takerU` and `makerU`, or
`taker` and `maker` when either of those is not a number, and the fee `level`. The account
configuration, `/api/v5/account/config`: `acctLv` and `posMode`. The exchange signs a fee the
account pays as negative and a rebate as positive, so a rate in basis points is the answer's
negation times 10,000, and a rebate is a negative `maker_bps`. The two reads are sent
whatever the first answers, so a check always costs two requests once anything is sent.

**What it is compared with.** The declaration with any `venues.OKX.costs` in
`portfolio.yaml` over it, field by field, as a venue model merges them: the rate a fill on
`OKX` is charged before a hypothesis's own `costs`. Equal is `ok`. An account that pays
more than is charged on either side fails — every card is costed below what the account
pays — and one that pays less on both warns, since charging more is conservative and may be
a stress the operator meant; both print the lines to state. Nothing is ever written for the
operator. An account in spot mode fails whatever it answers: it trades no perpetual, and
the rates a spot-mode account answers for swaps were measured to change when its mode did,
so no line is offered from them.

**What is sent, and what never is.** The real account's three names are resolved with
`kanso.creds.require` at the moment of use — never through the engine's environment helper
or its `OKX_*` names. The key and the passphrase travel only in the signed headers
`OK-ACCESS-KEY` and `OK-ACCESS-PASSPHRASE`, beside `OK-ACCESS-SIGN` and
`OK-ACCESS-TIMESTAMP`, and the secret travels nowhere: it keys the signature, the base64
HMAC-SHA256 of the timestamp, the method and the path with its query. No credential is in a
url, a detail, an item or a remedy. With any of the three unset, or no region stated, nothing
is sent and the declaration is still reported. The demo account is not read: its money is
simulated.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`nautilus_pyo3.HttpClient.request(method, url, params=None, headers=None, body=None,
keys=None, timeout_secs=None)` sends a url carrying its own query string unchanged when no
`params` are given, and the request's `headers` beside the client's defaults — measured on a
loopback server — so the path the signature covers is the path the exchange receives.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Protocol

from kanso import creds
from kanso.errors import Exit, KansoError, PreconditionError
from kanso.nautilus.adapters import AccountCheck
from kanso.nautilus.adapters.okx.config import ID, LIVE_CLIENT, credential_names, table
from kanso.nautilus.adapters.okx.reference import (
    QUOTA_KEY,
    TIMEOUT_S,
    Answer,
    Response,
    _answer,
    _http_client,
    base_url,
)
from kanso.nautilus.adapters.okx.venue import COMMISSION_BPS, MAKER_BPS, VENUE

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import VenueOverride
    from kanso.workspace import Workspace

__all__ = [
    "CONFIG",
    "TRADE_FEE",
    "SignedTransport",
    "check",
    "signed",
    "signed_transport",
    "stamp",
]

TRADE_FEE: Final = "/api/v5/account/trade-fee?instType=SWAP"
CONFIG: Final = "/api/v5/account/config"
"""The two reads, each with its query, as the signature covers them."""

RATES: Final = (("takerU", "makerU"), ("taker", "maker"))
"""Where the fee row states the USDT-margined rates, and where it is read when it does not."""

SPOT_MODE: Final = "1"
MODES: Final[dict[str, str]] = {
    "1": "spot mode",
    "2": "futures mode",
    "3": "multi-currency margin",
    "4": "portfolio margin",
}
"""`acctLv`, as the exchange documents it. Spot mode trades no perpetual."""

BP: Final = Decimal(10_000)

FIELDS: Final = ("commission_bps", "maker_bps")
"""The two rates a fee tier settles: the one a fill that takes pays, and one that rests."""


class SignedTransport(Protocol):
    """How a signed GET is sent: injectable, so the suite serves recorded bodies."""

    def __call__(self, url: str, headers: Mapping[str, str]) -> Response: ...


def stamp(moment: datetime) -> str:
    """An instant as the exchange signs it: UTC, ISO 8601, to the millisecond, `Z`."""
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def signed(key: str, secret: str, passphrase: str, path: str, moment: datetime) -> dict[str, str]:
    """The headers one GET of `path` — its query included — is sent under at `moment`."""
    timestamp = stamp(moment)
    digest = hmac.new(secret.encode(), f"{timestamp}GET{path}".encode(), hashlib.sha256)
    return {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": base64.b64encode(digest.digest()).decode(),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }


def signed_transport(rate_per_second: int, *, factory: Any = None) -> SignedTransport:
    """A signed transport over the engine client the public reference builds — a
    User-Agent and the table's quota, under `QUOTA_KEY` — with each request's own headers.

    The url carries its query and no `params` are passed, so nothing reorders what was
    signed. `factory` exists so the suite drives the plumbing without a socket.
    """
    client = (factory or _http_client)(rate_per_second)

    def send(url: str, headers: Mapping[str, str]) -> Response:
        from nautilus_trader.core import nautilus_pyo3

        async def once() -> Any:
            return await client.request(
                nautilus_pyo3.HttpMethod.GET,
                url,
                headers=dict(headers),
                keys=[QUOTA_KEY],
                timeout_secs=TIMEOUT_S,
            )

        answer = asyncio.run(once())
        return Response(status=int(answer.status), body=bytes(answer.body or b""))

    return send


def check(
    ws: Workspace,
    override: VenueOverride | None,
    *,
    transport: SignedTransport | None = None,
    now: Callable[[], datetime] | None = None,
) -> AccountCheck:
    """The real account's tier against the declaration with `override` over it, graded.

    `override` is the workspace's `venues.OKX` entry, or `None`. `transport` and `now` are
    how the suite serves recorded answers at a pinned instant; nothing else passes them.
    """
    charged = _charged(override)
    items = [
        f"{ID}: declares taker {_bp(COMMISSION_BPS)} bp and maker {_bp(MAKER_BPS)} bp on "
        f"{VENUE}, the exchange's global Regular tier; an entity's own Regular tier can differ",
        _stated(override),
        f"{ID}: the declaration and portfolio.yaml charge {_pair(charged)}",
    ]
    names = credential_names(LIVE_CLIENT)
    origins = {name: creds.origin(name, ws.root) for name in names}
    if None in origins.values():
        resolved = ", ".join(f"{name}={origin or 'unset'}" for name, origin in origins.items())
        items.append(
            f"{ID}: the account's own tier was not read and no request was made: {resolved}"
        )
        return AccountCheck(ID, "ok", f"{ID}'s account was not read: no key", items=tuple(items))
    settings = table(ws)
    try:
        region = settings.require_region()
    except PreconditionError as refusal:
        items.append(f"{ID}: the account's own tier was not read: {refusal.message}")
        return AccountCheck(
            ID,
            "warn",
            f"{ID}'s account was not read: [adapters.{ID}] states no region",
            items=tuple(items),
            remedy=refusal.remedy,
        )
    key, secret, passphrase = (creds.require(name, ws.root) for name in names)
    send = transport or signed_transport(settings.rate_per_second)
    clock = now or (lambda: datetime.now(tz=UTC))
    host = base_url(region)
    answers = {
        path: _get(send, f"{host}{path}", path, signed(key, secret, passphrase, path, clock()))
        for path in (TRADE_FEE, CONFIG)
    }
    return _graded(answers, charged, items)


# --- internals ---------------------------------------------------------------------


def _get(send: SignedTransport, url: str, path: str, headers: Mapping[str, str]) -> Answer:
    """One signed answer read as the API's envelope, or a stop when nothing came back."""
    try:
        return _answer(send(url, headers))
    except Exception as exc:  # every fault below the answer is one outcome
        raise KansoError(
            f"{ID}: {path} could not be reached ({type(exc).__name__})",
            Exit.ERROR,
            remedy="check the network and the exchange's status page, then re-run "
            "`kanso doctor --check-adapters`",
        ) from exc


def _graded(
    answers: Mapping[str, Answer], charged: Mapping[str, Decimal], items: list[str]
) -> AccountCheck:
    """The two answers read and set against what is charged, as one graded check."""
    sent = len(answers)
    for path, answer in answers.items():
        if not answer.listed:
            return AccountCheck(
                ID,
                "fail",
                f"{ID}'s account did not answer {path} as the exchange's API does "
                f"({answer.said()})",
                requests=sent,
                items=tuple(items),
                remedy=f"check {', '.join(credential_names(LIVE_CLIENT))}, and that "
                f"[adapters.{ID}] region names the host of the account's entity: another "
                "entity's host answers that the key does not exist",
            )
    fee = answers[TRADE_FEE].rows[0] if answers[TRADE_FEE].rows else {}
    config = answers[CONFIG].rows[0] if answers[CONFIG].rows else {}
    read = _rates(fee)
    if read is None:
        fields = ", or ".join(f"{taker} and {maker}" for taker, maker in RATES)
        return AccountCheck(
            ID,
            "fail",
            f"{ID}'s account answered {TRADE_FEE} with no readable rate in {fields}",
            requests=sent,
            items=tuple(items),
            remedy="re-run `kanso doctor --check-adapters`; if it repeats, the exchange has "
            "changed the answer and this check no longer reads it",
        )
    pays, where = read
    mode = str(config.get("acctLv") or "unstated")
    items += [
        f"{ID}: account fee level {fee.get('level') or 'unstated'} · acctLv {mode}, "
        f"{MODES.get(mode, 'a mode this check does not know')} · posMode "
        f"{config.get('posMode') or 'unstated'}",
        f"{ID}: the account's fee tier reads {_pair(pays)} on USDT-margined swaps ({where})",
    ]
    if mode == SPOT_MODE:
        return AccountCheck(
            ID,
            "fail",
            f"{ID}'s account is in spot mode (acctLv 1) and trades no perpetual",
            requests=sent,
            items=tuple(items),
            remedy="switch the account to futures mode on the exchange, then run `kanso doctor "
            "--check-adapters` again: a spot-mode account's answer for swaps need not be the "
            "rate it pays once it can trade them",
        )
    paying = (
        f"{ID}'s account pays taker {_bp(pays['commission_bps'])} bp and maker "
        f"{_bp(pays['maker_bps'])} bp"
    )
    charging = f"{_bp(charged['commission_bps'])} and {_bp(charged['maker_bps'])}"
    if pays == charged:
        return AccountCheck(
            ID,
            "ok",
            f"{ID}'s account pays what the declaration and portfolio.yaml charge",
            requests=sent,
            items=tuple(items),
        )
    items += [
        f"{ID}: state this in portfolio.yaml, merged into any venues.{VENUE} entry already there:",
        "  venues:",
        f"    {VENUE}:",
        "      costs:",
        *(f"        {name}: {_bp(pays[name])}" for name in FIELDS),
    ]
    remedy = (
        f"state the account's rates under venues.{VENUE}.costs in portfolio.yaml, as listed; "
        "a hypothesis that states its own commission_bps or maker_bps keeps charging it, so "
        "change it there too"
    )
    if any(pays[name] > charged[name] for name in FIELDS):
        return AccountCheck(
            ID,
            "fail",
            f"{paying}; the declaration and portfolio.yaml charge {charging}",
            requests=sent,
            items=tuple(items),
            remedy=remedy,
        )
    return AccountCheck(
        ID,
        "warn",
        f"{paying}, less than the declaration and portfolio.yaml charge ({charging})",
        requests=sent,
        items=tuple(items),
        remedy=remedy,
    )


def _overridden(override: VenueOverride | None) -> dict[str, float]:
    """The two rates the operator's `venues.OKX.costs` states, and only those it states."""
    stated = override.costs if override is not None else None
    return {} if stated is None else stated.model_dump(include=set(FIELDS), exclude_none=True)


def _charged(override: VenueOverride | None) -> dict[str, Decimal]:
    """The declared rates with the override's over them, field by field."""
    rates = {"commission_bps": COMMISSION_BPS, "maker_bps": MAKER_BPS} | _overridden(override)
    return {name: _decimal(rates[name]) for name in FIELDS}


def _stated(override: VenueOverride | None) -> str:
    """What the operator's `venues.OKX.costs` states of the two rates, as one line."""
    rates = _overridden(override)
    if not rates:
        return f"{ID}: portfolio.yaml states no venues.{VENUE}.costs rate"
    said = ", ".join(f"{name} {_bp(rates[name])}" for name in FIELDS if name in rates)
    return f"{ID}: portfolio.yaml venues.{VENUE}.costs states {said}"


def _rates(row: Mapping[str, Any]) -> tuple[dict[str, Decimal], str] | None:
    """The taker and maker rates a fee row states, in basis points, and where they were."""
    for taker, maker in RATES:
        took, rested = _bps(row.get(taker)), _bps(row.get(maker))
        if took is not None and rested is not None:
            return {"commission_bps": took, "maker_bps": rested}, f"{taker}, {maker}"
    return None


def _bps(text: object) -> Decimal | None:
    """A fee rate as the exchange signs it, in basis points as kanso does; `None` when the
    field is not a finite number."""
    try:
        rate = Decimal(str(text))
    except InvalidOperation:
        return None
    return Decimal(0) - rate * BP if rate.is_finite() else None


def _decimal(value: float) -> Decimal:
    """A rate as the operator wrote it, compared exactly rather than as a float."""
    return Decimal(str(value))


def _bp(value: Decimal | float) -> str:
    """A rate in basis points as an operator writes it: `7`, `0.5`, `-0.5`, `70`."""
    exact = value if isinstance(value, Decimal) else _decimal(value)
    return format(exact.normalize(), "f")


def _pair(rates: Mapping[str, Decimal]) -> str:
    return f"taker {_bp(rates['commission_bps'])} bp, maker {_bp(rates['maker_bps'])} bp"
