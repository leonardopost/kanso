"""Deterministic, deadline-free property testing and the lane-directory fixture."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile(
    "criteria",
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile("criteria")


@pytest.fixture
def lane(tmp_path: Path) -> Path:
    """A lane directory holding exactly the three scoped files."""
    directory = tmp_path / "runs" / "op" / "demo_mr"
    directory.mkdir(parents=True)
    (directory / "hypothesis.yaml").write_text("schema: 1\n", encoding="utf-8")
    (directory / "program.md").write_text("# program\n", encoding="utf-8")
    (directory / "strategy.py").write_text(
        "from kanso.nautilus.strategy import KansoStrategy\n\n\n"
        "class Strategy(KansoStrategy):\n    pass\n",
        encoding="utf-8",
    )
    return directory
