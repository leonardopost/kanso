"""The card path: a child of its own, an environment allow-list, and a supervisor."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

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
    store: Path, lane: Path, request_for
) -> None:
    """The child leads its own session; only the watcher can kill it with the lane."""
    import threading

    from kanso.errors import PreconditionError
    from kanso.nautilus import backtest as runner

    timer = threading.Timer(0.3, runner.interrupt)
    timer.start()
    try:
        with pytest.raises(PreconditionError, match="the card was interrupted") as failure:
            run_subprocess(request_for(source=SLOW_SLEEVE), store, lane)
    finally:
        timer.cancel()
        runner.resume()

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

    read = runner.window_data

    def reading(*args: object) -> object:
        runner.interrupt()
        return read(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "window_data", reading)
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
    would spend far longer than the wait below in `on_start` alone.
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
    handed.write_bytes(
        pickle.dumps(
            {"request": request.plain(), "instruments": list(instruments), "groups": list(groups)}
        )
    )
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
