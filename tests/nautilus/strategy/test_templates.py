"""The shipped stubs are true: they import what the API exports and they run.

`strategy_sleeve.py` and `strategy_modifier.py` are what every hypothesis starts from and
what the research loop edits, so a name that has drifted out of the strategy API would be
found by the first baseline card rather than here. They are rendered and executed against
a real engine instead.
"""

from __future__ import annotations

import pytest

from kanso.nautilus.strategy import KansoConfig, KansoModifier, KansoStrategy
from kanso.workspace import TEMPLATES

from .conftest import DEMO


def render(name: str, **values: str) -> dict[str, object]:
    """Substitute a stub's placeholders and execute it as the workspace would import it."""
    source = (TEMPLATES / name).read_text()
    for key, value in values.items():
        source = source.replace("{{" + key + "}}", value)
    assert "{{" not in source
    namespace: dict[str, object] = {}
    exec(compile(source, name, "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture
def sleeve() -> dict[str, object]:
    return render("strategy_sleeve.py", hyp_id="demo_mr")


@pytest.fixture
def modifier() -> dict[str, object]:
    return render("strategy_modifier.py", hyp_id="demo_filter", construct="filter", host="demo_mr")


def test_the_sleeve_stub_subclasses_the_strategy_api(sleeve) -> None:
    assert issubclass(sleeve["Config"], KansoConfig)
    assert issubclass(sleeve["Strategy"], KansoStrategy)
    assert sleeve["Strategy"].config_cls is sleeve["Config"]


def test_the_modifier_stub_subclasses_the_modifier_api(modifier) -> None:
    assert issubclass(modifier["Modifier"], KansoModifier)
    assert modifier["Modifier"].construct == "filter"
    assert modifier["Modifier"].config_cls is modifier["Config"]


def test_the_stubs_run_together_and_the_baseline_trades_nothing(backtest, sleeve, modifier):
    config = sleeve["Config"](
        hyp_id="demo_mr",
        universe=("DEMO.XNAS",),
        resolution="1m",
        data_requirements=("bar",),
        capital=100_000.0,
        max_position_pct=20.0,
        max_leverage=1.0,
    )
    strategy = sleeve["Strategy"](config)
    attached = modifier["Modifier"](
        modifier["Config"](host_strategy_id=strategy.host_names[1], hyp_id="demo_filter")
    )

    run = backtest(strategy, [attached])

    assert run.strategy.data_time > 0
    assert run.strategy.intents == ()


def test_the_modifier_stub_is_neutral(backtest, sleeve, modifier) -> None:
    class Trades(sleeve["Strategy"]):
        def on_bar(self, bar_: object) -> None:
            if self.data_time and not self.intents:
                self.submit_entry(DEMO, "BUY", qty=10)

    config = sleeve["Config"](universe=("DEMO.XNAS",), resolution="1m", capital=100_000.0)
    attached = modifier["Modifier"](modifier["Config"](host_strategy_id="Trades"))

    alone = backtest(Trades(config))
    with_stub = backtest(Trades(config), [attached])

    assert with_stub.strategy.intents == alone.strategy.intents
    assert len(alone.strategy.intents) == 1
