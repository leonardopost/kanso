"""`kanso doctor`'s `order rate` check: a broker's quota against what a stage's sleeves send.

A sleeve holds itself to the `costs.orders_per_minute` its version was certified under, and
a broker's client holds every request of the account to `requests_per_minute`. Where the
second is below the sum of the first over a stage, the client would hold back what the cards
sent at once, and the check warns; where the sum cannot be known, it says what it checks.
Nothing here opens a socket or resolves a credential: the stage only names a broker's paper
client, which is all the check reads of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.schemas import StrategyFile, resolve_venue_model, write_yaml
from kanso.schemas.venue import CostsOverride
from kanso.strategy.files import strategy_file
from kanso.workspace import find

from .conftest import at, payload

PAPER = "alpaca_paper"
"""A broker's paper client; the check reads its quota from the broker's own table."""


def order_rate(runner: CliRunner, root: Path) -> dict[str, Any]:
    """The `order rate` check of a `--json` diagnosis."""
    result = at(runner, root, "doctor", "--json")
    assert result.exit_code in (Exit.OK, Exit.PRECONDITION), result.stdout
    found = [check for check in payload(result)["checks"] if check["name"] == "order rate"]
    assert len(found) == 1
    return dict(found[0])


def composed(root: Path, strategy_id: str, per_minute: int | None) -> None:
    """A version 1 of `strategy_id`, certified under a venue model stating `per_minute`."""
    costs = CostsOverride(spread="fixed_bps", fixed_bps=1.0, orders_per_minute=per_minute)
    model = resolve_venue_model("XNAS", hypothesis_costs=costs, max_leverage=1.0)
    version = {
        "version": 1,
        "sleeve": {"hyp_id": strategy_id, "strategy_sha": "a" * 64},
        "pins": {
            "kanso_version": "0.20.0",
            "nautilus_version": "1.231.0",
            "criteria_version": "0.20.0",
            "plan_version": 1,
            "snapshot_id": "s" * 12,
            "venue_model": model.model_dump(),
        },
        "expectation": {
            "objective_id": "sharpe",
            "value": 1.0,
            "ci90": [0.5, 1.5],
            "mdd_p95": 0.1,
            "window": {"start": "2024-02-01", "end": "2024-02-29"},
        },
        "state": "paper",
        "created_at": datetime(2024, 3, 1, tzinfo=UTC),
    }
    made = StrategyFile.model_validate({"schema": 1, "id": strategy_id, "versions": [version]})
    path = strategy_file(find(root), strategy_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(made, path)


def staged(root: Path, *strategy_ids: str, exec_: str = PAPER) -> None:
    """The paper stage on `exec_`, holding version 1 of each of `strategy_ids`."""
    path = root / "portfolio.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["stages"]["paper"]["exec"] = exec_
    document["stages"]["paper"]["strategies"] = [
        {"id": name, "version": 1, "capital": 1_000.0, "joined_at": "2024-03-01T00:00:00Z"}
        for name in strategy_ids
    ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def quota(root: Path, per_minute: int) -> None:
    path = root / "kanso.toml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"\n[adapters.alpaca]\nrequests_per_minute = {per_minute}\n",
        encoding="utf-8",
    )


def test_a_fresh_workspace_says_what_it_checks_and_finds_no_quota_to_check(
    runner: CliRunner, workspace: Path
) -> None:
    check = order_rate(runner, workspace)

    assert check["status"] == "ok"
    assert "requests_per_minute" in check["detail"] and "orders_per_minute" in check["detail"]
    assert check["items"] == [
        "paper: sandbox holds its requests to no quota kanso reads",
        "live: sandbox holds its requests to no quota kanso reads",
    ]


def test_a_quota_below_what_the_stage_s_sleeves_may_send_warns(
    runner: CliRunner, workspace: Path
) -> None:
    """The broker's default quota stays under its published 200 a minute, so two versions
    certified at 200 and 166 between them may send more than the client lets through."""
    composed(workspace, "fast_one", 200)
    composed(workspace, "fast_two", 166)
    staged(workspace, "fast_one", "fast_two")

    check = order_rate(runner, workspace)

    assert check["status"] == "warn"
    assert check["detail"].startswith("paper (190 < 366)")
    assert (
        "paper: [adapters.alpaca] requests_per_minute 190 against 366 a minute between 2 "
        in (check["items"][0])
    )
    assert "requests_per_minute" in check["remedy"]


def test_a_quota_the_stage_s_sleeves_fit_inside_is_ok(runner: CliRunner, workspace: Path) -> None:
    composed(workspace, "fast_one", 200)
    composed(workspace, "fast_two", 166)
    staged(workspace, "fast_one", "fast_two")
    quota(workspace, 1_000)

    check = order_rate(runner, workspace)

    assert check["status"] == "ok"
    assert check["items"][0] == (
        "paper: [adapters.alpaca] requests_per_minute 1000 against 366 a minute between "
        "2 version(s)"
    )


def test_a_version_that_states_no_rate_leaves_the_sum_unknown_and_unjudged(
    runner: CliRunner, workspace: Path
) -> None:
    """A version certified under no rate may send any number; one the workspace has not
    composed states none it can read. Either leaves the sum unbounded, and the check says so
    rather than warning on a number it does not have."""
    composed(workspace, "fast_one", 200)
    composed(workspace, "unheld", None)
    staged(workspace, "fast_one", "unheld", "nowhere")

    check = order_rate(runner, workspace)

    assert check["status"] == "ok"
    assert check["items"][0] == (
        "paper: [adapters.alpaca] requests_per_minute 190 · unheld@1, nowhere@1 state(s) no "
        "orders_per_minute, so what the sleeves may send between them is unbounded and not "
        "compared"
    )


def test_a_broker_stage_with_nothing_deployed_has_nothing_to_compare(
    runner: CliRunner, workspace: Path
) -> None:
    staged(workspace)

    check = order_rate(runner, workspace)

    assert check["status"] == "ok"
    assert (
        check["items"][0]
        == "paper: [adapters.alpaca] requests_per_minute 190 · no version deployed"
    )


def test_a_workspace_with_no_portfolio_has_no_stage_to_check(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / "portfolio.yaml").unlink()

    check = order_rate(runner, workspace)

    assert check["status"] == "ok"
    assert check["detail"].startswith("no portfolio.yaml, so no stage to check")
