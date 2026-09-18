"""The skills instruct an agent to do what the package accepts, and nothing it refuses."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from kanso.config import EnvConfig
from kanso.schemas import CardStatus

ROOT = Path(__file__).resolve().parents[2]
PACKAGED = ROOT / "src" / "kanso" / "skills"
MAINTAINER = ROOT / "skills"


def skill(root: Path, name: str) -> str:
    return (root / name / "SKILL.md").read_text(encoding="utf-8")


def test_the_env_skill_names_every_override_the_configuration_declares() -> None:
    """A key the skill omits is one an operator in exactly its situation — a host
    planning a single lane — cannot learn exists."""
    text = skill(PACKAGED, "kanso-env")
    overrides = next(line for line in text.splitlines() if "`kanso.toml [env]`" in line)
    for name in EnvConfig.model_fields:
        assert f"`{name}`" in overrides, name
    assert "**replaces** the derived memory per lane" in text


def test_the_hypothesis_skill_names_the_spread_a_bar_only_hypothesis_needs() -> None:
    text = skill(PACKAGED, "kanso-hypothesis")
    assert "`costs`, `risk_limits`: keep defaults" not in text
    assert "set `costs: {spread: fixed_bps, fixed_bps: <width>}`" in text


def test_the_promote_skill_names_the_one_cost_a_broker_does_not_supply() -> None:
    text = skill(PACKAGED, "kanso-promote")
    assert "must be `fixed_bps` on the hypothesis or under `venues.<MIC>.costs`" in text


def test_the_release_skill_does_not_ask_for_a_schema_version_bump() -> None:
    text = skill(MAINTAINER, "kanso-release")
    assert "confirm `schema_version` was bumped" not in text
    assert "nothing is bumped by hand" in text


def test_the_release_skill_names_a_migration_fixture_that_exists() -> None:
    """Step 1 once sent the maintainer to a fixture the suite did not have."""
    text = skill(MAINTAINER, "kanso-release")
    assert "on a workspace created by the previous release" not in text
    named = re.findall(r"`(tests/state/fixtures/[a-z0-9_]+\.sql)`", text)
    assert named
    for path in named:
        assert (ROOT / path).is_file(), path


def test_the_research_skill_names_every_status_a_card_can_carry() -> None:
    """This file ships into every workspace on `kanso init` and `kanso skills sync`, so a
    status it omits is one an operator meets in `results.tsv` with nothing to read. It
    described `redundant` as a refusal with no card and no trial for a release after that
    stopped being true, which is the worse failure: a page that is confidently wrong."""
    text = skill(PACKAGED, "kanso-research")
    for status in get_args(CardStatus):
        assert f"`{status}`" in text or f"**{status}**" in text, status
    assert "no card, no trial" not in text
    assert "so it is a trial, a `results.tsv` row and a coverage entry like any other" in text
