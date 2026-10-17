"""A book: several sleeves on one account, handed room from one capital in the book's order.

Every test runs the real engine over the synthetic daily bars of `conftest`, published at
16:00:01 UTC — 11:00 in New York in January, inside regular hours — so each member's entries
of a session are made at one instant and the ledger places them when it closes.
"""

from __future__ import annotations

import pickle
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import fsum
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.objects import Quantity

from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus import book as shared
from kanso.nautilus.backtest import RunRequest, execute
from kanso.nautilus.book import (
    BELOW_C_MIN,
    NAME_CAP,
    OPPOSITE_REFUSED,
    OUTSIDE_RTH_UNSET,
    ROOM,
    TOUCH,
    UNFILLED,
    BookMember,
    execute_book,
    ranked,
    run_book,
    segment_of,
)
from kanso.nautilus.sizing import BOOK_ENTRY, SizingError
from kanso.schemas import Hypothesis, NetPerTrade, SharedBook

from .conftest import (
    CAPITAL,
    RESEARCH,
    SECOND_NS,
    SLEEVE,
    SNAPSHOT,
    bars,
    catalog,
    hypothesis,
    instrument,
    quotes,
    venue_model,
)

DEMO = "DEMO.XNAS"
OTHR = "OTHR.XNAS"
JOINED = datetime(2026, 10, 1, tzinfo=UTC)

TAKER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 0.0
    qty: float = 0.0
    edge: float = -1.0
    sell: float = 0.0
    limit: float = 0.0
    once: float = 1.0
    skip: float = 0.0
    twice: float = 0.0
    exit_on: float = 0.0
    rest_exit: float = 0.0


class Strategy(KansoStrategy):
    \"\"\"Enters each name it is handed a bar of with what its parameters say: once, after
    `skip` bars of the name, or on every bar; `twice` asks twice in the handler; `exit_on`
    sends the name's exit at market on that bar of it, and `rest_exit` rests one at that
    price on the bar after it entered.\"\"\"

    config_cls = Config

    def on_start(self):
        self.done = set()
        self.seen = {}

    def on_bar(self, bar):
        instrument_id = bar.bar_type.instrument_id
        key = str(instrument_id)
        cfg = self.kanso_config
        self.seen[key] = self.seen.get(key, 0) + 1
        if cfg.exit_on and self.seen[key] == cfg.exit_on:
            self.submit_exit(instrument_id)
        if cfg.rest_exit and key in self.done and self.seen[key] == cfg.skip + 2:
            self.submit_exit(instrument_id, price=cfg.rest_exit)
        if self.seen[key] <= cfg.skip or (cfg.once and key in self.done):
            return
        self.done.add(key)
        for _ in range(2 if cfg.twice else 1):
            self.submit_entry(
                instrument_id,
                "SELL" if cfg.sell else "BUY",
                notional=cfg.notional or None,
                qty=cfg.qty or None,
                price=cfg.limit or None,
                edge_bp=None if cfg.edge < 0 else cfg.edge,
            )
"""

BLIND = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Asks for every name of its universe on its first bar, priced or not.\"\"\"

    config_cls = Config

    def on_start(self):
        self.asked = False

    def on_bar(self, bar):
        if not self.asked:
            self.asked = True
            for instrument_id in self.universe:
                self.submit_entry(instrument_id, "BUY", notional=1_000.0)
"""

HAND_BUILT = b"""
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        self.submit_order(
            self.order_factory.market(
                bar.bar_type.instrument_id, OrderSide.BUY, Quantity.from_int(1)
            )
        )
"""


def hyp(universe: tuple[str, ...], **extra: Any) -> Hypothesis:
    """`conftest.hypothesis` at four times leverage with the whole of it on one name."""
    document = hypothesis(universe=universe, max_leverage=4.0, **extra).model_dump(mode="json")
    document["risk_limits"]["max_position_pct"] = 400.0
    return Hypothesis.model_validate(document)


def request(
    subject: Hypothesis, source: bytes = TAKER, *, capital: float = CAPITAL, **overrides: float
) -> RunRequest:
    return RunRequest(
        hyp=subject,
        strategy_source=source,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=venue_model(subject),
        capital=capital,
        overrides=overrides,
    )


def net(value: float, sessions: int = 20, t: float | None = 2.0) -> NetPerTrade:
    return NetPerTrade(
        net_per_trade_bp=value,
        net_se_session=None if t is None else value / t,
        net_t=t,
        sessions_with_trades=sessions,
        trades=sessions * 2,
    )


def member(
    member_id: str,
    run: RunRequest,
    value: float | None = None,
    joined: datetime = JOINED,
) -> BookMember:
    return BookMember(
        id=member_id,
        version=1,
        joined_at=joined,
        request=run,
        net=None if value is None else net(value),
    )


def block(**extra: Any) -> SharedBook:
    return SharedBook.model_validate(
        {
            "capital": CAPITAL,
            "leverage": {"rth": 4},
            "rank_key": "net_per_trade_bp",
            "k_min": 15,
            **extra,
        }
    )


def daily(symbol: str) -> tuple[tuple[object, ...], ...]:
    return (tuple(bars(RESEARCH, symbol=symbol)),)


INSTRUMENTS = (instrument("DEMO"), instrument("OTHR"))


def placed(run: Any, instrument_id: str) -> float:
    """What a member's first fill in a name bought, in shares."""
    return fsum(f.qty for f in run.fills if f.instrument_id == instrument_id and f.side == "BUY")


# --- one member, two members, the book's curve ----------------------------------------


def test_a_book_of_one_member_is_that_member_run_alone() -> None:
    """Byte for byte: the same fills, costs, trades and equity, and the book's curve is the
    member's own."""
    alone = request(hypothesis(), SLEEVE)

    solo = execute(alone, [instrument()], daily("DEMO"))
    booked = execute_book(block(), [member("demo", alone, 3.0)], [instrument()], [daily("DEMO")])

    (run,) = booked.runs
    assert pickle.dumps(run) == pickle.dumps(solo.run)
    assert booked.intents == (solo.intents,)
    assert booked.equity == solo.run.equity and booked.period_ends_ns == solo.run.period_ends_ns
    assert all(not any(counts.values()) for counts in booked.counts.values())


def test_two_members_on_disjoint_names_with_room_to_spare_each_run_as_alone() -> None:
    """Each member's run is its run alone, its costs charged once, and the book's equity is
    the capital plus what each made."""
    first = request(hyp((DEMO,)), SLEEVE)
    second = request(hyp((OTHR,)), SLEEVE)

    booked = execute_book(
        block(),
        [member("a", first, 2.0), member("b", second, 1.0)],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )

    solos = [
        execute(first, INSTRUMENTS, daily("DEMO")).run,
        execute(second, INSTRUMENTS, daily("OTHR")).run,
    ]
    assert [pickle.dumps(run) for run in booked.runs] == [pickle.dumps(run) for run in solos]
    assert fsum(f.cost for run in booked.runs for f in run.fills) == fsum(
        f.cost for run in solos for f in run.fills
    )
    assert booked.equity[-1] == pytest.approx(
        CAPITAL + fsum(run.equity[-1] - CAPITAL for run in solos)
    )
    assert booked.cuts == ()


def test_the_book_is_the_same_twice() -> None:
    def once() -> bytes:
        made = execute_book(
            block(),
            [
                member("a", request(hyp((DEMO,)), notional=300_000.0), 2.0),
                member("b", request(hyp((OTHR,)), notional=300_000.0), 1.0),
            ],
            INSTRUMENTS,
            [daily("DEMO"), daily("OTHR")],
        )
        return pickle.dumps((made.runs, made.equity, made.cuts, made.counts, made.gross))

    assert once() == once()


# --- the order: edge, then rank, never the stream ---------------------------------------


def contended(a_value: float | None, b_value: float | None, **b: float) -> Any:
    """Two members each asking for 300,000 of the 400,000 the book has, at one instant: `a`
    on DEMO, whose bar the stream hands over first, and `b` on OTHR."""
    return execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=300_000.0), a_value),
            member("b", request(hyp((OTHR,)), notional=300_000.0, **b), b_value),
        ],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )


def test_the_higher_ranked_member_takes_the_room_whatever_order_the_stream_serves_them() -> None:
    """`a`'s point arrives first; `b` ranks first by its certificate's net per trade, so it
    is placed whole and `a` is cut to what the room leaves — and the other way round when the
    ranks are swapped."""
    b_first = contended(1.0, 5.0)
    a_first = contended(5.0, 1.0)

    (cut,) = b_first.cuts
    assert (cut.member, cut.instrument_id, cut.by) == ("a", DEMO, ROOM)
    assert b_first.members == ("b", "a")
    (cut,) = a_first.cuts
    assert (cut.member, cut.instrument_id, cut.by) == ("b", OTHR, ROOM)
    assert placed(b_first.runs[0], OTHR) == placed(a_first.runs[0], DEMO) == 300_000.0 / 10.0


RESERVE = 1 + 2 * (1.0 + 2.0 + 4.0) / 10_000
"""What a notional is left over for its own round trip under `conftest`'s costs."""


def test_the_room_left_is_what_the_cut_member_is_placed() -> None:
    """The second entry takes what is left of `4 × capital` with the first in flight at the
    price it was sized at — nothing has filled, so the balance is the capital — over its own
    round trip: nothing is skipped whole."""
    made = contended(5.0, 1.0)

    (a, b) = made.runs
    assert placed(a, DEMO) == 30_000.0
    left = 4 * CAPITAL - 30_000.0 * 10.0
    assert placed(b, OTHR) == int(left / RESERVE / 10.0)
    (cut,) = made.cuts
    assert cut.placed == placed(b, OTHR) < cut.asked
    assert made.at_room_limit >= 1 and (made.room_limit_share or 0.0) > 0.0
    assert made.peak_gross is not None and made.peak_gross <= 4 * CAPITAL


def test_an_entry_s_edge_comes_before_its_member_s_rank() -> None:
    """`b` ranks second but states the larger edge, so it is placed first; `a`, with none,
    ranks after every entry that states one."""
    made = contended(5.0, 1.0, edge=12.0)

    (cut,) = made.cuts
    assert cut.member == "a"


def test_the_members_order_is_the_book_s_whatever_order_they_are_named_in() -> None:
    one = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=300_000.0), 5.0),
            member("b", request(hyp((OTHR,)), notional=300_000.0), 1.0),
        ],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )
    other = execute_book(
        block(),
        [
            member("b", request(hyp((OTHR,)), notional=300_000.0), 1.0),
            member("a", request(hyp((DEMO,)), notional=300_000.0), 5.0),
        ],
        INSTRUMENTS,
        [daily("OTHR"), daily("DEMO")],
    )

    assert one.members == other.members == ("a", "b")
    assert pickle.dumps(one.runs) == pickle.dumps(other.runs)


def test_ranked_orders_by_net_then_t_then_joining_then_id_and_unranked_last() -> None:
    run = request(hyp((DEMO,)))
    early = JOINED - timedelta(days=1)
    members = [
        member("unranked_low_sessions", run),
        replace(member("thin", run), net=net(9.0, sessions=14)),
        replace(member("negative", run), net=net(-1.0, t=-2.0)),
        member("b_tied", run, 3.0),
        member("a_tied", run, 3.0),
        member("early_tied", run, 3.0, joined=early),
        replace(member("higher_t", run), net=net(3.0, t=5.0)),
        member("best", run, 7.0),
        replace(member("no_t", run), net=net(3.0, t=None)),
    ]

    order = [m.id for m in ranked(members, block())]

    assert order == [
        "best",
        "higher_t",
        "early_tied",
        "a_tied",
        "b_tied",
        "no_t",
        "negative",
        "thin",
        "unranked_low_sessions",
    ]


# --- the caps ------------------------------------------------------------------------


def test_a_name_cap_cuts_the_book_s_holding_in_one_name() -> None:
    made = execute_book(
        block(max_name_c=0.5),
        [member("a", request(hyp((DEMO,)), notional=80_000.0), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )

    (cut,) = made.cuts
    assert (cut.by, cut.asked, cut.placed) == (NAME_CAP, 8_000.0, 5_000.0)
    assert placed(made.runs[0], DEMO) == 0.5 * CAPITAL / 10.0


def test_a_cut_below_a_lot_is_refused_and_counted_by_the_cap_that_bound_it() -> None:
    """`a` takes 300,000 of the 400,000; `b` is cut to the room over its round trip; `c` to
    the sliver that round trip left; `d` finds less than a share and is refused by the room.
    A name cap below one share refuses by the cap."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=300_000.0), 9.0),
            member("b", request(hyp((OTHR,)), notional=300_000.0), 7.0),
            member("c", request(hyp((OTHR,)), notional=300_000.0), 5.0),
            member("d", request(hyp((OTHR,)), notional=300_000.0), 3.0),
        ],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR"), daily("OTHR"), daily("OTHR")],
    )

    assert [cut.member for cut in made.cuts] == ["b", "c"]
    assert made.counts["d"][ROOM] == 1
    assert placed(made.runs[3], OTHR) == 0.0
    assert made.sessions[date(2024, 1, 1)]["d"][ROOM] == 1
    capped = execute_book(
        block(max_name_c=0.00005),
        [member("a", request(hyp((DEMO,)), notional=1_000.0), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )
    assert capped.counts["a"][NAME_CAP] == 1 and capped.runs[0].fills == ()


def test_an_entry_its_own_rule_sizes_below_a_lot_is_counted_below_its_minimum() -> None:
    made = execute_book(
        block(),
        [member("a", request(hyp((DEMO,)), notional=5.0), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )

    assert made.counts["a"][BELOW_C_MIN] == 1
    assert made.runs[0].fills == ()


def quoted(symbol: str, ask_size: int) -> tuple[QuoteTick, ...]:
    """`conftest.quotes` with the ask displaying `ask_size`."""
    return tuple(
        QuoteTick(
            InstrumentId(Symbol(symbol), Venue("XNAS")),
            q.bid_price,
            q.ask_price,
            q.bid_size,
            Quantity.from_int(ask_size),
            q.ts_event,
            q.ts_init,
        )
        for q in quotes(RESEARCH, symbol=symbol)
    )


def test_takers_of_one_name_at_one_instant_share_the_size_its_touch_displays() -> None:
    """Two members buying 80 each where the ask shows 100: the first is placed whole, the
    second cut to 20 — the touch, not the room, binds — and the quote the bar shares its
    instant with is the one read, because entries are placed when the instant closes."""
    subject = hyp((DEMO,), data_requirements=("bar", "quote"))
    feed = (tuple(bars(RESEARCH)), quoted("DEMO", 100))
    a = member("a", request(subject, qty=80.0), 5.0)
    b = member("b", request(subject, qty=80.0), 1.0)

    made = execute_book(block(), [a, b], [instrument()], [feed, feed])
    listed_last = execute_book(block(), [b, a], [instrument()], [feed, feed])

    (cut,) = made.cuts
    assert (cut.member, cut.asked, cut.placed, cut.by) == ("b", 80.0, 20.0, TOUCH)
    assert placed(made.runs[0], DEMO) == 80.0 and placed(made.runs[1], DEMO) == 20.0
    assert listed_last.cuts == made.cuts, "the engine serves `b` first; the book places `a`"
    assert pickle.dumps(listed_last.runs) == pickle.dumps(made.runs)


def test_a_third_taker_finds_the_touch_spent_and_is_counted() -> None:
    subject = hyp((DEMO,), data_requirements=("bar", "quote"))
    feed = (tuple(bars(RESEARCH)), quoted("DEMO", 100))

    made = execute_book(
        block(),
        [
            member("a", request(subject, qty=60.0), 5.0),
            member("b", request(subject, qty=60.0), 3.0),
            member("c", request(subject, qty=60.0), 1.0),
        ],
        [instrument()],
        [feed, feed, feed],
    )

    assert made.counts["c"][TOUCH] == 1
    assert placed(made.runs[2], DEMO) == 0.0


def test_an_entry_opposite_to_a_side_the_book_holds_is_refused() -> None:
    """`a` buys DEMO; `b` asks to sell it short at the same instant and is refused before it
    is sized, however it ranks against `a` by edge."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=10_000.0), 5.0),
            member("b", request(hyp((DEMO,)), notional=10_000.0, sell=1.0, edge=-1.0), 1.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )

    assert made.counts["b"][OPPOSITE_REFUSED] == 1
    assert made.runs[1].fills == ()


def test_an_entry_opposite_to_a_resting_entry_is_refused_and_an_unfilled_rest_is_counted() -> None:
    """`a` rests a buy far under the market, which never fills; `b`'s sell of the name is
    opposite to that working entry."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), qty=10.0, limit=1.0), 5.0),
            member("b", request(hyp((DEMO,)), notional=10_000.0, sell=1.0), 1.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )

    assert made.counts["b"][OPPOSITE_REFUSED] == 1
    assert made.counts["a"][UNFILLED] == 1


def test_an_entry_outside_regular_hours_is_refused_while_their_leverage_is_unset() -> None:
    late = tuple(
        replace_ts(point, -14 * 3600 * SECOND_NS) for point in bars(RESEARCH)
    )  # 02:00 UTC: 21:00 in New York the evening before
    made = execute_book(
        block(),
        [member("a", request(hyp((DEMO,)), notional=10_000.0), 1.0)],
        [instrument()],
        [(late,)],
    )

    assert made.counts["a"][OUTSIDE_RTH_UNSET] == 1
    assert made.runs[0].fills == ()


def replace_ts(bar: Any, shift: int) -> Any:
    from nautilus_trader.model.data import Bar

    return Bar(
        bar.bar_type,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        ts_event=bar.ts_event + shift,
        ts_init=bar.ts_init + shift,
    )


def test_segments_are_new_york_s_clock_across_daylight_saving() -> None:
    def at(day: date, hour: int, minute: int = 0) -> int:
        return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp()) * (
            SECOND_NS
        )

    winter, summer = date(2024, 1, 2), date(2024, 7, 2)
    assert segment_of(at(winter, 14, 29)) == ("pre", winter)
    assert segment_of(at(winter, 14, 30)) == ("rth", winter)
    assert segment_of(at(winter, 21, 0)) == ("post", winter)
    assert segment_of(at(summer, 13, 30)) == ("rth", summer)
    assert segment_of(at(summer, 20, 0)) == ("post", summer)
    assert segment_of(at(summer, 23, 30)) == ("post", summer)
    assert segment_of(at(summer, 0, 30)) == ("overnight", date(2024, 7, 1))
    assert segment_of(at(summer, 3, 0)) == ("overnight", date(2024, 7, 1))


# --- what a book refuses --------------------------------------------------------------


def test_an_entry_built_by_hand_in_a_book_is_refused() -> None:
    with pytest.raises(SizingError) as raised:
        execute_book(
            block(),
            [member("a", request(hyp((DEMO,)), HAND_BUILT), 1.0)],
            [instrument()],
            [daily("DEMO")],
        )

    assert raised.value.refusal.rule == BOOK_ENTRY


def test_an_edge_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValidationError, match="edge_bp"):
        execute_book(
            block(),
            [member("a", request(hyp((DEMO,)), notional=1_000.0, edge=float("inf")), 1.0)],
            [instrument()],
            [daily("DEMO")],
        )


@pytest.mark.parametrize(
    ("members", "said"),
    [
        ([], "no member"),
        ([member("a", request(hyp((DEMO,)))), member("a", request(hyp((DEMO,))))], "twice"),
        ([member("a", request(hyp((DEMO,)), capital=50_000.0))], "book's capital"),
        (
            [
                member("a", request(hyp((DEMO,)))),
                member(
                    "b",
                    replace(request(hyp((OTHR,))), window=(date(2024, 2, 6), date(2024, 2, 29))),
                ),
            ],
            "one window",
        ),
    ],
)
def test_what_a_book_cannot_hold_is_refused_before_anything_runs(
    members: list[BookMember], said: str
) -> None:
    with pytest.raises(PreconditionError, match=said):
        execute_book(block(), members, INSTRUMENTS, [daily("DEMO")] * len(members))


def test_a_member_with_an_attached_construct_or_a_level_two_book_is_refused() -> None:
    with_filter = replace(request(hyp((DEMO,))), modifiers=(("filter", b"", {}),))
    with pytest.raises(PreconditionError, match="attached construct"):
        execute_book(block(), [member("a", with_filter)], [instrument()], [daily("DEMO")])
    depth = hyp((DEMO,), data_requirements=("book", "trade"), resolution="tick")
    with pytest.raises(PreconditionError, match="level-two book"):
        execute_book(block(), [member("a", request(depth))], [instrument()], [daily("DEMO")])


def test_a_member_under_a_book_policy_is_refused() -> None:
    document = hyp((DEMO,)).model_dump(mode="json")
    document["book"] = {"financing_rate_bps": 500.0}
    with pytest.raises(PreconditionError, match="a `book` policy"):
        execute_book(
            block(),
            [member("a", request(Hypothesis.model_validate(document)))],
            [instrument()],
            [daily("DEMO")],
        )


def test_a_member_s_groups_are_its_own() -> None:
    with pytest.raises(ValidationError, match="each member is handed its own"):
        execute_book(block(), [member("a", request(hyp((DEMO,))))], [instrument()], [])


def test_a_window_the_hypothesis_does_not_declare_is_refused() -> None:
    odd = replace(request(hyp((DEMO,))), window=(date(2024, 1, 2), date(2024, 1, 30)))
    with pytest.raises(PreconditionError, match="is not a window"):
        execute_book(block(), [member("a", odd)], [instrument()], [daily("DEMO")])


def test_a_book_reads_each_member_s_window_from_the_catalog(tmp_path: Path) -> None:
    store = catalog(
        tmp_path / "catalog", [*bars(RESEARCH, "DEMO"), *bars(RESEARCH, "OTHR")], INSTRUMENTS
    )
    members = [
        member("a", request(hyp((DEMO,)), SLEEVE), 2.0),
        member("b", request(hyp((OTHR,)), SLEEVE), 1.0),
    ]

    read = run_book(block(), members, store)

    handed = execute_book(block(), members, INSTRUMENTS, [daily("DEMO"), daily("OTHR")])
    assert pickle.dumps(read.runs) == pickle.dumps(handed.runs)


def test_the_closing_marker_is_the_last_of_each_instant() -> None:
    from kanso.nautilus.cross_section import CLOSES, FLUSH, closed, is_marker, ordered

    points = closed(ordered([bars(RESEARCH, "DEMO"), bars(RESEARCH, "OTHR")], coincident=True))

    markers = [p for p in points if is_marker(p)]
    assert len(markers) == 2 * len(bars(RESEARCH))
    assert [m.data.n for m in markers[:2]] == [FLUSH, CLOSES]
    assert closed(()) == ()
    assert shared.COUNTERS[0] == ROOM


# --- what the room reads once the book holds something ---------------------------------


def test_a_later_instant_s_room_is_struck_on_the_realised_balance_and_what_is_held() -> None:
    """`a` bought on the first session; on the second `b` asks for 300,000. Its room is four
    times the capital less `a`'s costs — `a` has realised nothing else — less what `a` holds at
    its own cost, since no quote has been seen to mark it at."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=300_000.0), 5.0),
            member("b", request(hyp((OTHR,)), notional=300_000.0, skip=1.0), 1.0),
        ],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )

    a, b = made.runs
    bought = [f for f in a.fills if f.side == "BUY"]
    left = 4 * (CAPITAL - fsum(f.cost for f in bought)) - fsum(f.qty * f.px for f in bought)
    assert placed(b, OTHR) == int(left / RESERVE / 10.5)
    (cut,) = made.cuts
    assert (cut.member, cut.by) == ("b", ROOM)
    assert made.mean_gross is not None and made.mean_gross <= 4 * CAPITAL


def test_an_exit_in_flight_counts_as_gone_and_a_resting_exit_holds_nothing_back() -> None:
    """`a` holds both names and sends their exits at market on the session `b` enters DEMO:
    DEMO's lands before the instant closes, OTHR's — handled on the marker that closes it — is
    still in flight when `b` is placed, and what both free is free, so `b` is placed whole. A
    resting exit adds nothing to the book either."""
    both = (tuple(bars(RESEARCH, "DEMO")), tuple(bars(RESEARCH, "OTHR")))
    exiting = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO, OTHR)), notional=150_000.0, exit_on=2.0), 5.0),
            member("b", request(hyp((DEMO,)), notional=390_000.0, skip=1.0), 1.0),
        ],
        INSTRUMENTS,
        [both, daily("DEMO")],
    )
    resting = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=300_000.0, rest_exit=50.0), 5.0),
            member("b", request(hyp((OTHR,)), notional=300_000.0, skip=2.0), 1.0),
        ],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )

    assert exiting.cuts == ()
    assert placed(exiting.runs[1], DEMO) == int(390_000.0 / 10.5)
    a, b = resting.runs
    bought = [f for f in a.fills if f.side == "BUY"]
    left = 4 * (CAPITAL - fsum(f.cost for f in bought)) - fsum(f.qty * f.px for f in bought)
    assert placed(b, OTHR) == int(left / RESERVE / 11.0)
    assert resting.counts["a"][UNFILLED] == 0, "a resting exit is not an entry the book placed"


def test_an_add_in_flight_is_counted_at_its_price_beside_what_is_held() -> None:
    """`a` adds to its holding every session; on the second `b` is cut to what `a`'s holding
    and its add in flight leave."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=100_000.0, once=0.0), 5.0),
            member("b", request(hyp((OTHR,)), notional=300_000.0, skip=1.0), 1.0),
        ],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )

    a = made.runs[0]
    first, second = sorted({f.ts_ns for f in a.fills})[:2]
    held = fsum(f.qty * f.px for f in a.fills if f.ts_ns == first)
    cost = fsum(f.cost for f in a.fills if f.ts_ns == first)
    adding = fsum(f.qty for f in a.fills if f.ts_ns == second)
    left = 4 * (CAPITAL - cost) - held - adding * 10.5
    assert placed(made.runs[1], OTHR) == int(left / RESERVE / 10.5)


def test_an_entry_its_own_room_leaves_nothing_for_or_with_no_price_is_below_its_minimum() -> None:
    """`a` takes its whole allowance on the first session and asks again on the second, when
    the price has risen past it; `blind` asks for a name it has seen no price of."""
    full = execute_book(
        block(),
        [member("a", request(hyp((DEMO,)), notional=399_000.0, once=0.0), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )
    blind = execute_book(
        block(),
        [member("blind", request(hyp((DEMO, OTHR)), BLIND), 1.0)],
        INSTRUMENTS,
        [daily("DEMO")],
    )

    assert full.counts["a"][BELOW_C_MIN] >= 1
    assert blind.counts["blind"][BELOW_C_MIN] == 1
    assert placed(blind.runs[0], DEMO) > 0


def test_an_entry_asked_twice_at_one_instant_is_one_entry() -> None:
    made = execute_book(
        block(),
        [member("a", request(hyp((DEMO,)), notional=10_000.0, twice=1.0), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )

    assert placed(made.runs[0], DEMO) == 1_000.0


def test_a_resting_entry_draws_no_touch_and_one_that_never_fills_is_counted() -> None:
    subject = hyp((DEMO,), data_requirements=("bar", "quote"))
    feed = (tuple(bars(RESEARCH)), quoted("DEMO", 100))

    made = execute_book(
        block(), [member("a", request(subject, qty=150.0, limit=1.0), 1.0)], [instrument()], [feed]
    )

    assert made.cuts == ()
    assert made.counts["a"][UNFILLED] == 1
    assert made.counts["a"][TOUCH] == 0


def test_a_book_that_placed_nothing_has_no_gross() -> None:
    from .conftest import FLAT_SLEEVE

    made = execute_book(
        block(), [member("a", request(hyp((DEMO,)), FLAT_SLEEVE))], [instrument()], [daily("DEMO")]
    )

    assert (made.peak_gross, made.mean_gross, made.gross) == (None, None, ())
    assert made.room_limit_share is None


def test_a_warmed_member_trades_from_its_window_s_open_as_alone() -> None:
    prefix = (date(2023, 12, 29), date(2023, 12, 31))
    warmed = replace(request(hypothesis(), SLEEVE), prefix=prefix)
    feed = (tuple(bars((prefix[0], RESEARCH[1]))),)

    solo = execute(warmed, [instrument()], feed)
    booked = execute_book(block(), [member("a", warmed, 1.0)], [instrument()], [feed])

    assert pickle.dumps(booked.runs[0]) == pickle.dumps(solo.run)


def sized(universe: tuple[str, ...], budget: float) -> RunRequest:
    document = hyp(universe).model_dump(mode="json")
    document["sizing"] = {"mode": "full_book", "budget": budget}
    return replace(request(Hypothesis.model_validate(document)), sleeve_budget=budget, overrides={})


def test_a_sized_member_s_whole_budget_is_cut_to_the_room_like_any_entry() -> None:
    made = execute_book(
        block(),
        [member("a", sized((DEMO,), 300_000.0), 5.0), member("b", sized((OTHR,), 300_000.0), 1.0)],
        INSTRUMENTS,
        [daily("DEMO"), daily("OTHR")],
    )
    capped = execute_book(
        block(max_name_c=0.00005),
        [member("a", sized((DEMO,), 300_000.0), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )

    (cut,) = made.cuts
    assert (cut.member, cut.by) == ("b", ROOM)
    assert 0 < placed(made.runs[1], OTHR) < placed(made.runs[0], DEMO)
    assert capped.counts["a"][NAME_CAP] >= 1
    assert capped.runs[0].fills == ()


def test_a_sleeve_run_alone_ignores_the_edge_it_states() -> None:
    stated = request(hyp((DEMO,)), notional=1_000.0, edge=9.0)
    with_edge = execute(stated, [instrument()], daily("DEMO"))
    without = execute(request(hyp((DEMO,)), notional=1_000.0), [instrument()], daily("DEMO"))

    assert pickle.dumps(with_edge.run) == pickle.dumps(without.run)


HAND_EXITS = b"""
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    sells: float = 1.0


class Strategy(KansoStrategy):
    \"\"\"Buys 1000 on its first bar and, on its third, sells it by hand: `sells` market
    orders of 1000 each, in one handler.\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", qty=1000.0)
        elif self.n == 3:
            for _ in range(int(self.kanso_config.sells)):
                self.submit_order(
                    self.order_factory.market(
                        instrument_id, OrderSide.SELL, Quantity.from_int(1000)
                    )
                )
"""


def test_an_exit_built_by_hand_closes_and_two_that_would_pass_flat_are_an_entry() -> None:
    """One sell by hand of the whole long is an exit; a second in the same handler, with the
    first still in flight, would open a short the book handed no room for, and is refused
    as an entry built by hand, naming the member."""
    one = execute_book(
        block(),
        [member("a", request(hyp((DEMO,)), HAND_EXITS), 1.0)],
        [instrument()],
        [daily("DEMO")],
    )
    with pytest.raises(SizingError) as raised:
        execute_book(
            block(),
            [
                member("a", request(hyp((DEMO,)), HAND_EXITS, sells=2.0), 5.0),
                member("b", request(hyp((DEMO,)), qty=500.0, skip=4.0), 1.0),
            ],
            [instrument()],
            [daily("DEMO"), daily("DEMO")],
        )

    sold = fsum(f.qty for f in one.runs[0].fills if f.side == "SELL")
    assert sold == placed(one.runs[0], DEMO) == 1000.0
    assert raised.value.refusal.rule == BOOK_ENTRY
    assert raised.value.refusal.asked == "SELL"
    assert "book member a" in raised.value.refusal.why


LISTED = b"""
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import ContingencyType, OrderSide, TriggerType
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import LimitOrder, OrderList, StopMarketOrder

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    shape: float = 0.0
    linked: float = 0.0


class Strategy(KansoStrategy):
    \"\"\"Buys 1000 on its first bar and, on its third, sends orders built by hand, by
    `shape`: one list of two market sells of 1000 (0); one list of a market sell of 1 and a
    market buy of 5000 (1); a stop under the market and a take-profit far over it, each a
    sell of 1000, as one list (2) or sent apart (3), linked as the `ContingencyType` whose
    value `linked` is.\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", qty=1000.0)
        elif self.n == 3:
            shape = int(self.kanso_config.shape)
            list_id = self.order_factory.generate_order_list_id()
            if shape == 0:
                orders = [self.market(instrument_id, OrderSide.SELL, 1000) for _ in range(2)]
            elif shape == 1:
                orders = [
                    self.market(instrument_id, OrderSide.SELL, 1),
                    self.market(instrument_id, OrderSide.BUY, 5000),
                ]
            else:
                orders = self.bracketed(instrument_id, list_id if shape == 2 else None)
            if shape == 3:
                for order in orders:
                    self.submit_order(order)
            else:
                self.submit_order_list(OrderList(list_id, orders))

    def market(self, instrument_id, side, qty):
        return self.order_factory.market(instrument_id, side, Quantity.from_int(qty))

    def bracketed(self, instrument_id, list_id):
        contingency = ContingencyType(int(self.kanso_config.linked))
        ids = [self.order_factory.generate_client_order_id() for _ in range(2)]
        common = {
            "trader_id": self.trader_id,
            "strategy_id": self.id,
            "instrument_id": instrument_id,
            "order_side": OrderSide.SELL,
            "quantity": Quantity.from_int(1000),
            "ts_init": self.clock.timestamp_ns(),
            "contingency_type": contingency,
            "order_list_id": list_id,
        }
        linked = contingency != ContingencyType.NO_CONTINGENCY
        return [
            StopMarketOrder(
                client_order_id=ids[0],
                trigger_price=Price.from_str("5.00"),
                trigger_type=TriggerType.DEFAULT,
                init_id=UUID4(),
                linked_order_ids=[ids[1]] if linked else None,
                **common,
            ),
            LimitOrder(
                client_order_id=ids[1],
                price=Price.from_str("50.00"),
                init_id=UUID4(),
                linked_order_ids=[ids[0]] if linked else None,
                **common,
            ),
        ]
"""

NO_CONTINGENCY, OCO, OUO = 0.0, 1.0, 3.0
"""`ContingencyType`'s values in nautilus_trader 1.231.0."""


def listed(**overrides: float) -> BookMember:
    return member("a", request(hyp((DEMO,)), LISTED, **overrides), 5.0)


@pytest.mark.parametrize(
    ("shape", "linked", "asked"),
    [
        (0.0, NO_CONTINGENCY, "SELL"),
        (1.0, NO_CONTINGENCY, "BUY"),
        (2.0, NO_CONTINGENCY, "SELL"),
        (2.0, OUO, "SELL"),
        (3.0, NO_CONTINGENCY, "SELL"),
    ],
    ids=[
        "a-list-of-two-whole-sells",
        "a-list-of-an-exit-and-a-buy",
        "a-list-of-a-stop-and-a-take-profit-unlinked",
        "a-list-of-a-stop-and-a-take-profit-one-updates-other",
        "a-stop-and-a-take-profit-sent-apart",
    ],
)
def test_each_order_a_member_builds_is_checked_and_one_past_flat_is_an_entry(
    shape: float, linked: float, asked: str
) -> None:
    """Against a long of 1000, each second order — the second whole sell of a list, the buy
    of 5000 after a sell of 1, the take-profit beside a stop each for all of it, listed or
    sent apart, unlinked or linked one-updates-other, which the venue can fill both of — would
    take the position past flat or open one, and is refused as an entry built by hand, naming
    the member, before it reaches the venue."""
    with pytest.raises(SizingError) as raised:
        execute_book(block(), [listed(shape=shape, linked=linked)], [instrument()], [daily("DEMO")])

    assert raised.value.refusal.rule == BOOK_ENTRY
    assert raised.value.refusal.asked == asked
    assert "book member a" in raised.value.refusal.why


@pytest.mark.parametrize("shape", [2.0, 3.0], ids=["listed", "sent-apart"])
def test_a_stop_and_a_take_profit_linked_one_cancels_other_are_one_exit(shape: float) -> None:
    """The venue never fills both of two orders linked one-cancels-other, so a stop and a
    take-profit each for the whole long, listed or sent apart, are one exit, and the book
    counts no short in them: `b`'s later buy of the name is placed, not refused as opposite."""
    made = execute_book(
        block(),
        [
            listed(shape=shape, linked=OCO),
            member("b", request(hyp((DEMO,)), qty=500.0, skip=4.0), 1.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )

    a, b = made.members.index("a"), made.members.index("b")
    rested = [(side, qty, kind) for _, _, side, qty, kind, _ in made.intents[a] if side == "SELL"]
    assert rested == [("SELL", 1000.0, "STOP_MARKET"), ("SELL", 1000.0, "LIMIT")]
    assert made.counts["b"][OPPOSITE_REFUSED] == 0
    assert placed(made.runs[b], DEMO) == 500.0


NESTED = b"""
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Asks for one entry on its first bar and, as the engine reports the order the book
    placed for it submitted, buys a million shares by hand from that handler.\"\"\"

    config_cls = Config

    def on_start(self):
        self.asked = False
        self.sent = False

    def on_bar(self, bar):
        if not self.asked:
            self.asked = True
            self.submit_entry(bar.bar_type.instrument_id, "BUY", ENTRY)

    def on_order_submitted(self, event):
        if not self.sent:
            self.sent = True
            self.submit_order(
                self.order_factory.market(
                    event.instrument_id, OrderSide.BUY, Quantity.from_int(1_000_000)
                )
            )
"""


@pytest.mark.parametrize("sizing", ["own-rule", "sizing-rule"])
def test_an_order_sent_while_the_book_s_placement_is_submitted_is_checked(sizing: str) -> None:
    """The engine runs the member's `on_order_submitted` inside the submission of the order
    the book placed for it, and only that order is let through unasked: a buy of a million
    shares sent by hand from the handler is refused as an entry built by hand, naming the
    member once. Measured on the round before this test: it was let through with the book's
    order, and on a capital of 100,000 the member bought 1,001,000 shares under its own rule
    and 1,000,997 under a sizing rule."""
    if sizing == "own-rule":
        run = request(hyp((DEMO,)), NESTED.replace(b"ENTRY", b"qty=1000.0"))
    else:
        run = replace(sized((DEMO,), 10_000.0), strategy_source=NESTED.replace(b", ENTRY", b""))
    with pytest.raises(SizingError) as raised:
        execute_book(block(), [member("a", run, 5.0)], [instrument()], [daily("DEMO")])

    assert raised.value.refusal.rule == BOOK_ENTRY
    assert raised.value.refusal.asked == "BUY"
    assert raised.value.refusal.why.startswith("book member a: ")
    assert raised.value.refusal.why.count("book member a") == 1


def test_an_entry_opposite_to_a_rest_from_an_earlier_instant_or_a_later_holding_is_refused() -> (
    None
):
    """`b` sells DEMO on a later session than `a` rested a buy under the market, or than
    `a`, ranked below it, bought: each is a side the book works or holds."""
    resting = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), qty=10.0, limit=1.0), 1.0),
            member("b", request(hyp((DEMO,)), notional=10_000.0, sell=1.0, skip=3.0), 5.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )
    held = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), notional=10_000.0), 1.0),
            member("b", request(hyp((DEMO,)), notional=10_000.0, sell=1.0, skip=4.0), 5.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )

    for made in (resting, held):
        assert made.counts["b"][OPPOSITE_REFUSED] == 1
        assert made.runs[made.members.index("b")].fills == ()


GROWN_REST = b"""
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Buys 1000, rests its exit far over the market, then grows that rest to 2000.\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", qty=1000.0)
        elif self.n == 2:
            self.rest = self.submit_exit(instrument_id, price=100.0)
        elif self.n == 3:
            self.modify_order(self.rest, quantity=Quantity.from_int(2000))
"""


def test_a_resting_order_that_would_take_a_position_past_flat_works_the_other_side() -> None:
    """`a`'s rest of 2000 against its long of 1000 would leave it short 1000 if it filled, so
    the book counts that short as worked: `b`'s buy is opposite to it."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), GROWN_REST), 5.0),
            member("b", request(hyp((DEMO,)), qty=500.0, skip=4.0), 1.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )

    assert made.counts["b"][OPPOSITE_REFUSED] == 1
    assert made.runs[1].fills == ()


SIZED_BLIND = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Asks, on its first bar, for every name of its universe it has seen no price of.\"\"\"

    config_cls = Config

    def on_start(self):
        self.asked = False

    def on_bar(self, bar):
        if not self.asked:
            self.asked = True
            for instrument_id in self.universe:
                if instrument_id != bar.bar_type.instrument_id:
                    self.submit_entry(instrument_id, "BUY")
"""


def test_a_sized_entry_with_no_price_to_size_it_is_below_its_minimum() -> None:
    sized_blind = replace(sized((DEMO, OTHR), 300_000.0), strategy_source=SIZED_BLIND)

    made = execute_book(block(), [member("a", sized_blind, 1.0)], INSTRUMENTS, [daily("DEMO")])

    assert made.counts["a"][BELOW_C_MIN] == 1
    assert made.runs[0].fills == ()


RESTER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Rests a buy under the market and cancels it, rests another it leaves, then buys
    twice at a limit over the market, which fills.\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        if self.n in (1, 3):
            self.submit_entry(instrument_id, "BUY", qty=10.0, price=1.0)
        elif self.n == 2:
            self.cancel_all_orders(instrument_id)
        elif self.n in (4, 5):
            self.submit_entry(instrument_id, "BUY", qty=10.0, price=1000.0)
"""


def test_a_rest_the_venue_closed_unfilled_is_counted_in_its_own_session_and_a_filled_not() -> None:
    """The rest cancelled on the second session is counted unfilled in the first's, the one
    left working at the window's end in the third's, and the two that filled not at all."""
    made = execute_book(
        block(), [member("a", request(hyp((DEMO,)), RESTER), 1.0)], [instrument()], [daily("DEMO")]
    )

    assert placed(made.runs[0], DEMO) == 20.0
    assert made.counts["a"][UNFILLED] == 2
    days = sorted(day for day, by in made.sessions.items() if by["a"][UNFILLED])
    assert len(days) == 2 and days[0] < days[1]


def test_a_refusal_raised_while_the_book_places_an_entry_names_its_member() -> None:
    """The book places `a`'s entry from the handler of whichever member closed the instant,
    so its sizing rule's refusal says whose entry it was."""
    with pytest.raises(SizingError) as raised:
        execute_book(
            block(), [member("a", sized((DEMO,), 1.0), 1.0)], [instrument()], [daily("DEMO")]
        )

    assert raised.value.refusal.rule == "budget_below_lot"
    assert raised.value.refusal.why.startswith("book member a: ")
