"""`kanso doctor`: the diagnosis, its grades, its exit code and its redacted report."""

from __future__ import annotations

import importlib.metadata
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kanso import env
from kanso.cli import doctor as doctor_module
from kanso.errors import Exit, PreconditionError
from kanso.skills_sync import packaged_skills
from kanso.state import StateStore

from ..data.adapters.massive import Replay, refused
from .conftest import at, payload, run

CHECKS = (
    "versions",
    "install",
    "engine wheel",
    "schema",
    "envelope",
    "repository",
    "gitignore",
    "skills",
    "credentials",
    "adapters",
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
    (package / "__init__.py").write_text("PROVIDES = {'gates': ['mine']}\n", encoding="utf-8")
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
    (package / "__init__.py").write_text(
        "PROVIDES = {'loaders': ['synthetic'], 'adapters': ['massive']}\n", encoding="utf-8"
    )

    result = at(runner, workspace, "doctor", "--json")

    assert status(result, "extensions") == "warn"
    listed = items(result, "extensions")
    assert "greedy shadows the built-in loaders 'synthetic'" in listed
    assert "greedy shadows the built-in adapters 'massive'" in listed


def test_doctor_makes_no_network_call(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("doctor opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    assert at(runner, workspace, "doctor").exit_code == Exit.OK
    assert at(runner, workspace, "doctor", "--check-adapters").exit_code == Exit.OK


def test_engine_facts_report_the_claims_that_do_not_hold(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "doctor", "--json")

    check = checks(result)["engine facts"]
    assert "claims hold on nautilus_trader" in str(check["detail"])
    assert all(item.startswith("does not hold: ") for item in items(result, "engine facts"))


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
    monkeypatch.setattr(doctor_module.envelope_module, "engine_version", lambda: "0.0.1")

    result = at(runner, workspace, "doctor", "--json")

    assert result.exit_code == Exit.OK
    assert status(result, "versions") == "warn"
    assert status(result, "engine facts") == "warn"


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
    (package / "__init__.py").write_text("PROVIDES = {'gates': ['mine']}\n", encoding="utf-8")

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
