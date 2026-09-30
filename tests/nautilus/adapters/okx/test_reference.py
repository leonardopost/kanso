"""The exchange's public reference, driven against what the exchange was recorded answering.

Every body the provider reads here is a recording of the public API, made on 2026-09-30 with
no credential (`recorded.py`, `fixtures/provenance.json`); nothing reaches a network and no
variable is read. Five properties are under test. The adapter is discovered beside the
broker, needs no credential, and is configured by its table alone, so a workspace that never
named the exchange asks it nothing. A linear USDT swap resolves into a `CryptoPerpetual`
whose contract terms are the row's and whose fees are zero, and it validates on a USDT
account. An inverse contract, a suspended one, one not yet listed and one the exchange does
not list are each refused by name, per id. An answer that is not the API's is the call's
failure, not an id's. And the request carries a User-Agent and nothing else, and every
request to the API is held to the table's rate, however many threads send.
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from kanso import hyp
from kanso.config import CONFIG_NAME, load_config
from kanso.data import registry
from kanso.data.instruments import (
    NOT_YET_LISTED,
    UNKNOWN,
    ResolveError,
    engine_fields,
    read_cache,
    read_store,
    resolve_universe,
)
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus.adapters import okx
from kanso.nautilus.adapters.okx import reference
from kanso.nautilus.adapters.okx.reference import (
    ADAPTER,
    INSTRUMENTS,
    USER_AGENT,
    OkxReference,
    PublicClient,
    Response,
    definition,
    pyo3_transport,
)
from kanso.workspace import Workspace, init
from tests.hyp.conftest import DOCUMENT, write_hypothesis

from .recorded import FIXTURES, PROVENANCE, Replay, recorded, row

AS_OF = date(2026, 9, 30)
"""The day the recordings were made."""

US = "https://us.okx.com"
BTC = "BTC-USDT-SWAP.OKX"
ETH = "ETH-USDT-SWAP.OKX"


def reopened(ws: Workspace, text: str) -> Workspace:
    """The workspace with `text` appended to `kanso.toml`, read again as an operator's edit."""
    path = ws.path(CONFIG_NAME)
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
    return Workspace(root=ws.root, config=load_config(path))


def research(ws: Workspace, key: str, value: str) -> Workspace:
    """The workspace with one `[research]` key rewritten."""
    path = ws.path(CONFIG_NAME)
    text, count = re.subn(
        rf"^{key} = .*$", f'{key} = "{value}"', path.read_text(encoding="utf-8"), flags=re.M
    )
    assert count == 1, key
    path.write_text(text, encoding="utf-8")
    return Workspace(root=ws.root, config=load_config(path))


@pytest.fixture
def fresh(tmp_path: Path) -> Workspace:
    """A scaffolded workspace that has never named the exchange."""
    return init(tmp_path / "ws")


@pytest.fixture
def ws(fresh: Workspace) -> Workspace:
    """A workspace naming the exchange's US host and resolving through its reference."""
    return reopened(fresh, '\n[adapters.okx]\nregion = "us"\n\n[data]\nreference = "okx"\n')


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch) -> Replay:
    """The recordings, served wherever the adapter would have built the engine's client."""
    served = Replay()
    monkeypatch.setattr(reference, "pyo3_transport", lambda rate, **_: served)
    return served


def provider(ws: Workspace, replay: Replay) -> OkxReference:
    return ADAPTER.provider(ws, transport=replay)


# --- the adapter ---------------------------------------------------------------


def test_the_adapter_is_discovered_beside_the_broker_and_needs_no_credential(
    fresh: Workspace,
) -> None:
    assert okx.ADAPTER is ADAPTER
    assert registry.brokered() == {"okx": ADAPTER}
    assert registry.adapters()["okx"] is ADAPTER
    assert ADAPTER.kind == "data"
    assert ADAPTER.credentials == ()
    assert ADAPTER.credential_origins(fresh) == {}
    assert set(ADAPTER.loaders(fresh)) == {"okx_bars", "okx_trades", "okx_funding"}
    assert ADAPTER.capabilities.names() == ("reference", "bars", "trades", "funding")
    assert ADAPTER.capabilities.payload()["credential"] == (
        "none: the listing and the history are public"
    )


def test_a_workspace_that_never_named_the_exchange_has_it_unconfigured(
    fresh: Workspace, ws: Workspace
) -> None:
    assert not ADAPTER.configured(fresh)
    assert ADAPTER.configured(ws)


def test_a_table_that_names_no_host_leaves_it_unconfigured(fresh: Workspace) -> None:
    """The broker accepts a table with no region; the reference has nowhere to send a
    request with one, so a probe passes it by rather than stopping on it."""
    unnamed = reopened(fresh, "\n[adapters.okx]\nrate_per_second = 5\n")

    assert not ADAPTER.configured(unnamed)


def test_the_quota_is_the_table_s(fresh: Workspace) -> None:
    assert ADAPTER.quota(fresh) == "5/s"
    assert ADAPTER.quota(reopened(fresh, "\n[adapters.okx]\nrate_per_second = 8\n")) == "8/s"


def test_no_request_is_made_without_a_region_to_send_it_to(fresh: Workspace) -> None:
    """The exchange's reference named with no host stated is refused before a byte is sent."""
    named = reopened(fresh, '\n[data]\nreference = "okx"\n')
    served = Replay()

    with pytest.raises(PreconditionError) as refused:
        ADAPTER.provider(named, transport=served)

    assert "no region is declared" in refused.value.message
    assert served.asked == []


@pytest.mark.parametrize(
    ("region", "host"),
    [("us", US), ("global", "https://www.okx.com"), ("eea", "https://eea.okx.com")],
)
def test_the_host_is_the_one_the_engine_maps_the_table_s_region_to(
    fresh: Workspace, region: str, host: str
) -> None:
    client = ADAPTER.client(reopened(fresh, f'\n[adapters.okx]\nregion = "{region}"\n'))

    assert client.base_url == host


def test_an_id_is_asked_for_alone_on_the_swap_listing(ws: Workspace, replay: Replay) -> None:
    provider(ws, replay).resolve([BTC], AS_OF)

    assert replay.asked == [
        (f"{US}{INSTRUMENTS}", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}),
    ]
    assert PROVENANCE["files"]["swap_BTC-USDT-SWAP.json"]["url"] == (
        f"{US}{INSTRUMENTS}?instType=SWAP&instId=BTC-USDT-SWAP"
    )


# --- resolution ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wanted", "base", "multiplier", "tick", "lot"),
    [(BTC, "BTC", "0.01", "0.1", "0.01"), (ETH, "ETH", "0.1", "0.01", "0.01")],
)
def test_a_linear_usdt_swap_resolves_to_the_contract_the_exchange_lists(
    ws: Workspace, replay: Replay, wanted: str, base: str, multiplier: str, tick: str, lot: str
) -> None:
    found: Any = provider(ws, replay).resolve([wanted], AS_OF)[wanted]

    assert type(found).__name__ == "CryptoPerpetual"
    assert str(found.id) == wanted
    assert str(found.raw_symbol) == wanted.removesuffix(".OKX")
    assert (str(found.base_currency), str(found.quote_currency)) == (base, "USDT")
    assert str(found.settlement_currency) == "USDT"
    assert found.is_inverse is False
    assert Decimal(str(found.multiplier)) == Decimal(multiplier)
    assert Decimal(str(found.price_increment)) == Decimal(tick)
    assert Decimal(str(found.size_increment)) == Decimal(lot)
    assert Decimal(str(found.lot_size)) == Decimal(lot)
    assert Decimal(str(found.min_quantity)) == Decimal(lot)
    assert (found.maker_fee, found.taker_fee) == (0, 0)
    assert found.ts_event == found.ts_init


def test_the_definition_is_dated_the_day_it_was_resolved_as_of(
    ws: Workspace, replay: Replay
) -> None:
    """Pinned, not read off a clock: the same day, whatever hour the suite runs at."""
    found: Any = provider(ws, replay).resolve([BTC], AS_OF)[BTC]

    midnight = datetime(2026, 9, 30, tzinfo=UTC)
    assert engine_fields(found)["ts_init"] == int(midnight.timestamp()) * 1_000_000_000


def test_a_bare_inst_id_resolves_too_and_the_exchange_s_spelling_is_recorded(
    ws: Workspace, replay: Replay
) -> None:
    """A refresh asks for the key an entry records, which is the exchange's own."""
    reference_provider = provider(ws, replay)

    found = reference_provider.resolve(["BTC-USDT-SWAP"], AS_OF)["BTC-USDT-SWAP"]

    assert str(getattr(found, "id", None)) == BTC
    assert reference_provider.sources(BTC) == {"okx": "BTC-USDT-SWAP"}
    assert reference_provider.sources("BTC-USDT-SWAP") == {"okx": "BTC-USDT-SWAP"}
    assert reference_provider.sources(ETH) == {}


def test_an_inverse_contract_is_refused_by_name(ws: Workspace, replay: Replay) -> None:
    """The recorded BTC-USD-SWAP: margined and settled in the coin."""
    found = provider(ws, replay).resolve(["BTC-USD-SWAP.OKX"], AS_OF)["BTC-USD-SWAP.OKX"]

    assert isinstance(found, ResolveError)
    assert found.reason.startswith(
        "BTC-USD-SWAP is an inverse contract (ctType 'inverse', settled in BTC); kanso trades "
        "linear perpetuals"
    )


def test_a_suspended_contract_is_refused_by_name() -> None:
    """The page recorded on 2026-09-30 carried no suspended swap: all 494 were live. So this
    is the recorded BTC-USDT-SWAP row with `state` — the one field the refusal reads —
    changed to the exchange's documented `suspend`, and every other field as served."""
    found = definition({**row("BTC-USDT-SWAP"), "state": "suspend"}, AS_OF)

    assert found == (
        "BTC-USDT-SWAP is suspend on the exchange, not live; a definition is resolved only "
        "for a contract that trades"
    )


def test_a_contract_asked_for_before_it_listed_is_not_yet_listed(
    ws: Workspace, replay: Replay
) -> None:
    """The recorded row lists BTC-USDT-SWAP on 2019-11-12 (listTime 1573557408000)."""
    found = provider(ws, replay).resolve([BTC], date(2019, 11, 11))[BTC]

    assert found == ResolveError(BTC, f"{NOT_YET_LISTED} 2019-11-11: it was listed 2019-11-12")
    listed = provider(ws, replay).resolve([BTC], date(2019, 11, 12))[BTC]
    assert type(listed).__name__ == "CryptoPerpetual"


def test_an_id_the_exchange_does_not_list_is_unknown(ws: Workspace, replay: Replay) -> None:
    """Recorded: HTTP 200, code 51001 and no rows — the id's failure, not the endpoint's."""
    found = provider(ws, replay).resolve(["NOPE-USDT-SWAP.OKX"], AS_OF)

    assert found == {
        "NOPE-USDT-SWAP.OKX": ResolveError(
            "NOPE-USDT-SWAP.OKX", f"{UNKNOWN}: the exchange lists no perpetual swap NOPE-USDT-SWAP"
        )
    }


def test_an_id_the_exchange_rejects_as_malformed_is_that_id_s_failure(
    ws: Workspace, replay: Replay
) -> None:
    """Recorded: HTTP 400, code 51000 for a lower-case id."""
    found = provider(ws, replay).resolve(["btc-usdt-swap"], AS_OF)["btc-usdt-swap"]

    assert isinstance(found, ResolveError)
    assert "rejected 'btc-usdt-swap' as an instrument id" in found.reason


def test_an_id_on_another_venue_is_refused_without_a_request(ws: Workspace, replay: Replay) -> None:
    found = provider(ws, replay).resolve(["BTC-USDT-SWAP.XNAS"], AS_OF)["BTC-USDT-SWAP.XNAS"]

    assert isinstance(found, ResolveError)
    assert "names the venue XNAS" in found.reason
    assert replay.asked == []


def test_an_answer_that_is_not_the_api_s_stops_the_call(ws: Workspace) -> None:
    """Recorded: the exchange's edge answering the standard library's User-Agent, 403 and
    `error code: 1010` — nothing about the id was established, so no id is marked."""
    refusing = Replay(answers={"BTC-USDT-SWAP": "refused_user_agent.txt"})

    with pytest.raises(KansoError) as stopped:
        provider(ws, refusing).resolve([BTC], AS_OF)

    assert stopped.value.code is Exit.ERROR
    assert stopped.value.message == (
        f"okx: {INSTRUMENTS} did not answer as the exchange's API does (HTTP 403: error code: 1010)"
    )


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"ctType": ""}, "is an unstated contract (ctType '', settled in USDT)"),
        ({"state": "", "ctType": "linear"}, "is in no stated state on the exchange"),
        ({"settleCcy": ""}, "unknown: the exchange's row states no settleCcy"),
        ({"uly": "BTC"}, "unknown: the exchange's row names no quote currency in uly 'BTC'"),
        ({"ctVal": "a lot"}, "unknown: ctVal 'a lot' x ctMult '1' is not a number"),
        ({"tickSz": "0.0000000000000000001"}, "unknown: BTC-USDT-SWAP.OKX: CryptoPerpetual"),
    ],
)
def test_a_row_that_states_too_little_is_refused_naming_the_field(
    changed: dict[str, str], reason: str
) -> None:
    """Each is the recorded BTC-USDT-SWAP row with the one field the refusal reads blanked
    or changed; the exchange served none of these, and nothing here claims it did."""
    found = definition({**row("BTC-USDT-SWAP"), **changed}, AS_OF)

    assert isinstance(found, str)
    assert reason in found


@pytest.mark.parametrize("stamp", ["99999999999999999999", "-99999999999999999999", "soon"])
def test_a_listing_time_no_calendar_holds_is_read_as_no_listing_time(stamp: str) -> None:
    """The recorded row with its `listTime` replaced; the exchange served none of these."""
    found: Any = definition({**row("BTC-USDT-SWAP"), "listTime": stamp}, AS_OF)

    assert type(found).__name__ == "CryptoPerpetual"


def test_a_row_with_no_listing_time_or_minimum_still_resolves() -> None:
    held = {
        key: value
        for key, value in row("BTC-USDT-SWAP").items()
        if key not in {"listTime", "minSz"}
    }

    found: Any = definition(held, AS_OF)

    assert type(found).__name__ == "CryptoPerpetual"
    assert found.min_quantity is None


# --- through the core ---------------------------------------------------------


def test_the_workspace_s_reference_resolves_it_into_the_store_and_the_cache(
    ws: Workspace, replay: Replay
) -> None:
    resolved = resolve_universe(ws, [BTC], AS_OF)

    assert type(resolved[BTC]).__name__ == "CryptoPerpetual"
    assert len(read_store(ws)) == 1
    entry = read_cache(ws).root[BTC]
    assert entry.sources == {"okx": "BTC-USDT-SWAP"}
    assert entry.override == {"instrument_class": "swap"}
    assert entry.resolved is not None and entry.resolved.adapter == "okx"


def test_the_operator_s_override_applies_over_what_the_exchange_lists(
    ws: Workspace, replay: Replay
) -> None:
    resolve_universe(ws, [BTC], AS_OF)
    file = read_cache(ws)
    corrected = file.root[BTC].model_copy(
        update={"override": {"instrument_class": "swap", "lot_size": "1", "margin_init": "0.1"}}
    )
    ws.path("instruments.yaml").write_text(
        yaml.safe_dump(
            {BTC: corrected.model_dump(mode="json", exclude_none=True)}, sort_keys=False
        ),
        encoding="utf-8",
    )

    found: Any = resolve_universe(ws, [BTC], AS_OF, refresh=True, record=False)[BTC]

    assert Decimal(str(found.lot_size)) == 1
    assert Decimal(str(found.margin_init)) == Decimal("0.1")
    assert Decimal(str(found.multiplier)) == Decimal("0.01")
    assert replay.asked[-1][1]["instId"] == "BTC-USDT-SWAP"


@pytest.mark.parametrize(("key", "value"), [("currency", "USDT"), ("broker", "okx")])
def test_it_validates_on_a_usdt_account(
    ws: Workspace, replay: Replay, key: str, value: str
) -> None:
    """`[research] currency = "USDT"`, or the broker whose venue declares a USDT account; a
    perpetual's hypothesis requires its funding."""
    account = research(ws, key, value)
    requirements = [*DOCUMENT["data_requirements"], "funding"]
    path = write_hypothesis(
        account, {**DOCUMENT, "universe": [BTC], "data_requirements": requirements}
    )

    assert hyp.validate(account, path).universe == [BTC]


def test_it_is_refused_on_the_template_s_usd_account(ws: Workspace, replay: Replay) -> None:
    path = write_hypothesis(ws, {**DOCUMENT, "universe": [BTC]})

    with pytest.raises(ValidationError) as refused:
        hyp.validate(ws, path)

    assert refused.value.message.startswith(f"universe: {BTC} settles in USDT")


# --- the survey ----------------------------------------------------------------


def test_the_survey_is_one_public_request_for_the_listing(ws: Workspace) -> None:
    served = Replay()

    survey = ADAPTER.survey(ws, transport=served)

    assert served.asked == [(f"{US}{INSTRUMENTS}", {"instType": "SWAP"})]
    assert (survey.reachable, survey.requests) == (True, 1)
    assert [item.payload() for item in survey.reach] == [
        {
            "asset_class": "perpetuals",
            "dataset": "reference",
            "grain": "endpoint",
            "ticker": None,
            "outcome": "ok",
            "detail": "2 live linear swaps of 3 listed",
            "floor": None,
            "probed_on": None,
        }
    ]


def test_a_listing_that_does_not_answer_stops_the_survey_and_names_no_credential(
    ws: Workspace,
) -> None:
    """Recorded: the edge's 403. A survey's `reachable` is a credential's verdict, and this
    adapter sends none, so a host that does not answer is the call's failure instead."""
    with pytest.raises(KansoError) as stopped:
        ADAPTER.survey(ws, transport=Replay(answers={None: "refused_user_agent.txt"}))

    assert stopped.value.code is Exit.ERROR
    assert stopped.value.message == (
        f"okx: {INSTRUMENTS} did not answer as the exchange's API does (HTTP 403: error code: 1010)"
    )
    assert stopped.value.remedy is not None and "credential" not in stopped.value.remedy


def test_doctor_grades_a_host_that_does_not_answer_as_that_check_s_failure(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kanso.cli.doctor import _adapters, _guard

    for name in [name for name in os.environ if name.startswith("KANSO_")]:
        monkeypatch.delenv(name)  # no other configured adapter is asked anything
    refusing = Replay(answers={None: "refused_user_agent.txt"})
    monkeypatch.setattr(reference, "pyo3_transport", lambda rate, **_: refusing)

    check = _guard("adapters", lambda: _adapters(ws, True))

    assert check.status == "fail"
    assert "did not answer" in check.detail and "authenticate" not in check.detail
    assert check.remedy is not None and "rate_per_second" in check.remedy
    assert "credential" not in check.remedy


def test_a_transport_fault_is_the_call_s_failure_with_a_remedy(ws: Workspace) -> None:
    def unreachable(url: str, params: Any) -> Response:
        raise RuntimeError("error sending request for url")

    with pytest.raises(KansoError) as stopped:
        ADAPTER.provider(ws, transport=unreachable).resolve([BTC], AS_OF)

    assert stopped.value.code is Exit.ERROR
    assert stopped.value.message == f"okx: {INSTRUMENTS} could not be reached (RuntimeError)"
    assert stopped.value.remedy is not None and "network" in stopped.value.remedy


# --- the wire -------------------------------------------------------------------


def test_the_engine_client_carries_a_user_agent_and_no_other_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nautilus_trader.core import nautilus_pyo3

    built: dict[str, Any] = {}
    monkeypatch.setattr(nautilus_pyo3, "HttpClient", lambda **kwargs: built.update(kwargs))

    reference._http_client(7)

    assert built["default_headers"] == {"User-Agent": USER_AGENT}
    assert built["header_keys"] == []
    assert [key for key, _ in built["keyed_quotas"]] == [reference.ARCHIVES]
    assert USER_AGENT.startswith("kanso/")


def test_the_engine_client_is_built_offline_with_the_table_s_quota() -> None:
    from nautilus_trader.core import nautilus_pyo3

    assert isinstance(reference._http_client(5), nautilus_pyo3.HttpClient)


def test_the_transport_drives_the_engine_s_coroutine_and_reads_status_and_body() -> None:
    """The plumbing, without a socket: a stand-in client answering as the engine's does."""
    sent: list[tuple[Any, str, dict[str, str]]] = []

    class Answered:
        status = 200
        body = recorded("swap_BTC-USDT-SWAP.json").body

    class Client:
        async def request(
            self, method: Any, url: str, params: dict[str, str], **keyed: Any
        ) -> Answered:
            sent.append((method, url, params))
            quoted.append(keyed)
            return Answered()

    quoted: list[dict[str, Any]] = []

    rates: list[int] = []
    send = pyo3_transport(3, factory=lambda rate: rates.append(rate) or Client())

    answer = PublicClient(US, send).swaps("BTC-USDT-SWAP")

    assert rates == [3]
    assert answer.listed and answer.rows[0]["instId"] == "BTC-USDT-SWAP"
    assert sent[0][1:] == (f"{US}{INSTRUMENTS}", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
    assert str(sent[0][0]).endswith("GET")
    assert quoted[0] == {"keys": [reference.QUOTA_KEY], "timeout_secs": reference.TIMEOUT_S}


def test_every_api_request_names_the_table_s_quota_and_the_listing_its_own_as_well() -> None:
    """The engine holds a request to its default quota only under a key the request names,
    so every request to the API names one key, whatever its path, and all of them share the
    table's rate. The listing throttles hardest, so it names its path's own quota first; a
    file on the exchange's file host is not the API's, and is tens of megabytes, so it names
    no key and is given the longer timeout."""
    quoted: list[dict[str, Any]] = []

    class Answered:
        status = 200
        body = b"{}"

    class Client:
        async def request(self, method: Any, url: str, params: dict[str, str], **keyed: Any) -> Any:
            quoted.append(keyed)
            return Answered()

    send = pyo3_transport(3, factory=lambda rate: Client())
    send(f"{US}{reference.ARCHIVES}", {})
    send(f"{US}/api/v5/market/history-candles", {})
    send("https://static.okx.com/cdn/okex/traderecords/trades/daily/x.zip?v=999", {})

    assert quoted == [
        {"keys": [reference.ARCHIVES, reference.QUOTA_KEY], "timeout_secs": reference.TIMEOUT_S},
        {"keys": [reference.QUOTA_KEY], "timeout_secs": reference.TIMEOUT_S},
        {"keys": None, "timeout_secs": reference.DOWNLOAD_TIMEOUT_S},
    ]
    assert reference.QUOTA_KEY not in reference.KEYED_QUOTAS


def test_the_engine_s_client_holds_requests_from_several_threads_to_the_table_s_rate() -> None:
    """The transport over the engine's own client, sent to from three threads at once. The
    host is no name at all, so the engine fails each request once its quota admits it,
    before anything is resolved or connected: twelve requests at ten a second — ten at
    once, then one each 100 ms — take at least 0.2 s however fast the machine."""
    send = pyo3_transport(10)
    candles = "http://not a host/api/v5/market/history-candles"

    def sent(_: int) -> str:
        try:
            send(candles, {"instId": "BTC-USDT-SWAP"})
        except Exception as exc:  # the engine's parse error, once the quota admitted it
            return type(exc).__name__
        return "answered"  # pragma: no cover - nothing can answer it

    start = time.monotonic()
    with ThreadPoolExecutor(3) as pool:
        outcomes = set(pool.map(sent, range(12)))

    assert time.monotonic() - start >= 0.19
    assert "answered" not in outcomes


def test_a_body_that_is_json_but_not_the_envelope_is_no_answer() -> None:
    answer = PublicClient(US, lambda url, params: Response(502, b"[]")).swaps()

    assert not answer.listed and answer.code is None
    assert answer.said() == "HTTP 502: []"


def test_the_recordings_say_where_they_came_from() -> None:
    """Every fixture is accounted for: its url, its host, its status and when."""
    names = {path.name for path in FIXTURES.iterdir() if path.is_file()} - {"provenance.json"}

    assert names == set(PROVENANCE["files"])
    for name in names:
        entry = PROVENANCE["files"][name]
        assert entry["url"].startswith(f"https://{entry['host']}{INSTRUMENTS}?instType=SWAP")
        assert entry["recorded_at"].startswith("2026-09-30T")
    assert PROVENANCE["authentication"].startswith("none")
