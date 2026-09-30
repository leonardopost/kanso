"""A perpetual's funding, booked once by the runner and mirrored by the harness.

A card holding a perpetual across a settlement pays the realised rate on the notional it
held then, in the extraction, beside every other cost: out of cash at the settlement
instant, inside that period's return and equity, and on the trade open at the instant. The
sleeve's balance books the same amount when the point is delivered, so what a strategy
sizes from is what the card records. Everything here runs the real engine over a real
catalog of continuous synthetic bars and funding settlements, which the `synthetic` loader
generates with no vendor in the loop.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kanso.criteria.gates import repriced
from kanso.criteria.run import NS_PER_DAY, midnight_ns
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.data.types import Funding
from kanso.nautilus import backtest
from kanso.nautilus.backtest import (
    Marks,
    RunRequest,
    RunResult,
    run,
    run_subprocess,
    window_data,
)
from kanso.nautilus.costs import funding_payment
from kanso.schemas import Hypothesis, VenueModel, resolve_venue_model
from kanso.schemas.venue import CostsOverride, VenueDeclaration

from .conftest import CAPITAL, PERP, RESEARCH, SECOND_NS, SNAPSHOT, catalog, hypothesis, perpetual

HOUR_NS = 3_600 * SECOND_NS
MULTIPLIER = 0.01
SERVED = (date(2024, 1, 1), date(2024, 1, 4))
"""The days the catalog holds: four sessions of a round-the-clock venue."""

TRADER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    enter: int = 5
    leave: int = 27
    side: str = "BUY"
    record: str = ""


class Strategy(KansoStrategy):
    """Opens ten contracts on the `enter`-th bar and closes them on the `leave`-th, and
    writes down the balance it reads at every settlement it is handed."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        instrument_id = bar.bar_type.instrument_id
        if self.seen == self.kanso_config.enter:
            self.submit_entry(instrument_id, self.kanso_config.side, qty=10)
        elif self.seen == self.kanso_config.leave:
            self.submit_exit(instrument_id)

    def on_data(self, data):
        if self.kanso_config.record:
            with open(self.kanso_config.record, "a") as out:
                out.write(f"{data.ts_init} {data.rate!r} {self.balance!r}\\n")
'''

ANSWERING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""
    hour: int = -1
    below: float = 0.0


class Strategy(KansoStrategy):
    """Buys ten contracts in answer to the first settlement it is handed after a bar, the
    first at `hour` UTC when one is given, at market or resting `below` its last price, and
    writes down the balance it reads at every settlement."""

    config_cls = Config

    def on_start(self):
        self.bars = 0
        self.bought = False

    def on_bar(self, bar):
        self.bars += 1

    def on_data(self, data):
        with open(self.kanso_config.record, "a") as out:
            out.write(f"{data.ts_init} {data.rate!r} {self.balance!r}\\n")
        hour = data.ts_init // 3_600_000_000_000 % 24
        if self.bars and not self.bought and self.kanso_config.hour in (-1, hour):
            self.bought = True
            below = self.kanso_config.below
            price = self.last_price(data.instrument_id) * (1 - below) if below else None
            self.submit_entry(
                data.instrument_id, "BUY", qty=10, price=None if price is None else round(price, 1)
            )
'''


def generated(resolution: str, types: tuple[str, ...] = ("bar", "funding")) -> list[object]:
    """Four continuous sessions of the perpetual: bars at `resolution` and its settlements."""
    loader = SyntheticLoader()
    refs = loader.discover(
        {
            "loader": "synthetic",
            "model": "gbm",
            "seed": 7,
            "instruments": ["BTCUSDT-PERP"],
            "venue": "SIM",
            "resolution": resolution,
            "types": list(types),
            "start": SERVED[0],
            "end": SERVED[1],
            "start_price": 40_000.0,
            "sigma_bps": 20.0,
            "price_precision": 1,
            "calendar": "continuous",
        }
    )
    return [point for ref in refs for point in loader.load(ref, ref.span)]


def perp_hypothesis(
    resolution: str, *, funded: bool = True, costs: dict[str, object] | None = None
) -> Hypothesis:
    """The perpetual alone, at `resolution`, requiring its funding unless told otherwise."""
    return hypothesis(
        universe=(PERP,),
        data_requirements=("bar", "funding") if funded else ("bar",),
        resolution=resolution,
        costs=costs,
    )


def perp_request(hyp: Hypothesis, source: bytes = TRADER, **overrides: Any) -> RunRequest:
    """A research-window run on a USDT account, the runner's own costs stated."""
    model = resolve_venue_model(
        "SIM",
        config=VenueDeclaration(currency="USDT"),
        broker="synthetic",
        hypothesis_costs=CostsOverride.model_validate(hyp.costs.model_dump(exclude_none=True)),
        max_leverage=hyp.risk_limits.max_leverage,
        quotes_available=False,
    )
    return RunRequest(
        hyp=hyp,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=model.model_dump(),
        capital=CAPITAL,
        overrides=overrides,
    )


@pytest.fixture(scope="module")
def hourly(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A catalog of hourly bars and three settlements a day."""
    root = tmp_path_factory.mktemp("hourly")
    return catalog(root / "catalog", generated("1h"), [perpetual()])


@pytest.fixture(scope="module")
def daily(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A catalog of daily bars, each closing at 00:00Z, and three settlements a day."""
    root = tmp_path_factory.mktemp("daily")
    return catalog(root / "catalog", generated("1d"), [perpetual()])


def closes(held: Path, resolution: str) -> dict[int, float]:
    """Every bar's close by its availability instant, as the catalog serves it."""
    _, groups = window_data(perp_request(perp_hypothesis(resolution)), held)
    return {
        int(point.ts_init): float(point.close)  # type: ignore[attr-defined]
        for group in groups
        for point in group
        if hasattr(point, "close")
    }


def settlements(held: Path, resolution: str) -> list[tuple[int, float]]:
    """Every settlement the catalog serves over the window, as `(instant, rate)`."""
    _, groups = window_data(perp_request(perp_hypothesis(resolution)), held)
    found = [getattr(point, "data", point) for group in groups for point in group]
    return [(int(p.ts_init), float(p.rate)) for p in found if isinstance(p, Funding)]


def mark_at(prices: dict[int, float], instant: int) -> float:
    """The last close at or before an instant."""
    return prices[max(ts for ts in prices if ts <= instant)]


def cumulative(result: RunResult, until: int) -> float:
    """What the run paid in funding at or before an instant."""
    return sum(payment.paid for payment in result.run.funding if payment.ts_ns <= until)


# --- the runner ------------------------------------------------------------------


def test_a_held_perpetual_pays_each_settlement_it_held_once_in_the_extraction(
    hourly: Path,
) -> None:
    """Held from 05:00 on the first day to 03:00 on the second: three settlements, each the
    realised rate on ten contracts at the last close at or before it, times the multiplier."""
    funded = run(perp_request(perp_hypothesis("1h")), hourly)
    unfunded = run(perp_request(perp_hypothesis("1h", funded=False)), hourly)
    prices = closes(hourly, "1h")
    bought, sold = funded.run.fills
    held_through = [
        (ts, rate) for ts, rate in settlements(hourly, "1h") if bought.ts_ns <= ts < sold.ts_ns
    ]

    assert len(held_through) == 3
    assert [
        (payment.ts_ns, payment.instrument_id, payment.qty, payment.rate, payment.paid)
        for payment in funded.run.funding
    ] == [
        (ts, PERP, 10.0, rate, funding_payment(10.0, mark_at(prices, ts), MULTIPLIER, rate))
        for ts, rate in held_through
    ]
    assert unfunded.run.funding == ()
    assert funded.run.fills == unfunded.run.fills
    assert funded.run.period_ends_ns == unfunded.run.period_ends_ns
    for end, with_funding, without in zip(
        funded.run.period_ends_ns, funded.run.equity, unfunded.run.equity, strict=True
    ):
        assert with_funding == pytest.approx(without - cumulative(funded, end), abs=1e-9)
    total = sum(payment.paid for payment in funded.run.funding)
    (trade,) = funded.run.trades
    (plain,) = unfunded.run.trades
    assert trade.funding == pytest.approx(total, abs=1e-12)
    assert trade.cost == plain.cost
    assert trade.pnl_net == pytest.approx(plain.pnl_net - total, abs=1e-9)


def test_a_fill_stamped_at_a_settlement_is_funded_there(hourly: Path) -> None:
    """Bought on the 08:00 bar, the fill is stamped at 08:00: held when 08:00 settled."""
    result = run(perp_request(perp_hypothesis("1h"), enter=8, leave=20), hourly)
    bought, _ = result.run.fills

    assert bought.ts_ns == midnight_ns(SERVED[0]) + 8 * HOUR_NS
    assert [payment.ts_ns for payment in result.run.funding] == [
        bought.ts_ns,
        midnight_ns(SERVED[0]) + 16 * HOUR_NS,
    ]


def test_a_short_receives_a_positive_rate_and_pays_a_negative_one(hourly: Path) -> None:
    result = run(perp_request(perp_hypothesis("1h"), side="SELL"), hourly)
    prices = closes(hourly, "1h")
    rates = [payment.rate for payment in result.run.funding]

    assert any(rate > 0 for rate in rates) and any(rate < 0 for rate in rates)
    for payment in result.run.funding:
        assert payment.qty == -10.0
        assert payment.paid == funding_payment(
            -10.0, mark_at(prices, payment.ts_ns), MULTIPLIER, payment.rate
        )
        assert (payment.paid < 0) == (payment.rate > 0)


def test_a_day_of_the_run_keeps_that_day_s_payments_alone(hourly: Path) -> None:
    result = run(perp_request(perp_hypothesis("1h")), hourly)
    first = midnight_ns(SERVED[0])

    day = result.run.between(first, first + NS_PER_DAY)

    assert [payment.ts_ns for payment in day.funding] == [first + 8 * HOUR_NS, first + 16 * HOUR_NS]
    assert [
        payment.ts_ns
        for payment in result.run.between(first + NS_PER_DAY, first + 2 * NS_PER_DAY).funding
    ] == [first + NS_PER_DAY]


def test_a_card_books_the_funding_an_in_process_run_books(hourly: Path, tmp_path: Path) -> None:
    """The child is handed the window a session at a time, pickled; the payments agree."""
    request = perp_request(perp_hypothesis("1h"))

    carded = run_subprocess(request, hourly, tmp_path)

    assert not carded.crashed, carded.traceback_tail
    assert carded.run.funding == run(request, hourly).run.funding
    assert len(carded.run.funding) == 3


def test_a_cost_scenario_reprices_the_fills_and_leaves_the_funding_as_booked(
    hourly: Path,
) -> None:
    """The runner's own costs reproduce the recorded run; free fills move the equity by the
    fills' cost alone, and every trade keeps the funding it paid."""
    result = run(perp_request(perp_hypothesis("1h")), hourly)
    costs = VenueModel.model_validate(result.run.venue_model).costs
    own = {
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "fixed_bps": costs.fixed_bps,
    }

    same = repriced(result.run, own)
    free = repriced(result.run, {"commission_bps": 0.0, "slippage_bps": 0.0, "fixed_bps": 0.0})

    assert [fill.cost for fill in same.fills] == [fill.cost for fill in result.run.fills]
    assert same.equity == pytest.approx(result.run.equity, abs=1e-9)
    assert free.funding == result.run.funding
    (trade,) = result.run.trades
    (freed,) = free.trades
    assert freed.funding == trade.funding
    assert freed.cost == 0.0
    assert freed.pnl_net == pytest.approx(trade.pnl_net + trade.cost, abs=1e-9)
    assert free.equity[-1] == pytest.approx(
        result.run.equity[-1] + sum(fill.cost for fill in result.run.fills), abs=1e-9
    )


# --- the marks a settlement is struck at -----------------------------------------------


def settled_at(instant: int, rate: float = 0.0001) -> Funding:
    return Funding(
        instrument_id=perpetual().id,  # type: ignore[attr-defined]
        rate=rate,
        ts_event=instant,
        ts_init=instant,
    )


def printed(instant: int, close: float) -> object:
    """One hourly bar of the perpetual closing at `close`, available at `instant`."""
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.objects import Price, Quantity

    from kanso.nautilus.strategy import _bar_type

    return Bar(
        _bar_type(perpetual().id, "1h"),  # type: ignore[attr-defined]
        Price(close, 1),
        Price(close, 1),
        Price(close, 1),
        Price(close, 1),
        Quantity.from_int(1),
        ts_event=instant,
        ts_init=instant,
    )


def folded_marks(*chunks: list[object]) -> tuple[tuple[int, str, float, float], ...]:
    request = perp_request(perp_hypothesis("1h"))
    marks = Marks(request, VenueModel.model_validate(dict(request.venue_model)))
    for chunk in chunks:
        marks.take(chunk)
    return marks.settlements()


def test_a_settlement_is_marked_at_the_greatest_print_of_its_instant_in_any_order() -> None:
    """Two prints at the settlement instant and one after it: the greater of the two, however
    the chunk arrives."""
    at = midnight_ns(SERVED[0]) + 8 * HOUR_NS
    points = [
        printed(at, 40_000.0),
        printed(at, 40_010.0),
        settled_at(at),
        printed(at + HOUR_NS, 1.0),
    ]

    assert folded_marks(points) == ((at, PERP, 0.0001, 40_010.0),)
    assert folded_marks(points[::-1]) == ((at, PERP, 0.0001, 40_010.0),)


def test_a_settlement_is_marked_at_a_print_an_earlier_chunk_left() -> None:
    """A day with no print of its own settles at the last print of the day before."""
    first = midnight_ns(SERVED[0])
    earlier = [printed(first + HOUR_NS, 40_000.0), printed(first + 2 * HOUR_NS, 40_020.0)]
    later = [
        settled_at(first + NS_PER_DAY + 8 * HOUR_NS),
        settled_at(first + NS_PER_DAY + 16 * HOUR_NS),
    ]

    assert [mark for *_, mark in folded_marks(earlier, later)] == [40_020.0, 40_020.0]


def test_a_settlement_before_any_print_is_marked_at_nothing() -> None:
    at = midnight_ns(SERVED[0]) + 8 * HOUR_NS

    assert folded_marks([settled_at(at)]) == ((at, PERP, 0.0001, 0.0),)


# --- the harness -------------------------------------------------------------------


def balances(record: Path) -> list[tuple[int, float, float]]:
    """Each settlement a sleeve was handed: its instant, its rate and the balance it read."""
    rows = record.read_text(encoding="utf-8").split()
    return [(int(rows[i]), float(rows[i + 1]), float(rows[i + 2])) for i in range(0, len(rows), 3)]


def test_the_balance_at_a_settlement_is_the_equity_the_runner_strikes(
    daily: Path, tmp_path: Path
) -> None:
    """Daily bars close at 00:00Z, so each day's last point is its 16:00 settlement: the
    balance read there, after the harness booked it, is the equity the card records."""
    record = tmp_path / "balances.txt"
    result = run(perp_request(perp_hypothesis("1d"), enter=1, leave=99, record=str(record)), daily)
    read = balances(record)
    ends = dict(zip(result.run.period_ends_ns, result.run.equity, strict=True))

    assert result.run.funding
    compared = [(ts, balance) for ts, _, balance in read if ts in ends]
    assert len(compared) == len(ends)
    for ts, balance in compared:
        assert balance == pytest.approx(ends[ts], abs=1e-9)


def test_a_sleeve_that_does_not_book_funding_still_sees_every_settlement(
    daily: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `books_funding` off the balance is the equity before funding, and `on_data` is
    handed each point all the same."""
    sleeve = backtest._sleeve

    def unbooked(request: RunRequest) -> tuple[Any, Any]:
        cls, config = sleeve(request)
        return cls, type(config)(**{**config.dict(), "books_funding": False})

    monkeypatch.setattr(backtest, "_sleeve", unbooked)
    record = tmp_path / "balances.txt"
    result = run(perp_request(perp_hypothesis("1d"), enter=1, leave=99, record=str(record)), daily)
    read = balances(record)
    ends = dict(zip(result.run.period_ends_ns, result.run.equity, strict=True))

    assert [(ts, rate) for ts, rate, _ in read] == settlements(daily, "1d")
    for ts, _, balance in read:
        if ts in ends:
            assert balance == pytest.approx(ends[ts] + cumulative(result, ts), abs=1e-9)


def test_an_order_placed_in_answer_to_a_settlement_is_funded_and_the_balance_catches_up(
    daily: Path, tmp_path: Path
) -> None:
    """Bought in `on_data` at 08:00, the fill is stamped 08:00 and the runner funds it there;
    the harness booked 08:00 before the order existed and settles the difference when the
    next instant arrives, so the balance read at 16:00 is the equity struck there."""
    record = tmp_path / "balances.txt"
    result = run(perp_request(perp_hypothesis("1d"), ANSWERING, record=str(record)), daily)
    (bought,) = result.run.fills
    ends = dict(zip(result.run.period_ends_ns, result.run.equity, strict=True))

    assert result.run.funding[0].ts_ns == bought.ts_ns
    assert result.run.funding[0].qty == 10.0
    for ts, _, balance in balances(record):
        if ts in ends:
            assert balance == pytest.approx(ends[ts], abs=1e-9)


def test_a_fill_that_lands_after_a_settlement_is_left_out_of_what_it_settled(
    daily: Path, tmp_path: Path
) -> None:
    """A buy resting under the market from a 16:00 settlement fills when the next bar, at
    00:00, reaches it: the runner funds it at 00:00 and not at 16:00, and the harness,
    settling 16:00 again when that bar arrives, takes the fill the venue has just matched
    against it back out before it strikes 16:00."""
    record = tmp_path / "balances.txt"
    result = run(
        perp_request(perp_hypothesis("1d"), ANSWERING, record=str(record), hour=16, below=0.0005),
        daily,
    )
    (bought,) = result.run.fills
    ends = dict(zip(result.run.period_ends_ns, result.run.equity, strict=True))
    answered = midnight_ns(SERVED[0]) + NS_PER_DAY + 16 * HOUR_NS

    assert bought.ts_ns == answered + 8 * HOUR_NS
    assert [payment.ts_ns for payment in result.run.funding][:1] == [bought.ts_ns]
    for ts, _, balance in balances(record):
        if ts in ends:
            assert balance == pytest.approx(ends[ts], abs=1e-9)
