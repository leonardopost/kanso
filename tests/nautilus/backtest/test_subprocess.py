"""The card path: a child of its own, an environment allow-list, and a supervisor."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import pytest

from kanso.criteria.run import day_of
from kanso.nautilus.backtest import (
    ALLOWED_ENV,
    child_env,
    run,
    run_subprocess,
)
from tests.processes import ends, running

from .conftest import (
    RAISING_SLEEVE,
    REFUSING_SLEEVE,
    SLOW_SLEEVE,
    TELLING_SLEEVE,
    VANISHING_SLEEVE,
)

pytestmark = pytest.mark.usefixtures("store")


@pytest.fixture
def lane(tmp_path: Path) -> Path:
    """A lane directory holding the three scoped files a card is allowed to see."""
    directory = tmp_path / "lane"
    directory.mkdir()
    for name in ("hypothesis.yaml", "program.md", "strategy.py"):
        (directory / name).write_text(f"# {name}\n", encoding="utf-8")
    return directory


def test_a_card_produces_what_the_same_run_produces_in_process(
    store: Path, lane: Path, request_for
) -> None:
    request = request_for()

    inside = run(request, store)
    outside = run_subprocess(request, store, lane)

    assert outside.crashed is False
    assert outside.run == inside.run
    assert outside.intents == inside.intents


def test_a_card_leaves_the_lane_directory_exactly_as_it_found_it(
    store: Path, lane: Path, request_for
) -> None:
    before = {path.name: path.read_bytes() for path in lane.iterdir()}

    run_subprocess(request_for(), store, lane)

    assert {path.name: path.read_bytes() for path in lane.iterdir()} == before


def test_what_a_killed_lane_left_is_reclaimed_by_its_next_card(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane killed mid-card leaves what its card wrote in its own transfer directory —
    never in a temporary directory of its own that nothing would ever find again — so the
    next card empties it before it starts, and removes it once it has read what came back.
    The window itself is never there: it travels on the child's standard input."""
    from kanso.nautilus import backtest as runner

    left = lane / runner.CARD_ROOM
    left.mkdir()
    (left / runner.OUTPUT_FILE).write_bytes(b"what a killed card said" * 1_000)
    (left / runner.RESULT_FILE).write_bytes(b"a report nobody collected")
    seen: list[list[str]] = []
    supervised = runner._supervised

    def watching(request: object, room: Path, workdir: Path, payload: object) -> object:
        seen.append(sorted(path.name for path in room.iterdir()))
        assert room == left and workdir == lane
        return supervised(request, room, workdir, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_supervised", watching)

    carded = run_subprocess(request_for(), store, lane)

    assert not carded.crashed, carded.traceback_tail
    assert seen == [[]], "the room was emptied, and nothing of the window was put in it"
    assert not left.exists()


def test_the_stream_is_a_header_the_request_one_pickle_per_chunk_and_its_end(
    store: Path, request_for
) -> None:
    """The points are pickled a chunk at a time as the stream is asked for them, and a read
    of one name's daily bars is a day: no process holds the window, and no copy of it is
    held as bytes beside the objects, which for a window of ticks is gigabytes."""
    import pickle

    from kanso.nautilus import backtest as runner

    request = request_for()
    records = [
        pickle.loads(record)
        for record in runner._payload(request, store, (("/elsewhere", "an_extension"),))
    ]

    header, body, *chunks, end = records
    assert header == {"extensions": [["/elsewhere", "an_extension"]]}
    assert isinstance(body, dict) and set(body) == {"request", "instruments"}
    assert body["request"] == request.plain()
    assert end == runner.END
    _, groups = runner.window_data(request, store)
    whole = sorted(int(p.ts_init) for g in groups for p in g)
    assert all(set(chunk) == {"groups"} for chunk in chunks)
    sessions = {day_of(ts) for ts in whole}
    assert len(chunks) == len(sessions), "one pickle per session that held a point"
    assert sorted(int(p.ts_init) for c in chunks for g in c["groups"] for p in g) == whole


def test_the_child_is_given_an_allow_list_and_no_catalog(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The card reports the environment it was actually handed, so this is the boundary
    # itself under test rather than the function that computes it.
    monkeypatch.setenv("KANSO_CATALOG", str(store))
    monkeypatch.setenv("KANSO_MASSIVE_API_KEY", "not-a-real-key")

    result = run_subprocess(request_for(source=TELLING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.reason == "exception"
    reported = result.traceback_tail or ""
    named = reported.split("environment: ")[-1].strip("\"'").split(",")
    assert "KANSO_CATALOG" not in named
    assert "KANSO_MASSIVE_API_KEY" not in named
    assert "PYTHONHASHSEED" in named
    assert set(named) - PLATFORM_ADDED <= {*ALLOWED_ENV, "PYTHONHASHSEED", *_coverage_names()}


PLATFORM_ADDED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
"""Names the interpreter and the operating system add for themselves: the locale
coercion of PEP 538, and CoreFoundation's text encoding on macOS. Neither is inherited
— a child started with neither in its environment is handed both anyway."""


def _coverage_names() -> set[str]:
    return {name for name in os.environ if name.startswith("COVERAGE_")}


def test_the_allow_list_keeps_nothing_it_was_not_given() -> None:
    made = child_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/quant",
            "KANSO_CATALOG": "/data",
            "AWS_SECRET_ACCESS_KEY": "shhh",
            "COVERAGE_PROCESS_CONFIG": "/cfg",
            "PYTHONPATH": "/somewhere",
        }
    )

    assert made == {
        "PATH": "/usr/bin",
        "HOME": "/home/quant",
        "COVERAGE_PROCESS_CONFIG": "/cfg",
        "PYTHONHASHSEED": "0",
    }


def test_the_allow_list_reads_the_ambient_environment_by_default() -> None:
    made = child_env()

    assert made["PYTHONHASHSEED"] == "0"
    assert set(made) <= {*ALLOWED_ENV, "PYTHONHASHSEED", *_coverage_names()}
    assert not [name for name in made if name.startswith("KANSO")]


def test_a_card_that_raises_is_a_crash_with_its_traceback_tail(
    store: Path, lane: Path, request_for
) -> None:
    result = run_subprocess(request_for(source=RAISING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.reason == "exception"
    assert "the card asked for the impossible" in (result.traceback_tail or "")
    assert len((result.traceback_tail or "").splitlines()) <= 50
    assert result.remedy is None
    assert result.run.returns == ()
    assert result.run.capital == request_for().capital


def test_a_refusal_the_child_raised_brings_its_remedy_back(
    store: Path, lane: Path, request_for
) -> None:
    """A card fails for causes that are not the card's, and each names its own next action.

    The traceback tail carries the message and nothing else, so a caller reading only that
    has to guess a remedy for every failure alike. What the cause knew crosses the process
    boundary as a value.
    """
    result = run_subprocess(request_for(source=REFUSING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.remedy == "load it and try again"


def test_a_card_that_leaves_no_report_is_a_crash(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(source=VANISHING_SLEEVE), store, lane)

    assert result.crashed is True
    assert result.reason == "died"


def test_a_card_that_overruns_its_clock_is_killed(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(source=SLOW_SLEEVE, budget_s=0.05), store, lane)

    assert result.crashed is True
    assert result.reason == "budget"
    assert result.intents == ()


def test_a_card_that_outgrows_its_memory_is_killed(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kanso.nautilus.backtest.MEMORY_POLL_S", 0.0)

    result = run_subprocess(
        request_for(source=SLOW_SLEEVE, mem_cap_gb=1e-6),
        store,
        lane,
    )

    assert result.crashed is True
    assert result.reason == "memory"


def test_the_peak_of_the_reaped_child_is_reported(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(), store, lane)

    assert result.peak_mem_gb > 0
    assert result.wall_s > 0


def test_a_child_that_says_nothing_leaves_no_tail(store: Path, lane: Path, request_for) -> None:
    result = run_subprocess(request_for(), store, lane)

    assert result.traceback_tail is None


REVERSING_SLEEVE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 3:
            self.submit_entry(bar.bar_type.instrument_id, "BUY")
        elif self.seen == 5:
            self.submit_entry(bar.bar_type.instrument_id, "SELL")
"""


def test_a_sizing_refusal_crosses_the_process_boundary_as_a_refusal_not_a_crash(
    store: Path, lane: Path, request_for
) -> None:
    request = request_for(source=REVERSING_SLEEVE, sleeve_budget=10_000.0)

    result = run_subprocess(request, store, lane)

    assert result.crashed is False
    assert result.refused is not None
    assert result.refused.rule == "one_position"
    assert result.refused.instrument_id == "DEMO.XNAS"
    assert result.refused.asked == "SELL"
    assert "on the other side" in result.refused.why
    assert result.run.fills == () and result.intents == ()
    assert result.run.capital == request.capital


def test_a_refusal_stops_the_run_at_the_first_refused_order(
    store: Path, lane: Path, request_for
) -> None:
    from kanso.nautilus.sizing import SizingError

    with pytest.raises(SizingError) as failure:
        run(request_for(source=REVERSING_SLEEVE, sleeve_budget=10_000.0), store)

    assert failure.value.refusal.rule == "one_position"
    assert list(failure.value.refusal.held) == ["DEMO.XNAS"]


def test_a_card_interrupted_by_a_stop_is_killed_and_not_a_crash(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child leads its own session; only the watcher can kill it with the lane.

    The stop lands once the child is running, as the watcher starts, rather than on a
    timer: a timer that fired while the window was still being read was the refusal to
    start a card, which the tests below measure, and left the watcher's kill unmeasured.
    """
    import signal

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    watch = runner._watch
    watched: list[Any] = []

    def stopped_once_running(child: Any, *bounds: Any, **fed: Any) -> Any:
        watched.append(child)
        runner.interrupt()
        return watch(child, *bounds, **fed)

    monkeypatch.setattr(runner, "_watch", stopped_once_running)
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted") as failure:
            run_subprocess(request_for(source=SLOW_SLEEVE), store, lane)
    finally:
        runner.resume()

    assert [child.returncode for child in watched] == [-signal.SIGKILL]
    assert "the run resumes" in str(failure.value.remedy)
    assert not runner._INTERRUPT.is_set()


def test_a_card_no_longer_wanted_is_killed_at_the_watcher_s_next_ask(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane's claim taken back while its baseline card runs: the watcher asks the check
    between polls, kills the child and raises what the check raised.

    The check refuses once the child is running, as the watcher starts, so what is measured
    is the watcher's kill rather than the refusal to read or to spawn, which the daemon's
    suite measures (`test_a_removal_during_the_window_load_frees_the_lane_at_the_next_read`).
    """
    import signal

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    watch = runner._watch
    watched: list[Any] = []

    def held() -> None:
        if watched:
            raise PreconditionError("the claim was taken back", remedy="queue it again")

    def taken_once_running(child: Any, *bounds: Any, **fed: Any) -> Any:
        watched.append(child)
        return watch(child, *bounds, **fed)

    monkeypatch.setattr(runner, "_watch", taken_once_running)
    monkeypatch.setattr(runner, "WANTED_POLL_S", 0.05)
    with runner.wanted(held), pytest.raises(PreconditionError, match="taken back") as failure:
        run_subprocess(request_for(source=SLOW_SLEEVE), store, lane)

    assert [child.returncode for child in watched] == [-signal.SIGKILL]
    assert failure.value.remedy == "queue it again"
    assert runner._WANTED == [], "the check is the block's, and leaves with it"
    assert not (lane / runner.CARD_ROOM).exists()


def test_a_card_nobody_wants_reads_no_window_and_starts_no_card(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that already refuses stops the card before the first catalog read."""
    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    def taken() -> None:
        raise PreconditionError("the claim was taken back")

    monkeypatch.setattr(runner, "_window_points", lambda *_: pytest.fail("the window was read"))
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *_a, **_k: pytest.fail("a card was started")
    )
    with runner.wanted(taken), pytest.raises(PreconditionError, match="taken back"):
        run_subprocess(request_for(), store, lane)

    assert runner._WANTED == []


def test_a_process_told_to_stop_reads_no_window_and_starts_no_card(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane that was told to stop while a model answered it goes no further."""
    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    monkeypatch.setattr(runner, "window_data", lambda *_: pytest.fail("the window was read"))
    runner.interrupt()
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted"):
            run_subprocess(request_for(), store, lane)
    finally:
        runner.resume()


def test_a_stop_that_lands_while_the_window_is_read_kills_the_card_it_started(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is read while the card runs, so a stop that lands during a read is a stop
    of a running card: the watch kills it at its next poll, and nothing is read after it."""
    import signal

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    read = runner._window_points
    reads: list[object] = []

    def reading(*args: object) -> object:
        reads.append(args)
        runner.interrupt()
        return read(*args)  # type: ignore[arg-type]

    started = _children(monkeypatch)
    monkeypatch.setattr(runner, "_window_points", reading)
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted"):
            run_subprocess(request_for(), store, lane)
    finally:
        runner.resume()

    assert len(reads) == 1, "the stop ended the stream at the read it landed in"
    assert [child.returncode for child in started] == [-signal.SIGKILL]
    assert not (lane / runner.CARD_ROOM).exists()


def test_a_card_ends_itself_once_its_parent_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watch a card keeps on the lane that started it, driven in this process."""
    from kanso.nautilus import backtest as runner

    parents = iter([4242, 4242, 1])
    ended: list[int] = []
    monkeypatch.setattr(runner.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runner.os, "_exit", ended.append)

    runner.end_with(4242)

    assert ended == [runner.ORPHANED]


WAITS_FOR_GO = (
    "import os, sys, time\n"
    "print('up', flush=True)\n"
    "while not os.path.exists(sys.argv[1]):\n"
    "    time.sleep(0.01)\n"
)
"""A child that says it is up and then waits until it is told to go."""


def test_a_lock_handed_down_is_held_until_the_child_holding_it_exits(tmp_path: Path) -> None:
    """A lane hands its `.work` lock down to every card it starts, so a `stop` can see the
    card exit (`kanso.research.daemon`): with the lane's own descriptor closed, the card's
    still holds the lock, and it lets go only as the card exits."""
    import fcntl
    import sys
    import threading
    import time

    from kanso.nautilus import backtest as runner

    lock, go, said = tmp_path / "l1.4242.work", tmp_path / "go", tmp_path / "said.txt"

    def held() -> bool:
        with lock.open("rb") as reader:
            try:
                fcntl.flock(reader.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
        return False

    lane = lock.open("ab")
    fcntl.flock(lane.fileno(), fcntl.LOCK_EX)
    ended: list[str | None] = []

    def a_card() -> None:
        with runner.handed_down(lane.fileno()):
            breach, _peak, _wall = runner.watched(
                [sys.executable, "-c", WAITS_FOR_GO, str(go)],
                cwd=tmp_path,
                errors=said,
                env=None,
                budget_s=60.0,
                mem_cap_gb=None,
            )
        ended.append(breach)

    watcher = threading.Thread(target=a_card)
    watcher.start()
    try:
        deadline = time.monotonic() + 30.0
        while not (said.is_file() and said.read_text() == "up\n"):
            assert time.monotonic() < deadline, "the card never came up"
            time.sleep(0.01)
        lane.close()  # the lane is gone; only the card holds the descriptor now
        assert held(), "the card did not inherit the lock"
    finally:
        lane.close()
        go.touch()
        watcher.join(timeout=30.0)
    assert ended == [None]
    assert not held(), "the lock outlived the card"
    assert runner._HANDED == [], "nothing is handed down outside the block"


def test_a_card_whose_lane_is_killed_does_not_outlive_it(
    store: Path, lane: Path, request_for, tmp_path: Path
) -> None:
    """Measured with real processes: a lane killed outright takes its card with it.

    The card leads its own session, so the kill never reaches it; left to itself this one
    would spend far longer than the wait below in `on_start` alone. The stream is the one
    the parent writes — the header, the request, the window's chunks and the end — because
    a card handed no chunk runs nothing and ends at once, which made this check a race
    against the child's start-up.
    """
    import signal
    import subprocess
    import sys
    import time

    from kanso.nautilus.backtest import _BOOTSTRAP, _payload

    request = request_for(source=SLOW_SLEEVE)
    handed = tmp_path / "stream.pkl"
    handed.write_bytes(b"".join(_payload(request, store, ())))
    starts_a_card = (
        "import os, subprocess, sys, time\n"
        f"card = subprocess.Popen([sys.executable, '-c', {_BOOTSTRAP!r},"
        f" {str(tmp_path / 'result.pkl')!r}, str(os.getpid())], start_new_session=True,"
        f" cwd={str(lane)!r}, stdin=open({str(handed)!r}, 'rb'))\n"
        "print(card.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    fake_lane = subprocess.Popen([sys.executable, "-c", starts_a_card], stdout=subprocess.PIPE)
    assert fake_lane.stdout is not None
    card = int(fake_lane.stdout.readline())
    try:
        time.sleep(1.0)
        assert running(card), "the card was running while its lane was"
        fake_lane.send_signal(signal.SIGKILL)
        fake_lane.wait(timeout=10)

        assert ends(card, within_s=8.0), "the card outlived the lane that started it"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(card, signal.SIGKILL)


def test_the_parent_holds_one_read_of_the_window_at_a_time(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child holds its own copy of the chunk it runs; the parent, which reads the window
    only to stream it, holds nothing of a read once the next begins. Measured before this: a
    lane kept 1.4 GB of an eighteen-month five-second window alive beside a 2.7 GB card."""
    import sys

    from kanso.nautilus import backtest as runner

    read = runner._window_points
    window: list[object] = []

    def reading(*args: object) -> object:
        holding: list[str] = []
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_filename == runner.__file__:
                holding += [
                    f"{frame.f_code.co_name}.{name}"
                    for name, value in frame.f_locals.items()
                    if any(value is held for held in window)
                ]
            frame = frame.f_back
        assert holding == [], f"the parent still holds an earlier read: {holding}"
        groups, loaded = read(*args)  # type: ignore[arg-type]
        window.extend(groups)
        window.extend(point for group in groups for point in group)
        return groups, loaded

    monkeypatch.setattr(runner, "_window_points", reading)

    request = request_for()
    carded = run_subprocess(request, store, lane)

    assert not carded.crashed, carded.traceback_tail
    reads = sum(isinstance(held, tuple) for held in window)
    _, groups = runner.window_data(request, store)
    sessions = {day_of(int(p.ts_init)) for g in groups for p in g}
    assert reads == len(sessions), "the window was read once, a session at a time"


def _children(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every process the card path starts from here on, kept to be asked how it ended."""
    import subprocess

    started: list[Any] = []
    spawn = subprocess.Popen

    def recorded(*args: Any, **kwargs: Any) -> Any:
        child = spawn(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", recorded)
    return started


def _gone(pid: int) -> bool:
    """Whether no process of this id is left, reaped and all."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _tick_store(root: Path, *, two: bool = False) -> Path:
    """A catalog of the two tick sessions of `conftest.ticking`, and with `two` of
    `conftest.quiet` beside them."""
    from .conftest import QUIET, catalog, instrument, tick_groups

    names = [instrument()] + ([instrument(QUIET)] if two else [])
    return catalog(root, [point for group in tick_groups(two=two) for point in group], names)


def _session(day: Any, *, booked: bool, shift: int = 0) -> list[object]:
    """A book opened at noon, 10.00 bid and 10.02 offered (plus `shift` cents), when
    `booked`; and nineteen prints of 30 seven minutes apart after it, buyers' at the offer and
    sellers' at the bid in turn, so the session's last two hours hold prints and no change."""
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
    from nautilus_trader.model.objects import Price, Quantity

    from kanso.criteria.run import midnight_ns
    from kanso.data.loaders.points import make_delta

    from .conftest import SECOND_NS, SYMBOL, _venue

    ident = InstrumentId(Symbol(SYMBOL), _venue())
    base = midnight_ns(day) + 12 * 3_600 * SECOND_NS
    bid, ask = 1_000 + shift, 1_002 + shift
    made: list[object] = []
    if booked:
        made += [
            make_delta(ident, BookAction.ADD, OrderSide.BUY, bid, 50, 0, 2, 0, base, base),
            make_delta(ident, BookAction.ADD, OrderSide.SELL, ask, 50, 0, 2, 0, base, base),
        ]
    for event in range(1, 20):
        ts = base + event * 420 * SECOND_NS + 1_000_000
        px, side = (bid, AggressorSide.SELLER) if event % 2 == 0 else (ask, AggressorSide.BUYER)
        made.append(
            TradeTick(
                ident,
                Price(px / 100, 2),
                Quantity.from_int(30),
                side,
                TradeId(f"{day:%m%d}-{event}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return made


def test_a_day_of_prints_without_its_book_is_refused_however_the_window_is_read(
    tmp_path: Path, lane: Path, request_for
) -> None:
    """A book hypothesis whose window holds a day's prints and none of that day's book changes
    — its book archive missing, and the market three dollars on — is refused that day, read
    whole or an hour at a time, rather than matched against the book the day before left.
    Measured before this: streamed an hour or a day at a time, the card ran, and its venue
    matched the second day's prints against the first day's book. Hours of prints after a
    day's last change, inside a day that holds its book, are not refused."""
    from kanso.errors import PreconditionError

    from .conftest import POSTER, TICK_DAYS, catalog, instrument, tick_hypothesis

    first, second = TICK_DAYS
    request = request_for(source=POSTER, hypothesis_=tick_hypothesis(0.0))
    whole_day = catalog(tmp_path / "first", _session(first, booked=True), [instrument()])
    gap = catalog(
        tmp_path / "gap",
        [*_session(first, booked=True), *_session(second, booked=False, shift=300)],
        [instrument()],
    )

    alone = run_subprocess(request, whole_day, lane)
    assert not alone.crashed, alone.traceback_tail
    assert alone.run.fills, "the first day trades, its last two hours prints alone"

    with pytest.raises(PreconditionError, match=f"no book change for DEMO.XNAS on {second}"):
        run(request, gap)
    carded = run_subprocess(request, gap, lane)

    assert carded.crashed
    assert carded.traceback_tail is not None and f"DEMO.XNAS on {second}" in carded.traceback_tail
    assert carded.remedy == (
        f"load the book for DEMO.XNAS over {second}..{second} with `kanso data load`, "
        "then take a snapshot"
    )


@pytest.mark.parametrize("two", [False, True], ids=["one name", "two names"])
@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_tick_window_chunked_by_hour_by_point_cap_and_by_day_gives_the_identical_card(
    tmp_path: Path,
    lane: Path,
    request_for,
    monkeypatch: pytest.MonkeyPatch,
    latency_ms: float,
    two: bool,
) -> None:
    """A window of book changes and prints, several to an instant on some instants and one
    on others, read a day at a time, an hour at a time, an hour at a time cut to seven points
    a chunk, and in the reads its files allow: the card is the card the whole window run in
    this process gives.

    Seven points cut inside an hour, and leave chunks whose every instant holds one point of
    its kind — a feed that would go unmarked if whether it is marked were read off the
    chunk, and whose prints would then be handled before a command due at them landed.

    With two names, the second a quiet one whose book changes always follow the first's
    (`conftest.quiet`), and chunks that hold its prints and none of its changes. The engine
    reads the names a stream holds off the first point of each batch it is handed, so before
    this it refused to run the quiet name, whose changes began no batch and whose prints
    began their own, even handed the window whole — as it refused one of five OKX books on
    2026-10-03."""
    from nautilus_trader.model.data import OrderBookDelta, TradeTick

    from kanso.nautilus import backtest as runner

    from .conftest import INSTRUMENT, POSTER, QUIET, tick_hypothesis

    store = _tick_store(tmp_path / "ticks", two=two)
    quiet = f"{QUIET}.XNAS"
    universe = (INSTRUMENT, quiet) if two else (INSTRUMENT,)
    request = request_for(source=POSTER, hypothesis_=tick_hypothesis(latency_ms, universe))
    whole = run(request, store)
    traded = {fill.instrument_id for fill in whole.run.fills}
    assert traded == set(universe), "each name trades, so the comparison compares something"

    cut = runner._cut
    chunks: list[int] = []
    alone: list[bool] = []

    def counted(groups: Any, cap: int) -> Any:
        for chunk in cut(groups, cap):
            chunks.append(sum(len(group) for group in chunk))
            held = {(type(p), str(p.instrument_id)) for group in chunk for p in group}
            alone.append((TradeTick, quiet) in held and (OrderBookDelta, quiet) not in held)
            yield chunk

    monkeypatch.setattr(runner, "_cut", counted)
    carded: dict[str, Any] = {}
    for name, read_ns, lengthened, cap in (
        ("day", runner.NS_PER_DAY, 0, 10**9),
        ("hour", runner.READ_TICK_NS, 0, 10**9),
        ("hour, seven points", runner.READ_TICK_NS, 0, 7),
        ("planned", runner.READ_TICK_NS, runner.READ_POINTS, runner.CHUNK_POINTS),
    ):
        chunks.clear()
        alone.clear()
        monkeypatch.setattr(runner, "READ_TICK_NS", read_ns)
        monkeypatch.setattr(runner, "READ_POINTS", lengthened)
        monkeypatch.setattr(runner, "CHUNK_POINTS", cap)
        result = run_subprocess(request, store, lane)
        assert not result.crashed, result.traceback_tail
        carded[name] = (result.run, result.intents, tuple(chunks), any(alone))

    assert {name: (ran, intents) for name, (ran, intents, *_) in carded.items()} == {
        name: (whole.run, whole.intents) for name in carded
    }
    assert len(carded["day"][2]) == 2, "a read of a day is a session"
    assert len(carded["hour"][2]) == 10, "five hours a session"
    assert max(carded["hour, seven points"][2]) <= 7
    assert len(carded["hour, seven points"][2]) > len(carded["hour"][2])
    assert len(carded["planned"][2]) == 1, "two sparse sessions are one read"
    assert carded["hour, seven points"][3] is two, (
        "a chunk holds the quiet name's prints and none of its changes"
    )


def test_a_tick_window_whose_first_hour_holds_only_book_changes_gives_the_identical_card(
    tmp_path: Path, lane: Path, request_for
) -> None:
    """A book that opens an hour before the first print: read an hour at a time, the card's
    first chunk is two book changes and no marker, and the sleeve still has to be held for
    the markers of every chunk after it, because it subscribes to them as it starts. Held only
    from the first chunk that carries a marker, the same card made two fills and two orders
    fewer than the window run whole."""
    from nautilus_trader.model.enums import BookAction, OrderSide

    from kanso.data.loaders.points import make_delta

    from .conftest import (
        POSTER,
        SECOND_NS,
        TICK_DAYS,
        catalog,
        instrument,
        tick_hypothesis,
        ticking,
    )

    book, made = ticking(TICK_DAYS[0])
    opens = int(book[0].ts_init) - 3_600 * SECOND_NS  # type: ignore[attr-defined]
    ident = book[0].instrument_id  # type: ignore[attr-defined]
    book = [
        make_delta(ident, BookAction.ADD, OrderSide.BUY, 1_000, 50, 0, 2, 0, opens, opens),
        make_delta(ident, BookAction.ADD, OrderSide.SELL, 1_002, 50, 0, 2, 0, opens, opens),
        *book[2:],
    ]
    store = catalog(tmp_path / "early", [*book, *made], [instrument()])
    request = request_for(source=POSTER, hypothesis_=tick_hypothesis(20.0))

    whole = run(request, store)
    carded = run_subprocess(request, store, lane)

    assert whole.run.fills
    assert not carded.crashed, carded.traceback_tail
    assert (carded.run, carded.intents) == (whole.run, whole.intents)


def test_a_bar_window_cut_below_a_day_gives_the_identical_card(
    tmp_path: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two names' daily bars cut to one bar a chunk — below an instant, so each chunk is the
    instant both names share — give the card the day reads do."""
    from kanso.nautilus import backtest as runner

    from .conftest import INSTRUMENT, RESEARCH, bars, catalog, hypothesis, instrument

    other = "OTHR"
    store = catalog(
        tmp_path / "two",
        [*bars(RESEARCH), *bars(RESEARCH, other)],
        [instrument(), instrument(other)],
    )
    request = request_for(hypothesis_=hypothesis(universe=[INSTRUMENT, f"{other}.XNAS"]))

    by_day = run_subprocess(request, store, lane)
    monkeypatch.setattr(runner, "CHUNK_POINTS", 1)
    by_instant = run_subprocess(request, store, lane)

    assert not by_day.crashed, by_day.traceback_tail
    assert by_day.run.fills
    assert (by_instant.run, by_instant.intents) == (by_day.run, by_day.intents)


def _after_the_first_chunk(monkeypatch: pytest.MonkeyPatch, then: Any) -> list[int]:
    """Have the stream call `then` when it is asked for the record after its first chunk —
    once that chunk is wholly written — and count the chunks it was asked for."""
    import pickle

    from kanso.nautilus import backtest as runner

    stream = runner._stream
    asked: list[int] = []

    def streaming(*args: Any) -> Any:
        for record in stream(*args):
            yield record
            if "groups" in pickle.loads(record):
                asked.append(len(record))
                if len(asked) == 1:
                    then()

    monkeypatch.setattr(runner, "_stream", streaming)
    return asked


def test_a_card_stopped_mid_stream_leaves_no_spool_and_no_child(
    tmp_path: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that lands once the first chunk is written: the child is killed and reaped,
    the stream goes no further, and the lane holds its three files and nothing else."""
    import signal

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    from .conftest import tick_hypothesis

    store = _tick_store(tmp_path / "ticks")
    started = _children(monkeypatch)
    asked = _after_the_first_chunk(monkeypatch, runner.interrupt)
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted"):
            run_subprocess(
                request_for(source=SLOW_SLEEVE, hypothesis_=tick_hypothesis()), store, lane
            )
    finally:
        runner.resume()

    assert len(asked) == 1
    (child,) = started
    assert child.returncode == -signal.SIGKILL and _gone(child.pid)
    assert sorted(path.name for path in lane.iterdir()) == [
        "hypothesis.yaml",
        "program.md",
        "strategy.py",
    ]


def test_a_card_out_of_time_mid_stream_leaves_no_spool_and_no_child(
    tmp_path: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card whose budget runs out while its stream is still being written — its child
    busy in `on_start`, the pipe full behind it — is killed as any card out of time is."""
    import pickle
    import signal

    from kanso.nautilus import backtest as runner

    from .conftest import tick_hypothesis

    store = _tick_store(tmp_path / "ticks")
    stream = runner._stream

    def endless(*args: Any) -> Any:
        records = stream(*args)
        yield next(records)
        yield next(records)
        yield next(records)
        while True:  # more than any pipe holds, so the stream is mid-way when time runs out
            yield pickle.dumps({"groups": ()})

    started = _children(monkeypatch)
    monkeypatch.setattr(runner, "_stream", endless)
    result = run_subprocess(
        request_for(source=SLOW_SLEEVE, hypothesis_=tick_hypothesis(), budget_s=1.0), store, lane
    )

    assert (result.crashed, result.reason) == (True, "budget")
    (child,) = started
    assert child.returncode == -signal.SIGKILL and _gone(child.pid)
    assert not (lane / runner.CARD_ROOM).exists()


def test_a_refusal_raised_mid_stream_is_a_refusal(
    tmp_path: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `wanted` check that refuses before the second read — after the first chunk went
    out — comes out of the card as the refusal it was, remedy and all, with the child dead."""
    import signal

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    from .conftest import tick_hypothesis

    store = _tick_store(tmp_path / "ticks")
    read = runner._window_points
    reads: list[object] = []

    def reading(*args: object) -> object:
        reads.append(args)
        return read(*args)  # type: ignore[arg-type]

    def held() -> None:
        if reads:
            raise PreconditionError("the claim was taken back", remedy="queue it again")

    started = _children(monkeypatch)
    monkeypatch.setattr(runner, "_window_points", reading)
    with runner.wanted(held), pytest.raises(PreconditionError, match="taken back") as refused:
        run_subprocess(request_for(source=SLOW_SLEEVE, hypothesis_=tick_hypothesis()), store, lane)

    assert refused.value.remedy == "queue it again"
    assert len(reads) == 1
    (child,) = started
    assert child.returncode == -signal.SIGKILL and _gone(child.pid)
    assert not (lane / runner.CARD_ROOM).exists()


def test_a_window_holding_only_its_warmup_is_refused_after_the_stream(
    tmp_path: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether a window held anything is known only once all of it is read, with the child
    already running the prefix: it is killed, and the refusal is the parent's to make."""
    from datetime import date

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    from .conftest import bars, catalog, instrument

    prefix = (date(2023, 12, 1), date(2023, 12, 31))
    store = catalog(tmp_path / "december", bars(prefix), [instrument()])
    started = _children(monkeypatch)
    with pytest.raises(PreconditionError, match="holds nothing") as refused:
        run_subprocess(request_for(prefix=prefix), store, lane)

    assert refused.value.remedy is not None and "data load" in refused.value.remedy
    (child,) = started
    assert child.returncode is not None and _gone(child.pid)
    assert not (lane / runner.CARD_ROOM).exists()


def test_the_parent_reads_a_chunk_only_once_the_last_one_is_written() -> None:
    """The feed asks its stream for a record only once every byte of the one before is in
    the pipe, so what a slow reader has taken is never more than a pipe's worth behind what
    the stream has handed over."""
    import threading
    import time

    from kanso.nautilus import backtest as runner

    records = [bytes([index]) * 600_000 for index in range(4)]
    received = bytearray()
    behind: list[int] = []
    read_fd, write_fd = os.pipe()

    def payload() -> Any:
        for index, record in enumerate(records):
            behind.append(sum(map(len, records[:index])) - len(received))
            yield record

    def slow_reader() -> None:
        while chunk := os.read(read_fd, 16_384):
            received.extend(chunk)
            time.sleep(0.0005)

    reader = threading.Thread(target=slow_reader)
    reader.start()
    feed = runner._Feed(os.fdopen(write_fd, "wb"), payload())
    while not feed._done:
        feed.step(0.01)
    reader.join(timeout=30)
    os.close(read_fd)

    assert bytes(received) == b"".join(records)
    assert behind[0] == 0
    assert all(0 <= gap <= 256 * 1024 for gap in behind), behind


def test_a_feed_whose_reader_is_gone_goes_quiet() -> None:
    """A child that stops reading breaks the pipe: the feed lets go of its stream and leaves
    the watch to reap the child, rather than raising out of the watch."""
    from kanso.nautilus import backtest as runner

    closed: list[bool] = []

    def payload() -> Any:
        try:
            while True:
                yield b"x" * 100_000
        finally:
            closed.append(True)

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    pipe = os.fdopen(write_fd, "wb")
    feed = runner._Feed(pipe, payload())
    feed.step(0.01)
    feed.step(0.01)

    assert feed._done and closed == [True]
    assert pipe.closed


def test_a_feed_closes_the_pipe_the_moment_its_stream_ends() -> None:
    """The child reads the end of its input as soon as the stream ends, whether or not the
    stream's last record was `END`, so a stream that stopped short is refused by the child
    rather than waited on. Before this the pipe stayed open until the child had exited, and a
    stream with no `END` left the child waiting on its input until it was killed."""
    from kanso.nautilus import backtest as runner

    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    pipe = os.fdopen(write_fd, "wb")
    feed = runner._Feed(pipe, iter([b"a record"]))
    while not feed._done:
        feed.step(0.01)

    try:
        assert os.read(read_fd, 64) == b"a record"
        assert os.read(read_fd, 64) == b"", "the reader is at the end of its input"
    finally:
        os.close(read_fd)
    feed.close()
    assert pipe.closed


def test_a_card_whose_stream_ends_without_its_end_is_refused_not_waited_on(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that ends without `END` closes the child's input, and the child refuses the
    window as cut short; the card does not run on to its time budget."""
    import pickle

    from kanso.nautilus import backtest as runner

    stream = runner._stream

    def short(*args: Any) -> Any:
        for record in stream(*args):
            if pickle.loads(record) != runner.END:
                yield record

    monkeypatch.setattr(runner, "_stream", short)
    carded = run_subprocess(request_for(budget_s=30.0), store, lane)

    assert carded.crashed
    assert carded.reason != runner.BUDGET
    assert carded.traceback_tail is not None and "stopped before its end" in carded.traceback_tail


def test_a_stream_cut_before_its_end_is_not_a_card(
    tmp_path: Path, request_for, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child whose stream stops before `END` — its lane died mid-window — writes a failure
    naming the cut, never a run of the part it was handed."""
    import io
    import pickle
    import sys

    from kanso.nautilus import backtest as runner

    whole = list(runner._payload(request_for(), store, ()))
    assert pickle.loads(whole[-1]) == runner.END
    monkeypatch.setattr(runner, "end_with", lambda _parent: None)
    for cut in (b"".join(whole[:-1]), b"".join(whole[:-1])[:-10]):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(cut)))
        result = tmp_path / "result.pkl"

        assert runner.main([str(result), "1"]) == 1

        reported = pickle.loads(result.read_bytes())
        assert reported["ok"] is False
        assert "stopped before its end" in reported["traceback"]
        assert reported["remedy"] == "start the daemon again; the run resumes from its last card"
