"""`catch_up` through `kanso screen run`, `show` and `draft`: a pair's response fitted, its
positions ended by conditions, and the rule written as a seed the runner re-measures.

The two names of the paired workspace share nothing but their calendar, so the demo name's
response to the other is small and of either sign: what these tests read is the pipeline —
the fit span, the flags, the exits and the draft — and the unit suite reads the arithmetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit, ValidationError
from kanso.schemas.screen import Cell, ResponseStats
from kanso.screen import draft

from .conftest import INSTRUMENT, OTHER, at, classify, payload
from .test_screen import write_screen
from .test_screen_run import OU, run_screen


def caught(fit: str, **extra: Any) -> dict[str, Any]:
    return {
        **OU,
        "id": f"caught_{fit}",
        "title": "The demo name trails the other's hourly move",
        "thesis": "After the other name moves, the demo name follows it within hours.",
        "legs": {
            "a": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"},
            "b": {"instrument": OTHER, "type": "bar", "resolution": "1h"},
        },
        "derived": {"both": {"basket": {"a": 0.5, "b": 0.5}}},
        "groups": {"names": ["a", "both"]},
        "costs": {
            "SIM": {"commission_bps": 0.5, "slippage_bps": 1, "spread": "fixed_bps", "fixed_bps": 2}
        },
        "measures": [
            {
                "id": "catch_up",
                "leader": "b",
                "followers": "names",
                "within": "2h",
                "fit": fit,
                "beta_over": "1h",
                "enter_bp": [0.1],
                "close": [0.0],
                "stop": [3.0],
                "max_hold": "4h",
                "latency_ms": 0,
                **extra,
            }
        ],
        "verdict": {"alpha": 0.05, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 10},
    }


CELL = "catch_up/b>a/0.1bp/2h/close0/stop3"


def shown(runner: CliRunner, root: Path, screen: str, key: str) -> Cell:
    result = at(runner, root, "screen", "show", screen, "--cell", key, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return Cell.model_validate(yaml.safe_load(result.stdout))


def test_a_catch_up_fitted_on_its_first_fold_is_judged_on_the_rest(
    runner: CliRunner, paired: Path
) -> None:
    run_screen(runner, paired, caught("window"))
    run_screen(runner, paired, caught("first_fold"))

    whole = shown(runner, paired, "caught_window", CELL)
    later = shown(runner, paired, "caught_first_fold", CELL)

    assert whole.in_sample_fit is True and later.in_sample_fit is False
    assert later.folds[0] is None and whole.folds[0] is not None
    assert later.sessions < whole.sessions
    for cell in (whole, later):
        assert cell.id == "catch_up" and cell.catch_up is not None and cell.response is not None
        caught_ = cell.catch_up
        assert caught_.beta is not None
        assert caught_.closed + caught_.stopped + caught_.capped == cell.response.events > 0
        assert 0 < caught_.hold_median_s <= caught_.hold_p90_s <= 4 * 3600
    basket = shown(runner, paired, "caught_window", "catch_up/b>both/0.1bp/2h/close0/stop3")
    assert basket.catch_up is not None and basket.catch_up.beta is not None
    assert basket.catch_up.beta > whole.catch_up.beta  # type: ignore[union-attr]


def test_show_and_validate_say_how_a_catch_up_ends_and_what_it_fits(
    runner: CliRunner, paired: Path
) -> None:
    run_screen(runner, paired, caught("first_fold"))

    detail = at(runner, paired, "screen", "show", "caught_first_fold", "--cell", CELL).stdout
    checked = at(
        runner, paired, "screen", "validate", write_screen(paired, caught("first_fold"))
    ).stdout

    assert "exits" in detail and "closed" in detail and "hold median" in detail
    assert "beta" in detail and "(net bp a day)" in detail
    assert "catch_up · b leads · beta fitted on the first fold · 2 cell(s)" in checked


def test_a_passing_catch_up_is_worth_a_lane(runner: CliRunner, paired: Path) -> None:
    document = caught("window")
    document["verdict"] = {**document["verdict"], "min_margin_bp": -1e6, "alpha": 0.99}

    result = run_screen(runner, paired, document)

    assert result["verdict"]["worth_a_lane"] is True


def test_a_catch_up_cell_becomes_a_draft_whose_seed_is_its_rule(
    runner: CliRunner, paired: Path
) -> None:
    run_screen(runner, paired, caught("first_fold"))
    beta = shown(runner, paired, "caught_first_fold", CELL).catch_up

    result = at(
        runner,
        paired,
        "screen",
        "draft",
        "caught_first_fold",
        "--cell",
        CELL,
        "--as",
        "caught_lane",
        "--certify",
        "2024-03-30..2024-05-31",
        "--json",
    )

    assert result.exit_code == Exit.OK, result.stdout
    directory = Path(payload(result)["dir"])
    hypothesis = yaml.safe_load((directory / "hypothesis.yaml").read_text(encoding="utf-8"))
    assert hypothesis["universe"] == [OTHER, INSTRUMENT]
    assert hypothesis["mechanism"] == "stat_arb" and hypothesis["horizon"] == "4h"
    program = (directory / "program.md").read_text(encoding="utf-8")
    assert "- exits: " in program and "fitted response to its leader: beta" in program
    seed = (directory / "strategy.py").read_text(encoding="utf-8")
    assert f'LEADER = "{OTHER}"' in seed and f'FOLLOWER = "{INSTRUMENT}"' in seed
    assert beta is not None and f"BETA = {beta.beta!r}" in seed
    assert "stop: float = 3.0" in seed and "max_hold_s: float = 14400.0" in seed
    path = directory / "hypothesis.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "construct: {id: sleeve, rationale: the screened rule}\n"
        + "objective: {id: net_edge_bps, params: {min_delta: 0.0, k_se: 1.0}}\n"
        + "constraints: [{id: strategy_integrity}]\n",
        encoding="utf-8",
    )
    assert at(runner, paired, "hyp", "add", path).exit_code == Exit.OK
    classify(paired, "caught_lane")

    begun = at(runner, paired, "research", "begin", "caught_lane", "--json")

    assert begun.exit_code == Exit.OK, begun.stdout
    assert payload(begun)["baseline"]["status"] == "keep"


def test_a_catch_up_with_no_response_is_not_drafted() -> None:
    stats = ResponseStats(
        events=0,
        events_per_day=0,
        gross_bp=0,
        hurdle_bp=0,
        margin_bp=0,
        ceiling_bp_day=0,
        hit_rate=0,
        unfilled=0,
        drift_adjusted_bp=0,
    )
    cell = Cell.model_validate(
        {
            "measure": 0,
            "id": "catch_up",
            "key": CELL,
            "params": {"follower": "a", "enter_bp": 0.1, "close": 0.0, "stop": 3.0},
            "mean": 0,
            "se": 0,
            "t": 0,
            "p": 1,
            "sessions": 0,
            "folds": [],
            "folds_same_sign": 0,
            "response": stats,
            "catch_up": {
                "closed": 0,
                "stopped": 0,
                "capped": 0,
                "hold_median_s": 0,
                "hold_p90_s": 0,
            },
        }
    )
    from kanso.schemas.screen import CatchUp, Screen

    screen = Screen.model_validate({**caught("window"), "schema": 1})
    measure = screen.measures[0]
    assert isinstance(measure, CatchUp)

    with pytest.raises(ValidationError, match="no response to trade"):
        draft._caught("caught_lane", screen, measure, cell)
