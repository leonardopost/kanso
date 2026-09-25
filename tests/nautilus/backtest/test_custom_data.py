"""A custom data requirement, read from the catalog, carried into a card and handed over.

A hypothesis may require a registered custom type — `corporate_action` is kanso's own. The
parent reads the type's points for the universe over the window, the child is handed them
by pickle, and the sleeve receives each at the instant it became public, as the type
itself, without a subscription of its own: a researched `strategy.py` may not import the
class a subscription would need.
"""

from __future__ import annotations

from pathlib import Path

from nautilus_trader.model.identifiers import InstrumentId

from kanso.criteria.run import midnight_ns
from kanso.data.types import CorporateAction
from kanso.nautilus.backtest import run, run_subprocess

from .conftest import (
    CLOSE_NS,
    INSTRUMENT,
    RESEARCH,
    SECOND_NS,
    bars,
    catalog,
    hypothesis,
    instrument,
)

DAY_NS = 86_400 * SECOND_NS

DIVIDEND_TAKER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Buys a share for every cent of dividend declared, the moment it is declared."""

    config_cls = KansoConfig

    def on_bar(self, bar) -> None:
        return

    def on_data(self, data) -> None:
        if data.kind == "dividend":
            self.submit_entry(data.instrument_id, "BUY", qty=round(data.cash * 100))
'''


def declared(cents: int, day: int) -> CorporateAction:
    """A dividend of `cents` declared at the close of the research window's `day`-th day."""
    at = midnight_ns(RESEARCH[0]) + day * DAY_NS + CLOSE_NS
    return CorporateAction(
        ts_event=at,
        ts_init=at + SECOND_NS,
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        kind="dividend",
        ratio=1.0,
        cash=cents / 100,
        currency="USD",
        ex_date_ns=at + 14 * DAY_NS,
    )


def test_a_card_hands_the_sleeve_every_declaration_its_window_holds(
    tmp_path: Path, request_for
) -> None:
    held = catalog(
        tmp_path / "catalog", [*bars(RESEARCH), declared(24, 3), declared(26, 10)], [instrument()]
    )
    request = request_for(
        source=DIVIDEND_TAKER,
        hypothesis_=hypothesis(data_requirements=("bar", "corporate_action")),
    )

    carded = run_subprocess(request, held, tmp_path)
    in_process = run(request, held)

    assert not carded.crashed, carded.traceback_tail
    assert [(fill.side, fill.qty) for fill in carded.run.fills] == [("BUY", 24.0), ("BUY", 26.0)]
    assert carded.run.fills == in_process.run.fills


def test_a_hypothesis_that_does_not_require_the_type_is_not_handed_it(
    tmp_path: Path, request_for
) -> None:
    """The requirement is what is loaded and what is subscribed; the catalog is not."""
    held = catalog(tmp_path / "catalog", [*bars(RESEARCH), declared(24, 3)], [instrument()])

    carded = run_subprocess(request_for(source=DIVIDEND_TAKER), held, tmp_path)

    assert not carded.crashed, carded.traceback_tail
    assert carded.run.fills == ()
