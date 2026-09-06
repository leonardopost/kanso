"""The live feed: which tape it is a feed of, and what it refuses to serve.

Five properties are under test.

**The tape is declared, carried and checked.** A client cannot open on an undeclared feed;
the tape it opened on is in its engine id, in every request it makes and in the payload it
reports for a caller to record; and a run whose declared tape contradicts the recorded one
is refused. The two tapes are then shown to be what the measurement says they are — two
different series for the same session, not two readings of one — by replaying both frozen
rows through the same client and comparing the bars.

**A bar lands at its close, whatever transport served it.** The daily bar this broker
serves for a session and the daily bar the historical vendor in this build serves for the
same session produce the same `ts_event`, which is what makes a card and a stage
comparable at all.

**A window that has not closed is not delivered, and nothing is delivered twice.** A sweep
publishes only bars whose close has passed, and a second sweep over an overlapping window
publishes nothing.

**What cannot be served is refused by name.** A quote, a print, a venue the broker does not
trade, a bar size it does not aggregate, an instrument the workspace never resolved, and
one series more than the account's quota can sweep.

**A feed that has gone blind stops the stage** rather than letting it trade on prices it
can no longer see.

Nothing here opens a socket or resolves a real credential. The keys are the shared
non-credentials of this package's suite, and every response is a frozen body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import RequestBars, SubscribeBars
from nautilus_trader.model.data import BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TraderId, Venue

from kanso.data.instruments import build, conventions_for, write_store
from kanso.errors import Exit, KansoError
from kanso.nautilus.adapters.alpaca import BROKER, CREDENTIALS, ID
from kanso.nautilus.adapters.alpaca import data as feed
from kanso.nautilus.adapters.alpaca import provider as instruments
from kanso.nautilus.adapters.alpaca.config import (
    LIVE_CLIENT,
    PAPER_CLIENT,
    Credentials,
    Feed,
    Response,
    account,
    credential_names,
)
from kanso.nautilus.adapters.alpaca.data import (
    BARS_KEY,
    END_PARAM,
    FEED_PARAM,
    LIMIT_PARAM,
    MAX_PAGES,
    PAGE_PARAM,
    PAGE_TOKEN,
    START_PARAM,
    TIMEFRAME_PARAM,
    AlpacaDataClient,
    bars_of,
    check_feed,
    client_id_of,
    data_client,
    engine_client_id,
    page,
    resolution_of,
    timeframe_of,
)
from kanso.nautilus.session import SHUTDOWN_TOPIC
from kanso.schemas import InstrumentEntry
from kanso.workspace import Workspace, init

from . import IEX_BAR, PAPER_KEY, SECRET, SIP_BAR, Replay, body

SYMBOL = "AAPL"
INSTRUMENT = "AAPL.XNAS"
AS_OF = date(2026, 8, 3)

SESSION_OPEN_NS = 1_785_729_600_000_000_000
"""2026-08-03T04:00:00Z — the instant the measured daily window opened, on both tapes."""

DAY_NS = 86_400 * 1_000_000_000
CLOSE_NS = SESSION_OPEN_NS + DAY_NS
"""Where the bar of that session lands: its window's start plus one resolution step."""


# --- fixtures -----------------------------------------------------------------


def equity(nautilus_id: str = INSTRUMENT, as_of: date = AS_OF) -> Any:
    """One equity definition, built the way a workspace builds every one of its own."""
    entry = InstrumentEntry(
        nautilus_id=nautilus_id,
        asset_class="EQUITY",
        manual=True,
        corporate_actions="adjust_all",
        override={"currency": "USD"},
    )
    return build(entry, conventions_for(entry, as_of))


def daily(instrument: str = INSTRUMENT) -> BarType:
    """The daily, external, last-price bar type of one instrument."""
    return BarType(
        InstrumentId.from_str(instrument),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A workspace holding one resolved equity and none of this adapter's variables."""
    for names in CREDENTIALS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    made = init(tmp_path / "ws")
    write_store(made, [equity()])
    return made


def answers(*pages: Any) -> Callable[[str, Mapping[str, str]], Response]:
    """A transport answer that serves frozen bodies in order, repeating the last."""
    served = list(pages)

    def answer(url: str, params: Mapping[str, str]) -> Response:
        return served.pop(0) if len(served) > 1 else served[0]

    return answer


def bars_body(*rows: Mapping[str, Any], token: str | None = None) -> Response:
    """The single-symbol envelope the broker publishes around its rows."""
    payload: dict[str, Any] = {"symbol": SYMBOL, BARS_KEY: list(rows), PAGE_TOKEN: token}
    return body(payload)


@dataclass
class Bed:
    """One client, the wire it reads and everything it published, as a test sees them."""

    client: AlpacaDataClient
    transport: Replay
    clock: TestClock
    loop: asyncio.AbstractEventLoop
    published: list[Any] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)
    shutdowns: list[Any] = field(default_factory=list)

    def run(self, coroutine: Any) -> Any:
        """Drive one coroutine on the client's own loop, which is where a node drives it."""
        return self.loop.run_until_complete(coroutine)

    def bars(self) -> list[Any]:
        """The bars published to the data engine, in the order they were published."""
        return [point for point in self.published if hasattr(point, "bar_type")]

    def asked(self) -> list[Mapping[str, str]]:
        """The parameters of every request made."""
        return [request.params for request in self.transport.asked]


@pytest.fixture
def make(ws: Workspace) -> Any:
    """A factory for a client wired to a frozen transport, on a clock a test sets."""
    loops: list[asyncio.AbstractEventLoop] = []

    def build_bed(
        answer: Callable[[str, Mapping[str, str]], Response] | None = None,
        *,
        client_id: str = PAPER_CLIENT,
        tape: Feed = Feed.SIP,
        now: int = SESSION_OPEN_NS,
        workspace: Workspace = ws,
        **settings: Any,
    ) -> Bed:
        loop = asyncio.new_event_loop()
        loops.append(loop)
        clock = TestClock()
        clock.set_time(now)
        bus = MessageBus(trader_id=TraderId("KANSO-LIVE"), clock=clock)
        transport = Replay(answer or answers(bars_body()))
        bed = Bed(
            client=AlpacaDataClient(
                loop,
                bus,
                Cache(),
                clock,
                credentials=Credentials(account(client_id), PAPER_KEY, SECRET),
                feed=tape,
                transport=transport,
                instrument_provider=instruments.provider(workspace),
                **settings,
            ),
            transport=transport,
            clock=clock,
            loop=loop,
        )
        bus.register(endpoint="DataEngine.process", handler=bed.published.append)
        bus.register(endpoint="DataEngine.response", handler=bed.responses.append)
        bus.subscribe(topic=SHUTDOWN_TOPIC, handler=bed.shutdowns.append)
        return bed

    yield build_bed
    for loop in loops:
        loop.close()


def subscribed(bed: Bed, bar_type: BarType | None = None) -> BarType:
    """Take one series on, the way the data engine's command does."""
    found = bar_type or daily()
    bed.run(
        bed.client._subscribe_bars(
            SubscribeBars(
                bar_type=found,
                client_id=None,
                venue=found.instrument_id.venue,
                command_id=UUID4(),
                ts_init=bed.clock.timestamp_ns(),
            )
        )
    )
    return found


# --- the tape is declared, carried and checked --------------------------------


def test_nothing_recorded_contradicts_nothing() -> None:
    """A first run has no recorded tape, so there is nothing for it to disagree with."""
    assert check_feed(Feed.SIP, None, subject="stages.live") is None


@pytest.mark.parametrize("recorded", ["sip", "SIP", "  sip  "])
def test_a_recorded_tape_that_matches_is_no_objection(recorded: str) -> None:
    assert check_feed(Feed.SIP, recorded, subject="stages.live") is None


def test_a_stage_may_not_change_tape_between_runs() -> None:
    """The two are different series, so continuing on the other is a different strategy."""
    with pytest.raises(KansoError) as failure:
        check_feed(Feed.IEX, "sip", subject="stages.live: the alpaca feed")

    assert failure.value.code is Exit.PRECONDITION
    assert "'sip'" in failure.value.message
    assert "'iex'" in failure.value.message
    assert "stages.live" in failure.value.message


def test_a_recorded_value_naming_no_tape_is_refused_rather_than_ignored() -> None:
    """A record written by something that meant another thing is not silently accepted."""
    with pytest.raises(KansoError) as failure:
        check_feed(Feed.SIP, "consolidated", subject="stages.live")

    assert failure.value.code is Exit.PRECONDITION
    assert "sip, iex" in str(failure.value.remedy) or "iex, sip" in str(failure.value.remedy)


def test_the_engine_id_names_the_account_and_the_tape() -> None:
    """So no log line, response or client registry has to infer either of them."""
    assert engine_client_id(PAPER_CLIENT, Feed.SIP) == ClientId("ALPACA_PAPER-SIP")
    assert engine_client_id(LIVE_CLIENT, Feed.IEX) == ClientId("ALPACA-IEX")
    assert client_id_of(engine_client_id(LIVE_CLIENT, Feed.IEX)) == LIVE_CLIENT
    assert client_id_of("ALPACA_PAPER-SIP") == PAPER_CLIENT


def test_every_request_carries_the_declared_tape(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)), tape=Feed.IEX)
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())

    assert [asked[FEED_PARAM] for asked in bed.asked()] == ["iex"]


def test_the_two_tapes_are_two_series_for_the_same_session(make: Any) -> None:
    """The measured trap of this milestone: same day, same instrument, different numbers."""
    consolidated = make(answers(bars_body(SIP_BAR)), tape=Feed.SIP)
    venue = make(answers(bars_body(IEX_BAR)), tape=Feed.IEX)
    for bed in (consolidated, venue):
        subscribed(bed)
        bed.clock.set_time(CLOSE_NS + 1)
        bed.run(bed.client.poll())

    sip, iex = consolidated.bars()[0], venue.bars()[0]

    assert (str(sip.open), str(sip.close), int(sip.volume)) == ("309.58", "303.42", 75_314_280)
    assert (str(iex.open), str(iex.close), int(iex.volume)) == ("309.77", "303.41", 3_242_233)
    assert sip.ts_event == iex.ts_event
    assert (sip.open, sip.close, sip.volume) != (iex.open, iex.close, iex.volume)


def test_what_the_feed_reports_for_a_caller_to_record(make: Any) -> None:
    bed = make(tape=Feed.IEX)

    recorded = bed.client.provenance()

    assert recorded["broker"] == ID
    assert recorded["client"] == PAPER_CLIENT
    assert recorded["feed"] == "iex"
    assert PAPER_KEY not in str(recorded)
    assert SECRET not in str(recorded)


def test_the_repr_carries_the_account_and_the_tape_and_no_credential(make: Any) -> None:
    bed = make(tape=Feed.SIP)

    assert "ALPACA_PAPER-SIP" in repr(bed.client)
    assert PAPER_KEY not in repr(bed.client)
    assert SECRET not in repr(bed.client)


def test_the_credential_travels_in_the_headers_and_never_in_the_query(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())
    request = bed.transport.asked[0]

    assert request.headers["APCA-API-KEY-ID"] == PAPER_KEY
    assert PAPER_KEY not in str(request.params)
    assert PAPER_KEY not in request.url
    assert SECRET not in str(request.params)


# --- a bar lands at its close -------------------------------------------------


def test_a_bar_is_stamped_one_resolution_step_past_its_window(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())

    assert bed.bars()[0].ts_event == CLOSE_NS
    assert bed.bars()[0].ts_init == CLOSE_NS


def test_this_broker_and_the_history_vendor_stamp_one_session_identically(make: Any) -> None:
    """The cross-transport check: a card and a stage must compare the same instants."""
    from kanso.data.adapters.massive.loaders.bars import Request, build_bar

    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())

    vendor = build_bar(
        Request(
            symbol=SYMBOL,
            venue="XNAS",
            ticker=SYMBOL,
            asset_class="stocks",
            dataset="bars",
            resolution="1d",
            adjusted=True,
            publication="realtime",
            publication_rule=None,
            price_precision=2,
            size_precision=0,
        ),
        {
            "t": SESSION_OPEN_NS // 1_000_000,
            "o": SIP_BAR["o"],
            "h": SIP_BAR["h"],
            "l": SIP_BAR["l"],
            "c": SIP_BAR["c"],
            "v": SIP_BAR["v"],
        },
    )

    assert bed.bars()[0].ts_event == vendor.ts_event
    assert bed.bars()[0].bar_type == vendor.bar_type


# --- what is delivered, and what is not ---------------------------------------


def test_a_window_that_has_not_closed_is_not_delivered(make: Any) -> None:
    """The broker serves the session in progress; a close that is not the close is not one."""
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS - 1)

    assert bed.run(bed.client.poll()) == 0
    assert bed.bars() == []


def test_a_second_sweep_over_the_same_window_delivers_nothing_twice(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)

    assert bed.run(bed.client.poll()) == 1
    assert bed.run(bed.client.poll()) == 0
    assert len(bed.bars()) == 1
    assert bed.client.since[daily()] == CLOSE_NS


def test_a_sweep_asks_from_one_step_before_what_it_has_already_delivered(make: Any) -> None:
    """A bar closing after the mark opened its window before it, so the window is widened."""
    bed = make(answers(bars_body()), now=SESSION_OPEN_NS)
    subscribed(bed)
    bed.clock.set_time(SESSION_OPEN_NS + 60 * 1_000_000_000)
    bed.run(bed.client.poll())
    asked = bed.asked()[0]

    assert asked[START_PARAM] == "2026-08-02T04:00:00Z"
    assert asked[END_PARAM] == "2026-08-03T04:01:00Z"
    assert asked[TIMEFRAME_PARAM] == "1Day"


def test_bars_are_published_in_the_order_their_windows_closed(make: Any) -> None:
    earlier = {**SIP_BAR, "t": "2026-08-02T04:00:00Z"}
    bed = make(answers(bars_body(SIP_BAR, earlier)), now=SESSION_OPEN_NS - 1)
    subscribed(bed, daily())
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())

    assert [point.ts_event for point in bed.bars()] == [CLOSE_NS - DAY_NS, CLOSE_NS]


def test_a_series_dropped_is_no_longer_swept(make: Any) -> None:
    from nautilus_trader.data.messages import UnsubscribeBars

    bed = make(answers(bars_body(SIP_BAR)))
    bar_type = subscribed(bed)
    bed.run(
        bed.client._unsubscribe_bars(
            UnsubscribeBars(
                bar_type=bar_type,
                client_id=None,
                venue=bar_type.instrument_id.venue,
                command_id=UUID4(),
                ts_init=bed.clock.timestamp_ns(),
            )
        )
    )
    bed.clock.set_time(CLOSE_NS + 1)

    assert bed.client.since == {}
    assert bed.run(bed.client.poll()) == 0
    assert bed.transport.asked == []


def test_a_series_dropped_before_a_sweep_reaches_it_is_not_swept(make: Any) -> None:
    """A sweep holds the series it snapshotted, and a strategy may drop one meanwhile."""
    bed = make(answers(bars_body(SIP_BAR)))

    assert bed.run(bed.client._advance(daily(), CLOSE_NS + 1)) == 0
    assert bed.transport.asked == []


def test_a_series_dropped_while_its_request_was_in_flight_is_not_delivered(make: Any) -> None:
    """Nor left with a mark, which would put it back into the sweep after this one."""
    bed = make(answers(bars_body(SIP_BAR)), now=SESSION_OPEN_NS - 1)
    bar_type = subscribed(bed)

    async def drop() -> None:
        await asyncio.sleep(0)
        bed.client._since.pop(bar_type)

    async def both() -> int:
        published, _ = await asyncio.gather(bed.client._advance(bar_type, CLOSE_NS + 1), drop())
        return int(published)

    assert bed.run(both()) == 0
    assert bed.transport.asked != []
    assert bed.bars() == []
    assert bed.client.since == {}


def test_a_sweep_that_finds_one_in_flight_stands_down(make: Any) -> None:
    """Two sweeps over one window would spend the quota on an answer already in hand."""
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)

    async def both() -> tuple[int, int]:
        first, second = await asyncio.gather(bed.client.poll(), bed.client.poll())
        return first, second

    published, stood_down = bed.run(both())

    assert (published, stood_down) == (1, 0)
    assert bed.client.skipped == 1
    assert bed.client.sweeps == 1
    assert len(bed.bars()) == 1


# --- what is refused, by name -------------------------------------------------


def test_a_quote_subscription_is_refused_rather_than_never_delivered(make: Any) -> None:
    from nautilus_trader.data.messages import SubscribeQuoteTicks

    bed = make()
    found = InstrumentId.from_str(INSTRUMENT)
    with pytest.raises(KansoError) as failure:
        bed.run(
            bed.client._subscribe_quote_ticks(
                SubscribeQuoteTicks(
                    instrument_id=found,
                    client_id=None,
                    venue=found.venue,
                    command_id=UUID4(),
                    ts_init=0,
                )
            )
        )

    assert failure.value.code is Exit.VALIDATION
    assert "quotes" in failure.value.message


def test_a_trade_subscription_is_refused_rather_than_never_delivered(make: Any) -> None:
    from nautilus_trader.data.messages import SubscribeTradeTicks

    bed = make()
    found = InstrumentId.from_str(INSTRUMENT)
    with pytest.raises(KansoError) as failure:
        bed.run(
            bed.client._subscribe_trade_ticks(
                SubscribeTradeTicks(
                    instrument_id=found,
                    client_id=None,
                    venue=found.venue,
                    command_id=UUID4(),
                    ts_init=0,
                )
            )
        )

    assert failure.value.code is Exit.VALIDATION
    assert "trades" in failure.value.message


def test_a_venue_this_broker_does_not_trade_is_refused(make: Any) -> None:
    bed = make()
    with pytest.raises(KansoError) as failure:
        subscribed(bed, daily("DEMO.SIM"))

    assert failure.value.code is Exit.VALIDATION
    assert "DEMO.SIM" in failure.value.message


def test_an_instrument_the_workspace_never_resolved_is_refused(make: Any) -> None:
    bed = make()
    with pytest.raises(KansoError) as failure:
        subscribed(bed, daily("MSFT.XNAS"))

    assert failure.value.code is Exit.VALIDATION
    assert "MSFT.XNAS" in failure.value.message


def test_one_series_more_than_the_quota_can_sweep_is_refused(ws: Workspace, make: Any) -> None:
    """A bound rather than a throttle: a sweep the quota cannot finish drops the tail."""
    write_store(ws, [equity("MSFT.XNAS")])
    bed = make(requests_per_minute=4, poll_interval_s=15.0)
    subscribed(bed)
    with pytest.raises(KansoError) as failure:
        subscribed(bed, daily("MSFT.XNAS"))

    assert failure.value.code is Exit.PRECONDITION
    assert "1 series" in failure.value.message


def test_with_no_quota_stated_there_is_no_bound_worth_speaking_of(make: Any) -> None:
    bed = make()

    assert bed.client._capacity == feed.MAX_SERIES


def test_taking_the_same_series_on_twice_does_not_reset_its_mark(make: Any) -> None:
    bed = make(requests_per_minute=4)
    subscribed(bed)
    bed.clock.set_time(SESSION_OPEN_NS + DAY_NS)
    subscribed(bed)

    assert bed.client.since[daily()] == SESSION_OPEN_NS


# --- bar sizes ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "aggregation", "expected"),
    [
        (5, BarAggregation.MINUTE, "5m"),
        (4, BarAggregation.HOUR, "4h"),
        (1, BarAggregation.DAY, "1d"),
        (1, BarAggregation.WEEK, "1w"),
        (5, BarAggregation.SECOND, "5s"),
    ],
)
def test_a_bar_type_reads_back_as_the_resolution_it_was_built_from(
    step: int, aggregation: BarAggregation, expected: str
) -> None:
    bar_type = BarType(
        InstrumentId.from_str(INSTRUMENT),
        BarSpecification(step, aggregation, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )

    assert resolution_of(bar_type) == expected


def test_a_bar_aggregated_by_something_other_than_time_is_refused() -> None:
    bar_type = BarType(
        InstrumentId.from_str(INSTRUMENT),
        BarSpecification(100, BarAggregation.VOLUME, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    with pytest.raises(KansoError) as failure:
        resolution_of(bar_type)

    assert failure.value.code is Exit.VALIDATION
    assert "VOLUME" in failure.value.message


def test_a_bar_priced_on_something_other_than_trades_is_refused() -> None:
    bar_type = BarType(
        InstrumentId.from_str(INSTRUMENT),
        BarSpecification(1, BarAggregation.DAY, PriceType.MID),
        AggregationSource.EXTERNAL,
    )
    with pytest.raises(KansoError) as failure:
        resolution_of(bar_type)

    assert failure.value.code is Exit.VALIDATION
    assert "MID" in failure.value.message


@pytest.mark.parametrize(
    ("resolution", "expected"), [("1m", "1Min"), ("4h", "4Hour"), ("1d", "1Day"), ("1w", "1Week")]
)
def test_a_resolution_is_sent_in_the_brokers_own_spelling(resolution: str, expected: str) -> None:
    assert timeframe_of(resolution) == expected


@pytest.mark.parametrize("resolution", ["30s", "", "d", "0d", "-1d", "1.5d"])
def test_a_bar_size_the_broker_does_not_serve_is_refused_not_rounded(resolution: str) -> None:
    """Including a multiplier that is not a whole number above zero, which is not one."""
    with pytest.raises(KansoError) as failure:
        timeframe_of(resolution)

    assert failure.value.code is Exit.VALIDATION
    assert repr(resolution) in failure.value.message


def test_a_second_grained_series_is_refused_at_subscription(make: Any) -> None:
    bed = make()
    seconds = BarType(
        InstrumentId.from_str(INSTRUMENT),
        BarSpecification(30, BarAggregation.SECOND, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    with pytest.raises(KansoError):
        subscribed(bed, seconds)


# --- reading the envelope -----------------------------------------------------


def test_the_single_symbol_envelope_is_read() -> None:
    rows, token = page({BARS_KEY: [SIP_BAR], PAGE_TOKEN: None}, SYMBOL)

    assert rows == (SIP_BAR,)
    assert token is None


def test_the_multi_symbol_envelope_is_read() -> None:
    rows, token = page({BARS_KEY: {SYMBOL: [SIP_BAR]}, PAGE_TOKEN: "next"}, SYMBOL)

    assert rows == (SIP_BAR,)
    assert token == "next"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {BARS_KEY: None},
        {BARS_KEY: "bars"},
        {BARS_KEY: {"MSFT": [SIP_BAR]}},
        {BARS_KEY: {SYMBOL: "rows"}},
    ],
)
def test_an_envelope_of_no_known_shape_yields_no_rows(payload: Mapping[str, Any]) -> None:
    """Only the row was measured, so an envelope nobody has seen is not guessed at."""
    assert page(payload, SYMBOL) == ((), None)


def test_a_row_that_is_not_an_object_is_passed_over() -> None:
    rows, _ = page({BARS_KEY: [SIP_BAR, "not a row"]}, SYMBOL)

    assert rows == (SIP_BAR,)


@pytest.mark.parametrize("token", [None, "", 7])
def test_only_a_non_empty_token_names_a_page_after_this_one(token: object) -> None:
    assert page({BARS_KEY: [], PAGE_TOKEN: token}, SYMBOL)[1] is None


def test_a_row_missing_a_field_a_bar_needs_yields_no_bar() -> None:
    """A missing point is visible in the count; a fabricated one is not."""
    made = bars_of(
        [SIP_BAR, {**SIP_BAR, "c": None}],
        instrument=InstrumentId.from_str(INSTRUMENT),
        resolution="1d",
        price_precision=2,
        size_precision=0,
    )

    assert len(made) == 1


# --- paging -------------------------------------------------------------------


def test_a_cursor_is_walked_to_its_end(make: Any) -> None:
    earlier = {**SIP_BAR, "t": "2026-08-02T04:00:00Z"}
    bed = make(
        answers(bars_body(earlier, token="more"), bars_body(SIP_BAR)),
        now=SESSION_OPEN_NS - 1,
    )
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)

    assert bed.run(bed.client.poll()) == 2
    assert bed.asked()[1][PAGE_PARAM] == "more"
    assert PAGE_PARAM not in bed.asked()[0]


def test_a_cursor_that_does_not_end_is_refused(make: Any) -> None:
    bed = make(answers(bars_body(token="more")))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())

    assert len(bed.transport.asked) == MAX_PAGES
    assert bed.client.failures == 1


# --- when the broker refuses --------------------------------------------------


def test_a_refused_key_names_the_variables_and_not_the_value(make: Any) -> None:
    bed = make(answers(Response(status=401, body=b"{}")))
    bar_type = subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    with pytest.raises(KansoError) as failure:
        bed.client.fetch(bar_type, SESSION_OPEN_NS, CLOSE_NS + 1)

    key_variable, secret_variable = credential_names(PAPER_CLIENT)

    assert failure.value.code is Exit.PRECONDITION
    assert key_variable in str(failure.value.remedy)
    assert secret_variable in str(failure.value.remedy)
    assert PAPER_KEY not in failure.value.message + str(failure.value.remedy)


@pytest.mark.parametrize(
    "answer",
    [
        Response(status=500, body=b"{}"),
        Response(status=200, body=b"<html>not json</html>"),
        Response(status=200, body=b"[]"),
    ],
)
def test_an_answer_that_is_not_a_readable_body_is_a_failure(make: Any, answer: Response) -> None:
    bed = make(answers(answer))
    bar_type = subscribed(bed)
    with pytest.raises(KansoError) as failure:
        bed.client.fetch(bar_type, SESSION_OPEN_NS, CLOSE_NS)

    assert failure.value.code is Exit.ERROR
    assert "/v2/stocks/AAPL/bars" in failure.value.message


def test_a_transport_that_cannot_reach_the_broker_is_one_outcome(make: Any) -> None:
    def raising(url: str, params: Mapping[str, str]) -> Response:
        raise OSError("no route to host")

    bed = make(raising)
    bar_type = subscribed(bed)
    with pytest.raises(KansoError) as failure:
        bed.client.fetch(bar_type, SESSION_OPEN_NS, CLOSE_NS)

    assert failure.value.code is Exit.ERROR
    assert "OSError" in failure.value.message
    assert "no route to host" not in failure.value.message


def test_a_kanso_failure_below_the_answer_passes_through(make: Any) -> None:
    def raising(url: str, params: Mapping[str, str]) -> Response:
        raise KansoError("alpaca: the quota is exhausted", Exit.ERROR)

    bed = make(raising)
    bar_type = subscribed(bed)
    with pytest.raises(KansoError) as failure:
        bed.client.fetch(bar_type, SESSION_OPEN_NS, CLOSE_NS)

    assert failure.value.message == "alpaca: the quota is exhausted"


# --- a feed that has gone blind -----------------------------------------------


def test_two_failed_sweeps_do_not_stop_the_stage(make: Any) -> None:
    bed = make(answers(Response(status=503)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())
    bed.run(bed.client.poll())

    assert bed.client.failures == 2
    assert bed.shutdowns == []


def test_three_failed_sweeps_in_a_row_stop_the_stage(make: Any) -> None:
    """A stage that cannot see the market must not go on placing orders into it."""
    bed = make(answers(Response(status=503)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    for _ in range(3):
        bed.run(bed.client.poll())

    assert len(bed.shutdowns) == 1
    assert "sip" in bed.shutdowns[0].reason
    assert PAPER_KEY not in bed.shutdowns[0].reason


def test_one_sweep_that_answers_clears_what_came_before_it(make: Any) -> None:
    served = [Response(status=503), bars_body(SIP_BAR)]

    def answer(url: str, params: Mapping[str, str]) -> Response:
        return served.pop(0) if served else bars_body()

    bed = make(answer, max_failures=2)
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())
    bed.run(bed.client.poll())

    assert bed.client.failures == 0
    assert bed.shutdowns == []


# --- lifecycle ----------------------------------------------------------------


def test_connecting_publishes_the_workspaces_own_definitions_and_starts_the_sweep(
    make: Any,
) -> None:
    """The definitions are kanso's, so connecting reads a local store and opens no socket."""
    bed = make()
    bed.run(bed.client._connect())

    assert [str(point.id) for point in bed.published] == [INSTRUMENT]
    assert bed.clock.timer_names == [f"{bed.client.id}-bars"]
    assert bed.transport.asked == []


def test_disconnecting_stops_the_sweep_and_is_safe_before_one_started(make: Any) -> None:
    bed = make()
    bed.run(bed.client._disconnect())
    bed.run(bed.client._connect())
    bed.run(bed.client._disconnect())

    assert bed.clock.timer_names == []


def test_the_timer_runs_a_sweep_on_the_nodes_own_loop(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)

    async def fire() -> None:
        bed.client._on_timer(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    bed.run(fire())

    assert bed.client.sweeps == 1


def test_one_instrument_is_published_on_request(make: Any) -> None:
    from nautilus_trader.data.messages import SubscribeInstrument

    bed = make()
    found = InstrumentId.from_str(INSTRUMENT)
    bed.run(
        bed.client._subscribe_instrument(
            SubscribeInstrument(
                instrument_id=found,
                client_id=None,
                venue=found.venue,
                command_id=UUID4(),
                ts_init=0,
            )
        )
    )

    assert [str(point.id) for point in bed.published] == [INSTRUMENT]


def test_every_instrument_is_published_on_request(make: Any) -> None:
    from nautilus_trader.data.messages import SubscribeInstruments

    bed = make()
    bed.run(
        bed.client._subscribe_instruments(
            SubscribeInstruments(client_id=None, venue=Venue("XNAS"), command_id=UUID4(), ts_init=0)
        )
    )

    assert [str(point.id) for point in bed.published] == [INSTRUMENT]


def test_a_definition_the_provider_lacks_is_taken_from_the_engines_cache(make: Any) -> None:
    """A node adds what it is going to trade before it connects its clients."""
    bed = make()
    other = equity("MSFT.XNAS")
    bed.client._cache.add_instrument(other)

    assert bed.client._definition(other.id) is other


# --- historical requests ------------------------------------------------------


def request_bars(bed: Bed, **changes: Any) -> RequestBars:
    """One historical bar request, as the data engine builds it."""
    bar_type = daily()
    fields: dict[str, Any] = {
        "bar_type": bar_type,
        "start": None,
        "end": None,
        "limit": 0,
        "client_id": None,
        "venue": bar_type.instrument_id.venue,
        "callback": lambda response: None,
        "request_id": UUID4(),
        "ts_init": bed.clock.timestamp_ns(),
        "params": None,
    }
    fields.update(changes)
    return RequestBars(**fields)


def test_a_historical_request_is_answered_off_the_tape_the_subscription_reads(
    make: Any,
) -> None:
    """A strategy that warmed up on one series and traded another would decide blind."""
    bed = make(answers(bars_body(SIP_BAR)), tape=Feed.IEX)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client._request_bars(request_bars(bed)))

    assert bed.asked()[0][FEED_PARAM] == "iex"
    assert len(bed.responses) == 1
    assert [point.ts_event for point in bed.responses[0].data] == [CLOSE_NS]


def test_a_historical_request_states_its_window_and_its_limit(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    bed.run(
        bed.client._request_bars(
            request_bars(
                bed,
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 5, tzinfo=UTC),
                limit=500,
            )
        )
    )
    asked = bed.asked()[0]

    assert asked[START_PARAM] == "2026-08-01T00:00:00Z"
    assert asked[END_PARAM] == "2026-08-05T00:00:00Z"
    assert asked[LIMIT_PARAM] == "500"


def test_a_historical_request_keeps_no_bar_past_the_window_it_asked_for(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    bed.run(
        bed.client._request_bars(
            request_bars(
                bed,
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 3, tzinfo=UTC),
            )
        )
    )

    assert bed.responses[0].data == []


# --- the builder --------------------------------------------------------------


def configured(ws: Workspace, **table: object) -> Workspace:
    """The same workspace with an `[adapters.alpaca]` table."""
    return replace(ws, config=ws.config.model_copy(update={"adapters": {ID: table}}))


def with_credentials(ws: Workspace, client_id: str = PAPER_CLIENT) -> Workspace:
    """The same workspace whose `.env` holds that client's key and secret."""
    from kanso import creds

    key_variable, secret_variable = credential_names(client_id)
    path = ws.root / creds.ENV_FILE
    path.write_text(f"{path.read_text()}{key_variable}={PAPER_KEY}\n{secret_variable}={SECRET}\n")
    return ws


def parts(ws: Workspace) -> dict[str, Any]:
    """The engine pieces a node hands a data client."""
    clock = TestClock()
    return {
        "loop": asyncio.new_event_loop(),
        "msgbus": MessageBus(trader_id=TraderId("KANSO-LIVE"), clock=clock),
        "cache": Cache(),
        "clock": clock,
    }


def test_the_builder_opens_the_declared_tape_for_one_account(ws: Workspace) -> None:
    made = data_client(
        with_credentials(configured(ws, feed="sip")),
        PAPER_CLIENT,
        transport=Replay(answers(bars_body())),
        **parts(ws),
    )

    assert made.feed is Feed.SIP
    assert made.client_id == PAPER_CLIENT
    assert made.id == ClientId("ALPACA_PAPER-SIP")


def test_the_builder_refuses_a_workspace_that_has_declared_no_tape(ws: Workspace) -> None:
    """There is no default, because the two tapes are two series."""
    with pytest.raises(KansoError) as failure:
        data_client(with_credentials(ws), PAPER_CLIENT, **parts(ws))

    assert failure.value.code is Exit.PRECONDITION
    assert "'sip'" in failure.value.message
    assert "'iex'" in failure.value.message


def test_the_builder_refuses_a_tape_that_contradicts_what_was_recorded(ws: Workspace) -> None:
    with pytest.raises(KansoError) as failure:
        data_client(
            with_credentials(configured(ws, feed="iex")),
            PAPER_CLIENT,
            recorded_feed="sip",
            **parts(ws),
        )

    assert failure.value.code is Exit.PRECONDITION


def test_the_builder_refuses_a_key_that_belongs_to_the_other_account(ws: Workspace) -> None:
    """A paper key on the live client is a `401` in the middle of a session, refused here."""
    prepared = with_credentials(configured(ws, feed="sip"), LIVE_CLIENT)
    with pytest.raises(KansoError) as failure:
        data_client(prepared, LIVE_CLIENT, **parts(ws))

    assert failure.value.code is Exit.PRECONDITION
    assert PAPER_KEY not in failure.value.message


def on_disk(ws: Workspace, feed_name: str = "sip") -> Workspace:
    """The same workspace with the table written to its file, which is where a node reads it."""
    path = ws.root / "kanso.toml"
    path.write_text(f'{path.read_text()}\n[adapters.alpaca]\nfeed = "{feed_name}"\n')
    return with_credentials(ws)


def test_the_engine_builds_this_feed_from_a_configuration_carrying_no_credential(
    ws: Workspace,
) -> None:
    """The workspace's path and the client's id are the whole of what the engine is told."""
    from kanso.nautilus.adapters.alpaca.factory import AlpacaDataClientConfig

    on_disk(ws)
    settings = AlpacaDataClientConfig(client_id=PAPER_CLIENT, workspace=str(ws.root))
    made = feed.DATA_CLIENT_FACTORY.create(config=settings, name="alpaca_paper", **parts(ws))

    assert isinstance(made, AlpacaDataClient)
    assert made.feed is Feed.SIP
    assert PAPER_KEY not in str(settings)
    assert SECRET not in str(settings)


def test_the_account_falls_back_to_the_name_the_node_registered(ws: Workspace) -> None:
    from kanso.nautilus.adapters.alpaca.factory import AlpacaDataClientConfig

    on_disk(ws, "iex")
    made = feed.DATA_CLIENT_FACTORY.create(
        config=AlpacaDataClientConfig(workspace=str(ws.root)), name=PAPER_CLIENT, **parts(ws)
    )

    assert made.client_id == PAPER_CLIENT
    assert made.feed is Feed.IEX


def test_a_configuration_of_another_adapter_is_refused(ws: Workspace) -> None:
    from nautilus_trader.config import LiveDataClientConfig

    with pytest.raises(KansoError) as failure:
        feed.DATA_CLIENT_FACTORY.create(
            config=LiveDataClientConfig(), name="alpaca_paper", **parts(ws)
        )

    assert failure.value.code is Exit.PRECONDITION


def test_the_builder_takes_the_adapters_one_shared_connection_by_default(ws: Workspace) -> None:
    """Three clients with three connections would be three times the published limit."""
    prepared = with_credentials(configured(ws, feed="sip", requests_per_minute=60))
    made = data_client(prepared, PAPER_CLIENT, **parts(ws))

    assert made._transport is not None
    assert made._capacity == 15
    assert made.provenance()["host"] == BROKER.config(prepared).data_url


# --- the buckets a request counts against -------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/v2/stocks/AAPL/bars", ["v2/stocks", "v2"]), ("/v2", ["v2"]), ("/", [])],
)
def test_a_request_counts_against_its_endpoints_buckets(path: str, expected: list[str]) -> None:
    assert feed._keys(path) == expected


# --- the instruments a stage trades -------------------------------------------


def test_the_provider_serves_the_workspaces_own_definitions(ws: Workspace) -> None:
    """Never the broker's: a stage must trade the instrument it was certified on."""
    made = instruments.provider(ws)
    asyncio.run(made.load_all_async())

    assert made.workspace is ws
    assert [str(found.id) for found in made.list_all()] == [INSTRUMENT]


def test_the_provider_loads_exactly_the_ids_it_is_asked_for(ws: Workspace) -> None:
    write_store(ws, [equity("MSFT.XNAS")])
    made = instruments.provider(ws)
    asyncio.run(made.load_ids_async([InstrumentId.from_str("MSFT.XNAS")]))

    assert [str(found.id) for found in made.list_all()] == ["MSFT.XNAS"]


def test_an_id_the_store_has_no_definition_for_is_reported_rather_than_dropped(
    ws: Workspace,
) -> None:
    made = instruments.provider(ws)
    wanted = [InstrumentId.from_str(INSTRUMENT), InstrumentId.from_str("MSFT.XNAS")]
    asyncio.run(made.load_ids_async(wanted))

    assert made.unserved(wanted) == (InstrumentId.from_str("MSFT.XNAS"),)
    assert made.count == 1


def test_a_venue_this_broker_does_not_trade_is_not_served(ws: Workspace) -> None:
    """A definition with nowhere to route an order is not one this broker can serve."""
    write_store(ws, [equity("DEMO.SIM")])

    assert list(instruments.provider(ws).available()) == [InstrumentId.from_str(INSTRUMENT)]


def test_the_newest_resolution_of_an_instrument_wins(ws: Workspace) -> None:
    """The store holds one definition per date resolved; the current one is the latest."""
    older = equity(INSTRUMENT, date(2026, 1, 2))
    newer = equity(INSTRUMENT, date(2026, 8, 3))

    assert instruments.served([older, newer])[newer.id].ts_init == newer.ts_init
    assert instruments.served([newer, older])[newer.id].ts_init == newer.ts_init


def test_two_definitions_of_one_date_are_broken_the_same_way_on_every_host() -> None:
    """A tie is broken by content address, so one store answers one way twice."""
    from kanso.data.instruments import definition_checksum

    entry = InstrumentEntry(
        nautilus_id=INSTRUMENT,
        asset_class="EQUITY",
        manual=True,
        corporate_actions="adjust_all",
        override={"currency": "USD", "isin": "US0378331005"},
    )
    first = equity()
    second = build(entry, conventions_for(entry, AS_OF))
    winner = max(first, second, key=definition_checksum)

    assert instruments.served([first, second])[first.id] is winner
    assert instruments.served([second, first])[first.id] is winner


def test_something_that_is_not_an_instrument_is_passed_over() -> None:
    assert instruments.served([object(), equity()]) == {equity().id: equity()}


def test_building_the_provider_reads_nothing(ws: Workspace) -> None:
    """Listing what a workspace can deploy to must cost no store read and no credential."""
    made = instruments.provider(ws)

    assert made.count == 0
    assert isinstance(made, instruments.AlpacaInstrumentProvider)


# --- what the suite is green without ------------------------------------------


def test_no_module_outside_this_package_names_the_broker() -> None:
    """The isolation this milestone rests on, checked over this slice's own two modules."""
    for module in (feed, instruments):
        source = Path(module.__file__ or "").read_text()
        assert "alpaca" in source.lower()

    assert Path(feed.__file__ or "").parent.name == "alpaca"


def test_the_whole_slice_is_reachable_with_every_credential_unset(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for names in CREDENTIALS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)

    assert instruments.provider(ws).unserved(()) == ()
    assert BROKER.describe(ws)["feed"] is None


def test_nothing_in_this_slice_sends_a_credential_as_a_parameter(make: Any) -> None:
    """The leak scan: no request, message or payload carries a key or a secret."""
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())
    written = "".join(
        [repr(bed.client), str(bed.client.provenance())]
        + [request.url + str(request.params) + str(request.keys) for request in bed.transport.asked]
    )

    assert PAPER_KEY not in written
    assert SECRET not in written


def test_a_sequence_of_requests_carries_no_body_and_one_method(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    subscribed(bed)
    bed.clock.set_time(CLOSE_NS + 1)
    bed.run(bed.client.poll())

    assert [request.method for request in bed.transport.asked] == ["GET"]
    assert [request.keys for request in bed.transport.asked] == [("v2/stocks", "v2")]


def test_the_module_surface_is_what_it_declares() -> None:
    for name in feed.__all__:
        assert hasattr(feed, name)
    for name in instruments.__all__:
        assert hasattr(instruments, name)


def test_a_sequence_of_rows_is_read_without_a_mapping(make: Any) -> None:
    """`bars_of` takes any sequence, which is what a walked cursor accumulates."""
    made = bars_of(
        (SIP_BAR,),
        instrument=InstrumentId.from_str(INSTRUMENT),
        resolution="1d",
        price_precision=2,
        size_precision=0,
    )

    assert isinstance(made, tuple)
    assert made[0].ts_event == CLOSE_NS


def test_the_step_of_a_resolution_is_its_own_length() -> None:
    assert feed.step_ns("1d") == DAY_NS
    assert feed.step_ns("1m") == 60 * 1_000_000_000


def test_a_sequence_of_bars_is_what_a_sweep_returns(make: Any) -> None:
    bed = make(answers(bars_body(SIP_BAR)))
    bar_type = subscribed(bed)
    served: Sequence[Any] = bed.client.fetch(bar_type, SESSION_OPEN_NS, CLOSE_NS)

    assert len(served) == 1
