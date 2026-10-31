"""`balance_at_quote` marks every open position at the mid of its name's quote in force, so a
print outside the quote moves `balance` and not it; with no valid quote at the last trade, or
at cost before any, never at that quote's mid; and the two code paths read the same number on
every point.

The tick sleeve buys 100 at market on its first point and holds them, writing down both readers
on every quote and print it handles. Instants are milliseconds after 15:00 UTC on 2024-03-04, a
point's `ts_event` is its `ts_init`, and the venue fills by the tape (`limit_fill:
print_through`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import backtest, session
from tests.replay.conftest import FORWARD, INSTRUMENT, bars, hypothesis, instrument, request_for

MS = 1_000_000
T0 = midnight_ns(date(2024, 3, 4)) + 15 * 3_600 * 1_000 * MS
DEMO = InstrumentId.from_str(INSTRUMENT)

HOLDS = """
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

RECORD = Path(%r)


class Strategy(KansoStrategy):
    \"\"\"Buys 100 at market on its first point and holds them, and writes down, on every quote and
    print it handles, the point's kind, both balances and what it holds.\"\"\"

    config_cls = KansoConfig

    def on_start(self):
        self.bought = False

    def _point(self, kind, tick):
        if not self.bought:
            self.bought = self.submit_entry(tick.instrument_id, "BUY", qty=100) is not None
        with RECORD.open("a") as out:
            out.write(
                f"{kind} {self.balance.hex()} {self.balance_at_quote.hex()} "
                f"{self.held(tick.instrument_id)}\\n"
            )

    def on_quote_tick(self, tick):
        self._point("quote", tick)

    def on_trade_tick(self, tick):
        self._point("print", tick)
"""

_printed = [0]


def q(bid: float, ask: float, ms: int) -> QuoteTick:
    """A quote of the demo name, a thousand shown each side."""
    ns = T0 + ms * MS
    return QuoteTick(
        DEMO,
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_int(1_000),
        Quantity.from_int(1_000),
        ns,
        ns,
    )


def t(px: float, size: int, ms: int) -> TradeTick:
    """A print of the demo name, with no aggressor."""
    _printed[0] += 1
    ns = T0 + ms * MS
    return TradeTick(
        DEMO,
        Price(px, 2),
        Quantity.from_int(size),
        AggressorSide.NO_AGGRESSOR,
        TradeId(f"Q{_printed[0]}"),
        ns,
        ns,
    )


Read = tuple[str, float, float, float]
"""One point the sleeve handled: its kind, `balance`, `balance_at_quote` and what it held."""


def both_paths(points: list[object], record: Path) -> dict[str, list[Read]]:
    """The holding sleeve over `points` on the backtest engine and on the node, under the print
    rule: what each path read, once both are seen to have bought the 100 alike, at the offer."""
    hyp = hypothesis(
        resolution="tick", horizon="1d", data_requirements=["quote", "trade"], universe=[INSTRUMENT]
    )
    groups = [
        group
        for kind in (QuoteTick, TradeTick)
        if (group := tuple(point for point in points if isinstance(point, kind)))
    ]
    read: dict[str, list[Read]] = {}
    fills = {}
    for path in ("engine", "node"):
        written = record.with_suffix(f".{path}.txt")
        request = request_for(hyp=hyp, source=(HOLDS % str(written)).encode())
        model = dict(request.venue_model)
        model["costs"] = {
            **dict(model["costs"]),  # type: ignore[arg-type]
            "limit_fill": "print_through",
        }
        subject = replace(request, venue_model=model)
        if path == "engine":
            fills[path] = backtest.execute(subject, [instrument()], groups).run.fills
        else:
            fills[path] = session.run_node(subject, [instrument()], groups).result.run.fills
        read[path] = [
            (kind, float.fromhex(balance), float.fromhex(at_quote), float(held))
            for kind, balance, at_quote, held in (
                line.split() for line in written.read_text().splitlines()
            )
        ]
    assert fills["node"] == fills["engine"]
    assert [(fill.qty, fill.px) for fill in fills["engine"]] == [(100.0, 10.02)]
    return read


def test_a_print_outside_the_quote_moves_the_balance_and_not_the_balance_at_quote(
    tmp_path: Path,
) -> None:
    """Bought at the 10.02 offer, the sleeve holds 100. A print of one share at 9.50, under the
    9.98 bid, marks `balance` 50.00 down and leaves `balance_at_quote` at the 10.00 mid; prints
    at the next quote's 10.03 offer and at 11.00 mark `balance` 2.00 and 99.00 over that quote's
    10.01 mid and leave `balance_at_quote` there; at every quote the two are one number. Both
    paths read every one of them alike."""
    read = both_paths(
        [
            q(9.98, 10.02, 10),
            q(9.98, 10.02, 20),
            t(9.50, 1, 30),
            q(9.99, 10.03, 40),
            t(10.03, 100, 50),
            t(11.00, 1, 60),
            q(9.99, 10.01, 70),
        ],
        tmp_path / "outside",
    )

    assert read["node"] == read["engine"]
    assert all(held == 100.0 for *_, held in read["engine"])
    assert all(
        balance.hex() == at_quote.hex()
        for kind, balance, at_quote, _ in read["engine"]
        if kind == "quote"
    )
    _, ten, under, ten_one, at_offer, over, back = read["engine"]
    assert under[2] == ten[2]
    assert under[1] == pytest.approx(ten[1] - 50.0, abs=1e-9)
    assert at_offer[2] == over[2] == ten_one[2]
    assert at_offer[1] == pytest.approx(ten_one[1] + 2.0, abs=1e-9)
    assert over[1] == pytest.approx(ten_one[1] + 99.0, abs=1e-9)
    assert back[2] == ten[2]


def test_with_no_valid_quote_in_force_the_balance_at_quote_marks_at_the_last_trade(
    tmp_path: Path,
) -> None:
    """A locked quote, a crossed one and one that shows no bid are not `ask > bid > 0`, so on
    each `balance_at_quote` marks the 100 held at the last trade, where `balance` marks them at
    that quote's own mid: on the locked quote, before any trade, at the 10.02 they cost against
    its 10.00; on the crossed one at the 9.60 printed before it against its 10.00; on the
    one-sided one at 9.70 against its 5.01, half the offer. On the print after each the two
    are one number. The next valid quote is marked at its mid again, and a print outside it
    moves `balance` alone."""
    read = both_paths(
        [
            q(9.98, 10.02, 10),
            q(9.98, 10.02, 20),
            q(10.00, 10.00, 30),
            t(9.60, 1, 40),
            q(10.01, 9.99, 50),
            t(9.70, 1, 60),
            q(0.00, 10.02, 70),
            t(9.80, 1, 80),
            q(9.99, 10.01, 90),
            t(9.40, 1, 100),
        ],
        tmp_path / "invalid",
    )

    assert read["node"] == read["engine"]
    _, ten, *invalid, valid, stray = read["engine"]
    assert [kind for kind, *_ in invalid] == ["quote", "print"] * 3
    assert all(held == 100.0 for *_, held in invalid)
    quoted, printed = invalid[0::2], invalid[1::2]
    assert [balance for _, balance, _, _ in quoted] == pytest.approx(
        [ten[1], ten[1], ten[1] - 499.0], abs=1e-9
    )
    assert [at_quote for _, _, at_quote, _ in quoted] == pytest.approx(
        [ten[2] + 2.0, ten[2] - 40.0, ten[2] - 30.0], abs=1e-9
    )
    assert all(balance.hex() == at_quote.hex() for _, balance, at_quote, _ in printed)
    assert [balance for _, balance, _, _ in printed] == pytest.approx(
        [ten[1] - 40.0, ten[1] - 30.0, ten[1] - 20.0], abs=1e-9
    )
    assert valid[1] == valid[2] == ten[1]
    assert stray[2] == valid[2]
    assert stray[1] == pytest.approx(ten[1] - 60.0, abs=1e-9)


BUYS_AND_SELLS = """
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

RECORD = Path(%r)


class Strategy(KansoStrategy):
    \"\"\"Buys the trough and sells the peak of the saw-tooth, and writes down both balances and
    what it holds on every bar.\"\"\"

    config_cls = KansoConfig

    def on_start(self):
        self.closes = []

    def on_bar(self, bar):
        name = bar.bar_type.instrument_id
        with RECORD.open("a") as out:
            out.write(f"{self.balance.hex()} {self.balance_at_quote.hex()} {self.held(name)}\\n")
        self.closes.append(float(bar.close))
        if len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        if first > second > third and self.held(name) == 0.0:
            self.submit_entry(name, "BUY", notional=5_000.0)
        elif first < second < third and self.held(name) > 0.0:
            self.submit_exit(name)
"""


def test_a_sleeve_handed_no_quotes_reads_its_balance_at_quote_as_its_balance(
    tmp_path: Path,
) -> None:
    """A bar sleeve has no quote to mark at, so on every bar of both paths, holding or flat, its
    `balance_at_quote` is its `balance`: each position at its last close."""
    read: dict[str, list[str]] = {}
    for path in ("engine", "node"):
        written = tmp_path / f"{path}.txt"
        request = request_for(source=(BUYS_AND_SELLS % str(written)).encode())
        groups = [tuple(bars(FORWARD))]
        if path == "engine":
            backtest.execute(request, [instrument()], groups)
        else:
            session.run_node(request, [instrument()], groups)
        read[path] = written.read_text().splitlines()

    assert read["node"] == read["engine"]
    rows = [line.split() for line in read["engine"]]
    assert sum(float(held) > 0.0 for _, _, held in rows) >= 5
    assert all(balance == at_quote for balance, at_quote, _ in rows)
