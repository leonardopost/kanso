"""`kanso data adapters`: what is registered here, and what a key actually reaches.

Two properties are under test and they pull in opposite directions.

Adapter isolation is the first: the core knows no vendor, so a workspace with every credential
unset still has a complete answer to "what can this fetch?" — the package's own loaders,
the manual instrument provider and every registered adapter, listed with the variable
names each would need and where each resolves from, never a value. Nothing opens a socket
and nothing resolves a secret to say it, which is why the command answers at all here.

The second is what `--check` adds: the offer is declared and the reach is measured, and
they are different questions. Entitlement and the history floor both belong to a plan on a
day, so both are probed, per dataset, at the grain the source gates on — and the two are
reported apart, because a dataset a plan excludes and a range older than the source holds
are the pair an operator confuses at the cost of a subscription they already hold.

Every request here is answered from a frozen body through an injected transport. Nothing
in this module reaches a network, and the credential it sets is a string a test made up.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.data.adapters.massive import ACCESS_KEY_ID, API_KEY, SECRET_KEY
from kanso.data.adapters.massive.client import Response
from kanso.errors import Exit

from ..data.adapters.massive import Replay, refused, served
from . import massive_wire
from .conftest import at, payload
from .massive_wire import FUTURE, KEY, OPTION

# -- what is registered, with nothing configured ------------------------------------


def test_the_loaders_the_manual_provider_and_the_adapter_are_what_is_registered(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "data", "adapters", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    by_id = {item["id"]: item for item in document["adapters"]}
    assert set(by_id) == {"synthetic", "csv_parquet", "manual", "massive"}
    assert by_id["synthetic"]["kind"] == "data"
    assert by_id["manual"]["kind"] == "reference"
    assert by_id["massive"]["kind"] == "data"
    assert by_id["massive"]["provider"] == "builtin"


def test_a_loader_that_needs_nothing_resolves_and_an_adapter_that_needs_three_does_not(
    runner: CliRunner, workspace: Path
) -> None:
    """An unconfigured adapter is the ordinary state of a fresh workspace, not a failure."""
    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert by_id["synthetic"]["credentials"] == []
    assert by_id["synthetic"]["credentials_resolve"] is True
    assert by_id["massive"]["credentials_resolve"] is False
    assert set(by_id["massive"]["credential_origins"].values()) == {None}
    assert by_id["massive"]["quota"] == "90/s"


def test_the_vendor_loaders_are_listed_under_their_adapter_and_none_is_built(
    runner: CliRunner, workspace: Path
) -> None:
    """Listing what an adapter can fetch must open nothing: no credential resolves here."""
    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert "massive_bars" in by_id["massive"]["loaders"]
    assert "massive_bulk" in by_id["massive"]["loaders"]
    assert "massive_financials" in by_id["massive"]["loaders"]


def test_it_says_what_an_unconfigured_adapter_would_need(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "data", "adapters")

    assert result.exit_code == Exit.OK
    assert "synthetic · data · builtin · no credential" in result.stdout
    assert "registered and unconfigured" in result.stdout
    assert "KANSO_MASSIVE_API_KEY" in result.stdout


def test_a_half_configured_adapter_says_which_name_is_still_missing(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """One transport's key set and another's not is not "configured", and rounding it to
    that hides the refusal until something reaches for the name that is missing."""
    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert by_id["massive"]["credentials_resolve"] is False
    note = next(item for item in document["notes"] if "is configured" in item)
    assert "KANSO_MASSIVE_SECRET_KEY" in note
    assert "KANSO_MASSIVE_API_KEY" not in note
    assert wired.asked == []


def test_a_fully_configured_adapter_is_reported_without_a_note(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is missing, so nothing is said: a note about every adapter is a note ignored."""
    for name in (API_KEY, ACCESS_KEY_ID, SECRET_KEY):
        monkeypatch.setenv(name, KEY)

    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert by_id["massive"]["credentials_resolve"] is True
    assert set(by_id["massive"]["credential_origins"].values()) == {"environment"}
    assert document["notes"] == []


def test_check_makes_no_network_call_when_nothing_is_configured(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "data", "adapters", "--check", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["checked"] is True
    assert document["reach"] == []
    assert any("made no network call" in note for note in document["notes"])


def test_a_configured_table_nothing_provides_is_named(runner: CliRunner, workspace: Path) -> None:
    config = workspace / "kanso.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[adapters.acme]\nbase_url = "https://acme"\n',
        encoding="utf-8",
    )

    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    unprovided = [note for note in document["notes"] if "nothing here provides" in note]
    assert unprovided == ["kanso.toml configures acme, which nothing here provides"]


def test_an_extension_s_loader_is_registered_beside_the_built_in_ones(
    runner: CliRunner, workspace: Path
) -> None:
    """The registry has one entry point, identical for a package loader and an operator's."""
    package = workspace / "kanso_ext" / "mine"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "PROVIDES = {'loaders': ('mine',)}\n\n\n"
        "class Mine:\n"
        "    id = 'mine'\n\n"
        "    def discover(self, spec):\n        return []\n\n"
        "    def load(self, ref, window):\n        return []\n\n"
        "    def load_arrow(self, ref, window):\n        return None\n\n"
        "    def manifest(self, ref):\n        return None\n\n\n"
        "LOADERS = {'mine': Mine()}\n",
        encoding="utf-8",
    )

    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert by_id["mine"]["provider"] == "extension"


# -- what the key reaches ------------------------------------------------------------


def reach_of(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Every measured reach, keyed by the class and dataset it is about."""
    return {
        (item["asset_class"], item["dataset"]): item
        for survey in document["reach"]
        for item in survey["reach"]
    }


def test_entitlement_is_reported_per_dataset_because_a_class_has_two_answers(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """Options aggregates are included and options ticks are not, under one plan."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("options", "bars")]["outcome"] == "ok"
    assert found[("options", "trades")]["outcome"] == "not_entitled"
    assert found[("options", "quotes")]["outcome"] == "not_entitled"
    assert found[("stocks", "trades")]["outcome"] == "ok"


def test_a_refusal_and_a_history_floor_are_never_the_same_answer(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """The one confusion this costs money: a plan that excludes, or a source that is short."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("forex", "bars")]["outcome"] == "ok"
    assert found[("forex", "bars")]["floor"] == "2024-09-04"
    assert found[("forex", "quotes")]["outcome"] == "not_entitled"
    assert found[("forex", "quotes")]["floor"] is None


def test_the_floor_is_measured_per_dataset_and_dated(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """A plan with a rolling window has a floor that moves, so a floor names its own day."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("stocks", "bars")]["floor"] == "2003-09-10"
    assert found[("options", "bars")]["floor"] == "2024-09-03"
    assert found[("indices", "bars")]["floor"] == "2023-03-01"
    assert found[("stocks", "bars")]["probed_on"] is not None


def test_an_index_answer_is_reported_at_the_grain_the_source_gates_on(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """One index says nothing about the next, so the line says which index it is about."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("indices", "bars")]["grain"] == "ticker"
    assert found[("indices", "bars")]["ticker"] == "I:NDX"
    assert found[("stocks", "bars")]["grain"] == "endpoint"
    survey = document["reach"][0]
    assert any("no other index" in note for note in survey["notes"])


def test_a_reference_listing_is_probed_over_the_market_and_never_floored(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """An empty fortnight for one issuer is that issuer; an empty market is the plan."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("stocks", "splits")]["ticker"] is None
    assert found[("stocks", "splits")]["floor"] is None
    assert found[("stocks", "filings")]["outcome"] == "not_entitled"
    assert not any(asked.params.get("ticker") for asked in wired.asked if "splits" in asked.url)


def test_an_entitled_listing_reads_ok_on_the_screen_read_before_buying(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """The defect, on the line it was reported from, and the true refusal beside it.

    Statements, splits and dividends are in this plan and answer a fortnight with an empty
    page; filings are genuinely excluded and refused at every date. All four are asked the
    same way, so nothing here distinguishes them but the vendor's own answer — which is the
    point: an empty page reported as `not_entitled` sends an operator to buy a subscription
    they already hold, on the one screen they read before buying.
    """
    found = reach_of(payload(at(runner, workspace, "data", "adapters", "--check", "--json")))

    assert found[("stocks", "financials")]["outcome"] == "ok"
    assert found[("stocks", "splits")]["outcome"] == "ok"
    assert found[("stocks", "dividends")]["outcome"] == "ok"
    assert found[("stocks", "filings")]["outcome"] == "not_entitled"


def test_a_listing_line_never_reports_a_window_that_was_never_asked_for(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """The detail beside an outcome is the whole account of the probe an operator gets.

    These listings are asked market-wide with no dates at all, so a line claiming a recent
    window asserts that the key returns *current* statements when all that was established
    is that the vendor holds some statement for some issuer at some date.
    """
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    for dataset in ("financials", "splits", "dividends", "filings"):
        line = found[("stocks", dataset)]
        assert "recent window" not in line["detail"], dataset
    assert any("no date window" in note for note in document["reach"][0]["notes"])


def test_an_option_key_is_asked_about_where_the_vendor_keeps_it(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """The generic ticker reference rejects an option key, so the control cannot be asked
    there: a plan that really excludes option ticks would come back reported as a wrong
    prefix, sending an operator to correct a ticker that was already right.
    """
    payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    assert any(f"{massive_wire.CONTRACTS}/{OPTION}" in asked.url for asked in wired.asked)
    assert not [asked for asked in wired.asked if f"{massive_wire.TICKERS}/{OPTION}" in asked.url]


def test_each_listing_is_asked_for_under_the_version_that_serves_it(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """The vendor versions its listings one at a time, and a wrong prefix is a bare 404 —
    which is not a refusal and not an empty window, so a survey reading it as either says
    `unavailable` about a dataset whose entitlement it never established."""
    found = reach_of(payload(at(runner, workspace, "data", "adapters", "--check", "--json")))

    assert found[("stocks", "filings")]["outcome"] == "not_entitled"
    asked = {asked.url.split("//", 1)[-1].partition("/")[2].split("?")[0] for asked in wired.asked}
    assert {path for path in asked if path.endswith("reference/sec/filings")} == {
        "v1/reference/sec/filings"
    }
    assert "unavailable" not in {item["outcome"] for item in found.values()}


def test_no_listing_asks_for_a_page_wider_than_the_source_serves(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """A ceiling belongs to one endpoint. The financials listing rejects a page of a
    thousand outright rather than trimming it, so a limit borrowed from the listing next
    door yields no rows at all and reads as a dataset the plan excludes."""
    payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    sent = {
        massive_wire.LISTINGS[path]: int(asked.params["limit"])
        for asked in wired.asked
        for path in [f"/{asked.url.split('//', 1)[-1].partition('/')[2].split('?')[0]}"]
        if path in massive_wire.LISTINGS
    }

    assert set(sent) == set(massive_wire.LISTINGS.values())
    assert all(width <= massive_wire.CEILING[name] for name, width in sent.items()), sent
    assert sent["financials"] < sent["splits"]


def test_the_two_keys_that_expire_are_discovered_and_not_assumed(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """A hard-coded contract would go stale and report a dead key as a plan failure."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("options", "bars")]["ticker"] == OPTION
    assert found[("futures", "bars")]["ticker"] == FUTURE
    assert any("options/contracts" in asked.url for asked in wired.asked)


def test_the_survey_reports_the_requests_it_actually_made(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """A check has to be bounded, and the number it reports is counted rather than claimed."""
    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    assert document["reach"][0]["requests"] == len(wired.asked)
    assert document["reach"][0]["reachable"] is True


def test_a_key_the_vendor_refuses_ends_the_survey_rather_than_reporting_a_plan(
    runner: CliRunner, wired: Replay, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key that does not authenticate refuses everything; calling that a plan is the trap."""
    monkeypatch.setattr(wired, "answer", lambda url, params: refused())

    result = at(runner, workspace, "data", "adapters", "--check", "--json")

    assert result.exit_code == Exit.PRECONDITION
    survey = payload(result)["reach"][0]
    assert survey["reachable"] is False
    assert survey["reach"] == []
    assert survey["requests"] == 1
    assert any("would be the wrong answer" in note for note in survey["notes"])


def test_no_credential_value_reaches_the_output(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """Names and origins are reportable; a value is not, in either rendering."""
    plain = at(runner, workspace, "data", "adapters", "--check")
    as_json = at(runner, workspace, "data", "adapters", "--check", "--json")

    assert KEY not in plain.stdout
    assert KEY not in as_json.stdout
    assert "environment" in plain.stdout


# -- the seams: one registry, one loader lookup, one reference provider ---------------


def test_a_vendor_loader_is_opened_for_the_workspace_and_writes_what_it_served(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """`data load` reaches a vendor loader through the same lookup a built-in one uses.

    A vendor loader cannot be a shared instance — its credential, its quota and its cache
    all come from the workspace it is opened for — so the registry hands out factories and
    this is the one place one is called.
    """
    end = date.today() - timedelta(days=5)
    start = end - timedelta(days=15)
    spec = workspace / "vendor.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "loader": "massive_bars",
                "asset_class": "stocks",
                "instruments": ["AAPL"],
                "venue": "XNAS",
                "resolution": "1d",
                "start": str(start),
                "end": str(end),
            }
        ),
        encoding="utf-8",
    )

    result = at(runner, workspace, "data", "load", "--loader", "massive_bars", "--spec", spec)

    assert result.exit_code == Exit.OK, result.stdout
    held = payload(at(runner, workspace, "data", "show", "--json"))
    assert held["datasets"] == 1
    assert held["series"][0]["instrument"] == "AAPL.XNAS"


def test_an_unknown_loader_is_refused_naming_the_vendor_ids_too(
    runner: CliRunner, workspace: Path
) -> None:
    """A typo in a spec is the operator's to fix, so the refusal lists what does exist."""
    spec = workspace / "typo.yaml"
    spec.write_text("loader: massive_bar\n", encoding="utf-8")

    result = at(
        runner, workspace, "data", "load", "--loader", "massive_bar", "--spec", spec, "--json"
    )

    assert result.exit_code == Exit.VALIDATION
    message = str(payload(result)["error"])
    assert "massive_bars" in message
    assert "synthetic" in message


def test_the_reference_provider_is_reached_through_the_registry(
    runner: CliRunner, wired: Replay, workspace: Path
) -> None:
    """`[data] reference` names an adapter id, and nothing in the core knows which."""
    config = workspace / "kanso.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[data]\nreference = "massive"\n',
        encoding="utf-8",
    )
    (workspace / "instruments.yaml").write_text(
        yaml.safe_dump(
            {
                "AAPL": {
                    "nautilus_id": "AAPL.XNAS",
                    "asset_class": "EQUITY",
                    "corporate_actions": "adjust_all",
                }
            }
        ),
        encoding="utf-8",
    )

    result = at(runner, workspace, "data", "instruments", "resolve", "--as-of", "2026-01-02")

    assert result.exit_code == Exit.OK, result.stdout
    assert any("/v3/reference/tickers/AAPL" in asked.url for asked in wired.asked)


def test_an_extension_may_register_an_adapter_of_its_own(
    runner: CliRunner, workspace: Path
) -> None:
    """One registry, and the same entry point for a packaged adapter and an operator's."""
    package = workspace / "kanso_ext" / "acme"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(ADAPTER_EXTENSION, encoding="utf-8")

    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert by_id["acme"]["provider"] == "extension"
    assert by_id["acme"]["credentials"] == ["KANSO_ACME_API_KEY"]
    assert by_id["acme"]["capabilities"] == ["bars"]


ADAPTER_EXTENSION = """PROVIDES = {"adapters": ("acme",)}


class Capabilities:
    def names(self):
        return ("bars",)

    def payload(self):
        return {"datasets": ["bars"]}


class Acme:
    id = "acme"
    kind = "data"
    capabilities = Capabilities()
    credentials = ("KANSO_ACME_API_KEY",)

    def client(self, ws):
        raise RuntimeError("not opened by this test")

    def configured(self, ws):
        return False

    def credential_origins(self, ws):
        return {"KANSO_ACME_API_KEY": None}

    def quota(self, ws):
        return "10/s"

    def loaders(self, ws):
        return {}

    def provider(self, ws):
        return None

    def survey(self, ws):
        raise RuntimeError("not probed by this test")


ADAPTERS = {"acme": Acme()}
"""
"""An adapter written the way an operator would write one, in a workspace extension."""


# -- what the survey does when it cannot ask ------------------------------------------


def test_a_class_whose_key_cannot_be_found_is_reported_unprobed_not_unentitled(
    runner: CliRunner, wired: Replay, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was asked, so nothing was established — which is a fifth thing to say."""

    def scarce(url: str, params: Mapping[str, str]) -> Response:
        if "options/contracts" in url or url.endswith("/v3/reference/tickers"):
            return refused()
        return massive_wire.answer(url, params)

    monkeypatch.setattr(wired, "answer", scarce)

    document = payload(at(runner, workspace, "data", "adapters", "--check", "--json"))

    found = reach_of(document)
    assert found[("options", "bars")]["outcome"] == "unprobed"
    assert found[("futures", "bars")]["outcome"] == "unprobed"
    assert found[("stocks", "bars")]["outcome"] == "ok"
    survey = document["reach"][0]
    assert any("neither entitled nor refused" in note for note in survey["notes"])


def test_a_listing_page_that_names_no_key_leaves_the_class_unprobed(
    runner: CliRunner, wired: Replay, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page of rows without a ticker in them is not a key, and is not treated as one."""

    def nameless(url: str, params: Mapping[str, str]) -> Response:
        if "options/contracts" in url:
            return served([{"name": "a row with no key in it"}])
        return massive_wire.answer(url, params)

    monkeypatch.setattr(wired, "answer", nameless)

    found = reach_of(payload(at(runner, workspace, "data", "adapters", "--check", "--json")))

    assert found[("options", "bars")]["outcome"] == "unprobed"
    assert found[("options", "bars")]["ticker"] is None


def test_a_source_that_does_not_answer_is_not_an_entitlement_answer(
    runner: CliRunner, wired: Replay, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The absence of an answer is not one of the four outcomes, and is never read as one."""

    def flaky(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/quotes/" in url:
            return Response(status=503)
        return massive_wire.answer(url, params)

    monkeypatch.setattr(wired, "answer", flaky)

    found = reach_of(payload(at(runner, workspace, "data", "adapters", "--check", "--json")))

    assert found[("stocks", "quotes")]["outcome"] == "unavailable"
    assert found[("stocks", "bars")]["outcome"] == "ok"


def test_a_vendor_loader_refuses_on_its_variable_rather_than_reaching_for_a_key(
    runner: CliRunner, workspace: Path
) -> None:
    """With no credential set, a vendor loader is built and stops on the name it needs."""
    spec = workspace / "fundamentals.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "loader": "massive_financials",
                "instruments": ["AAPL"],
                "venue": "XNAS",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ),
        encoding="utf-8",
    )

    result = at(
        runner,
        workspace,
        "data",
        "load",
        "--loader",
        "massive_financials",
        "--spec",
        spec,
        "--json",
    )

    assert result.exit_code == Exit.PRECONDITION
    assert "KANSO_MASSIVE_API_KEY" in str(payload(result)["error"])
