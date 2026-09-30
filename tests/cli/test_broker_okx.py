"""The OKX broker from the shell: listed, refused at deploy, and read by `doctor`.

The package is declarations only, so what an operator can see of it is exactly what these
drive: `portfolio clients` lists both accounts with the variables each would read and the
refusal a stage naming one would meet, and `doctor` reads `[adapters.okx]` through the
package's own model and reports the clients unset. No variable is set and nothing reaches a
network — there is no network code to reach one.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

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
