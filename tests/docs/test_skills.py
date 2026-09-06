"""The skills instruct an agent to do what the package accepts, and nothing it refuses."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGED = ROOT / "src" / "kanso" / "skills"
MAINTAINER = ROOT / "skills"


def skill(root: Path, name: str) -> str:
    return (root / name / "SKILL.md").read_text(encoding="utf-8")


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
