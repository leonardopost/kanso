"""The real account's own terms, read with its key and set against what the workspace charges.

Every answer the check reads here is a recording of the account's own, made with the
operator's read-only key on 2026-09-30 (spot mode) and 2026-10-01 (futures mode)
(`recorded.py`, `fixtures/account/provenance.json`); where a test serves an answer the
exchange was never recorded giving, it builds it in the test and says so. The credentials
are this file's own placeholders, written to the workspace's `.env`; nothing reaches a
network. Five properties are under test. With any of the three names unset, or no region,
nothing is sent and the declared table is still reported. The two reads are signed as the
exchange documents, the key and passphrase travel in headers only, and the secret travels
nowhere. The account's USDT-margined rates are compared with the declaration and any
`venues.OKX.costs` over it, field by field: equal is ok, an account paying more fails, one
paying less warns, and both print the lines to state. A spot-mode account is never called
tradable. And an answer that is not the API's success fails, naming it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kanso.errors import KansoError
from kanso.nautilus.adapters import AccountCheck
from kanso.nautilus.adapters.okx import BROKER, account
from kanso.nautilus.adapters.okx.account import CONFIG, TRADE_FEE, check, signed, stamp
from kanso.nautilus.adapters.okx.reference import QUOTA_KEY, TIMEOUT_S, Response
from kanso.schemas import CostsOverride, VenueOverride
from kanso.workspace import Workspace, init

from .recorded import SPOT_MODE, Account, account_answer
from .test_reference import reopened

KEY = "placeholder-key-0001"
SECRET = "placeholder-secret-0002"
PASSPHRASE = "placeholder-passphrase-0003"
"""This file's own placeholders: no credential of anyone's."""

NAMES = ("KANSO_OKX_API_KEY", "KANSO_OKX_API_SECRET", "KANSO_OKX_PASSPHRASE")

MOMENT = datetime(2026, 10, 1, 10, 25, 10, 914_000, tzinfo=UTC)
"""The instant the futures-mode fee row was stamped by the exchange, pinned."""

US = "https://us.okx.com"

LINES = (
    "  venues:",
    "    OKX:",
    "      costs:",
)


@pytest.fixture(autouse=True)
def unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `KANSO_OKX_*` of the developer's shell reaches a test."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A workspace on the exchange's US host, the real account's three names in its `.env`."""
    workspace = reopened(init(tmp_path / "ws"), '\n[adapters.okx]\nregion = "us"\n')
    keyed(workspace, KEY, SECRET, PASSPHRASE)
    return workspace


def keyed(ws: Workspace, *values: str) -> None:
    """Write the first `len(values)` of the three names to the workspace's `.env`."""
    lines = [f"{name}={value}" for name, value in zip(NAMES, values, strict=False)]
    ws.path(".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def costs(**rates: float) -> VenueOverride:
    """A `venues.OKX` entry stating these rates."""
    return VenueOverride(costs=CostsOverride(**rates))


def read(ws: Workspace, served: Account, override: VenueOverride | None = None) -> AccountCheck:
    return check(ws, override, transport=served, now=lambda: MOMENT)


def refuse(url: str, headers: Any) -> Response:
    raise AssertionError(f"a request was sent: {url}")


def tier(taker: float, maker: float, where: str = "takerU, makerU") -> str:
    """The line naming the rates the account's fee tier reads, and the fields read."""
    return (
        f"okx: the account's fee tier reads taker {taker:g} bp, maker {maker:g} bp on "
        f"USDT-margined swaps ({where})"
    )


def wire(served: Account) -> str:
    """Everything the transport was handed, as one string to search."""
    return json.dumps(served.sent)


# --- the signature -------------------------------------------------------------------


def test_the_instant_is_signed_in_utc_to_the_millisecond() -> None:
    assert stamp(MOMENT) == "2026-10-01T10:25:10.914Z"
    eastern = datetime.fromisoformat("2026-10-01T20:25:10.914999+10:00")
    assert stamp(eastern) == "2026-10-01T10:25:10.914Z"


def test_the_signature_is_the_hmac_of_the_instant_the_method_and_the_path_with_its_query() -> None:
    """Recomputed here from the exchange's documented rule, not from the module."""
    headers = signed(KEY, SECRET, PASSPHRASE, TRADE_FEE, MOMENT)

    message = f"2026-10-01T10:25:10.914ZGET{TRADE_FEE}".encode()
    expected = base64.b64encode(hmac.new(SECRET.encode(), message, hashlib.sha256).digest())
    assert headers == {
        "OK-ACCESS-KEY": KEY,
        "OK-ACCESS-SIGN": expected.decode(),
        "OK-ACCESS-TIMESTAMP": "2026-10-01T10:25:10.914Z",
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }
    assert TRADE_FEE == "/api/v5/account/trade-fee?instType=SWAP"
    assert CONFIG == "/api/v5/account/config"


def test_the_signed_transport_hands_the_engine_the_url_whole_the_headers_and_the_quota() -> None:
    """The plumbing, without a socket: a stand-in client answering as the engine's does.
    The query rides in the url and no `params` are passed, so the path the signature covers
    is the one the engine sends — measured on a loopback server for `nautilus_trader 1.231.0`."""
    sent: list[tuple[Any, str, dict[str, Any]]] = []
    rates: list[int] = []

    class Answered:
        status = 200
        body = account_answer("config_2026-10-01.json").body

    class Client:
        async def request(self, method: Any, url: str, **keyed: Any) -> Answered:
            sent.append((method, url, keyed))
            return Answered()

    send = account.signed_transport(4, factory=lambda rate: rates.append(rate) or Client())
    answer = send(f"{US}{CONFIG}", {"OK-ACCESS-KEY": KEY})

    assert rates == [4]
    assert answer == account_answer("config_2026-10-01.json")
    [(method, url, keyed)] = sent
    assert str(method).endswith("GET") and url == f"{US}{CONFIG}"
    assert keyed == {
        "headers": {"OK-ACCESS-KEY": KEY},
        "keys": [QUOTA_KEY],
        "timeout_secs": TIMEOUT_S,
    }


# --- nothing is sent without all three names and a host ----------------------------


def test_with_no_key_the_declared_table_is_reported_and_nothing_is_sent(tmp_path: Path) -> None:
    ws = reopened(init(tmp_path / "ws"), '\n[adapters.okx]\nregion = "us"\n')

    found = check(ws, None, transport=refuse)

    assert (found.broker, found.status, found.requests) == ("okx", "ok", 0)
    assert found.items[0] == (
        "okx: declares taker 5 bp and maker 2 bp on OKX, the exchange's global Regular tier; "
        "an entity's own Regular tier can differ"
    )
    assert found.items[1] == "okx: portfolio.yaml states no venues.OKX.costs rate"
    assert found.items[2] == "okx: the declaration and portfolio.yaml charge taker 5 bp, maker 2 bp"
    assert found.items[3] == (
        "okx: the account's own tier was not read and no request was made: "
        "KANSO_OKX_API_KEY=unset, KANSO_OKX_API_SECRET=unset, KANSO_OKX_PASSPHRASE=unset"
    )


def test_one_name_unset_is_no_key_and_says_which(ws: Workspace) -> None:
    keyed(ws, KEY, SECRET)

    found = check(ws, costs(commission_bps=7.0), transport=refuse)

    assert (found.status, found.requests) == ("ok", 0)
    assert "okx: portfolio.yaml venues.OKX.costs states commission_bps 7" in found.items
    assert "okx: the declaration and portfolio.yaml charge taker 7 bp, maker 2 bp" in found.items
    assert found.items[-1].endswith(
        "KANSO_OKX_API_KEY=.env, KANSO_OKX_API_SECRET=.env, KANSO_OKX_PASSPHRASE=unset"
    )


def test_keys_with_no_region_warn_and_nothing_is_sent(tmp_path: Path) -> None:
    """A key is accepted by its own entity's host only, so with none stated there is nowhere
    to send it; the engine's default host would answer that the key does not exist."""
    ws = reopened(init(tmp_path / "ws"), "\n[adapters.okx]\nrate_per_second = 5\n")
    keyed(ws, KEY, SECRET, PASSPHRASE)

    found = check(ws, None, transport=refuse)

    assert (found.status, found.requests) == ("warn", 0)
    assert found.detail == "okx's account was not read: [adapters.okx] states no region"
    assert 'region = "us"' in str(found.remedy)
    assert KEY not in json.dumps(found.items) + str(found.remedy)


# --- the measured account against the declaration -----------------------------------


def test_the_futures_mode_account_pays_more_than_the_declaration_and_fails(
    ws: Workspace,
) -> None:
    """Measured: the Australian entity's Regular tier is 5 and 7, the declaration 2 and 5."""
    served = Account()

    found = read(ws, served)

    assert (found.status, found.requests) == ("fail", 2)
    assert found.detail == (
        "okx's account pays taker 7 bp and maker 5 bp; the declaration and portfolio.yaml "
        "charge 5 and 2"
    )
    items = list(found.items)
    assert "okx: account fee level Lv1 · acctLv 2, futures mode · posMode net_mode" in items
    assert tier(7, 5) in items
    start = items.index(
        "okx: state this in portfolio.yaml, merged into any venues.OKX entry already there:"
    )
    assert tuple(items[start + 1 :]) == (
        *LINES,
        "        commission_bps: 7",
        "        maker_bps: 5",
    )
    assert "venues.OKX.costs" in str(found.remedy)
    assert "a hypothesis that states its own" in str(found.remedy)


def test_the_two_reads_are_signed_and_carry_the_key_in_headers_only(ws: Workspace) -> None:
    served = Account()

    read(ws, served)

    assert [url for url, _ in served.sent] == [f"{US}{TRADE_FEE}", f"{US}{CONFIG}"]
    for url, headers in served.sent:
        path = url.removeprefix(US)
        assert headers == signed(KEY, SECRET, PASSPHRASE, path, MOMENT)
        assert KEY not in url and PASSPHRASE not in url
    assert SECRET not in wire(served)


def test_no_credential_is_in_anything_the_check_reports(ws: Workspace) -> None:
    found = read(ws, Account())

    told = json.dumps([found.detail, found.items, found.remedy])
    assert KEY not in told and SECRET not in told and PASSPHRASE not in told


def test_an_override_stating_the_account_s_rates_is_ok_and_offers_no_lines(
    ws: Workspace,
) -> None:
    found = read(ws, Account(), costs(commission_bps=7.0, maker_bps=5.0))

    assert (found.status, found.requests, found.remedy) == ("ok", 2, None)
    assert found.detail == "okx's account pays what the declaration and portfolio.yaml charge"
    assert "okx: portfolio.yaml venues.OKX.costs states commission_bps 7, maker_bps 5" in (
        found.items
    )
    assert not any("state this" in item for item in found.items)


def test_one_side_still_charged_below_the_account_fails(ws: Workspace) -> None:
    """The override merges over the declaration field by field, as a venue model does: a
    taker rate alone leaves the declared maker rate charged."""
    found = read(ws, Account(), costs(commission_bps=7.0))

    assert found.status == "fail"
    assert found.detail.endswith("charge 7 and 2")
    assert found.items[-2:] == ("        commission_bps: 7", "        maker_bps: 5")


def test_charging_more_than_the_account_pays_warns_and_still_offers_the_lines(
    ws: Workspace,
) -> None:
    """Conservative, and perhaps a stress the operator meant, so a warning and not a
    failure — but the rates the account pays are printed all the same."""
    found = read(ws, Account(), costs(commission_bps=8.0, maker_bps=6.5))

    assert found.status == "warn"
    assert found.detail == (
        "okx's account pays taker 7 bp and maker 5 bp, less than the declaration and "
        "portfolio.yaml charge (8 and 6.5)"
    )
    assert found.items[-2:] == ("        commission_bps: 7", "        maker_bps: 5")


def test_a_spot_mode_account_is_never_called_tradable(ws: Workspace) -> None:
    """Measured the day before: in spot mode the account answered the global figures for
    swaps, and switched to futures mode it answered its own entity's — so no line is
    offered from a spot-mode answer, whatever it says."""
    found = read(ws, Account(day=SPOT_MODE))

    assert (found.status, found.requests) == ("fail", 2)
    assert found.detail == "okx's account is in spot mode (acctLv 1) and trades no perpetual"
    assert "okx: account fee level Lv1 · acctLv 1, spot mode · posMode net_mode" in found.items
    assert tier(5, 2) in found.items
    assert not any("state this" in item for item in found.items)
    assert "futures mode" in str(found.remedy)


def test_empty_usdt_rates_fall_back_to_the_maker_and_taker_fields(ws: Workspace) -> None:
    """Derived from the futures-mode recording with `makerU` and `takerU` emptied, as the
    exchange serves them for a SPOT row: not an answer it was recorded giving for swaps."""
    body = json.loads(account_answer("trade-fee_instType-SWAP_2026-10-01.json").body)
    body["data"][0].update(makerU="", takerU="")
    served = Account(answers={TRADE_FEE: Response(200, json.dumps(body).encode())})

    found = read(ws, served)

    assert tier(7, 5, "taker, maker") in found.items


def test_a_rebate_reads_as_a_negative_maker_rate(ws: Workspace) -> None:
    """Built here: the exchange signs a rebate positive, and kanso's `maker_bps` negative."""
    body = json.loads(account_answer("trade-fee_instType-SWAP_2026-10-01.json").body)
    body["data"][0].update(makerU="0.00005")
    served = Account(answers={TRADE_FEE: Response(200, json.dumps(body).encode())})

    found = read(ws, served)

    assert found.items[-1] == "        maker_bps: -0.5"


def test_an_answer_with_no_readable_rate_fails_naming_the_fields(ws: Workspace) -> None:
    """Built here: a success carrying no row."""
    empty = Response(200, b'{"code": "0", "data": [], "msg": ""}')

    found = read(ws, Account(answers={TRADE_FEE: empty}))

    assert (found.status, found.requests) == ("fail", 2)
    assert found.detail == (
        f"okx's account answered {TRADE_FEE} with no readable rate in takerU and makerU, or "
        "taker and maker"
    )


def test_an_answer_that_is_not_the_api_s_success_fails_naming_it(ws: Workspace) -> None:
    """Built here, not recorded: the exchange's message for a key another host does not
    know was measured as "API key doesn't exist"; its status and code were not."""
    refused = Response(401, b'{"code": "50119", "msg": "API key doesn\'t exist", "data": []}')

    found = read(ws, Account(answers={CONFIG: refused}))

    assert (found.status, found.requests) == ("fail", 2)
    assert found.detail == (
        f"okx's account did not answer {CONFIG} as the exchange's API does "
        "(HTTP 401, code 50119: API key doesn't exist)"
    )
    assert all(name in str(found.remedy) for name in NAMES)
    assert "region" in str(found.remedy)


def test_a_fault_below_any_answer_stops_with_a_network_remedy(ws: Workspace) -> None:
    def unreachable(url: str, headers: Any) -> Response:
        raise OSError("connection refused")

    with pytest.raises(KansoError) as raised:
        check(ws, None, transport=unreachable, now=lambda: MOMENT)

    assert raised.value.message == f"okx: {TRADE_FEE} could not be reached (OSError)"
    assert "network" in str(raised.value.remedy)
    assert KEY not in raised.value.message


def test_the_broker_reads_its_own_venue_s_entry(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry's entry point hands the check the `OKX` entry and no other."""
    served = Account()
    monkeypatch.setattr(account, "signed_transport", lambda rate, **_: served)

    found = BROKER.check_account(ws, {"XNAS": costs(commission_bps=1.0), "OKX": costs(maker_bps=5)})

    assert found is not None and found.detail.endswith("charge 5 and 5")
    assert len(served.sent) == 2
