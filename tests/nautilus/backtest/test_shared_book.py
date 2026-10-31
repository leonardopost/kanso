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
from tests.own_bar_size import hidden_after_its_body

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


def test_a_member_whose_config_hides_bar_resolution_trades_the_book_as_it_trades_alone() -> None:
    """A book rebuilds each member's config with its order tag, from the fields as attributes. An
    attribute the file sets on its `Config` once the class is made hides `bar_resolution` from
    that read; the rebuild carries the size kanso set, so the member reads the file's daily bars
    in the book as it does alone, where the attribute's 5m would have it subscribe none of them."""
    alone = request(hypothesis(), hidden_after_its_body(SLEEVE))

    solo = execute(alone, [instrument()], daily("DEMO"))
    booked = execute_book(block(), [member("demo", alone, 3.0)], [instrument()], [daily("DEMO")])

    assert solo.intents, "alone, the member trades the daily bars"
    assert booked.intents == (solo.intents,)


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


BY_HAND = b"""
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import OrderList

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    how: float = 0.0


class Strategy(KansoStrategy):
    \"\"\"Buys 1000 on its first bar and, on its third, sends an order past `submit_entry` and
    `submit_exit`, by `how`: a sale of the whole long by `submit_order` (0), the same sale as a
    list of one by `submit_order_list` (1), the exit it rested far over the market on its
    second bar moved down to the market by `modify_order` (2), or the long closed by the
    engine's own `close_position` (3), or by its `market_exit` (5); or it asks, from `hedges`,
    for a buy of 5000 beside its entry (4).\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        how = int(self.kanso_config.how)
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", qty=1000.0)
        elif self.n == 2 and how == 2:
            self.rest = self.submit_exit(instrument_id, price=100.0)
        elif self.n == 3:
            sale = self.order_factory.market(instrument_id, OrderSide.SELL, Quantity.from_int(1000))
            if how == 0:
                self.submit_order(sale)
            elif how == 1:
                listed = self.order_factory.generate_order_list_id()
                self.submit_order_list(OrderList(listed, [sale]))
            elif how == 2:
                self.modify_order(self.rest, price=Price.from_str("5.00"))
            elif how == 3:
                (position,) = self.cache.positions_open(strategy_id=self.id)
                self.close_position(position)
            elif how == 5:
                self.market_exit()

    def hedges(self, ctx):
        if int(self.kanso_config.how) != 4:
            return []
        instrument_id = InstrumentId.from_str(ctx.instrument_id)
        return [self.order_factory.market(instrument_id, OrderSide.BUY, Quantity.from_int(5000))]
"""

BY_HAND_WAYS = {
    "submit_order": (0.0, "SELL", "submit_order"),
    "submit_order_list": (1.0, "SELL", "submit_order_list"),
    "modify_order": (2.0, "SELL", "modify_order"),
    "close_position": (3.0, "SELL", "submit_order"),
    "hedges": (4.0, "BUY", "hedges()"),
    "market_exit": (5.0, "SELL", "submit_order"),
}
"""Each way `BY_HAND` sends an order past the two sized calls: its `how`, the side of the
order, and the call the refusal names — `close_position` and `market_exit` send their order
by `submit_order`."""


@pytest.mark.parametrize("way", sorted(BY_HAND_WAYS))
def test_a_member_s_orders_go_out_only_through_submit_entry_and_submit_exit(way: str) -> None:
    """In a book an order a member sends past `submit_entry` and `submit_exit` is refused as
    `book_entry`, naming the member, whatever the order is, before it reaches the venue, and
    the same sleeve run alone sends it as before. Measured on the round before this test, the
    book let each of them out: a sale by hand of no more than the long, alone or as a list of
    one, and `close_position`'s, passed its check as an exit; a modify and a leg `hedges()`
    returned were not asked at all, the leg buying 5000 past the book."""
    how, side, call = BY_HAND_WAYS[way]
    run = request(hyp((DEMO,)), BY_HAND, how=how)

    with pytest.raises(SizingError) as raised:
        execute_book(block(), [member("a", run, 5.0)], [instrument()], [daily("DEMO")])
    alone = execute(run, [instrument()], daily("DEMO")).run

    refusal = raised.value.refusal
    assert (refusal.rule, refusal.asked) == (BOOK_ENTRY, side)
    assert refusal.why.startswith(f"book member a: {call} ")
    assert "submit_entry" in refusal.why and "submit_exit" in refusal.why
    sold = fsum(f.qty for f in alone.fills if f.side == "SELL")
    assert (placed(alone, DEMO), sold) == ((6000.0, 0.0) if way == "hedges" else (1000.0, 1000.0))


PAST_KANSO = b"""
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import MarketOrder, OrderList
from nautilus_trader.trading.strategy import Strategy as Engine

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    how: float = 0.0


class Strategy(KansoStrategy):
    \"\"\"Buys 1000 on its first bar and, on its third, reaches the engine's own methods past
    kanso's, by `how`: a buy of a million at market by `Strategy.submit_order` (0), the same
    through `super(KansoStrategy, self)` (1), the same as a list by
    `Strategy.submit_order_list` (2), the exit it rested far over the market on its second
    bar moved to 5000 at 5.00 by `Strategy.modify_order` (3), its long closed by
    `Strategy.close_all_positions` (4), a sale of 1000 by `Strategy.submit_order` from the
    handler the engine runs inside the submission of its exit at market (5), or a buy of a
    million in the name of a strategy no member runs (6).\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0
        self.inside = False

    def buy(self, instrument_id):
        return self.order_factory.market(instrument_id, OrderSide.BUY, Quantity.from_int(10**6))

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        how = int(self.kanso_config.how)
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", qty=1000.0)
        elif self.n == 2 and how == 3:
            self.rest = self.submit_exit(instrument_id, price=100.0)
        elif self.n == 3:
            if how == 0:
                Engine.submit_order(self, self.buy(instrument_id))
            elif how == 1:
                super(KansoStrategy, self).submit_order(self.buy(instrument_id))
            elif how == 2:
                listed = self.order_factory.generate_order_list_id()
                Engine.submit_order_list(self, OrderList(listed, [self.buy(instrument_id)]))
            elif how == 3:
                Engine.modify_order(
                    self, self.rest, quantity=Quantity.from_int(5000), price=Price.from_str("5.00")
                )
            elif how == 4:
                Engine.close_all_positions(self, instrument_id)
            elif how == 5:
                self.inside = True
                self.submit_exit(instrument_id)
            elif how == 6:
                stranger = MarketOrder(
                    trader_id=self.trader_id,
                    strategy_id=StrategyId("Nobody-001"),
                    instrument_id=instrument_id,
                    client_order_id=ClientOrderId("O-nobody-1"),
                    order_side=OrderSide.BUY,
                    quantity=Quantity.from_int(10**6),
                    init_id=UUID4(),
                    ts_init=bar.ts_init,
                )
                Engine.submit_order(self, stranger)

    def on_order_submitted(self, event):
        if self.inside:
            self.inside = False
            sale = Quantity.from_int(1000)
            Engine.submit_order(
                self, self.order_factory.market(event.instrument_id, OrderSide.SELL, sale)
            )
"""

ENGINE_SEND = "the engine's own submit_order or submit_order_list, reached past KansoStrategy's, "
PAST_KANSO_WAYS = {
    "Strategy.submit_order": (0.0, "BUY", ENGINE_SEND, (1_001_000.0, 0.0)),
    "super(KansoStrategy, self).submit_order": (1.0, "BUY", ENGINE_SEND, (1_001_000.0, 0.0)),
    "Strategy.submit_order_list": (2.0, "BUY", ENGINE_SEND, (1_001_000.0, 0.0)),
    "Strategy.modify_order": (
        3.0,
        "SELL",
        "the engine's own modify_order, reached past KansoStrategy's, ",
        (1000.0, 5000.0),
    ),
    "Strategy.close_all_positions": (4.0, "SELL", "submit_order ", (1000.0, 1000.0)),
    "Strategy.submit_order inside an exit's submission": (
        5.0,
        "SELL",
        ENGINE_SEND,
        (1000.0, 2000.0),
    ),
}
"""Each way `PAST_KANSO` reaches the engine past kanso's own methods: its `how`, the side of
the order, how the refusal goes on after naming the member, and what the sleeve run alone
bought and sold by it. `close_all_positions` sends by `submit_order`, which is kanso's."""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize("way", sorted(PAST_KANSO_WAYS))
def test_a_member_reaching_the_engine_past_kanso_s_methods_is_refused(
    way: str, latency_ms: float
) -> None:
    """The engine's own `submit_order`, `submit_order_list` and `modify_order`, called on its
    class or through `super(KansoStrategy, self)`, never reach kanso's overrides; the engine
    still hands the order's `OrderInitialized`, and the modify's `OrderPendingUpdate`, to the
    bus before it routes either, and the book refuses there what kanso is not sending, at no
    latency and at 20 ms. The same sleeve alone sends each, as it did before. Measured on the
    round before this test, the book let all but `close_all_positions` out: a member bought
    1,001,000 on 100,000, sold 5000 of a long of 1000 by a modify, and sold 2000 of 1000 from
    `on_order_submitted`."""
    how, side, did, alone = PAST_KANSO_WAYS[way]
    document = hyp((DEMO,)).model_dump(mode="json")
    document["costs"]["latency_ms"] = latency_ms
    run = request(Hypothesis.model_validate(document), PAST_KANSO, how=how)

    with pytest.raises(SizingError) as raised:
        execute_book(block(), [member("a", run, 5.0)], [instrument()], [daily("DEMO")])

    refusal = raised.value.refusal
    assert (refusal.rule, refusal.asked) == (BOOK_ENTRY, side)
    assert refusal.why.startswith(f"book member a: {did}")
    assert "submit_entry(instrument_id, side) or submit_exit(instrument_id)" in refusal.why
    if latency_ms == 0.0:
        solo = execute(run, [instrument()], daily("DEMO")).run
        sold = fsum(f.qty for f in solo.fills if f.side == "SELL")
        assert (placed(solo, DEMO), sold) == alone


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_order_in_the_name_of_no_member_is_refused(latency_ms: float) -> None:
    """An order a member builds in the name of a strategy no member runs goes to no member's
    check and into no member's extraction, so the book refuses it as the engine hands it to
    the bus. Measured on the round before this test, the book sent it and the venue filled it:
    a buy of a million shares no number of the book's held."""
    document = hyp((DEMO,)).model_dump(mode="json")
    document["costs"]["latency_ms"] = latency_ms
    run = request(Hypothesis.model_validate(document), PAST_KANSO, how=6.0)

    with pytest.raises(SizingError) as raised:
        execute_book(block(), [member("a", run, 5.0)], [instrument()], [daily("DEMO")])

    refusal = raised.value.refusal
    assert (refusal.rule, refusal.asked, refusal.instrument_id) == (BOOK_ENTRY, "BUY", DEMO)
    assert refusal.why.startswith(
        "book member a: an order in the name of Nobody-001, which is no member of this book, "
        "was handed to the engine while this member ran"
    )


IN_ANOTHER_NAME = b"""
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.trading.strategy import Strategy as Engine

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"On its third bar, buys a million at market in the name of the book's other member, by
    the engine's own `submit_order` past kanso's.\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        if self.n == 3:
            (other,) = [held for held in self.cache.strategy_ids() if held != self.id]
            order = MarketOrder(
                trader_id=self.trader_id,
                strategy_id=other,
                instrument_id=bar.bar_type.instrument_id,
                client_order_id=ClientOrderId("O-other-1"),
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(10**6),
                init_id=UUID4(),
                ts_init=bar.ts_init,
            )
            Engine.submit_order(self, order)
"""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_order_one_member_sends_in_another_s_name_is_refused_naming_the_sender(
    latency_ms: float,
) -> None:
    """The event the book hears names the strategy the order is in the name of, not the one
    that sent it; the book names the member whose handler was running. On the round before
    this test the refusal named member b, which sent nothing."""
    document = hyp((DEMO,)).model_dump(mode="json")
    document["costs"]["latency_ms"] = latency_ms
    subject = Hypothesis.model_validate(document)
    members = [
        member("a", request(subject, IN_ANOTHER_NAME), 5.0),
        member("b", request(subject, TAKER, qty=10.0), 1.0),
    ]

    with pytest.raises(SizingError) as raised:
        execute_book(block(), members, [instrument()], [daily("DEMO"), daily("DEMO")])

    refusal = raised.value.refusal
    assert (refusal.rule, refusal.asked, refusal.instrument_id) == (BOOK_ENTRY, "BUY", DEMO)
    assert refusal.why.startswith(
        f"book member a: {ENGINE_SEND}sends an order in the name of book member b while this "
        "member runs, past the book"
    )


OWN = b"""
from nautilus_trader.model.enums import OrderSide

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Trades only through kanso's own calls, and cancels. Buys 1000 at market (bar 1); rests
    a buy of 10 far under the market and an exit far over it (2); cancels the buy (3); sells
    400 at market, which cancels the resting exit first (4); rests an exit of 300 far over the
    market (6) and cancels every order in the name (7); sells 300 at a marketable limit (8);
    and sells what is left at market (9).\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", qty=1000.0)
        elif self.n == 2:
            self.submit_entry(instrument_id, "BUY", qty=10.0, price=1.0)
            self.submit_exit(instrument_id, price=100.0)
        elif self.n == 3:
            open_orders = self.cache.orders_open(strategy_id=self.id)
            (resting,) = [order for order in open_orders if order.side == OrderSide.BUY]
            self.cancel_order(resting)
        elif self.n == 4:
            self.submit_exit(instrument_id, qty=400.0)
        elif self.n == 6:
            self.submit_exit(instrument_id, qty=300.0, price=100.0)
        elif self.n == 7:
            self.cancel_all_orders(instrument_id)
        elif self.n == 8:
            self.submit_exit(instrument_id, qty=300.0, price=5.0)
        elif self.n == 9:
            self.submit_exit(instrument_id)
"""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_member_trades_and_cancels_through_kanso_s_own_calls_in_a_book(latency_ms: float) -> None:
    """Everything kanso sends for a member goes out in a book: its entries at market and
    resting, placed by the book; its exits at market and at a price, sized never past flat;
    the cancel an exit at market sends for the exit resting on its side; and, under a latency,
    with that cancel still in flight, the 400 the exit at market is owed, asked for again by
    kanso on the next point. A member's own cancels go out as well, and it ends flat, never
    having sold what it did not hold."""
    document = hyp((DEMO,)).model_dump(mode="json")
    document["costs"]["latency_ms"] = latency_ms
    run = request(Hypothesis.model_validate(document), OWN)

    made = execute_book(block(), [member("a", run, 5.0)], [instrument()], [daily("DEMO")])

    sides = [(f.side, f.qty) for f in made.runs[0].fills]
    assert sides == [("BUY", 1000.0), ("SELL", 400.0), ("SELL", 300.0), ("SELL", 300.0)]
    (asked,) = [ts for ts, _, _, qty, _, _ in made.intents[0] if qty == 400.0]
    sessions = [int(bar.ts_event) for bar in bars(RESEARCH)]
    assert asked == sessions[3 if latency_ms == 0.0 else 4], "the fourth bar's, or the fifth's"
    assert made.counts["a"][UNFILLED] == 1, "the resting buy it cancelled"
    assert sum(made.counts["a"].values()) == 1


def test_an_exit_at_market_takes_the_exit_resting_on_its_side_out_of_the_book_at_once() -> None:
    """`a` rests an exit of its long far over the market and, on the session `b` buys the
    name, sends its exit at market, which cancels the resting one. With no latency stated the
    cancel lands before anything further is matched, so the rest can no longer fill and holds
    nothing: `b`'s buy is placed. Measured on the round before this test: the ledger counted
    the rest until the venue closed it, so beside the sale in flight it read as a short of
    1000, and `b`'s buy was refused as opposite to it."""
    made = execute_book(
        block(),
        [
            member("a", request(hyp((DEMO,)), qty=1000.0, rest_exit=100.0, exit_on=3.0), 5.0),
            member("b", request(hyp((DEMO,)), qty=500.0, skip=2.0), 1.0),
        ],
        [instrument()],
        [daily("DEMO"), daily("DEMO")],
    )

    a, b = made.runs[made.members.index("a")], made.runs[made.members.index("b")]
    assert [(f.side, f.qty) for f in a.fills] == [("BUY", 1000.0), ("SELL", 1000.0)]
    assert made.counts["b"][OPPOSITE_REFUSED] == 0
    assert placed(b, DEMO) == 500.0


AGAIN = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    priced: float = 0.0


class Strategy(KansoStrategy):
    \"\"\"Enters on its first bar; on its third asks for its exit, at market or, with
    `priced`, at a marketable limit, and asks for it again, the same, as the engine reports
    the first submitted.\"\"\"

    config_cls = Config

    def on_start(self):
        self.n = 0
        self.again = False

    def on_bar(self, bar):
        self.n += 1
        instrument_id = bar.bar_type.instrument_id
        if self.n == 1:
            self.submit_entry(instrument_id, "BUY", ENTRY)
        elif self.n == 3:
            self.again = True
            self.leave(instrument_id)

    def on_order_submitted(self, event):
        if self.again:
            self.again = False
            self.leave(event.instrument_id)

    def leave(self, instrument_id):
        if self.kanso_config.priced:
            self.submit_exit(instrument_id, price=5.0)
        else:
            self.submit_exit(instrument_id)
"""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize("how", ["at-market", "at-a-price", "sizing-rule"])
def test_an_exit_asked_again_as_the_first_is_submitted_counts_the_first(
    how: str, latency_ms: float
) -> None:
    """The engine runs a member's `on_order_submitted` inside the exit's own submission, and
    the exit asked again there finds the first working and sends nothing, so the member sells
    what it holds and no more, at no latency and at 20 ms; the second `submit_exit` is
    kanso's own, and the book lets it be asked. Measured two rounds before this test: the
    first was not yet counted, the second was sized to the whole long and let through, and
    the member sold twice what it held — 2000 of 1000 at market and at a price, 1994 of 997
    under a sizing rule."""
    if how == "sizing-rule":
        run = replace(sized((DEMO,), 10_000.0), strategy_source=AGAIN.replace(b", ENTRY", b""))
    else:
        source = AGAIN.replace(b"ENTRY", b"qty=1000.0")
        run = request(hyp((DEMO,)), source, priced=float(how == "at-a-price"))
    document = run.hyp.model_dump(mode="json")
    document["costs"]["latency_ms"] = latency_ms
    subject = Hypothesis.model_validate(document)
    run = replace(run, hyp=subject, venue_model=venue_model(subject))

    made = execute_book(block(), [member("a", run, 5.0)], [instrument()], [daily("DEMO")])

    fills = made.runs[0].fills
    bought = placed(made.runs[0], DEMO)
    assert bought > 0 and fsum(f.qty for f in fills if f.side == "SELL") == bought
    assert len(made.intents[0]) == 2


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
def test_an_order_sent_while_the_book_s_placement_is_submitted_is_refused(sizing: str) -> None:
    """The engine runs the member's `on_order_submitted` inside the submission of the order
    the book placed for it, and only that order is let through: a buy of a million shares
    sent by hand from the handler is refused, naming the member once. Measured two rounds
    before this test: it was let through with the book's order, and on a capital of 100,000
    the member bought 1,001,000 shares under its own rule and 1,000,997 under a sizing rule."""
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
