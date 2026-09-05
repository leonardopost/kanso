"""The client and the adapter declaration: header auth, one quota, and no interpretation.

Two properties are under test here. The client sends the credential in a header and puts
it nowhere else, and it reduces every answer to a signal without reading the vendor's
prose — which is what leaves the four outcomes separable further up. The declaration is
under test for the third: it names three variables and offers a set of classes, and it
declares no entitlement and no history floor, because both are measurements.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kanso.data.adapters.massive import (
    ACCESS_KEY_ID,
    ADAPTER,
    API_KEY,
    CAPABILITIES,
    CREDENTIALS,
    ID,
    KIND,
    SECRET_KEY,
    MassiveConfig,
)
from kanso.data.adapters.massive.client import (
    API_HOST,
    DEFAULT_REQUESTS_PER_SECOND,
    MassiveClient,
    Response,
    Signal,
    _method,
    pyo3_transport,
    signal_of,
)
from kanso.data.adapters.massive.errors import TransportError
from kanso.errors import Exit, ValidationError
from kanso.workspace import Workspace, init

from . import WARNING, Replay, bar, body, definition, nothing, refused, rejected, served

KEY = "test-key-not-a-secret"


def client(answer: Any, **kwargs: Any) -> tuple[MassiveClient, Replay]:
    """A client over a frozen wire, and the transport that recorded what it asked."""
    replay = Replay(answer)
    return MassiveClient(KEY, transport=replay, **kwargs), replay


# --- the credential -----------------------------------------------------------


def test_the_key_travels_in_a_header_and_never_in_the_query_string() -> None:
    """A query string reaches proxy logs and manifests; a header reaches neither."""
    reader, replay = client(lambda url, params: served([bar(date(2024, 3, 1))]))

    reader.call("/v2/aggs/ticker/AAPL/range/1/day/2024-03-01/2024-03-02", {"sort": "asc"})

    asked = replay.asked[0]
    assert asked.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in asked.url
    assert KEY not in str(asked.params)


def test_a_repr_never_carries_the_key() -> None:
    """A repr reaches tracebacks and crash reports, which reach issue trackers."""
    reader, _ = client(lambda url, params: nothing())

    assert KEY not in repr(reader)
    assert reader.base_url in repr(reader)


def test_the_quota_is_reported_as_the_rate_it_enforces() -> None:
    reader, _ = client(lambda url, params: nothing(), requests_per_second=17)

    assert reader.quota == "17/s"


def test_parameters_are_sent_in_a_stable_order() -> None:
    """Two identical requests must look identical, so a cache and a log can compare them."""
    reader, replay = client(lambda url, params: nothing())

    reader.call("/v3/reference/tickers", {"sort": "asc", "adjusted": "false"})

    assert list(replay.asked[0].params) == ["adjusted", "sort"]


def test_a_relative_path_is_joined_to_the_host_and_a_cursor_url_is_left_alone() -> None:
    reader, replay = client(lambda url, params: nothing())

    reader.call("/v3/reference/tickers")
    reader.call("https://elsewhere.example/v3/reference/tickers?cursor=abc")

    assert replay.paths[0] == f"{API_HOST}/v3/reference/tickers"
    assert replay.paths[1] == "https://elsewhere.example/v3/reference/tickers?cursor=abc"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-02", ["v2/aggs", "v2"]),
        ("https://api.massive.com/v3/reference/tickers?cursor=abc", ["v3/reference", "v3"]),
        ("/v3", ["v3"]),
        ("https://api.massive.com", []),
        ("/", []),
    ],
)
def test_a_request_counts_against_its_endpoint_family(path: str, expected: list[str]) -> None:
    """The buckets a keyed quota would later use, coarsest last."""
    reader, replay = client(lambda url, params: nothing())

    reader.call(path)

    assert list(replay.asked[0].keys) == expected


# --- the signal, which is never the message -----------------------------------


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"status": "OK", "results": [{"t": 1}]}, Signal.ROWS),
        (200, {"status": "DELAYED", "results": [{"t": 1}]}, Signal.ROWS),
        (200, {"status": "OK", "results": []}, Signal.NO_ROWS),
        (200, {"results": [{"t": 1}]}, Signal.ROWS),
        (200, {"status": "NOT_AUTHORIZED", "message": WARNING}, Signal.REFUSED),
        (403, {"status": "NOT_AUTHORIZED", "message": WARNING}, Signal.REFUSED),
        (401, {"message": WARNING}, Signal.REFUSED),
        (400, {"status": "ERROR", "message": WARNING}, Signal.BAD_REQUEST),
        (404, {"message": "not found"}, Signal.BAD_REQUEST),
        (429, {"message": "slow down"}, Signal.THROTTLED),
        (503, {"message": "unavailable"}, Signal.UNAVAILABLE),
        (302, {"message": "moved"}, Signal.UNAVAILABLE),
    ],
)
def test_the_signal_comes_from_the_status_and_the_shape(
    status: int, payload: dict[str, Any], expected: Signal
) -> None:
    reader, _ = client(lambda url, params: body(payload, status))

    assert reader.call("/v2/x").signal is expected


def test_the_same_sentence_under_two_transports_is_the_same_signal() -> None:
    """The vendor states a refusal as a client error and as a 200; both are one signal.

    Neither is interpreted here: `refused` is where a probe starts, not where it ends.
    """
    reader, _ = client(lambda url, params: refused(403 if "a" in url else 200))

    assert reader.call("/v2/a").signal is Signal.REFUSED
    assert reader.call("/v2/b").signal is Signal.REFUSED


def test_replacing_the_vendor_s_message_changes_nothing() -> None:
    """Proof by construction that no branch reads the prose."""
    plain = body({"status": "NOT_AUTHORIZED", "message": WARNING}, 403)
    other = body({"status": "NOT_AUTHORIZED", "message": "something else entirely"}, 403)

    assert signal_of(plain.status, {"status": "NOT_AUTHORIZED"}) is signal_of(
        other.status, {"status": "NOT_AUTHORIZED"}
    )


def test_a_body_that_is_not_a_json_object_is_unreadable_rather_than_empty() -> None:
    """A gateway's HTML page is not an empty result, and one of the four outcomes hangs
    on the difference."""
    html = Response(status=200, body=b"<html>400 Bad Request</html>")
    array = Response(status=200, body=b"[1, 2, 3]")

    assert signal_of(html.status, None) is Signal.UNREADABLE
    reader, _ = client(lambda url, params: array)
    assert reader.call("/v2/x").signal is Signal.UNREADABLE


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([{"t": 1}, {"t": 2}], 2),
        ({"ticker": "AAPL"}, 1),
        ([{"t": 1}, "junk"], 1),
        (None, 0),
    ],
)
def test_results_are_read_as_rows_whatever_shape_they_arrive_in(
    results: Any, expected: int
) -> None:
    """One endpoint answers with a list and another with a single object; both are rows."""
    payload: dict[str, Any] = {"status": "OK"}
    if results is not None:
        payload["results"] = results
    reader, _ = client(lambda url, params: body(payload))

    assert len(reader.call("/v3/x").rows) == expected


def test_the_evidence_of_a_call_carries_no_credential_and_no_vendor_prose() -> None:
    """Evidence is what a probe records and an operator reads; the sentence means four
    things, so it is not among it."""
    reader, _ = client(lambda url, params: refused())

    evidence = reader.call("/v2/aggs/ticker/I:SPX/range/1/day/2024-01-01/2024-02-01").evidence()

    assert WARNING not in str(evidence)
    assert KEY not in str(evidence)
    assert evidence["signal"] == "refused"
    assert evidence["vendor_status"] == "NOT_AUTHORIZED"
    assert evidence["request_id"] == "frozen"


# --- an answer versus no answer ------------------------------------------------


@pytest.mark.parametrize("status", [200, 403, 400])
def test_an_answer_of_any_kind_returns_rather_than_raises(status: int) -> None:
    """A refusal is evidence a probe needs, so it comes back rather than being thrown."""
    reader, _ = client(lambda url, params: body({"status": "OK", "results": []}, status))

    reader.call("/v2/x").raise_for_transport()


@pytest.mark.parametrize("status", [429, 500])
def test_the_absence_of_an_answer_fails_the_step(status: int) -> None:
    reader, _ = client(lambda url, params: body({"message": "later"}, status))

    with pytest.raises(TransportError) as caught:
        reader.call("/v2/x").raise_for_transport()

    assert caught.value.code == Exit.ERROR
    assert caught.value.status == status


def test_a_transport_that_raises_becomes_one_outcome() -> None:
    def explode(url: str, params: Mapping[str, str]) -> Response:
        raise OSError("no route to host")

    reader, _ = client(explode)

    with pytest.raises(TransportError, match="could not be reached"):
        reader.call("/v2/x")


def test_a_transport_failure_from_below_is_not_wrapped_twice() -> None:
    def explode(url: str, params: Mapping[str, str]) -> Response:
        raise TransportError("the engine's client refused to build")

    reader, _ = client(explode)

    with pytest.raises(TransportError, match="refused to build"):
        reader.call("/v2/x")


# --- cursors -------------------------------------------------------------------


def test_a_cursor_is_walked_to_its_end() -> None:
    pages = {
        "/v3/reference/tickers": served([definition("AAPL")], next_url="https://x/second"),
        "https://x/second": served([definition("MSFT")]),
    }
    reader, _ = client(lambda url, params: pages[url.replace(API_HOST, "")])

    assert [row["ticker"] for row in reader.rows("/v3/reference/tickers")] == ["AAPL", "MSFT"]


def test_a_page_that_is_not_rows_ends_the_walk() -> None:
    """A universe whose second page is refused is a short universe, not an error here."""
    pages = {
        "/v3/reference/tickers": served([definition("AAPL")], next_url="https://x/second"),
        "https://x/second": refused(),
    }
    reader, _ = client(lambda url, params: pages[url.replace(API_HOST, "")])

    walked = list(reader.pages("/v3/reference/tickers"))

    assert [page.signal for page in walked] == [Signal.ROWS, Signal.REFUSED]


def test_a_cursor_that_points_at_itself_ends_the_walk() -> None:
    reader, replay = client(
        lambda url, params: served([definition("AAPL")], next_url="https://x/same")
    )

    walked = list(reader.pages("/v3/reference/tickers"))

    assert len(walked) == 2
    assert len(replay.asked) == 2


def test_a_cursor_that_does_not_end_fails_rather_than_running_forever() -> None:
    reached: list[str] = []

    def endless(url: str, params: Mapping[str, str]) -> Response:
        reached.append(url)
        return served([definition("AAPL")], next_url=f"https://x/page{len(reached)}")

    reader, _ = client(endless)

    with pytest.raises(TransportError, match="does not end"):
        list(reader.pages("/v3/reference/tickers", max_pages=3))


def test_a_failure_half_way_through_a_walk_is_not_the_end_of_the_data() -> None:
    """A timeout mid-universe looks exactly like the end of it, and must not be one."""
    pages = {
        "/v3/reference/tickers": served([definition("AAPL")], next_url="https://x/second"),
        "https://x/second": body({"message": "later"}, 503),
    }
    reader, _ = client(lambda url, params: pages[url.replace(API_HOST, "")])

    with pytest.raises(TransportError):
        list(reader.pages("/v3/reference/tickers"))


# --- the transport the engine provides -----------------------------------------


class FakeResponse:
    """What the engine's client resolves to, in the shape this adapter reads."""

    def __init__(self, status: int, payload: bytes | None, headers: dict[str, str] | None) -> None:
        self.status = status
        self.body = payload
        self.headers = headers


class FakeEngineClient:
    """The engine's client, minus the socket: the coroutine and the arguments are real."""

    def __init__(self, requests_per_second: int, timeout_s: int) -> None:
        self.requests_per_second = requests_per_second
        self.timeout_s = timeout_s
        self.calls: list[dict[str, Any]] = []
        self.answer = FakeResponse(200, b'{"status":"OK","results":[]}', {"x-request-id": "1"})

    async def request(
        self,
        method: Any,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        keys: list[str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {"method": method, "url": url, "params": params, "headers": headers, "keys": keys}
        )
        return self.answer


def test_the_engine_transport_drives_the_coroutine_and_passes_the_request_through() -> None:
    """The coroutine is built inside the loop, which is the one way the engine accepts it."""
    built: list[FakeEngineClient] = []

    def factory(requests_per_second: int, timeout_s: int) -> FakeEngineClient:
        made = FakeEngineClient(requests_per_second, timeout_s)
        built.append(made)
        return made

    transport = pyo3_transport(requests_per_second=7, timeout_s=11, factory=factory)
    answer = transport("GET", "https://api/x", {"a": "b"}, {"Authorization": "Bearer k"}, ["v2"])

    assert (built[0].requests_per_second, built[0].timeout_s) == (7, 11)
    assert built[0].calls[0]["params"] == {"a": "b"}
    assert built[0].calls[0]["keys"] == ["v2"]
    assert str(built[0].calls[0]["method"]) == "HttpMethod.GET"
    assert answer.status == 200


def test_the_engine_transport_survives_an_answer_with_no_body_and_no_headers() -> None:
    def factory(requests_per_second: int, timeout_s: int) -> FakeEngineClient:
        made = FakeEngineClient(requests_per_second, timeout_s)
        made.answer = FakeResponse(204, None, None)
        return made

    transport = pyo3_transport(factory=factory)

    assert transport("GET", "https://api/x", {}, {}, []) == Response(204, b"", {})


def test_one_client_is_built_once_so_the_quota_is_one_quota() -> None:
    """A client per request would be a rate limit per request, which is no rate limit."""
    built: list[FakeEngineClient] = []

    def factory(requests_per_second: int, timeout_s: int) -> FakeEngineClient:
        built.append(FakeEngineClient(requests_per_second, timeout_s))
        return built[-1]

    transport = pyo3_transport(factory=factory)
    transport("GET", "https://api/x", {}, {}, [])
    transport("GET", "https://api/y", {}, {}, [])

    assert len(built) == 1
    assert len(built[0].calls) == 2


def test_the_engine_s_own_client_builds_with_the_quota_and_never_leaves_the_host() -> None:
    """Building the rate-limited client is offline; only a request would not be."""
    assert callable(pyo3_transport(requests_per_second=3, timeout_s=1))


def test_a_method_the_engine_does_not_send_is_refused_before_a_socket_is_opened() -> None:
    with pytest.raises(TransportError, match="not an HTTP method"):
        _method("BREW")


def test_the_engine_names_the_methods_this_adapter_uses() -> None:
    assert str(_method("get")) == "HttpMethod.GET"


def test_an_event_loop_is_not_needed_by_the_caller() -> None:
    """`data load` is synchronous; the transport owns the loop and returns a value."""
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


# --- the configuration table ---------------------------------------------------


def test_the_table_defaults_to_the_vendor_s_host_and_a_quota() -> None:
    settings = MassiveConfig()

    assert settings.base_url == API_HOST
    assert settings.requests_per_second == DEFAULT_REQUESTS_PER_SECOND


def test_a_quota_of_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        MassiveConfig(requests_per_second=0)


def test_the_table_holds_no_credential() -> None:
    """Credentials are variables resolved at use; a workspace file never holds one."""
    assert not [name for name in MassiveConfig.model_fields if "key" in name or "secret" in name]


# --- the adapter declaration ---------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return init(tmp_path / "ws")


def configured(ws: Workspace, **table: object) -> Workspace:
    """The same workspace with an `[adapters.massive]` table."""
    return replace(ws, config=ws.config.model_copy(update={"adapters": {ID: table}}))


def test_the_adapter_is_a_data_adapter_under_its_own_id() -> None:
    assert (ADAPTER.id, ADAPTER.kind) == ("massive", "data")


def test_it_declares_three_variables_under_the_standard_scheme() -> None:
    """Derived from the id rather than spelled out, so the two cannot drift apart."""
    assert CREDENTIALS == (API_KEY, ACCESS_KEY_ID, SECRET_KEY)
    assert API_KEY == "KANSO_MASSIVE_API_KEY"
    assert ACCESS_KEY_ID == "KANSO_MASSIVE_ACCESS_KEY_ID"
    assert SECRET_KEY == "KANSO_MASSIVE_SECRET_KEY"
    assert KIND == "data"


def test_it_declares_what_it_offers_and_never_what_the_plan_grants() -> None:
    """Entitlement and the floor are measurements; a declared constant would go stale."""
    payload = CAPABILITIES.payload()

    assert payload["entitlement"] == "probed per ticker, cached at the grain the source gates on"
    assert payload["history_floor"] == "probed per series, on the day it is used"
    assert "bars" in CAPABILITIES.datasets()
    assert "bulk" in CAPABILITIES.names()


def test_indices_are_the_class_gated_per_ticker() -> None:
    """One index serves bars and another does not, on the same endpoint and range."""
    grains = {item.asset_class: item.grain for item in CAPABILITIES.classes}

    assert grains["indices"] == "ticker"
    assert grains["stocks"] == "endpoint"


def test_a_class_offers_the_datasets_it_can_ask_for_not_the_ones_it_is_entitled_to() -> None:
    """Options ticks are refused under this plan and are still offered: only a probe knows."""
    options = next(item for item in CAPABILITIES.classes if item.asset_class == "options")

    assert set(options.datasets) >= {"bars", "trades", "quotes"}
    assert options.payload()["entitlement"] == "probed"


def test_no_credential_is_needed_to_declare_any_of_this() -> None:
    """The property D14 asserts: importing and describing the adapter reaches nothing."""
    assert ADAPTER.capabilities.payload()["classes"]


def test_a_missing_key_fails_the_step_naming_the_variable(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(Exception) as caught:
        ADAPTER.client(ws)

    assert API_KEY in str(caught.value)
    assert getattr(caught.value, "code", None) == Exit.PRECONDITION


def test_a_resolved_key_builds_a_client_at_the_configured_host(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_KEY, KEY)
    at = configured(ws, base_url="https://proxy.example", requests_per_second=5)

    reader = ADAPTER.client(at, transport=Replay(lambda url, params: nothing()))

    assert reader.base_url == "https://proxy.example"
    assert reader.quota == "5/s"


def test_the_quota_is_reportable_without_opening_a_client(ws: Workspace) -> None:
    """`data adapters` describes an adapter whose credential may not be set at all."""
    assert ADAPTER.quota(ws) == f"{DEFAULT_REQUESTS_PER_SECOND}/s"
    assert ADAPTER.quota(configured(ws, requests_per_second=12)) == "12/s"


def test_the_table_is_validated_by_the_adapter_s_own_model(ws: Workspace) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.config(configured(ws, requests_per_second=0))


def test_the_object_store_keys_are_resolved_independently_of_the_rest_key(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They carry the same value under today's plan; nothing here relies on that."""
    monkeypatch.setenv(API_KEY, KEY)
    monkeypatch.setenv(ACCESS_KEY_ID, "an-id")
    monkeypatch.delenv(SECRET_KEY, raising=False)

    with pytest.raises(Exception, match=SECRET_KEY):
        ADAPTER.object_store_keys(ws)

    monkeypatch.setenv(SECRET_KEY, "a-secret")
    assert ADAPTER.object_store_keys(ws) == ("an-id", "a-secret")


def test_doctor_is_told_where_a_variable_resolved_and_never_what_it_holds(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_KEY, KEY)
    for name in (ACCESS_KEY_ID, SECRET_KEY):
        monkeypatch.delenv(name, raising=False)

    origins = ADAPTER.credential_origins(ws)

    assert origins == {API_KEY: "environment", ACCESS_KEY_ID: None, SECRET_KEY: None}
    assert KEY not in str(origins)
    assert ADAPTER.configured(ws) is True


def test_an_unconfigured_workspace_says_so_without_reaching_anywhere(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)

    assert ADAPTER.configured(ws) is False


@pytest.mark.parametrize(
    ("answer", "ok", "detail"),
    [
        (lambda url, params: served([definition("AAPL")]), True, "authenticates"),
        (lambda url, params: nothing(), False, "knows no such key"),
        (lambda url, params: refused(), False, "refused the key"),
        (lambda url, params: rejected(), False, "rejected the request shape"),
        (lambda url, params: body({}, 429), False, "throttled"),
        (lambda url, params: body({}, 503), False, "did not answer"),
        (lambda url, params: Response(200, b"<html/>"), False, "could not be read"),
    ],
)
def test_one_authenticated_request_says_whether_the_credential_reaches_the_vendor(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, answer: Any, ok: bool, detail: str
) -> None:
    """`data adapters --check` makes exactly this one request and reads only its status."""
    monkeypatch.setenv(API_KEY, KEY)
    replay = Replay(answer)

    checked = ADAPTER.check(ws, transport=replay)

    assert checked.ok is ok
    assert detail in checked.detail
    assert len(replay.asked) == 1
    assert checked.payload()["path"] == "/v3/reference/tickers/AAPL"


def test_transport_and_client_are_the_only_way_a_request_leaves_this_package(
    tmp_path: Path,
) -> None:
    """A `Sequence` of keys and a `Mapping` of params is the whole transport contract."""
    seen: list[tuple[str, Sequence[str]]] = []

    def record(
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
    ) -> Response:
        seen.append((method, keys))
        return nothing()

    MassiveClient(KEY, transport=record).call("/v2/aggs/x")

    assert seen == [("GET", ["v2/aggs", "v2"])]


def test_a_request_this_client_cannot_sign_borrows_the_same_connection() -> None:
    """A signed object-store GET carries its own headers and still counts against one quota."""
    reader, replay = client(lambda url, params: Response(206, b"gzipped-bytes"))

    answer = reader.transport("GET", "https://files/x.csv.gz", {}, {"Authorization": "AWS4"}, [])

    assert answer.status == 206
    assert replay.asked[0].headers == {"Authorization": "AWS4"}
    assert KEY not in str(replay.asked[0].headers)
