"""`kanso hyp explore`: a draft hypothesis from a parent's research, from the command line.

The explorer itself is proved in its own slice; what is asserted here is the command around
it — that the object printed under `--json` names what was written, that the draft it
writes is one `hyp validate` admits and `hyp add` registers as a draft, and that with no
model to call the step exits 2 rather than writing anything of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from . import mocked
from .conftest import DRAFT, FLAT, HYP_ID, at, payload

CANDIDATE_ID = "demo_breakout"


def answer() -> dict[str, Any]:
    """The explorer's scripted candidate: the parent's windows and universe, a new idea."""
    return {
        "id": CANDIDATE_ID,
        "hypothesis_yaml": yaml.safe_dump(
            {
                **DRAFT,
                "id": CANDIDATE_ID,
                "title": "Demo: hourly breakout on a synthetic OU series",
                "thesis": "A close through the session's range continues for an hour.",
                "mechanism": "momentum",
            },
            sort_keys=False,
        ),
        "program_md": "# program\n\nEdit strategy.py; keep the breakout.\n",
        "strategy_py": FLAT.replace("pass", "notional: float = 1_000.0", 1),
        "rationale": "the parent only faded deviations; this trades the break instead",
        "tags": ["signal_breakout"],
    }


def researched(runner: CliRunner, root: Path) -> None:
    """The parent after a baseline and one proposed card, and an explorer to call."""
    assert at(runner, root, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK
    mocked.write_script(root, "frontier", {"certify_plan": [mocked.PLAN], "explore": [answer()]})


def test_explore_writes_a_draft_that_validate_admits_and_add_registers(
    runner: CliRunner, mocked_ws: Path
) -> None:
    researched(runner, mocked_ws)

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    written = payload(result)
    path = mocked_ws / "hypotheses" / CANDIDATE_ID / "hypothesis.yaml"
    assert (written["parent"], written["id"]) == (HYP_ID, CANDIDATE_ID)
    assert Path(written["dir"]) == path.parent
    assert written["files"] == ["hypothesis.yaml", "program.md", "strategy.py"]
    assert written["tags"] == ["signal_breakout"]

    inbox = payload(at(runner, mocked_ws, "inbox", "--json"))
    assert [(entry["kind"], entry["subject"]) for entry in inbox["entries"]] == [
        ("explored", CANDIDATE_ID)
    ]
    assert at(runner, mocked_ws, "hyp", "validate", path).exit_code == Exit.OK
    added = at(runner, mocked_ws, "hyp", "add", path, "--json")
    assert added.exit_code == Exit.OK, added.stdout
    assert payload(added)["status"] == "draft"


def test_explore_reads_as_what_was_written_and_the_next_command(
    runner: CliRunner, mocked_ws: Path
) -> None:
    researched(runner, mocked_ws)

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID)

    assert result.exit_code == Exit.OK, result.stdout
    assert f"{CANDIDATE_ID} · from {HYP_ID}" in result.stdout
    assert "the parent only faded deviations" in result.stdout
    assert f"kanso hyp validate {mocked_ws / 'hypotheses' / CANDIDATE_ID}" in result.stdout


def test_explore_with_no_model_exits_2_and_writes_nothing(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    (mocked_ws / "models.yaml").unlink()

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "models.yaml" in payload(result)["error"]
    assert sorted(path.name for path in (mocked_ws / "hypotheses").iterdir()) == [HYP_ID]


def test_explore_of_a_hypothesis_never_researched_exits_2(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert payload(result)["remedy"] == f"run `kanso research run {HYP_ID}`"
