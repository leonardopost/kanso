"""`okx_book`, driven against what the exchange was recorded serving.

The listing answers were recorded on 2026-10-02 by driving the loader's own listing code
against `us.okx.com` with no credential, and the two archives the loader reads are excerpts
cut from the archives the exchange served that day — `AEON-USDT-SWAP` on 2026-09-01, lines
1-5,000 (the opening snapshot, the 00:15 snapshot at line 4,782 and 218 updates after it),
and on 2026-08-31, lines 571,775-575,961 (the day's last snapshot to its last message) —
re-packed under the member's own name with no line edited (`fixtures/history/provenance.json`
gives each one's URL, size, sha256 and line range). `History` serves each answer only for
exactly the request it was recorded for, and the loaders run with `as_of` pinned to the day
of the recording. Where a test needs an archive the exchange did not serve — a second member,
a message set back in time, a truncated stream — it builds it in the test from the recorded
excerpt and says so.

AEON's definition is a manual entry: its tick of 0.00001, its lot of one contract and its
contract of 10 AEON are what the exchange's instrument listing gave the operator's workspace
on 2026-10-02, recorded beside the archives; this run was not approved to ask the listing
itself.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, BookType, OrderSide, RecordFlag

from kanso.config import CONFIG_NAME, load_config
from kanso.data.instruments import resolve_universe
from kanso.data.loader import Loader
from kanso.data.manifest import cache_path
from kanso.errors import Exit, KansoError, ValidationError
from kanso.nautilus.adapters.okx import book, trades
from kanso.nautilus.adapters.okx.book import OkxBookLoader, listed_books
from kanso.nautilus.adapters.okx.history import Series
from kanso.nautilus.adapters.okx.reference import Response
from kanso.nautilus.adapters.okx.trades import Archive, OkxTradesLoader
from kanso.workspace import Workspace, init

from .recorded import HISTORY, RANGE_NOT_SATISFIABLE, History, answer

AS_OF = date(2026, 10, 2)
"""The day the listing answers and the archives were recorded."""

AEON = "AEON-USDT-SWAP.OKX"
INST = "AEON-USDT-SWAP"
SEP1 = date(2026, 9, 1)
AUG31 = date(2026, 8, 31)
NAME01 = "AEON-USDT-SWAP-L2orderbook-400lv-2026-09-01.tar.gz"
NAME31 = "AEON-USDT-SWAP-L2orderbook-400lv-2026-08-31.tar.gz"
FILES = "https://static.okx.com/cdn/okx/match/orderbook/pro/L2/400lv/daily"
URL01 = f"{FILES}/20260901/{NAME01}"
URL31 = f"{FILES}/20260831/{NAME31}"
SNAPSHOT_0015 = 1_788_221_700_008
"""Line 4,782 of 2026-09-01: the day's first fifteen-minute snapshot, 00:15:00.008 UTC."""

ENTRY: dict[str, Any] = {
    AEON: {
        "nautilus_id": AEON,
        "asset_class": "CRYPTOCURRENCY",
        "manual": True,
        "corporate_actions": "none",
        "override": {
            "instrument_class": "swap",
            "base_currency": "AEON",
            "quote_currency": "USDT",
            "settlement_currency": "USDT",
            "multiplier": "10",
            "price_increment": "0.00001",
            "size_increment": "1",
            "lot_size": "1",
        },
    }
}


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A workspace on the US host holding AEON's definition."""
    fresh = init(tmp_path / "ws")
    path = fresh.path(CONFIG_NAME)
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[adapters.okx]\nregion = "us"\n', encoding="utf-8"
    )
    (fresh.root / "instruments.yaml").write_text(yaml.safe_dump(ENTRY), encoding="utf-8")
    ready = Workspace(root=fresh.root, config=load_config(path))
    resolve_universe(ready, [AEON], AS_OF)
    return ready


def opened(ws: Workspace, transport: Any = None) -> Any:
    pauses: list[float] = []
    loader = OkxBookLoader(
        workspace=ws, transport=transport or History(), as_of=AS_OF, pause=pauses.append
    )
    loader.pauses = pauses  # type: ignore[attr-defined]
    return loader


def spec(**over: Any) -> dict[str, Any]:
    return {"instruments": [INST], "start": "2026-09-01", "end": "2026-09-01", "levels": 3, **over}


def root(ws: Workspace) -> Path:
    return cache_path(ws) / "okx" / "book"


def loaded(ws: Workspace, levels: int = 3, transport: Any = None) -> list[OrderBookDelta]:
    loader = opened(ws, transport)
    [ref] = loader.discover(spec(levels=levels))
    return list(loader.load(ref, ref.span))


# --- the archive as recorded, read independently of the loader -------------------


def excerpt(name: str) -> list[bytes]:
    """The recorded excerpt's lines, newline kept."""
    with tarfile.open(HISTORY / name, "r:gz") as archive:
        [member] = archive.getmembers()
        handle = archive.extractfile(member)
        assert handle is not None
        return handle.read().splitlines(keepends=True)


def ticks(text: str, precision: int) -> int:
    return int(Decimal(text).scaleb(precision))


def archive_tops(name: str, levels: int) -> dict[int, tuple[list[Any], list[Any]]]:
    """After every message of the excerpt, the archive's best `levels` of each side, as
    (price ticks, contracts) best first — computed here with plain dicts, not the loader."""
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    tops: dict[int, tuple[list[Any], list[Any]]] = {}
    for line in excerpt(name):
        message = json.loads(line)
        for held, side in ((bids, "bids"), (asks, "asks")):
            if message["action"] == "snapshot":
                held.clear()
            for price, size, _ in message[side]:
                if ticks(size, 0):
                    held[ticks(price, 5)] = ticks(size, 0)
                else:
                    held.pop(ticks(price, 5), None)
        tops[int(message["ts"])] = (
            [(p, bids[p]) for p in sorted(bids, reverse=True)[:levels]],
            [(p, asks[p]) for p in sorted(asks)[:levels]],
        )
    return tops


def by_message(points: list[OrderBookDelta]) -> dict[int, list[OrderBookDelta]]:
    grouped: dict[int, list[OrderBookDelta]] = defaultdict(list)
    for point in points:
        grouped[point.ts_init].append(point)
    return grouped


def top(engine: OrderBook, levels: int) -> tuple[list[Any], list[Any]]:
    def read(side: Any) -> list[Any]:
        return [(ticks(str(level.price), 5), int(level.size())) for level in side][:levels]

    return read(engine.bids()), read(engine.asks())


def pack(path: Path, members: dict[str, bytes]) -> Path:
    """A gzip tar holding `members`, as the exchange packs one."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    return path


# --- the adapter hands it out, and the spec it reads ------------------------------


def test_the_book_loader_is_handed_out_with_the_others(ws: Workspace) -> None:
    from kanso.data import registry

    built = registry.adapter_loaders(ws)["okx_book"]()

    assert isinstance(built, OkxBookLoader) and isinstance(built, Loader)
    assert (built.id, built.type, built.chunk_days, built.leveled) == ("okx_book", "book", 1, True)
    assert OkxTradesLoader.chunk_days == 1


def test_discover_names_one_dataset_per_swap_with_its_depth(ws: Workspace) -> None:
    replay = History()
    loader = opened(ws, replay)

    [ref] = loader.discover(spec(instruments=[AEON]))

    assert (ref.instrument, ref.type, ref.resolution, ref.span) == (AEON, "book", None, (SEP1,) * 2)
    assert ref.dataset_id == "AEON_USDT_SWAP.OKX-book-none-raw-20260901"
    assert (ref.vendor, ref.vendor_dataset, ref.publication) == (
        "okx_book",
        "/api/v5/public/market-data-history?module=4",
        "realtime",
    )
    assert ref.request_params == {
        "inst_id": INST,
        "price_precision": "5",
        "size_precision": "0",
        "host": "https://us.okx.com",
        "levels": "3",
    }
    assert Series.of(ref, "okx_book").levels == 3
    assert [params for _, params in replay.asked] == [
        {
            "module": "4",
            "instType": "SWAP",
            "instFamilyList": "AEON-USDT",
            "dateAggrType": "daily",
            "begin": "1788220800000",
            "end": "1788220800000",
        }
    ]
    assert loader.pauses == [trades.LISTING_GAP_S]


@pytest.mark.parametrize(
    ("over", "refusal"),
    [
        ({"levels": None}, "levels: okx_book loads a book kept exact to a depth"),
        ({"levels": 0}, "greater than or equal to 1"),
        ({"levels": 401}, "less than or equal to 400"),
        ({"resolution": "1m"}, "resolution: okx_book loads book points"),
    ],
)
def test_the_depth_is_required_and_bounded(
    ws: Workspace, over: dict[str, Any], refusal: str
) -> None:
    document = {key: value for key, value in spec(**over).items() if value is not None}

    with pytest.raises(ValidationError, match=refusal):
        opened(ws).discover(document)


def test_another_loader_refuses_a_depth(ws: Workspace) -> None:
    with pytest.raises(ValidationError, match="levels: okx_trades loads trade points") as refused:
        OkxTradesLoader(workspace=ws, transport=History(), as_of=AS_OF).discover(spec())
    assert "okx_book" in str(refused.value.remedy)


def test_a_ten_day_range_is_one_listing_request(ws: Workspace) -> None:
    """Recorded: 2026-09-01..10 asked at their UTC midnights lists exactly those ten days."""
    loader = opened(ws)

    found = listed_books(loader.client(), INST, SEP1, date(2026, 9, 10), loader.pauses.append)

    assert sorted(found) == [date(2026, 9, day) for day in range(1, 11)]
    assert found[SEP1] == Archive(SEP1, NAME01, URL01)
    assert len(loader.pauses) == 1


def test_an_eleven_day_listing_is_refused_by_the_exchange(ws: Workspace) -> None:
    """Recorded by hand once: module 4 refuses eleven days as module 1 does (code 50076)."""
    eleven = (
        "market-data-history__begin-1788220800000_dateAggrType-daily_end-1789084800000"
        "_instFamilyList-AEON-USDT_instType-SWAP_module-4.json"
    )
    loader = opened(ws, lambda url, params: answer(eleven))

    with pytest.raises(KansoError, match="HTTP 400, code 50076") as refused:
        loader.discover(spec())
    assert refused.value.code is Exit.ERROR


def test_days_the_exchange_lists_no_archive_for_are_refused_by_name(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(book, "listed_books", lambda *args: {})
    with pytest.raises(ValidationError, match="it lists no day of that range"):
        opened(ws).discover(spec())

    def partly(*args: Any) -> dict[date, Archive]:
        return {SEP1: Archive(SEP1, NAME01, URL01)}

    monkeypatch.setattr(book, "listed_books", partly)
    with pytest.raises(ValidationError) as refused:
        opened(ws).discover(spec(start="2026-09-01", end="2026-09-03"))
    assert refused.value.message.startswith(
        "okx book AEON-USDT-SWAP: UTC days 2026-09-02..2026-09-03 are not served"
    )
    assert "it lists the UTC days 2026-09-01 of that range" in refused.value.message


@pytest.mark.parametrize(
    ("field", "value", "refusal"),
    [
        ("filename", "../" + NAME01, "not a bare tar.gz name"),
        ("filename", "AEON-USDT-SWAP-L2orderbook-400lv-2026-09-01.zip", "not a bare tar.gz name"),
        ("url", URL01.replace("https", "http"), "from a URL that is not https"),
    ],
)
def test_a_listing_entry_that_would_leave_the_cache_or_https_is_refused(
    field: str, value: str, refusal: str
) -> None:
    """The recorded listing with one entry's field changed in the test."""
    listing = json.loads(
        (
            HISTORY / "market-data-history__begin-1788134400000_dateAggrType-daily_end"
            "-1788220800000_instFamilyList-AEON-USDT_instType-SWAP_module-4.json"
        ).read_bytes()
    )["data"]
    listing[0]["details"][0]["groupDetails"][0][field] = value

    with pytest.raises(ValidationError, match=refusal):
        list(trades._archives(tuple(listing), book.SUFFIX, book.DAY * 0))


# --- what the archive becomes -------------------------------------------------------


@pytest.mark.parametrize("levels", [3, 10])
def test_the_engine_s_book_equals_the_archive_s_top_after_every_message(
    ws: Workspace, levels: int
) -> None:
    """Every change of the excerpt replayed into the engine's own L2 book, one message at a
    time: after each of the 5,000 messages its best `levels` equal the archive's."""
    points = loaded(ws, levels)
    grouped = by_message(points)
    engine = OrderBook(points[0].instrument_id, BookType.L2_MBP)
    wanted = archive_tops(NAME01, levels)

    for ts_ms, expected in wanted.items():
        batch = grouped.get(ts_ms * 1_000_000)
        if batch:
            engine.apply_deltas(OrderBookDeltas(batch[0].instrument_id, batch))
        assert top(engine, levels) == expected, ts_ms
    assert len(wanted) == 5000
    assert set(grouped) <= {ts * 1_000_000 for ts in wanted}


def test_every_change_is_stamped_when_the_exchange_stamped_it(ws: Workspace) -> None:
    points = loaded(ws)
    stamps = {int(json.loads(line)["ts"]) * 1_000_000 for line in excerpt(NAME01)}

    assert all(point.ts_event == point.ts_init for point in points)
    assert {point.ts_init for point in points} <= stamps
    assert [point.ts_init for point in points] == sorted(point.ts_init for point in points)


def test_nothing_is_a_clear_or_a_snapshot_and_each_message_ends_on_f_last(ws: Workspace) -> None:
    points = loaded(ws)

    assert {point.action for point in points} == {
        BookAction.ADD,
        BookAction.UPDATE,
        BookAction.DELETE,
    }
    assert not [p for p in points if p.flags & RecordFlag.F_SNAPSHOT]
    for batch in by_message(points).values():
        assert [bool(p.flags & RecordFlag.F_LAST) for p in batch] == [False] * (len(batch) - 1) + [
            True
        ]
        assert all(p.flags in (0, int(RecordFlag.F_LAST)) for p in batch)


def test_sizes_are_contracts_at_the_definition_s_precision(ws: Workspace) -> None:
    """AEON's lot is one contract: the first ask of the opening snapshot, `0.06252` for
    `45`, is 45 contracts at five decimals."""
    points = loaded(ws)
    [first_ask] = [
        p
        for p in by_message(points)[1_788_220_800_004 * 1_000_000]
        if p.order.side is OrderSide.SELL and str(p.order.price) == "0.06252"
    ]

    assert (str(first_ask.order.size), first_ask.order.size.precision) == ("45", 0)
    assert {p.order.price.precision for p in points} == {5}
    assert {str(p.order.size) for p in points if p.action is BookAction.DELETE} == {"0"}


def test_a_level_pushed_past_the_depth_is_not_deleted(ws: Workspace) -> None:
    """Line 26, ts 1788220803514: a new best ask at 0.06251 pushes 0.06254 from third to
    fourth; the archive still shows 0.06254, so no change deletes it and it stays in the
    engine's book past the depth, where an order resting on it keeps its place."""
    points = loaded(ws)
    grouped = by_message(points)
    pushed = 1_788_220_803_514 * 1_000_000
    engine = OrderBook(points[0].instrument_id, BookType.L2_MBP)
    for ts in sorted(grouped):
        if ts > pushed:
            break
        engine.apply_deltas(OrderBookDeltas(grouped[ts][0].instrument_id, grouped[ts]))

    changes = [(p.action, str(p.order.price)) for p in grouped[pushed]]
    assert (BookAction.ADD, "0.06251") in changes
    assert not [c for c in changes if c[1] == "0.06254"]
    asks = [str(level.price) for level in engine.asks()]
    assert asks[:3] == ["0.06251", "0.06252", "0.06253"]
    assert "0.06254" in asks[3:]


def side(*levels: tuple[int, int]) -> Any:
    return book._Side(levels)


def test_a_level_past_the_depth_that_the_archive_drops_is_left_where_it_is() -> None:
    """Built in the test, as keys: the model holds 5 past a depth of three, and the archive
    drops it while showing 4 deeper still. Nothing inside the window moved, so nothing is
    emitted, and 5 stays in the model, as it stays in the engine's book."""
    model = side((1, 5), (2, 5), (3, 5), (5, 9))

    assert book._follow(side((1, 5), (2, 5), (3, 5), (4, 1)), model, 3) == []
    assert (model.keys, model.sizes[5]) == ([1, 2, 3, 5], 9)


def test_a_level_past_the_depth_is_deleted_once_the_window_reaches_it_and_it_is_gone() -> None:
    """The archive's side falls to two levels, so the window's worst is no edge: every
    price the model holds that the archive does not is deleted, 5 among them."""
    model = side((1, 5), (2, 5), (3, 5), (5, 9))

    assert book._follow(side((1, 5), (4, 1)), model, 3) == [
        (BookAction.DELETE, 2, 0),
        (BookAction.DELETE, 3, 0),
        (BookAction.DELETE, 5, 0),
        (BookAction.ADD, 4, 1),
    ]
    assert model.keys == [1, 4]


def test_a_message_s_changes_go_deletes_then_updates_then_adds() -> None:
    model = side((1, 5), (2, 5), (3, 5))

    assert book._follow(side((0, 1), (2, 6), (3, 5)), model, 3) == [
        (BookAction.DELETE, 1, 0),
        (BookAction.UPDATE, 2, 6),
        (BookAction.ADD, 0, 1),
    ]
    assert (model.keys, model.sizes) == ([0, 2, 3], {0: 1, 2: 6, 3: 5})


def test_a_fifteen_minute_snapshot_that_agrees_with_the_book_changes_nothing(
    ws: Workspace,
) -> None:
    """Line 4,782 is a full snapshot of 400 levels a side, and it agreed with the book the
    updates before it had built, at every depth: it becomes no change at all, where a
    snapshot loaded as one would have reset every queue in the book."""
    assert json.loads(excerpt(NAME01)[4781])["action"] == "snapshot"
    for levels in (3, 400):
        assert SNAPSHOT_0015 * 1_000_000 not in by_message(loaded(ws, levels))
        for path in root(ws).rglob("*.json"):
            path.unlink()


def test_a_snapshot_that_disagrees_becomes_ordinary_changes(ws: Workspace) -> None:
    """The 00:15 snapshot with, in the test, its best bid's size changed and its best ask
    removed: the changes are an update of the bid, and a delete and an add on the ask side."""

    def disagree(message: dict[str, Any]) -> None:
        message["bids"][0][1] = str(int(message["bids"][0][1]) + 7)
        del message["asks"][0]

    snapshot = json.loads(excerpt(NAME01)[4781])
    cached(ws, edited(4781, disagree))
    batch = by_message(loaded(ws))[SNAPSHOT_0015 * 1_000_000]

    assert [(p.action, p.order.side, str(p.order.price)) for p in batch] == [
        (BookAction.UPDATE, OrderSide.BUY, snapshot["bids"][0][0]),
        (BookAction.DELETE, OrderSide.SELL, snapshot["asks"][0][0]),
        (BookAction.ADD, OrderSide.SELL, snapshot["asks"][3][0]),
    ]
    assert str(batch[0].order.size) == str(int(snapshot["bids"][0][1]) + 7)


# --- the day's opening and the kept model ------------------------------------------


def test_the_day_opens_from_the_model_the_day_before_closed_with(ws: Workspace) -> None:
    """2026-09-01 opened from the recorded 2026-08-31 excerpt's close: a level that close held
    is deleted only when the opening snapshot does not show it at all; the top three, and
    every deeper level the close held that the snapshot still shows, are added at the
    snapshot's size. A level just past the depth keeps its place in the queue at midnight."""
    replay = History()
    points = loaded(ws, transport=replay)
    kept = json.loads((root(ws) / INST / "2026-08-31-k3-p5-s0.json").read_text())
    opening = by_message(points)[1_788_220_800_004 * 1_000_000]
    first = json.loads(excerpt(NAME01)[0])
    shown = {
        OrderSide.BUY: {ticks(p, 5): ticks(s, 0) for p, s, _ in first["bids"]},
        OrderSide.SELL: {ticks(p, 5): ticks(s, 0) for p, s, _ in first["asks"]},
    }
    window = {
        OrderSide.BUY: {ticks(p, 5) for p, _, _ in first["bids"][:3]},
        OrderSide.SELL: {ticks(p, 5) for p, _, _ in first["asks"][:3]},
    }
    closed = {
        OrderSide.BUY: {price for price, _ in kept["bids"]},
        OrderSide.SELL: {price for price, _ in kept["asks"]},
    }

    for side in (OrderSide.BUY, OrderSide.SELL):
        mine = [p for p in opening if p.order.side is side]
        deleted = {ticks(str(p.order.price), 5) for p in mine if p.action is BookAction.DELETE}
        added = {
            ticks(str(p.order.price), 5): int(p.order.size)
            for p in mine
            if p.action is BookAction.ADD
        }
        assert deleted == closed[side] - set(shown[side])
        assert set(added) == window[side] | (closed[side] & set(shown[side]))
        assert all(added[price] == shown[side][price] for price in added)
        assert len(mine) == len(deleted) + len(added)
        assert (closed[side] - window[side]) & set(shown[side]), "no level past the depth kept"
    assert [p.action for p in opening] == sorted(
        (p.action for p in opening), key=[BookAction.DELETE, BookAction.ADD].index
    )
    assert [url for url, _ in replay.asked if url.startswith(FILES)] == [URL31, URL01]


def test_a_day_s_opening_deletes_only_what_the_snapshot_does_not_show() -> None:
    """Built in the test, as keys, at a depth of three: the day before closed holding 6 and
    7 past the window. The opening snapshot shows 6 and not 7, so 7 goes with 1 and 3, and 6
    is restated at its size beside the window — deletes first, then additions, best first."""
    model = side((1, 5), (2, 5), (3, 5), (6, 2), (7, 3))

    assert book._open(side((0, 4), (2, 5), (4, 1), (6, 1), (8, 1)), model, 3) == [
        (BookAction.DELETE, 1, 0),
        (BookAction.DELETE, 3, 0),
        (BookAction.DELETE, 7, 0),
        (BookAction.ADD, 0, 4),
        (BookAction.ADD, 2, 5),
        (BookAction.ADD, 4, 1),
        (BookAction.ADD, 6, 1),
    ]
    assert (model.keys, model.sizes) == ([0, 2, 4, 6], {0: 4, 2: 5, 4: 1, 6: 1})


def test_a_day_continued_from_the_day_before_reaches_the_archive_s_top(ws: Workspace) -> None:
    """The engine's book fed 2026-08-31's changes and then 2026-09-01's equals the archive's
    top three after every message of 09-01, as one that starts empty on 09-01 does."""
    root(ws).mkdir(parents=True)
    for name in (NAME31, NAME01):
        (root(ws) / name).write_bytes((HISTORY / name).read_bytes())
    loader = opened(ws)
    series = Series(INST, 5, 0, "us.okx.com", None, 3)
    model = (book._Side(), book._Side())
    before = list(loader._day(None, series, Archive(AUG31, NAME31, URL31), model, emit=True))
    engine = OrderBook(before[0].instrument_id, BookType.L2_MBP)
    for batch in by_message(before).values():
        engine.apply_deltas(OrderBookDeltas(batch[0].instrument_id, batch))
    grouped = by_message(
        list(loader._day(None, series, Archive(SEP1, NAME01, URL01), model, emit=True))
    )

    for ts_ms, expected in archive_tops(NAME01, 3).items():
        batch = grouped.get(ts_ms * 1_000_000)
        if batch:
            engine.apply_deltas(OrderBookDeltas(batch[0].instrument_id, batch))
        assert top(engine, 3) == expected, ts_ms


def test_a_day_opened_on_an_empty_book_reaches_the_same_top(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 2026-08-31 not listed, the day opens from nothing: its opening is additions
    only, and the engine's top three after it are the same as when opened from 08-31."""
    real = book.listed_books

    def without(*args: Any) -> dict[date, Archive]:
        found = real(*args)
        found.pop(AUG31, None)
        return found

    monkeypatch.setattr(book, "listed_books", without)
    points = loaded(ws)
    opening = by_message(points)[1_788_220_800_004 * 1_000_000]
    engine = OrderBook(points[0].instrument_id, BookType.L2_MBP)
    engine.apply_deltas(OrderBookDeltas(opening[0].instrument_id, opening))

    assert {p.action for p in opening} == {BookAction.ADD}
    assert len(opening) == 6
    assert top(engine, 3) == archive_tops(NAME01, 3)[1_788_220_800_004]
    assert not (root(ws) / INST / "2026-08-31-k3-p5-s0.json").exists()


def test_the_model_is_kept_and_the_archives_are_deleted_once_read(ws: Workspace) -> None:
    first = loaded(ws)

    assert sorted(path.name for path in (root(ws) / INST).iterdir()) == [
        "2026-08-31-k3-p5-s0.json",
        "2026-09-01-k3-p5-s0.json",
    ]
    assert not list(root(ws).glob("*.tar.gz*"))

    again = History()
    second = loaded(ws, transport=again)

    assert [url for url, _ in again.asked if url.startswith(FILES)] == [URL01]
    assert [p.to_dict(p) for p in second] == [p.to_dict(p) for p in first]


def test_a_model_is_kept_per_depth(ws: Workspace) -> None:
    """A model holds the levels past its own depth, so a load at ten levels never opens from
    the one a load at three kept: it rebuilds 2026-08-31's at ten."""
    loaded(ws, 3)
    again = History()
    loaded(ws, 10, transport=again)

    assert sorted(path.name for path in (root(ws) / INST).iterdir()) == [
        "2026-08-31-k10-p5-s0.json",
        "2026-08-31-k3-p5-s0.json",
        "2026-09-01-k10-p5-s0.json",
        "2026-09-01-k3-p5-s0.json",
    ]
    assert [url for url, _ in again.asked if url.startswith(FILES)] == [URL31, URL01]


def test_a_kept_model_that_does_not_read_is_refused_naming_the_file(ws: Workspace) -> None:
    path = root(ws) / INST / "2026-08-31-k3-p5-s0.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValidationError, match="cannot be read") as refused:
        loaded(ws)
    assert f"delete {path}" in str(refused.value.remedy)


def test_a_day_left_part_way_keeps_its_archive_and_no_model(ws: Workspace) -> None:
    loader = opened(ws)
    [ref] = loader.discover(spec())
    points = loader.load(ref, ref.span)
    next(iter(points))
    points.close()  # type: ignore[attr-defined]

    assert (root(ws) / NAME01).is_file()
    assert not (root(ws) / INST / "2026-09-01-k3-p5-s0.json").exists()
    assert len(loaded(ws)) > 0
    assert not (root(ws) / NAME01).exists()


# --- what is refused ----------------------------------------------------------------


def cached(ws: Workspace, lines: list[bytes], name: str = NAME01) -> Path:
    """The recorded excerpt with its lines changed in the test, put where the loader caches
    the day's archive, under the member name the exchange uses."""
    member = name.removesuffix(".tar.gz") + ".data"
    return pack(root(ws) / name, {member: b"".join(lines)})


def edited(line: int, change: Callable[[dict[str, Any]], Any]) -> list[bytes]:
    lines = excerpt(NAME01)
    message = json.loads(lines[line])
    change(message)
    lines[line] = json.dumps(message, separators=(",", ":")).encode() + b"\n"
    return lines


def setting(key: str, value: Any) -> Callable[[dict[str, Any]], Any]:
    return lambda message: message.__setitem__(key, value)


@pytest.mark.parametrize(
    ("lines", "refusal"),
    [
        (lambda: excerpt(NAME01)[1:], "ts 1788220800014 opens the day with an update"),
        (lambda: edited(1, setting("checksum", 0)), "a message with the keys"),
        (lambda: edited(1, setting("action", "partial")), "has the action 'partial'"),
        (lambda: edited(1, setting("instId", "GRVT-USDT-SWAP")), "is a message of 'GRVT"),
        (lambda: edited(1, setting("ts", "1788220800003")), "or before the one before it"),
        (lambda: edited(1, setting("ts", "1788307200000")), "falls outside 2026-09-01"),
        (lambda: edited(-1, setting("ts", "1788307200000")), "falls outside 2026-09-01"),
        (lambda: edited(1, setting("ts", "17882208.5")), "whose ts is '17882208.5'"),
        (lambda: edited(1, setting("asks", {})), "a side that is not a list of levels"),
        (lambda: edited(1, setting("asks", [["0.062565", "1", "1"]])), "at the definition"),
        (lambda: edited(1, setting("asks", [["0.06256", "1"]])), "at the definition"),
        (lambda: edited(1, setting("asks", [["0.06256", "-1", "1"]])), "at the definition"),
        (lambda: edited(1, setting("bids", [["0", "1", "1"]])), "at the definition"),
        (lambda: edited(-1, setting("asks", [["0.06262", "5", "1"]])), "leaves the book crossed"),
        (lambda: edited(1, setting("asks", [["0.06240", "5", "1"]])), "leaves the book crossed"),
        (lambda: [*excerpt(NAME01)[:2], b"<html>\n"], "followed by a line that is not JSON"),
        (lambda: [*excerpt(NAME01)[:2], b"[1, 2]\n"], "a message with the keys list"),
    ],
)
def test_a_message_not_of_the_measured_shape_is_refused_by_its_ts(
    ws: Workspace, lines: Callable[[], list[bytes]], refusal: str
) -> None:
    """Each archive is the recorded excerpt with one line changed in the test."""
    cached(ws, lines())

    with pytest.raises(ValidationError) as refused:
        loaded(ws)
    assert refused.value.message.startswith(f"okx: {NAME01}: the message at ts ")
    assert refusal in refused.value.message
    assert refused.value.code is Exit.VALIDATION


@pytest.mark.parametrize(
    "lines",
    [
        lambda: edited(1, setting("ts", "1788220800004")),
        lambda: edited(-1, setting("ts", "1788307199999")),
        lambda: edited(0, lambda message: message["asks"].insert(0, ["0.06250", "0", "0"])),
    ],
    ids=["a ts equal to the one before", "the day's last millisecond", "a zero in a snapshot"],
)
def test_a_message_at_the_edge_of_the_measured_shape_is_read(
    ws: Workspace, lines: Callable[[], list[bytes]]
) -> None:
    """Each archive is the recorded excerpt with one line changed in the test: two messages
    sharing a ts, a message at 23:59:59.999, a snapshot level of size zero. Each is read,
    and the zero in a snapshot changes nothing."""
    cached(ws, lines())
    edge = loaded(ws)

    opening = by_message(edge)[1_788_220_800_004 * 1_000_000]
    assert "0.06250" not in {str(p.order.price) for p in opening}


@pytest.mark.parametrize(
    ("members", "refusal"),
    [
        ({"a.data": b"", "b.data": b""}, "holds 'a.data', where every archive measured holds"),
        (
            {"AEON-USDT-SWAP-L2orderbook-400lv-2026-09-01.data": b"", "b.data": b""},
            "holds 'b.data'",
        ),
        ({}, "holds no file"),
    ],
)
def test_an_archive_of_another_layout_is_refused(
    ws: Workspace, members: dict[str, bytes], refusal: str
) -> None:
    """Built in the test: layouts the exchange was not seen to serve."""
    pack(root(ws) / NAME01, members)

    with pytest.raises(ValidationError, match=refusal):
        loaded(ws)


def test_a_cached_archive_that_does_not_read_through_is_removed(ws: Workspace) -> None:
    """The recorded excerpt cut short by a thousand bytes in the test."""
    path = root(ws) / NAME01
    path.parent.mkdir(parents=True)
    path.write_bytes((HISTORY / NAME01).read_bytes()[:-1000])

    with pytest.raises(KansoError, match="does not read through as a gzip tar") as refused:
        loaded(ws)
    assert refused.value.code is Exit.ERROR
    assert "downloads it again" in str(refused.value.remedy)
    assert not path.exists()
    assert len(loaded(ws)) > 0


@pytest.mark.parametrize(
    "damage",
    [
        lambda body: body[:-1000],
        lambda body: body[:-8] + bytes(8),
    ],
    ids=["truncated", "crc"],
)
def test_a_download_that_does_not_read_through_is_never_cached(
    ws: Workspace, damage: Callable[[bytes], bytes]
) -> None:
    """The recorded excerpt cut short, or its CRC and length zeroed, in the test."""
    replay = History()

    def damaged(url: str, params: dict[str, str], headers: Any = None) -> Response:
        served = replay(url, params, headers)
        return Response(served.status, damage(served.body)) if url == URL01 else served

    with pytest.raises(KansoError, match=f"the archive {NAME01} does not read through"):
        loaded(ws, transport=damaged)
    assert not [path.name for path in root(ws).glob("*.tar.gz*")]


@pytest.mark.parametrize("piece", [50_000, 59_372, 1 << 30])
def test_an_archive_is_fetched_in_ranges_and_arrives_whole(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, piece: int
) -> None:
    """Pieces of 50,000 bytes end on a short one; of 59,372, exactly half the 118,744-byte
    excerpt, on the host's 416 for a range starting at the end; one piece is the whole."""
    monkeypatch.setattr(book, "PIECE", piece)
    replay = History()
    seen: list[str] = []

    def ranged(url: str, params: dict[str, str], headers: Any = None) -> Response:
        if url == URL01:
            seen.append(headers["Range"])
        return replay(url, params, headers)

    points = loaded(ws, transport=ranged)

    size = (HISTORY / NAME01).stat().st_size
    assert size == 118_744
    assert seen == [f"bytes={n}-{n + piece - 1}" for n in range(0, size + 1, piece)][: len(seen)]
    assert len(seen) == {50_000: 3, 59_372: 3, 1 << 30: 1}[piece]
    assert len(points) == len(loaded(ws))


def test_a_file_host_that_ignores_the_range_is_read_whole(ws: Workspace) -> None:
    replay = History()

    def whole(url: str, params: dict[str, str], headers: Any = None) -> Response:
        return replay(url, params)

    assert len(loaded(ws, transport=whole)) == len(loaded(ws))


def test_an_archive_answered_other_than_200_stops_the_load(ws: Workspace) -> None:
    replay = History()

    def missing(url: str, params: dict[str, str], headers: Any = None) -> Response:
        return Response(404, b"<Error/>") if url == URL01 else replay(url, params, headers)

    with pytest.raises(KansoError, match=f"the archive {NAME01} answered HTTP 404") as refused:
        loaded(ws, transport=missing)
    assert refused.value.code is Exit.ERROR


def test_an_archive_whose_first_range_is_refused_stops_the_load(ws: Workspace) -> None:
    """A 416 ends an archive only after a first piece: one for the first range is a refusal,
    never an empty archive."""
    replay = History()

    def refused(url: str, params: dict[str, str], headers: Any = None) -> Response:
        if url == URL01:
            return Response(416, RANGE_NOT_SATISFIABLE)
        return replay(url, params, headers)

    with pytest.raises(KansoError, match=f"the archive {NAME01} answered HTTP 416"):
        loaded(ws, transport=refused)


def test_a_day_missing_between_listed_archives_stops_the_load(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = opened(ws)
    [ref] = loader.discover(spec())
    later = replace(ref, span=(SEP1, date(2026, 9, 3)))
    monkeypatch.setattr(
        book,
        "listed_books",
        lambda *args: {
            SEP1: Archive(SEP1, NAME01, URL01),
            date(2026, 9, 3): Archive(SEP1, "x", ""),
        },
    )

    with pytest.raises(ValidationError, match="UTC days 2026-09-02, between archives"):
        list(loader.load(later, later.span))


def test_a_sync_past_the_newest_archive_stops_there(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = opened(ws)
    [ref] = loader.discover(spec())
    real = book.listed_books
    monkeypatch.setattr(book, "listed_books", lambda client, inst, first, last, pause: real(
        client, inst, first, SEP1, pause
    ))  # fmt: skip

    later = replace(ref, span=(SEP1, date(2026, 9, 2)))
    assert {p.ts_init // 86_400_000_000_000 for p in loader.load(later, later.span)} == {20_697}
    nothing = replace(ref, span=(date(2026, 9, 2), date(2026, 9, 2)))
    monkeypatch.setattr(book, "listed_books", lambda *args: {})
    assert list(loader.load(nothing, nothing.span)) == []


def test_the_recorded_excerpts_are_what_provenance_says() -> None:
    import hashlib

    from .recorded import HISTORY_PROVENANCE

    for name in (NAME01, NAME31):
        entry = HISTORY_PROVENANCE["files"][name]
        body = (HISTORY / name).read_bytes()
        lines = excerpt(name)
        first, last = entry["excerpt"]["lines"]
        assert entry["body"] == "excerpt"
        assert hashlib.sha256(body).hexdigest() == entry["excerpt"]["sha256"]
        assert len(body) == entry["excerpt"]["bytes"]
        assert len(lines) == last - first + 1
        assert hashlib.sha256(b"".join(lines)).hexdigest() == entry["excerpt"]["member_sha256"]
        assert entry["served"]["md5"] == entry["served"]["etag"]
    assert HISTORY_PROVENANCE["files"][NAME01]["served"]["sha256"].startswith("e8790da6")
    assert HISTORY_PROVENANCE["files"][NAME31]["served"]["bytes"] == 13_162_775
