"""`okx_book`: a swap's level-two book, from the exchange's daily order-book archives.

The exchange publishes a day of every swap's 400-level book as one archive, listed by the same
`GET /api/v5/public/market-data-history` that `okx_trades` reads, under `module=4`. Every fact
below was measured against `us.okx.com` and its file host on 2026-10-02 with no credential;
the archives and answers the suite replays were recorded then
(`tests/nautilus/adapters/okx/fixtures/history/`).

**The listing.** `module=4&instType=SWAP&instFamilyList=AEON-USDT&dateAggrType=daily&begin=&end=`
lists one `<instId>-L2orderbook-400lv-<YYYY-MM-DD>.tar.gz` a day, each with a `dateTs` that is
the **UTC** midnight opening the day the file is named for — unlike a trade archive, whose
`dateTs` is the exchange's midnight. `begin` and `end` are sent at the UTC midnights of the
first and last day wanted, and list exactly those days, both included; ranges are asked ten
days at a time, as `okx_trades` asks them, each after the same pause (`trades.LISTING_GAP_S`).

**The archive.** A gzip of one tar member, `<the archive's name>.data`: newline-delimited
JSON, one message a line, keys exactly `instId`, `action`, `ts`, `asks` and `bids`. `ts` is
the exchange's epoch milliseconds as a string, and each level is `[price, size, orders]` as
strings, its size in **contracts** — `"0"` removes the level. The file named for UTC day `D`
holds `D`'s messages from 00:00:00.00x to 23:59:59.9xx UTC, so one archive is one UTC day.
The first message is a full `snapshot` of the book; a snapshot follows every fifteen minutes,
96 a day; the rest are `update`s. On `AEON-USDT-SWAP` 2026-09-01 — 10,663,558 bytes,
486,916 messages, 83.7 MB unpacked — no two messages shared a `ts`, `ts` never went back,
the snapshots fell at lines 1, 4,782 (00:15:00.008), 13,239, ... and 481,641, and the book
was never crossed after a message; over 19 swaps on 2026-09-01 and `BTC-USDT-SWAP` and
`SOL-USDT-SWAP` on 2026-07-15 no top of book was crossed either. A `BTC-USDT-SWAP` day is
273-423 MB and about 5.9 million messages, more than the engine's client accepts in one
answer, so an archive is fetched in ranges (`PIECE`): the file host answers a `Range` with
206 and those bytes, the last piece short, and 416 for a range that starts at the end. It
sent an `etag` equal to the MD5 of the bytes, but the engine's client hands back no header,
so the archive's own checks are the ones there are: it is read through once as it arrives —
the gzip CRC and length, the tar structure, one member of the expected name — before it is
used.

**What a message becomes.** Changes to a book the engine keeps exact to the spec's
`levels`, `K`, of each side, and to nothing deeper:

1. The message is applied to the archive's full book (a snapshot replaces it).
2. On each side, `W` is the archive book's best `K` levels and `w` the worst price in `W`.
   The loader keeps `M`, a model of what the engine holds: `W` as last emitted, and every
   level pushed past `K` at the size it last had.
3. Side by side — bids, then asks — it emits a `DELETE` for every price of `M` better than
   `w` that the archive no longer holds (every price of `M` it does not hold, when the side
   holds fewer than `K`), then an `UPDATE` for every price of `W` whose size moved, then an
   `ADD` for every price of `W` that `M` does not hold, each best first.
4. Nothing is emitted for a level pushed past `K` by better ones: it is left in the
   engine's book as it was, so an order resting there keeps its place in the queue, which a
   `DELETE` would reset. A level that comes back inside `K` is updated or deleted then.

So no message ever becomes a `CLEAR` or carries `F_SNAPSHOT` — the engine sets every resting
order's queue position to zero on either — and each fifteen-minute snapshot is diffed
against the book like any update. The changes of one message share `ts_event` = `ts_init` =
its `ts`, the instant the exchange stamped it public, and the last of them carries `F_LAST`;
a message that changes nothing inside `M`'s view emits nothing. Prices and sizes are read at
the definition's precision, exactly or refused (`history.units`).

**A day opens from the day before.** The first message of day `D` meets `M` as day `D - 1`
closed it — or empty, when `D - 1` is not listed — and emits a `DELETE` for every price of
`M` outside the opening `W` and an `ADD` for every level of `W`, after which `M` is `W`
whatever the engine held. A run that starts on `D` has an empty book, gets the `ADD`s, and
its `DELETE`s do nothing; a run continuing from `D - 1` loses exactly the levels `D - 1` left
behind. Either way its book equals `M` after every message: replayed into the engine's own
`OrderBook`, the top `K` equalled the archive's after all 486,916 messages of 2026-09-01
opened from 2026-08-31, at `K` = 3 (209,367 changes from 124,279 messages) and at `K` = 10
(437,687 from 199,015). `M` as each day closes is kept in the adapter's cache,
`catalog/.cache/okx/book/<instId>/<day>-k<K>-p<price precision>-s<size precision>.json`;
a load whose first day's predecessor is missing there downloads that archive, runs it to its
close without writing a point, keeps the model and deletes the archive.

**An archive is deleted once its day is read.** It is written under a scratch name and
renamed only once it has read through; it stays in the cache if its day stops part-way, and
goes once the day's last message has been read. A cached archive that no longer reads
through is removed and the load stops (exit 1); running it again downloads it again.

**Nothing is skipped in silence.** A range with a UTC day the exchange lists no archive for
is refused at `discover`, naming the days. A `load` that `data sync` asks past the newest
archive stops there; one that meets an unlisted day between listed ones stops with its name.
A message that is not one of the measured shape — another key, an action other than
`snapshot` or `update`, another swap's `instId`, a day not opened by a snapshot, a `ts` going
back or outside the archive's day, a number the precision cannot hold, a crossed book — is
refused naming the archive and the message's `ts`.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`OrderMatchingEngine.process_order_book_delta` clears every queue position on a `CLEAR` or a
delta flagged `F_SNAPSHOT`, and the queue at a price on a `DELETE` there. An `L2_MBP`
`OrderBook` adds the level on an `UPDATE` of a price it does not hold, ignores a `DELETE` of
one it does not hold, and replaces the size on an `ADD` of one it does; an `ADD` or `UPDATE`
of size zero is refused when the delta is built, so a `DELETE` carries size zero.
`nautilus_pyo3.HttpResponse.headers` was measured empty for the file host's answer.
"""

from __future__ import annotations

import bisect
import gzip
import json
import tarfile
import zlib
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar, Final

from nautilus_trader.model.enums import BookAction, OrderSide, RecordFlag

from kanso.data.loaders.points import instrument_id, make_delta
from kanso.errors import Exit, KansoError, ValidationError
from kanso.nautilus.adapters.okx.config import ID
from kanso.nautilus.adapters.okx.history import (
    DAY,
    MS_PER_DAY,
    NS_PER_MS,
    HistoryLoader,
    Series,
    answered,
    day_ms,
    days,
    runs,
    units,
)
from kanso.nautilus.adapters.okx.reference import ARCHIVES, PublicClient
from kanso.nautilus.adapters.okx.trades import (
    LISTING_DAYS,
    LISTING_GAP_S,
    PARTIAL,
    SWAP_SUFFIX,
    Archive,
    _archives,
)
from kanso.nautilus.adapters.okx.venue import VENUE

__all__ = ["BOOK_MODULE", "KEYS", "OkxBookLoader", "listed_books"]

BOOK_MODULE: Final = "4"
"""The listing's `module` for the daily 400-level book; `5` lists a 5000-level book from
2025-11 and `6` a tick-by-tick book behind a plain-`http` link, and neither is read."""

SUFFIX: Final = ".tar.gz"
MEMBER: Final = ".data"
KEYS: Final = frozenset({"instId", "action", "ts", "asks", "bids"})
SNAPSHOT: Final = "snapshot"
UPDATE: Final = "update"
READ: Final = 1 << 20
PIECE: Final = 64 << 20
"""Bytes asked for in one ranged GET. The engine's client refuses an answer over 100 MiB —
measured on 2026-10-02: the 286,582,314 bytes of `BTC-USDT-SWAP` 2026-06-21 were refused
whole — so an archive is fetched a piece at a time and written to disk as it arrives."""

_UNREADABLE = (tarfile.TarError, OSError, EOFError, zlib.error)
"""What a damaged gzip or tar raises while it is read: `gzip.BadGzipFile` (an `OSError`)
for a CRC or length that does not match, `EOFError` for a stream cut short."""

Change = tuple[BookAction, int, int]
"""One change to a side: its action, the side's key for the price, the size after it."""


def listed_books(
    client: PublicClient, inst_id: str, first: date, last: date, pause: Any
) -> dict[date, Archive]:
    """Every book archive of `inst_id` the exchange lists for the UTC days `first`..`last`."""
    family = inst_id.removesuffix(SWAP_SUFFIX)
    found: dict[date, Archive] = {}
    begin = first
    while begin <= last:
        end = min(begin + DAY * (LISTING_DAYS - 1), last)
        pause(LISTING_GAP_S)
        data = answered(
            client,
            ARCHIVES,
            {
                "module": BOOK_MODULE,
                "instType": "SWAP",
                "instFamilyList": family,
                "dateAggrType": "daily",
                "begin": str(day_ms(begin)),
                "end": str(day_ms(end)),
            },
        )
        for archive in _archives(data, SUFFIX, timedelta(0)):
            if first <= archive.day <= last:
                found[archive.day] = archive
        begin = end + DAY
    return found


# --- the two books: the archive's, and the model of the engine's ----------------


class _Side:
    """One side of a book, price key to size, its keys kept sorted best first.

    A bid's key is its price negated, so on both sides the smallest key is the best price.
    """

    __slots__ = ("keys", "sizes")

    def __init__(self, levels: Iterable[tuple[int, int]] = ()) -> None:
        self.sizes: dict[int, int] = dict(levels)
        self.keys: list[int] = sorted(self.sizes)

    def put(self, key: int, size: int) -> None:
        if key not in self.sizes:
            bisect.insort(self.keys, key)
        self.sizes[key] = size

    def drop(self, key: int) -> None:
        if self.sizes.pop(key, None) is not None:
            del self.keys[bisect.bisect_left(self.keys, key)]


def _follow(book: _Side, model: _Side, depth: int) -> list[Change]:
    """The changes that bring the engine's side, as `model` holds it, to the archive's best
    `depth` levels, applied to `model`: the rules of the module docstring, steps 2 to 4."""
    window = book.keys[:depth]
    held = (
        model.keys
        if len(window) < depth
        else model.keys[: bisect.bisect_left(model.keys, window[-1])]
    )
    gone = [key for key in held if key not in book.sizes]
    updates: list[Change] = []
    adds: list[Change] = []
    for key in window:
        size = book.sizes[key]
        was = model.sizes.get(key)
        if was is None:
            adds.append((BookAction.ADD, key, size))
        elif was != size:
            updates.append((BookAction.UPDATE, key, size))
    for key in gone:
        model.drop(key)
    for _, key, size in updates + adds:
        model.put(key, size)
    return [(BookAction.DELETE, key, 0) for key in gone] + updates + adds


def _open(book: _Side, model: _Side, depth: int) -> list[Change]:
    """A day's opening: every level the day before left that the opening window does not
    hold goes, and the whole window is added, so `model` is the window afterwards."""
    window = book.keys[:depth]
    inside = set(window)
    out: list[Change] = [(BookAction.DELETE, key, 0) for key in model.keys if key not in inside]
    out += [(BookAction.ADD, key, book.sizes[key]) for key in window]
    model.sizes = {key: book.sizes[key] for key in window}
    model.keys = list(window)
    return out


@dataclass
class _Message:
    """One archive line, read: its instant, its action and each side's levels as keys."""

    ts: int
    action: str
    bids: list[tuple[int, int]]
    asks: list[tuple[int, int]]


@dataclass
class OkxBookLoader(HistoryLoader):
    """The listed swaps' books, kept exact to `levels` deep, from the daily archives."""

    id: ClassVar[str] = "okx_book"
    type: ClassVar[str] = "book"
    vendor_dataset: ClassVar[str] = f"{ARCHIVES}?module={BOOK_MODULE}"
    chunk_days: ClassVar[int] = 1
    """A liquid swap's day is millions of changes, so a dataset holds one day of them."""
    leveled: ClassVar[bool] = True

    def measure(self, client: PublicClient, series: Series, window: tuple[date, date]) -> None:
        """Refuse a window with a UTC day the exchange lists no book archive for."""
        archives = listed_books(client, series.inst_id, window[0], window[1], self.pause)
        missing = [day for day in days(window) if day not in archives]
        if not missing:
            return
        within = (
            f"it lists the UTC days {runs(sorted(archives))} of that range"
            if archives
            else "it lists no day of that range"
        )
        raise ValidationError(
            f"okx book {series.inst_id}: UTC days {runs(missing)} are not served — the exchange "
            f"lists no order-book archive for them; {within}",
            remedy="ask for days the exchange lists an archive for; kanso does not load a day "
            "it does not serve",
        )

    def points(
        self, client: PublicClient, series: Series, span: tuple[date, date]
    ) -> Iterator[Any]:
        """The book changes of every served day of `span`, in order, one archive at a time."""
        archives = listed_books(client, series.inst_id, span[0] - DAY, span[1], self.pause)
        newest = max(archives, default=span[0])
        gaps = [day for day in days(span) if day not in archives and day < newest]
        if gaps:
            raise ValidationError(
                f"okx book {series.inst_id}: the exchange lists no archive for the UTC days "
                f"{runs(gaps)}, between archives it does list",
                remedy="leave those days out of the range; kanso does not load a day in part",
            )
        served = [day for day in days(span) if day in archives]
        if not served:
            return
        model = self._opened(client, series, served[0], archives)
        for day in served:
            yield from self._day(client, series, archives[day], model, emit=True)

    # --- internals ------------------------------------------------------------

    def _root(self) -> Path:
        from kanso.data.manifest import cache_path

        return self.cache or cache_path(self.workspace) / ID / "book"

    def _model_path(self, series: Series, day: date) -> Path:
        name = f"{day}-k{series.levels}-p{series.price_precision}-s{series.size_precision}.json"
        return self._root() / series.inst_id / name

    def _opened(
        self, client: PublicClient, series: Series, day: date, archives: dict[date, Archive]
    ) -> tuple[_Side, _Side]:
        """The model the day before `day` closed with: kept, rebuilt from its archive, or
        empty when the exchange lists no archive for it."""
        before = day - DAY
        path = self._model_path(series, before)
        if path.is_file():
            return _read_model(path)
        model = (_Side(), _Side())
        if before in archives:
            list(self._day(client, series, archives[before], model, emit=False))
        return model

    def _day(
        self,
        client: PublicClient,
        series: Series,
        archive: Archive,
        model: tuple[_Side, _Side],
        *,
        emit: bool,
    ) -> Iterator[Any]:
        """One archive's changes, `model` carried through it; at its close the model is kept
        and the archive deleted."""
        path = self._fetched(client, archive)
        depth = int(series.levels or 0)
        instrument = instrument_id(series.inst_id, VENUE)
        book = (_Side(), _Side())
        opening = True
        for message in _messages(_lines(path, archive.filename), archive, series):
            if opening and message.action != SNAPSHOT:
                raise _refused(archive, message.ts, "opens the day with an update, not a snapshot")
            changes: list[tuple[OrderSide, Change]] = []
            for side, levels, held, mine in (
                (OrderSide.BUY, message.bids, book[0], model[0]),
                (OrderSide.SELL, message.asks, book[1], model[1]),
            ):
                if message.action == SNAPSHOT:
                    held.sizes = {key: size for key, size in levels if size}
                    held.keys = sorted(held.sizes)
                elif not _touched(held, levels, depth):
                    continue
                follow = _open if opening else _follow
                changes += [(side, change) for change in follow(held, mine, depth)]
            opening = False
            if book[0].keys and book[1].keys and -book[0].keys[0] >= book[1].keys[0]:
                raise _refused(archive, message.ts, "leaves the book crossed")
            if emit and changes:
                instant = message.ts * NS_PER_MS
                last = len(changes) - 1
                for index, (side, (action, key, size)) in enumerate(changes):
                    yield make_delta(
                        instrument,
                        action,
                        side,
                        -key if side is OrderSide.BUY else key,
                        size,
                        0,
                        series.price_precision,
                        series.size_precision,
                        instant,
                        instant,
                        int(RecordFlag.F_LAST) if index == last else 0,
                    )
        _keep_model(self._model_path(series, archive.day), model)
        path.unlink(missing_ok=True)

    def _fetched(self, client: PublicClient, archive: Archive) -> Path:
        """The archive on disk, downloaded and read through once unless it is there already."""
        root = self._root()
        path = root / archive.filename
        if path.is_file():
            return path
        root.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(f"{path.name}{PARTIAL}")
        try:
            with scratch.open("wb") as out:
                for piece in _pieces(client, archive):
                    out.write(piece)
            for _ in _lines(scratch, archive.filename):
                pass
            scratch.replace(path)
        finally:
            scratch.unlink(missing_ok=True)
        return path


def _pieces(client: PublicClient, archive: Archive) -> Iterator[bytes]:
    """The archive's bytes in ranged GETs of `PIECE`, as the file host was measured
    answering them: 206 and the range, a shorter last piece, and 416 for a range starting
    at the end — or 200 and the whole file, were it to ignore the range."""
    offset = 0
    while True:
        wanted = {"Range": f"bytes={offset}-{offset + PIECE - 1}"}
        response = client.fetch(archive.url, name=archive.filename, headers=wanted)
        if response.status == 416 and offset:
            return
        if response.status not in (200, 206):
            raise KansoError(
                f"okx: the archive {archive.filename} answered HTTP {response.status}",
                Exit.ERROR,
                remedy="re-run the command; if it repeats, check the exchange's status page",
            )
        yield response.body
        offset += len(response.body)
        if response.status == 200 or len(response.body) < PIECE:
            return


def _touched(side: _Side, levels: list[tuple[int, int]], depth: int) -> bool:
    """Apply an update's levels to the archive's side, and say whether its best `depth`
    could have moved: not when every change lies past a full window's worst level."""
    edge = side.keys[depth - 1] if len(side.keys) >= depth else None
    touched = bool(levels) and (edge is None or min(key for key, _ in levels) <= edge)
    for key, size in levels:
        if size:
            side.put(key, size)
        else:
            side.drop(key)
    return touched


def _lines(path: Path, name: str) -> Iterator[bytes]:
    """The lines of an archive's one member, the gzip's CRC and length checked at its end.

    An archive that does not read through is removed before the refusal, so the next run
    downloads it again rather than meeting the same bytes.
    """
    member = name.removesuffix(SUFFIX) + MEMBER
    try:
        with gzip.open(path, "rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
            found = 0
            for entry in tar:
                found += 1
                handle = tar.extractfile(entry) if entry.name == member else None
                if found > 1 or handle is None:
                    raise ValidationError(
                        f"okx: the archive {name} holds {entry.name!r}, where every archive "
                        f"measured holds one file, {member}",
                        remedy="the exchange changed its archives; measure them again",
                    )
                yield from handle
            if not found:
                raise ValidationError(
                    f"okx: the archive {name} holds no file, where every archive measured "
                    f"holds one, {member}",
                    remedy="the exchange changed its archives; measure them again",
                )
            deque(iter(lambda: raw.read(READ), b""), maxlen=0)  # to the end: the CRC check
    except _UNREADABLE as exc:
        path.unlink(missing_ok=True)
        raise KansoError(
            f"okx: the archive {name} does not read through as a gzip tar "
            f"({type(exc).__name__}: {exc}); {path} was removed",
            Exit.ERROR,
            remedy="re-run the command, which downloads it again",
        ) from None


def _messages(lines: Iterable[bytes], archive: Archive, series: Series) -> Iterator[_Message]:
    """Each line read as a message of the measured shape, or a refusal naming its `ts`."""
    opens = day_ms(archive.day)
    last = opens
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            raise _refused(archive, last, "is followed by a line that is not JSON") from None
        if not isinstance(record, dict) or record.keys() != KEYS:
            shown = sorted(record) if isinstance(record, dict) else type(record).__name__
            raise _refused(archive, last, f"is followed by a message with the keys {shown}")
        ts = record["ts"]
        if not (isinstance(ts, str) and ts.isdigit()):
            raise _refused(archive, last, f"is followed by a message whose ts is {ts!r}")
        at = int(ts)
        if record["action"] not in (SNAPSHOT, UPDATE):
            raise _refused(archive, at, f"has the action {record['action']!r}")
        if record["instId"] != series.inst_id:
            raise _refused(archive, at, f"is a message of {record['instId']!r}")
        if at < last or at >= opens + MS_PER_DAY:
            raise _refused(archive, at, f"falls outside {archive.day} or before the one before it")
        last = at
        yield _Message(
            at,
            record["action"],
            [(-price, size) for price, size in _levels(record["bids"], archive, at, series)],
            list(_levels(record["asks"], archive, at, series)),
        )


def _levels(levels: object, archive: Archive, ts: int, series: Series) -> Iterator[tuple[int, int]]:
    """A side's levels as whole ticks and contracts, exactly or refused."""
    if not isinstance(levels, list):
        raise _refused(archive, ts, f"has a side that is not a list of levels: {levels!r}")
    for level in levels:
        shaped = isinstance(level, list) and len(level) == 3
        price = units(level[0], series.price_precision) if shaped else None
        size = units(level[1], series.size_precision) if shaped else None
        if price is None or size is None or price <= 0 or size < 0:
            raise ValidationError(
                f"okx: {archive.filename}: the message at ts {ts} has the level {level!r}, "
                f"which is not a price and a size at the definition's precision (price "
                f"{series.price_precision}, size {series.size_precision}) and a count",
                remedy=(
                    "if the contract's tick or lot was different then, state the precision it "
                    "had in the entry's `override` in instruments.yaml and resolve again"
                ),
            )
        yield price, size


def _refused(archive: Archive, ts: int, what: str) -> ValidationError:
    return ValidationError(
        f"okx: {archive.filename}: the message at ts {ts} {what}",
        remedy="the exchange changed its archives; measure them again",
    )


def _keep_model(path: Path, model: tuple[_Side, _Side]) -> None:
    """The model a day closed with, written whole under its own name or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}{PARTIAL}")
    bids, asks = model
    document = {
        "bids": [[-key, bids.sizes[key]] for key in bids.keys],
        "asks": [[key, asks.sizes[key]] for key in asks.keys],
    }
    scratch.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    scratch.replace(path)


def _read_model(path: Path) -> tuple[_Side, _Side]:
    """A kept model, or a refusal naming the file to delete so it is built again."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        bids = _Side((-int(price), int(size)) for price, size in document["bids"])
        asks = _Side((int(price), int(size)) for price, size in document["asks"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError(
            f"okx: the kept book model {path} cannot be read ({type(exc).__name__})",
            remedy=f"delete {path} and load again, which builds it from the day's archive",
        ) from None
    return bids, asks
