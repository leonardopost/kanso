"""Derived legs through `kanso screen run`: a fitted spread, a basket and a gap of two names.

A second synthetic name is loaded beside the demo's, on its own seed, so the two paths share
nothing but their calendar: a spread of the two is fitted, and the cells that read it are
masked or flagged as the fit requires.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from .conftest import INSTRUMENT, SPEC, at
from .test_screen_run import OU, run_screen

OTHER = "OTHR.SIM"


@pytest.fixture
def paired(runner: CliRunner, loaded: Path) -> Path:
    """The loaded workspace with a second, independent hourly name and a fresh snapshot."""
    entries = yaml.safe_load((loaded / "instruments.yaml").read_text(encoding="utf-8"))
    entries[OTHER] = {**entries[INSTRUMENT], "nautilus_id": OTHER}
    (loaded / "instruments.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    spec = loaded / "other.yaml"
    spec.write_text(yaml.safe_dump({**SPEC, "instruments": ["OTHR"], "seed": 11}), encoding="utf-8")
    assert (
        at(runner, loaded, "data", "instruments", "resolve", "--as-of", "2024-01-02").exit_code == 0
    )
    assert (
        at(runner, loaded, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0
    )
    assert at(runner, loaded, "data", "snapshot").exit_code == 0
    return loaded


def pair(fit: str, measures: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **OU,
        "id": f"pair_{fit}",
        "legs": {
            "a": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"},
            "b": {"instrument": OTHER, "type": "bar", "resolution": "1h"},
        },
        "derived": {
            "s": {"spread": {"long": "a", "short": "b", "hedge": "ols", "fit": fit}},
            "both": {"basket": {"a": 0.5, "b": 0.5}},
            "g": {"gap": {"a": "a", "b": "b"}},
        },
        "costs": {
            "SIM": {"commission_bps": 0.5, "slippage_bps": 1, "spread": "fixed_bps", "fixed_bps": 2}
        },
        "measures": measures,
    }


SELF = {"id": "lead_lag", "from": "s", "to": "s", "estimator": "grid", "lags": ["1h"]}


def cell(document: dict[str, Any], key: str, runner: CliRunner, root: Path) -> dict[str, Any]:
    result = at(runner, root, "screen", "show", document["screen"], "--cell", key, "--json")
    assert result.exit_code == 0, result.stdout
    found: dict[str, Any] = yaml.safe_load(result.stdout)
    return found


def test_a_spread_fitted_on_its_first_fold_is_judged_on_the_rest(
    runner: CliRunner, paired: Path
) -> None:
    window = run_screen(runner, paired, pair("window", [SELF]))
    first = run_screen(runner, paired, pair("first_fold", [SELF]))

    whole = cell(window, "lead_lag/s>s/1h", runner, paired)
    later = cell(first, "lead_lag/s>s/1h", runner, paired)
    assert whole["in_sample_fit"] is True and later["in_sample_fit"] is False
    assert later["sessions"] < whole["sessions"]
    assert later["folds"][0] is None and whole["folds"][0] is not None


def test_a_basket_and_a_gap_are_read_as_levels_of_their_legs(
    runner: CliRunner, paired: Path
) -> None:
    measures = [
        {"id": "lead_lag", "from": "both", "to": "a", "estimator": "grid", "lags": ["1h"]},
        {"id": "lead_lag", "from": "g", "to": "g", "estimator": "hy", "lags": ["1h"]},
    ]
    document = run_screen(runner, paired, pair("window", measures))

    assert document["cells"] == 2
    gap = cell(document, "lead_lag/g>g/1h", runner, paired)
    assert gap["sessions"] > 50 and gap["mean"] < 0


def test_fading_the_spread_pays_both_legs_round_trips_in_their_shares(
    runner: CliRunner, paired: Path
) -> None:
    fade = {
        "id": "response",
        "trigger": {"leg": "s", "z": [1.5], "lookback": "1d"},
        "followers": "s",
        "side": "against",
        "horizons": ["2h"],
        "latency_ms": 0,
    }
    document = run_screen(runner, paired, pair("window", [fade]))

    found = cell(document, "response/s/1.5z/1d/s/2h", runner, paired)
    assert found["in_sample_fit"] is True
    assert found["response"]["events"] > 0 and found["response"]["hurdle_bp"] > 5.0
    later = cell(
        run_screen(runner, paired, pair("first_fold", [fade])), found["key"], runner, paired
    )
    assert later["in_sample_fit"] is False and later["sessions"] < found["sessions"]


def test_data_held_but_never_frozen_is_frozen_by_the_screen(
    runner: CliRunner, loaded: Path
) -> None:
    from kanso.data.snapshot import snapshots
    from kanso.workspace import find

    entries = yaml.safe_load((loaded / "instruments.yaml").read_text(encoding="utf-8"))
    entries[OTHER] = {**entries[INSTRUMENT], "nautilus_id": OTHER}
    (loaded / "instruments.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    spec = loaded / "other.yaml"
    spec.write_text(yaml.safe_dump({**SPEC, "instruments": ["OTHR"], "seed": 11}), encoding="utf-8")
    assert (
        at(runner, loaded, "data", "instruments", "resolve", "--as-of", "2024-01-02").exit_code == 0
    )
    assert (
        at(runner, loaded, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0
    )
    taken = len(snapshots(find(loaded)))

    document = run_screen(runner, loaded, pair("window", [SELF]))

    assert document["cells"] == 1
    assert len(snapshots(find(loaded))) == taken + 1
