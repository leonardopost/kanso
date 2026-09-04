"""`kanso models check`: the register as the router reads it, and whether it answers.

The command has to be readable before it is useful — an operator runs it to find out
which half of a register works — so the object it prints carries the routing table as
well as the result of every call, and a model that fails is reported rather than raised.
The exit code is the summary: 0 when every model answered, 2 when any did not.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.models import spend
from kanso.state import StateStore

from . import mocked
from .conftest import at, payload


def test_check_prints_the_routing_and_one_result_per_model(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "models", "check", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    checked = payload(result)
    assert checked["ok"] is True
    assert set(checked["routing"]) == {"classify", "propose", "align_check", "certify_plan"}
    assert checked["routing"]["classify"]["tier"] == "frontier"
    assert checked["routing"]["align_check"]["effort"] == "none"
    assert [entry["id"] for entry in checked["models"]] == list(mocked.TIER_MODELS.values())
    assert all(entry["ok"] for entry in checked["models"])


def test_every_check_call_is_ledgered_like_any_other(runner: CliRunner, mocked_ws: Path) -> None:
    with StateStore(mocked_ws / "state.db") as store:
        before = spend(store).calls

    assert at(runner, mocked_ws, "models", "check").exit_code == Exit.OK

    with StateStore(mocked_ws / "state.db") as store:
        assert spend(store).calls - before == len(mocked.TIER_MODELS)


def test_a_model_that_does_not_answer_is_reported_and_exits_two(
    runner: CliRunner, mocked_ws: Path
) -> None:
    # A script that is not there is a model that cannot answer, in the mock protocol.
    document = yaml.safe_load((mocked_ws / "models.yaml").read_text(encoding="utf-8"))
    document["models"][0]["script"] = "mock/absent.yaml"
    (mocked_ws / "models.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    result = at(runner, mocked_ws, "models", "check", "--json")

    assert result.exit_code == Exit.PRECONDITION
    checked = payload(result)
    assert checked["ok"] is False
    failed = [entry for entry in checked["models"] if not entry["ok"]]
    assert len(failed) == 1
    # The rest of the register was still checked, which is the point of the command.
    assert len(checked["models"]) == len(mocked.TIER_MODELS)


def test_check_reads_as_the_routing_then_the_models(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "models", "check")

    assert result.exit_code == Exit.OK
    assert "classify" in result.stdout
    assert "frontier" in result.stdout
    assert f"{len(mocked.TIER_MODELS)}/{len(mocked.TIER_MODELS)} answered" in result.stdout


def test_a_register_missing_a_tier_is_refused_before_any_call(
    runner: CliRunner, mocked_ws: Path
) -> None:
    document = yaml.safe_load((mocked_ws / "models.yaml").read_text(encoding="utf-8"))
    document["models"] = [entry for entry in document["models"] if entry["tier"] != "frontier"]
    (mocked_ws / "models.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    result = at(runner, mocked_ws, "models", "check", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "frontier" in payload(result)["error"]
