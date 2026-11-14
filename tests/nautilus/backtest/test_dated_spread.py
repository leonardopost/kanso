"""A dated spread charges each fill its row's width at the instant its price was struck at.

Two names are fed one-minute bars on Tuesday 2 January 2024 (New York on EST, UTC−5) around
the open and the close. A sleeve buys both on the bar stamped 09:30 — a pre-market price —
and sells both on the bar stamped 16:00, the last regular one. Under a latency of 10 ms each
order lands at the next instant's first point: the name whose bar that point is fills at its
bar of the instant, the other at its bar before it (the venue settles the commands due after
each point, `docs/concepts.md`, Delivery). So the first name's buy pays `open` and its sale
`post`, and the second name's buy pays `pre` and its sale `rth`; swapping the order the two
series are handed in swaps which name pays which. At no latency both names fill at their bar
of the instant they were sent at. Every fill's cost is its notional times half its row's width,
by hand, and the balance the sleeve reads once flat is the equity the run ended on.

Which point a fill's price was struck at is the venue's to say, and is read off it
(`kanso.nautilus.struck`), so the same holds where a stream does not show it: an order a fill
handler sends before the venue has applied the rest of an instant, a bar published after its
close, a name read as prints, and the evening of the New York day before a window's first UTC
day.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar, BarSpecification, BarType, TradeTick
from nautilus_trader.model.enums import AggregationSource, AggressorSide, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.codec import decode_run, encode_run
from kanso.criteria.run import CardRun, midnight_ns
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.backtest import RunRequest, execute
from kanso.nautilus.book import BookMember, execute_book
from kanso.schemas import Hypothesis, SharedBook

from .conftest import CAPITAL, RESEARCH, SECOND_NS, SNAPSHOT, VENUE, hypothesis, instrument

FIRST = "LEAD"
SECOND = "LAGG"
BPS = 10_000.0
MINUTE_NS = 60 * SECOND_NS
DAY = date(2024, 1, 2)
"""A Tuesday in January: New York is five hours behind UTC."""

STAMPS = ("09:29", "09:30", "09:31", "09:32", "15:59", "16:00", "16:01", "16:02", "16:05")
"""The New York minutes each name's bars are stamped at."""

WIDTHS = {
    FIRST: {"pre": 100.0, "open": 40.0, "rth": 10.0, "post": 200.0},
    SECOND: {"pre": 300.0, "open": 60.0, "rth": 20.0, "post": 400.0},
}
"""Each name's widths from 2024 on, every part of the session its own, so the part a fill
paid is read off its cost."""

OLD = {"pre": 900.0, "open": 900.0, "rth": 900.0, "post": 900.0}
"""Each name's widths through 2023, which no fill of 2024 may pay."""

ROUND_TRIP = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        minute = self.session.minute
        if minute == 569:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100)
        elif minute == 959:
            self.submit_exit(bar.bar_type.instrument_id)
        elif minute == 964 and self.kanso_config.record:
            with Path(self.kanso_config.record).open("a") as out:
                out.write(f"{self.balance!r}\\n")
"""


def _stamp(hhmm: str) -> int:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return midnight_ns(DAY) + ((hours + 5) * 60 + minutes) * MINUTE_NS


def _close(symbol: str, index: int) -> float:
    """Each bar's own close, so the bar a fill was priced at is read off its price."""
    return (10.0 if symbol == FIRST else 20.0) + index * 0.01


def _bars(symbol: str) -> tuple[Bar, ...]:
    kind = BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for index, hhmm in enumerate(STAMPS):
        close = Price(_close(symbol, index), 2)
        ts = _stamp(hhmm)
        made.append(Bar(kind, close, close, close, close, Quantity.from_int(100_000), ts, ts))
    return tuple(made)


def _rows(symbol: str) -> list[dict[str, object]]:
    return [
        {
            "instrument": f"{symbol}.{VENUE}",
            "from": "2023-01-01",
            **OLD,
            "n_sessions": 5,
            "n_open": 25,
        },
        {
            "instrument": f"{symbol}.{VENUE}",
            "from": "2024-01-01",
            **WIDTHS[symbol],
            "n_sessions": 5,
            "n_open": 25,
        },
    ]


def _hypothesis(latency_ms: float) -> Hypothesis:
    document = hypothesis(universe=(f"{FIRST}.{VENUE}", f"{SECOND}.{VENUE}")).model_dump(
        mode="json"
    )
    document.update(resolution="1m", horizon="1d")
    document["costs"] = {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "spread": "dated",
        "dated": _rows(FIRST) + _rows(SECOND),
        "latency_ms": latency_ms,
    }
    return Hypothesis.model_validate(document)


def _request(latency_ms: float, record: Path | None = None) -> RunRequest:
    from kanso.schemas import CostsOverride, resolve_venue_model

    hyp = _hypothesis(latency_ms)
    assert hyp.costs is not None
    model = resolve_venue_model(
        VENUE,
        broker="synthetic",
        hypothesis_costs=CostsOverride.model_validate(hyp.costs.model_dump(exclude_none=True)),
        max_leverage=hyp.risk_limits.max_leverage,
        quotes_available=False,
    )
    return RunRequest(
        hyp=hyp,
        strategy_source=ROUND_TRIP,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=model.model_dump(),
        capital=CAPITAL,
        overrides={} if record is None else {"record": str(record)},
    )


def _run(order: tuple[str, str], latency_ms: float, record: Path | None = None) -> CardRun:
    return execute(
        _request(latency_ms, record),
        [instrument(FIRST), instrument(SECOND)],
        [_bars(symbol) for symbol in order],
    ).run


def _paid(card: CardRun) -> dict[tuple[str, str], tuple[str, str]]:
    """Each fill, by name and side, as the stamp of the bar its price came from and the part
    of the session it paid — read off its price and its cost, by hand."""
    paid = {}
    for fill in card.fills:
        symbol = fill.instrument_id.split(".")[0]
        index = round((fill.px - _close(symbol, 0)) / 0.01)
        width = fill.cost / fill.notional * 2.0 * BPS
        parts = [part for part, bps in WIDTHS[symbol].items() if bps == pytest.approx(width)]
        assert len(parts) == 1, (fill, width)
        assert fill.price_ts_ns == _stamp(STAMPS[index])
        paid[(symbol, fill.side)] = (STAMPS[index], parts[0])
    return paid


@pytest.mark.parametrize("order", [(FIRST, SECOND), (SECOND, FIRST)])
def test_under_a_latency_each_leg_pays_the_part_its_price_was_struck_in(
    order: tuple[str, str], tmp_path: Path
) -> None:
    """The name whose bar opens the instant an order lands at fills at that bar, and the other
    at its bar before it; each pays its own price's part of the session, never the fill's."""
    record = tmp_path / "balance.txt"
    card = _run(order, 10.0, record)
    lead, lag = order

    assert _paid(card) == {
        (lead, "BUY"): ("09:31", "open"),
        (lead, "SELL"): ("16:01", "post"),
        (lag, "BUY"): ("09:30", "pre"),
        (lag, "SELL"): ("16:00", "rth"),
    }
    assert all(fill.ts_ns in (_stamp("09:31"), _stamp("16:01")) for fill in card.fills)
    (balance,) = [float(line) for line in record.read_text().split()[-1:]]
    assert balance == pytest.approx(card.equity[-1], rel=1e-12)


@pytest.mark.parametrize("order", [(FIRST, SECOND), (SECOND, FIRST)])
def test_at_no_latency_both_legs_fill_at_the_bar_they_were_sent_on(
    order: tuple[str, str], tmp_path: Path
) -> None:
    record = tmp_path / "balance.txt"
    card = _run(order, 0.0, record)

    assert _paid(card) == {
        (FIRST, "BUY"): ("09:30", "pre"),
        (FIRST, "SELL"): ("16:00", "rth"),
        (SECOND, "BUY"): ("09:30", "pre"),
        (SECOND, "SELL"): ("16:00", "rth"),
    }
    (balance,) = [float(line) for line in record.read_text().split()[-1:]]
    assert balance == pytest.approx(card.equity[-1], rel=1e-12)


def test_the_run_records_the_instant_each_fill_was_priced_at_and_reads_it_back() -> None:
    card = _run((FIRST, SECOND), 10.0)

    assert decode_run(encode_run(card)) == card
    stored = encode_run(card)["fills"]
    assert all("price_ts_ns" in fill for fill in stored)


def test_a_run_whose_first_delivered_instant_no_row_covers_is_refused() -> None:
    """The warmup sessions the catalog resolved are held to the table where the run is built,
    on the New York date of the first instant delivered: 00:00 UTC of the first session is the
    evening of the New York day before it."""
    request = _request(10.0)

    with pytest.raises(ValidationError) as refused:
        replace(request, prefix=(date(2023, 1, 1), date(2023, 1, 1)))

    assert "has no row in force on 2022-12-31" in refused.value.message
    assert "state a row from 2022-12-31 or earlier" in (refused.value.remedy or "")
    assert replace(request, prefix=(date(2023, 1, 2), date(2023, 1, 2))).prefix


def test_the_first_utc_day_s_evening_is_charged_the_new_york_day_before_s_row(
    tmp_path: Path,
) -> None:
    """A window opening Wednesday 3 January is delivered from 00:00 UTC, which is 19:00 on
    Tuesday in New York: a buy on the bar stamped 00:31 UTC is priced in Tuesday's post-market
    and pays the row in force on Tuesday, so a table whose first row is Wednesday's is refused
    rather than charging that fill nothing."""
    opens = date(2024, 1, 3)
    evening = midnight_ns(opens) + 31 * MINUTE_NS
    hyp = _hypothesis(0.0).model_dump(mode="json")
    hyp["universe"] = [f"{FIRST}.{VENUE}"]
    hyp["windows"]["research"]["start"] = opens.isoformat()
    hyp["costs"]["dated"] = [{**_rows(FIRST)[1], "from": "2024-01-02"}]
    request = replace(
        _request(0.0),
        hyp=Hypothesis.model_validate(hyp),
        window=(opens, RESEARCH[1]),
        strategy_source=EVENING,
    )
    late = [{**_rows(FIRST)[1], "from": "2024-01-03"}]
    with pytest.raises(ValidationError, match="no row in force on 2024-01-02"):
        replace(request, venue_model=_dated_model(request, late))

    kind = _bars(FIRST)[0].bar_type
    bars = tuple(
        Bar(kind, px, px, px, px, Quantity.from_int(100_000), ts, ts)
        for px, ts in ((Price(10.0, 2), evening), (Price(10.01, 2), evening + MINUTE_NS))
    )
    (bought,) = [fill for fill in execute(request, [instrument(FIRST)], [bars]).run.fills]

    assert bought.price_ts_ns == evening
    assert bought.cost == pytest.approx(bought.notional * WIDTHS[FIRST]["post"] / 2 / BPS)


EVENING = b"""
from kanso.nautilus.strategy import KansoStrategy


class Strategy(KansoStrategy):
    def on_start(self):
        self.sent = False

    def on_bar(self, bar):
        if not self.sent:
            self.sent = True
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100)
"""


def _dated_model(request: RunRequest, rows: list[dict[str, object]]) -> dict[str, object]:
    """The request's venue model with these rows in place of its table."""
    model = dict(request.venue_model)
    model["costs"] = {**dict(model["costs"]), "dated": rows}  # type: ignore[call-overload]
    return model


def test_a_shared_book_refuses_a_member_charged_a_dated_spread() -> None:
    book = SharedBook.model_validate(
        {"capital": CAPITAL, "leverage": {"rth": 4}, "rank_key": "net_per_trade_bp", "k_min": 1}
    )
    member = BookMember("one", 1, datetime(2024, 1, 1, tzinfo=UTC), _request(10.0))

    with pytest.raises(PreconditionError, match="holds a dated spread"):
        execute_book(book, [member], [], [])


RESTING = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""
    limit: float = 0.0


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        minute = self.session.minute
        if minute == 568:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", qty=100, price=self.kanso_config.limit
            )
        elif minute == 959:
            self.submit_exit(bar.bar_type.instrument_id)
        elif minute == 964:
            self._write()

    def on_order_filled(self, event):
        self._write()

    def _write(self):
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{self.balance!r}\\n")
"""


def _dipping(symbol: str) -> tuple[Bar, ...]:
    """The bars of `_bars`, but the one stamped 09:31 dips to a cent under the 09:29 close."""
    made = []
    for index, bar in enumerate(_bars(symbol)):
        low = Price(_close(symbol, 0) - 0.01, 2) if STAMPS[index] == "09:31" else bar.low
        made.append(
            Bar(
                bar.bar_type,
                bar.open,
                bar.high,
                low,
                bar.close,
                bar.volume,
                bar.ts_event,
                bar.ts_init,
            )
        )
    return tuple(made)


@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_a_resting_fill_read_before_its_bar_is_delivered_is_charged_that_bar_s_width(
    latency_ms: float, tmp_path: Path
) -> None:
    """The venue fills a resting limit on the bar stamped 09:31 before the sleeve is handed
    that bar, and the sleeve reads its balance in between: the venue has said which bar it
    filled on, so the balance read there already charges the open's width, as the extraction
    charges it, and the balance once flat is the equity."""
    record = tmp_path / "balance.txt"
    hyp = _hypothesis(latency_ms).model_dump(mode="json")
    hyp["universe"] = [f"{FIRST}.{VENUE}"]
    hyp["costs"]["dated"] = _rows(FIRST)
    request = replace(
        _request(latency_ms),
        hyp=Hypothesis.model_validate(hyp),
        strategy_source=RESTING,
        overrides={"record": str(record), "limit": _close(FIRST, 0) - 0.01},
    )
    card = execute(request, [instrument(FIRST)], [_dipping(FIRST)]).run

    bought, sold = card.fills
    assert (bought.px, bought.price_ts_ns) == (_close(FIRST, 0) - 0.01, _stamp("09:31"))
    assert bought.cost == pytest.approx(bought.notional * WIDTHS[FIRST]["open"] / 2 / BPS)
    stamp, part = ("16:01", "post") if latency_ms else ("16:00", "rth")  # its own bar opens 16:01
    assert sold.price_ts_ns == _stamp(stamp)
    assert sold.cost == pytest.approx(sold.notional * WIDTHS[FIRST][part] / 2 / BPS)
    reads = [float(line) for line in record.read_text().split()]
    held = 100 * (_close(FIRST, 1) - bought.px)  # marked at the bar stamped 09:30, the last read
    assert reads[0] == pytest.approx(CAPITAL + held - bought.cost, rel=1e-12)
    assert reads[-1] == pytest.approx(card.equity[-1], rel=1e-12)


@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
@pytest.mark.parametrize("order", [(FIRST, SECOND), (SECOND, FIRST)])
def test_the_live_code_path_charges_every_fill_the_row_the_backtest_does(
    order: tuple[str, str], latency_ms: float
) -> None:
    """A replay runs the same sleeve on a node: the same fills, priced at the same instants and
    charged the same rows, element by element."""
    from kanso.nautilus import session

    request = _request(latency_ms)
    instruments = [instrument(FIRST), instrument(SECOND)]
    groups = [_bars(symbol) for symbol in order]

    researched = execute(request, instruments, groups).run
    replayed = session.run_node(request, instruments, groups).result.run

    assert replayed.fills == researched.fills
    assert replayed.equity == researched.equity


HEDGE = b"""
from pathlib import Path

from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""
    limit: float = 0.0


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.sent = False
        self.hedged = False

    def on_bar(self, bar):
        name = bar.bar_type.instrument_id
        if self.session.minute == 568 and name.symbol.value == "LEAD" and not self.sent:
            self.sent = True
            self.submit_entry(name, "BUY", qty=100, price=self.kanso_config.limit)
        if self.session.minute == 964:
            with Path(self.kanso_config.record).open("a") as out:
                out.write(f"{self.balance!r}\\n")

    def on_order_filled(self, event):
        if event.instrument_id.symbol.value == "LEAD" and not self.hedged:
            self.hedged = True
            self.submit_entry(InstrumentId.from_str("LAGG.XNAS"), "BUY", qty=100)
"""


@pytest.mark.parametrize("order", [(FIRST, SECOND), (SECOND, FIRST)])
def test_an_order_a_fill_handler_sends_is_priced_at_the_market_the_venue_had(
    order: tuple[str, str], tmp_path: Path
) -> None:
    """With no latency a resting limit is filled on its own name's bar stamped 09:31, and its
    fill handler buys the other name there and then: where the venue has not applied that
    name's bar of 09:31 yet, the hedge fills at its 09:30 close, a pre-market price, and pays
    `pre`; where it has, at its bar of 09:31, and pays `open`. Either way the instant charged
    is the bar the price came from, and the balance once flat is the equity."""
    record = tmp_path / "balance.txt"
    request = replace(
        _request(0.0),
        strategy_source=HEDGE,
        overrides={"record": str(record), "limit": _close(FIRST, 0) - 0.01},
    )
    feeds = {FIRST: _dipping(FIRST), SECOND: _bars(SECOND)}
    card = execute(
        request, [instrument(FIRST), instrument(SECOND)], [feeds[name] for name in order]
    ).run

    hedge = next(fill for fill in card.fills if fill.instrument_id.startswith(SECOND))
    stamp = STAMPS[round((hedge.px - _close(SECOND, 0)) / 0.01)]
    assert stamp == ("09:30" if order[0] == FIRST else "09:31")
    assert hedge.price_ts_ns == _stamp(stamp)
    part = "pre" if stamp == "09:30" else "open"
    assert hedge.cost == pytest.approx(hedge.notional * WIDTHS[SECOND][part] / 2 / BPS)
    (balance,) = [float(line) for line in record.read_text().split()[-1:]]
    assert balance == pytest.approx(card.equity[-1], rel=1e-12)


DELAY_NS = 15 * MINUTE_NS

SEEN = b"""
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        session = self.session
        self._write(f"{session.minute} {session.segment}")
        if session.minute == 959:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100)
        elif session.minute == 961:
            self.submit_exit(bar.bar_type.instrument_id)

    def on_order_filled(self, event):
        self._write(f"filled {self.balance!r}")

    def _write(self, line):
        with Path(self.kanso_config.record).open("a") as out:
            out.write(line + "\\n")
"""


@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_a_bar_published_after_its_close_is_charged_the_minute_it_covers(
    latency_ms: float, tmp_path: Path
) -> None:
    """A bar stamped 16:00 and published at 16:15 covers the last regular minute: the sleeve
    reads it as `rth`, and a buy priced at its close pays `rth`, not the post-market width of
    the instant it was published at."""
    record = tmp_path / "seen.txt"
    hyp = _hypothesis(latency_ms).model_dump(mode="json")
    hyp["universe"] = [f"{FIRST}.{VENUE}"]
    hyp["costs"]["dated"] = _rows(FIRST)
    request = replace(
        _request(latency_ms),
        hyp=Hypothesis.model_validate(hyp),
        strategy_source=SEEN,
        overrides={"record": str(record)},
    )
    delayed = tuple(
        Bar(bar.bar_type, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.ts_event, ts)
        for bar in _bars(FIRST)
        for ts in (bar.ts_event + DELAY_NS,)
    )
    card = execute(request, [instrument(FIRST)], [delayed]).run

    bought = card.fills[0]
    stamp = "16:01" if latency_ms else "16:00"  # under a latency, the next bar's close
    assert bought.px == _close(FIRST, STAMPS.index(stamp))
    assert bought.price_ts_ns == _stamp(stamp)
    part = "post" if latency_ms else "rth"
    assert bought.cost == pytest.approx(bought.notional * WIDTHS[FIRST][part] / 2 / BPS)
    assert "959 rth" in record.read_text()
    filled = [line for line in record.read_text().splitlines() if line.startswith("filled")]
    balance = float(filled[-1].split()[-1])  # read as the exit filled, flat
    assert balance == pytest.approx(card.equity[-1], rel=1e-12)


PRINTS = b"""
from kanso.nautilus.strategy import KansoStrategy


class Strategy(KansoStrategy):
    def on_start(self):
        self.sent = False

    def on_trade_tick(self, tick):
        if tick.instrument_id.symbol.value == "LEAD" and self.session.minute == 959:
            if not self.sent:
                self.sent = True
                self.submit_entry(tick.instrument_id, "BUY", qty=100)
"""


def _print(symbol: str, hhmmss: str, px: float, index: int) -> TradeTick:
    hours, minutes, seconds = (float(part) for part in hhmmss.split(":"))
    ts = midnight_ns(DAY) + int(((hours + 5) * 3600 + minutes * 60 + seconds) * SECOND_NS)
    return TradeTick(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        Price(px, 2),
        Quantity.from_int(1000),
        AggressorSide.BUYER,
        TradeId(f"{symbol}-{index}"),
        ts,
        ts,
    )


@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_a_name_read_as_prints_pays_the_part_of_the_print_its_price_came_from(
    latency_ms: float,
) -> None:
    """A buy sent on LEAD's print at 15:59:59.5 is priced at that print, `rth`. Under a latency
    it lands on LAGG's print at 16:00:00.5, in the post-market, and fills at LEAD's last
    print all the same: it pays `rth`, and records that print's instant."""
    hyp = _hypothesis(latency_ms).model_dump(mode="json")
    hyp.update(resolution="trade", data_requirements=["trade"])
    request = replace(
        _request(latency_ms), hyp=Hypothesis.model_validate(hyp), strategy_source=PRINTS
    )
    feeds = [
        (_print(FIRST, "15:58:30", 10.0, 0), _print(FIRST, "15:59:59.5", 10.01, 1)),
        (_print(SECOND, "15:59:58", 20.0, 0), _print(SECOND, "16:00:00.5", 20.01, 1)),
    ]
    card = execute(request, [instrument(FIRST), instrument(SECOND)], feeds).run

    (bought,) = card.fills
    priced = midnight_ns(DAY) + int(((15 + 5) * 3600 + 59 * 60 + 59.5) * SECOND_NS)
    assert bought.px == pytest.approx(10.01)
    assert bought.price_ts_ns == priced
    assert bought.ts_ns == (priced if latency_ms == 0.0 else priced + SECOND_NS)
    assert bought.cost == pytest.approx(bought.notional * WIDTHS[FIRST]["rth"] / 2 / BPS)


def test_a_cost_multiple_of_one_reproduces_a_dated_run() -> None:
    from kanso.criteria.gates import stressed

    card = _run((FIRST, SECOND), 10.0)
    assert stressed(card, 1.0) == card
