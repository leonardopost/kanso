"""The card path: a child of its own, an environment allow-list, and a supervisor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kanso.nautilus.backtest import (
    ALLOWED_ENV,
    child_env,
    run,
    run_subprocess,
)

from .conftest import (
    RAISING_SLEEVE,
    REFUSING_SLEEVE,
    SLOW_SLEEVE,
    TELLING_SLEEVE,
    VANISHING_SLEEVE,
)

pytestmark = pytest.mark.usefixtures("store")


@pytest.fixture
def lane(tmp_path: Path) -> Path:
    """A lane directory holding the three scoped files a card is allowed to see."""
    directory = tmp_path / "lane"
    directory.mkdir()
    for name in ("hypothesis.yaml", "program.md", "strategy.py"):
        (directory / name).write_text(f"# {name}\n", encoding="utf-8")
    return directory


def test_a_card_produces_what_the_same_run_produces_in_process(
    store: Path, lane: Path, request_for
) -> None:
    request = request_for()

    inside = run(request, store)
    outside = run_subprocess(request, store, lane)

    assert outside.crashed is False
    assert outside.run == inside.run
    assert outside.intents == inside.intents


def test_a_card_leaves_the_lane_directory_exactly_as_it_found_it(
    store: Path, lane: Path, request_for
) -> None:
    before = {path.name: path.read_bytes() for path in lane.iterdir()}

    run_subprocess(request_for(), store, lane)

    assert {path.name: path.read_bytes() for path in lane.iterdir()} == before


def test_the_child_is_given_an_allow_list_and_no_catalog(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The card reports the environment it was actually handed, so this is the boundary
    # itself under test rather than the function that computes it.
    monkeypatch.setenv("KANSO_CATALOG", str(store))
    monkeypatch.setenv("KANSO_MASSIVE_API_KEY", "not-a-real-key")

    result = run_subprocess(request_for(source=TELLING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.reason == "exception"
    reported = result.traceback_tail or ""
    named = reported.split("environment: ")[-1].strip("\"'").split(",")
    assert "KANSO_CATALOG" not in named
    assert "KANSO_MASSIVE_API_KEY" not in named
    assert "PYTHONHASHSEED" in named
    assert set(named) - PLATFORM_ADDED <= {*ALLOWED_ENV, "PYTHONHASHSEED", *_coverage_names()}


PLATFORM_ADDED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
"""Names the interpreter and the operating system add for themselves: the locale
coercion of PEP 538, and CoreFoundation's text encoding on macOS. Neither is inherited
— a child started with neither in its environment is handed both anyway."""


def _coverage_names() -> set[str]:
    return {name for name in os.environ if name.startswith("COVERAGE_")}


def test_the_allow_list_keeps_nothing_it_was_not_given() -> None:
    made = child_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/quant",
            "KANSO_CATALOG": "/data",
            "AWS_SECRET_ACCESS_KEY": "shhh",
            "COVERAGE_PROCESS_CONFIG": "/cfg",
            "PYTHONPATH": "/somewhere",
        }
    )

    assert made == {
        "PATH": "/usr/bin",
        "HOME": "/home/quant",
        "COVERAGE_PROCESS_CONFIG": "/cfg",
        "PYTHONHASHSEED": "0",
    }


def test_the_allow_list_reads_the_ambient_environment_by_default() -> None:
    made = child_env()

    assert made["PYTHONHASHSEED"] == "0"
    assert set(made) <= {*ALLOWED_ENV, "PYTHONHASHSEED", *_coverage_names()}
    assert not [name for name in made if name.startswith("KANSO")]


def test_a_card_that_raises_is_a_crash_with_its_traceback_tail(
    store: Path, lane: Path, request_for
) -> None:
    result = run_subprocess(request_for(source=RAISING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.reason == "exception"
    assert "the card asked for the impossible" in (result.traceback_tail or "")
    assert len((result.traceback_tail or "").splitlines()) <= 50
    assert result.remedy is None
    assert result.run.returns == ()
    assert result.run.capital == request_for().capital


def test_a_refusal_the_child_raised_brings_its_remedy_back(
    store: Path, lane: Path, request_for
) -> None:
    """A card fails for causes that are not the card's, and each names its own next action.

    The traceback tail carries the message and nothing else, so a caller reading only that
    has to guess a remedy for every failure alike. What the cause knew crosses the process
    boundary as a value.
    """
    result = run_subprocess(request_for(source=REFUSING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.remedy == "load it and try again"


def test_a_card_that_leaves_no_report_is_a_crash(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(source=VANISHING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.reason == "died"


def test_a_card_that_overruns_its_clock_is_killed(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(source=SLOW_SLEEVE, budget_s=0.05), store, lane)

    assert result.crashed is True
    assert result.reason == "budget"
    assert result.intents == ()


def test_a_card_that_outgrows_its_memory_is_killed(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kanso.nautilus.backtest.MEMORY_POLL_S", 0.0)

    result = run_subprocess(
        request_for(source=SLOW_SLEEVE, mem_cap_gb=1e-6),
        store,
        lane,
    )

    assert result.crashed is True
    assert result.reason == "memory"


def test_the_peak_of_the_reaped_child_is_reported(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(), store, lane)

    assert result.peak_mem_gb > 0
    assert result.wall_s > 0


def test_a_child_that_says_nothing_leaves_no_tail(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(), store, lane)

    assert result.traceback_tail is None
