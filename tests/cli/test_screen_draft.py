"""`kanso screen draft`: a response cell becomes a draft hypothesis whose seed is its rule.

The end-to-end test drafts the fade the OU path supports, classifies it by hand as the
operator's override path does, registers it, and begins research: the baseline card is the
screened cell re-measured through the runner, and it keeps an edge of the cell's sign.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit, ValidationError
from kanso.schemas.screen import Screen, ScreenResult
from kanso.screen import draft
from kanso.state import StateStore
from kanso.workspace import find

from .conftest import INSTRUMENT, at, classify, payload
from .test_screen_run import FADE, OU, run_screen

CELL = "response/a/40bp/1h/a/1h"


def drafted(runner: CliRunner, root: Path, *extra: str) -> Any:
    return at(runner, root, "screen", "draft", "ou_fade", "--cell", CELL, *extra, "--json")


def test_a_cell_becomes_a_draft_whose_seed_is_its_rule(runner: CliRunner, loaded: Path) -> None:
    run_screen(runner, loaded, FADE)

    result = drafted(runner, loaded, "--as", "ou_lane", "--certify", "2024-03-30..2024-05-31")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert document["files"] == ["hypothesis.yaml", "program.md", "strategy.py"]
    directory = Path(document["dir"])
    hypothesis = yaml.safe_load((directory / "hypothesis.yaml").read_text(encoding="utf-8"))
    assert hypothesis["universe"] == [INSTRUMENT]
    assert (hypothesis["horizon"], hypothesis["resolution"]) == ("1h", "1h")
    assert hypothesis["data_requirements"] == ["bar"]
    assert hypothesis["mechanism"] == "mean_reversion"
    assert hypothesis["costs"]["fixed_bps"] == 2
    assert hypothesis["windows"]["research"] == {"start": "2024-01-02", "end": "2024-03-29"}
    assert hypothesis["windows"]["forward"] == {"start": "2024-06-01"}
    assert "construct" not in hypothesis
    program = (directory / "program.md").read_text(encoding="utf-8")
    assert "## Measured by screen ou_fade" in program and "hurdle of 5 bp" in program
    seed = (directory / "strategy.py").read_text(encoding="utf-8")
    assert (
        'TRIGGER = "DEMO.SIM"' in seed and "SIDE = -1" in seed and "threshold: float = 40.0" in seed
    )
    with StateStore(find(loaded).path("state.db")) as store:
        assert len(store.events(kind="screen_drafted", subject="ou_lane")) == 1
    assert at(runner, loaded, "hyp", "validate", directory / "hypothesis.yaml").exit_code == Exit.OK


def test_the_baseline_card_re_measures_the_screened_cell(runner: CliRunner, loaded: Path) -> None:
    run_screen(runner, loaded, FADE)
    drafted(runner, loaded, "--as", "ou_lane", "--certify", "2024-03-30..2024-05-31")
    path = loaded / "hypotheses" / "ou_lane" / "hypothesis.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "construct: {id: sleeve, rationale: the screened rule}\n"
        + "objective: {id: net_edge_bps, params: {min_delta: 0.0, k_se: 1.0}}\n"
        + "constraints: [{id: strategy_integrity}, {id: min_trades, params: {min: 10}}]\n",
        encoding="utf-8",
    )
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded, "ou_lane")

    begun = at(runner, loaded, "research", "begin", "ou_lane", "--json")

    assert begun.exit_code == Exit.OK, begun.stdout
    baseline = payload(begun)["baseline"]
    assert baseline["status"] == "keep" and baseline["metric"] > 0


@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        (("--as", "ou_lane"), Exit.VALIDATION, "has no default"),
        (("--as", "ou_lane", "--certify", "2024-05-31"), Exit.VALIDATION, "is not a window"),
        (
            ("--as", "ou_lane", "--certify", "2024-05-31..2024-04-01"),
            Exit.VALIDATION,
            "not a window",
        ),
        (
            ("--as", "ou_lane", "--certify", "2024-03-29..2024-05-31"),
            Exit.VALIDATION,
            "on or after 2024-03-30",
        ),
        (
            ("--as", "portfolio", "--certify", "2024-04-01..2024-05-31"),
            Exit.PRECONDITION,
            "is taken",
        ),
        (
            ("--as", "Not An Id", "--certify", "2024-04-01..2024-05-31"),
            Exit.VALIDATION,
            "not a hypothesis id",
        ),
    ],
)
def test_a_draft_refuses_what_it_cannot_write(
    runner: CliRunner, loaded: Path, arguments: tuple[str, ...], code: Exit, message: str
) -> None:
    run_screen(runner, loaded, FADE)

    result = drafted(runner, loaded, *arguments)

    assert result.exit_code == code, result.stdout
    assert message in payload(result)["error"] + payload(result).get("remedy", "")


def test_a_draft_needs_a_free_screen_a_result_and_a_response_cell(
    runner: CliRunner, loaded: Path
) -> None:
    window = ("--as", "ou_lane", "--certify", "2024-04-01..2024-05-31")
    nothing = at(runner, loaded, "screen", "draft", "ou_fade", "--cell", CELL, *window, "--json")
    assert (
        nothing.exit_code == Exit.VALIDATION and "no recorded result" in payload(nothing)["error"]
    )
    run_screen(runner, loaded, FADE)
    unknown = at(runner, loaded, "screen", "draft", "ou_fade", "--cell", "nope", *window, "--json")
    assert unknown.exit_code == Exit.VALIDATION and "is not a cell" in payload(unknown)["error"]
    run_screen(runner, loaded)
    lead = at(
        runner, loaded, "screen", "draft", "ou_rev", "--cell", "lead_lag/a>a/1h", *window, "--json"
    )
    assert lead.exit_code == Exit.VALIDATION and "names no rule" in payload(lead)["error"]
    (loaded / "hypotheses" / "taken").mkdir(parents=True)
    taken = drafted(runner, loaded, "--as", "taken", "--certify", "2024-04-01..2024-05-31")
    assert taken.exit_code == Exit.PRECONDITION


def test_a_bound_screen_s_hypothesis_already_exists(runner: CliRunner, registered: Path) -> None:
    bound = {key: value for key, value in FADE.items() if key not in ("window", "costs")}
    run_screen(runner, registered, {**bound, "id": "mr_fade", "hyp": "demo_mr"})

    result = at(
        runner,
        registered,
        "screen",
        "draft",
        "mr_fade",
        "--cell",
        CELL,
        "--as",
        "other",
        "--certify",
        "2024-04-01..2024-05-31",
        "--json",
    )

    assert result.exit_code == Exit.VALIDATION
    assert "is bound to demo_mr" in payload(result)["error"]


def result_of(screen: Screen, cell: str, follower: str = "b") -> ScreenResult:
    return ScreenResult.model_validate(
        {
            "screen": screen.id,
            "sha": "a" * 64,
            "snapshot": "b" * 64,
            "version": "0.14.0+0123456789ab",
            "window": {"start": "2024-01-02", "end": "2024-03-29"},
            "draws": 99,
            "folds": 4,
            "assumption": "sessions are roughly independent of each other",
            "created_at": "2026-10-05T00:00:00+00:00",
            "wall_s": 0.0,
            "peak_mem_gb": 0.0,
            "series": [],
            "cells": [
                {
                    "measure": 0,
                    "id": "response",
                    "key": cell,
                    "params": {"follower": follower, "threshold": 10.0, "horizon": "500ms"},
                    "mean": 1.0,
                    "se": 0.1,
                    "t": 10.0,
                    "p": 0.01,
                    "sessions": 20,
                    "folds": [1.0],
                    "folds_same_sign": 1,
                    "response": {
                        "events": 5,
                        "events_per_day": 1.0,
                        "gross_bp": 5.0,
                        "hurdle_bp": 1.0,
                        "margin_bp": 4.0,
                        "ceiling_bp_day": 4.0,
                        "hit_rate": 0.5,
                        "unfilled": 0,
                        "drift_adjusted_bp": 4.0,
                    },
                }
            ],
            "summary": {"declared": False},
        }
    )


@pytest.mark.parametrize(
    ("legs", "side", "resolution", "types", "mechanism"),
    [
        ({"a": "trade", "b": "quote"}, "with", "trade", ["quote", "trade"], "stat_arb"),
        ({"a": "quote", "b": "quote"}, "with", "quote", ["quote"], "stat_arb"),
    ],
)
def test_the_draft_s_grain_and_mechanism_follow_from_the_cell(
    loaded: Path,
    legs: dict[str, str],
    side: str,
    resolution: str,
    types: list[str],
    mechanism: str,
) -> None:
    spec = Screen.model_validate(
        {
            **OU,
            "id": "grains",
            "legs": {
                "a": {"instrument": INSTRUMENT, "type": legs["a"]},
                "b": {"instrument": "OTHR.SIM", "type": legs["b"]},
            },
            "clock": {"hours": "overlap"},
            "measures": [
                {
                    "id": "response",
                    "trigger": {"leg": "a", "move_bp": [10], "within": "1s"},
                    "followers": "b",
                    "side": side,
                    "horizons": ["500ms"],
                    "latency_ms": 5,
                }
            ],
        }
    )
    result = result_of(spec, "response/a/10bp/1s/b/500ms")
    found = draft._hypothesis(
        find(loaded),
        spec,
        result,
        result.cells[0],
        "grains_lane",
        "1s",
        (date(2024, 4, 1), date(2024, 5, 31)),
    )

    assert (found.resolution, found.data_requirements, found.mechanism) == (
        resolution,
        types,
        mechanism,
    )
    assert found.horizon == "1s"


def test_two_bar_grains_are_not_one_hypothesis(loaded: Path) -> None:
    spec = Screen.model_validate(
        {
            **OU,
            "legs": {
                "a": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"},
                "b": {"instrument": "OTHR.SIM", "type": "bar", "resolution": "1m"},
            },
            "measures": [{**FADE["measures"][0], "followers": "b"}],
        }
    )
    result = result_of(spec, "response/a/40bp/1h/b/1h")

    with pytest.raises(ValidationError, match="reads one bar size"):
        draft._hypothesis(
            find(loaded),
            spec,
            result,
            result.cells[0],
            "x_lane",
            "1h",
            (date(2024, 4, 1), date(2024, 5, 31)),
        )


def test_a_perpetual_in_the_universe_brings_its_funding(
    loaded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.nautilus.backtest.conftest import perpetual

    monkeypatch.setattr(draft, "current_definitions", lambda ws: {INSTRUMENT: perpetual()})
    spec = Screen.model_validate({**FADE, "costs": None})
    result = result_of(spec, CELL, follower="a")
    found = draft._hypothesis(
        find(loaded),
        spec,
        result,
        result.cells[0],
        "perp_lane",
        "1h",
        (date(2024, 4, 1), date(2024, 5, 31)),
    )

    assert found.data_requirements == ["bar", "funding"]


def test_a_derived_or_book_leg_is_drafted_by_hand(loaded: Path) -> None:
    spec = Screen.model_validate(
        {
            **FADE,
            "legs": {**FADE["legs"], "b": {"instrument": INSTRUMENT, "type": "book"}},
            "derived": {"s": {"basket": {"a": 0.5, "b": 0.5}}},
        }
    )
    with pytest.raises(ValidationError, match="is a derived leg"):
        draft._refuse_untradable(spec, ("a", "s"))
    with pytest.raises(ValidationError, match="read as a book"):
        draft._refuse_untradable(spec, ("a", "b"))
