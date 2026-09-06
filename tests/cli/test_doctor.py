"""`kanso doctor`: the diagnosis, its grades, its exit code and its redacted report."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import socket
import sqlite3
from pathlib import Path
from typing import Any

import nautilus_trader
import pytest
from typer.testing import CliRunner

from kanso import env
from kanso.cli import doctor as doctor_module
from kanso.data.snapshot import InstrumentDrift, newest
from kanso.errors import Exit, PreconditionError
from kanso.ext import KINDS, shipped
from kanso.nautilus import facts
from kanso.nautilus.adapters import exec_clients
from kanso.skills_sync import packaged_skills
from kanso.state import StateStore
from kanso.workspace import find

from ..data.adapters.massive import Replay, refused
from .conftest import HYP_ID, INSTRUMENT, RESEARCH, at, lane, payload, run

CHECKS = (
    "versions",
    "install",
    "engine wheel",
    "schema",
    "envelope",
    "repository",
    "gitignore",
    "best",
    "certificates",
    "record",
    "skills",
    "credentials",
    "adapters",
    "execution",
    "instruments",
    "lanes",
    "extensions",
    "engine facts",
)

REGISTER = """\
schema: 1
models:
  - id: big
    provider: acme-labs
    protocol: anthropic
    tier: [cheap, mid, frontier]
    local: false
    ctx: 200000
    cost_in: 1.0
    cost_out: 2.0
    tools: true
"""

SECRET = "sk-do-not-print-this"


def checks(result: Any) -> dict[str, dict[str, Any]]:
    """The checks of a `--json` diagnosis, by name."""
    document = payload(result)
    found = {str(check["name"]): check for check in document["checks"]}
    assert set(found) == set(CHECKS)
    return found


def status(result: Any, name: str) -> str:
    return str(checks(result)[name]["status"])


def items(result: Any, name: str) -> list[str]:
    return [str(item) for item in checks(result)[name].get("items", [])]


# -- the M0 acceptance: `kanso init && kanso doctor` green in all three situations ----


def test_init_then_doctor_is_green_in_a_fresh_directory(runner: CliRunner, fresh: Path) -> None:
    assert run(runner, "init", fresh).exit_code == Exit.OK

    result = at(runner, fresh, "doctor")

    assert result.exit_code == Exit.OK
    assert result.stdout.rstrip().endswith("0 fail")
    assert not [line for line in result.stdout.splitlines() if line.startswith("fail")]


def test_init_then_doctor_is_green_inside_a_repository(runner: CliRunner, repo: Path) -> None:
    assert run(runner, "init", repo).exit_code == Exit.OK

    result = at(runner, repo, "doctor", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["ok"] is True
    assert document["counts"]["fail"] == 0
    assert "enclosed by a repository" in str(checks(result)["repository"]["detail"])


def test_init_then_doctor_is_green_in_a_monorepo_subdirectory(
    runner: CliRunner, monorepo: Path
) -> None:
    assert run(runner, "init", monorepo).exit_code == Exit.OK

    result = at(runner, monorepo, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["ok"] is True


# -- shape and grading ----------------------------------------------------------------


def test_the_diagnosis_is_one_object_carrying_every_check(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "doctor", "--json")

    document = payload(result)
    assert set(document) == {"ok", "workspace", "counts", "checks"}
    assert document["workspace"] == str(workspace.resolve())
    for check in document["checks"]:
        assert check["status"] in {"ok", "warn", "fail"}
        assert check["detail"]
    assert sum(document["counts"].values()) == len(CHECKS)


def test_the_human_diagnosis_names_every_check(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "doctor")

    assert result.exit_code == Exit.OK
    for name in CHECKS:
        assert name in result.stdout
    assert f"{len(CHECKS)} checks" in result.stdout


def test_a_failing_check_exits_two_with_the_diagnosis_on_stdout(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "wheel_ok", lambda: (False, "wheel macosx_99_0_ppc does not fit"))

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    document = payload(result)
    assert document["ok"] is False
    assert document["counts"]["fail"] == 1
    assert checks(result)["engine wheel"]["status"] == "fail"


def test_versions_names_kanso_python_and_the_engine(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "doctor", "--json")

    detail = str(checks(result)["versions"]["detail"])
    assert "kanso" in detail and "python" in detail and "nautilus_trader" in detail


def test_install_reports_the_mode_and_the_running_package(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "doctor", "--json")

    detail = str(checks(result)["install"]["detail"])
    assert detail.split(" · ")[0] in {"editable", "package"}
    assert Path(detail.split(" · ")[1]).name == "kanso"


def test_schema_is_ok_in_a_fresh_workspace_because_init_migrated_it(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "schema") == "ok"
    assert result.exit_code == Exit.OK


def test_schema_warns_when_the_state_store_is_behind_the_package(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / "state.db").unlink()

    before = at(runner, workspace, "doctor", "--json")
    assert status(before, "schema") == "warn"
    assert "run `kanso migrate`" in str(checks(before)["schema"]["remedy"])

    assert at(runner, workspace, "migrate").exit_code == Exit.OK

    after = at(runner, workspace, "doctor", "--json")
    assert status(after, "schema") == "ok"
    assert after.exit_code == Exit.OK


def test_a_missing_envelope_is_a_warning_with_the_command_that_writes_one(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / "envelope.yaml").unlink()

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "envelope") == "warn"
    assert "kanso env detect" in str(checks(result)["envelope"]["remedy"])
    assert result.exit_code == Exit.OK


def test_an_envelope_detected_long_ago_is_stale(runner: CliRunner, workspace: Path) -> None:
    path = workspace / "envelope.yaml"
    text = path.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("detected_at:"))
    path.write_text(
        text.replace(line, "detected_at: '2020-01-01T00:00:00+00:00'"), encoding="utf-8"
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "envelope") == "warn"
    assert "days old" in str(checks(result)["envelope"]["detail"])


def test_an_envelope_from_another_machine_reports_the_field_that_changed(
    runner: CliRunner, workspace: Path
) -> None:
    path = workspace / "envelope.yaml"
    written = env.read(_workspace_object(workspace))
    assert written is not None
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            f"cores_total: {written.detected.cores_total}",
            f"cores_total: {written.detected.cores_total + 41}",
        ),
        encoding="utf-8",
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "envelope") == "warn"
    assert any("cores_total" in item for item in items(result, "envelope"))


def test_a_broken_envelope_is_reported_rather_than_believed(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / "envelope.yaml").write_text("schema: 1\nplan: nonsense\n", encoding="utf-8")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "envelope") == "warn"
    assert result.exit_code == Exit.OK


def test_missing_gitignore_entries_are_named(runner: CliRunner, workspace: Path) -> None:
    (workspace / ".gitignore").write_text("# nothing kanso asked for\n", encoding="utf-8")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "gitignore") == "warn"
    assert ".env" in items(result, "gitignore")


def test_a_removed_skill_link_is_a_warning_the_sync_clears(
    runner: CliRunner, workspace: Path
) -> None:
    link = sorted((workspace / ".claude" / "skills").iterdir())[0]
    link.unlink()

    result = at(runner, workspace, "doctor", "--json")
    assert status(result, "skills") == "warn"
    assert "kanso skills sync" in str(checks(result)["skills"]["remedy"])

    assert at(runner, workspace, "skills", "sync").exit_code == Exit.OK
    assert status(at(runner, workspace, "doctor", "--json"), "skills") == "ok"


def test_the_skills_check_counts_every_target(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "doctor", "--json")

    detail = str(checks(result)["skills"]["detail"])
    assert str(len(packaged_skills()) * 3) in detail


# -- credentials: names and origins, never values -------------------------------------


def _register(workspace: Path, text: str = REGISTER) -> None:
    (workspace / "models.yaml").write_text(text, encoding="utf-8")


def test_a_credential_resolves_from_the_env_file(runner: CliRunner, workspace: Path) -> None:
    _register(workspace)
    (workspace / ".env").write_text(f"KANSO_ACME_LABS_API_KEY={SECRET}\n", encoding="utf-8")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "credentials") == "ok"
    assert "KANSO_ACME_LABS_API_KEY: .env · big" in items(result, "credentials")
    assert SECRET not in result.stdout


def test_a_credential_resolves_from_the_environment(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(workspace)
    monkeypatch.setenv("KANSO_ACME_LABS_API_KEY", SECRET)

    result = at(runner, workspace, "doctor", "--json")

    assert "KANSO_ACME_LABS_API_KEY: environment · big" in items(result, "credentials")
    assert SECRET not in result.stdout


def test_an_unset_credential_is_a_warning_naming_the_variable(
    runner: CliRunner, workspace: Path
) -> None:
    _register(workspace)

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "credentials") == "warn"
    assert "KANSO_ACME_LABS_API_KEY: unset · big" in items(result, "credentials")
    assert result.exit_code == Exit.OK


def test_an_api_key_env_override_replaces_the_standard_name(
    runner: CliRunner, workspace: Path
) -> None:
    _register(workspace, REGISTER.replace("    tier:", "    api_key_env: MY_OWN_KEY\n    tier:"))

    result = at(runner, workspace, "doctor", "--json")

    listed = items(result, "credentials")
    assert any(item.startswith("MY_OWN_KEY:") for item in listed)
    assert not any(item.startswith("KANSO_ACME_LABS_API_KEY") for item in listed)


def test_the_mock_register_needs_no_credential(runner: CliRunner, fresh: Path) -> None:
    assert run(runner, "init", fresh, "--demo").exit_code == Exit.OK

    result = at(runner, fresh, "doctor", "--json")

    assert status(result, "credentials") == "ok"
    assert items(result, "credentials") == [
        "KANSO_WEBHOOK_URL: unset · escalation webhook (optional)"
    ]


def test_an_unreadable_register_is_a_warning_not_a_crash(
    runner: CliRunner, workspace: Path
) -> None:
    _register(workspace, "schema: 1\nmodels: [{id: x}]\n")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "credentials") == "warn"
    assert result.exit_code == Exit.OK


# -- extensions, adapters and the engine ----------------------------------------------


def test_extensions_are_listed_and_a_broken_one_is_reported(
    runner: CliRunner, workspace: Path
) -> None:
    package = workspace / "kanso_ext" / "good"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("PROVIDES = {'loaders': ['mine']}\n", encoding="utf-8")
    (workspace / "kanso_ext" / "broken.py").write_text(
        "raise RuntimeError('boom')\n", encoding="utf-8"
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "extensions") == "warn"
    listed = items(result, "extensions")
    assert "good: ok" in listed
    assert any(item.startswith("broken: RuntimeError") for item in listed)


def test_an_unconfigured_adapter_is_registered_reported_and_green(
    runner: CliRunner, workspace: Path
) -> None:
    """An adapter with nothing set is reported, and it is not a fault.

    Enablement is by credential and never by installation, so a registered adapter with no
    variable set is the ordinary state of a fresh workspace.
    """
    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "adapters") == "ok"
    assert "1 registered · 0 configured" in str(checks(result)["adapters"]["detail"])
    listed = items(result, "adapters")
    assert any(item.startswith("massive: data · 90/s") for item in listed)
    assert any("KANSO_MASSIVE_API_KEY=unset" in item for item in listed)


def test_check_adapters_reaches_nothing_when_no_credential_resolves(
    runner: CliRunner, workspace: Path
) -> None:
    """Opening an adapter with no key would fail on the variable, not on the plan."""
    asked = at(runner, workspace, "doctor", "--check-adapters", "--json")

    assert asked.exit_code == Exit.OK
    assert "made no network call" in str(checks(asked)["adapters"]["detail"])
    assert status(asked, "adapters") == "ok"


def test_check_adapters_reports_what_the_key_reaches_and_stays_green(
    runner: CliRunner, workspace: Path, wired: Replay
) -> None:
    """A dataset the plan excludes is reported, never graded down: it is a subscription."""
    result = at(runner, workspace, "doctor", "--check-adapters", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "adapters") == "ok"
    listed = items(result, "adapters")
    assert "massive stocks bars (AAPL, per endpoint) → ok · from 2003-09-10" in listed
    assert any("options trades" in item and "not_entitled" in item for item in listed)
    assert not any("from " in item and "not_entitled" in item for item in listed)


def test_a_key_that_does_not_authenticate_is_the_one_adapter_failure(
    runner: CliRunner, workspace: Path, wired: Replay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every later command through that adapter stops on it, so the diagnosis says so."""
    monkeypatch.setattr(wired, "answer", lambda url, params: refused())

    result = at(runner, workspace, "doctor", "--check-adapters", "--json")

    assert status(result, "adapters") == "fail"
    assert result.exit_code == Exit.PRECONDITION
    assert "did not authenticate" in str(checks(result)["adapters"]["detail"])


def test_an_extension_shadowing_a_registered_id_is_reported(
    runner: CliRunner, workspace: Path
) -> None:
    """The registries are read for the ids an extension would shadow, never listed by hand."""
    package = workspace / "kanso_ext" / "greedy"
    package.mkdir(parents=True)
    broker_client = sorted(exec_clients())[0]
    (package / "__init__.py").write_text(
        "PROVIDES = {'loaders': ['synthetic'], 'adapters': ['massive'], "
        f"'exec_clients': ['{broker_client}']}}\n",
        encoding="utf-8",
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "extensions") == "warn"
    listed = items(result, "extensions")
    assert "greedy shadows the built-in loaders 'synthetic'" in listed
    assert "greedy shadows the built-in adapters 'massive'" in listed
    assert f"greedy shadows the built-in exec_clients '{broker_client}'" in listed


def test_a_loader_a_packaged_adapter_provides_is_shadowed_in_doctor_as_in_ext_show(
    runner: CliRunner, workspace: Path
) -> None:
    """One table of what ships serves both commands, so neither is silent where the other
    speaks: an id an adapter's loaders hand out is reported here as `ext show` marks it."""
    package = workspace / "kanso_ext" / "vendored"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "PROVIDES = {'loaders': ['massive_bars']}\n", encoding="utf-8"
    )

    result = at(runner, workspace, "doctor", "--json")
    shown = payload(at(runner, workspace, "ext", "show", "--json"))

    assert status(result, "extensions") == "warn"
    assert "vendored shadows the built-in loaders 'massive_bars'" in items(result, "extensions")
    assert "1 shadowed id(s)" in str(checks(result)["extensions"]["detail"])
    [extension] = shown["extensions"]
    assert [item["state"] for item in extension["provides"]] == ["shadowed"]


def test_the_shadow_check_reads_every_kind_a_declaration_may_carry(
    runner: CliRunner, workspace: Path
) -> None:
    """A construct, a data type and the framework's own client shadow as easily as a loader.

    Each of those registries keeps the packaged id, so an extension declaring one is
    registered nowhere — the same silence a shadowed loader would be, and the reason to
    read every registry rather than the three an adapter happens to touch. `sandbox` is
    the sharpest of them: it is the execution client every workspace has without a broker.
    """
    package = workspace / "kanso_ext" / "unseen"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "PROVIDES = {'constructs': ['filter'], 'data_types': ['bar'], "
        "'exec_clients': ['sandbox']}\n",
        encoding="utf-8",
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "extensions") == "warn"
    listed = items(result, "extensions")
    assert "unseen shadows the built-in constructs 'filter'" in listed
    assert "unseen shadows the built-in data_types 'bar'" in listed
    assert "unseen shadows the built-in exec_clients 'sandbox'" in listed
    assert "3 shadowed id(s)" in str(checks(result)["extensions"]["detail"])


def test_the_declaration_and_the_shadow_check_name_the_same_kinds(workspace: Path) -> None:
    """One comparison reads both tables, so a kind in only one of them is a blind spot."""
    assert set(shipped(find(workspace))) == set(KINDS)


def test_doctor_makes_no_network_call(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("doctor opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    assert at(runner, workspace, "doctor").exit_code == Exit.OK
    assert at(runner, workspace, "doctor", "--check-adapters").exit_code == Exit.OK


def test_engine_facts_list_the_design_constraints_and_grade_ok(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "doctor", "--json")

    check = checks(result)["engine facts"]
    assert check["status"] == "ok"
    assert "claims hold on nautilus_trader" in str(check["detail"])
    listed = items(result, "engine facts")
    assert all(item.startswith("by design: ") for item in listed)
    assert {item.removeprefix("by design: ") for item in listed} == facts.DESIGN_CONSTRAINTS


def test_a_binding_that_no_longer_holds_fails_the_engine_facts(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gap outside the design constraints is a broken binding, evidence and all."""
    verified = facts.verify()
    broken = facts.Fact(
        claim="a claim kanso binds to", holds=False, evidence="RuntimeError: engine gone"
    )
    monkeypatch.setattr(facts, "verify", lambda: [*verified, broken])

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    check = checks(result)["engine facts"]
    assert check["status"] == "fail"
    assert "1 binding(s) broken" in str(check["detail"])
    assert "reinstall nautilus_trader" in str(check["remedy"])
    assert items(result, "engine facts")[-1] == (
        "does not hold: a claim kanso binds to — RuntimeError: engine gone"
    )


# -- the redacted report ---------------------------------------------------------------


def test_report_redacts_the_workspace_the_repository_and_the_home_directory(
    runner: CliRunner, monorepo: Path
) -> None:
    assert run(runner, "init", monorepo).exit_code == Exit.OK

    result = at(runner, monorepo, "doctor", "--report")

    assert result.exit_code == Exit.OK
    assert str(monorepo.resolve()) not in result.stdout
    assert str(Path.home()) not in result.stdout
    assert "<workspace>" in result.stdout
    assert "<repository>" in result.stdout


def test_report_redacts_the_json_diagnosis_too(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "doctor", "--report", "--json")

    document = payload(result)
    assert document["workspace"] == "<workspace>"
    assert str(workspace.resolve()) not in result.stdout


def test_report_carries_credential_names_and_no_values(runner: CliRunner, workspace: Path) -> None:
    _register(workspace)
    (workspace / ".env").write_text(f"KANSO_ACME_LABS_API_KEY={SECRET}\n", encoding="utf-8")

    result = at(runner, workspace, "doctor", "--report")

    assert "KANSO_ACME_LABS_API_KEY" in result.stdout
    assert SECRET not in result.stdout


def _workspace_object(root: Path) -> Any:
    from kanso.workspace import find

    return find(root)


# -- checks that grade a broken workspace ----------------------------------------------


def test_a_state_database_that_is_not_one_fails_and_the_rest_still_report(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / "state.db").write_bytes(b"not a database at all")

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert status(result, "schema") == "fail"
    assert status(result, "versions") == "ok"


def test_another_engine_version_warns_on_the_versions_and_the_facts(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine other than the one the facts were verified against is a warning, not a
    broken binding: the version is a fact about the facts, and grades no claim by itself."""
    monkeypatch.setattr(doctor_module.envelope_module, "engine_version", lambda: "0.0.1")
    monkeypatch.setattr(nautilus_trader, "__version__", "0.0.1")
    claims = len(facts.verify())

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "versions") == "warn"
    check = checks(result)["engine facts"]
    assert check["status"] == "warn"
    assert f"/{claims} claims hold on nautilus_trader 0.0.1" in str(check["detail"])
    assert "binding(s) broken" not in str(check["detail"])
    assert f"verified against {facts.ENGINE_VERSION}" in str(check["detail"])
    assert "re-verify" in str(check["remedy"])


def test_running_without_an_installed_distribution_is_a_warning(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = importlib.metadata.distribution

    def missing(name: str) -> object:
        if name == "kanso":
            raise importlib.metadata.PackageNotFoundError(name)
        return installed(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing)

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "install") == "warn"
    assert result.exit_code == Exit.OK


@pytest.mark.parametrize(
    ("recorded", "mode"),
    [
        (None, "package"),
        ("{ not json", "package"),
        ('{"url": "file:///src"}', "package"),
        ('{"url": "file:///src", "dir_info": {}}', "package"),
        ('{"url": "file:///src", "dir_info": {"editable": true}}', "editable"),
    ],
)
def test_the_install_mode_reads_what_the_installer_recorded(
    recorded: str | None, mode: str
) -> None:
    class Recorded:
        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return recorded

    assert doctor_module._install_mode(Recorded()) == mode  # type: ignore[arg-type]


def test_an_envelope_whose_timestamp_is_not_one_is_a_warning(
    runner: CliRunner, workspace: Path
) -> None:
    path = workspace / "envelope.yaml"
    line = next(
        ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("detected_at:")
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(line, "detected_at: yesterday"), encoding="utf-8"
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "envelope") == "warn"
    assert "not a timestamp" in str(checks(result)["envelope"]["detail"])


def test_a_workspace_with_no_gitignore_is_told_which_entries_it_needs(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / ".gitignore").unlink()

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "gitignore") == "warn"
    assert ".env" in items(result, "gitignore")
    assert "state.db" in items(result, "gitignore")


def test_a_workspace_that_configures_no_skills_target_has_nothing_to_link(
    runner: CliRunner, workspace: Path
) -> None:
    _configure(workspace, "targets = [", "targets = []  # ")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "skills") == "ok"
    assert "no [skills] targets" in str(checks(result)["skills"]["detail"])


def test_a_configuration_needing_no_credential_says_so(runner: CliRunner, workspace: Path) -> None:
    (workspace / "models.yaml").unlink()
    _configure(workspace, "# url =", 'url = "https://example.invalid/hook"  # ')

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "credentials") == "ok"
    assert items(result, "credentials") == []


def test_a_configured_adapter_nothing_registers_is_named(
    runner: CliRunner, workspace: Path
) -> None:
    path = workspace / "kanso.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[adapters.acme]\nbase_url = "https://acme"\n',
        encoding="utf-8",
    )

    result = at(runner, workspace, "doctor", "--json")

    assert any("acme" in item for item in items(result, "adapters"))


def test_an_extension_that_loads_is_listed_without_a_warning(
    runner: CliRunner, workspace: Path
) -> None:
    package = workspace / "kanso_ext" / "fine"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("PROVIDES = {'loaders': ['mine']}\n", encoding="utf-8")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "extensions") == "ok"
    assert items(result, "extensions") == ["fine: ok"]


def _configure(workspace: Path, old: str, new: str) -> None:
    """Rewrite one line of `kanso.toml`, so a test states the setting it changes."""
    path = workspace / "kanso.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_a_check_that_raises_is_itself_a_failed_check() -> None:
    """The diagnosis never stops at the first broken thing."""

    def kanso_failure() -> doctor_module.Check:
        raise PreconditionError("the store is elsewhere", remedy="move it back")

    def unexpected() -> doctor_module.Check:
        raise RuntimeError("boom")

    graded = doctor_module._guard("schema", kanso_failure)
    assert (graded.status, graded.detail, graded.remedy) == (
        "fail",
        "the store is elsewhere",
        "move it back",
    )
    assert doctor_module._guard("schema", unexpected).detail == "RuntimeError: boom"


def test_a_database_from_an_older_kanso_reports_the_migration_it_lacks(
    runner: CliRunner, workspace: Path
) -> None:
    assert at(runner, workspace, "migrate").exit_code == Exit.OK
    with StateStore(workspace / "state.db") as store:
        store.connection.execute("PRAGMA user_version = 0")

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "schema") == "warn"
    assert "0001_init.sql" in items(result, "schema")
    assert result.exit_code == Exit.OK


def test_a_packaged_skill_is_looked_up_under_its_prefixed_link_name(
    runner: CliRunner, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills sync` prefixes a skill directory that lacks the prefix; the check agrees."""
    skill = tmp_path / "extra"
    skill.mkdir()
    monkeypatch.setattr(doctor_module.skills_sync, "packaged_skills", lambda: [skill])
    for target in (".claude/skills", ".cursor/skills", ".codex/skills"):
        (workspace / target / "kanso-extra").symlink_to(skill, target_is_directory=True)

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "skills") == "ok"
    assert "3 links" in str(checks(result)["skills"]["detail"])


def test_a_state_database_ahead_of_the_package_fails_rather_than_reading_as_up_to_date(
    runner: CliRunner, fresh: Path
) -> None:
    """`pending` only looks upward, so without the other direction `doctor` would be the
    one command calling a workspace well that every other command refuses."""
    assert run(runner, "init", fresh).exit_code == Exit.OK
    with sqlite3.connect(fresh / "state.db") as conn:
        conn.execute("PRAGMA user_version = 99")

    result = at(runner, fresh, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    schema = next(c for c in payload(result)["checks"] if c["name"] == "schema")
    assert schema["status"] == "fail"
    assert "past the newest this package ships" in schema["detail"]


# -- the files against the records ----------------------------------------------------


def _remedy(result: Any, name: str) -> str:
    return str(checks(result)[name].get("remedy", ""))


def _best_sha(root: Path) -> str:
    with StateStore(root / "state.db") as store:
        row = store.connection.execute(
            "SELECT best_sha FROM hypotheses WHERE hyp_id = ?", (HYP_ID,)
        ).fetchone()
    return str(row["best_sha"])


def _certified_sha(root: Path) -> str:
    with StateStore(root / "state.db") as store:
        row = store.connection.execute("SELECT strategy_sha FROM certificates").fetchone()
    return str(row["strategy_sha"])


def _drop_blob(root: Path, sha: str) -> None:
    with sqlite3.connect(root / "state.db") as conn:
        conn.execute("DELETE FROM blobs WHERE sha = ?", (sha,))


def test_best_warns_and_never_fails_when_the_workspace_strategy_differs_from_best(
    runner: CliRunner, deployed: Path
) -> None:
    """Editing `hypotheses/<id>/strategy.py` is how an operator prepares `--from-workspace`,
    so the file kanso owns once `best` exists is graded against the best blob and warns."""
    path = deployed / "hypotheses" / HYP_ID / "strategy.py"
    best = _best_sha(deployed)
    before = at(runner, deployed, "doctor", "--json")
    assert status(before, "best") == "ok"
    assert items(before, "best") == [f"{HYP_ID}: strategy.py is best {best[:7]}"]

    path.write_text(path.read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")
    result = at(runner, deployed, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "best") == "warn"
    assert items(result, "best") == [f"{HYP_ID}: strategy.py is {_sha(path)[:7]} · best {best[:7]}"]
    assert f"kanso research begin {HYP_ID} --from-workspace" in _remedy(result, "best")
    assert f"kanso research show {HYP_ID} > hypotheses/{HYP_ID}/strategy.py" in _remedy(
        result, "best"
    )

    shown = at(runner, deployed, "research", "show", HYP_ID)
    path.write_text(shown.stdout, encoding="utf-8")
    assert status(at(runner, deployed, "doctor", "--json"), "best") == "ok"


def test_a_missing_workspace_strategy_is_the_same_warning(
    runner: CliRunner, deployed: Path
) -> None:
    (deployed / "hypotheses" / HYP_ID / "strategy.py").unlink()

    result = at(runner, deployed, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "best") == "warn"
    assert items(result, "best") == [
        f"{HYP_ID}: strategy.py is missing · best {_best_sha(deployed)[:7]}"
    ]


def test_certificates_fail_only_when_a_subject_s_bytes_are_held_nowhere(
    runner: CliRunner, deployed: Path
) -> None:
    """The bytes are held twice, as a blob and as `<sha7>.py` beside the certificate, so
    losing one is nothing; losing both stops every command that reads the subject."""
    sha = _certified_sha(deployed)
    beside = deployed / "certificates" / HYP_ID / f"{sha[:7]}.py"
    before = at(runner, deployed, "doctor", "--json")
    assert status(before, "certificates") == "ok"
    assert checks(before)["certificates"]["detail"] == (
        "1 certificate(s) · 1 version(s) · 1 subject(s) · every one held"
    )

    _drop_blob(deployed, sha)
    assert status(at(runner, deployed, "doctor", "--json"), "certificates") == "ok"

    beside.unlink()
    result = at(runner, deployed, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert status(result, "certificates") == "fail"
    [item] = items(result, "certificates")
    assert item.startswith(f"{HYP_ID} {sha[:7]}: certificate certificates/{HYP_ID}/{sha[:7]}-")
    assert f", {HYP_ID}@1 · no blob in state · no certificates/{HYP_ID}/{sha[:7]}.py" in item
    assert item.endswith(f" · bytes held by hypotheses/{HYP_ID}/strategy.py")
    assert _remedy(result, "certificates") == (
        f"copy hypotheses/{HYP_ID}/strategy.py to certificates/{HYP_ID}/{sha[:7]}.py, or "
        f"certify anew with `kanso cert run {HYP_ID}`"
    )

    shutil.copy(deployed / "hypotheses" / HYP_ID / "strategy.py", beside)
    assert status(at(runner, deployed, "doctor", "--json"), "certificates") == "ok"


def test_a_source_file_holding_other_bytes_is_no_copy_of_the_subject(
    runner: CliRunner, deployed: Path
) -> None:
    sha = _certified_sha(deployed)
    _drop_blob(deployed, sha)
    (deployed / "certificates" / HYP_ID / f"{sha[:7]}.py").write_text("# not it\n")
    (deployed / "hypotheses" / HYP_ID / "strategy.py").unlink()

    result = at(runner, deployed, "doctor", "--json")

    assert status(result, "certificates") == "fail"
    [item] = items(result, "certificates")
    assert f"certificates/{HYP_ID}/{sha[:7]}.py holds other bytes" in item
    assert item.endswith(
        f" · bytes held by strategies/{HYP_ID}/impl/1/kanso_impl_sleeve_{HYP_ID}_{sha[:12]}.py"
    )
    assert _remedy(result, "certificates").startswith(
        f"copy strategies/{HYP_ID}/impl/1/kanso_impl_sleeve_{HYP_ID}_{sha[:12]}.py to "
    )


def test_a_version_citing_bytes_held_nowhere_is_named_by_its_construct(
    runner: CliRunner, deployed: Path
) -> None:
    """A strategy version cites its sleeve and every attached construct by sha; each is a
    subject of its own, and one held nowhere in the workspace is reported by that name."""
    attached = [
        {"hyp_id": "demo_filter", "strategy_sha": "c" * 64, "construct": "filter", "params": {}}
    ]
    with sqlite3.connect(deployed / "state.db") as conn:
        conn.execute(
            "INSERT INTO strategy_versions (strategy_id, version, state, sleeve, attached, config,"
            " pins, expectation, created_at) SELECT strategy_id, 2, 'composed', sleeve, ?, config,"
            " pins, expectation, created_at FROM strategy_versions WHERE version = 1",
            (json.dumps(attached),),
        )

    result = at(runner, deployed, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "1 certificate(s) · 2 version(s) · 2 subject(s)" in str(
        checks(result)["certificates"]["detail"]
    )
    [item] = items(result, "certificates")
    assert item.startswith(f"demo_filter ccccccc: {HYP_ID}@2 filter · no blob in state · no ")
    assert "bytes held by" not in item
    assert _remedy(result, "certificates") == (
        "restore certificates/demo_filter/ccccccc.py from a copy of the workspace, or certify "
        "anew with `kanso cert run demo_filter`"
    )


def test_instruments_lists_the_operator_s_entries_and_resolves_every_universe(
    runner: CliRunner, registered: Path
) -> None:
    result = at(runner, registered, "doctor", "--json")

    assert status(result, "instruments") == "ok"
    assert checks(result)["instruments"]["detail"] == (
        "1 entry · 1 manual · 1 overridden · 1 universe id(s) across 1 hypothesis(es) · the "
        "store matches the newest snapshot"
    )
    assert items(result, "instruments") == [
        f"{INSTRUMENT}: manual · override currency, lot_size, price_increment",
        f"{HYP_ID} {INSTRUMENT}: manual, built · as of {RESEARCH[0]}",
    ]


def test_a_resolved_entry_answers_from_the_store_for_the_date_it_was_resolved_as_of(
    runner: CliRunner, registered: Path
) -> None:
    """A vendor-resolved entry needs no vendor again while the store holds the definition
    its `resolved` block names, as of the date the hypothesis researches from."""
    shown = payload(at(runner, registered, "data", "instruments", "show", "--json"))
    [held] = shown["instruments"]
    path = registered / "instruments.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "manual: true",
            "manual: false\n  resolved:\n    adapter: acme\n    as_of: 2024-01-02\n"
            f"    at: 2026-01-01T00:00:00+00:00\n    checksum: {held['checksum']}",
        ),
        encoding="utf-8",
    )

    result = at(runner, registered, "doctor", "--json")

    assert status(result, "instruments") == "ok"
    assert items(result, "instruments") == [
        f"{INSTRUMENT}: override currency, lot_size, price_increment · resolved by acme as of "
        "2024-01-02",
        f"{HYP_ID} {INSTRUMENT}: resolved by acme, in the store · as of {RESEARCH[0]}",
    ]


def test_a_manual_entry_the_engine_rejects_fails_by_name(
    runner: CliRunner, registered: Path
) -> None:
    path = registered / "instruments.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  override:\n", "  override:\n    nonsense: 1\n"),
        encoding="utf-8",
    )

    result = at(runner, registered, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert status(result, "instruments") == "fail"
    assert any(
        item.startswith(f"{HYP_ID} {INSTRUMENT}: {INSTRUMENT}: Equity has no field nonsense")
        for item in items(result, "instruments")
    )


def test_a_universe_id_the_registry_no_longer_names_fails(
    runner: CliRunner, registered: Path
) -> None:
    path = registered / "instruments.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(INSTRUMENT, "OTHER.SIM"))

    result = at(runner, registered, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert status(result, "instruments") == "fail"
    assert f"{HYP_ID} {INSTRUMENT}: unknown: no entry in instruments.yaml · as of" in " ".join(
        items(result, "instruments")
    )
    assert _remedy(result, "instruments") == (
        f"correct {INSTRUMENT} in instruments.yaml, then run `kanso data instruments resolve`"
    )


def test_an_id_only_a_reference_adapter_could_resolve_is_reported_and_never_fetched(
    runner: CliRunner, registered: Path
) -> None:
    """`doctor` makes no network call, so such an id is a warning naming the resolve to
    run — and a failure when no adapter is configured, since resolution would refuse."""
    path = registered / "instruments.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("manual: true", "manual: false"))

    refused = at(runner, registered, "doctor", "--json")
    assert status(refused, "instruments") == "fail"
    assert any(
        "no reference adapter is configured ([data] reference)" in item
        for item in items(refused, "instruments")
    )

    config = registered / "kanso.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[data]\nreference = "massive"\n', encoding="utf-8"
    )
    result = at(runner, registered, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "instruments") == "warn"
    assert f"{INSTRUMENT}: override currency, lot_size, price_increment · unresolved" in items(
        result, "instruments"
    )
    assert (
        f"{HYP_ID} {INSTRUMENT}: would resolve through massive, which doctor does not call · "
        f"as of {RESEARCH[0]}"
    ) in items(result, "instruments")
    assert _remedy(result, "instruments") == (
        f"run `kanso data instruments resolve {INSTRUMENT}` to resolve them"
    )


def test_a_resolution_since_the_newest_snapshot_is_drift_a_new_snapshot_clears(
    runner: CliRunner, registered: Path
) -> None:
    assert (
        at(runner, registered, "data", "instruments", "resolve", "--as-of", "2024-02-01").exit_code
        == Exit.OK
    )

    result = at(runner, registered, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "instruments") == "warn"
    assert "the store differs from what the newest snapshot pinned" in str(
        checks(result)["instruments"]["detail"]
    )
    assert any(
        item.startswith("store ") and "pinned" in item for item in items(result, "instruments")
    )
    assert _remedy(result, "instruments") == (
        "run `kanso data snapshot` to pin the definitions the store holds now"
    )

    assert at(runner, registered, "data", "snapshot").exit_code == Exit.OK
    assert status(at(runner, registered, "doctor", "--json"), "instruments") == "ok"


def test_instrument_drift_is_the_comparison_a_run_is_pinned_by(
    runner: CliRunner, registered: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`doctor` asks the comparison `research begin` pins a run by, and reports its answer.

    The comparison is stubbed both ways: a store that did move is reported as matching
    when the package's comparison says it does, and a store that did not move is reported
    as drifted, with the snapshot and both checksums the comparison named, when it says
    so. A comparison of doctor's own would answer from the files and disagree.
    """
    taken = newest(find(registered))
    assert taken is not None
    resolve = ("data", "instruments", "resolve", "--as-of", "2024-02-01")
    assert at(runner, registered, *resolve).exit_code == Exit.OK

    monkeypatch.setattr(doctor_module, "instrument_drift", lambda ws, snapshot: None)
    agreed = at(runner, registered, "doctor", "--json")

    assert status(agreed, "instruments") == "ok"
    assert "the store matches the newest snapshot" in str(checks(agreed)["instruments"]["detail"])

    assert at(runner, registered, "data", "snapshot").exit_code == Exit.OK
    stub = InstrumentDrift(snapshot_id=taken.snapshot_id, pinned="p" * 64, held="h" * 64)
    monkeypatch.setattr(doctor_module, "instrument_drift", lambda ws, snapshot: stub)
    moved = at(runner, registered, "doctor", "--json")

    assert status(moved, "instruments") == "warn"
    assert f"store {'h' * 12} · newest snapshot {taken.snapshot_id[:7]} pinned {'p' * 12}" in items(
        moved, "instruments"
    )
    assert _remedy(moved, "instruments") == (
        "run `kanso data snapshot` to pin the definitions the store holds now"
    )


def test_instruments_makes_no_catalog_where_there_is_none(
    runner: CliRunner, workspace: Path
) -> None:
    """A workspace that never loaded data has no `catalog/`, and diagnosing it creates none."""
    shutil.rmtree(workspace / "catalog", ignore_errors=True)

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert str(checks(result)["instruments"]["detail"]).endswith(" · no snapshot yet")
    assert not (workspace / "catalog").exists()


def test_lanes_grade_an_open_run_by_what_its_directory_holds(
    runner: CliRunner, registered: Path
) -> None:
    """The three scoped files as pinned are `ok`; a departure warns before the next card's
    `strategy_integrity` refuses it; a directory gone under the run fails, since no card
    can run there; ending the run clears all three."""
    assert at(runner, registered, "research", "begin", HYP_ID).exit_code == Exit.OK
    directory = lane(registered)
    relative = f"runs/op/{HYP_ID}"
    opened = at(runner, registered, "doctor", "--json")
    assert status(opened, "lanes") == "ok"
    assert items(opened, "lanes") == [f"op {HYP_ID}: {relative} holds the three scoped files"]

    (directory / "extra.txt").write_text("junk\n", encoding="utf-8")
    (directory / "program.md").unlink()
    departed = at(runner, registered, "doctor", "--json")
    assert departed.exit_code == Exit.OK
    assert status(departed, "lanes") == "warn"
    [item] = items(departed, "lanes")
    assert "'extra.txt' is not one of the three scoped files" in item
    assert "'program.md' is missing from the lane directory" in item
    assert _remedy(departed, "lanes").startswith(
        f"run `kanso research end {HYP_ID}` and begin again"
    )

    shutil.rmtree(directory)
    gone = at(runner, registered, "doctor", "--json")
    assert gone.exit_code == Exit.PRECONDITION
    assert status(gone, "lanes") == "fail"
    assert items(gone, "lanes") == [f"op {HYP_ID}: {relative} is gone"]
    assert _remedy(gone, "lanes").startswith(f"run `kanso research end {HYP_ID}`")

    assert at(runner, registered, "research", "end", HYP_ID).exit_code == Exit.OK
    ended = at(runner, registered, "doctor", "--json")
    assert status(ended, "lanes") == "ok"
    assert checks(ended)["lanes"]["detail"] == "0 open run(s) · 0 lane directories"


def test_a_lane_directory_with_no_open_run_behind_it_warns(
    runner: CliRunner, workspace: Path
) -> None:
    """Files beside the lanes — the daemon's log, a run's log — are not lane directories."""
    (workspace / "runs" / "l1" / "ghost").mkdir(parents=True)
    (workspace / "runs" / "daemon.log").write_text("", encoding="utf-8")
    (workspace / "runs" / "l1" / "ghost-20260906-1.jsonl").write_text("", encoding="utf-8")

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "lanes") == "warn"
    assert items(result, "lanes") == ["runs/l1/ghost: no open run behind it"]
    assert _remedy(result, "lanes") == (
        "`rm -r runs/l1/ghost`; a lane directory holds only copies of blobs in state"
    )


def test_the_record_checks_have_nothing_to_grade_without_a_state_database(
    runner: CliRunner, workspace: Path
) -> None:
    """An absent `state.db` is the schema check's warning; the checks that read records
    report that nothing is recorded, and never create the database they would read."""
    (workspace / "state.db").unlink()

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.OK
    for name in ("best", "certificates", "lanes"):
        assert status(result, name) == "ok"
        assert checks(result)[name]["detail"] == "nothing recorded yet · no state.db"
    assert "universes not checked: no state.db" in items(result, "instruments")
    assert not (workspace / "state.db").exists()


def test_the_record_checks_are_not_graded_against_a_database_this_package_cannot_read(
    runner: CliRunner, workspace: Path
) -> None:
    with sqlite3.connect(workspace / "state.db") as conn:
        conn.execute("PRAGMA user_version = 0")
    behind = at(runner, workspace, "doctor", "--json")
    for name in ("best", "certificates", "lanes"):
        assert status(behind, name) == "warn"
        assert checks(behind)[name]["detail"] == "not checked: state.db is 2 migration(s) behind"
        assert _remedy(behind, name) == "run `kanso migrate`"
    assert "universes not checked: state.db is 2 migration(s) behind" in items(
        behind, "instruments"
    )

    (workspace / "state.db").write_bytes(b"not a database at all")
    broken = at(runner, workspace, "doctor", "--json")
    assert status(broken, "schema") == "fail"
    for name in ("best", "certificates", "lanes"):
        assert status(broken, name) == "warn"
        assert str(checks(broken)[name]["detail"]).startswith(
            "not checked: state.db could not be opened"
        )


def test_a_store_that_raises_anything_is_graded_once_and_never_stops_the_diagnosis(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record checks share one reading of the store, taken outside their guard, so a
    store that raises something neither kanso nor sqlite names must still be a graded
    finding — the schema check's failure — and the five say `not checked` rather than dying."""

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(doctor_module, "StateStore", boom)

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert status(result, "schema") == "fail"
    assert checks(result)["schema"]["detail"] == "RuntimeError: the disk went away"
    for name in ("best", "certificates", "record", "lanes"):
        assert status(result, name) == "warn"
        assert checks(result)[name]["detail"] == (
            "not checked: state.db could not be opened: the disk went away"
        )
    assert "universes not checked: state.db could not be opened: the disk went away" in items(
        result, "instruments"
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_certified_files_the_record_has_no_memory_of_are_named_as_a_fresh_start(
    runner: CliRunner, deployed: Path
) -> None:
    """A clone carries the files and not `state.db`, by design: `doctor` says so once,
    warns rather than fails, and names what did not travel and what re-establishes it."""
    before = at(runner, deployed, "doctor", "--json")
    record = next(c for c in payload(before)["checks"] if c["name"] == "record")
    assert record["status"] == "ok"

    (deployed / "state.db").unlink()
    for suffix in ("-journal", "-wal", "-shm"):
        (deployed / f"state.db{suffix}").unlink(missing_ok=True)
    assert at(runner, deployed, "migrate").exit_code == Exit.OK  # a fresh, empty record

    after = at(runner, deployed, "doctor", "--json")
    record = next(c for c in payload(after)["checks"] if c["name"] == "record")
    assert record["status"] == "warn"
    assert any("certificate(s) on disk" in item for item in record["items"])
    assert any("version(s) on disk" in item for item in record["items"])
    assert "did not travel" in record["remedy"]
    assert "--as NAME" in record["remedy"], "approvals are re-made by a person, never carried"
