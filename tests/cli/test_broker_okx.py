"""The OKX broker from the shell: listed, refused at deploy, read by `doctor`, and resolved.

`portfolio clients` lists both accounts with the variables each would read and the refusal a
stage naming one would meet, and `doctor` reads `[adapters.okx]` through the package's own
model and reports the clients unset. The package's public reference resolves a listed swap
through `kanso data instruments resolve` and is surveyed by `--check`, and its public-history
loaders fill the catalog through `kanso data load`, answered here from the recordings in
`tests/nautilus/adapters/okx/fixtures/`. No variable is set and nothing reaches a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.nautilus.adapters.okx import reference

from ..nautilus.adapters.okx.recorded import History, Replay, recorded_for
from .conftest import at, payload, reconfigure

DEMO = "okx_demo"
LIVE = "okx"


def with_table(root: Path, body: str) -> Path:
    """The workspace's `kanso.toml` with an `[adapters.okx]` table appended."""
    path = root / "kanso.toml"
    path.write_text(f"{path.read_text(encoding='utf-8')}\n[adapters.okx]\n{body}", "utf-8")
    return root


def execution(runner: CliRunner, root: Path) -> tuple[int, dict[str, object]]:
    result = at(runner, root, "doctor", "--json")
    check = next(one for one in payload(result)["checks"] if one["name"] == "execution")
    return result.exit_code, check


def test_portfolio_clients_lists_both_accounts_with_their_names_and_nothing_resolved(
    runner: CliRunner, workspace: Path
) -> None:
    listed = {
        one["id"]: one
        for one in payload(at(runner, workspace, "portfolio", "clients", "--json"))["clients"]
    }

    assert (listed[DEMO]["capital"], listed[DEMO]["clock"]) == ("broker_paper", "wall")
    assert (listed[LIVE]["capital"], listed[LIVE]["clock"]) == ("real", "wall")
    assert listed[LIVE]["stages"] == ["live"]
    assert listed[DEMO]["source"] == listed[LIVE]["source"] == "okx"
    assert listed[DEMO]["credentials"] == [
        "KANSO_OKX_DEMO_API_KEY",
        "KANSO_OKX_DEMO_API_SECRET",
        "KANSO_OKX_DEMO_PASSPHRASE",
    ]
    assert listed[LIVE]["credentials_resolve"] is False


def test_a_stage_naming_the_demo_client_is_refused_by_the_wall_clock_rule(
    runner: CliRunner, workspace: Path
) -> None:
    """The long-running node is what these clients await, exactly as every broker's do."""
    reconfigure(workspace, "paper", exec=DEMO, data="okx", speed=1)

    stages = payload(at(runner, workspace, "portfolio", "clients", "--json"))["stages"]

    assert "runs on the wall clock" in str(stages["paper"])
    assert "simulated venue" in str(stages["paper"])


def test_a_live_stage_naming_the_real_client_is_refused_by_the_same_rule(
    runner: CliRunner, workspace: Path
) -> None:
    """Real capital is confined to the live stage, and the live stage is no exception to
    the clock: a bounded replay would fill a real account's orders in simulation."""
    reconfigure(workspace, "live", exec=LIVE, data="okx", speed=1)

    stages = payload(at(runner, workspace, "portfolio", "clients", "--json"))["stages"]

    assert "runs on the wall clock" in str(stages["live"])
    assert "simulated venue" in str(stages["live"])


def test_doctor_with_no_table_and_no_keys_reports_okx_not_configured_and_passes(
    runner: CliRunner, workspace: Path
) -> None:
    code, check = execution(runner, workspace)

    assert code == Exit.OK and check["status"] == "ok"
    assert (
        "broker okx: not configured (0/2 account(s) configured) · [adapters.okx] absent, "
        "read as its defaults"
    ) in check["items"]  # type: ignore[operator]
    assert any(
        str(item).startswith(f"{DEMO}: broker_paper · clock wall · okx")
        and "KANSO_OKX_DEMO_PASSPHRASE=unset" in str(item)
        for item in check["items"]  # type: ignore[attr-defined]
    )


def test_doctor_with_a_valid_table_and_no_keys_reports_the_clients_unset_and_passes(
    runner: CliRunner, workspace: Path
) -> None:
    code, check = execution(runner, with_table(workspace, 'region = "us"\nrate_per_second = 8\n'))

    assert code == Exit.OK and check["status"] == "ok"
    items = [str(item) for item in check["items"]]  # type: ignore[attr-defined]
    assert "broker okx: not configured (0/2 account(s) configured) · [adapters.okx] valid" in items
    assert any(
        item.startswith(f"{LIVE}: real") and "KANSO_OKX_API_KEY=unset" in item for item in items
    )


def test_doctor_fails_a_table_the_package_s_model_refuses(
    runner: CliRunner, workspace: Path
) -> None:
    code, check = execution(runner, with_table(workspace, 'region = "au"\n'))

    assert code == Exit.PRECONDITION and check["status"] == "fail"
    assert "[adapters.okx] refused" in str(check["detail"])
    assert any("'au'" in str(item) for item in check["items"])  # type: ignore[attr-defined]


# -- the public reference -------------------------------------------------------------


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch) -> Replay:
    """The recorded answers, served wherever the adapter would build the engine's client."""
    served = Replay()
    monkeypatch.setattr(reference, "pyo3_transport", lambda rate, **_: served)
    return served


def resolving(root: Path) -> Path:
    """The workspace on the US host, resolving through the exchange, on a USDT account."""
    with_table(root, 'region = "us"\n\n[data]\nreference = "okx"\n')
    path = root / "kanso.toml"
    text = path.read_text(encoding="utf-8").replace('currency = "USD"', 'currency = "USDT"', 1)
    path.write_text(text, encoding="utf-8")
    return root


def test_resolve_puts_the_listed_swap_in_the_store_with_zero_fees(
    runner: CliRunner, workspace: Path, replay: Replay
) -> None:
    result = at(
        runner,
        resolving(workspace),
        "data",
        "instruments",
        "resolve",
        "BTC-USDT-SWAP.OKX",
        "ETH-USDT-SWAP.OKX",
        "--as-of",
        "2026-09-30",
        "--json",
    )

    assert result.exit_code == Exit.OK, result.stdout
    held = {one["id"]: one["definition"] for one in payload(result)["instruments"]}
    btc = held["BTC-USDT-SWAP.OKX"]
    assert btc["type"] == "CryptoPerpetual"
    assert (btc["multiplier"], btc["price_increment"], btc["lot_size"]) == ("0.01", "0.1", "0.01")
    assert (btc["settlement_currency"], btc["quote_currency"]) == ("USDT", "USDT")
    assert (btc["maker_fee"], btc["taker_fee"], btc["is_inverse"]) == ("0", "0", False)
    assert held["ETH-USDT-SWAP.OKX"]["multiplier"] == "0.1"
    assert [params["instId"] for _, params in replay.asked] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def test_resolve_refuses_the_inverse_contract_by_name(
    runner: CliRunner, workspace: Path, replay: Replay
) -> None:
    result = at(
        runner,
        resolving(workspace),
        "data",
        "instruments",
        "resolve",
        "BTC-USD-SWAP.OKX",
        "--as-of",
        "2026-09-30",
        "--json",
    )

    assert result.exit_code == Exit.VALIDATION
    assert "BTC-USD-SWAP is an inverse contract" in result.stdout
    assert "kanso trades linear perpetuals" in result.stdout


def test_check_surveys_the_listing_with_one_request_once_the_table_names_a_host(
    runner: CliRunner, workspace: Path, replay: Replay
) -> None:
    unnamed = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))
    assert replay.asked == []
    assert "okx: not configured, so no request was made for it" in unnamed["notes"]

    named = payload(
        at(
            runner,
            with_table(workspace, 'region = "us"\n'),
            "data",
            "adapters",
            "--check",
            "--json",
        )
    )

    assert len(replay.asked) == 1
    [survey] = [one for one in named["reach"] if one["adapter"] == "okx"]
    assert (survey["reachable"], survey["requests"]) == (True, 1)


def test_doctor_checks_the_listing_only_when_asked(
    runner: CliRunner, workspace: Path, replay: Replay
) -> None:
    root = with_table(workspace, 'region = "us"\n')
    plain = payload(at(runner, root, "doctor", "--json"))
    adapters = next(one for one in plain["checks"] if one["name"] == "adapters")
    assert "2 registered · 1 configured; no request was made" in str(adapters["detail"])
    assert replay.asked == []

    checked = payload(at(runner, root, "doctor", "--check-adapters", "--json"))

    adapters = next(one for one in checked["checks"] if one["name"] == "adapters")
    assert adapters["status"] == "ok"
    assert "1/1 datasets included · 1 request(s)" in str(adapters["detail"])
    assert any("okx perpetuals reference → ok" in str(item) for item in adapters["items"])


def test_a_table_that_names_no_host_is_passed_by_and_every_other_adapter_still_probed(
    runner: CliRunner, workspace: Path, replay: Replay
) -> None:
    """The broker's model accepts `[adapters.okx]` with no region; the reference cannot
    send a request without one, so both probes list it unconfigured and carry on."""
    root = with_table(workspace, "rate_per_second = 5\n")

    listed = at(runner, root, "data", "adapters", "--check", "--json")
    doctor = at(runner, root, "doctor", "--check-adapters", "--json")

    assert listed.exit_code == Exit.OK, listed.stdout
    assert "okx: not configured, so no request was made for it" in payload(listed)["notes"]
    assert {one["id"] for one in payload(listed)["adapters"]} >= {"okx", "massive"}
    adapters = next(one for one in payload(doctor)["checks"] if one["name"] == "adapters")
    assert adapters["status"] == "ok"
    assert "2 registered · 0 configured" in str(adapters["detail"])
    assert replay.asked == []


def test_data_load_fills_the_catalog_with_bars_prints_and_funding_and_no_credential(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each loader through the command, on the requests the loaders were recorded sending."""
    listing, history = Replay(), History()
    monkeypatch.setattr(
        reference,
        "pyo3_transport",
        lambda rate, **_: (
            lambda url, params: (
                history(url, params) if recorded_for(url, params) else listing(url, params)
            )
        ),
    )
    root = resolving(workspace)
    resolved = at(
        runner, root, "data", "instruments", "resolve", "BTC-USDT-SWAP.OKX", "USDC-USDT-SWAP.OKX",
        "--as-of", "2026-09-30", "--json",
    )  # fmt: skip
    assert resolved.exit_code == Exit.OK, resolved.stdout
    specs = {
        "okx_bars": "instruments: [USDC-USDT-SWAP]\nstart: 2026-09-28\nend: 2026-09-28\n"
        "resolution: 1m\n",
        "okx_trades": "instruments: [USDC-USDT-SWAP.OKX]\nstart: 2026-09-26\nend: 2026-09-26\n",
        "okx_funding": "instruments: [BTC-USDT-SWAP]\nstart: 2026-09-28\nend: 2026-09-29\n",
    }
    loaded: dict[str, dict[str, object]] = {}
    for loader, text in specs.items():
        spec = root / f"{loader}.yaml"
        spec.write_text(f"loader: {loader}\n{text}", encoding="utf-8")
        result = at(runner, root, "data", "load", "--loader", loader, "--spec", spec, "--json")
        assert result.exit_code == Exit.OK, result.stdout
        [loaded[loader]] = payload(result)["datasets"]

    assert {name: one["rows"] for name, one in loaded.items()} == {
        "okx_bars": 1440,
        "okx_trades": 214,
        "okx_funding": 6,
    }
    assert loaded["okx_funding"]["dataset_id"] == "BTC_USDT_SWAP.OKX-funding-none-raw-20260929"
    assert all(one["publication"] == "realtime" and not one["truncated"] for one in loaded.values())
    assert not [url for url, params in history.asked if "key" in str(params).lower()]


def test_data_load_refuses_trade_days_the_archives_do_not_serve(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing, history = Replay(), History()
    monkeypatch.setattr(
        reference,
        "pyo3_transport",
        lambda rate, **_: (
            lambda url, params: (
                history(url, params) if recorded_for(url, params) else listing(url, params)
            )
        ),
    )
    root = resolving(workspace)
    at(
        runner,
        root,
        "data",
        "instruments",
        "resolve",
        "USDC-USDT-SWAP.OKX",
        "--as-of",
        "2026-09-30",
    )
    spec = root / "trades.yaml"
    spec.write_text(
        "loader: okx_trades\ninstruments: [USDC-USDT-SWAP]\nstart: 2026-09-28\nend: 2026-09-29\n",
        encoding="utf-8",
    )

    result = at(runner, root, "data", "load", "--loader", "okx_trades", "--spec", spec, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "UTC days 2026-09-29 are not served" in payload(result)["error"]


AEON_ENTRY = """AEON-USDT-SWAP.OKX:
  nautilus_id: AEON-USDT-SWAP.OKX
  asset_class: CRYPTOCURRENCY
  manual: true
  corporate_actions: none
  override:
    instrument_class: swap
    base_currency: AEON
    quote_currency: USDT
    settlement_currency: USDT
    multiplier: "10"
    price_increment: "0.00001"
    size_increment: "1"
    lot_size: "1"
"""
"""AEON as the exchange's listing gave it to the operator's workspace on 2026-10-02 (see
`tests/nautilus/adapters/okx/test_book.py`)."""


def test_data_load_writes_a_book_and_prints_a_dataset_a_day(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """okx_book and okx_trades declare a day per dataset: two days of prints are two
    datasets with a manifest each, and a day of the book is one, its points the changes the
    recorded excerpt makes to the top three."""
    listing, history = Replay(), History()
    monkeypatch.setattr(
        reference,
        "pyo3_transport",
        lambda rate, **_: (
            lambda url, params, headers=None: (
                history(url, params, headers) if recorded_for(url, params) else listing(url, params)
            )
        ),
    )
    root = resolving(workspace)
    (root / "instruments.yaml").write_text(AEON_ENTRY, encoding="utf-8")
    resolved = at(
        runner, root, "data", "instruments", "resolve", "USDC-USDT-SWAP.OKX",
        "AEON-USDT-SWAP.OKX", "--as-of", "2026-09-30", "--json",
    )  # fmt: skip
    assert resolved.exit_code == Exit.OK, resolved.stdout
    specs = {
        "okx_trades": "instruments: [USDC-USDT-SWAP]\nstart: 2026-09-26\nend: 2026-09-27\n",
        "okx_book": "instruments: [AEON-USDT-SWAP]\nstart: 2026-09-01\nend: 2026-09-01\n"
        "levels: 3\n",
    }
    loaded: dict[str, list[dict[str, object]]] = {}
    for loader, text in specs.items():
        spec = root / f"{loader}.yaml"
        spec.write_text(f"loader: {loader}\n{text}", encoding="utf-8")
        result = at(runner, root, "data", "load", "--loader", loader, "--spec", spec, "--json")
        assert result.exit_code == Exit.OK, result.stdout
        loaded[loader] = payload(result)["datasets"]

    assert [(one["span"], one["rows"]) for one in loaded["okx_trades"]] == [
        (["2026-09-26", "2026-09-26"], 214),
        (["2026-09-27", "2026-09-27"], 288),
    ]
    [day] = loaded["okx_book"]
    assert (day["dataset_id"], day["span"]) == (
        "AEON_USDT_SWAP.OKX-book-none-raw-20260901",
        ["2026-09-01", "2026-09-01"],
    )
    assert day["rows"] > 0 and day["publication"] == "realtime"
    shown = payload(at(runner, root, "data", "show", "--json"))["series"]
    assert {(one["instrument"], one["type"]) for one in shown} == {
        ("USDC-USDT-SWAP.OKX", "trade"),
        ("AEON-USDT-SWAP.OKX", "book"),
    }
    assert not [url for url, params in history.asked if "key" in str(params).lower()]
