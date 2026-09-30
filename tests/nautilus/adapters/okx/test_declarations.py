"""What the OKX package declares, and nothing it could send.

Four properties are under test. The registry finds the broker by reading the directory and
hands out both clients with the funding and the clock the core refuses from. Each client
resolves exactly three variables derived from its own id. The `[adapters.okx]` table
accepts the engine's regions and nothing else, holds no credential and has no default
region. And the venue declaration states the account, the currency and the two fee rates
and leaves the spread and slippage to kanso's defaults — so a bar-only hypothesis with no
spread width is refused rather than costed at a spread of zero.

Nothing here resolves a real credential or opens a socket: the package has no network code.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso import creds
from kanso.errors import Exit, PreconditionError, ValidationError
from kanso.nautilus import adapters
from kanso.nautilus import facts as engine_facts
from kanso.nautilus.adapters.okx import (
    BROKER,
    CREDENTIALS,
    DATA_CLIENTS,
    ID,
    OkxConfig,
    Region,
    facts,
)
from kanso.nautilus.adapters.okx.config import DEFAULT_RATE_PER_SECOND, spec
from kanso.nautilus.adapters.okx.venue import VENUE, declaration, instrument_id
from kanso.schemas import resolve_venue_model
from kanso.workspace import Workspace, init

DEMO = "okx_demo"
LIVE = "okx"


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A fresh workspace with none of this adapter's variables set anywhere."""
    for names in CREDENTIALS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    return init(tmp_path / "ws")


def table(ws: Workspace, **values: object) -> Workspace:
    """The same workspace with an `[adapters.okx]` table."""
    return replace(ws, config=ws.config.model_copy(update={"adapters": {ID: values}}))


# --- the registry ----------------------------------------------------------------


def test_the_registry_finds_the_broker_with_both_clients_and_their_declarations() -> None:
    assert adapters.packaged()[ID] is BROKER
    found = adapters.exec_clients()

    assert (found[DEMO].capital, found[DEMO].clock) == ("broker_paper", "wall")
    assert (found[LIVE].capital, found[LIVE].clock) == ("real", "wall")
    assert adapters.broker_of(DEMO) is BROKER
    assert adapters.broker_of(LIVE) is BROKER
    assert DATA_CLIENTS == ("okx",)
    assert BROKER.kind == "execution"


def test_a_client_this_broker_does_not_have_is_refused_by_name() -> None:
    with pytest.raises(PreconditionError) as refused:
        spec("okx_paper")

    assert "okx_demo" in str(refused.value) and refused.value.code is Exit.PRECONDITION


# --- the credentials -------------------------------------------------------------


def test_each_client_resolves_three_names_derived_from_its_own_id() -> None:
    assert BROKER.credentials(LIVE) == (
        "KANSO_OKX_API_KEY",
        "KANSO_OKX_API_SECRET",
        "KANSO_OKX_PASSPHRASE",
    )
    assert BROKER.credentials(DEMO) == (
        "KANSO_OKX_DEMO_API_KEY",
        "KANSO_OKX_DEMO_API_SECRET",
        "KANSO_OKX_DEMO_PASSPHRASE",
    )
    assert BROKER.credentials(DEMO)[0] == creds.standard_name(DEMO)


def test_nothing_is_configured_in_a_fresh_workspace(ws: Workspace) -> None:
    assert not BROKER.configured(ws, DEMO)
    assert not BROKER.configured(ws, LIVE)
    assert set(BROKER.credential_origins(ws, LIVE).values()) == {None}


def test_an_account_needs_all_three_to_count_as_configured(ws: Workspace) -> None:
    key, secret, passphrase = BROKER.credentials(DEMO)
    path = ws.root / creds.ENV_FILE
    path.write_text(f"{key}=not-a-key\n{secret}=not-a-secret\n")

    assert not BROKER.configured(ws, DEMO)

    path.write_text(f"{key}=not-a-key\n{secret}=not-a-secret\n{passphrase}=not-one\n")

    assert BROKER.configured(ws, DEMO)
    assert not BROKER.configured(ws, LIVE)


def test_the_engine_s_own_variables_configure_nothing(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine would read these for a `None`; the package never reads them at all."""
    for name in facts.AMBIENT:
        monkeypatch.setenv(name, "not-anyones")

    assert not BROKER.configured(ws, LIVE)
    assert not BROKER.configured(ws, DEMO)


# --- the table -------------------------------------------------------------------


@pytest.mark.parametrize("region", ["global", "eea", "us"])
def test_each_engine_region_is_accepted(ws: Workspace, region: str) -> None:
    settings = BROKER.config(table(ws, region=region))

    assert settings.require_region() is Region(region)


@given(st.sampled_from(list(Region)), st.sampled_from([str.upper, str.lower, str.title]))
def test_a_region_is_read_in_any_case_as_the_engine_parses_one(
    region: Region, case: object
) -> None:
    assert OkxConfig(region=case(region.value)).region is region  # type: ignore[operator]


def test_a_region_the_engine_does_not_serve_is_refused_by_name(ws: Workspace) -> None:
    with pytest.raises(ValidationError) as refused:
        BROKER.config(table(ws, region="au"))

    message = str(refused.value)
    assert "region" in message and "'au'" in message
    assert "global, eea, us" in message


def test_the_region_has_no_default_and_is_refused_rather_than_guessed(ws: Workspace) -> None:
    """The engine's own default sends a key to the global host, which refuses most of them."""
    assert OkxConfig(region=None).region is None
    with pytest.raises(PreconditionError) as refused:
        BROKER.config(ws).require_region()

    assert "us = https://us.okx.com" in str(refused.value)
    assert refused.value.remedy is not None and "region" in refused.value.remedy


def test_the_quota_is_read_back_from_the_table(ws: Workspace) -> None:
    assert BROKER.config(ws).rate_per_second == DEFAULT_RATE_PER_SECOND
    assert BROKER.config(table(ws, region="us", rate_per_second=12)).rate_per_second == 12
    with pytest.raises(ValidationError, match="rate_per_second"):
        BROKER.config(table(ws, rate_per_second=0))


def test_no_credential_may_be_written_into_the_table(ws: Workspace) -> None:
    with pytest.raises(ValidationError):
        BROKER.config(table(ws, api_key="nope"))


# --- the venue -------------------------------------------------------------------


def test_the_venue_declares_the_account_the_currency_and_the_two_rates_only() -> None:
    declared = adapters.venue_declaration(ID, VENUE)

    assert declared is not None and declared is declaration("OKX")
    assert (declared.account, declared.currency) == ("margin", "USDT")
    assert declared.costs is not None
    assert declared.costs.model_dump(exclude_none=True) == {
        "commission_bps": 5.0,
        "maker_bps": 2.0,
    }
    assert declaration("XNAS") is None


def test_the_declared_model_resolves_with_the_default_slippage_and_a_quoted_spread() -> None:
    model = resolve_venue_model(VENUE, broker=ID, declaration=declaration(VENUE))

    assert (model.costs.commission_bps, model.costs.maker_bps) == (5.0, 2.0)
    assert (model.costs.slippage_bps, model.costs.spread) == (1.0, "quotes")
    assert (model.currency, model.origins.costs) == ("USDT", "broker")


def test_a_bar_only_hypothesis_with_no_spread_width_is_refused_not_costed_at_zero() -> None:
    with pytest.raises(ValidationError, match="costs.fixed_bps"):
        resolve_venue_model(VENUE, declaration=declaration(VENUE), quotes_available=False)


def test_an_instrument_id_is_the_exchange_s_own_with_the_venue_appended() -> None:
    assert instrument_id("BTC-USDT-SWAP") == "BTC-USDT-SWAP.OKX"


# --- the engine facts ------------------------------------------------------------


def test_each_engine_fact_holds_and_is_among_the_claims_doctor_re_checks() -> None:
    """Checked one by one here; `tests/nautilus/test_facts.py` runs the whole set."""
    every = [claim for claim, _ in engine_facts.claims()]

    for claim, check in facts.CLAIMS:
        holds, evidence = check()
        assert holds, (claim, evidence)
        assert claim in every


def test_the_probe_s_child_sees_markers_and_never_the_parent_s_credentials() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "OKX_API_KEY": "the-parents-own",
        "OKX_WS_URL": "wss://elsewhere",
        "KANSO_OKX_API_KEY": "kanso-s-own",
        "UNRELATED": "x",
    }

    child = facts.probe_env(parent)

    assert child == {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        **dict.fromkeys(facts.AMBIENT, facts.MARKER),
    }


def _fingerprint() -> str:
    """The process environment as one digest, so a failure can never print a value."""
    return hashlib.sha256(repr(sorted(os.environ.items())).encode()).hexdigest()


def test_the_ambient_probe_runs_in_a_child_and_leaves_this_process_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check launches `sys.executable` with the probe's environment and nothing else."""
    launched: list[dict[str, str]] = []
    real = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        launched.append(dict(kwargs["env"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    before = _fingerprint()

    holds, _ = dict(facts.CLAIMS)[facts.CLAIMS[2][0]]()

    assert holds
    assert _fingerprint() == before
    (env,) = launched
    assert set(env) <= {*facts.PROBE_ENV, *facts.AMBIENT}
    assert all(env[name] == facts.MARKER for name in facts.AMBIENT)


def test_a_module_lying_in_the_working_directory_is_never_run_by_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kanso doctor` runs from a workspace; a `json.py` sitting there must not be imported by
    the probe's child, which would otherwise run whatever that file holds."""
    (tmp_path / "json.py").write_text("raise SystemExit('the working directory was imported')\n")
    monkeypatch.chdir(tmp_path)

    holds, evidence = dict(facts.CLAIMS)[facts.CLAIMS[2][0]]()

    assert holds, evidence


def test_a_probe_that_fails_is_a_claim_that_does_not_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(facts, "_PROBE", "raise SystemExit('engine gone')")

    for _, check in facts.CLAIMS[2:4]:
        holds, evidence = check()
        assert not holds
        assert "the probe exited 1" in evidence and "engine gone" in evidence
