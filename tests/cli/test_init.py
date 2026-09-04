"""`kanso init` in the three repository situations, plain and `--demo`."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.skills_sync import packaged_skills
from kanso.workspace import gitignore_entries

from .conftest import at, payload, run

SCAFFOLDED = (
    "kanso.toml",
    "AGENTS.md",
    "CLAUDE.md",
    ".gitignore",
    ".env",
    "models.yaml",
    "instruments.yaml",
    "portfolio.yaml",
    "envelope.yaml",
    "escalations/inbox.md",
)


def _assert_workspace(root: Path) -> None:
    for name in SCAFFOLDED:
        assert (root / name).is_file(), name
    present = {
        line.strip() for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert set(gitignore_entries()) <= present
    linked = sorted((root / ".claude" / "skills").iterdir())
    assert len(linked) == len(packaged_skills())
    assert all(link.is_symlink() for link in linked)


def _outside(root: Path, workspace: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and workspace not in path.parents and path != workspace
    }


def test_init_scaffolds_a_fresh_directory(runner: CliRunner, fresh: Path) -> None:
    result = run(runner, "init", fresh)

    assert result.exit_code == Exit.OK
    _assert_workspace(fresh)
    assert str(fresh) in result.stdout


def test_init_reports_the_workspace_the_skills_and_the_envelope(
    runner: CliRunner, fresh: Path
) -> None:
    result = run(runner, "init", fresh, "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["workspace"] == str(fresh.resolve())
    assert document["demo"] is False
    assert document["repository"] is None
    assert len(document["skills"]) == len(packaged_skills()) * 3
    assert document["envelope"]["lanes"] >= 1
    assert Path(str(document["envelope"]["path"])).is_file()
    assert document["notices"] == []


def test_init_inside_a_repository_reports_it_and_writes_only_the_workspace(
    runner: CliRunner, repo: Path
) -> None:
    before = _outside(repo, repo / "unused")

    result = run(runner, "init", repo, "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["repository"] == str(repo.resolve())
    _assert_workspace(repo)
    assert (repo / "README.md").read_bytes() == before["README.md"]
    assert (repo / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_init_in_a_monorepo_subdirectory_touches_nothing_above_it(
    runner: CliRunner, repo: Path, monorepo: Path
) -> None:
    before = _outside(repo, monorepo)

    result = run(runner, "init", monorepo, "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["repository"] == str(repo.resolve())
    _assert_workspace(monorepo)
    assert _outside(repo, monorepo) == before


def test_init_demo_renders_the_demo_hypothesis_and_the_mock_register(
    runner: CliRunner, fresh: Path
) -> None:
    result = run(runner, "init", fresh, "--demo", "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["demo"] is True
    assert (fresh / "hypotheses" / "demo_mr" / "hypothesis.yaml").is_file()
    assert (fresh / "hypotheses" / "demo_mr" / "program.md").is_file()
    assert (fresh / "demo.yaml").is_file()
    assert (fresh / "mock" / "responses.yaml").is_file()
    assert "protocol: mock" in (fresh / "models.yaml").read_text(encoding="utf-8")


def test_init_without_a_directory_scaffolds_the_current_one(
    runner: CliRunner, fresh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fresh)

    result = run(runner, "init")

    assert result.exit_code == Exit.OK
    _assert_workspace(fresh)


def test_init_takes_the_global_workspace_option(runner: CliRunner, fresh: Path) -> None:
    result = at(runner, fresh, "init")

    assert result.exit_code == Exit.OK
    _assert_workspace(fresh)


def test_init_refuses_a_workspace_that_already_exists(runner: CliRunner, fresh: Path) -> None:
    assert run(runner, "init", fresh).exit_code == Exit.OK

    result = run(runner, "init", fresh, "--json")

    assert result.exit_code == Exit.PRECONDITION
    document = payload(result)
    assert document["code"] == int(Exit.PRECONDITION)
    assert "kanso.toml" in str(document["error"])


def test_init_reports_the_instructions_it_left_alone(runner: CliRunner, fresh: Path) -> None:
    (fresh / "AGENTS.md").write_text("# mine\n", encoding="utf-8")

    result = run(runner, "init", fresh, "--json")

    assert result.exit_code == Exit.OK
    notices = document_notices(payload(result))
    assert any("AGENTS.md" in notice for notice in notices)
    assert (fresh / "AGENTS.md").read_text(encoding="utf-8") == "# mine\n"


def document_notices(document: dict[str, object]) -> list[str]:
    notices = document["notices"]
    assert isinstance(notices, list)
    return [str(notice) for notice in notices]
