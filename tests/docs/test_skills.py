"""The skills instruct an agent to do what the package accepts, and nothing it refuses."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import yaml

from kanso.config import EnvConfig, ResearchConfig
from kanso.criteria import catalogue, check_params
from kanso.schemas import CardStatus
from tests.criteria.builders import make_hyp
from tests.docs.test_pages import scope_names

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


def test_the_hypothesis_skill_writes_a_session_the_gate_accepts() -> None:
    """The example is copied into hypotheses as written, so it must validate as written."""
    text = skill(PACKAGED, "kanso-hypothesis")
    found = re.search(r"`required_constraints: \[\{id: trading_hours, params: (\{.*?\})\}\]`", text)
    assert found is not None
    params = yaml.safe_load(found.group(1))
    assert check_params(catalogue()["trading_hours"], params, make_hyp(), 4) == []


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


def test_the_align_skill_says_the_run_s_base_is_never_judged() -> None:
    """This file ships into every workspace, and it is what an agent reads to explain a
    `misaligned` entry. Told only that the model is asked once the syntax tree passes, an
    agent would take a check of a run still on its base for a verdict on it."""
    text = skill(PACKAGED, "kanso-align")
    assert "The run's base is never judged" in text


def test_the_data_skill_says_where_a_split_s_ex_date_is_dated() -> None:
    """An agent declaring a US split without a zone gets a UTC day, which lands the split
    inside a winter post-market; the skill is where it learns the key exists."""
    text = skill(PACKAGED, "kanso-data")
    assert "`override.info.timezone`" in text
    assert "first session that traded at the new price" in text


def test_the_data_skill_says_an_unmapped_side_is_no_aggressor() -> None:
    """The loader called every print of a file without the column a buyer's, and an agent
    writing a spec had nowhere to learn that the column decides which resting orders fill."""
    text = skill(PACKAGED, "kanso-data")
    assert "left unmapped, every print is loaded with no aggressor, never a guessed one" in text


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


def test_the_research_skill_names_every_field_a_re_pin_clears_the_best_on() -> None:
    """An agent re-pinning a hypothesis between runs reads here whether the edit costs the
    best; the list stopped at `book` while the objective, the costs and the depth cleared it
    too, as the types each instrument is asked for and the session scope now do."""
    text = skill(PACKAGED, "kanso-research")
    for name in scope_names():
        assert name in text, name


def test_the_research_skill_states_the_exploration_default_the_package_ships() -> None:
    """The agent that reads this skill is the one an `explored` draft lands in front of.

    The skill is a symlink into the installed package, so an agent in a workspace an older
    template wrote reads it too, beside a `kanso.toml` that `init` never rewrote: it is told
    to read the key there rather than to assume the template's number."""
    default = ResearchConfig().explore_after_stalls
    text = skill(PACKAGED, "kanso-research")
    assert (
        "`[research] explore_after_stalls` — read it in the workspace's `kanso.toml`, which"
        f" decides: the template writes {default}, 0 is never, and a workspace initialised on"
        " 0.8.0 to 0.13.x states 0"
    ) in text


def test_the_data_skill_says_what_a_gap_and_an_answer_mean() -> None:
    """An agent reading `data show` for an operator meets `empty` inside `gaps`, and told
    nothing would either ask for those days again or call a lost trading day a holiday."""
    text = skill(PACKAGED, "kanso-data")
    step = next(line for line in text.splitlines() if "`kanso data show` →" in line)
    assert "A gap always holds a day the instrument's market opened" in step
    assert "never something to explain away as a holiday" in step
    assert "which a backfill will not ask for again and which no snapshot covers" in step


def test_the_data_skill_names_funding_as_realised_and_required_of_a_perpetual() -> None:
    """An agent loading a perpetual's history reaches for whatever rate a feed publishes; the
    skill is where it learns the predicted one is not funding, and that validation needs it."""
    text = skill(PACKAGED, "kanso-data")
    assert "| funding |" in text
    assert "the **realised** rate of the period that settled" in text
    assert "must list `funding` in `data_requirements`" in text
    assert "a spot leg beside it is asked for none" in text
    assert "the runner books each settlement on what the card held before it" in text
    assert "`types: [bar, funding]`" in text


def test_the_screen_skill_names_what_the_screen_reads_and_refuses() -> None:
    """The skill an agent writes a screen from names every state a series can be in, every
    verdict floor, the three declarations an adapter it builds must make, and the field
    the followers are spelt with — never `on`, which YAML reads as true."""
    from kanso.schemas.screen import ScreenVerdict

    text = skill(PACKAGED, "kanso-screen")
    for state in ("held", "fetchable", "unresolved", "unserved"):
        assert f"`{state}`" in text, state
    for floor in ScreenVerdict.model_fields:
        assert floor in text, floor
    for member in ("timestamps", "serves", "spec_for"):
        assert f"`{member}`" in text, member
    assert "`followers`" in text and "never `on`" in text
    assert "kanso screen draft" in text and "--certify" in text
