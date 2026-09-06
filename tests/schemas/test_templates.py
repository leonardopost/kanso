"""The shipped workspace templates validate against the schemas that read them."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.schemas import (
    Envelope,
    Hypothesis,
    InstrumentsFile,
    ModelsFile,
    Portfolio,
    load_yaml,
)

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "kanso" / "templates"


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("demo/hypothesis.yaml", Hypothesis),
        ("portfolio.yaml", Portfolio),
        ("models.yaml", ModelsFile),
        ("envelope.yaml", Envelope),
        ("instruments.yaml", InstrumentsFile),
    ],
)
def test_a_template_validates(name: str, model: type) -> None:
    assert load_yaml(model, TEMPLATES / name) is not None


def test_the_demo_hypothesis_is_the_one_the_demo_workspace_runs() -> None:
    hyp = load_yaml(Hypothesis, TEMPLATES / "demo" / "hypothesis.yaml")
    assert hyp.id == "demo_mr"
    assert hyp.universe == ["DEMO.SIM"]
    assert hyp.horizon == "30m"
    assert hyp.resolution == "1m"
    assert hyp.data_requirements == ["bar"]
    assert hyp.costs is not None
    assert hyp.costs.spread == "fixed_bps"
    assert hyp.windows.certification.start >= hyp.windows.research.end


def test_the_routing_template_matches_the_shipped_defaults() -> None:
    from kanso.schemas import ROUTING_DEFAULTS

    register = load_yaml(ModelsFile, TEMPLATES / "models.yaml")
    assert register.routes() == dict(ROUTING_DEFAULTS)
