"""The Massive REST client: header auth, one quota, and a signal for every answer.

Three properties make this client what the rest of the adapter is built on.

**The key travels in a header.** Massive also accepts it in the query string; kanso never
sends it that way, because a query string reaches proxy logs, error reports and the
`request_params` a manifest records, and a credential that has been written to a file is
a credential that has to be rotated.

**One quota, enforced in the transport.** The rate limit belongs to the connection rather
than to any caller, so it lives in the `nautilus_pyo3.HttpClient` that every request goes
through, and a loader cannot forget it. There is no retry: a throttle means the quota is
set wrong, and a client that quietly retried would hide that.

**The client never decides what an answer means.** It reduces each response to a
`Signal` — rows, no rows, a refusal, a rejected request shape, or no answer at all — and
stops there. Meaning is `entitlement`'s to establish, by probing. The reason is that the
vendor states four quite different conditions with one byte-identical sentence, so any
code that read that sentence would be wrong a quarter of the time; `Call` therefore
carries the status, the machine-readable `status` field and the request id, and does not
carry the message at all, so nothing downstream can be tempted to parse it.

The transport is injectable, which is what lets the whole adapter be tested against
frozen response bodies with no network, no credential and no recorded secret. The same
transport serves the object store's probe path: a signed URL sent through it comes back
with a body, where `nautilus_pyo3.http_download` discards bodies and collapses every
refusal into one message.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`nautilus_trader.core.nautilus_pyo3.HttpClient(default_headers, header_keys,
keyed_quotas, default_quota, timeout_secs, proxy_url)` wraps `reqwest` with a rate
limiter. Its `request(method, url, params, headers, body, keys, timeout_secs)` is a
coroutine and must be created inside a running loop — building it outside one raises
`RuntimeError: no running event loop` — so the coroutine is constructed within the
function `asyncio.run` drives. It resolves to an `HttpResponse` carrying `status`,
`headers` and `body` (bytes), and raises `nautilus_pyo3.HttpError` when no response
arrives. `Quota.rate_per_second(n)` is the per-second quota; `keys` names the rate-limit
buckets a request counts against, so a keyed quota can be added later without touching
call sites.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol

from pydantic import Field

from kanso import __version__
from kanso.data.adapters.massive.errors import TransportError
from kanso.schemas.base import KansoModel, NonEmpty

__all__ = [
    "API_HOST",
    "ANSWERED",
    "Call",
    "MassiveClient",
    "MassiveConfig",
    "Response",
    "Signal",
    "Transport",
    "pyo3_transport",
    "signal_of",
]

API_HOST: Final = "https://api.massive.com"
"""Where the REST API lives. `[adapters.massive] base_url` overrides it for a proxy."""

USER_AGENT: Final = f"kanso/{__version__}"

AUTH_HEADER: Final = "Authorization"
AUTH_SCHEME: Final = "Bearer"
"""The header the key travels in and the scheme it travels under; never a query
parameter, where it would reach proxy logs and a manifest's recorded request."""

DEFAULT_REQUESTS_PER_SECOND: Final = 90
"""The default quota: below the plan's published ceiling, so a burst never trips it."""

DEFAULT_TIMEOUT_S: Final = 30

MAX_PAGES: Final = 1_000
"""Pages one cursor walk may fetch. A universe page is a thousand rows, so this is a
million rows: far past any legitimate answer, and a cheap guard against a cursor loop."""

OK_STATUS: Final = frozenset({"OK", "DELAYED", "SUCCESS"})
"""The values of the response's machine-readable `status` field that are not a refusal.

`DELAYED` is one of them: a delayed answer is served data, and how late a point became
public is a publication question the loader settles, not an entitlement question."""


class Signal(StrEnum):
    """What one response was, at the wire, before anybody decided what it meant."""

    ROWS = "rows"
    NO_ROWS = "no_rows"
    REFUSED = "refused"
    BAD_REQUEST = "bad_request"
    THROTTLED = "throttled"
    UNAVAILABLE = "unavailable"
    UNREADABLE = "unreadable"


ANSWERED: Final[frozenset[Signal]] = frozenset(
    {Signal.ROWS, Signal.NO_ROWS, Signal.REFUSED, Signal.BAD_REQUEST}
)
"""The signals that are the source's answer. The rest are a failure to obtain one."""


class MassiveConfig(KansoModel):
    """The `[adapters.massive]` table: how to reach the vendor, never a credential.

    Credentials are resolved from the environment at the moment of use and never appear
    in a workspace file, so nothing here holds one.
    """

    base_url: NonEmpty = API_HOST
    requests_per_second: int = Field(default=DEFAULT_REQUESTS_PER_SECOND, ge=1, le=10_000)
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=600)


@dataclass(frozen=True, slots=True)
class Response:
    """One HTTP response, reduced to what this adapter reads."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """How a request is actually sent.

    Injectable so the adapter's tests replay frozen response bodies with no network, and
    so the object store can send a signed URL through the same rate-limited connection.
    """

    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
    ) -> Response: ...


def pyo3_transport(
    *,
    requests_per_second: int = DEFAULT_REQUESTS_PER_SECOND,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    factory: Any = None,
) -> Transport:
    """A transport over one rate-limited `nautilus_pyo3.HttpClient`.

    The client is built once and closed over, because the quota lives in the client: a
    fresh client per request would be a fresh quota per request, which is no quota at
    all. `factory` exists so a test can drive the same coroutine plumbing without a
    socket; production passes nothing.
    """
    client = (factory or _http_client)(requests_per_second, timeout_s)

    def send(
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
    ) -> Response:
        async def once() -> Any:
            return await client.request(
                _method(method),
                url,
                params=dict(params),
                headers=dict(headers),
                body=None,
                keys=list(keys),
            )

        answer = asyncio.run(once())
        return Response(
            status=int(answer.status),
            body=bytes(answer.body or b""),
            headers=dict(answer.headers or {}),
        )

    return send


def _http_client(requests_per_second: int, timeout_s: int) -> Any:
    from nautilus_trader.core import nautilus_pyo3

    return nautilus_pyo3.HttpClient(
        default_headers={"User-Agent": USER_AGENT},
        header_keys=[],
        keyed_quotas=[],
        default_quota=nautilus_pyo3.Quota.rate_per_second(requests_per_second),
        timeout_secs=timeout_s,
    )


def _method(name: str) -> Any:
    from nautilus_trader.core import nautilus_pyo3

    found = getattr(nautilus_pyo3.HttpMethod, name.upper(), None)
    if found is None:
        raise TransportError(f"{name} is not an HTTP method the engine's client sends")
    return found


def signal_of(status: int, body: Mapping[str, Any] | None) -> Signal:
    """The signal a response carries, from its status and its parsed body — never its text.

    The one thing read out of the body is the machine-readable `status` field, whose
    values divide into those that carry data and those that refuse. The human-readable
    message beside it is deliberately ignored: it is identical for a dataset the plan
    excludes, a range older than the plan's window, a wrong asset-class prefix and an
    unrecognised key, and reading it would collapse four outcomes into one.
    """
    if status == 429:
        return Signal.THROTTLED
    if status in (401, 403):
        return Signal.REFUSED
    if status >= 500:
        return Signal.UNAVAILABLE
    if 400 <= status < 500:
        return Signal.BAD_REQUEST
    if not 200 <= status < 300:
        return Signal.UNAVAILABLE
    if body is None:
        return Signal.UNREADABLE
    vendor = body.get("status")
    if isinstance(vendor, str) and vendor.upper() not in OK_STATUS:
        return Signal.REFUSED
    return Signal.ROWS if _rows(body) else Signal.NO_ROWS


@dataclass(frozen=True, slots=True)
class Call:
    """One request and what came back, in the terms the rest of the adapter reasons in.

    `rows` is the `results` array normalised to a tuple, so an endpoint that answers with
    a single object and one that answers with a list are read the same way. `body` is the
    whole parsed document, because a loader needs the fields beside the results; what may
    be *reported* is `evidence`, and the vendor's message is not in it — see `signal_of`.
    """

    path: str
    params: tuple[tuple[str, str], ...]
    status: int
    signal: Signal
    rows: tuple[Mapping[str, Any], ...] = ()
    body: Mapping[str, Any] = field(default_factory=dict)
    next_url: str | None = None
    vendor_status: str | None = None
    request_id: str | None = None

    @property
    def answered(self) -> bool:
        """True when the source answered, whatever the answer was."""
        return self.signal in ANSWERED

    def evidence(self) -> dict[str, object]:
        """What this call proves, for a probe to record and an operator to read.

        Carries no credential — the key is a header and headers are not here — and no
        vendor prose, so a reader is never handed a sentence that means four things.
        """
        return {
            "path": self.path,
            "params": dict(self.params),
            "status": self.status,
            "signal": str(self.signal),
            "vendor_status": self.vendor_status,
            "request_id": self.request_id,
            "rows": len(self.rows),
        }

    def raise_for_transport(self) -> None:
        """Fail when no answer arrived, leaving every answer to the caller."""
        if self.answered:
            return
        raise TransportError(
            f"massive: {self.path} did not answer ({self.signal}, HTTP {self.status})",
            remedy=(
                "re-run the command; if it repeats, lower `[adapters.massive] "
                "requests_per_second` or check the vendor's status page"
            ),
            status=self.status,
        )


class MassiveClient:
    """A rate-limited, header-authenticated reader of the Massive REST API.

    Holds the key for the life of the call chain and puts it nowhere else: not in a
    query string, not in a `repr`, not in a `Call`. Stateless otherwise, so two clients
    built from the same credential answer identically and a loader may build one per
    command without consequence.
    """

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport,
        base_url: str = API_HOST,
        requests_per_second: int = DEFAULT_REQUESTS_PER_SECOND,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self.requests_per_second = requests_per_second

    def __repr__(self) -> str:
        """Never the key: a repr reaches tracebacks, logs and crash reports."""
        return f"MassiveClient(base_url={self.base_url!r}, quota={self.quota!r})"

    @property
    def quota(self) -> str:
        """The rate limit, as `data adapters` and `doctor` report it."""
        return f"{self.requests_per_second}/s"

    @property
    def transport(self) -> Transport:
        """The rate-limited connection itself, for a request this client cannot sign.

        An object-store request carries its own signature over its own header set and
        must not also carry this client's, so it goes through the transport rather than
        through `call`. Borrowing the same one keeps both paths under one quota, and it
        is what lets a signed request be read with a body at all: the engine's bulk
        download discards bodies, and a refusal with no body is four conditions again.
        """
        return self._transport

    def headers(self) -> dict[str, str]:
        """The headers every request carries, including the credential."""
        return {
            AUTH_HEADER: f"{AUTH_SCHEME} {self._api_key}",
            "Accept": "application/json",
        }

    def call(self, path: str, params: Mapping[str, str] | None = None) -> Call:
        """One request, reduced to a `Call`. Answers and refusals both return normally.

        Only the absence of an answer raises, and only when the transport itself fails:
        a refusal is data a probe needs, so it comes back rather than being thrown.
        """
        asked = tuple(sorted((params or {}).items()))
        url = self._url(path)
        try:
            response = self._transport("GET", url, dict(asked), self.headers(), self._keys(path))
        except TransportError:
            raise
        except Exception as exc:  # every fault below the answer is one outcome
            raise TransportError(
                f"massive: {path} could not be reached ({type(exc).__name__})",
                remedy="check the network and the vendor's status page, then re-run",
            ) from exc
        return _read(path, asked, response)

    def pages(
        self, path: str, params: Mapping[str, str] | None = None, *, max_pages: int = MAX_PAGES
    ) -> Iterator[Call]:
        """Every page of a cursor walk, stopping at the first page that is not rows.

        A page that did not answer fails the walk rather than ending it: a timeout half
        way through a universe looks exactly like the end of the universe, and a loader
        that mistook one for the other would record a short span as a complete one.
        """
        seen: set[str] = set()
        page = self.call(path, params)
        for _ in range(max_pages):
            page.raise_for_transport()
            yield page
            following = page.next_url
            if following is None or page.signal is not Signal.ROWS or following in seen:
                return
            seen.add(following)
            page = self.call(following)
        raise TransportError(
            f"massive: {path} returned more than {max_pages} pages, which is a cursor that "
            "does not end",
            remedy="narrow the request's range or filters",
        )

    def rows(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Iterator[Mapping[str, Any]]:
        """The rows of every page, for a caller that has already established entitlement.

        A refusal yields nothing here, which is why the entitlement probe comes first:
        this method cannot tell an empty history from a plan that excludes the series.
        """
        for page in self.pages(path, params):
            yield from page.rows

    def _url(self, path: str) -> str:
        """The absolute URL of `path`; a cursor's own absolute URL passes through."""
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _keys(path: str) -> list[str]:
        """The rate-limit buckets a request counts against, coarsest last.

        `/v2/aggs/ticker/…` counts against `v2/aggs` and `v2`, which is the shape the
        engine's limiter expects and what a per-endpoint quota would later be keyed by.
        A cursor arrives as an absolute URL and must land in the same buckets as the page
        before it, so the host is stripped rather than counted as an endpoint of its own.
        """
        route = path.split("?", 1)[0]
        if route.startswith(("http://", "https://")):
            route = route.split("//", 1)[1].partition("/")[2]
        parts = [part for part in route.split("/") if part][:2]
        if not parts:
            return []
        return ["/".join(parts), parts[0]] if len(parts) > 1 else [parts[0]]


def _read(path: str, params: tuple[tuple[str, str], ...], response: Response) -> Call:
    body = _parse(response.body)
    signal = signal_of(response.status, body)
    document: Mapping[str, Any] = body or {}
    following = document.get("next_url")
    vendor = document.get("status")
    request_id = document.get("request_id")
    return Call(
        path=path,
        params=params,
        status=response.status,
        signal=signal,
        rows=_rows(document),
        body=document,
        next_url=following if isinstance(following, str) and following else None,
        vendor_status=vendor if isinstance(vendor, str) else None,
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _parse(body: bytes) -> Mapping[str, Any] | None:
    """The body as a JSON object, or `None` when it is not one.

    A gateway's HTML error page and a truncated response both land here, and both are
    unreadable rather than empty: an empty answer is a fact about the data, and one of
    the four outcomes hangs on the difference.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rows(body: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The `results` payload as a tuple of rows, whether it was a list or one object."""
    results = body.get("results")
    if isinstance(results, Mapping):
        return (results,)
    if isinstance(results, list):
        return tuple(row for row in results if isinstance(row, Mapping))
    return ()
