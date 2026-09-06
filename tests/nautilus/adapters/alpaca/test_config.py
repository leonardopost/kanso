"""The declarations, the credentials and the pairing that keeps a key on its own host.

Four properties are under test. The two execution clients declare the funding and the
clock the core makes its refusals from, and the registry finds them without anything in
the core naming a broker. The adapter knows exactly four credential names, all derived
from the standard scheme, and no other spelling of one exists anywhere in the package. A
key is refused against the host it does not belong to before a request is built, in both
directions. And nothing — a repr, a message, a payload — ever carries a value.

Nothing here resolves a real credential or opens a socket. The keys below are not
credentials; only their prefixes matter.
"""

from __future__ import annotations

import re
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kanso import creds
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus import adapters
from kanso.nautilus.adapters.alpaca import (
    BROKER,
    CREDENTIALS,
    DATA_CLIENTS,
    EXEC_CLIENTS,
    ID,
    KIND,
    AlpacaConfig,
    Feed,
)
from kanso.nautilus.adapters.alpaca import config as configuration
from kanso.nautilus.adapters.alpaca import venue as venues
from kanso.nautilus.adapters.alpaca.config import (
    DATA_HOST,
    KEY_HEADER,
    LIVE_CLIENT,
    LIVE_HOST,
    PAPER_CLIENT,
    PAPER_HOST,
    SECRET_HEADER,
    Environment,
    Response,
    account,
    check_key,
    credential_names,
    endpoint,
    pyo3_transport,
    resolve,
)
from kanso.workspace import Workspace, init

from . import LIVE_KEY, PAPER_KEY, SECRET

PACKAGE = Path(configuration.__file__).parent

CLIENT_KEYS = {PAPER_CLIENT: PAPER_KEY, LIVE_CLIENT: LIVE_KEY}


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A fresh workspace with none of this adapter's variables set anywhere."""
    for names in CREDENTIALS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    return init(tmp_path / "ws")


def configured(ws: Workspace, **table: object) -> Workspace:
    """The same workspace with an `[adapters.alpaca]` table."""
    return replace(ws, config=ws.config.model_copy(update={"adapters": {ID: table}}))


def with_credentials(ws: Workspace, client_id: str) -> Workspace:
    """The same workspace whose `.env` holds that client's key and secret."""
    key_name, secret_name = credential_names(client_id)
    path = ws.root / creds.ENV_FILE
    lines = [f"{key_name}={CLIENT_KEYS[client_id]}", f"{secret_name}={SECRET}"]
    path.write_text(path.read_text() + "\n".join(lines) + "\n")
    return ws


# --- what the broker declares --------------------------------------------------


def test_the_adapter_is_an_execution_adapter_under_its_own_id() -> None:
    assert (BROKER.id, BROKER.kind) == ("alpaca", "execution")
    assert (BROKER.id, BROKER.kind) == (ID, KIND)


def test_the_paper_client_declares_broker_paper_capital_on_the_wall_clock() -> None:
    paper = next(spec for spec in EXEC_CLIENTS if spec.id == PAPER_CLIENT)

    assert (paper.id, paper.capital, paper.clock) == ("alpaca_paper", "broker_paper", "wall")


def test_the_live_client_declares_real_capital_on_the_wall_clock() -> None:
    """`capital: real` is what confines this client to the live stage and to an approval."""
    live = next(spec for spec in EXEC_CLIENTS if spec.id == LIVE_CLIENT)

    assert (live.id, live.capital, live.clock) == ("alpaca", "real", "wall")


def test_exactly_one_client_trades_real_capital() -> None:
    """Two would be two ways to reach the money; one is the one the guard is written for."""
    assert [spec.id for spec in EXEC_CLIENTS if spec.capital == "real"] == [LIVE_CLIENT]


def test_neither_client_runs_on_the_replay_clock() -> None:
    """A broker fills against current prices, so replayed history would fill at unrelated ones."""
    assert {spec.clock for spec in EXEC_CLIENTS} == {"wall"}


def test_a_data_client_is_offered_for_each_account() -> None:
    """The feed host serves both, but a key belongs to one, so each id reads its own."""
    assert DATA_CLIENTS == (PAPER_CLIENT, LIVE_CLIENT)


# --- the registry --------------------------------------------------------------


def test_the_registry_finds_the_broker_by_reading_the_directory() -> None:
    assert adapters.packaged()[ID] is BROKER


def test_the_registry_hands_out_both_execution_client_declarations() -> None:
    found = adapters.exec_clients()

    assert sorted(found) == sorted([PAPER_CLIENT, LIVE_CLIENT])
    assert found[LIVE_CLIENT].capital == "real"


def test_a_client_id_resolves_to_the_broker_that_declares_it() -> None:
    assert adapters.broker_of(PAPER_CLIENT) is BROKER
    assert adapters.broker_of("sandbox") is None


def test_a_module_beside_the_adapters_that_declares_nothing_is_passed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared helper living next to the adapters is not a broker and not an error."""
    bare = types.ModuleType(f"{adapters.PACKAGE}.helper")
    monkeypatch.setitem(sys.modules, f"{adapters.PACKAGE}.helper", bare)
    monkeypatch.setattr(
        adapters.pkgutil,
        "iter_modules",
        lambda path: [types.SimpleNamespace(name="helper"), types.SimpleNamespace(name="alpaca")],
    )

    assert sorted(adapters.packaged()) == [ID]


def test_the_venue_declaration_is_reached_through_the_registry() -> None:
    declared = adapters.venue_declaration(ID, "XNAS")

    assert declared is not None
    assert (declared.account, declared.currency) == ("margin", "USD")


def test_an_unknown_broker_declares_nothing_rather_than_failing() -> None:
    """A workspace naming a broker it has no adapter for still resolves its venues."""
    assert adapters.venue_declaration("nobody", "XNAS") is None
    assert adapters.venue_declaration(None, "XNAS") is None


# --- the venues it serves ------------------------------------------------------


def test_the_broker_declares_a_margin_account_in_dollars_charging_no_commission() -> None:
    declared = BROKER.venue_declaration("XNAS")

    assert declared is not None
    assert (declared.account, declared.currency) == ("margin", "USD")
    assert declared.costs is not None
    assert declared.costs.commission_bps == 0.0


def test_slippage_and_the_spread_are_left_to_the_shipped_defaults() -> None:
    """The broker publishes neither, so it declares neither and nothing invents one."""
    declared = venues.US_EQUITY

    assert declared.costs is not None
    assert declared.costs.slippage_bps is None
    assert declared.costs.spread is None


def test_every_venue_the_broker_can_resolve_an_instrument_onto_is_one_it_declares() -> None:
    assert sorted(venues.VENUES) == sorted(set(venues.EXCHANGES.values()))
    assert venues.serves("XNAS")
    assert not venues.serves("XLON")


def test_a_venue_this_broker_does_not_trade_gets_no_declaration() -> None:
    assert BROKER.venue_declaration("XLON") is None


def test_the_measured_exchange_spelling_is_the_listing_venue() -> None:
    """`NASDAQ` is what the live asset row carried; the code kanso keys everything by is XNAS."""
    assert venues.venue_of("NASDAQ") == "XNAS"
    assert venues.venue_of(" nasdaq ") == "XNAS"
    assert venues.venue_of("MOON") is None


# --- the credential names ------------------------------------------------------


def test_each_client_names_its_own_two_variables_under_the_standard_scheme() -> None:
    assert CREDENTIALS[PAPER_CLIENT] == (
        "KANSO_ALPACA_PAPER_API_KEY",
        "KANSO_ALPACA_PAPER_API_SECRET",
    )
    assert CREDENTIALS[LIVE_CLIENT] == ("KANSO_ALPACA_API_KEY", "KANSO_ALPACA_API_SECRET")


def test_the_names_are_derived_from_the_client_id_rather_than_spelled_out() -> None:
    """An id and the variables an operator sets cannot drift apart if one derives the other."""
    for client_id, names in CREDENTIALS.items():
        assert names == (
            creds.standard_name(client_id),
            creds.standard_name(client_id, "API_SECRET"),
        )


def test_the_two_accounts_share_no_variable() -> None:
    """A paper key must not be able to reach the real account by any name."""
    assert not set(CREDENTIALS[PAPER_CLIENT]) & set(CREDENTIALS[LIVE_CLIENT])


def test_the_adapter_refuses_a_client_id_it_does_not_provide() -> None:
    with pytest.raises(PreconditionError) as failure:
        BROKER.credentials("alpaca_demo")

    assert "alpaca_paper" in str(failure.value)
    assert failure.value.code is Exit.PRECONDITION


def test_the_package_knows_no_other_spelling_of_a_credential_variable() -> None:
    """The one rule that keeps a key meant for another tool out of this one.

    A fallback to whatever a machine happens to export is how a key intended for one
    program ends up trading through another, so the package is scanned: the only
    environment names it may contain are the four the scheme derives, none of the broker's
    own environment spellings appears at all, and no module reads the environment itself —
    every value comes through `kanso.creds`.
    """
    allowed = {name for names in CREDENTIALS.values() for name in names}
    forbidden = (
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "APCA_API_BASE_URL",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_KEY",
    )
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text()
        assert "os.environ" not in text and "getenv" not in text
        for spelling in forbidden:
            loose = re.search(rf"(?<![A-Z0-9_]){re.escape(spelling)}", text)
            assert loose is None, f"{path.name} names {spelling}"
        for found in re.findall(r"\bKANSO_[A-Z0-9_]+\b", text):
            assert found in allowed, f"{path.name} names {found}"


# --- which host a key may address ----------------------------------------------


def test_the_hosts_are_the_measured_ones() -> None:
    settings = AlpacaConfig()

    assert settings.host(PAPER_CLIENT) == PAPER_HOST == "https://paper-api.alpaca.markets"
    assert settings.host(LIVE_CLIENT) == LIVE_HOST == "https://api.alpaca.markets"
    assert settings.data_url == DATA_HOST == "https://data.alpaca.markets"


def test_a_paper_key_is_refused_against_the_live_host() -> None:
    """Measured: the live host answers a paper key with 401. It is refused before it is sent."""
    with pytest.raises(PreconditionError) as failure:
        check_key(PAPER_KEY, account(LIVE_CLIENT), "KANSO_ALPACA_API_KEY")

    assert "401" in str(failure.value)
    assert PAPER_KEY not in str(failure.value)
    assert PAPER_KEY not in str(failure.value.remedy)


def test_a_key_that_is_not_a_paper_key_is_refused_against_the_paper_host() -> None:
    """The reverse: the paper account accepts its own key and no other."""
    with pytest.raises(PreconditionError) as failure:
        check_key(LIVE_KEY, account(PAPER_CLIENT), "KANSO_ALPACA_PAPER_API_KEY")

    assert "paper" in str(failure.value)
    assert LIVE_KEY not in str(failure.value)


def test_each_key_is_accepted_by_its_own_account() -> None:
    check_key(PAPER_KEY, account(PAPER_CLIENT), "KANSO_ALPACA_PAPER_API_KEY")
    check_key(LIVE_KEY, account(LIVE_CLIENT), "KANSO_ALPACA_API_KEY")


def test_a_blank_key_is_not_a_key() -> None:
    with pytest.raises(PreconditionError) as failure:
        check_key("   ", account(PAPER_CLIENT), "KANSO_ALPACA_PAPER_API_KEY")

    assert "blank" in str(failure.value)


def test_an_unknown_client_id_names_the_ones_there_are() -> None:
    with pytest.raises(PreconditionError) as failure:
        account("alpaca_live")

    assert "alpaca_paper" in str(failure.value) and "alpaca" in str(failure.value)


def test_the_environments_are_two_and_each_client_belongs_to_one() -> None:
    assert account(PAPER_CLIENT).environment is Environment.PAPER
    assert account(LIVE_CLIENT).environment is Environment.LIVE


# --- resolving credentials -----------------------------------------------------


def test_a_client_resolves_its_own_two_variables_from_the_workspace_env(ws: Workspace) -> None:
    held = resolve(with_credentials(ws, PAPER_CLIENT).root, PAPER_CLIENT)

    assert held.headers() == {KEY_HEADER: PAPER_KEY, SECRET_HEADER: SECRET}
    assert held.account.client_id == PAPER_CLIENT


def test_the_two_headers_are_the_measured_ones() -> None:
    assert (KEY_HEADER, SECRET_HEADER) == ("APCA-API-KEY-ID", "APCA-API-SECRET-KEY")


def test_a_missing_variable_names_itself_and_both_places_searched(ws: Workspace) -> None:
    with pytest.raises(PreconditionError) as failure:
        resolve(ws.root, LIVE_CLIENT)

    assert "KANSO_ALPACA_API_KEY" in str(failure.value)
    assert failure.value.code is Exit.PRECONDITION


def test_a_paper_key_set_in_the_live_variable_is_refused_at_resolution(ws: Workspace) -> None:
    """The whole pairing rule, end to end: the wrong key in the right variable stops here."""
    path = ws.root / creds.ENV_FILE
    path.write_text(f"KANSO_ALPACA_API_KEY={PAPER_KEY}\nKANSO_ALPACA_API_SECRET={SECRET}\n")

    with pytest.raises(PreconditionError) as failure:
        resolve(ws.root, LIVE_CLIENT)

    assert "401" in str(failure.value)


def test_a_blank_secret_is_refused(ws: Workspace) -> None:
    path = ws.root / creds.ENV_FILE
    path.write_text(f"KANSO_ALPACA_PAPER_API_KEY={PAPER_KEY}\nKANSO_ALPACA_PAPER_API_SECRET='  '\n")

    with pytest.raises(PreconditionError) as failure:
        resolve(ws.root, PAPER_CLIENT)

    assert "KANSO_ALPACA_PAPER_API_SECRET" in str(failure.value)


def test_a_repr_carries_neither_the_key_nor_the_secret(ws: Workspace) -> None:
    """A repr reaches tracebacks, logs and crash reports, which reach issue trackers."""
    held = resolve(with_credentials(ws, LIVE_CLIENT).root, LIVE_CLIENT)

    assert LIVE_KEY not in repr(held) and SECRET not in repr(held)
    assert LIVE_KEY not in str(held) and SECRET not in str(held)
    assert "alpaca" in repr(held)


def test_the_broker_opens_a_client_through_the_workspace(ws: Workspace) -> None:
    held = BROKER.open(with_credentials(ws, PAPER_CLIENT), PAPER_CLIENT)

    assert held.account.environment is Environment.PAPER


# --- what a workspace with no credentials still does ---------------------------


def test_nothing_is_configured_in_a_fresh_workspace(ws: Workspace) -> None:
    """The whole suite, `doctor` and the demo are green with every variable unset."""
    assert not BROKER.configured(ws, PAPER_CLIENT)
    assert not BROKER.configured(ws, LIVE_CLIENT)
    assert BROKER.credential_origins(ws, LIVE_CLIENT) == {
        "KANSO_ALPACA_API_KEY": None,
        "KANSO_ALPACA_API_SECRET": None,
    }


def test_an_account_needs_both_halves_to_count_as_configured(ws: Workspace) -> None:
    path = ws.root / creds.ENV_FILE
    path.write_text(f"KANSO_ALPACA_PAPER_API_KEY={PAPER_KEY}\n")

    assert not BROKER.configured(ws, PAPER_CLIENT)


def test_credentials_that_resolve_are_reported_by_origin_and_never_by_value(
    ws: Workspace,
) -> None:
    origins = BROKER.credential_origins(with_credentials(ws, PAPER_CLIENT), PAPER_CLIENT)

    assert set(origins.values()) == {creds.FROM_ENV_FILE}
    assert PAPER_KEY not in str(origins) and SECRET not in str(origins)


def test_the_description_carries_no_credential_and_says_what_trades_what(ws: Workspace) -> None:
    payload = BROKER.describe(with_credentials(ws, PAPER_CLIENT))

    assert payload["adapter"] == "alpaca"
    assert payload["venues"] == sorted(venues.VENUES)
    assert {"id": PAPER_CLIENT, "capital": "broker_paper", "clock": "wall"}.items() <= dict(
        payload["exec_clients"][0]  # type: ignore[index]
    ).items()
    assert PAPER_KEY not in str(payload) and SECRET not in str(payload)


# --- the table an operator writes ----------------------------------------------


def test_the_feed_has_no_default_and_is_refused_rather_than_guessed(ws: Workspace) -> None:
    """The consolidated tape and one venue's slice are different series for the same day."""
    with pytest.raises(PreconditionError) as failure:
        BROKER.config(ws).require_feed()

    assert "sip" in str(failure.value) and "iex" in str(failure.value)
    assert failure.value.code is Exit.PRECONDITION


def test_a_declared_feed_is_what_the_data_client_opens(ws: Workspace) -> None:
    settings = BROKER.config(configured(ws, feed="sip"))

    assert settings.require_feed() is Feed.SIP
    assert settings.data_stream(Feed.SIP) == "wss://stream.data.alpaca.markets/v2/sip"


def test_a_feed_the_broker_does_not_serve_is_a_validation_failure(ws: Workspace) -> None:
    with pytest.raises(ValidationError):
        BROKER.config(configured(ws, feed="darkpool"))


def test_each_client_streams_its_own_account(ws: Workspace) -> None:
    settings = BROKER.config(ws)

    assert settings.stream(PAPER_CLIENT).startswith("wss://paper-api.")
    assert settings.stream(LIVE_CLIENT).startswith("wss://api.")


def test_the_quota_is_reported_as_the_rate_it_enforces(ws: Workspace) -> None:
    assert BROKER.quota(configured(ws, requests_per_minute=17)) == "17/min"


def test_no_credential_may_be_written_into_the_table(ws: Workspace) -> None:
    """Configuration names variables and never holds values, so an unknown key is refused."""
    with pytest.raises(ValidationError):
        BROKER.config(configured(ws, api_key="nope"))


def test_a_url_is_joined_with_exactly_one_separator() -> None:
    assert endpoint("https://host/", "/v2/clock") == "https://host/v2/clock"
    assert endpoint("https://host", "v2/clock") == "https://host/v2/clock"


# --- the connection ------------------------------------------------------------


class FakeResponse:
    """What the engine's client resolves to, in the shape this adapter reads."""

    def __init__(self, status: int, payload: bytes | None, headers: dict[str, str] | None) -> None:
        self.status = status
        self.body = payload
        self.headers = headers


class FakeEngineClient:
    """The engine's client, minus the socket: the coroutine and the arguments are real."""

    def __init__(self, requests_per_minute: int, timeout_s: int) -> None:
        self.requests_per_minute = requests_per_minute
        self.timeout_s = timeout_s
        self.calls: list[dict[str, Any]] = []
        self.answer = FakeResponse(200, b"{}", {"x-request-id": "1"})

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
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "keys": keys,
                "body": body,
            }
        )
        return self.answer


def test_the_transport_drives_the_coroutine_and_passes_the_request_through() -> None:
    """The coroutine is built inside the loop, which is the one way the engine accepts it."""
    built: list[FakeEngineClient] = []

    def factory(requests_per_minute: int, timeout_s: int) -> FakeEngineClient:
        built.append(FakeEngineClient(requests_per_minute, timeout_s))
        return built[-1]

    transport = pyo3_transport(requests_per_minute=7, timeout_s=11, factory=factory)
    answer = transport("GET", "https://api/v2/clock", {"a": "b"}, {KEY_HEADER: "k"}, ["v2"])

    assert (built[0].requests_per_minute, built[0].timeout_s) == (7, 11)
    assert built[0].calls[0]["params"] == {"a": "b"}
    assert str(built[0].calls[0]["method"]) == "HttpMethod.GET"
    assert answer.status == 200


def test_a_request_body_reaches_the_wire_and_a_read_sends_none() -> None:
    """An order's fields travel in the body; a body dropped here is an order with no symbol."""
    built: list[FakeEngineClient] = []

    def factory(requests_per_minute: int, timeout_s: int) -> FakeEngineClient:
        built.append(FakeEngineClient(requests_per_minute, timeout_s))
        return built[-1]

    transport = pyo3_transport(factory=factory)
    transport("GET", "https://api/v2/clock", {}, {}, [])
    transport("POST", "https://api/v2/orders", {}, {}, [], b'{"symbol":"AAPL"}')

    assert [call["body"] for call in built[0].calls] == [None, b'{"symbol":"AAPL"}']


def test_the_transport_survives_an_answer_with_no_body_and_no_headers() -> None:
    def factory(requests_per_minute: int, timeout_s: int) -> FakeEngineClient:
        made = FakeEngineClient(requests_per_minute, timeout_s)
        made.answer = FakeResponse(204, None, None)
        return made

    transport = pyo3_transport(factory=factory)

    assert transport("GET", "https://api/x", {}, {}, []) == Response(204, b"", {})


def test_one_connection_is_built_once_so_the_quota_is_one_quota(ws: Workspace) -> None:
    """A client per request would be a rate limit per request, which is no rate limit."""
    built: list[FakeEngineClient] = []

    def factory(requests_per_minute: int, timeout_s: int) -> FakeEngineClient:
        built.append(FakeEngineClient(requests_per_minute, timeout_s))
        return built[-1]

    transport = BROKER.transport(configured(ws, requests_per_minute=42), factory=factory)
    transport("GET", "https://api/x", {}, {}, [])
    transport("GET", "https://api/y", {}, {}, [])

    assert len(built) == 1
    assert built[0].requests_per_minute == 42


def test_a_method_the_engine_does_not_send_is_a_failure_rather_than_a_guess() -> None:
    with pytest.raises(KansoError) as failure:
        configuration._method("teleport")

    assert failure.value.code is Exit.ERROR


def test_the_engine_client_is_built_with_a_per_minute_quota() -> None:
    """The broker publishes its limit per minute, so that is the grain the quota is set at."""
    built = configuration._http_client(60, 5)

    assert hasattr(built, "request")


# --- the modules the engine-facing slices provide ------------------------------


def test_an_engine_module_is_imported_when_it_is_asked_for() -> None:
    """By name, so listing what a workspace can deploy to builds no client at all."""
    from kanso.nautilus.adapters import alpaca

    assert alpaca._engine_module("config") is configuration


@pytest.mark.parametrize(
    ("accessor", "module", "attribute"),
    [
        ("exec_client_factory", "factory", "EXEC_CLIENT_FACTORY"),
        ("data_client_factory", "factory", "DATA_CLIENT_FACTORY"),
    ],
)
def test_a_client_factory_is_read_off_the_module_that_provides_it(
    monkeypatch: pytest.MonkeyPatch, accessor: str, module: str, attribute: str
) -> None:
    """The names the engine-facing modules must expose, pinned here so they cannot drift."""
    stub = types.ModuleType(f"kanso.nautilus.adapters.alpaca.{module}")
    setattr(stub, attribute, object())
    monkeypatch.setitem(sys.modules, stub.__name__, stub)

    assert getattr(BROKER, accessor)() is getattr(stub, attribute)


def test_the_provider_and_the_overlay_are_opened_for_one_workspace(
    monkeypatch: pytest.MonkeyPatch, ws: Workspace
) -> None:
    """And the overlay for one account: a permission belongs to the key that was asked."""
    provider_stub = types.ModuleType("kanso.nautilus.adapters.alpaca.provider")
    provider_stub.provider = lambda workspace: ("built", workspace)  # type: ignore[attr-defined]
    overlay_stub = types.ModuleType("kanso.nautilus.adapters.alpaca.tradability")
    overlay_stub.overlay = lambda workspace, *, client_id: (  # type: ignore[attr-defined]
        "built",
        workspace,
        client_id,
    )
    for stub in (provider_stub, overlay_stub):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)

    assert BROKER.provider(ws) == ("built", ws)
    assert BROKER.tradability(ws) == ("built", ws, None)
    assert BROKER.tradability(ws, LIVE_CLIENT) == ("built", ws, LIVE_CLIENT)
