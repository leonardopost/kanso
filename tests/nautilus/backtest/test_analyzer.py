"""The engine a card runs in has its portfolio's analyzer record no closed position.

kanso reads nothing the analyzer holds, and the analyzer copied its whole series to record
each close, so a card's time grew with the square of its trades (`kanso.nautilus.facts`).
Both halves are under test: the analyzer of a card's engine keeps nothing, and a run whose
analyzer records every close, as the engine's own does, is extracted to the same card.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kanso.nautilus import backtest, facts
from kanso.nautilus.backtest import RunRequest, execute

from .conftest import POSTER, RESEARCH, bars, instrument, tick_groups, tick_hypothesis


def test_a_card_s_engine_records_no_closed_position() -> None:
    trades, handed, kept, _ = facts._probe_recorded_closes(recorded=False)

    assert trades > 0
    assert (handed, kept) == (trades, 0), "every close reached the analyzer, and none was kept"


def _recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build every engine with the analyzer's own `record_trade`, as before kanso replaced it."""
    built = backtest.engine_for

    def engine_for() -> Any:
        engine = built()
        del engine.portfolio.analyzer.record_trade
        return engine

    monkeypatch.setattr(backtest, "engine_for", engine_for)


@pytest.mark.parametrize("feed", ["daily bars", "a book and its prints"])
def test_a_card_is_the_same_whether_its_analyzer_records_its_closes_or_not(
    feed: str, request_for: Callable[..., RunRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    if feed == "daily bars":
        request, groups = request_for(), [tuple(bars(RESEARCH))]
    else:
        request, groups = request_for(source=POSTER, hypothesis_=tick_hypothesis()), tick_groups()
    unrecorded = execute(request, [instrument()], groups)
    _recording(monkeypatch)
    recorded = execute(request, [instrument()], groups)

    assert unrecorded.run.trades, "the sleeve closed positions for the analyzer to record"
    assert repr(recorded.run) == repr(unrecorded.run)
    assert recorded.intents == unrecorded.intents
