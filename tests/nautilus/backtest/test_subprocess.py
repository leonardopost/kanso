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


def test_a_payload_a_killed_lane_left_is_reclaimed_by_its_next_card(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane killed mid-card leaves its payload in its own transfer directory — never in a
    temporary directory of its own that nothing would ever find again — so the next card
    empties it before writing, and removes it once it has read what came back."""
    from kanso.nautilus import backtest as runner

    left = lane / runner.CARD_ROOM
    left.mkdir()
    (left / "request.pkl").write_bytes(b"a window of points nobody will read" * 1_000)
    (left / "result.pkl").write_bytes(b"a report nobody collected")
    seen: list[list[str]] = []
    supervised = runner._supervised

    def watching(request: object, room: Path, workdir: Path) -> object:
        seen.append(sorted(path.name for path in room.iterdir()))
        assert room == left and workdir == lane
        return supervised(request, room, workdir)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_supervised", watching)

    carded = run_subprocess(request_for(), store, lane)

    assert not carded.crashed, carded.traceback_tail
    assert seen == [["request.pkl"]], "the stale report went, and this card's payload is fresh"
    assert not left.exists()


def test_the_payload_is_a_header_the_request_and_one_pickle_per_session(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The points reach the file a session at a time: no process holds the window, and no
    copy of it is held as bytes beside the objects, which for a window of ticks is gigabytes."""
    import pickle

    from kanso.nautilus import backtest as runner

    read: list[tuple[object, object, list[object]]] = []

    def reading(request: object, room: Path, workdir: Path) -> object:
        with (room / runner.REQUEST_FILE).open("rb") as handle:
            header, body = pickle.load(handle), pickle.load(handle)
            chunks: list[object] = []
            while True:
                try:
                    chunks.append(pickle.load(handle))
                except EOFError:
                    break
            read.append((header, body, chunks))
        raise RuntimeError("read, and not run")

    monkeypatch.setattr(runner, "_supervised", reading)
    request = request_for()

    with pytest.raises(RuntimeError, match="read, and not run"):
        run_subprocess(request, store, lane, (("/elsewhere", "an_extension"),))

    ((header, body, chunks),) = read
    assert header == {"extensions": [["/elsewhere", "an_extension"]]}
    assert isinstance(body, dict) and set(body) == {"request", "instruments"}
    assert body["request"] == request.plain()
    _, groups = runner.window_data(request, store)
    whole = sorted(int(p.ts_init) for g in groups for p in g)
    assert all(set(chunk) == {"groups"} for chunk in chunks)
    sessions = {day_of(ts) for ts in whole}
    assert len(chunks) == len(sessions), "one pickle per session that held a point"
    assert sorted(int(p.ts_init) for c in chunks for g in c["groups"] for p in g) == whole
    assert not (lane / runner.CARD_ROOM).exists(), "removed even when the card never ran"


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

    def stopped_once_running(child: Any, budget_s: Any, mem_cap_gb: Any) -> Any:
        watched.append(child)
        runner.interrupt()
        return watch(child, budget_s, mem_cap_gb)

    monkeypatch.setattr(runner, "_watch", stopped_once_running)
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted") as failure:
            run_subprocess(request_for(source=SLOW_SLEEVE), store, lane)
    finally:
        runner.resume()

    assert [child.returncode for child in watched] == [-signal.SIGKILL]
    assert "the run resumes" in str(failure.value.remedy)
    assert not runner._INTERRUPT.is_set()


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


def test_a_stop_that_lands_while_the_window_is_read_starts_no_card(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    read = runner._window_points

    def reading(*args: object) -> object:
        runner.interrupt()
        return read(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_window_points", reading)
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *_a, **_k: pytest.fail("a card was started")
    )
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted"):
            run_subprocess(request_for(), store, lane)
    finally:
        runner.resume()


def test_a_card_ends_itself_once_its_parent_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watch a card keeps on the lane that started it, driven in this process."""
    from kanso.nautilus import backtest as runner

    parents = iter([4242, 4242, 1])
    ended: list[int] = []
    monkeypatch.setattr(runner.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runner.os, "_exit", ended.append)

    runner._end_with(4242)

    assert ended == [runner.ORPHANED]


def test_a_card_whose_lane_is_killed_does_not_outlive_it(
    store: Path, lane: Path, request_for, tmp_path: Path
) -> None:
    """Measured with real processes: a lane killed outright takes its card with it.

    The card leads its own session, so the kill never reaches it; left to itself this one
    would spend far longer than the wait below in `on_start` alone. The payload is staged
    as the parent stages it — the header, the request, then the session's points as their
    own record — because a card handed no session runs nothing and ends at once, which
    made this check a race against the child's start-up.
    """
    import pickle
    import signal
    import subprocess
    import sys
    import time

    from kanso.nautilus.backtest import _BOOTSTRAP, window_data

    request = request_for(source=SLOW_SLEEVE)
    instruments, groups = window_data(request, store)
    handed = tmp_path / "request.pkl"
    with handed.open("wb") as handle:
        pickle.dump({"extensions": []}, handle)
        pickle.dump({"request": request.plain(), "instruments": instruments}, handle)
        pickle.dump({"groups": groups}, handle)  # one session, as the parent stages it
    starts_a_card = (
        "import os, subprocess, sys, time\n"
        f"card = subprocess.Popen([sys.executable, '-c', {_BOOTSTRAP!r}, {str(handed)!r},"
        f" {str(tmp_path / 'result.pkl')!r}, str(os.getpid())], start_new_session=True,"
        f" cwd={str(lane)!r})\n"
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


def test_the_window_is_released_before_the_card_is_supervised(
    store: Path, lane: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child holds its own copy of the points for the whole card; the parent, which read
    them only to write the payload, holds none of them while it waits. Measured before this:
    a lane kept 1.4 GB of an eighteen-month five-second window alive beside a 2.7 GB card."""
    import sys

    from kanso.nautilus import backtest as runner

    read = runner._window_points
    window: list[object] = []

    def reading(*args: object) -> object:
        groups, loaded = read(*args)  # type: ignore[arg-type]
        window.extend(groups)
        return groups, loaded

    supervised = runner._supervised

    def checking(request: object, room: Path, workdir: Path) -> object:
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
        assert holding == [], f"the parent still holds the window while the card runs: {holding}"
        return supervised(request, room, workdir)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_window_points", reading)
    monkeypatch.setattr(runner, "_supervised", checking)

    request = request_for()
    carded = run_subprocess(request, store, lane)

    assert not carded.crashed, carded.traceback_tail
    reads = len(window)
    _, groups = runner.window_data(request, store)
    sessions = {day_of(int(p.ts_init)) for g in groups for p in g}
    assert reads == len(sessions), "the window was read once, a session at a time"
