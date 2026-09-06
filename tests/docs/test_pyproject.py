"""The project configuration selects nothing that does not exist.

A pytest marker no test carries, deselected by default, is a suite that quietly promises
a class of test it does not have. Every marker declared is carried by at least one test.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_declared_marker_is_carried_by_a_test() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        options = tomllib.load(handle)["tool"]["pytest"]["ini_options"]
    declared = [marker.split(":")[0].strip() for marker in options.get("markers", [])]
    carried = set()
    for path in (ROOT / "tests").rglob("*.py"):
        carried.update(re.findall(r"pytest\.mark\.([a-z_]+)", path.read_text(encoding="utf-8")))
    for marker in declared:
        assert marker in carried, f"the marker {marker!r} is declared and no test carries it"
    assert "-m" not in options.get("addopts", "").split()
