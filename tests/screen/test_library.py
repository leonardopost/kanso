"""The measure library: every shipped measure, its ranges, and the version a result records."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from kanso import __version__
from kanso.errors import ValidationError
from kanso.screen import catalogue, check, screen_version
from kanso.screen.library import LIBRARY, SOURCE
from tests.schemas.test_screen import BASE, CATCH_UP, CONDITION, TESTED, build, conditioned


def test_every_measure_a_screen_can_name_is_shipped_under_its_own_id() -> None:
    assert sorted(catalogue()) == ["catch_up", "condition", "lead_lag", "response"]
    assert sorted(path.stem for path in LIBRARY.glob("*.yaml")) == [
        "catch_up",
        "condition",
        "lead_lag",
        "response",
    ]


def test_a_misfiled_measure_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "lead_lag.yaml").write_text(
        (LIBRARY / "response.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr("kanso.screen.library.LIBRARY", tmp_path)
    catalogue.cache_clear()
    try:
        with pytest.raises(ValidationError, match="declares id 'response', so it is misfiled"):
            catalogue()
    finally:
        catalogue.cache_clear()


def test_the_version_is_the_package_s_and_a_digest_of_the_library() -> None:
    assert re.fullmatch(rf"{re.escape(__version__)}\+[0-9a-f]{{12}}", screen_version())


def test_the_version_moves_with_the_modules_that_measure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A copy of the package is the same build; one byte more in the module that scores a
    # z-trigger is another, whose results are measured again rather than returned as stored.
    copy = tmp_path / "screen"
    shutil.copytree(SOURCE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr("kanso.screen.library.SOURCE", copy)
    monkeypatch.setattr("kanso.screen.library.LIBRARY", copy / "library")
    shipped = screen_version()
    screen_version.cache_clear()
    try:
        assert screen_version() == shipped
        (copy / "response.py").write_bytes((copy / "response.py").read_bytes() + b"\n")
        screen_version.cache_clear()
        assert screen_version() != shipped
    finally:
        screen_version.cache_clear()


def test_a_screen_inside_every_range_is_admitted() -> None:
    check(build())
    check(build(measures=[*BASE["measures"], CATCH_UP]))


@pytest.mark.parametrize(
    ("index", "change", "message"),
    [
        (0, {"lags": ["2h", "-30h"]}, r"measures\.0\.lag: 108000s is outside the lead_lag range"),
        (0, {"lags": [f"{n}s" for n in range(1, 66)]}, r"measures\.0\.lags: 65 is outside"),
        (1, {"horizons": ["2d"]}, r"measures\.1\.horizon: 172800s is outside"),
        (1, {"latency_ms": 120000}, r"measures\.1\.latency_ms: 120000 is outside"),
        (
            1,
            {"trigger": {"leg": "btc", "move_bp": [0.01], "within": "1s"}},
            r"measures\.1\.move_bp: 0\.01 is outside",
        ),
        (
            1,
            {"trigger": {"leg": "btc", "z": [2], "lookback": "60d"}},
            r"measures\.1\.lookback: 5\.184e\+06s is outside",
        ),
    ],
)
def test_a_parameter_outside_its_range_is_refused_naming_it(
    index: int, change: dict[str, Any], message: str
) -> None:
    measures = [dict(measure) for measure in BASE["measures"]]
    measures[index] = {**measures[index], **change}
    with pytest.raises(ValidationError, match=message) as refused:
        check(build(measures=measures))
    assert "kanso/screen/library/" in (refused.value.remedy or "")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"within": "2d"}, r"measures\.0\.within: 172800s is outside the catch_up range"),
        ({"beta_over": "2d"}, r"measures\.0\.beta_over: 172800s is outside"),
        ({"max_hold": "2d"}, r"measures\.0\.max_hold: 172800s is outside"),
        ({"enter_bp": [0.01]}, r"measures\.0\.enter_bp: 0\.01 is outside"),
        ({"stop": [200]}, r"measures\.0\.stop: 200 is outside"),
        ({"latency_ms": 120000}, r"measures\.0\.latency_ms: 120000 is outside"),
    ],
)
def test_a_catch_up_parameter_outside_its_range_is_refused_naming_it(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        check(build(measures=[{**CATCH_UP, **change}]))


def test_a_condition_inside_every_range_is_admitted() -> None:
    check(conditioned())
    blocks = {**CONDITION, "null_by": "sign_flip", "min_shift": None, "block": 252}
    check(
        conditioned(
            {**blocks, "mde_power": 0.8, "tests": {"up": {**TESTED, "hold": {"sessions": 5}}}}
        )
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"latency_ms": 0}, r"measures\.0\.latency_ms: 0 is outside the condition range \[1\.0, "),
        ({"latency_ms": 90_000_000}, r"measures\.0\.latency_ms: 9e\+07 is outside"),
        ({"min_shift": 2521}, r"measures\.0\.min_shift: 2521 is outside"),
        (
            {"null_by": "sign_flip", "min_shift": None, "block": 253},
            r"measures\.0\.block: 253 is outside",
        ),
        ({"demean": [{"turn_of_month": [11, 1]}]}, r"measures\.0\.turn_of_month: 11 is outside"),
        ({"mde_power": 0.3}, r"measures\.0\.mde_power: 0\.3 is outside"),
        (
            {"tests": {"up": {**TESTED, "hold": {"sessions": 300}}}},
            r"hold_sessions: 300 is outside",
        ),
        ({"tests": {f"t{n}": TESTED for n in range(65)}}, r"measures\.0\.tests: 65 is outside"),
    ],
)
def test_a_condition_parameter_outside_its_range_is_refused_naming_it(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        check(conditioned({**CONDITION, **change}))


def test_more_controls_than_the_range_admits_are_refused() -> None:
    signals = {
        f"c{n}": {"type": "house_calendar", "field": "flag", "instrument": "MARA.XNAS"}
        for n in range(17)
    }
    screen = build(
        signals=signals
        | {"regime": {"type": "house_state", "field": "level", "instrument": "MARA.XNAS"}},
        groups={**BASE["groups"], "names": ["mara", "riot"]},
        measures=[{**CONDITION, "controls": list(signals)}],
    )

    with pytest.raises(ValidationError, match=r"measures\.0\.controls: 17 is outside"):
        check(screen)
