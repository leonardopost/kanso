"""The warmup prefix on the runner: resolved in the parent, delivered whole, measured after.

The window's own bounds are what they were; only the data fed before it widens. Every test
runs the real engine over real parquet, because the claims are about what the venue saw
before the open and what the extraction did with it.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from kanso.criteria import Fill
from kanso.criteria.run import midnight_ns
from kanso.data.types import CorporateAction
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.backtest import (
    _equity,
    execute,
    run,
    warmup_prefix,
    window_data,
)
from kanso.schemas import Hypothesis, Warmup

from .conftest import (
    CAPITAL,
    CERTIFICATION,
    INSTRUMENT,
    RESEARCH,
    SLEEVE,
    bars,
    catalog,
    hypothesis,
    instrument,
)

SESSIONS = 5
DECEMBER = (date(2023, 12, 1), date(2023, 12, 31))
PREFIX = (date(2023, 12, 27), date(2023, 12, 31))
"""The last five calendar days of December: what five sessions resolve to over a series
that prints every day."""

OTHER = "OTHR.XNAS"


def warmed(sessions: int = SESSIONS, **changes: object) -> Hypothesis:
    """The demo hypothesis, warming on this many sessions."""
    return hypothesis(**changes).model_copy(  # type: ignore[arg-type]
        update={"warmup": Warmup(sessions=sessions)}
    )


@pytest.fixture
def deep(tmp_path: Path) -> Path:
    """A catalog holding December as well as both windows, so a prefix can be resolved."""
    points = [*bars(DECEMBER), *bars(RESEARCH), *bars(CERTIFICATION)]
    return catalog(tmp_path / "deep", points, [instrument()])


# --- resolving the prefix -----------------------------------------------------


def test_a_hypothesis_without_a_warmup_resolves_no_prefix(deep: Path) -> None:
    assert warmup_prefix(hypothesis(), RESEARCH, deep) is None


def test_the_prefix_is_the_last_n_sessions_before_the_window(deep: Path) -> None:
    assert warmup_prefix(warmed(), RESEARCH, deep) == PREFIX


def test_a_session_is_a_day_that_printed_and_a_gap_is_not_one(tmp_path: Path) -> None:
    """Six sessions over a series with a three-day hole reach back past the hole."""
    printed = [
        *bars((date(2023, 12, 20), date(2023, 12, 24))),
        *bars((date(2023, 12, 28), date(2023, 12, 31))),
    ]
    store = catalog(tmp_path / "gapped", [*printed, *bars(RESEARCH)], [instrument()])

    assert warmup_prefix(warmed(6), RESEARCH, store) == (date(2023, 12, 23), date(2023, 12, 31))


def test_the_lookback_doubles_until_the_sessions_are_found(tmp_path: Path) -> None:
    """Two sessions asked for; the last two prints are twenty days back, past 2N and 4N."""
    far = bars((date(2023, 12, 10), date(2023, 12, 11)))
    store = catalog(tmp_path / "far", [*far, *bars(RESEARCH)], [instrument()])

    assert warmup_prefix(warmed(2), RESEARCH, store) == (date(2023, 12, 10), date(2023, 12, 11))


def test_too_few_sessions_inside_the_bound_is_a_refusal_naming_what_was_found(
    tmp_path: Path,
) -> None:
    store = catalog(
        tmp_path / "shallow",
        [*bars((date(2023, 12, 29), date(2023, 12, 31))), *bars(RESEARCH)],
        [instrument()],
    )

    with pytest.raises(PreconditionError) as refused:
        warmup_prefix(warmed(), RESEARCH, store)

    assert (
        "asks for 5 session(s) before 2024-01-01 and the catalog holds 3" in refused.value.message
    )
    assert "in the 160 calendar days before it" in refused.value.message
    assert "back to at least 2023-07-25" in (refused.value.remedy or "")
    assert "lower warmup.sessions" in (refused.value.remedy or "")


def test_a_clock_resolves_the_sessions_at_or_before_it(deep: Path) -> None:
    """A stage restart warms on what it replayed: the day its clock stands in counts once
    that day has printed, and not before."""
    from .conftest import CLOSE_NS, SECOND_NS

    tenth = midnight_ns(date(2024, 1, 10))
    published = tenth + CLOSE_NS + SECOND_NS

    before_the_print = warmup_prefix(warmed(3), RESEARCH, deep, until_ns=published - 1)
    at_the_print = warmup_prefix(warmed(3), RESEARCH, deep, until_ns=published)

    assert before_the_print == (date(2024, 1, 7), date(2024, 1, 9))
    assert at_the_print == (date(2024, 1, 8), date(2024, 1, 10))


def test_too_few_sessions_before_a_clock_names_the_clock(tmp_path: Path) -> None:
    store = catalog(tmp_path / "shallow", bars(RESEARCH), [instrument()])
    clock = midnight_ns(date(2024, 1, 3))

    with pytest.raises(PreconditionError) as refused:
        warmup_prefix(warmed(), RESEARCH, store, until_ns=clock)

    assert f"session(s) at or before 2024-01-03 ({clock})" in refused.value.message
    assert "the catalog holds 2" in refused.value.message


def test_an_unresolved_name_is_refused_before_any_lookback(deep: Path) -> None:
    with pytest.raises(PreconditionError, match="holds no definition for OTHR.XNAS"):
        warmup_prefix(warmed(universe=[INSTRUMENT, OTHER]), RESEARCH, deep)


def test_sessions_are_counted_on_quotes_when_the_hypothesis_requires_no_bars(
    tmp_path: Path,
) -> None:
    from .conftest import quotes

    store = catalog(tmp_path / "quoted", [*quotes(DECEMBER), *quotes(RESEARCH)], [instrument()])
    quoted = Hypothesis.model_validate(
        {
            **hypothesis().model_dump(by_alias=True, mode="json"),
            "resolution": "quote",
            "data_requirements": ["quote"],
            "warmup": {"sessions": 3},
        }
    )

    assert warmup_prefix(quoted, RESEARCH, store) == (date(2023, 12, 29), date(2023, 12, 31))


# --- the request ---------------------------------------------------------------


def test_a_prefix_ends_before_the_window_opens_or_on_the_day_it_opens(request_for) -> None:
    """A stage restart's window opens on its clock's day, and the prefix may end there."""
    with pytest.raises(ValidationError, match="is not a span of sessions before the window"):
        request_for(RESEARCH, prefix=(date(2023, 12, 27), date(2024, 1, 2)))
    with pytest.raises(ValidationError, match="is not a span of sessions before the window"):
        request_for(RESEARCH, prefix=(date(2023, 12, 31), date(2023, 12, 27)))

    restarted = request_for(RESEARCH, prefix=(date(2023, 12, 30), date(2024, 1, 1)))

    assert restarted.span == (date(2023, 12, 30), RESEARCH[1])
    assert restarted.bounds == request_for(RESEARCH).bounds


def test_the_delivered_span_widens_the_lower_bound_and_only_that(request_for) -> None:
    cold = request_for(RESEARCH)
    warm = request_for(RESEARCH, prefix=PREFIX)

    assert warm.bounds == cold.bounds
    assert warm.span == (PREFIX[0], RESEARCH[1])
    assert warm.delivered == (midnight_ns(PREFIX[0]), cold.bounds[1])
    assert cold.delivered == cold.bounds


# --- loading and checking ------------------------------------------------------


def test_the_prefix_is_loaded_with_the_window_and_nothing_before_it(
    deep: Path, request_for
) -> None:
    _, groups = window_data(request_for(RESEARCH, prefix=PREFIX), deep)

    assert sum(len(group) for group in groups) == 31 + SESSIONS
    first = min(int(point.ts_init) for group in groups for point in group)
    assert first >= midnight_ns(PREFIX[0])


def test_a_point_after_the_window_is_still_refused_with_a_prefix_declared(
    deep: Path, request_for
) -> None:
    """The embargo: only the lower bound widens."""
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, _ = window_data(request, deep)

    with pytest.raises(PreconditionError, match="lies outside the requested window"):
        execute(request, instruments, [tuple(bars(CERTIFICATION))])


def test_a_point_before_the_prefix_is_refused_too(deep: Path, request_for) -> None:
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, _ = window_data(request, deep)

    with pytest.raises(PreconditionError, match="lies outside the requested window 2023-12-27"):
        execute(request, instruments, [tuple(bars(DECEMBER))])


def test_a_prefix_with_nothing_measured_after_it_is_not_a_run(deep: Path, request_for) -> None:
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, _ = window_data(request, deep)

    with pytest.raises(PreconditionError, match="holds nothing for demo_mr over 2024-01-01"):
        execute(request, instruments, [tuple(bars(PREFIX))])


def test_an_unscheduled_split_inside_the_prefix_is_refused(deep: Path, request_for) -> None:
    """A split adjusts no position in the prefix, but the venue restates the book at it."""
    ex = midnight_ns(date(2023, 12, 29))
    split = CorporateAction(
        instrument_id=instrument().id,
        kind="split",
        ratio=0.1,
        cash=0.0,
        currency="USD",
        ex_date_ns=ex,
        ts_event=ex,
        ts_init=ex,
    )
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, groups = window_data(request, deep)

    with pytest.raises(PreconditionError, match="split effective 2023-12-29"):
        execute(request, instruments, [*groups, (split,)])


def test_the_parent_refuses_a_split_inside_the_prefix_before_any_child_runs(
    deep: Path, tmp_path: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child would refuse it too, but as a crash the run records; the parent's own
    check over the delivered span is what makes it a refusal the operator reads."""
    from kanso.nautilus import backtest

    ex = midnight_ns(date(2023, 12, 29))
    split = CorporateAction(
        instrument_id=instrument().id,
        kind="split",
        ratio=0.1,
        cash=0.0,
        currency="USD",
        ex_date_ns=ex,
        ts_event=ex,
        ts_init=ex,
    )
    loaded = window_data(request_for(RESEARCH, prefix=PREFIX), deep)
    monkeypatch.setattr(
        backtest, "window_data", lambda request, catalog: (loaded[0], [*loaded[1], (split,)])
    )
    lane = tmp_path / "lane"
    lane.mkdir()

    with pytest.raises(PreconditionError, match="split effective 2023-12-29"):
        backtest.run_subprocess(request_for(RESEARCH, prefix=PREFIX), deep, lane)


# --- what the run measures -----------------------------------------------------


def test_the_strategy_is_fed_the_prefix_and_measured_from_the_open(deep: Path, request_for) -> None:
    cold = run(request_for(RESEARCH), deep)
    warm = run(request_for(RESEARCH, prefix=PREFIX), deep)
    opens, closes = warm.run.bounds

    assert warm.run.window == RESEARCH
    assert len(warm.run.period_ends_ns) == 31
    assert all(opens <= ts < closes for ts in warm.run.period_ends_ns)
    assert all(opens <= fill.ts_ns < closes for fill in warm.run.fills)
    # The demo sleeve enters every sixth bar it sees, counting from the first: cold, the
    # first is the window's first day; warmed on five sessions, it is the second.
    assert cold.intents[0][0] == bars(RESEARCH)[0].ts_event
    assert warm.intents[0][0] == bars(RESEARCH)[1].ts_event


def test_the_last_prefix_print_marks_the_first_measured_period(tmp_path: Path, request_for) -> None:
    """A name bought at the open off its prefix print is worth that print, not nothing."""
    source = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys the other name on the first bar of the window it sees, whatever printed."""

    config_cls = Config

    def on_start(self):
        self.bought = False

    def on_bar(self, bar):
        if not self.bought:
            self.bought = self.submit_entry("OTHR.XNAS", "BUY", notional=1_000.0) is not None
'''
    points = [*bars(PREFIX), *bars(RESEARCH), *bars(PREFIX, "OTHR")]
    store = catalog(tmp_path / "two", points, [instrument(), instrument("OTHR")])
    hyp = warmed(universe=[INSTRUMENT, OTHER])
    request = request_for(RESEARCH, source=source, hypothesis_=hyp, prefix=PREFIX)

    card = run(request, store).run
    (fill,) = card.fills

    assert fill.instrument_id == OTHER
    assert fill.ts_ns >= card.bounds[0], "filled at the open, against the prefix print"
    assert card.equity[0] == pytest.approx(CAPITAL - fill.cost)


def test_a_fill_before_the_open_is_refused_by_the_extraction(request_for) -> None:
    request = request_for(RESEARCH, prefix=PREFIX)
    early = Fill(
        ts_ns=midnight_ns(PREFIX[1]), instrument_id=INSTRUMENT, side="BUY", qty=1, px=10, cost=0
    )

    with pytest.raises(ValidationError, match="precedes the window opening"):
        _equity(request, [(midnight_ns(RESEARCH[0]), INSTRUMENT, 10.0, 9.0, 11.0)], [early], {})


def test_a_warmed_card_runs_in_its_child_on_the_span_it_was_handed(
    deep: Path, tmp_path: Path, request_for
) -> None:
    from kanso.nautilus.backtest import run_subprocess

    lane = tmp_path / "lane"
    lane.mkdir()

    result = run_subprocess(request_for(RESEARCH, prefix=PREFIX, source=SLEEVE), deep, lane)

    assert result.crashed is False
    assert len(result.run.period_ends_ns) == 31
    assert result.intents[0][0] == bars(RESEARCH)[1].ts_event


def test_the_prefix_does_not_reach_the_certification_window_either(
    deep: Path, tmp_path: Path, request_for
) -> None:
    """A warmed card still refuses the window that judges it."""
    from kanso.nautilus.backtest import run_subprocess

    lane = tmp_path / "lane"
    lane.mkdir()
    before = (CERTIFICATION[0] - timedelta(days=SESSIONS), CERTIFICATION[0] - timedelta(days=1))

    with pytest.raises(PreconditionError, match="certification window"):
        run_subprocess(request_for(CERTIFICATION, prefix=before), deep, lane)
