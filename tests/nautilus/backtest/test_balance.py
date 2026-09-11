"""The balance a sleeve sizes against is the equity the runner strikes, at every period end.

The harness keeps it from its own fills while the run is going, with the runner's cost
arithmetic; the runner strikes the equity from the same fills after the run. A sleeve that
reads one number and is measured on another would be sized against money it does not have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.nautilus.backtest import execute

from .conftest import RESEARCH, bars, hypothesis, instrument, quotes

PROBE = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    \"\"\"Takes the room every six sessions, and writes down the balance it read first.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        step = self.seen % 6
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{bar.ts_init} {self.balance!r} {int(step in (1, 4))}\\n")
        if step == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY")
        elif step == 4:
            self.submit_exit(bar.bar_type.instrument_id)
"""

FIXED = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "fixed_bps", "fixed_bps": 4.0}
QUOTED = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "quotes"}


@pytest.mark.parametrize("quoted", [False, True], ids=["fixed spread", "quoted spread"])
def test_the_balance_read_before_acting_is_the_equity_struck_at_that_period_end(
    tmp_path: Path, request_for, quoted: bool
) -> None:
    """Compared on every session the probe does not trade on: a session it trades on is
    struck after the fills its own orders made, which the balance it read before them
    cannot hold."""
    hyp = hypothesis(
        data_requirements=("bar", "quote") if quoted else ("bar",),
        costs=QUOTED if quoted else FIXED,
    )
    record = tmp_path / "balance.txt"
    request = request_for(
        RESEARCH,
        source=PROBE,
        hypothesis_=hyp,
        quotes_available=quoted,
        overrides={"record": str(record)},
    )
    groups: list[tuple[object, ...]] = [tuple(bars(RESEARCH))]
    if quoted:
        groups.append(tuple(quotes(RESEARCH)))

    card = execute(request, [instrument()], groups).run
    struck = dict(zip(card.period_ends_ns, card.equity, strict=True))
    read = [line.split() for line in record.read_text().splitlines()]
    compared = [(int(ts), float(balance)) for ts, balance, acted in read if acted == "0"]

    assert card.fills
    assert all(fill.cost > 0 for fill in card.fills)
    assert len(compared) >= 15
    for ts, balance in compared:
        assert balance == pytest.approx(struck[ts], rel=1e-12)
