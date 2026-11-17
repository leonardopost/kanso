"""Looks at a forward window, struck from the reads recorded of it, and judging nothing.

The forward window is the one nothing may backtest, and every act that shows someone a day of
it is a read of it. What is under test is who counts — the connected component of
`derived_from`, as for the certification looks — which reads count — any that is not exempt
and shares a day with the window, or whose days cannot be read — and how they are grouped into
looks: every read of one version is that version's one look, every read of one replay that
replay's one look, placed where its first read was taken.

The workspace is the replay suite's synthetic saw-tooth, whose catalog reaches through March so
its forward window has data. Reads are written by the writer, `record_read`, from reads built
here where only the counting is under test.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kanso.certify import looks as looks_module
from kanso.certify.looks import (
    EXEMPT_STAGE,
    FORWARD_READ,
    REPLAY,
    STAGE,
    ForwardRead,
    forward_looks,
    reads,
    record_read,
)
from kanso.hyp import add as register
from kanso.state import Event, StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import (
    FORWARD,
    FORWARD_START,
    HYP_ID,
    document,
    prepared,
    store,
    write_hypothesis,
    ws,
)
from tests.replay.test_run import _reachable

__all__ = ["prepared", "store", "ws"]

CHILD, STRANGER = "demo_child", "demo_x"

VERSION = f"{HYP_ID}@1"
"""The demo sleeve's first composed version, as a stage read names it."""


def registered(ws: Workspace, store: StateStore, hyp_id: str, *parents: str) -> str:
    """A hypothesis registered beside the demo, naming `parents` as what it derives from."""
    doc = document(id=hyp_id, derived_from=list(parents))
    return register(ws, store, write_hypothesis(ws, doc, hyp_id=hyp_id)).id


def read(
    hyp_id: str,
    of: str,
    first: date,
    last: date,
    *,
    via: str = REPLAY,
    exempt: str | None = None,
    shown: tuple[date, date] | None = None,
) -> ForwardRead:
    """A read of `hyp_id`'s forward window from `first` to `last`, judged against the demo's."""
    return ForwardRead(
        hyp_id=hyp_id,
        via=via,
        of=of,
        target=of if via == STAGE else f"{hyp_id}@abcdef0",
        where="paper" if via == STAGE else "node",
        session=f"session-{of}",
        forward_start=FORWARD_START,
        first=first,
        last=last,
        shown=shown,
        exempt=exempt,
    )


# --- which reads count --------------------------------------------------------


def test_a_look_counts_at_a_forward_window_it_shares_a_day_with_and_not_at_one_opening_after_it(
    ws: Workspace, store: StateStore
) -> None:
    """A read through 31 March is a look at a window opening on or before that day, and a
    re-pin onto a window opening on 1 April starts a fresh count, as for certification
    windows."""
    registered(ws, store, HYP_ID)
    record_read(store, read(HYP_ID, "replay-1", *FORWARD))

    for start in (date(2024, 2, 1), FORWARD_START, FORWARD[1]):
        assert [look.of for look in forward_looks(store, HYP_ID, start).taken] == ["replay-1"]
    assert forward_looks(store, HYP_ID, date(2024, 4, 1)).taken == ()


def test_the_lineage_is_struck_when_counted_so_a_re_pin_that_drops_derived_from_splits_it(
    ws: Workspace, store: StateStore
) -> None:
    """A child's replay is a look of its parent's lineage while `derived_from` joins them, and
    of its own alone once a re-pin drops the link; a hypothesis joined to neither counts
    neither."""
    registered(ws, store, HYP_ID)
    registered(ws, store, CHILD, HYP_ID)
    registered(ws, store, STRANGER)
    record_read(store, read(CHILD, "replay-child", *FORWARD))
    record_read(store, read(STRANGER, "replay-stranger", *FORWARD))

    joined = forward_looks(store, HYP_ID, FORWARD_START)
    assert joined.lineage == (HYP_ID, CHILD)
    assert [look.of for look in joined.taken] == ["replay-child"]

    registered(ws, store, CHILD)

    split = forward_looks(store, HYP_ID, FORWARD_START)
    assert split.lineage == (HYP_ID,)
    assert split.taken == ()
    assert [look.of for look in forward_looks(store, CHILD, FORWARD_START).taken] == [
        "replay-child"
    ]


def test_a_read_whose_days_do_not_parse_counts_at_every_forward_window(
    ws: Workspace, store: StateStore
) -> None:
    """A read whose last day cannot be read cannot be shown to have read elsewhere."""
    registered(ws, store, HYP_ID)
    store.event(FORWARD_READ, HYP_ID, {"via": REPLAY, "of": "replay-1", "to": "the 31st"})

    for start in (FORWARD_START, date(2099, 1, 1)):
        (look,) = forward_looks(store, HYP_ID, start).taken
        assert (look.of, look.first, look.last) == ("replay-1", None, None)


# --- how reads become looks ---------------------------------------------------


def test_looks_are_ordered_by_when_they_were_taken_and_a_version_keeps_its_place(
    ws: Workspace, store: StateStore
) -> None:
    """A version's first window is look 1; two replays after it are 2 and 3; its later window
    adds no look and leaves its place at 1, over the days both windows read. A read recorded
    exempt counts nothing."""
    registered(ws, store, HYP_ID)
    record_read(store, read(HYP_ID, VERSION, FORWARD_START, date(2024, 3, 10), via=STAGE))
    record_read(store, read(HYP_ID, "replay-1", *FORWARD))
    record_read(store, read(HYP_ID, "replay-2", *FORWARD, exempt=EXEMPT_STAGE))
    record_read(store, read(HYP_ID, "replay-3", *FORWARD))
    before = forward_looks(store, HYP_ID, FORWARD_START)
    record_read(store, read(HYP_ID, VERSION, date(2024, 3, 11), date(2024, 3, 20), via=STAGE))

    found = forward_looks(store, HYP_ID, FORWARD_START)

    assert [look.of for look in found.taken] == [VERSION, "replay-1", "replay-3"]
    assert found.place(VERSION) == before.place(VERSION) == 1
    assert found.place("replay-3") == 3
    assert found.place("replay-2") is None, "an exempt read is no look"
    assert len(found.taken) == len(before.taken) == 3
    version = found.taken[0]
    assert (version.via, version.first, version.last) == (STAGE, FORWARD_START, date(2024, 3, 20))
    assert version.at == reads(store, HYP_ID)[0].at


# --- the format ---------------------------------------------------------------

_DAYS = st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
_TEXT = st.text(st.characters(blacklist_categories=("Cs",)), max_size=12)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    made=st.builds(
        ForwardRead,
        hyp_id=st.sampled_from((HYP_ID, CHILD)),
        via=st.sampled_from((STAGE, REPLAY)),
        of=_TEXT,
        target=_TEXT,
        where=_TEXT,
        session=_TEXT,
        forward_start=st.none() | _DAYS,
        first=st.none() | _DAYS,
        last=st.none() | _DAYS,
        shown=st.none() | st.tuples(_DAYS, _DAYS).map(lambda days: tuple(sorted(days))),
        exempt=st.sampled_from((None, EXEMPT_STAGE)),
    )
)
def test_a_forward_read_round_trips_through_its_event(store: StateStore, made: ForwardRead) -> None:
    """What the writer records is what the counter reads back, its event's id and time added."""
    event_id = record_read(store, made)

    (back,) = [found for found in reads(store, made.hyp_id) if found.event_id == event_id]

    assert back == ForwardRead(**{**made.__dict__, "event_id": event_id, "at": back.at})
    assert back.at


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {
            "via": 7,
            "of": ["not", "text"],
            "target": None,
            "where": 1.5,
            "session": {},
            "forward_start": 20240301,
            "from": "first of March",
            "to": None,
            "shown": ["2024-03-01"],
            "exempt": "because",
        },
        {"via": "screen", "shown": ["2024-03-02", "2024-03-01"], "exempt": True},
        {"shown": "2024-03-01..2024-03-31", "from": "2024-03-01", "to": "2024-02-30"},
    ],
    ids=["empty", "other-types", "unknown-via", "unparsed-days"],
)
def test_a_forward_read_event_kanso_did_not_write_reads_without_raising(
    detail: dict[str, Any],
) -> None:
    """A detail missing every key, holding values of other types or days that do not parse
    reads as empty text, no day and no `shown`: it counts at every start, as a replay's look of
    its own, and exempts nothing — the monitor catches only kanso's own errors, so one corrupt
    event would otherwise end its whole pass."""
    found = ForwardRead.of_event(
        Event(event_id=9, ts="then", kind=FORWARD_READ, subject=HYP_ID, detail=detail)
    )

    assert found.via == REPLAY
    assert (found.of, found.target, found.where, found.session) == ("", "", "", "")
    assert found.forward_start is None and found.last is None
    assert found.shown is None and found.exempt is None
    assert (found.hyp_id, found.event_id, found.at) == (HYP_ID, 9, "then")


def test_reads_kanso_did_not_write_are_each_a_look_of_their_own(
    ws: Workspace, store: StateStore
) -> None:
    """Two reads that name nothing they were of are two looks, never one."""
    registered(ws, store, HYP_ID)
    store.event(FORWARD_READ, HYP_ID, {})
    store.event(FORWARD_READ, HYP_ID, {"of": 3})

    assert len(forward_looks(store, HYP_ID, FORWARD_START).taken) == 2


# --- what the counter can reach -----------------------------------------------


def test_the_look_counter_reaches_neither_the_stage_record_nor_replay() -> None:
    """The research loop reaches the counter through the planner, and the stage's record
    reaches replay, so the counter reads its own events and imports neither: a guard, which
    holds before the forward reads were counted as after."""
    reachable = _reachable(looks_module.__name__)

    assert "kanso.portfolio.records" not in reachable
    assert not any(name.startswith("kanso.replay") for name in reachable)
