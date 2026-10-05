"""The measure library: every shipped measure, its ranges, and the version a result records."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kanso import __version__
from kanso.errors import ValidationError
from kanso.screen import catalogue, check, screen_version
from kanso.screen.library import LIBRARY
from tests.schemas.test_screen import BASE, build


def test_every_measure_a_screen_can_name_is_shipped_under_its_own_id() -> None:
    assert sorted(catalogue()) == ["lead_lag", "response"]
    assert sorted(path.stem for path in LIBRARY.glob("*.yaml")) == ["lead_lag", "response"]


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


def test_a_screen_inside_every_range_is_admitted() -> None:
    check(build())


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
