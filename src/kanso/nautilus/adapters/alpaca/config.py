"""Which account a credential belongs to, which host it may address, and how it is reached.

This module is where the milestone's money-safety begins, because it is where a key is
paired with a host. The broker runs two entirely separate environments — a paper one and
a real one — and a key belongs to exactly one of them. A paper key answers `401` on the
real host, and a mismatched pairing is therefore not a subtle mispricing but a broker
refusal in the middle of a trading session. So the pairing is settled here, before any
socket is opened, by two independent rules.

**The variable name names the account.** Under kanso's standard credential scheme the
subject is the id the consumer is configured under, so the paper execution client
`alpaca_paper` resolves `KANSO_ALPACA_PAPER_API_KEY` and `KANSO_ALPACA_PAPER_API_SECRET`
while the real-capital client `alpaca` resolves `KANSO_ALPACA_API_KEY` and
`KANSO_ALPACA_API_SECRET`. Two accounts, two pairs of names, no sharing: the client id
decides both which variables are read and which host they may address, so a key cannot
reach an account its variable does not name. Those are the only spellings this adapter
knows. It never falls back to any other name a particular machine happens to export,
because a fallback is how a key meant for one tool ends up trading through another.

**The key itself names the account too.** A paper key carries the prefix `PK`, which is
a fact about the key rather than about the request, so a key set in the wrong variable is
refused rather than sent: a `PK` key configured for the real host would be refused by the
broker anyway, and a key without that prefix configured for the paper host is not a paper
key at all. Both refusals name the variable and never its value — no credential reaches a
message, a log, a repr or a manifest from anywhere in this module.

**The feed has no default.** The consolidated tape and a single venue's slice of it are
different series for the same instrument on the same day, so a card researched on one and
traded on the other is not the same strategy. A data client therefore refuses to open on
an undeclared feed rather than picking one, and the declared feed travels with everything
the client produces.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`nautilus_trader.core.nautilus_pyo3.HttpClient(default_headers, header_keys,
keyed_quotas, default_quota, timeout_secs, proxy_url)` wraps `reqwest` with a rate
limiter. Its `request(method, url, params, headers, body, keys, timeout_secs)` is a
coroutine and must be created inside a running loop — building it outside one raises
`RuntimeError: no running event loop` — so the coroutine is constructed within the
function `asyncio.run` drives. It resolves to an `HttpResponse` carrying `status`,
`headers` and `body` (bytes). `Quota.rate_per_minute(n)` is the per-minute quota, which
is the grain this broker publishes its own limit at; `keys` names the rate-limit buckets
a request counts against, so a keyed quota can be added later without touching call
sites.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import Field

from kanso import creds
from kanso.errors import Exit, KansoError, PreconditionError
from kanso.schemas import ExecutionClientSpec
from kanso.schemas.base import KansoModel, NonEmpty

__all__ = [
    "ACCOUNTS",
    "CLIENTS",
    "DATA_HOST",
    "DATA_STREAM",
    "KEY_HEADER",
    "LIVE",
    "LIVE_CLIENT",
    "LIVE_HOST",
    "LIVE_STREAM",
    "PAPER",
    "PAPER_CLIENT",
    "PAPER_HOST",
    "PAPER_KEY_PREFIX",
    "PAPER_STREAM",
    "SECRET_HEADER",
    "Account",
    "AlpacaConfig",
    "Credentials",
    "Environment",
    "Feed",
    "Response",
    "Transport",
    "account",
    "buckets",
    "check_key",
    "credential_names",
    "document",
    "key_name",
    "pyo3_transport",
    "resolve",
    "secret_name",
    "endpoint",
]

PAPER_HOST: Final = "https://paper-api.alpaca.markets"
LIVE_HOST: Final = "https://api.alpaca.markets"
DATA_HOST: Final = "https://data.alpaca.markets"
"""The three hosts, measured. Trading is served per environment and market data from one
host for both, which is why the data client still declares which account's key it uses."""

PAPER_STREAM: Final = "wss://paper-api.alpaca.markets/stream"
LIVE_STREAM: Final = "wss://api.alpaca.markets/stream"
DATA_STREAM: Final = "wss://stream.data.alpaca.markets/v2"
"""The websocket origins, each the trading host's own with the stream path. These are the
broker's published endpoints and were not part of the read-only measurement, so they are
configurable rather than compiled in, and a first connection is where they are confirmed."""

KEY_HEADER: Final = "APCA-API-KEY-ID"
SECRET_HEADER: Final = "APCA-API-SECRET-KEY"
"""The two headers the credential travels in. Never a query parameter: a URL reaches proxy
logs, error reports and a manifest's recorded request, and a written credential is a
rotated credential."""

PAPER_KEY_PREFIX: Final = "PK"
"""The prefix a paper key carries. Measured, together with the `401` such a key gets from
the live host — which is why a mismatched pairing is refused here rather than discovered
in the middle of a session."""

SECRET_PURPOSE: Final = "API_SECRET"
"""The second credential's purpose under the standard scheme; the first takes the default."""

DEFAULT_REQUESTS_PER_MINUTE: Final = 190
"""The default quota, under the broker's published 200-a-minute ceiling so a burst never
trips it. An operator on a plan with a higher ceiling raises it in `[adapters.alpaca]`."""

DEFAULT_TIMEOUT_S: Final = 30

DEFAULT_POLL_INTERVAL_S: Final = 15.0
"""Seconds between the live feed's sweeps of the series a stage has subscribed. One request
per series per sweep, so this and the quota above together bound how many series a stage may
run, which is why a subscription past that bound is refused rather than silently throttled
into missing bars. It is a default and not a constant of the broker: a daily stage has no use
for fifteen seconds and a minute stage has no use for more, so an operator whose stage trades
a coarser bar widens it in `[adapters.alpaca]`."""

MIN_POLL_INTERVAL_S: Final = 1.0
MAX_POLL_INTERVAL_S: Final = 3600.0
"""The cadence an operator may state, bounded at both ends because outside them it stops
meaning anything. The finest bar this broker aggregates is a minute, so one second is already
sixty sweeps per bar and every sweep but the first spends quota on a window with nothing new
in it. And three consecutive failed sweeps is what stops a stage that has gone blind, so at
an hour that verdict takes three hours to reach — most of a session traded on prices the feed
had already stopped serving."""

USER_AGENT: Final = "kanso"


class Environment(StrEnum):
    """Which of the broker's two worlds an account, a key and a host belong to."""

    PAPER = "paper"
    LIVE = "live"


class Feed(StrEnum):
    """Which tape the market data client reads.

    `SIP` is the consolidated tape — every venue's prints — and `IEX` is one venue's slice
    of it. They are different series for the same instrument on the same day, so the choice
    is declared and recorded rather than defaulted.
    """

    SIP = "sip"
    IEX = "iex"


PAPER_CLIENT: Final = "alpaca_paper"
LIVE_CLIENT: Final = "alpaca"
"""The two client ids. They are the subjects of the credential scheme as well as the ids a
stage names, which is what keeps a variable and the account it opens in step."""


@dataclass(frozen=True, slots=True)
class Account:
    """One of the broker's two environments, and the execution client that trades it.

    `spec` is the declaration the core reasons about: how the capital is funded and which
    clock it runs on. Both accounts run on the wall clock, because a broker fills against
    current prices whatever data a stage is replaying.
    """

    environment: Environment
    spec: ExecutionClientSpec
    default_host: str
    default_stream: str

    @property
    def client_id(self) -> str:
        """The id this account is configured and credentialed under."""
        return self.spec.id


PAPER: Final = Account(
    environment=Environment.PAPER,
    spec=ExecutionClientSpec(id=PAPER_CLIENT, capital="broker_paper", clock="wall"),
    default_host=PAPER_HOST,
    default_stream=PAPER_STREAM,
)
"""The broker's paper account: real order handling, simulated money."""

LIVE: Final = Account(
    environment=Environment.LIVE,
    spec=ExecutionClientSpec(id=LIVE_CLIENT, capital="real", clock="wall"),
    default_host=LIVE_HOST,
    default_stream=LIVE_STREAM,
)
"""The real account. `capital: real` is what confines it to the live stage and puts a
recorded, named approval between a certified version and this client."""

ACCOUNTS: Final[dict[str, Account]] = {PAPER.client_id: PAPER, LIVE.client_id: LIVE}
CLIENTS: Final[tuple[str, ...]] = (PAPER_CLIENT, LIVE_CLIENT)
"""Every client id this adapter serves, paper first, in the order they are reported."""


def account(client_id: str) -> Account:
    """The account `client_id` names, or a refusal naming the ids there are."""
    found = ACCOUNTS.get(client_id)
    if found is None:
        raise PreconditionError(
            f"alpaca: {client_id!r} is not a client this broker provides; it provides "
            f"{', '.join(CLIENTS)}",
            remedy=f"name {PAPER_CLIENT} for the paper account or {LIVE_CLIENT} for the real one",
        )
    return found


def key_name(client_id: str) -> str:
    """The variable holding the key id of `client_id`, under the standard scheme."""
    return creds.standard_name(client_id)


def secret_name(client_id: str) -> str:
    """The variable holding the secret of `client_id`, under the standard scheme."""
    return creds.standard_name(client_id, SECRET_PURPOSE)


def credential_names(client_id: str) -> tuple[str, str]:
    """Both variable names of one client, derived rather than spelled out.

    Derived so the id a stage names and the variables an operator sets cannot drift apart,
    and so this adapter knows exactly two spellings per account and no others.
    """
    return key_name(client_id), secret_name(client_id)


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    """One account's key and secret, and the account they were resolved for.

    The values live here and travel to the headers; they reach nothing else. The `repr`
    is overridden because a `repr` reaches tracebacks, logs and crash reports, and a
    dataclass's generated one would print both values into all three.
    """

    account: Account
    key: str
    secret: str

    def __repr__(self) -> str:
        """Never the values: the account this opens, and nothing more."""
        return (
            f"Credentials(client_id={self.account.client_id!r}, "
            f"environment={self.account.environment.value!r})"
        )

    def headers(self) -> dict[str, str]:
        """The two headers every authenticated request carries."""
        return {KEY_HEADER: self.key, SECRET_HEADER: self.secret}


def check_key(key: str, found: Account, name: str) -> None:
    """Refuse a key that belongs to the other environment, before anything is sent.

    A paper key carries the prefix and a real key does not, so the check runs in both
    directions from that one fact: a paper key aimed at the live host would be refused by
    the broker with a `401`, and a key without the prefix aimed at the paper host is not
    that account's key at all. Neither the value nor its prefix appears in the message —
    the variable and the environment are what an operator needs to fix it.
    """
    text = key.strip()
    if not text:
        raise PreconditionError(
            f"{name} is set to a blank value, which is not a key",
            remedy=f"set {name} to the key id of the {found.environment.value} account",
        )
    paper = text.startswith(PAPER_KEY_PREFIX)
    if paper and found.environment is Environment.LIVE:
        raise PreconditionError(
            f"{name} holds a paper key, and the live host refuses one with HTTP 401; "
            f"a key belongs to one environment and {found.client_id!r} trades the real account",
            remedy=(
                f"set {name} to the live account's key, or configure the {PAPER_CLIENT} "
                "client, which reads the paper account's own variables"
            ),
        )
    if not paper and found.environment is Environment.PAPER:
        raise PreconditionError(
            f"{name} does not hold a paper key, and {found.client_id!r} trades the paper "
            "account; a paper key is the only key that account accepts",
            remedy=(
                f"set {name} to the paper account's key, or configure the {LIVE_CLIENT} "
                "client, which reads the live account's own variables"
            ),
        )


def resolve(workspace: Path, client_id: str) -> Credentials:
    """One client's credentials, refusing a key that does not belong to its account.

    Resolved at the moment of use from the workspace `.env` and then the process
    environment, exactly as every other credential in kanso is, and held no longer than
    the call chain that asked for them.
    """
    found = account(client_id)
    key_variable, secret_variable = credential_names(client_id)
    key = creds.require(key_variable, workspace)
    secret = creds.require(secret_variable, workspace)
    check_key(key, found, key_variable)
    if not secret.strip():
        raise PreconditionError(
            f"{secret_variable} is set to a blank value, which is not a secret",
            remedy=f"set {secret_variable} to the secret of the {found.client_id!r} account",
        )
    return Credentials(account=found, key=key, secret=secret)


class AlpacaConfig(KansoModel):
    """The `[adapters.alpaca]` table: where the broker is reached and which tape is read.

    Credentials never appear here — they are resolved from the environment at the moment
    of use — so this table is safe to print, to record and to commit. Every host is
    overridable because an operator behind a proxy has to be able to say so, and because
    the websocket origins were published rather than measured.

    The two rates are here for the same reason as the hosts: they are this account's, not
    this broker's. `requests_per_minute` is the ceiling the plan grants and
    `poll_interval_s` is how often the live feed spends it, and together they decide how
    many series a stage may subscribe — so an operator who cannot state the second cannot
    trade a daily strategy without sweeping it every fifteen seconds, nor a minute one
    without missing bars. Both are bounded, and a value outside the bounds is refused when
    the table is read rather than at the first sweep.
    """

    paper_url: NonEmpty = PAPER_HOST
    live_url: NonEmpty = LIVE_HOST
    data_url: NonEmpty = DATA_HOST
    paper_stream_url: NonEmpty = PAPER_STREAM
    live_stream_url: NonEmpty = LIVE_STREAM
    data_stream_url: NonEmpty = DATA_STREAM
    feed: Feed | None = None
    requests_per_minute: int = Field(default=DEFAULT_REQUESTS_PER_MINUTE, ge=1, le=100_000)
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=600)
    poll_interval_s: float = Field(
        default=DEFAULT_POLL_INTERVAL_S, ge=MIN_POLL_INTERVAL_S, le=MAX_POLL_INTERVAL_S
    )

    def host(self, client_id: str) -> str:
        """The trading host `client_id` addresses, which is its account's and no other."""
        found = account(client_id)
        return self.paper_url if found.environment is Environment.PAPER else self.live_url

    def stream(self, client_id: str) -> str:
        """The order-update stream of `client_id`'s own account."""
        found = account(client_id)
        return (
            self.paper_stream_url
            if found.environment is Environment.PAPER
            else self.live_stream_url
        )

    def data_stream(self, feed: Feed) -> str:
        """The market data stream of one tape, which is named in the path."""
        return endpoint(self.data_stream_url, feed.value)

    def require_feed(self) -> Feed:
        """The declared feed, refusing to guess one.

        There is no default because the two tapes are different series: one is every
        venue's prints and the other is a single venue's, and a strategy researched on one
        and traded on the other is a different strategy. A missing declaration is an
        operator's decision that has not been taken yet, not a value to invent.
        """
        if self.feed is None:
            raise PreconditionError(
                "[adapters.alpaca] feed: no market data feed is declared, and there is no "
                f"default; {Feed.SIP.value!r} is the consolidated tape and {Feed.IEX.value!r} "
                "is one venue's slice of it, and they are different series for the same day",
                remedy=(
                    'add `feed = "sip"` (or "iex") to the [adapters.alpaca] table in '
                    "kanso.toml, matching the tape the strategy was researched on"
                ),
            )
        return self.feed

    @property
    def quota(self) -> str:
        """The rate limit this workspace enforces, as `doctor` reports it."""
        return f"{self.requests_per_minute}/min"


def endpoint(base: str, path: str) -> str:
    """One absolute URL from a configured base and a path, with exactly one separator."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def buckets(path: str) -> list[str]:
    """The rate-limit buckets one request counts against, coarsest last."""
    parts = [part for part in path.split("/") if part][:2]
    return ["/".join(parts), parts[0]] if len(parts) > 1 else parts


@dataclass(frozen=True, slots=True)
class Response:
    """One HTTP response, reduced to what this adapter reads."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


def document(response: Response) -> Mapping[str, Any] | None:
    """The response body as one JSON object, or `None` when it is not one."""
    try:
        parsed = json.loads(response.body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


class Transport(Protocol):
    """How a request is actually sent.

    Injectable so every part of this adapter is tested against frozen response bodies with
    no network and no credential, and so the execution client, the data client and the
    tradability overlay share one rate-limited connection instead of opening three.

    `body` is optional because most of this adapter reads: a clock, a bar, an asset and a
    report are all query strings. An order is not — it is a POST whose fields travel in a
    JSON body — so the one connection every part of this adapter shares has to be able to
    carry one. A transport without it can read every report and then fail on the single
    request that moves money, which is why the execution client checks for the argument
    when it is built rather than discovering its absence mid-session.
    """

    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
        body: bytes | None = None,
    ) -> Response: ...


def pyo3_transport(
    *,
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    factory: Any = None,
) -> Transport:
    """A transport over one rate-limited engine HTTP client.

    The client is built once and closed over, because the quota lives in the client: a
    fresh client per request would be a fresh quota per request, which is no quota at all.
    That is also why this is the only place in the adapter that builds one — three callers
    with three clients would be three times the published limit. `factory` exists so the
    suite can drive the same coroutine plumbing without a socket; production passes nothing.
    """
    client = (factory or _http_client)(requests_per_minute, timeout_s)

    def send(
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
        body: bytes | None = None,
    ) -> Response:
        async def once() -> Any:
            return await client.request(
                _method(method),
                url,
                params=dict(params),
                headers=dict(headers),
                body=body,
                keys=list(keys),
            )

        answer = asyncio.run(once())
        return Response(
            status=int(answer.status),
            body=bytes(answer.body or b""),
            headers=dict(answer.headers or {}),
        )

    return send


def _http_client(requests_per_minute: int, timeout_s: int) -> Any:
    from nautilus_trader.core import nautilus_pyo3

    return nautilus_pyo3.HttpClient(
        default_headers={"User-Agent": USER_AGENT},
        header_keys=[],
        keyed_quotas=[],
        default_quota=nautilus_pyo3.Quota.rate_per_minute(requests_per_minute),
        timeout_secs=timeout_s,
    )


def _method(name: str) -> Any:
    from nautilus_trader.core import nautilus_pyo3

    found = getattr(nautilus_pyo3.HttpMethod, name.upper(), None)
    if found is None:
        raise KansoError(
            f"alpaca: {name} is not an HTTP method the engine's client sends",
            Exit.ERROR,
        )
    return found
