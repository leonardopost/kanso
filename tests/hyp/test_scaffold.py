"""Scaffolding: three files, rendered for one id, written once."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kanso import hyp
from kanso.errors import Exit, KansoError
from kanso.workspace import Workspace


def test_scaffold_writes_exactly_the_three_scoped_files(ws: Workspace) -> None:
    directory = hyp.scaffold(ws, "new_idea")

    assert directory == ws.path("hypotheses", "new_idea")
    assert sorted(path.name for path in directory.iterdir()) == [
        "hypothesis.yaml",
        "program.md",
        "strategy.py",
    ]


def test_scaffold_renders_the_id_into_every_file(ws: Workspace) -> None:
    directory = hyp.scaffold(ws, "new_idea")

    for name in ("hypothesis.yaml", "program.md", "strategy.py"):
        text = (directory / name).read_text(encoding="utf-8")
        assert "new_idea" in text
        assert "{{" not in text


def test_scaffold_dates_the_programs_example_tag(ws: Workspace) -> None:
    before = datetime.now(tz=UTC).date()
    directory = hyp.scaffold(ws, "new_idea")
    after = datetime.now(tz=UTC).date()

    program = (directory / "program.md").read_text(encoding="utf-8")
    # Either day the call spanned: a scaffold written across UTC midnight is dated
    # correctly, just not with the day this assertion would have computed for itself.
    assert any(f"{day:%Y%m%d}-1" in program for day in {before, after})


def test_the_scaffolded_strategy_is_the_sleeve_stub(ws: Workspace) -> None:
    directory = hyp.scaffold(ws, "new_idea")

    assert (directory / "strategy.py").read_text(encoding="utf-8") == hyp.stub("new_idea")


def test_the_scaffolded_hypothesis_still_needs_filling_in(ws: Workspace) -> None:
    directory = hyp.scaffold(ws, "new_idea")

    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, directory / "hypothesis.yaml")

    assert failure.value.code is Exit.VALIDATION


def test_scaffold_refuses_an_existing_directory(ws: Workspace) -> None:
    hyp.scaffold(ws, "new_idea")

    with pytest.raises(KansoError) as failure:
        hyp.scaffold(ws, "new_idea")

    assert failure.value.code is Exit.PRECONDITION
    assert "scaffolded once" in failure.value.message


@pytest.mark.parametrize("bad", ["ab", "Upper", "with-dash", "x" * 41, ""])
def test_scaffold_refuses_an_id_that_is_not_one(ws: Workspace, bad: str) -> None:
    with pytest.raises(KansoError) as failure:
        hyp.scaffold(ws, bad)

    assert failure.value.code is Exit.VALIDATION
    assert failure.value.message.startswith("id:")


def test_the_modifier_stub_names_its_construct_and_host() -> None:
    text = hyp.stub("new_idea", "filter", "host_sleeve")

    assert 'construct = "filter"' in text
    assert "host_sleeve" in text
    assert "KansoModifier" in text


def test_a_sleeve_stub_takes_no_host() -> None:
    with pytest.raises(KansoError) as failure:
        hyp.stub("new_idea", "sleeve", "host_sleeve")

    assert failure.value.code is Exit.VALIDATION
    assert failure.value.message.startswith("host:")


def test_an_attached_stub_needs_one() -> None:
    with pytest.raises(KansoError) as failure:
        hyp.stub("new_idea", "filter")

    assert failure.value.code is Exit.VALIDATION
    assert failure.value.message.startswith("host:")


@settings(max_examples=50, deadline=None)
@given(st.from_regex(r"\A[a-z0-9_]{3,40}\Z"))
def test_check_id_accepts_exactly_the_ids_the_registry_keys_on(candidate: str) -> None:
    assert hyp.check_id(candidate) == candidate


def test_an_unfilled_placeholder_is_a_fault() -> None:
    from kanso.hyp.scaffold import render

    with pytest.raises(KansoError) as failure:
        render("program.md", hyp_id="new_idea")

    assert failure.value.code is Exit.VALIDATION
    assert "unfilled placeholder" in failure.value.message
