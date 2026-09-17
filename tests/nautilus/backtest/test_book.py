"""The book policy, applied once by the runner at every period end of the extraction.

Every run here crosses a month boundary on the synthetic saw-tooth, so the reset has an end
to fire on, and every number asserted is read back off the card's own fills and holdings and
compared with the arithmetic in `kanso.nautilus.costs` by hand.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price

from kanso.criteria import drawdown_pct
from kanso.criteria.run import NS_PER_DAY, Fill, midnight_ns
from kanso.nautilus.backtest import RunRequest, _equity, execute, run
from kanso.nautilus.costs import NS_PER_YEAR, carry
from kanso.schemas import Hypothesis

from .conftest import (
    CAPITAL,
    INSTRUMENT,
    SNAPSHOT,
    bars,
    catalog,
    hypothesis,
    instrument,
    quotes,
    venue_model,
)

QUARTER = (date(2024, 1, 1), date(2024, 3, 31))
"""Three month ends, so a surplus is set aside once and drawn on once."""

JANUARY_ENDS = 31
"""Period ends before the first of February: one per calendar day of January."""

HOLDER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    side: str = "BUY"
    notional: float = 50_000.0


class Strategy(KansoStrategy):
    """Enters once on the first bar and holds to the end of the window."""

    config_cls = Config

    def on_start(self):
        self.entered = False

    def on_bar(self, bar):
        if not self.entered:
            self.entered = True
            self.submit_entry(
                bar.bar_type.instrument_id,
                self.kanso_config.side,
                notional=self.kanso_config.notional,
            )
'''


def booked(
    book: dict[str, object] | None,
    *,
    max_position_pct: float = 100.0,
    max_leverage: float = 1.0,
) -> Hypothesis:
    """The demo hypothesis over a quarter, with a book policy and room for a full position."""
    fields = hypothesis(max_leverage=max_leverage).model_dump(by_alias=True, mode="json")
    fields["risk_limits"]["max_position_pct"] = max_position_pct
    fields["windows"] = {
        "research": {"start": QUARTER[0].isoformat(), "end": QUARTER[1].isoformat()},
        "certification": {"start": "2024-04-08", "end": "2024-04-30"},
        "forward": {"start": "2024-05-01"},
    }
    if book is not None:
        fields["book"] = book
    return Hypothesis.model_validate(fields)


@pytest.fixture
def quarter(tmp_path: Path) -> Path:
    return catalog(tmp_path / "quarter", bars(QUARTER), [instrument()])


def request_over(hyp: Hypothesis, **overrides: object) -> RunRequest:
    return RunRequest(
        hyp=hyp,
        strategy_source=HOLDER,
        window=QUARTER,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp),
        capital=CAPITAL,
        overrides=overrides,  # type: ignore[arg-type]
    )


def returns_are_the_differences_net_of_transfers(card) -> None:
    """`returns[i]` is what the book made; `equity[i]` is what it kept after the transfer."""
    previous_equity, previous_cushion = (
        card.capital,
        card.cushion[0] - (card.capital + card.returns[0] - card.equity[0]),
    )
    for made, value, cushion in zip(card.returns, card.equity, card.cushion, strict=True):
        assert made == pytest.approx(value - previous_equity + (cushion - previous_cushion))
        previous_equity, previous_cushion = value, cushion


def entered(card) -> tuple[float, float, float]:
    """The position the holder took at the first bar — the venue walks it one tick, so it
    is two fills — as a signed quantity, what it paid for it and what it was charged."""
    signed = sum(fill.qty if fill.side == "BUY" else -fill.qty for fill in card.fills)
    paid = sum((fill.qty if fill.side == "BUY" else -fill.qty) * fill.px for fill in card.fills)
    return signed, paid, sum(fill.cost for fill in card.fills)


# --- no policy -------------------------------------------------------------------


def test_a_hypothesis_without_a_book_records_no_book_series(quarter: Path) -> None:
    card = run(request_over(booked(None)), quarter).run

    assert card.fills
    assert (card.cushion, card.carry, card.worst_ratio) == ((), (), ())
    previous = card.capital
    for made, value in zip(card.returns, card.equity, strict=True):
        assert made == pytest.approx(value - previous)
        previous = value


# --- the reset -------------------------------------------------------------------


def test_a_surplus_is_set_aside_at_the_first_end_of_february_and_drawn_on_in_march(
    quarter: Path,
) -> None:
    """Long 5,000 at 10.00 from the first bar, the saw-tooth stands at 12.50 on February 1
    and at 10.00 on March 1: February's first end moves the gain out and the book reads its
    capital; March's first end brings back all the cushion holds, which is the gain less
    the costs of the entry, so the book is short by exactly those costs."""
    card = run(request_over(booked({"reset": "monthly"})), quarter).run

    qty, paid, cost = entered(card)
    assert (qty, paid) == (5_000.0, 50_025.0)
    assert len(card.cushion) == len(card.equity)
    assert set(card.cushion[:JANUARY_ENDS]) == {0.0}, "nothing moves inside a month"
    february = card.equity[JANUARY_ENDS - 1] + card.returns[JANUARY_ENDS] - CAPITAL
    assert february == pytest.approx(qty * 12.5 - paid - cost)
    assert card.equity[JANUARY_ENDS] == pytest.approx(CAPITAL)
    assert card.cushion[JANUARY_ENDS] == pytest.approx(february)
    assert len(set(card.cushion[JANUARY_ENDS : JANUARY_ENDS + 29])) == 1, "held all February"
    march = JANUARY_ENDS + 29
    deficit = CAPITAL - (card.equity[march - 1] + card.returns[march])
    assert deficit == pytest.approx(qty * 2.5), "12.50 down to 10.00"
    assert card.cushion[march] == 0.0, "drawn to the last cent"
    assert card.equity[march] == pytest.approx(CAPITAL - (deficit - february))
    assert card.equity[march] == pytest.approx(CAPITAL - cost - 25.0), "the costs and the tick"
    returns_are_the_differences_net_of_transfers(card)
    assert sum(card.returns) == pytest.approx(card.equity[-1] + card.cushion[-1] - CAPITAL)


def test_a_reset_book_s_drawdown_is_bounded_by_the_month_it_fell_in(quarter: Path) -> None:
    """The same fills without the reset draw down 15.0: 114,949.99 to 99,949.99 inside
    January. With it, the 12,449.99 swept out at February's first end is no loss, and the
    worst is February's own: 102,500 to 87,500 at March's first end, struck before the
    cushion restores it — 15,000 again, where a peak kept across the sweep read 27.45."""
    plain = run(request_over(booked(None)), quarter).run
    reset = run(request_over(booked({"reset": "monthly"})), quarter).run

    assert drawdown_pct(plain) == pytest.approx(15.0)
    assert sum(reset.returns) == pytest.approx(sum(plain.returns))
    february = reset.equity[JANUARY_ENDS : JANUARY_ENDS + 29]
    assert (max(february), reset.equity[JANUARY_ENDS + 28] + reset.returns[JANUARY_ENDS + 29]) == (
        pytest.approx(102_500.0),
        pytest.approx(87_500.0),
    )
    assert drawdown_pct(reset) == pytest.approx(15.0)


def test_a_deficit_the_cushion_cannot_cover_leaves_the_book_short(quarter: Path) -> None:
    """Short from the first bar, the book has lost by January 31 and has set nothing
    aside, so February's first end restores nothing and borrows nothing."""
    card = run(request_over(booked({"reset": "monthly"}), side="SELL"), quarter).run

    assert card.equity[JANUARY_ENDS - 1] < CAPITAL
    assert card.cushion[JANUARY_ENDS] == 0.0
    assert card.equity[JANUARY_ENDS] == pytest.approx(
        card.equity[JANUARY_ENDS - 1] + card.returns[JANUARY_ENDS]
    )
    returns_are_the_differences_net_of_transfers(card)


def test_a_reset_moves_cash_and_leaves_the_holdings_where_they_were(quarter: Path) -> None:
    card = run(request_over(booked({"reset": "monthly"})), quarter).run

    qty, _, _ = entered(card)
    before = [item for item in card.held if item.ts_ns == card.period_ends_ns[JANUARY_ENDS - 1]]
    after = [item for item in card.held if item.ts_ns == card.period_ends_ns[JANUARY_ENDS]]
    assert [item.qty for item in before] == [item.qty for item in after] == [qty]


# --- the carry -------------------------------------------------------------------


def test_a_book_levered_past_its_equity_is_charged_the_carry_every_period(
    quarter: Path,
) -> None:
    """150,000 of stock on 100,000 of capital borrows about 50,000; at 500 bps a year a
    calendar day costs 50,000 x 5% / 365.25, and the first period only the sixteen hours
    it spanned from the window's open."""
    hyp = booked({"financing_rate_bps": 500.0}, max_position_pct=200.0, max_leverage=2.0)
    card = run(request_over(hyp, notional=150_000.0), quarter).run

    qty, paid, _ = entered(card)
    assert (qty, paid) == (15_000.0, 150_125.0)
    assert card.cushion == (), "no reset was declared"
    opens = midnight_ns(QUARTER[0])
    previous = opens
    for index, (end, charged) in enumerate(zip(card.period_ends_ns, card.carry, strict=True)):
        gross = sum(item.notional for item in card.held if item.ts_ns == end)
        assert charged == pytest.approx(
            carry(gross, card.equity[index] + charged, 500.0, end - previous)
        )
        previous = end
    a_day = 50_000.0 * 0.05 * NS_PER_DAY / NS_PER_YEAR
    assert card.carry[2] == pytest.approx(a_day, rel=0.02)
    assert card.carry[0] == pytest.approx(a_day * (16 * 3_600 + 1) / 86_400, rel=0.02)
    assert sum(card.carry) > 89 * a_day
    previous_equity = card.capital
    for made, value in zip(card.returns, card.equity, strict=True):
        assert made == pytest.approx(value - previous_equity), "the carry is in the return"
        previous_equity = value


def test_a_book_inside_its_equity_is_charged_nothing(quarter: Path) -> None:
    card = run(request_over(booked({"financing_rate_bps": 500.0})), quarter).run

    assert card.fills
    assert set(card.carry) == {0.0}


# --- the maintenance ratio ---------------------------------------------------------


def test_the_worst_ratio_values_a_long_at_the_period_s_low(quarter: Path) -> None:
    """One bar a day, low a quarter below the close: the ratio is cash plus the position
    at that low, over the position at that low."""
    card = run(request_over(booked({"maintenance_pct": 30.0})), quarter).run

    qty, paid, cost = entered(card)
    cash = CAPITAL - paid - cost
    for index in (0, 1, 15, 40):
        floor = float(bars(QUARTER)[index].close) - 0.25
        assert card.worst_ratio[index] == pytest.approx((cash + qty * floor) / (qty * floor))
    assert card.worst_ratio[1] == pytest.approx(1.97463, abs=1e-5), "measured once, by hand"


def test_the_worst_ratio_values_a_short_at_the_period_s_high(quarter: Path) -> None:
    card = run(request_over(booked({"maintenance_pct": 30.0}), side="SELL"), quarter).run

    qty, paid, cost = entered(card)
    assert qty == -5_000.0
    cash = CAPITAL - paid - cost
    ceiling = float(bars(QUARTER)[15].close) + 0.25
    assert card.worst_ratio[15] == pytest.approx((cash + qty * ceiling) / (-qty * ceiling))
    assert card.worst_ratio[1] == pytest.approx(1.78977, abs=1e-5)


def test_the_adverse_range_is_the_period_s_own_and_never_the_prefix_s(request_for) -> None:
    """A prefix bar with a low of 1.00 precedes two window bars at 10.00 on one day; the
    position opened at the first is valued at the window's own low, and the next day's
    ratio reads that day's low alone."""
    opens = midnight_ns(date(2024, 1, 1))
    prefix = opens - NS_PER_DAY // 2
    stream = [
        (prefix, INSTRUMENT, 10.0, 1.0, 10.0),
        (opens + 1, INSTRUMENT, 10.0, 9.0, 10.0),
        (opens + 2, INSTRUMENT, 10.0, 9.5, 10.0),
        (opens + NS_PER_DAY + 1, INSTRUMENT, 10.0, 8.0, 10.0),
    ]
    request = request_for(
        hypothesis_=booked({"maintenance_pct": 30.0}),
        window=(date(2024, 1, 1), date(2024, 1, 2)),
        prefix=(date(2023, 12, 31), date(2023, 12, 31)),
    )
    bought = Fill(ts_ns=opens + 1, instrument_id=INSTRUMENT, side="BUY", qty=1_000, px=10.0, cost=0)

    curve = _equity(request, stream, [bought], {})

    cash = CAPITAL - 10_000.0
    assert curve.worst_ratio == (
        pytest.approx((cash + 9_000.0) / 9_000.0),
        pytest.approx((cash + 8_000.0) / 8_000.0),
    )


def test_a_name_that_did_not_print_in_the_period_is_valued_at_its_mark(request_for) -> None:
    opens = midnight_ns(date(2024, 1, 1))
    stream = [
        (opens + 1, INSTRUMENT, 10.0, 9.0, 10.0),
        (opens + NS_PER_DAY + 1, "OTHER.XNAS", 5.0, 4.0, 5.0),
    ]
    request = request_for(
        hypothesis_=booked({"maintenance_pct": 30.0}), window=(date(2024, 1, 1), date(2024, 1, 2))
    )
    bought = Fill(ts_ns=opens + 1, instrument_id=INSTRUMENT, side="BUY", qty=1_000, px=10.0, cost=0)

    curve = _equity(request, stream, [bought], {})

    assert curve.worst_ratio[1] == pytest.approx((CAPITAL - 10_000.0 + 10_000.0) / 10_000.0)


def test_a_quote_and_a_trade_carry_their_own_range() -> None:
    from nautilus_trader.model.data import QuoteTick, TradeTick

    from kanso.nautilus.backtest import _range_of

    from .conftest import quotes, trades

    quote = quotes(QUARTER)[0]
    trade = trades(QUARTER)[0]
    assert _range_of(quote) == (float(quote.bid_price), float(quote.ask_price))
    assert _range_of(trade) == (float(trade.price), float(trade.price))
    assert isinstance(quote, QuoteTick) and isinstance(trade, TradeTick)
    assert _range_of(object()) == (None, None)


def test_a_bar_s_range_is_its_low_and_its_high() -> None:
    from kanso.nautilus.backtest import _range_of

    made = bars(QUARTER)[3]
    assert isinstance(made, Bar)
    assert _range_of(made) == (float(made.low), float(made.high))
    assert made.low == Price(float(made.close) - 0.25, 2)


# --- the seed ----------------------------------------------------------------------


def test_a_seeded_cushion_is_drawn_on_when_the_month_s_own_surplus_runs_out(
    quarter: Path,
) -> None:
    """A stage restart hands the runner what earlier windows set aside. With 4,000 seeded
    the March restore is whole — the book reads its capital — and what remains is the
    seed less the entry's costs, which February's surplus had fallen short by."""
    seeded = RunRequest(
        hyp=booked({"reset": "monthly"}),
        strategy_source=HOLDER,
        window=QUARTER,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(booked(None)),
        capital=CAPITAL,
        cushion=4_000.0,
    )

    card = run(seeded, quarter).run

    _, _, cost = entered(card)
    march = JANUARY_ENDS + 29
    assert card.cushion[0] == 4_000.0
    assert card.cushion[JANUARY_ENDS] == pytest.approx(4_000.0 + 12_500.0 - cost - 25.0)
    assert card.equity[march] == pytest.approx(CAPITAL)
    assert card.cushion[march] == pytest.approx(4_000.0 - cost - 25.0)
    returns_are_the_differences_net_of_transfers(card)


# --- the harness ---------------------------------------------------------------------

BOOK_PROBE = b'''
from pathlib import Path

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""
    notional: float = 150_000.0


class Strategy(KansoStrategy):
    """Levers into the saw-tooth on the first bar, rests a bid the market reaches on the
    thirteenth, exits in February and re-enters in March, and writes down every balance it
    reads before it acts."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{bar.ts_init} {self.balance!r}\\n")
        name = bar.bar_type.instrument_id
        if self.seen in (1, 62):
            self.submit_entry(name, "BUY", notional=self.kanso_config.notional)
        if self.seen == 1:
            self.submit_order(
                self.order_factory.limit(
                    name, OrderSide.BUY, Quantity.from_int(1_000), Price.from_str("9.80")
                )
            )
        if self.seen == 40:
            self.submit_exit(name)
'''


@pytest.mark.parametrize("quoted", [False, True], ids=["fixed spread", "quoted spread"])
def test_the_balance_read_before_acting_is_the_book_the_policy_left(
    tmp_path: Path, quoted: bool
) -> None:
    """Every balance read at a period end the sleeve did not trade in is the equity the
    card struck there before that end's own carry and transfer — so every earlier carry,
    every earlier reset and the fills of every earlier end, including the resting bid the
    venue matched against the point that turned a period, are in it to the last cent."""
    fields = booked(
        {"reset": "monthly", "financing_rate_bps": 500.0}, max_position_pct=200.0, max_leverage=2.0
    ).model_dump(by_alias=True, mode="json")
    if quoted:
        fields["data_requirements"] = ["bar", "quote"]
        fields["costs"] = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "quotes"}
    hyp = Hypothesis.model_validate(fields)
    record = tmp_path / "balance.txt"
    request = RunRequest(
        hyp=hyp,
        strategy_source=BOOK_PROBE,
        window=QUARTER,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(hyp, quotes_available=quoted),
        capital=CAPITAL,
        overrides={"record": str(record)},
    )
    groups: list[tuple[object, ...]] = [tuple(bars(QUARTER))]
    if quoted:
        # Each day's quote ahead of its bar at the same instant: a fill the bar matches is
        # then charged the quote of its own instant on both sides, which a sleeve handed the
        # bar first has not seen yet.
        groups.insert(0, tuple(quotes(QUARTER)))

    card = execute(request, [instrument()], groups).run

    ends = list(card.period_ends_ns)
    filled = {ends[bisect_left(ends, fill.ts_ns)] for fill in card.fills}
    assert len(filled) == 4, "the entry, the bid, the exit and the re-entry"
    assert (card.fills[2].px, card.fills[2].ts_ns) == (9.8, ends[12]), "the bid, on January 13"
    assert all(charged > 0 for charged in card.carry[:39]), "levered until the exit"
    moved = [
        index
        for index, cushion in enumerate(card.cushion)
        if cushion != (card.cushion[index - 1] if index else 0.0)
    ]
    assert moved == [31, 60], "a transfer at the first end of February and of March"
    compared = 0
    for line in record.read_text().splitlines():
        ts, balance = int(line.split()[0]), float(line.split()[1])
        if ts in filled:
            continue
        index = ends.index(ts)
        before = card.cushion[index - 1] if index else 0.0
        struck = card.equity[index] + card.carry[index] + card.cushion[index] - before
        assert balance == pytest.approx(struck, rel=1e-12), f"period {index}"
        compared += 1
    assert compared == len(ends) - 4


# --- a restart ---------------------------------------------------------------------


def test_a_restart_carries_from_the_end_it_settled_and_resets_across_the_turn(request_for) -> None:
    """A stage restart names the last end the previous window settled: the first carry runs
    from it rather than from the window's midnight, and a month that turned between the two
    resets at the first end after it, drawing on the cushion the restart was seeded with."""
    opens = midnight_ns(date(2024, 2, 1))
    settled = opens - NS_PER_DAY // 2
    end = opens + NS_PER_DAY // 2
    request = request_for(
        hypothesis_=booked(
            {"reset": "monthly", "financing_rate_bps": 500.0},
            max_position_pct=200.0,
            max_leverage=2.0,
        ),
        window=(date(2024, 2, 1), date(2024, 2, 1)),
        cushion=1_000.0,
        settled_ns=settled,
    )
    bought = Fill(
        ts_ns=opens + 1, instrument_id=INSTRUMENT, side="BUY", qty=15_000, px=10.0, cost=0
    )

    curve = _equity(
        request,
        [(opens + 1, INSTRUMENT, 10.0, 9.0, 10.0), (end, INSTRUMENT, 9.9, 9.9, 9.9)],
        [bought],
        {},
    )

    worth = 15_000 * 9.9
    value = CAPITAL - 150_000.0 + worth
    charged = carry(worth, value, 500.0, end - settled)
    assert curve.carry == (pytest.approx(charged),)
    assert charged == pytest.approx((worth - value) * 0.05 * NS_PER_DAY / NS_PER_YEAR)
    assert curve.cushion == (0.0,), "the whole seed restores February's first deficit"
    assert curve.equity == (pytest.approx(value - charged + 1_000.0),)
    assert curve.returns == (pytest.approx(value - charged - CAPITAL),)


def test_a_restart_inside_the_month_it_settled_in_resets_nothing(request_for) -> None:
    opens = midnight_ns(date(2024, 2, 2))
    request = request_for(
        hypothesis_=booked({"reset": "monthly"}),
        window=(date(2024, 2, 2), date(2024, 2, 2)),
        cushion=1_000.0,
        settled_ns=opens - 1,
    )

    curve = _equity(request, [(opens + 1, INSTRUMENT, 10.0, 9.0, 10.0)], [], {})

    assert (curve.cushion, curve.equity) == ((1_000.0,), (CAPITAL,))
