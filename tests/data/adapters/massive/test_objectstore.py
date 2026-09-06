"""The flat-file store: the signer against published vectors, and the loader over frozen wire.

Three things are proved here.

**The signer is right, and provably so offline.** The AWS Signature Version 4 test suite is
the published oracle for a signer, and it is used here under a frozen clock, with a
credential that exists only in the suite. Every applicable case is asserted on the final
`Authorization` header, which no wrong canonical request or wrong string to sign can
survive; four of them are asserted on all three stages, so a failure says which stage
broke. The cases that are *not* applicable are named below with the reason each one cannot
be expressed, because a signer quietly scoring seventeen out of thirty-three while
pretending to score thirty-three is worse than one that scores nothing.

**A refusal is read from its code and never from its sentence.** The store answers a class
the plan excludes, an object that is not there and a wrong secret with the same prose and
three different `<Code>` elements; a test feeds the identical sentence into all three and
asserts three different outcomes, and another swaps the sentence for different text and
asserts the classification does not move.

**Entitlement and history are two questions.** The store's listing is not scoped by plan,
so a prefix lists cleanly and refuses every read inside it: a test lists a decade, refuses
every object, and asserts the answer is "not entitled" — and a second refuses only the old
half and asserts the answer is a floor, with the newest object served. Reporting the second
as the first is the most expensive wrong answer this adapter can give.

**A sender is handed what it can actually send.** The store's front end answers a request
bearing two `Host` headers with a `400` and an HTML page, and both of this adapter's
senders set a `Host` from the URL themselves. So the frozen wire here refuses a `Host` on
either way out rather than shrugging at it: a signature that hands one over fails a test
in this file instead of every live request against the store.

**Where the vectors come from.** The suite is distributed with the AWS SDKs' source trees
and not with the wheels this project installs, and kanso may add no dependency to fetch
it, so the cases are written out here instead: the request each one makes and the
`Authorization` header it must produce. The expected values were reproduced with an
independent, widely used implementation of the same algorithm before being frozen, and the
two vanilla cases carry the signatures the suite itself publishes — which is what pins the
table to the suite rather than to any one implementation.

Nothing here opens a socket, reads a credential from the environment or carries a recorded
secret. The suite's own example credential is published, expires nowhere and authenticates
nothing.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest

from kanso.data.adapters.massive import objectstore
from kanso.data.adapters.massive.client import MassiveClient, Response
from kanso.data.adapters.massive.entitlement import settled_end
from kanso.data.adapters.massive.errors import (
    MalformedRequestError,
    NotEntitledError,
    TransportError,
)
from kanso.data.adapters.massive.loaders import bulk
from kanso.data.adapters.massive.loaders.bars import MassiveBarsLoader
from kanso.data.adapters.massive.loaders.bulk import (
    COLUMNS,
    Bulk,
    BulkLoader,
    BulkSpec,
    Series,
    day_of,
    key_for,
    prefix_for,
)
from kanso.data.adapters.massive.objectstore import (
    ANSWERED,
    BUCKET,
    FILES_HOST,
    REGION,
    SERVICE,
    Access,
    Entry,
    ObjectStore,
    Reply,
    Signer,
    access_of,
    error_code,
    object_path,
    utc_now,
)
from kanso.data.loader import to_ns, utc_day
from kanso.data.manifest import shortfall
from kanso.errors import PreconditionError, ValidationError
from kanso.workspace import Workspace, init

from . import Replay, definition, nothing, refuse_host, window_of
from . import served as served_rows

# --- the published signature vectors ------------------------------------------

UNRESERVED = "-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
"""The characters RFC 3986 says are never percent-encoded, which two cases are made of."""

SUITE_KEY_ID = "AKIDEXAMPLE"
SUITE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
"""The suite's own published example credential. It authenticates nothing anywhere."""

SUITE_REGION = "us-east-1"
SUITE_SERVICE = "service"
SUITE_HOST = "example.amazonaws.com"
SUITE_MOMENT = datetime(2015, 8, 30, 12, 36, tzinfo=UTC)
"""The instant every vector is signed at. Frozen, so the expected header is a constant."""


@dataclass(frozen=True)
class Vector:
    """One case of the suite: the request it makes and the signature it must produce."""

    name: str
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: str
    signature: str


VECTORS: tuple[Vector, ...] = (
    Vector(
        name="get-header-value-trim",
        method="GET",
        path="/",
        query={},
        headers={"My-Header1": "  value1  ", "My-Header2": '"a   b   c"'},
        body="",
        signature="acc3ed3afb60bb290fc8d2dd0098b9911fcaa05412b367055dee359757a9c736",
    ),
    Vector(
        name="get-unreserved",
        method="GET",
        path="/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        query={},
        headers={},
        body="",
        signature="07ef7494c76fa4850883e2b006601f940f8a34d404d0cfa977f52a65bbf5f24f",
    ),
    Vector(
        name="get-utf8",
        method="GET",
        path="/ሴ",
        query={},
        headers={},
        body="",
        signature="8318018e0b0f223aa2bbf98705b62bb787dc9c0e678f255a891fd03141be5d85",
    ),
    Vector(
        name="get-vanilla",
        method="GET",
        path="/",
        query={},
        headers={},
        body="",
        signature="5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
    ),
    Vector(
        name="get-vanilla-empty-query-key",
        method="GET",
        path="/",
        query={"Param1": "value1"},
        headers={},
        body="",
        signature="a67d582fa61cc504c4bae71f336f98b97f1ea3c7a6bfe1b6e45aec72011b9aeb",
    ),
    Vector(
        name="get-vanilla-query",
        method="GET",
        path="/",
        query={},
        headers={},
        body="",
        signature="5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
    ),
    Vector(
        name="get-vanilla-query-order-key",
        method="GET",
        path="/",
        query={"Param2": "value2", "Param1": "value1"},
        headers={},
        body="",
        signature="b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
    ),
    Vector(
        name="get-vanilla-query-unreserved",
        method="GET",
        path="/",
        query={UNRESERVED: UNRESERVED},
        headers={},
        body="",
        signature="9c3e54bfcdf0b19771a7f523ee5669cdf59bc7cc0884027167c21bb143a40197",
    ),
    Vector(
        name="get-vanilla-utf8-query",
        method="GET",
        path="/",
        query={"ሴ": "bar"},
        headers={},
        body="",
        signature="2cdec8eed098649ff3a119c94853b13c643bcf08f8b0a1d91e12c9027818dd04",
    ),
    Vector(
        name="post-header-key-case",
        method="POST",
        path="/",
        query={},
        headers={"MY-HEADER1": "value1"},
        body="",
        signature="c5410059b04c1ee005303aed430f6e6645f61f4dc9e1461ec8f8916fdf18852c",
    ),
    Vector(
        name="post-header-key-sort",
        method="POST",
        path="/",
        query={},
        headers={"My-Header2": "value2", "My-Header1": "value1"},
        body="",
        signature="7cfc02f603a8bc7102119d630fcf67fe9760f7a8a96c825d480017f16714d837",
    ),
    Vector(
        name="post-header-value-case",
        method="POST",
        path="/",
        query={},
        headers={"My-Header1": "VALUE1"},
        body="",
        signature="cdbc9802e29d2942e5e10b5bccfdd67c5f22c7c4e8ae67b53629efa58b974b7d",
    ),
    Vector(
        name="post-vanilla",
        method="POST",
        path="/",
        query={},
        headers={},
        body="",
        signature="5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b",
    ),
    Vector(
        name="post-vanilla-empty-query-value",
        method="POST",
        path="/",
        query={"Param1": ""},
        headers={},
        body="",
        signature="bf3f0f582c3027713f97f4cdd7cfa9e1db035de22ef7b04373f44f3a8ae51aa8",
    ),
    Vector(
        name="post-vanilla-query",
        method="POST",
        path="/",
        query={"Param1": "value1"},
        headers={},
        body="",
        signature="28038455d6de14eafc1f9222cf5aa6f1a96197d7deb8263271d420d138af7f11",
    ),
    Vector(
        name="post-x-www-form-urlencoded",
        method="POST",
        path="/",
        query={},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body="Param1=value1",
        signature="ff11897932ad3f4e8b18135d722051e5ac45fc38421b1da7b9d196a0fe09473a",
    ),
    Vector(
        name="post-x-www-form-urlencoded-parameters",
        method="POST",
        path="/",
        query={},
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        body="Param1=value1",
        signature="2f3b42f35f135abf9c562afcbbc44fc03df96dcfd4332ecebad8b39a7d4b6125",
    ),
)
"""The applicable cases. Seventeen of the suite's thirty-three: the rest are below."""

STAGES: dict[str, tuple[str, str]] = {
    "get-vanilla": (
        "\n".join(
            (
                "GET",
                "/",
                "",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ),
        "\n".join(
            (
                "AWS4-HMAC-SHA256",
                "20150830T123600Z",
                "20150830/us-east-1/service/aws4_request",
                "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63",
            )
        ),
    ),
    "get-header-value-trim": (
        "\n".join(
            (
                "GET",
                "/",
                "",
                "host:example.amazonaws.com",
                "my-header1:value1",
                'my-header2:"a b c"',
                "x-amz-date:20150830T123600Z",
                "",
                "host;my-header1;my-header2;x-amz-date",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ),
        "\n".join(
            (
                "AWS4-HMAC-SHA256",
                "20150830T123600Z",
                "20150830/us-east-1/service/aws4_request",
                "a726db9b0df21c14f559d0a978e563112acb1b9e05476f0a6a1c7d68f28605c7",
            )
        ),
    ),
    "get-vanilla-utf8-query": (
        "\n".join(
            (
                "GET",
                "/",
                "%E1%88%B4=bar",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ),
        "\n".join(
            (
                "AWS4-HMAC-SHA256",
                "20150830T123600Z",
                "20150830/us-east-1/service/aws4_request",
                "eb30c5bed55734080471a834cc727ae56beb50e5f39d1bff6d0d38cb192a7073",
            )
        ),
    ),
    "post-x-www-form-urlencoded-parameters": (
        "\n".join(
            (
                "POST",
                "/",
                "",
                "content-type:application/x-www-form-urlencoded; charset=utf-8",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "content-type;host;x-amz-date",
                "9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e",
            )
        ),
        "\n".join(
            (
                "AWS4-HMAC-SHA256",
                "20150830T123600Z",
                "20150830/us-east-1/service/aws4_request",
                "32031df15172a0c1541fd8f995b6351948c6a4b045b8c592e4d1b59299ed3a29",
            )
        ),
    ),
}

"""Canonical request and string to sign for four cases, so a failure localises to a stage
instead of only saying that the last of the three came out wrong."""

INAPPLICABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "the same header name is sent more than once, and a header map keyed by name "
        "cannot hold two values for one name; the store is addressed with a fixed, "
        "single-valued header set, so this is a shape kanso never sends",
        ("get-header-key-duplicate", "get-header-value-order", "get-header-value-multiline"),
    ),
    (
        "the same query key is sent more than once, and a query map keyed by name cannot "
        "hold two values for one key; the signer refuses such a request outright rather "
        "than signing one shape and sending another",
        ("get-vanilla-query-order-key-case", "get-vanilla-query-order-value"),
    ),
    (
        "the case expects the path to be normalised before signing, and S3 forbids it: "
        "`a/../b` and `b` are two different objects there, so a signer that collapsed a "
        "dot segment would sign a key the store does not hold",
        (
            "normalize-path/get-relative",
            "normalize-path/get-relative-relative",
            "normalize-path/get-slash",
            "normalize-path/get-slash-dot-slash",
            "normalize-path/get-slash-pointless-dot",
            "normalize-path/get-slashes",
            "normalize-path/get-space",
        ),
    ),
    (
        "the case signs a session token, and the `-after` variant signs a header set that "
        "differs from the one it sends; the store issues long-lived keys, kanso sends no "
        "session token, and this signer deliberately cannot sign one header set and send "
        "another",
        ("post-sts-token/post-sts-header-before", "post-sts-token/post-sts-header-after"),
    ),
    (
        "the case's query is documented as malformed and has no defined encoding, so there "
        "is no signature for a correct signer to produce",
        ("post-vanilla-query-nonunreserved", "post-vanilla-query-space"),
    ),
)
"""The sixteen cases that cannot be expressed here, and why each one cannot."""


def suite_signer(**kwargs: Any) -> Signer:
    """A signer at the suite's own credential, scope and instant."""
    return Signer(
        SUITE_KEY_ID,
        SUITE_SECRET,
        region=SUITE_REGION,
        service=SUITE_SERVICE,
        clock=lambda: SUITE_MOMENT,
        content_sha256=False,
        **kwargs,
    )


def suite_url(path: str) -> str:
    """The vector's path as a URL, percent-encoded once and never normalised."""
    return f"https://{SUITE_HOST}{quote(path, safe='/-_.~')}"


def sign_vector(vector: Vector) -> Any:
    """One vector signed."""
    return suite_signer().sign(
        vector.method,
        suite_url(vector.path),
        params=vector.query,
        headers=vector.headers,
        payload=vector.body.encode(),
    )


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.name)
def test_the_signer_reproduces_every_applicable_published_vector(vector: Vector) -> None:
    """The whole point of a vector: a header a correct signer cannot miss by luck."""
    signed = sign_vector(vector)

    assert signed.signature == vector.signature
    assert signed.authorization == (
        f"AWS4-HMAC-SHA256 Credential={SUITE_KEY_ID}/20150830/{SUITE_REGION}/"
        f"{SUITE_SERVICE}/aws4_request, SignedHeaders={';'.join(signed.signed_headers)}, "
        f"Signature={vector.signature}"
    )


@pytest.mark.parametrize("name", sorted(STAGES), ids=str)
def test_each_stage_of_a_signature_matches_the_vector_not_only_the_last(name: str) -> None:
    """A canonical request and a string to sign are stated by the suite as well."""
    vector = next(item for item in VECTORS if item.name == name)
    canonical, to_sign = STAGES[name]

    signed = sign_vector(vector)

    assert signed.canonical_request == canonical
    assert signed.string_to_sign == to_sign


def test_seventeen_cases_are_applicable_and_sixteen_are_named_as_not() -> None:
    """The count is stated so a case cannot be quietly dropped to make the suite pass."""
    named = [name for _, names in INAPPLICABLE for name in names]

    assert len(VECTORS) == 17
    assert len(named) == 16
    assert len(set(named)) == 16
    assert all(reason for reason, _ in INAPPLICABLE)


def test_every_vector_signs_exactly_host_and_the_date_plus_its_own_headers() -> None:
    """Without the flag that omits the content hash, not one vector could be used at all."""
    for vector in VECTORS:
        signed = sign_vector(vector)

        assert "x-amz-content-sha256" not in signed.signed_headers
        assert set(signed.signed_headers) == {"host", "x-amz-date"} | {
            name.lower() for name in vector.headers
        }


def test_the_content_hash_is_sent_by_default_because_the_store_requires_it() -> None:
    """The vectors omit the header and S3 rejects a request without it; hence the flag."""
    signed = Signer(SUITE_KEY_ID, SUITE_SECRET, clock=lambda: SUITE_MOMENT).sign(
        "GET", f"https://{SUITE_HOST}/flatfiles/a"
    )

    assert "x-amz-content-sha256" in signed.signed_headers
    assert signed.headers["x-amz-content-sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_a_path_is_signed_exactly_as_given_and_never_normalised() -> None:
    """S3 addresses `a/../b` and `b` as two objects; collapsing one signs the wrong key."""
    signed = suite_signer().sign("GET", f"https://{SUITE_HOST}/example/..")

    assert signed.canonical_request.splitlines()[1] == "/example/.."


def test_a_query_key_given_twice_is_refused_rather_than_silently_halved() -> None:
    """Signing one shape and sending another is a signature failure nobody can debug."""
    with pytest.raises(MalformedRequestError) as caught:
        suite_signer().sign("GET", f"https://{SUITE_HOST}/?a=1", params={"a": "2"})

    assert "twice" in caught.value.message


def test_a_signature_never_carries_the_secret_and_neither_does_a_repr() -> None:
    """A repr reaches tracebacks, and a traceback reaches an issue tracker."""
    signer = suite_signer()

    signed = signer.sign("GET", f"https://{SUITE_HOST}/")

    assert SUITE_SECRET not in repr(signer)
    assert SUITE_SECRET not in signed.canonical_request
    assert SUITE_SECRET not in signed.string_to_sign
    assert SUITE_SECRET not in signed.authorization


def test_a_signature_signs_host_and_hands_over_headers_that_do_not_carry_it() -> None:
    """Corrected fixture: it used to assert `Host` was present in the headers to send.

    That is what the store was assumed to want and not what it was measured wanting. One
    object, one signer, one signature: sent with the signed `Host` in the map, `400` and
    150 bytes of HTML from a front end that never read the key; sent without it, `200`
    and the object. So `Host` is signed, stays legible in the canonical request, and is
    not in the map a sender is handed — and there is no second form that includes it for
    a call site to reach for by mistake.
    """
    signed = suite_signer().sign("GET", f"https://{SUITE_HOST}/")

    assert "host" in signed.signed_headers
    assert f"host:{SUITE_HOST}" in signed.canonical_request
    assert not [name for name in signed.headers if name.lower() == "host"]
    assert "Authorization" in signed.headers
    assert not hasattr(signed, "without_host")


def test_a_host_a_caller_wrote_itself_is_dropped_whatever_case_it_is_in() -> None:
    """The URL is the only source of a host; one written by hand is signed over and left
    behind rather than sent as a second header under a different spelling."""
    signed = suite_signer().sign("GET", f"https://{SUITE_HOST}/", headers={"host": "elsewhere"})

    assert f"host:{SUITE_HOST}" in signed.canonical_request
    assert not [name for name in signed.headers if name.lower() == "host"]


def test_the_default_clock_is_the_wall_clock_in_utc() -> None:
    """A signer built without a clock still signs; only a vector freezes one."""
    before = utc_now()

    signed = Signer(SUITE_KEY_ID, SUITE_SECRET).sign("GET", f"https://{SUITE_HOST}/")

    assert signed.amz_date >= before.strftime("%Y%m%dT%H%M%SZ")
    assert signed.scope.endswith(f"/{REGION}/{SERVICE}/aws4_request")


def test_an_explicit_moment_overrides_the_injected_clock() -> None:
    """`now` is for a caller that already holds the instant the request belongs to."""
    signed = suite_signer().sign(
        "GET", f"https://{SUITE_HOST}/", now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC)
    )

    assert signed.amz_date == "20150830T123600Z"


def test_an_object_key_is_encoded_once_and_keeps_its_separators() -> None:
    """A key is a name, not a path to be tidied; only the unsafe bytes change."""
    assert object_path("us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.csv.gz") == (
        "/us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.csv.gz"
    )
    assert object_path("/a b/c") == "/a%20b/c"
    assert object_path("a/../b") == "/a/../b"


# --- the wire: what a refusal says, and what a listing may be trusted for ------

PROSE = "This data isn't included in your current plan. Please upgrade your plan."
"""One sentence the store puts on several quite different refusals. No code reads it."""

STORE_KEY_ID = "AKIDTESTONLY"
STORE_SECRET = "secret-that-signs-nothing-real"

LISTED = "http://s3.amazonaws.com/doc/2006-03-01/"


def listing_xml(keys: Sequence[str], *, token: str | None = None, truncated: bool = False) -> bytes:
    """One `ListObjectsV2` page, in the namespace the store answers a listing in."""
    contents = "".join(
        f"<Contents><Key>{key}</Key><Size>{index + 1}</Size>"
        f"<LastModified>2026-09-01T00:00:00.000Z</LastModified></Contents>"
        for index, key in enumerate(keys)
    )
    following = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult xmlns="{LISTED}"><Name>{BUCKET}</Name>'
        f"<IsTruncated>{'true' if truncated or token else 'false'}</IsTruncated>"
        f"{contents}{following}</ListBucketResult>"
    ).encode()


def refusal_xml(code: str, message: str = PROSE) -> bytes:
    """One refusal document: a machine-readable code and a sentence beside it."""
    return (
        f"<Error><Code>{code}</Code><Message>{message}</Message>"
        f"<RequestId>frozen</RequestId></Error>"
    ).encode()


def guarded(download: objectstore.Downloader) -> objectstore.Downloader:
    """A frozen downloader that cares about `Host` the way the real one does."""

    def send(url: str, filepath: str, headers: Mapping[str, str]) -> None:
        refuse_host(headers, "download")
        download(url, filepath, headers)

    return send


def wire(
    answer: Any, *, download: objectstore.Downloader | None = None, **kwargs: Any
) -> tuple[ObjectStore, Replay]:
    """A store over a frozen wire, and the transport that recorded what it asked.

    Both ways out are guarded, because both are real senders: whichever one is handed a
    `Host` fails the test that handed it over rather than a live request months later.
    The transport carries the guard for the whole suite, so only the download needs
    wrapping here.
    """
    replay = Replay(answer)
    store = ObjectStore(
        STORE_KEY_ID,
        STORE_SECRET,
        transport=replay,
        clock=lambda: SUITE_MOMENT,
        download=None if download is None else guarded(download),
        **kwargs,
    )
    return store, replay


def key_of(url: str) -> str:
    """The object key a request URL names."""
    return url.split(f"/{BUCKET}/", 1)[1]


def test_one_sentence_and_three_codes_are_three_different_answers() -> None:
    """The store states unrelated conditions in identical prose; only the code separates."""
    seen = {
        code: access_of(status, refusal_xml(code))
        for code, status in (
            ("AccessDenied", 403),
            ("NoSuchKey", 404),
            ("SignatureDoesNotMatch", 403),
            ("InvalidAccessKeyId", 403),
        )
    }

    assert seen == {
        "AccessDenied": Access.REFUSED,
        "NoSuchKey": Access.MISSING,
        "SignatureDoesNotMatch": Access.BAD_SIGNATURE,
        "InvalidAccessKeyId": Access.UNKNOWN_KEY,
    }


def test_changing_the_sentence_changes_nothing_about_the_classification() -> None:
    """If the prose were being read, this would move the answer. It does not."""
    assert access_of(403, refusal_xml("AccessDenied", "something else entirely")) is (
        access_of(403, refusal_xml("AccessDenied"))
    )


def test_a_refusal_with_no_readable_code_is_its_own_answer() -> None:
    """An unrecognised key id comes back as a bodiless 403; calling that `refused` would
    tell an operator their plan is wrong when their credential is."""
    assert access_of(403, b"") is Access.UNIDENTIFIED
    assert access_of(404, b"") is Access.MISSING
    assert access_of(206, b"partial") is Access.SERVED
    assert access_of(500, b"") is Access.UNAVAILABLE
    assert access_of(429, b"") is Access.UNAVAILABLE
    assert access_of(0, b"") is Access.UNAVAILABLE
    assert access_of(400, b"<html>gateway</html>") is Access.UNAVAILABLE
    assert Access.UNAVAILABLE not in ANSWERED


def test_a_body_that_is_not_a_refusal_document_carries_no_code() -> None:
    """A gateway's HTML page and an empty body are both codeless rather than denied."""
    assert error_code(b"<html><body>400</body></html>") is None
    assert error_code(b"not xml at all") is None
    assert error_code(refusal_xml("AccessDenied")) == "AccessDenied"


def test_a_ranged_read_is_a_served_answer_and_sends_the_range_it_asked_for() -> None:
    """206 is how one byte of a multi-gigabyte day is read without fetching the day."""
    store, replay = wire(lambda url, params: Response(206, b"t"))

    reply = store.probe("us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.csv.gz")

    assert reply.served and reply.answered and reply.status == 206
    assert replay.asked[0].headers["Range"] == "bytes=0-0"


def test_a_range_that_names_nothing_is_refused_before_it_is_signed() -> None:
    store, replay = wire(lambda url, params: Response(206, b"t"))

    with pytest.raises(MalformedRequestError):
        store.read("a", byte_range=(4, 1))

    assert replay.asked == []


def test_a_request_that_did_not_answer_raises_and_one_that_refused_returns() -> None:
    """A refusal is evidence a probe needs; only the absence of an answer is a failure."""
    store, _ = wire(lambda url, params: Response(403, refusal_xml("AccessDenied")))
    reply = store.read("a")
    reply.raise_for_transport()

    store, _ = wire(lambda url, params: Response(503, b""))
    with pytest.raises(TransportError) as caught:
        store.read("a").raise_for_transport()

    assert caught.value.status == 503
    assert reply.access is Access.REFUSED


def test_a_transport_that_fails_below_the_answer_is_one_outcome() -> None:
    def broken(url: str, params: Mapping[str, str]) -> Response:
        raise OSError("connection reset")

    store, _ = wire(broken)

    with pytest.raises(TransportError) as caught:
        store.read("a")

    assert "OSError" in caught.value.message


def test_a_transport_error_from_below_is_not_wrapped_twice() -> None:
    def refusing(url: str, params: Mapping[str, str]) -> Response:
        raise TransportError("the quota is exhausted")

    store, _ = wire(refusing)

    with pytest.raises(TransportError) as caught:
        store.read("a")

    assert caught.value.message == "the quota is exhausted"


def test_a_listing_walks_every_continuation_token() -> None:
    pages = {
        None: listing_xml(["p/2024/01/2024-01-02.csv.gz"], token="second"),
        "second": listing_xml(["p/2024/01/2024-01-03.csv.gz"]),
    }

    store, replay = wire(lambda url, params: Response(200, pages[params.get("continuation-token")]))

    found = list(store.listing("p/"))

    assert [entry.key for entry in found] == [
        "p/2024/01/2024-01-02.csv.gz",
        "p/2024/01/2024-01-03.csv.gz",
    ]
    assert found[0].payload() == {
        "key": "p/2024/01/2024-01-02.csv.gz",
        "size": 1,
        "modified": "2026-09-01T00:00:00.000Z",
    }
    assert [item.params["list-type"] for item in replay.asked] == ["2", "2"]


def test_a_page_that_says_it_is_truncated_and_names_no_token_ends_the_walk() -> None:
    """A store that contradicts itself must not become an endless loop."""
    store, _ = wire(lambda url, params: Response(200, listing_xml(["p/a.csv.gz"], truncated=True)))

    assert len(list(store.listing("p/"))) == 1


def test_a_listing_that_never_ends_is_a_failure_rather_than_a_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(objectstore, "MAX_PAGES", 2)
    store, _ = wire(lambda url, params: Response(200, listing_xml(["p/a.csv.gz"], token="always")))

    with pytest.raises(TransportError) as caught:
        list(store.listing("p/"))

    assert "does not end" in caught.value.message


def test_a_refused_listing_names_the_credential_rather_than_the_plan() -> None:
    """Listing is not scoped by plan, so a refusal here is about the key, not the class."""
    store, _ = wire(lambda url, params: Response(403, refusal_xml("SignatureDoesNotMatch")))

    with pytest.raises(TransportError) as caught:
        list(store.listing("p/"))

    assert "SignatureDoesNotMatch" in caught.value.message
    assert "SECRET_KEY" in (caught.value.remedy or "")


def test_a_listing_that_is_not_the_documented_xml_is_a_transport_failure() -> None:
    store, _ = wire(lambda url, params: Response(200, b"a proxy said no"))

    with pytest.raises(TransportError) as caught:
        list(store.listing("p/"))

    assert "XML" in caught.value.message


def test_a_well_formed_document_that_is_not_a_listing_is_never_read_as_an_empty_prefix() -> None:
    """A captive portal answering in XML would otherwise look like a source with no history."""
    store, _ = wire(lambda url, params: Response(200, b"<html><body>sign in</body></html>"))

    with pytest.raises(TransportError) as caught:
        list(store.listing("p/"))

    assert "ListBucketResult" in caught.value.message


def test_every_request_path_sends_the_signature_and_never_a_second_host_header() -> None:
    """`read`, `probe` and `listing` all go out through the transport, and every one of
    them was answered `400` with an nginx page live while this suite was green: the
    signature was correct and the request carried a `Host` the transport also sets. The
    download path was the only one asserted on before, so the assertion is made of the
    request path too — three requests, one map, no `Host` in it.
    """
    store, replay = wire(lambda url, params: Response(200, listing_xml([])))

    store.read("a/b.csv.gz")
    store.probe("a/b.csv.gz")
    list(store.listing("p/"))

    assert len(replay.asked) == 3
    for asked in replay.asked:
        assert asked.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert not [name for name in asked.headers if name.lower() == "host"]


def test_the_frozen_transport_fails_a_request_that_carries_a_host_header() -> None:
    """The tripwire itself, so it cannot rot into a fixture that shrugs again."""
    _, replay = wire(lambda url, params: Response(200, b""))

    with pytest.raises(pytest.fail.Exception) as caught:
        replay("GET", f"{FILES_HOST}/{BUCKET}/a", {}, {"Host": "files.massive.com"}, [])

    assert "two Host headers" in str(caught.value)


def test_the_frozen_downloader_fails_a_download_that_carries_a_host_header(
    tmp_path: Path,
) -> None:
    """The same tripwire on the other way out, which is where the trap was already known."""

    def downloader(url: str, filepath: str, headers: Mapping[str, str]) -> None:
        Path(filepath).write_bytes(b"never reached")

    with pytest.raises(pytest.fail.Exception):
        guarded(downloader)("url", str(tmp_path / "b"), {"host": "files.massive.com"})


def test_a_download_sends_the_signature_and_never_a_second_host_header(
    tmp_path: Path,
) -> None:
    """Two Host headers is a 400 with an HTML body: signed, then dropped."""
    seen: dict[str, Any] = {}

    def downloader(url: str, filepath: str, headers: Mapping[str, str]) -> None:
        seen.update({"url": url, "path": filepath, "headers": dict(headers)})
        Path(filepath).write_bytes(b"object bytes")

    store, _ = wire(lambda url, params: Response(200, b""), download=downloader)

    written = store.fetch("a/b.csv.gz", tmp_path / "sub" / "b.csv.gz", byte_range=(0, 15))

    assert written.read_bytes() == b"object bytes"
    assert seen["url"] == f"{FILES_HOST}/{BUCKET}/a/b.csv.gz"
    assert "Host" not in seen["headers"]
    assert seen["headers"]["Range"] == "bytes=0-15"
    assert seen["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")


def test_a_failed_download_points_at_the_probe_that_can_say_why(tmp_path: Path) -> None:
    """The bulk download reports a status line and no code; the probe reports a code."""

    def downloader(url: str, filepath: str, headers: Mapping[str, str]) -> None:
        raise RuntimeError("HTTP error: 403 Forbidden")

    store, _ = wire(lambda url, params: Response(200, b""), download=downloader)

    with pytest.raises(TransportError) as caught:
        store.fetch("a/b.csv.gz", tmp_path / "b.csv.gz")

    assert "403 Forbidden" in caught.value.message
    assert "probe" in (caught.value.remedy or "")


def test_the_engine_is_what_streams_an_object_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no downloader injected, the call goes to the engine's own bulk download."""
    from nautilus_trader.core import nautilus_pyo3

    seen: dict[str, Any] = {}

    def fake(url: str, filepath: str, **kwargs: Any) -> None:
        seen.update({"url": url, "filepath": filepath, **kwargs})
        Path(filepath).write_bytes(b"streamed")

    monkeypatch.setattr(nautilus_pyo3, "http_download", fake)
    store, _ = wire(lambda url, params: Response(200, b""), timeout_s=11)

    store.fetch("a/b.csv.gz", tmp_path / "b.csv.gz")

    assert seen["timeout_secs"] == 11
    assert "Authorization" in seen["headers"]
    assert not [name for name in seen["headers"] if name.lower() == "host"]
    assert (tmp_path / "b.csv.gz").read_bytes() == b"streamed"


def test_a_store_names_its_bucket_and_never_a_credential() -> None:
    store, _ = wire(lambda url, params: Response(200, b""))

    assert store.url("a/b") == f"{FILES_HOST}/{BUCKET}/a/b"
    assert store.bucket_url() == f"{FILES_HOST}/{BUCKET}"
    assert STORE_SECRET not in repr(store)
    assert STORE_KEY_ID not in repr(store)
    assert BUCKET in repr(store)


def test_an_unanswered_reply_reports_the_status_it_never_got_past() -> None:
    reply = Reply(key="a", status=0, access=Access.UNAVAILABLE)

    with pytest.raises(TransportError) as caught:
        reply.raise_for_transport()

    assert caught.value.status == 0
    assert not reply.answered and not reply.served


def test_an_entry_is_a_key_a_size_and_a_time() -> None:
    assert Entry("k", 3, "t").payload() == {"key": "k", "size": 3, "modified": "t"}


# --- the bulk loader: what the store holds, and what the plan lets it serve ----

TICKER = "AAPL"
VENUE = "XNAS"
MARKET_ZONE = "America/New_York"
"""The zone the store's daily windows are anchored to.

A fixture fact and not a loader one: it is how a realistic `window_start` is written here.
The loader reads no zone at all, which is the point of this pass — `window_start` is the
start of the vendor's calendar day, and the bar's close is that instant plus the
resolution, so nothing about a session has to be supplied to place a bar."""

DAY_NS = 86_400 * 1_000_000_000

DAILY_SPEC: dict[str, object] = {
    "loader": "massive_bulk",
    "asset_class": "stocks",
    "venue": VENUE,
    "instruments": [TICKER],
    "resolution": "1d",
    "start": "2024-01-02",
    "end": "2024-01-08",
}


def midnight_ns(day: date) -> int:
    """The instant a daily window opens, as the store writes it.

    Midnight in the exchange's own zone — 04:00Z in summer and 05:00Z in winter — which is
    the start of the calendar day the vendor aggregates over and not the session's open.
    """
    return to_ns(datetime.combine(day, clock_time(0, 0), tzinfo=ZoneInfo(MARKET_ZONE)))


def aggregate(rows: Sequence[Mapping[str, object]], header: Sequence[str] = COLUMNS) -> bytes:
    """One gzipped aggregate object, written the way the store writes one."""
    lines = [",".join(header)]
    lines += [",".join(str(row[name]) for name in header) for row in rows]
    return gzip.compress(("\n".join(lines) + "\n").encode())


def agg_row(ticker: str, window_start: int, close: float = 100.0) -> dict[str, object]:
    """One aggregate row, timed in the nanoseconds the flat files use."""
    return {
        "ticker": ticker,
        "volume": 1000,
        "open": close,
        "close": close,
        "high": close + 1,
        "low": close - 1,
        "window_start": window_start,
        "transactions": 10,
    }


@dataclass
class Flat:
    """A frozen flat-file store: which days exist, which serve, and what each holds."""

    days: Sequence[date]
    objects: Mapping[date, bytes]
    floor: date | None = None
    resolution: str = "1d"
    asset_class: str = "stocks"
    page: int = 1000
    downloads: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [key_for(self.asset_class, self.resolution, day) for day in self.days]

    def answer(self, url: str, params: Mapping[str, str]) -> Response:
        if params.get("list-type"):
            return self._listing(params.get("continuation-token"))
        day = day_of(key_of(url))
        if day is None or day not in self.days:
            return Response(404, refusal_xml("NoSuchKey"))
        if self.floor is not None and day < self.floor:
            return Response(403, refusal_xml("AccessDenied"))
        return Response(206, self.objects.get(day, b"")[:1])

    def download(self, url: str, filepath: str, headers: Mapping[str, str]) -> None:
        key = key_of(url)
        self.downloads.append(key)
        day = day_of(key)
        assert day is not None
        Path(filepath).write_bytes(self.objects[day])

    def _listing(self, token: str | None) -> Response:
        start = int(token or 0)
        window = self.keys[start : start + self.page]
        following = start + self.page
        return Response(
            200,
            listing_xml(window, token=str(following) if following < len(self.keys) else None),
        )


Builder = Callable[..., tuple[BulkLoader, Flat, Replay]]
"""What the `flat` fixture hands a test: a bulk loader over a frozen store, caching under
the test's own scratch directory, and the two recorders behind it."""


@pytest.fixture
def flat(tmp_path: Path) -> Builder:
    def build(
        days: Sequence[date],
        *,
        floor: date | None = None,
        rows: Mapping[date, Sequence[Mapping[str, object]]] | None = None,
        resolution: str = "1d",
        asset_class: str = "stocks",
        page: int = 1000,
        header: Sequence[str] = COLUMNS,
    ) -> tuple[BulkLoader, Flat, Replay]:
        supplied = (
            rows if rows is not None else {day: [agg_row(TICKER, midnight_ns(day))] for day in days}
        )
        store_rows = {day: aggregate(supplied.get(day, ()), header) for day in days}
        fixture = Flat(
            days=days,
            objects=store_rows,
            floor=floor,
            resolution=resolution,
            asset_class=asset_class,
            page=page,
        )
        store, replay = wire(fixture.answer, download=fixture.download)
        return BulkLoader(store=store, bulk=Bulk(store), cache=tmp_path), fixture, replay

    return build


def week(start: date, count: int) -> list[date]:
    """`count` consecutive days from `start`."""
    return [start + timedelta(days=offset) for offset in range(count)]


def test_a_prefix_that_serves_its_oldest_object_has_that_object_as_its_floor(flat: Builder) -> None:
    """One read at each end answers both questions; nothing is bisected needlessly."""
    loader, _, replay = flat(week(date(2024, 1, 2), 7))

    found = loader.bulk.coverage("stocks", "1d")

    assert found.floor == date(2024, 1, 2)
    assert found.newest == date(2024, 1, 8)
    assert len(found.days) == 7
    assert len(replay.asked) == 3, "one listing page, the newest object and the oldest"


def test_a_prefix_refused_before_a_date_reports_a_floor_and_not_a_plan(flat: Builder) -> None:
    """The newest object serves, so the old refusals are history, not entitlement.

    Reporting this as `not entitled` is the single most expensive wrong answer here: it
    sends an operator to buy a subscription they already hold.
    """
    days = week(date(2024, 1, 1), 64)
    loader, _, replay = flat(days, floor=date(2024, 2, 1))

    found = loader.bulk.coverage("stocks", "1d")

    assert found.floor == date(2024, 2, 1)
    assert len(replay.asked) < len(days), "halved over the listed days, not read one by one"


def test_a_prefix_whose_newest_object_is_refused_is_a_plan_and_not_a_floor(flat: Builder) -> None:
    """Listing is not scoped by plan, so only a GET can tell these two apart."""
    days = week(date(2024, 1, 2), 5)
    loader, _, _ = flat(days, floor=date(2030, 1, 1))

    with pytest.raises(NotEntitledError) as caught:
        loader.bulk.coverage("stocks", "1d")

    assert "not a history-floor failure" in (caught.value.remedy or "")
    assert caught.value.fatal is False


def test_a_prefix_with_no_object_of_this_layout_serves_no_bulk_history(flat: Builder) -> None:
    loader, _, _ = flat([])

    with pytest.raises(NotEntitledError) as caught:
        loader.bulk.coverage("stocks", "1d")

    assert "no object of this layout" in caught.value.message


def test_the_measurement_is_taken_once_however_many_chunks_ask_for_it(flat: Builder) -> None:
    """A backfill walks years in chunks; re-listing per chunk would cost a run."""
    loader, _, replay = flat(week(date(2024, 1, 2), 5))

    loader.bulk.coverage("stocks", "1d")
    before = len(replay.asked)
    loader.bulk.coverage("stocks", "1d")

    assert len(replay.asked) == before


def test_a_listing_is_walked_to_its_end_before_a_floor_is_measured(flat: Builder) -> None:
    loader, fixture, _ = flat(week(date(2024, 1, 2), 7), page=2)

    found = loader.bulk.coverage("stocks", "1d")

    assert len(found.days) == len(fixture.keys) == 7


# --- what the loader serves ---------------------------------------------------


def test_a_daily_bar_closes_a_resolution_after_its_window_opens(flat: Builder) -> None:
    """`window_start` is the start of the vendor's calendar day and not the session's open.

    So the close is that instant plus the resolution, which lands a daily bar on the UTC day
    after the one its window opened in — exactly where the request path lands the same bar.
    This fixture used to assert a session close the spec had to supply, which is how the two
    transports could stamp one session at two instants with every test green.
    """
    days = week(date(2024, 1, 2), 3)
    loader, _, _ = flat(days)

    ref = loader.discover(DAILY_SPEC)[0]
    bars = list(loader.load(ref, ref.span))

    assert [bar.ts_event for bar in bars] == [midnight_ns(day) + DAY_NS for day in days]
    assert [utc_day(bar.ts_event) for bar in bars] == [day + timedelta(days=1) for day in days]
    assert all(bar.ts_init == bar.ts_event for bar in bars)
    assert str(bars[0].bar_type) == f"{TICKER}.{VENUE}-1-DAY-LAST-EXTERNAL"


def test_a_row_timed_in_the_rest_epoch_falls_out_of_the_window_rather_than_into_it(
    flat: Builder,
) -> None:
    """The two epochs differ by a million; read as milliseconds, a 2024 window opens in 1970.

    Which is not the range the dataset asked for, so the mistake serves nothing instead of
    placing a bar half a century early and calling it coverage.
    """
    day = date(2024, 1, 2)
    as_millis = midnight_ns(day) // 1_000_000
    loader, _, _ = flat([day], rows={day: [agg_row(TICKER, as_millis)]})

    ref = loader.discover({**DAILY_SPEC, "start": "2024-01-03", "end": "2024-01-03"})[0]

    assert list(loader.load(ref, ref.span)) == []


def test_a_minute_bar_closes_one_step_after_its_window_opens(flat: Builder) -> None:
    """An intraday window's close is derivable, so no session fact is needed or accepted."""
    day = date(2024, 1, 2)
    opened = to_ns(datetime(2024, 1, 2, 14, 31, tzinfo=UTC))
    loader, _, _ = flat([day], rows={day: [agg_row(TICKER, opened)]}, resolution="1m")

    ref = loader.discover(
        {
            "loader": "massive_bulk",
            "asset_class": "stocks",
            "venue": VENUE,
            "instruments": [TICKER],
            "resolution": "1m",
            "start": "2024-01-02",
            "end": "2024-01-02",
        }
    )[0]
    bars = list(loader.load(ref, ref.span))

    assert bars[0].ts_event == opened + 60 * 1_000_000_000


def test_only_the_ticker_of_the_dataset_comes_out_of_a_day_that_holds_them_all(
    flat: Builder,
) -> None:
    """One object per day holds the whole class; a dataset is one name out of it."""
    day = date(2024, 1, 2)
    loader, _, _ = flat(
        [day],
        rows={day: [agg_row("MSFT", midnight_ns(day), 400.0), agg_row(TICKER, midnight_ns(day))]},
    )

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]
    bars = list(loader.load(ref, ref.span))

    assert len(bars) == 1
    assert float(bars[0].close) == 100.0


def test_a_day_is_downloaded_once_and_read_from_the_cache_after_that(
    flat: Builder, tmp_path: Path
) -> None:
    """An interrupted backfill re-reads what it already fetched rather than the network."""
    days = week(date(2024, 1, 2), 2)
    loader, fixture, _ = flat(days)

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]
    list(loader.load(ref, ref.span))
    list(loader.load(ref, ref.span))

    assert len(fixture.downloads) == 2
    assert (tmp_path / key_for("stocks", "1d", days[0])).is_file()


def test_the_manifest_records_the_span_that_was_served_not_the_one_asked_for(flat: Builder) -> None:
    """A day whose object holds no row for this name is coverage the dataset does not have."""
    days = week(date(2024, 1, 2), 5)
    held = {day: [agg_row(TICKER, midnight_ns(day))] for day in days[:3]}
    loader, _, _ = flat(days, rows=held)

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-06"})[0]
    manifest = loader.manifest(ref)

    assert ref.span == (date(2024, 1, 3), date(2024, 1, 6))
    assert manifest.span == (date(2024, 1, 3), date(2024, 1, 5))
    assert manifest.row_count == 3
    assert shortfall(ref.span, manifest.span) is not None
    assert manifest.vendor == "massive"
    assert manifest.vendor_dataset == "us_stocks_sip/day_aggs_v1/"
    assert manifest.publication == "realtime"


def test_a_discovered_span_runs_from_the_floor_s_close_to_the_newest_object_s(
    flat: Builder,
) -> None:
    """`backfill` clamps to a floor it was told rather than to one it guessed.

    Stated in reference days, so both ends of the store's measurement are shifted by the
    resolution: the oldest object the plan serves is the oldest *window*, and the bar that
    window carries belongs to the following day.
    """
    days = week(date(2024, 1, 2), 10)
    loader, _, _ = flat(days, floor=date(2024, 1, 6))

    ref = loader.discover({**DAILY_SPEC, "start": "2020-01-01", "end": "2030-01-01"})[0]

    assert ref.span == (date(2024, 1, 7), date(2024, 1, 12))


def test_a_range_that_lies_outside_what_the_store_serves_is_refused_by_name(flat: Builder) -> None:
    loader, _, _ = flat(week(date(2024, 1, 2), 3))

    with pytest.raises(ValidationError) as caught:
        loader.discover({**DAILY_SPEC, "start": "2020-01-01", "end": "2020-02-01"})

    assert "do not meet" in caught.value.message


def test_the_arrow_path_yields_the_catalog_s_own_tables(flat: Builder) -> None:
    loader, _, _ = flat(week(date(2024, 1, 2), 2))

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]
    tables = list(loader.load_arrow(ref, ref.span) or ())

    assert sum(table.num_rows for table in tables) == 2


def test_a_ref_carries_everything_a_later_process_needs_to_extend_it(flat: Builder) -> None:
    """`data sync` rebuilds a ref from the manifest, without the spec that first wrote it."""
    loader, _, _ = flat(week(date(2024, 1, 2), 2))

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]

    assert Series.of(ref) == Series(
        asset_class="stocks",
        resolution="1d",
        venue=VENUE,
        symbol=TICKER,
        ticker=TICKER,
    )
    assert not any(STORE_SECRET in value for value in (ref.request_params or {}).values())


def test_a_ref_that_was_not_discovered_here_is_refused_rather_than_guessed_at(
    flat: Builder,
) -> None:
    loader, _, _ = flat(week(date(2024, 1, 2), 2))
    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]
    handmade = replace(ref, request_params={"ticker": TICKER, "symbol": TICKER})

    with pytest.raises(ValidationError) as caught:
        loader.load(handmade, ref.span)

    assert "asset_class" in caught.value.message


# --- the two transports, over one series --------------------------------------

FLAT_WINDOW_START = 1_785_729_600_000_000_000
"""What the store's `2026-08-03.csv.gz` carries in `window_start` for AAPL. Measured."""

REST_T = 1_785_729_600_000
"""What `/v2/aggs/ticker/AAPL/range/1/day/...` carries in `t` for the same day. Measured."""

FIRST_SESSION = date(2026, 8, 3)
PROBE_DAY = date(2026, 9, 5)
LAST_SESSION = settled_end(PROBE_DAY)


def sessions() -> list[date]:
    """Every calendar day both transports are given a row for; a fixture keeps no calendar."""
    span = (LAST_SESSION - FIRST_SESSION).days
    return [FIRST_SESSION + timedelta(days=step) for step in range(span + 1)]


def rest_row(day: date, close: float = 100.0) -> dict[str, Any]:
    """The REST aggregate for one day, carrying the row `agg_row` writes into a file.

    Same window, same prices, same volume — spelled the way the API spells them and timed
    in the milliseconds it times them in. The pair is what makes the comparison below a
    comparison of two paths rather than of two fixtures.
    """
    return {
        "t": midnight_ns(day) // 1_000_000,
        "o": close,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": 1000,
        "n": 10,
    }


def rest_bars_loader(**over: Any) -> MassiveBarsLoader:
    """The request-path loader over a frozen vendor serving those same days.

    `over` edits every row it serves, which is how one malformed vendor answer is put on
    both transports at once and the two refusals compared.
    """

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition(TICKER)])
        start, end = window_of(url)
        rows = [{**rest_row(day), **over} for day in sessions() if start <= day <= end]
        return served_rows(rows) if rows else nothing()

    client = MassiveClient("frozen-key-not-a-credential", transport=Replay(answer))
    return MassiveBarsLoader(client=client, as_of=PROBE_DAY)


def test_the_two_epochs_name_one_instant_in_different_units() -> None:
    """The measurement both loaders are built on, taken on AAPL's 2026-08-03 session.

    `window_start` is not the session's open. It is the start of the vendor's calendar day
    — midnight in the exchange's zone, so 04:00Z in summer and 05:00Z in winter — and it is
    the same instant the REST aggregate calls `t`, a million times coarser.
    """
    assert FLAT_WINDOW_START == REST_T * 1_000_000
    assert midnight_ns(FIRST_SESSION) == FLAT_WINDOW_START
    assert datetime.fromtimestamp(REST_T / 1000, UTC) == datetime(2026, 8, 3, 4, tzinfo=UTC)


def test_the_bulk_and_the_request_path_serve_one_session_as_one_bar(flat: Builder) -> None:
    """One session is one bar, whichever transport served it.

    A `data backfill` over the store followed by a `data sync` over the API must extend one
    series, not interleave two conventions in it — and M5 asks for exactly that sequence
    over both paths. So the same vendor rows are served over both transports and the two
    loaders are asked the same operator range; what is compared is one path against the
    other rather than either against a constant, so a rule that drifts on one side fails
    here while that side is still perfectly self-consistent.
    """
    days = sessions()
    window = (date(2026, 8, 10), date(2026, 8, 20))
    spec: dict[str, object] = {**DAILY_SPEC, "start": "2026-08-01", "end": "2026-09-30"}
    over_store, _, _ = flat(days, rows={day: [agg_row(TICKER, midnight_ns(day))] for day in days})
    over_api = rest_bars_loader()

    bulk_ref = over_store.discover(spec)[0]
    api_ref = over_api.discover({**spec, "loader": None})[0]
    from_store = list(over_store.load(bulk_ref, window))
    from_api = list(over_api.load(api_ref, window))

    assert bulk_ref.span == api_ref.span == (date(2026, 8, 4), LAST_SESSION + timedelta(days=1))
    assert len(from_store) == (window[1] - window[0]).days + 1
    assert [type(point).to_dict(point) for point in from_store] == [
        type(point).to_dict(point) for point in from_api
    ]


BROKEN_REST = {"h": 95.0, "l": 99.0}
"""One aggregate whose high is below its open, as the API spells it. The engine refuses
such a row at construction, which is the one refusal both transports have to share."""

BROKEN_FLAT = {"high": 95.0, "low": 99.0}
"""The same aggregate as the store spells it: same numbers, the file's own column names."""


def test_a_row_the_engine_refuses_is_the_same_refusal_over_either_transport(flat: Builder) -> None:
    """A vendor row the engine will not accept is one vendor answer, not two.

    Both transports hand the row to the one builder, so both fail as a kanso validation
    naming the series and carrying a remedy. When only the request path did that, the same
    day over the store surfaced as a bare `ValueError` — exit 1 rather than 3, with no
    instrument, no remedy and nothing to tell an operator which of the two paths was even
    at fault.
    """
    days = sessions()
    rows = {day: [agg_row(TICKER, midnight_ns(day))] for day in days}
    rows[FIRST_SESSION] = [{**rows[FIRST_SESSION][0], **BROKEN_FLAT}]
    spec: dict[str, object] = {**DAILY_SPEC, "start": "2026-08-01", "end": "2026-09-30"}
    over_store, _, _ = flat(days, rows=rows)
    over_api = rest_bars_loader(**BROKEN_REST)

    bulk_ref = over_store.discover(spec)[0]
    api_ref = over_api.discover({**spec, "loader": None})[0]

    with pytest.raises(ValidationError) as from_store:
        list(over_store.load(bulk_ref, bulk_ref.span))
    with pytest.raises(ValidationError) as from_api:
        list(over_api.load(api_ref, api_ref.span))

    assert from_store.value.message == from_api.value.message
    assert from_store.value.remedy == from_api.value.remedy
    assert f"massive bars {TICKER}" in from_store.value.message


def test_a_delayed_plan_stamps_one_availability_over_either_transport(flat: Builder) -> None:
    """`publication` describes the plan, so it is a bulk spec's field as much as a request
    spec's — and reaches the points the same way.

    A tier that publishes late publishes late whichever way a day was fetched. While only
    the request path could declare it, a bulk-filled and API-extended history filed under
    one key carried `ts_init == ts_event` for the half fetched over the store and a lagged
    stamp for the half fetched over the API, which is look-ahead in the older half.
    """
    days = sessions()
    window = (date(2026, 8, 10), date(2026, 8, 20))
    delayed: dict[str, object] = {
        **DAILY_SPEC,
        "start": "2026-08-01",
        "end": "2026-09-30",
        "publication": "delayed",
        "publication_rule": "official_close",
    }
    over_store, _, _ = flat(days, rows={day: [agg_row(TICKER, midnight_ns(day))] for day in days})
    over_api = rest_bars_loader()

    bulk_ref = over_store.discover(delayed)[0]
    api_ref = over_api.discover({**delayed, "loader": None})[0]
    from_store = list(over_store.load(bulk_ref, window))
    from_api = list(over_api.load(api_ref, window))

    assert bulk_ref.publication == "delayed"
    assert bulk_ref.publication_rule == "official_close"
    assert [type(point).to_dict(point) for point in from_store] == [
        type(point).to_dict(point) for point in from_api
    ]
    assert all(point.ts_init == point.ts_event + 15 * 60 * 1_000_000_000 for point in from_store)


def test_a_delayed_bulk_spec_that_names_no_rule_is_refused_by_name() -> None:
    """Refused for the reason, rather than for naming a field the model does not know."""
    with pytest.raises(ValidationError) as caught:
        BulkSpec.model_validate({**DAILY_SPEC, "publication": "delayed"})
    assert "publication_rule" in str(caught.value)

    with pytest.raises(ValidationError) as unknown:
        BulkSpec.model_validate({**DAILY_SPEC, "publication": "eventually"})
    assert "is not a publication class" in str(unknown.value)


def test_an_interrupted_download_leaves_nothing_the_next_run_would_read(
    tmp_path: Path,
) -> None:
    """The cache is consulted by whether a file is there, so a half-written one is poison.

    A download that stops mid-stream used to land its bytes on the object's own name, and
    every later run then found the file, skipped the fetch and failed to decompress it —
    for good, since nothing re-downloads a day the cache appears to hold. The object is
    written beside itself and renamed on, so an interruption leaves the cache as it was.
    """
    day = date(2024, 1, 2)
    complete = aggregate([agg_row(TICKER, midnight_ns(day))])
    fail = True

    def download(url: str, filepath: str, headers: Mapping[str, str]) -> None:
        if not fail:
            Path(filepath).write_bytes(complete)
            return
        Path(filepath).write_bytes(complete[: len(complete) // 2])
        raise OSError("the connection dropped half way through")

    fixture = Flat(days=[day], objects={day: complete})
    store, _ = wire(fixture.answer, download=download)
    loader = BulkLoader(store=store, bulk=Bulk(store), cache=tmp_path)
    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]

    with pytest.raises(TransportError):
        list(loader.load(ref, ref.span))

    cached = tmp_path / key_for("stocks", "1d", day)
    assert not cached.exists()
    assert [item.name for item in cached.parent.iterdir()] == []

    fail = False
    assert len(list(loader.load(ref, ref.span))) == 1


def test_a_cached_object_that_is_not_readable_gzip_names_the_file_to_delete(
    flat: Builder, tmp_path: Path
) -> None:
    """However it got there, the only remedy is deleting it, and it has to be nameable."""
    day = date(2024, 1, 2)
    loader, _, _ = flat([day])
    poisoned = tmp_path / key_for("stocks", "1d", day)
    poisoned.parent.mkdir(parents=True, exist_ok=True)
    poisoned.write_bytes(gzip.compress(b"ticker,volume\n")[:6])

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]

    with pytest.raises(ValidationError) as caught:
        list(loader.load(ref, ref.span))

    assert str(poisoned) in caught.value.message
    assert str(poisoned) in (caught.value.remedy or "")


def test_a_ref_that_names_no_venue_is_refused_for_the_venue_and_not_for_the_id(
    flat: Builder,
) -> None:
    """`venue` is as required as the four beside it: an instrument id is symbol and venue."""
    loader, _, _ = flat(week(date(2024, 1, 2), 2))
    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]
    params = dict(ref.request_params or {})
    del params["venue"]

    with pytest.raises(ValidationError) as caught:
        loader.load(replace(ref, request_params=params), ref.span)

    assert "venue absent" in caught.value.message


# --- what the loader refuses --------------------------------------------------


def test_a_file_is_read_by_column_name_so_a_reordering_cannot_swap_two_fields(
    flat: Builder,
) -> None:
    """Read positionally, a vendor moving `close` before `open` would still validate."""
    day = date(2024, 1, 2)
    swapped = ("ticker", "volume", "close", "open", "high", "low", "window_start", "transactions")
    loader, _, _ = flat(
        [day], rows={day: [agg_row(TICKER, midnight_ns(day), 100.0)]}, header=swapped
    )

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]
    bars = list(loader.load(ref, ref.span))

    assert float(bars[0].open) == 100.0 and float(bars[0].high) == 101.0


def test_a_file_missing_a_column_this_loader_maps_is_refused(flat: Builder) -> None:
    day = date(2024, 1, 2)
    loader, _, _ = flat([day], header=COLUMNS[:-1])

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]

    with pytest.raises(ValidationError) as caught:
        list(loader.load(ref, ref.span))

    assert "changed its layout" in (caught.value.remedy or "")


def test_a_window_start_that_is_not_a_whole_number_is_refused(flat: Builder) -> None:
    day = date(2024, 1, 2)
    loader, _, _ = flat([day], rows={day: [{**agg_row(TICKER, 0), "window_start": "noon"}]})

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]

    with pytest.raises(ValidationError) as caught:
        list(loader.load(ref, ref.span))

    assert "nanoseconds" in caught.value.message


def test_a_price_that_is_not_a_number_is_refused(flat: Builder) -> None:
    day = date(2024, 1, 2)
    loader, _, _ = flat([day], rows={day: [{**agg_row(TICKER, midnight_ns(day)), "open": "n/a"}]})

    ref = loader.discover({**DAILY_SPEC, "end": "2024-01-04"})[0]

    with pytest.raises(ValidationError) as caught:
        list(loader.load(ref, ref.span))

    assert "not a number" in caught.value.message


# --- the spec -----------------------------------------------------------------


def test_a_spec_names_no_session_and_no_zone_at_any_resolution() -> None:
    """No spec declares a session close or the zone it is stated in, at any resolution.

    A daily bar's close is its window start plus a day, which the file and the resolution
    settle between them, so there is nothing about a session left for an operator to
    declare. The two fields were once required of a daily spec, and supplying them put every
    daily bar at a different instant than the request path put the same bar at.
    """
    assert "session_close" not in BulkSpec.model_fields
    assert "timezone" not in BulkSpec.model_fields
    assert BulkSpec.model_validate({**DAILY_SPEC, "resolution": "1m"}).resolution == "1m"


def test_a_spec_refuses_a_backwards_range_and_a_repeated_ticker() -> None:
    with pytest.raises(ValidationError) as caught:
        BulkSpec.model_validate({**DAILY_SPEC, "start": "2024-02-01", "end": "2024-01-01"})
    assert "is before start" in str(caught.value)

    with pytest.raises(ValidationError) as twice:
        BulkSpec.model_validate({**DAILY_SPEC, "instruments": [TICKER, TICKER]})
    assert "named twice" in str(twice.value)

    with pytest.raises(ValidationError) as stray:
        BulkSpec.model_validate({**DAILY_SPEC, "tickers": {"MSFT": "MSFT"}})
    assert "never be used" in str(stray.value)


def test_the_store_carries_two_aggregates_and_says_so_when_asked_for_more() -> None:
    """Ticks are served over the request path; their column layout is not measured here."""
    assert prefix_for("stocks", "1d") == "us_stocks_sip/day_aggs_v1/"
    assert prefix_for("options", "1m") == "us_options_opra/minute_aggs_v1/"

    with pytest.raises(ValidationError) as caught:
        prefix_for("stocks", "5m")
    assert "request-path loaders" in (caught.value.remedy or "")

    with pytest.raises(ValidationError):
        prefix_for("bonds", "1d")


def test_a_class_this_loader_has_no_vendor_key_for_is_refused_by_the_spec() -> None:
    """The store lays out more classes than the adapter spells tickers for.

    Accepting one of those would spend a listing and an entitlement read and then fail on
    the ticker, contradicting the validator that had just accepted it — so the class map
    here is the intersection, and the refusal happens before anything is spent.
    """
    with pytest.raises(ValidationError) as caught:
        BulkSpec.model_validate({**DAILY_SPEC, "asset_class": "crypto"})

    assert "crypto" in caught.value.message
    assert "request-path loaders" in (caught.value.remedy or "")


def test_an_object_key_names_its_class_its_type_and_its_day() -> None:
    assert key_for("stocks", "1d", date(2024, 1, 2)) == (
        "us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.csv.gz"
    )
    assert day_of("us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.csv.gz") == date(2024, 1, 2)
    assert day_of("us_stocks_sip/day_aggs_v1/2024/01/manifest.json") is None
    assert day_of("us_stocks_sip/day_aggs_v1/2024/01/not-a-date.csv.gz") is None


def test_coverage_reports_only_the_days_it_both_holds_and_serves(flat: Builder) -> None:
    days = week(date(2024, 1, 2), 5)
    loader, _, _ = flat(days, floor=date(2024, 1, 4))

    found = loader.bulk.coverage("stocks", "1d")

    assert found.within((date(2024, 1, 1), date(2024, 1, 31))) == tuple(days[2:])


# --- opening the store from a workspace ---------------------------------------


def workspace(tmp_path: Path) -> Workspace:
    return init(tmp_path / "ws")


def test_a_prefixed_class_keeps_its_prefix_in_the_key_and_out_of_the_instrument_id(
    flat: Builder,
) -> None:
    """The object's `ticker` column holds the vendor key; a kanso id names a venue instead."""
    day = date(2024, 1, 2)
    loader, _, _ = flat(
        [day], rows={day: [agg_row("C:EURUSD", midnight_ns(day), 1.09)]}, asset_class="forex"
    )

    ref = loader.discover(
        {
            **DAILY_SPEC,
            "asset_class": "forex",
            "venue": "SIM",
            "instruments": ["EURUSD"],
            "end": "2024-01-03",
            "price_precision": 5,
        }
    )[0]
    bars = list(loader.load(ref, ref.span))

    assert Series.of(ref).ticker == "C:EURUSD"
    assert ref.instrument == "EURUSD.SIM"
    assert len(bars) == 1


def test_an_operator_may_override_the_key_a_symbol_derives(flat: Builder, tmp_path: Path) -> None:
    """A spelling the convention does not reach is named rather than worked around."""
    day = date(2024, 1, 2)
    loader, _, _ = flat([day], rows={day: [agg_row("BRK.B", midnight_ns(day))]})

    ref = loader.discover(
        {**DAILY_SPEC, "instruments": ["BRKB"], "tickers": {"BRKB": "BRK.B"}, "end": "2024-01-03"}
    )[0]

    assert Series.of(ref).ticker == "BRK.B"
    assert len(list(loader.load(ref, ref.span))) == 1


def test_the_two_object_store_credentials_are_required_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each is resolved on its own; that two share a value today is nowhere relied on."""
    ws = workspace(tmp_path)
    monkeypatch.delenv("KANSO_MASSIVE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("KANSO_MASSIVE_SECRET_KEY", raising=False)

    with pytest.raises(PreconditionError) as caught:
        bulk.loader(ws)

    assert "KANSO_MASSIVE_ACCESS_KEY_ID" in caught.value.message


def test_a_workspace_loader_caches_under_the_catalog_s_own_scratch_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = workspace(tmp_path)
    monkeypatch.setenv("KANSO_MASSIVE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("KANSO_MASSIVE_ACCESS_KEY_ID", "not-a-real-id")
    monkeypatch.setenv("KANSO_MASSIVE_SECRET_KEY", "not-a-real-secret")

    built = bulk.loader(ws)

    assert built.id == "massive_bulk"
    assert built.cache == ws.path("catalog", ".cache")


def test_a_workspace_loader_may_be_handed_the_transport_the_suite_replays_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = workspace(tmp_path)
    monkeypatch.setenv("KANSO_MASSIVE_ACCESS_KEY_ID", "not-a-real-id")
    monkeypatch.setenv("KANSO_MASSIVE_SECRET_KEY", "not-a-real-secret")
    replay = Replay(lambda url, params: Response(200, listing_xml([])))

    built = bulk.loader(ws, transport=replay, cache=tmp_path / "scratch")

    assert built.cache == tmp_path / "scratch"
    assert list(built.store.listing("p/")) == []
