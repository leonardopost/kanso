"""Massive's flat-file store: signing, listing, and reading an object one range at a time.

The vendor serves its history twice — once as paginated JSON over the REST API and once
as one gzipped CSV per class, per data type, per day in an S3-compatible object store.
The second is what a backfill wants: a decade of daily aggregates is three thousand
objects instead of a hundred thousand requests. This module is the wire for that store
and stops where meaning begins, exactly as `client` does for the REST API.

**Signing is mandatory and is done here, from the standard library alone.** The store
accepts no API-key transport at all: neither a query parameter nor a bearer header
authenticates, so every request carries an AWS Signature Version 4 over its own headers.
The signer takes an injectable clock, which is what makes the published signature vectors
an offline oracle: freeze the clock at a vector's date and the signature is reproducible
on any host, with no credential and no network.

**`x-amz-content-sha256` is optional, deliberately.** S3 requires the header on every
request; the published vectors were written before it existed and expect exactly
`host;x-amz-date` in `SignedHeaders`. A signer that always injected the header could not
be checked against a single vector, so whether it is sent is a flag. Production sends it;
the vectors are signed without it.

**The path is signed exactly as given.** S3 forbids normalising a key — `a/../b` and `b`
are two different objects — so nothing here collapses a dot segment or a double slash.
`object_path` percent-encodes a key into a path once, and the signer then treats that
path as opaque bytes.

Three facts about the store shape everything below, each of them measured rather than
assumed.

* **Only a GET proves entitlement.** Listing is not scoped by product: a prefix the plan
  does not include lists cleanly and refuses every read inside it. So a listing is used
  to discover which days exist and never to decide whether they can be read.
* **A refusal must be read from the code, never from the prose.** A `403` covers a class
  the plan excludes, a date outside the plan's window and an unrecognised key; the
  machine-readable `<Code>` element separates a denial from a bad signature from an
  unknown access-key id, and a well-formed request for an object that is not there is a
  `404 NoSuchKey`. `access_of` reads the status and that element and nothing else.
* **The engine's bulk download discards response bodies**, collapsing every refusal into
  one message with no code in it. So the two paths are split: bytes come down through
  `nautilus_pyo3.http_download`, and anything whose *answer* matters — a listing, a
  probe, a small ranged read — goes through the REST client's own rate-limited transport,
  which returns a status and a body. Both therefore count against one quota.

**`Host` is signed and never sent.** Every sender here derives a `Host` from the URL —
the engine's bulk download and the rate-limited transport alike — and a request that
arrives carrying a second one is answered by the store's front end with a `400` and an
HTML page instead of an object. Measured on one aggregate object, with one signer and one
signature: sent with the signed `Host` in the header map, `400` and 150 bytes of HTML;
sent without it, `200` and the whole object. So a `Signature` hands over a header map that
has no `Host` in it at all, rather than a map each call site must remember to strip: what
was signed stays legible in `canonical_request` and `signed_headers`, and the one form a
sender can reach is the one that works.

The store's own gzip is left alone. `pyarrow` is present as the engine's dependency and
its S3 filesystem would read these objects, but it decompresses by file extension, which
turns a byte-exact ingestion into a silent transformation; the bytes are fetched and
decompressed explicitly instead.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`nautilus_trader.core.nautilus_pyo3.http_download(url, filepath, params=None,
headers=None, timeout_secs=None)` streams a response body to a file with
`reqwest::blocking`, creating parent directories, and raises on a non-success status. It
is GET-only, it reports no status and no body on success, and its error carries the
status line alone — which is why it is used for bytes and never for an answer. A `206
Partial Content` is a success to it, so a ranged read of a multi-gigabyte object writes
only the requested bytes.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from urllib.parse import parse_qsl, quote, urlsplit
from xml.etree import ElementTree

from kanso.data.adapters.massive.client import Transport
from kanso.data.adapters.massive.errors import MalformedRequestError, TransportError

__all__ = [
    "ANSWERED",
    "BUCKET",
    "FILES_HOST",
    "REGION",
    "SERVICE",
    "Access",
    "Downloader",
    "Entry",
    "ObjectStore",
    "Reply",
    "Signature",
    "Signer",
    "access_of",
    "error_code",
    "object_path",
    "utc_now",
]

FILES_HOST: Final = "https://files.massive.com"
"""Where the flat files live. `[adapters.massive] base_url` governs the REST API only:
the two hosts are separate services and a proxy for one is not a proxy for the other."""

BUCKET: Final = "flatfiles"
"""The path-style bucket every object sits in: `<host>/flatfiles/<key>`."""

REGION: Final = "us-east-1"
SERVICE: Final = "s3"
"""The credential scope the store signs under."""

ALGORITHM: Final = "AWS4-HMAC-SHA256"
TERMINATOR: Final = "aws4_request"

DATE_FORMAT: Final = "%Y%m%dT%H%M%SZ"
DAY_FORMAT: Final = "%Y%m%d"

AMZ_DATE: Final = "x-amz-date"
CONTENT_SHA256: Final = "x-amz-content-sha256"
AUTHORIZATION: Final = "Authorization"
HOST: Final = "Host"

EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
"""The payload hash of a body-less request, which every GET here has."""

UNRESERVED: Final = "-_.~"
"""What percent-encoding leaves alone, per RFC 3986. Python's `quote` already keeps
letters, digits and these four, so naming them is documentation rather than an argument."""

MAX_KEYS: Final = 1_000
"""Keys per listing page: the store's own maximum, so a walk costs the fewest pages."""

MAX_PAGES: Final = 10_000
"""Pages one listing walk may fetch. Ten million keys is far past any prefix this adapter
asks for, and a cheap guard against a continuation token that never terminates."""

LIST_TYPE: Final = "2"
"""`ListObjectsV2`. Version 1 pages on the last key returned, which silently skips keys
when one is deleted mid-walk; version 2 pages on an opaque continuation token."""

S3_NAMESPACE: Final = "http://s3.amazonaws.com/doc/2006-03-01/"

Clock = Callable[[], datetime]
"""Where the signer's `x-amz-date` comes from. Injectable so a vector is reproducible."""

Downloader = Callable[[str, str, Mapping[str, str]], None]
"""How bytes reach a file: a URL, a path and the headers to send. Injectable so the
streaming path is exercised with no socket."""


def utc_now() -> datetime:
    """The signer's default clock."""
    return datetime.now(UTC)


class Access(StrEnum):
    """What one object request was, at the wire, before anybody decided what it meant.

    `REFUSED` deliberately covers three conditions the store states identically — a class
    the plan excludes, a date outside the plan's window, and a key shape it will not
    honour. Separating those is a matter of probing at the grain the store gates on, not
    of reading a refusal more closely.
    """

    SERVED = "served"
    MISSING = "missing"
    REFUSED = "refused"
    UNKNOWN_KEY = "unknown_key"
    BAD_SIGNATURE = "bad_signature"
    UNIDENTIFIED = "unidentified"
    UNAVAILABLE = "unavailable"


ANSWERED: Final[frozenset[Access]] = frozenset(
    {
        Access.SERVED,
        Access.MISSING,
        Access.REFUSED,
        Access.UNKNOWN_KEY,
        Access.BAD_SIGNATURE,
        Access.UNIDENTIFIED,
    }
)
"""The answers. `UNAVAILABLE` alone is the absence of one and carries no fact."""

CODES: Final[dict[str, Access]] = {
    "accessdenied": Access.REFUSED,
    "allaccessdisabled": Access.REFUSED,
    "invalidaccesskeyid": Access.UNKNOWN_KEY,
    "signaturedoesnotmatch": Access.BAD_SIGNATURE,
    "nosuchkey": Access.MISSING,
    "nosuchbucket": Access.MISSING,
}
"""The store's machine-readable refusal codes, folded to lower case. The sentence beside
one is never read: it is prose, and prose that means several things at once."""


def object_path(key: str) -> str:
    """One object key as a request path: encoded once, never normalised.

    S3 addresses `a/../b` and `b` as two different objects, so a dot segment is encoded
    and kept rather than collapsed. `/` is the separator and stays literal.
    """
    return "/" + quote(key.lstrip("/"), safe="/" + UNRESERVED)


def error_code(body: bytes) -> str | None:
    """The `<Code>` of a store error document, or `None` when there is not one.

    Only this one element is read. The `<Message>` beside it is prose that covers several
    conditions at once, which is exactly the confusion this adapter exists to prevent.

    The document is parsed with the standard library's own XML reader, which fetches no
    external entity, so a body from a host that is not the store cannot make this process
    open a file or a socket. A body that is not well-formed simply carries no code.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if _tag(element.tag) == "Code" and element.text:
            return element.text.strip()
    return None


def access_of(status: int, body: bytes) -> Access:
    """What a response to an object request was, from its status and its refusal code."""
    if 200 <= status < 300:
        return Access.SERVED
    if status >= 500 or status in (0, 429):
        return Access.UNAVAILABLE
    found = CODES.get((error_code(body) or "").lower())
    if found is not None:
        return found
    if status == 404:
        return Access.MISSING
    if status in (401, 403):
        return Access.UNIDENTIFIED
    return Access.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class Signature:
    """One signed request: the headers to send, and every stage that produced them.

    The intermediate stages are part of the value rather than a debugging aid: a
    signature vector states the canonical request and the string to sign as well as the
    final header, and a signer that only produced the last of the three could be checked
    at one point instead of three.

    `headers` is the map a sender is handed: everything that was signed except `Host`,
    plus the `Authorization` the signature became. `Host` is signed and must be, and is
    absent here because every sender sets its own from the URL and a request bearing two
    of them is answered `400` with an HTML page. There is deliberately no second form
    that includes it: a call site cannot forget a step it is never offered. What was
    signed remains readable in `canonical_request` and `signed_headers`.
    """

    canonical_request: str
    string_to_sign: str
    signature: str
    scope: str
    amz_date: str
    signed_headers: tuple[str, ...]
    headers: dict[str, str]

    @property
    def authorization(self) -> str:
        """The `Authorization` header this signature is sent under."""
        return self.headers[AUTHORIZATION]


class Signer:
    """AWS Signature Version 4 over the standard library, with the clock injected.

    Holds the secret for the life of the signer and puts it nowhere else: not in a
    signature, not in a header other than the derived one, not in a `repr`.
    """

    def __init__(
        self,
        access_key_id: str,
        secret_key: str,
        *,
        region: str = REGION,
        service: str = SERVICE,
        clock: Clock = utc_now,
        content_sha256: bool = True,
    ) -> None:
        self.access_key_id = access_key_id
        self._secret_key = secret_key
        self.region = region
        self.service = service
        self._clock = clock
        self.content_sha256 = content_sha256

    def __repr__(self) -> str:
        """Never a credential: a repr reaches tracebacks, logs and crash reports."""
        return f"Signer(region={self.region!r}, service={self.service!r})"

    def sign(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        payload: bytes = b"",
        now: datetime | None = None,
    ) -> Signature:
        """Sign one request and return the headers that send it.

        `params` and any query already in `url` are one set; a key given in both is
        refused rather than silently resolved, because a repeated query key is not
        representable here and quietly dropping one would sign a request that differs
        from the one sent.

        `Host` is signed from the URL and then left out of the headers returned, because
        every sender sets its own and the store answers a duplicate with a `400`.
        """
        moment = (now or self._clock()).astimezone(UTC)
        amz_date = moment.strftime(DATE_FORMAT)
        scope = f"{moment.strftime(DAY_FORMAT)}/{self.region}/{self.service}/{TERMINATOR}"
        split = urlsplit(url)
        digest = hashlib.sha256(payload).hexdigest()

        signing = dict(headers or {})
        signing[HOST] = split.netloc
        signing[AMZ_DATE] = amz_date
        if self.content_sha256:
            signing[CONTENT_SHA256] = digest

        canonical_headers, names = _canonical_headers(signing)
        canonical = "\n".join(
            (
                method.upper(),
                split.path or "/",
                _canonical_query(_query(split.query, params)),
                canonical_headers,
                ";".join(names),
                digest,
            )
        )
        to_sign = "\n".join(
            (ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest())
        )
        signature = _hmac(self._signing_key(moment), to_sign.encode()).hex()
        sending = _sendable(signing)
        sending[AUTHORIZATION] = (
            f"{ALGORITHM} Credential={self.access_key_id}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        )
        return Signature(
            canonical_request=canonical,
            string_to_sign=to_sign,
            signature=signature,
            scope=scope,
            amz_date=amz_date,
            signed_headers=names,
            headers=sending,
        )

    def _signing_key(self, moment: datetime) -> bytes:
        """The date, region and service scoped key, derived fresh for each signature.

        Deriving it per request rather than caching it is four HMACs of a few bytes: the
        cost is invisible beside one round trip, and a cache keyed on a date is one more
        thing to get wrong when a run crosses midnight UTC.
        """
        key = _hmac(f"AWS4{self._secret_key}".encode(), moment.strftime(DAY_FORMAT).encode())
        key = _hmac(key, self.region.encode())
        key = _hmac(key, self.service.encode())
        return _hmac(key, TERMINATOR.encode())


@dataclass(frozen=True, slots=True)
class Entry:
    """One object a listing named: its key, its size and when the store last wrote it."""

    key: str
    size: int
    modified: str

    def payload(self) -> dict[str, object]:
        return {"key": self.key, "size": self.size, "modified": self.modified}


@dataclass(frozen=True, slots=True)
class Reply:
    """One object request and what came back, in the terms this adapter reasons in.

    Carries the refusal code and never the sentence beside it, for the same reason a REST
    `Call` carries no message: the prose covers several conditions and reading it would
    collapse them.
    """

    key: str
    status: int
    access: Access
    body: bytes = b""
    code: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        """True when the store answered, whatever the answer was."""
        return self.access in ANSWERED

    @property
    def served(self) -> bool:
        """True when the bytes came back."""
        return self.access is Access.SERVED

    def raise_for_transport(self) -> None:
        """Fail when no answer arrived, leaving every answer to the caller."""
        if self.answered:
            return
        raise TransportError(
            f"massive files: {self.key} did not answer (HTTP {self.status})",
            remedy=(
                "re-run the command; if it repeats, lower `[adapters.massive] "
                "requests_per_second` or check the vendor's status page"
            ),
            status=self.status,
        )


class ObjectStore:
    """A signed, rate-limited reader of the flat-file bucket.

    Two ways out, for two different questions. `read` and `listing` go through the REST
    client's transport and come back with a status, a body and therefore a refusal code;
    `fetch` streams an object to a file through the engine's bulk download, which is what
    a multi-gigabyte day needs and which reports nothing but a status line. A caller that
    wants to know *why* a key is refused asks `probe`, never `fetch`.
    """

    def __init__(
        self,
        access_key_id: str,
        secret_key: str,
        *,
        transport: Transport,
        base_url: str = FILES_HOST,
        bucket: str = BUCKET,
        region: str = REGION,
        clock: Clock = utc_now,
        download: Downloader | None = None,
        timeout_s: int | None = None,
    ) -> None:
        self.signer = Signer(access_key_id, secret_key, region=region, clock=clock)
        self.base_url = base_url.rstrip("/")
        self.bucket = bucket
        self._transport = transport
        self._download = download
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        """Never a credential: a repr reaches tracebacks and crash reports."""
        return f"ObjectStore(base_url={self.base_url!r}, bucket={self.bucket!r})"

    def url(self, key: str) -> str:
        """The path-style URL of one object."""
        return f"{self.base_url}{object_path(f'{self.bucket}/{key}')}"

    def bucket_url(self) -> str:
        """The bucket's own URL, which is what a listing addresses."""
        return f"{self.base_url}{object_path(self.bucket)}"

    def read(self, key: str, *, byte_range: tuple[int, int] | None = None) -> Reply:
        """One object, or the part of it `byte_range` names, with the answer readable.

        A ranged request comes back `206 Partial Content`, which is a served answer: this
        is how a probe reads one byte of a multi-gigabyte object and how a header row is
        read without the day behind it.
        """
        headers = {"Range": _range(byte_range)} if byte_range is not None else {}
        return self._request("GET", self.url(key), key, headers=headers)

    def probe(self, key: str) -> Reply:
        """The cheapest request that establishes whether this object can be read.

        One byte. Only a GET proves entitlement here — a prefix the plan excludes lists
        cleanly and refuses every read inside it — so a listing can never answer this.
        """
        return self.read(key, byte_range=(0, 0))

    def listing(self, prefix: str, *, max_keys: int = MAX_KEYS) -> Iterator[Entry]:
        """Every object under `prefix`, walking the store's continuation tokens.

        A page that did not answer fails the walk rather than ending it: a timeout half
        way through a prefix looks exactly like the end of it, and a caller that mistook
        one for the other would take a partial history for a complete one.

        A listing says which objects exist and nothing about whether they can be read.
        """
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = {"list-type": LIST_TYPE, "prefix": prefix, "max-keys": str(max_keys)}
            if token is not None:
                params["continuation-token"] = token
            reply = self._request("GET", self.bucket_url(), prefix, params=params)
            reply.raise_for_transport()
            if not reply.served:
                raise _refused(prefix, reply)
            entries, token = _page(reply.body)
            yield from entries
            if token is None:
                return
        raise TransportError(
            f"massive files: {prefix} returned more than {MAX_PAGES} listing pages, which "
            "is a continuation token that does not end",
            remedy="narrow the prefix",
        )

    def fetch(self, key: str, path: Path, *, byte_range: tuple[int, int] | None = None) -> Path:
        """Stream one object to `path`, or the part of it `byte_range` names.

        The bytes never pass through memory, which is the whole reason this path exists.
        What it cannot do is say why a refusal happened: the download reports a status
        line and no body, so a failure here points the caller at `probe`, which asks the
        same question through the transport and gets a code back.
        """
        signed = self.signer.sign("GET", self.url(key))
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = dict(signed.headers)
        if byte_range is not None:
            headers["Range"] = _range(byte_range)
        try:
            (self._download or self._http_download)(self.url(key), str(path), headers)
        except Exception as exc:
            raise TransportError(
                f"massive files: {key} could not be downloaded ({type(exc).__name__}: {exc})",
                remedy=(
                    "the bulk download reports no refusal code; probe the same key to see "
                    "whether the plan excludes it, the date is outside its window, or the "
                    "object is simply not there"
                ),
            ) from exc
        return path

    def _http_download(self, url: str, filepath: str, headers: Mapping[str, str]) -> None:
        from nautilus_trader.core import nautilus_pyo3

        nautilus_pyo3.http_download(
            url, filepath, headers=dict(headers), timeout_secs=self._timeout_s
        )

    def _request(
        self,
        method: str,
        url: str,
        subject: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        """One signed request through the shared transport, reduced to a `Reply`.

        The headers sent are the signature's own, which carry no `Host`: this transport
        sets one from the URL, and a request that arrives with two of them is answered
        with a `400` and an HTML page by a front end that never sees the key.
        """
        signed = self.signer.sign(method, url, params=params, headers=headers)
        try:
            response = self._transport(method, url, dict(params or {}), signed.headers, [])
        except TransportError:
            raise
        except Exception as exc:  # every fault below the answer is one outcome
            raise TransportError(
                f"massive files: {subject} could not be reached ({type(exc).__name__})",
                remedy="check the network and the vendor's status page, then re-run",
            ) from exc
        body = bytes(response.body)
        return Reply(
            key=subject,
            status=response.status,
            access=access_of(response.status, body),
            body=body,
            code=error_code(body),
            headers=dict(response.headers),
        )


def _refused(prefix: str, reply: Reply) -> TransportError:
    """A listing that was answered and not served, as the failure a caller sees."""
    return TransportError(
        f"massive files: listing {prefix} was {reply.access} (HTTP {reply.status}"
        + (f", {reply.code}" if reply.code else "")
        + ")",
        remedy=(
            "check `KANSO_MASSIVE_ACCESS_KEY_ID` and `KANSO_MASSIVE_SECRET_KEY`; listing is "
            "not scoped by plan, so a refusal here is about the credential"
        ),
        status=reply.status,
    )


def _page(body: bytes) -> tuple[list[Entry], str | None]:
    """One `ListObjectsV2` page: its entries, and the token of the page after it."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise TransportError(
            f"massive files: a listing page is not the XML the store documents ({exc})",
            remedy=(
                "re-run; if it repeats, the host is answering for something other than the "
                "object store"
            ),
        ) from None
    if _tag(root.tag) != "ListBucketResult":
        raise TransportError(
            f"massive files: a listing page is a <{_tag(root.tag)}> document rather than the "
            "<ListBucketResult> the store documents",
            remedy=(
                "a proxy or captive portal is answering in place of the store; an unrecognised "
                "document is never read as an empty prefix, which would look like no history"
            ),
        )
    entries = [
        Entry(
            key=_text(element, "Key"),
            size=int(_text(element, "Size") or 0),
            modified=_text(element, "LastModified"),
        )
        for element in root
        if _tag(element.tag) == "Contents"
    ]
    truncated = _text(root, "IsTruncated").lower() == "true"
    token = _text(root, "NextContinuationToken")
    return entries, (token or None) if truncated else None


def _text(parent: ElementTree.Element, name: str) -> str:
    """One child element's text, whether or not the document declares a namespace.

    The store answers a listing in the `2006-03-01` namespace and an error with none, so
    matching on the local name is what reads both without a namespace map per document.
    """
    for element in parent:
        if _tag(element.tag) == name:
            return (element.text or "").strip()
    return ""


def _tag(tag: str) -> str:
    """An element's local name, with any namespace stripped."""
    return tag.rpartition("}")[2]


def _range(byte_range: tuple[int, int]) -> str:
    """A byte range as the header spells it, refusing one that names nothing."""
    start, end = byte_range
    if start < 0 or end < start:
        raise MalformedRequestError(
            f"massive files: bytes {start}-{end} is not a range",
            remedy="a range runs from a non-negative first byte to a last byte at or after it",
        )
    return f"bytes={start}-{end}"


def _query(embedded: str, params: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """The request's whole query, refusing a key given twice.

    A repeated query key cannot be represented in the `dict` this adapter passes around,
    and resolving the clash silently would sign one request and send another.
    """
    pairs = list(parse_qsl(embedded, keep_blank_values=True))
    pairs += list((params or {}).items())
    seen = {name for name, _ in pairs}
    if len(seen) != len(pairs):
        raise MalformedRequestError(
            "massive files: a query key is given twice, which this signer cannot represent",
            remedy="send each query key once",
        )
    return tuple(pairs)


def _canonical_query(pairs: Sequence[tuple[str, str]]) -> str:
    """The query in canonical form: encoded, then sorted on the encoded bytes."""
    encoded = sorted((_encode(name), _encode(value)) for name, value in pairs)
    return "&".join(f"{name}={value}" for name, value in encoded)


def _encode(text: str) -> str:
    """One query component, percent-encoded with nothing held back but the unreserved."""
    return quote(text, safe="")


def _sendable(headers: Mapping[str, str]) -> dict[str, str]:
    """The signed headers a sender may be handed: all of them but `Host`.

    Matched on the folded name, so a `Host` a caller wrote in any casing goes the same
    way as the one signed from the URL. A sender derives its own from the URL, and the
    store's front end answers a request carrying two with a `400` and an HTML page.
    """
    return {name: value for name, value in headers.items() if name.lower() != HOST.lower()}


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    """The headers in canonical form, and the names in the order they were signed.

    A name is folded to lower case and a value has its runs of whitespace squeezed to one
    space, which is what the specification requires and what makes a header that survived
    a proxy sign the same as one that did not. Two headers differing only in case are one
    header here; sending both is not representable and is not something this adapter does.
    """
    folded = {name.lower().strip(): " ".join(value.split()) for name, value in headers.items()}
    names = tuple(sorted(folded))
    return "".join(f"{name}:{folded[name]}\n" for name in names), names


def _hmac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()
