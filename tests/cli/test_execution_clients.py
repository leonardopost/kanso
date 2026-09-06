"""The execution clients from the shell: what is offered, what is refused, and what leaks.

This is the milestone's safety surface, driven the way an operator drives it. Three claims
are under test.

**A broker's clients are offered without being configured.** They are discovered from the
adapter directory, so `portfolio clients` and `doctor` answer completely in a workspace with
every broker variable unset — which is the ordinary state of a fresh one, and the state the
whole suite runs in. Nothing here resolves a value, opens a socket or needs a key.

**Every refusal `deploy` makes is reachable and carries its contracted code.** Real capital
off the live stage and real capital with no recorded approval are exit 4, because both are a
missing act. A wall-clock client fed replayed data, run at any speed but one, or handed to a
stage node that would fill its orders in simulation are exit 2, because all three are broken
preconditions. The clients are read out of the registry by what they declare rather than
named, so a second broker is covered by the same tests without an edit.

**No credential reaches anything.** The last test sets made-up values under kanso's own
variable names — in the process environment and in the workspace `.env`, which is exactly
how a real one would arrive — drives every command that touches the broker, and asserts that
neither value appears in any output, any error, any file the workspace holds or the state
database. The values are strings this module invented; nothing here reaches a network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.nautilus import adapters

from .conftest import HYP_ID, at, payload, portfolio_document, reconfigure

BROKER_PAPER = "broker_paper"
REAL = "real"
WALL = "wall"

KEY = "PKTESTKEYIDNOTAREALONE"
SECRET = "test-secret-that-is-not-a-real-one-0123456789"
"""Two strings this module made up. A paper key carries the prefix a real one does not, so
the first is shaped like the account the paper client opens — which is what makes the
adapter accept it far enough to matter, without it being anyone's key."""


def client_of(capital: str) -> str:
    """One packaged broker client id, chosen by what it declares rather than by name."""
    found = sorted(
        client_id
        for client_id, spec in adapters.exec_clients().items()
        if spec.capital == capital and spec.clock == WALL
    )
    assert found, f"no packaged broker declares {capital} on a wall clock"
    return found[0]


@pytest.fixture
def paper_client() -> str:
    return client_of(BROKER_PAPER)


@pytest.fixture
def real_client() -> str:
    return client_of(REAL)


@pytest.fixture
def funded(deployed: Path) -> Path:
    """That workspace with the live stage funded, which is what a promotion needs."""
    reconfigure(deployed, "live", capital=100_000)
    return deployed


def clients(runner: CliRunner, root: Path) -> dict[str, dict[str, Any]]:
    """`portfolio clients --json`, by client id."""
    document = payload(at(runner, root, "portfolio", "clients", "--json"))
    listed = document["clients"]
    assert isinstance(listed, list)
    return {str(one["id"]): one for one in listed}


# -- what is offered, with nothing configured ---------------------------------------


def test_every_packaged_broker_client_is_listed_in_a_workspace_with_nothing_set(
    runner: CliRunner, workspace: Path
) -> None:
    """An adapter is enabled by its credentials, never by installation."""
    listed = clients(runner, workspace)

    assert set(listed) >= {"sandbox", *adapters.exec_clients()}
    assert listed["sandbox"]["credentials"] == []
    assert listed["sandbox"]["credentials_resolve"] is True


def test_a_broker_client_declares_its_variables_and_resolves_none_of_them(
    runner: CliRunner, workspace: Path, paper_client: str
) -> None:
    one = clients(runner, workspace)[paper_client]

    assert one["credentials"], "a broker account is opened with named variables"
    assert one["credentials_resolve"] is False
    assert set(one["credential_origins"].values()) == {None}
    assert all(name.startswith("KANSO_") for name in one["credentials"])


def test_real_capital_may_be_configured_on_one_stage_and_paper_money_on_both(
    runner: CliRunner, workspace: Path, paper_client: str, real_client: str
) -> None:
    """The declaration that keeps real capital off the paper stage, reported before a deploy."""
    listed = clients(runner, workspace)

    assert listed[real_client]["stages"] == ["live"]
    assert listed[real_client]["capital"] == REAL
    assert listed[paper_client]["stages"] == ["paper", "live"]
    assert listed["sandbox"]["stages"] == ["paper", "live"]


def test_the_client_listing_reports_which_stage_would_be_refused_and_why(
    runner: CliRunner, workspace: Path, paper_client: str
) -> None:
    reconfigure(workspace, "paper", exec=paper_client)

    document = payload(at(runner, workspace, "portfolio", "clients", "--json"))

    assert document["stages"]["live"] is None
    assert "live data client" in str(document["stages"]["paper"])


def test_doctor_lists_the_execution_clients_and_grades_a_clean_stage_green(
    runner: CliRunner, workspace: Path, paper_client: str
) -> None:
    document = payload(at(runner, workspace, "doctor", "--json"))
    execution = next(one for one in document["checks"] if one["name"] == "execution")

    assert execution["status"] == "ok"
    assert any(paper_client in item for item in execution["items"])
    assert any(item.startswith("paper: ok") for item in execution["items"])


def test_doctor_fails_a_stage_whose_configuration_deploy_would_refuse(
    runner: CliRunner, workspace: Path, paper_client: str
) -> None:
    """The refusal is `deploy`'s own, called rather than restated, so the two agree."""
    reconfigure(workspace, "paper", exec=paper_client)

    result = at(runner, workspace, "doctor", "--json")
    execution = next(one for one in payload(result)["checks"] if one["name"] == "execution")

    assert result.exit_code == Exit.PRECONDITION
    assert execution["status"] == "fail"
    assert "paper" in execution["detail"]


def test_a_broker_table_in_the_configuration_is_not_reported_as_unprovided(
    runner: CliRunner, workspace: Path
) -> None:
    """A broker is configured through the same table a data adapter is, in its own registry."""
    broker = sorted(adapters.packaged())[0]
    path = workspace / "kanso.toml"
    path.write_text(f"{path.read_text(encoding='utf-8')}\n[adapters.{broker}]\n", encoding="utf-8")

    document = payload(at(runner, workspace, "doctor", "--json"))
    adapters_check = next(one for one in document["checks"] if one["name"] == "adapters")

    assert not any(
        "nothing registered here provides it" in item for item in adapters_check["items"]
    )


# -- every refusal `deploy` makes ---------------------------------------------------


def test_real_capital_off_the_live_stage_is_exit_four(
    runner: CliRunner, deployed: Path, real_client: str
) -> None:
    reconfigure(deployed, "paper", exec=real_client, data=real_client)

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.APPROVAL
    assert "only on the live stage" in payload(result)["error"]


def test_real_capital_on_the_live_stage_without_an_approval_is_exit_four(
    runner: CliRunner, funded: Path, real_client: str
) -> None:
    """The file says the version is live; only the record says it may be.

    The version is moved onto the live stage by editing the files, which is the act the
    approval record exists to defeat.
    """
    reconfigure(funded, "live", exec=real_client, data=real_client)
    _onto_live_by_hand(funded)

    result = at(runner, funded, "portfolio", "deploy", "--stage", "live", "--json")

    assert result.exit_code == Exit.APPROVAL
    assert "operator approval" in payload(result)["error"]
    assert "--live --as NAME" in str(payload(result)["remedy"])


def test_a_wall_clock_client_fed_the_catalog_replay_is_exit_two(
    runner: CliRunner, deployed: Path, paper_client: str
) -> None:
    reconfigure(deployed, "paper", exec=paper_client, data="replay")

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "live data client" in payload(result)["error"]


def test_a_wall_clock_client_run_at_any_speed_but_one_is_exit_two(
    runner: CliRunner, deployed: Path, paper_client: str
) -> None:
    reconfigure(deployed, "paper", exec=paper_client, data=paper_client, speed=0)

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "speed 1" in payload(result)["error"]


def test_a_wall_clock_client_this_versions_node_cannot_run_is_exit_two(
    runner: CliRunner, deployed: Path, paper_client: str
) -> None:
    """The last refusal, and the one this milestone exists for.

    A stage node here is a bounded replay of the catalog into kanso's own simulated venue.
    Running a broker's client through it would fill every order in simulation while the
    stage record — and the paper and live gates reading it — called the money the broker's.
    """
    reconfigure(deployed, "paper", exec=paper_client, data=paper_client, speed=1)

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "simulated venue" in payload(result)["error"]
    assert portfolio_document(deployed)["stages"]["paper"]["exec"] == paper_client


def test_an_execution_client_nothing_provides_is_refused_by_name(
    runner: CliRunner, deployed: Path
) -> None:
    reconfigure(deployed, "paper", exec="nobodys_broker", data="nobodys_broker")

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "is not an execution client of this workspace" in payload(result)["error"]
    assert "sandbox" in payload(result)["error"]


# -- promotion is the only path to real capital -------------------------------------


def test_a_promotion_is_what_gets_a_real_capital_client_past_the_approval(
    runner: CliRunner, funded: Path, real_client: str
) -> None:
    """The one act that changes the answer, shown by the refusal that moves.

    A version put on the live stage by editing the files is refused for the approval it has
    not got — that is the test above. This one takes the same version there through
    `promote --live --as NAME` and finds the live stage refused for the node it would have
    run in instead: a precondition of this version, not a missing act. The approval is the
    only thing that differs between the two, which is the whole of what it is for.
    """
    _promotable(runner, funded)
    reconfigure(funded, "live", exec=real_client, data=real_client)

    approved = at(runner, funded, "promote", HYP_ID, "--live", "--as", "Ada Lovelace", "--json")
    after = at(runner, funded, "portfolio", "deploy", "--stage", "live", "--json")

    assert approved.exit_code == Exit.PRECONDITION, "the promotion redeploys, and is refused too"
    assert "simulated venue" in payload(approved)["error"]
    assert after.exit_code == Exit.PRECONDITION
    assert "simulated venue" in payload(after)["error"]
    assert "operator approval" not in payload(after)["error"]
    state = payload(at(runner, funded, "strat", "show", f"{HYP_ID}@1", "--json"))["state"]
    assert state == "live", "the approval moved the version even though the node refused it"


def test_a_promotion_without_a_named_operator_moves_nothing(
    runner: CliRunner, funded: Path, real_client: str
) -> None:
    """There is no environment fallback and no other way in: `--as NAME` or nothing."""
    _promotable(runner, funded)
    reconfigure(funded, "live", exec=real_client, data=real_client)
    before = portfolio_document(funded)

    result = at(runner, funded, "promote", HYP_ID, "--live", "--json")

    assert result.exit_code == Exit.APPROVAL
    assert portfolio_document(funded) == before, "nothing moved"


# -- the credential-leak scan -------------------------------------------------------


def test_no_credential_reaches_an_output_an_error_a_file_or_the_state_database(
    runner: CliRunner, deployed: Path, paper_client: str, real_client: str
) -> None:
    """The scan the milestone asks for, over every surface a value could reach.

    The values are set the way an operator's arrive — in the workspace `.env`, which is
    where kanso looks first — and then every command that touches the broker is driven,
    including the ones that refuse. The stages are put back on the simulated client
    afterwards and a deployment and a replay are run, so that a session, a manifest and the
    state rows this scan reads were all *written while a credential was resolvable* rather
    than before one existed. What is asserted is that neither string appears in any output,
    any error, any file the workspace holds afterwards, or the state database, with the
    `.env` an operator wrote excepted because that is where they put them.
    """
    names = [
        name
        for client in (paper_client, real_client)
        for name in _broker_of(client).credentials(client)
    ]
    _write_env(deployed, {name: KEY if name.endswith("KEY") else SECRET for name in names})
    reconfigure(deployed, "paper", exec=paper_client, data=paper_client, speed=1)
    reconfigure(deployed, "live", exec=real_client, data=real_client, capital=100_000)

    refused = "\n".join(
        _both(at(runner, deployed, *command))
        for command in (
            ("doctor", "--json"),
            ("doctor", "--report"),
            ("portfolio", "clients", "--json"),
            ("portfolio", "show", "--json"),
            ("portfolio", "deploy", "--stage", "paper", "--json"),
            ("portfolio", "deploy", "--stage", "live", "--json"),
            ("data", "adapters", "--json"),
            ("status", "--json"),
            ("inbox", "--json"),
        )
    )
    reconfigure(deployed, "paper", exec="sandbox", data="replay", speed=1)
    reconfigure(deployed, "live", exec="sandbox", data="replay")
    ran = [
        at(runner, deployed, *command)
        for command in (
            ("portfolio", "deploy", "--stage", "paper", "--json"),
            ("replay", "run", "--strategy", HYP_ID, "--json"),
            ("replay", "show", "--json"),
        )
    ]
    printed = "\n".join([refused, *(_both(one) for one in ran)])

    assert names, "the broker declares variables to leak in the first place"
    assert _holds_a_secret(deployed / ".env"), "the scan can find a value where one really is"
    assert [one.exit_code for one in ran] == [Exit.OK] * len(ran), "a session was really written"
    assert KEY not in printed
    assert SECRET not in printed
    leaked = sorted(
        str(path.relative_to(deployed))
        for path in deployed.rglob("*")
        if path.is_file() and path.name != ".env" and _holds_a_secret(path)
    )
    assert leaked == []


def test_the_credentials_object_never_reprs_its_values(deployed: Path, paper_client: str) -> None:
    """A repr reaches a traceback, a log and a crash report, so it carries neither value."""
    _write_env(
        deployed,
        {
            name: KEY if name.endswith("KEY") else SECRET
            for name in _broker_of(paper_client).credentials(paper_client)
        },
    )
    from kanso.workspace import find

    opened = _broker_of(paper_client).open(find(deployed), paper_client)  # type: ignore[attr-defined]

    assert KEY not in repr(opened)
    assert SECRET not in repr(opened)
    assert paper_client in repr(opened)
    assert opened.headers()[next(iter(opened.headers()))] in {KEY, SECRET}


# -- helpers ------------------------------------------------------------------------


def _broker_of(client_id: str) -> Any:
    broker = adapters.broker_of(client_id)
    assert broker is not None
    return broker


def _write_env(root: Path, values: dict[str, str]) -> None:
    """The workspace `.env`, written the way an operator writes one."""
    (root / ".env").write_text(
        "".join(f"{name}={value}\n" for name, value in sorted(values.items())), encoding="utf-8"
    )


def _both(result: Any) -> str:
    """Everything a command printed, on both streams."""
    return f"{result.stdout}\n{getattr(result, 'stderr', '')}"


def _holds_a_secret(path: Path) -> bool:
    """Whether a file — text or binary, the state database included — holds either value."""
    blob = path.read_bytes()
    return KEY.encode() in blob or SECRET.encode() in blob


def _onto_live_by_hand(root: Path) -> None:
    """Move the composed version onto the live stage by editing the files, approving nothing."""
    import yaml

    document = portfolio_document(root)
    paper = document["stages"]["paper"]["strategies"]
    assert paper, "the paper stage holds the version this moves"
    document["stages"]["live"]["strategies"] = paper
    document["stages"]["paper"]["strategies"] = []
    (root / "portfolio.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    path = root / "strategies" / HYP_ID / "strategy.yaml"
    strategy = yaml.safe_load(path.read_text(encoding="utf-8"))
    strategy["versions"][0]["state"] = "live"
    path.write_text(yaml.safe_dump(strategy, sort_keys=False), encoding="utf-8")


def _promotable(runner: CliRunner, root: Path) -> None:
    """The paper version moved to `promotable`, which is what a monitor pass does."""
    outcome = payload(at(runner, root, "monitor", "run", "--json"))
    assert "promotable" in outcome["actions"], outcome
