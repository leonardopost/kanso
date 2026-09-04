"""`kanso strat`: composing a certificate into a version, reading one, and ending one.

The composition itself is proved in its own slice; what is asserted here is the command
around it — the arguments, the `STRATEGY[@V]` notation every later command shares, the exit
codes, and the one JSON object an agent branches on. The workspace these run against is one
whose sleeve is already certified, because that is the only state in which any of them has
anything to say.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import HYP_ID, at, payload


def test_compose_returns_the_version_certification_already_made(
    runner: CliRunner, deployed: Path
) -> None:
    """Certification composes on its own, so the command is the hand-driven form of it."""
    result = at(runner, deployed, "strat", "compose", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    version = payload(result)
    assert version["strategy"] == HYP_ID
    assert version["version"] == 1
    assert version["sleeve"]["hyp_id"] == HYP_ID
    assert Path(version["impl"]).is_dir(), "the version's implementation is on disk"
    assert Path(version["path"]).is_file()
    listed = payload(at(runner, deployed, "strat", "show", "--json"))
    assert [one["id"] for one in listed["strategies"]] == [HYP_ID]
    assert len(listed["strategies"][0]["versions"]) == 1, "composing twice makes one version"


def test_compose_reads_as_the_version_and_where_it_was_written(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "compose", HYP_ID)

    assert result.exit_code == Exit.OK
    assert f"{HYP_ID}@1" in result.stdout
    assert "sleeve" in result.stdout
    assert "impl" in result.stdout


def test_compose_refuses_a_hypothesis_with_no_passing_certificate(
    runner: CliRunner, registered: Path
) -> None:
    result = at(runner, registered, "strat", "compose", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no passing certificate" in payload(result)["error"]


def test_show_with_no_argument_lists_every_composed_strategy(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "show")

    assert result.exit_code == Exit.OK
    assert "1 composed" in result.stdout
    assert f"{HYP_ID}@1" in result.stdout
    assert "paper" in result.stdout, "the state a version is in is on its line"


def test_show_of_a_strategy_is_every_version_it_has(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "strat", "show", HYP_ID, "--json")

    document = payload(result)
    assert document["id"] == HYP_ID
    assert [one["version"] for one in document["versions"]] == [1]
    assert document["versions"][0]["expectation"]["objective_id"] == "net_edge_bps"


def test_show_of_one_version_carries_its_band_and_its_pins(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "show", f"{HYP_ID}@1", "--json")

    version = payload(result)
    assert version["version"] == 1
    assert version["pins"]["nautilus_version"]
    low, high = version["expectation"]["ci90"]
    assert low <= version["expectation"]["value"] <= high


def test_a_version_the_strategy_does_not_have_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "strat", "show", f"{HYP_ID}@9", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "has no version 9" in payload(result)["error"]


def test_a_target_that_is_not_a_version_number_is_refused(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "show", f"{HYP_ID}@latest", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "not a version number" in payload(result)["error"]


def test_a_target_naming_no_strategy_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "strat", "show", "@1", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "names no strategy" in payload(result)["error"]


def test_a_strategy_this_workspace_never_composed_is_refused(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "show", "ghost", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "not a composed strategy" in payload(result)["error"]


def test_retire_takes_the_version_off_its_stage_and_restarts_what_runs(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "retire", f"{HYP_ID}@1", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    retired = payload(result)
    assert retired["state"] == "retired"
    assert retired["was"] == "paper"
    assert retired["left"] == ["paper"]
    assert set(retired["redeployed"]) == {"paper", "live"}
    portfolio = payload(at(runner, deployed, "portfolio", "show", "--json"))
    paper = next(one for one in portfolio["stages"] if one["stage"] == "paper")
    assert paper["strategies"] == [], "a retired version holds no capital"


def test_retiring_the_same_version_twice_is_refused(runner: CliRunner, deployed: Path) -> None:
    assert at(runner, deployed, "strat", "retire", HYP_ID).exit_code == Exit.OK

    result = at(runner, deployed, "strat", "retire", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "already retired" in payload(result)["error"]


def test_retire_reads_as_what_left_and_what_was_restarted(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "strat", "retire", HYP_ID)

    assert result.exit_code == Exit.OK
    assert f"{HYP_ID}@1" in result.stdout
    assert "paper" in result.stdout
