"""`init` scaffolds the same workspace in all three repository situations."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kanso import __version__
from kanso.errors import Exit, PreconditionError
from kanso.workspace import (
    AGENTS_FALLBACK,
    AGENTS_IMPORT,
    Workspace,
    append_gitignore,
    gitignore_entries,
    in_git_repo,
    init,
)

from .conftest import snapshot

EXPECTED_FILES = [
    "kanso.toml",
    "AGENTS.md",
    "CLAUDE.md",
    ".gitignore",
    ".env",
    "models.yaml",
    "instruments.yaml",
    "portfolio.yaml",
    "escalations/inbox.md",
]
EXPECTED_DIRS = ["hypotheses", "catalog"]


def _assert_scaffolded(root: Path) -> None:
    for name in EXPECTED_FILES:
        assert (root / name).is_file(), name
    for name in EXPECTED_DIRS:
        assert (root / name).is_dir(), name


def test_fresh_directory(fresh: Path) -> None:
    ws = init(fresh)

    assert isinstance(ws, Workspace)
    assert ws.root == fresh.resolve()
    assert ws.notices == ()
    _assert_scaffolded(ws.root)
    assert __version__ in (ws.root / "kanso.toml").read_text(encoding="utf-8")
    assert (ws.root / "CLAUDE.md").read_text(encoding="utf-8").strip() == AGENTS_IMPORT
    assert ws.path("hypotheses", "x") == ws.root / "hypotheses" / "x"


def test_creates_a_missing_directory(tmp_path: Path) -> None:
    ws = init(tmp_path / "nested" / "ws")

    _assert_scaffolded(ws.root)


def test_inside_a_repository(repo: Path) -> None:
    before = snapshot(repo, exclude=repo / "nothing")

    ws = init(repo)

    _assert_scaffolded(ws.root)
    assert in_git_repo(ws.root) == repo.resolve()
    assert before["README.md"] == (repo / "README.md").read_bytes()
    assert (repo / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_in_a_monorepo_subdirectory(repo: Path, monorepo: Path) -> None:
    outside = snapshot(repo, exclude=monorepo)

    ws = init(monorepo)

    _assert_scaffolded(ws.root)
    assert in_git_repo(ws.root) == repo.resolve()
    assert snapshot(repo, exclude=monorepo) == outside, "init wrote outside the workspace"


def test_all_three_situations_agree(fresh: Path, repo: Path, monorepo: Path) -> None:
    """The repository situation changes nothing about what `init` writes."""
    written: list[dict[str, bytes]] = []
    for directory in (fresh, monorepo, repo):
        before = set(snapshot(directory, exclude=directory / ".git"))
        ws = init(directory)
        after = snapshot(ws.root, exclude=directory / ".git")
        written.append({name: body for name, body in after.items() if name not in before})

    assert written[0].keys() == written[1].keys() == written[2].keys()
    assert written[0] == written[1] == written[2]


def test_refuses_to_reinitialise(fresh: Path) -> None:
    init(fresh)
    edited = (fresh / "kanso.toml").read_text(encoding="utf-8") + "\n# operator edit\n"
    (fresh / "kanso.toml").write_text(edited, encoding="utf-8")

    with pytest.raises(PreconditionError) as caught:
        init(fresh)

    assert caught.value.code == Exit.PRECONDITION
    assert "skills sync" in (caught.value.remedy or "")
    assert "env detect" in (caught.value.remedy or "")
    assert (fresh / "kanso.toml").read_text(encoding="utf-8") == edited


def test_env_is_empty_and_owner_only(fresh: Path) -> None:
    ws = init(fresh)

    env = ws.path(".env")
    assert env.read_bytes() == b""
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_existing_env_is_never_touched(fresh: Path) -> None:
    (fresh / ".env").write_text("KANSO_MOCK_API_KEY=kept\n", encoding="utf-8")

    ws = init(fresh)

    assert ws.path(".env").read_text(encoding="utf-8") == "KANSO_MOCK_API_KEY=kept\n"


def test_existing_instructions_are_left_alone(fresh: Path) -> None:
    (fresh / "AGENTS.md").write_text("# my rules\n", encoding="utf-8")
    (fresh / "CLAUDE.md").write_text("# my claude rules\n", encoding="utf-8")

    ws = init(fresh)

    assert ws.path("AGENTS.md").read_text(encoding="utf-8") == "# my rules\n"
    assert ws.path("CLAUDE.md").read_text(encoding="utf-8") == "# my claude rules\n"
    assert ws.path(AGENTS_FALLBACK).is_file()
    assert any(AGENTS_FALLBACK in note for note in ws.notices)
    assert any(AGENTS_IMPORT in note for note in ws.notices)


def test_gitignore_created_with_every_entry(fresh: Path) -> None:
    ws = init(fresh)

    lines = ws.path(".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in gitignore_entries():
        assert entry in lines
    assert "# catalog/" in [ln.strip() for ln in lines], "the catalog choice stays commented"


def test_the_envelope_is_kept_out_of_git(fresh: Path) -> None:
    """`envelope.yaml` measures the host, so a clone must detect its own rather than inherit
    a committed one — the workspace template excludes it.
    """
    assert "envelope.yaml" in gitignore_entries()

    ws = init(fresh)

    lines = [
        line.strip() for line in ws.path(".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    assert "envelope.yaml" in lines


def test_gitignore_appends_only_what_is_missing(fresh: Path) -> None:
    (fresh / ".gitignore").write_text("*.pyc\n.env\n", encoding="utf-8")

    ws = init(fresh)

    lines = ws.path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "*.pyc"
    assert lines.count(".env") == 1
    for entry in gitignore_entries():
        assert lines.count(entry) == 1


def test_gitignore_append_is_idempotent(fresh: Path) -> None:
    ws = init(fresh)
    first = ws.path(".gitignore").read_text(encoding="utf-8")

    appended = append_gitignore(ws.root, gitignore_entries())

    assert appended == []
    assert ws.path(".gitignore").read_text(encoding="utf-8") == first


def test_gitignore_append_handles_a_file_without_a_final_newline(fresh: Path) -> None:
    (fresh / ".gitignore").write_text("build", encoding="utf-8")

    appended = append_gitignore(fresh, [".env", "runs/"])

    assert appended == [".env", "runs/"]
    assert fresh.joinpath(".gitignore").read_text(encoding="utf-8").splitlines()[:2] == [
        "build",
        "",
    ]


def test_gitignore_append_creates_the_file(fresh: Path) -> None:
    appended = append_gitignore(fresh, ["runs/", "runs/"])

    assert appended == ["runs/"]
    assert fresh.joinpath(".gitignore").read_text(encoding="utf-8").endswith("runs/\n")


def test_in_git_repo_without_a_repository(fresh: Path) -> None:
    assert in_git_repo(fresh) is None


def test_in_git_repo_finds_a_worktree_file(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

    assert in_git_repo(tmp_path / "wt") == (tmp_path / "wt").resolve()


def test_demo_renders_the_demo_files(fresh: Path) -> None:
    ws = init(fresh, demo=True)

    _assert_scaffolded(ws.root)
    assert ws.path("demo.yaml").is_file()
    assert ws.path("mock", "responses.yaml").is_file()
    assert ws.path("hypotheses", "demo_mr", "hypothesis.yaml").is_file()

    models = ws.path("models.yaml").read_text(encoding="utf-8")
    assert "protocol: mock" in models
    assert "<frontier-model-id>" not in models
    for tier in ("cheap", "mid", "frontier"):
        assert tier in models

    instruments = ws.path("instruments.yaml").read_text(encoding="utf-8")
    assert "DEMO.SIM" in instruments
    assert "manual: true" in instruments

    program = ws.path("hypotheses", "demo_mr", "program.md").read_text(encoding="utf-8")
    assert "{{" not in program
    assert "demo_mr" in program


def test_without_demo_nothing_demo_is_written(fresh: Path) -> None:
    ws = init(fresh)

    assert not ws.path("demo.yaml").exists()
    assert not ws.path("mock").exists()
    assert list(ws.path("hypotheses").iterdir()) == []
    assert "<frontier-model-id>" in ws.path("models.yaml").read_text(encoding="utf-8")


def test_a_commented_entry_does_not_count_as_present(fresh: Path) -> None:
    """`# state.db` ignores nothing, so the bare entry is appended beside it."""
    (fresh / ".gitignore").write_text("# state.db\n", encoding="utf-8")

    appended = append_gitignore(fresh, ["state.db"])

    lines = (fresh / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert appended == ["state.db"]
    assert lines.count("state.db") == 1 and "# state.db" in lines


def test_an_existing_gitignore_is_offered_the_catalog_choice_once(fresh: Path) -> None:
    """`init` into a directory that already has a `.gitignore` — every `uv init` project —
    used to write only the normative entries, so the commented `catalog/` line the docs call
    the operator's to decide never reached them. It is appended commented, once, and left
    alone when the file already mentions `catalog/` either way. `append_gitignore` itself
    stays a plain append: `skills sync` calls it too."""
    from kanso.workspace import _write_gitignore, init

    (fresh / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    init(fresh)
    first = (fresh / ".gitignore").read_text(encoding="utf-8")
    _write_gitignore(fresh)
    second = (fresh / ".gitignore").read_text(encoding="utf-8")

    assert first.count("# catalog/") == 1
    assert "catalog/.cache/" in first, "the normative entries still arrive"
    assert second == first, "a second pass changes nothing"

    decided = fresh / "decided"
    decided.mkdir()
    (decided / ".gitignore").write_text("catalog/\n", encoding="utf-8")
    _write_gitignore(decided)
    assert "# catalog/" not in (decided / ".gitignore").read_text(encoding="utf-8")

    untouched = fresh / "plain"
    untouched.mkdir()
    (untouched / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    append_gitignore(untouched, ["runs/"])
    assert "catalog" not in (untouched / ".gitignore").read_text(encoding="utf-8")

    # Every normative entry already present and no final newline: nothing to append, so
    # the choice is the only thing written, and it must start on its own line.
    complete = fresh / "complete"
    complete.mkdir()
    (complete / ".gitignore").write_text("\n".join(gitignore_entries()), encoding="utf-8")
    _write_gitignore(complete)
    text = (complete / ".gitignore").read_text(encoding="utf-8")
    assert text.endswith("# catalog/\n")
    assert f"{gitignore_entries()[-1]}\n" in text, "the last entry keeps its own line"
