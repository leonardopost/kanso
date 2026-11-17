"""Looks at a forward window, struck from the reads recorded of it, and judging nothing.

The forward window is the one nothing may backtest, and every act that shows someone a day of
it is a read of it. What is under test is who counts — the connected component of
`derived_from`, as for the certification looks — which reads count — any that is not exempt
and shares a day with the window, or whose days cannot be read — and how they are grouped into
looks: every read of one version is that version's one look, every read of one replay that
replay's one look, placed where its first read was taken.

Then who reads: a replay whose range reaches its target's forward window — one look for a
parity's two paths, recorded as soon as its session is on disk, and none for a replay refused
before it wrote one — and the one exemption: a parity of a version over days a stage already
showed it, which excludes the day a version joined a running stage, the days past where the
catalog served a version's series when its window ran, a window that halted, a `replay run`, a
card and a day past the stage's.

The workspace is the replay suite's synthetic saw-tooth, whose catalog reaches through March so
its forward window has data. Reads are written through the real writers — a replay, a parity, a
stage's deploy — and by `record_read` from reads built here where only the counting is under
test.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kanso import replay
from kanso.certify import looks as looks_module
from kanso.certify.looks import (
    EXEMPT_STAGE,
    FORWARD_READ,
    REPLAY,
    STAGE,
    ForwardRead,
    forward_looks,
    read_of,
    reads,
    record_read,
)
from kanso.data import catalog, snapshot
from kanso.data.instruments import resolve_universe
from kanso.errors import PreconditionError
from kanso.hyp import add as register
from kanso.portfolio import deploy
from kanso.replay import record
from kanso.state import Event, StateStore
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable
from tests.replay.conftest import (
    BLOCKING_FILTER,
    CERTIFICATION,
    FORWARD,
    FORWARD_START,
    HYP_ID,
    INSTRUMENT,
    RAISING,
    RESEARCH,
    SPAN,
    bars,
    carded,
    composed,
    dataset,
    document,
    prepared,
    store,
    write_hypothesis,
    write_instruments,
    ws,
)
from tests.replay.test_run import _reachable

__all__ = ["prepared", "store", "ws"]

CHILD, STRANGER = "demo_child", "demo_x"

VERSION = f"{HYP_ID}@1"
"""The demo sleeve's first composed version, as a stage read names it."""

APRIL = (date(2024, 4, 1), date(2024, 4, 30))
"""A month past the forward window's data: what a stage resumed past its clock replays."""

LAGGING = "LAGG.XNAS"
SYNCED_TO = date(2024, 3, 20)
"""Where a sync that lagged the demo's left a second universe: eleven days short of its end."""


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


def april(ws: Workspace) -> None:
    """The catalog grown by a month, as a `kanso data sync` after the stage ran would grow it."""
    catalog.write(ws, bars(APRIL), ref=dataset(span=APRIL), source="synthetic")


def lagging(ws: Workspace, store: StateStore) -> None:
    """A version beside the demo's whose universe the catalog serves only to `SYNCED_TO`, as a
    sync that lagged the demo's leaves it, composed and ready for the paper stage."""
    write_instruments(ws, ids=(INSTRUMENT, LAGGING))
    resolve_universe(ws, [LAGGING], RESEARCH[0])
    held = (SPAN[0], SYNCED_TO)
    catalog.write(ws, bars(held, "LAGG"), ref=dataset(LAGGING, held), source="synthetic")
    snapshot.freeze(ws)
    deployable(ws, store, "lagging", doc=document(id="lagging", universe=[LAGGING]))


def looks_of(store: StateStore, hyp_id: str = HYP_ID) -> list[str]:
    """What each look the lineage took at the demo's forward window was of, oldest first."""
    return [look.of for look in forward_looks(store, hyp_id, FORWARD_START).taken]


def the_read(store: StateStore, session_id: str) -> ForwardRead:
    """The read a replay's session made, which it must have made."""
    found = read_of(store, session_id)
    assert found is not None, f"{session_id} recorded no read"
    return found


# --- a replay reads the forward window ----------------------------------------


def test_a_replay_that_reads_the_forward_window_is_a_look_of_its_lineage(
    ws: Workspace, store: StateStore
) -> None:
    """A bare replay runs from the forward start, so it is a look of its hypothesis's lineage
    there — a child's count includes it — and of no hypothesis outside it."""
    carded(ws, store)
    registered(ws, store, CHILD, HYP_ID)
    registered(ws, store, STRANGER)

    session = replay.run(ws, store, hyp=HYP_ID)

    (made,) = reads(store, HYP_ID)
    assert (made.via, made.of, made.where, made.session) == (
        REPLAY,
        session.session_id,
        "node",
        session.session_id,
    )
    assert made.target == session.target
    assert (made.forward_start, made.first, made.last) == (FORWARD_START, *FORWARD)
    assert made.exempt is None and made.shown is None
    assert looks_of(store, CHILD) == looks_of(store) == [session.session_id]
    assert looks_of(store, STRANGER) == []


def test_a_replay_that_ends_before_the_forward_window_reads_none(
    ws: Workspace, store: StateStore
) -> None:
    """A replay of the certification window, and a sleeve's certification, whose parity replays
    that window, read no forward day; the same target replayed through the forward start reads
    exactly that one."""
    from kanso.certify.run import certify
    from tests.certify.test_run import a_card, write_plan
    from tests.research.conftest import DOCUMENT as RESEARCHED
    from tests.research.conftest import REVERTING as RESEARCHED_SLEEVE
    from tests.research.conftest import classify

    classify(ws, store, RESEARCHED, RESEARCHED_SLEEVE)
    a_card(ws, store, RESEARCHED_SLEEVE)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)
    certification = replay.run(ws, store, hyp=HYP_ID, start=CERTIFICATION[0], end=CERTIFICATION[1])

    (parity,) = [gate for gate in made.gates if gate.id == "parity_replay"]
    assert parity.skipped is None, "the certification ran both paths"
    assert read_of(store, str(parity.evidence["node"])) is None
    assert read_of(store, certification.session_id) is None
    assert [found for found in reads(store, HYP_ID) if found.via == REPLAY] == []

    reaching = replay.run(ws, store, hyp=HYP_ID, start=CERTIFICATION[0], end=FORWARD_START)

    (one,) = [found for found in reads(store, HYP_ID) if found.via == REPLAY]
    assert (one.of, one.first, one.last) == (reaching.session_id, FORWARD_START, FORWARD_START)


def test_a_replay_from_before_the_forward_start_reads_from_it(
    ws: Workspace, store: StateStore
) -> None:
    """Only the forward days are the read, so a span starting in the certification window is
    the days from the forward start on."""
    carded(ws, store)

    session = replay.run(ws, store, hyp=HYP_ID, start=CERTIFICATION[0], end=date(2024, 3, 10))

    found = the_read(store, session.session_id)
    assert (found.first, found.last) == (FORWARD_START, date(2024, 3, 10))
    assert session.from_ == CERTIFICATION[0], "the session ran the whole range"


def test_parity_is_one_look_for_both_paths(ws: Workspace, store: StateStore) -> None:
    """Two sessions, one read, of the node session."""
    carded(ws, store)

    found = replay.parity(ws, store, hyp=HYP_ID)

    (made,) = reads(store, HYP_ID)
    assert (made.of, made.session, made.where) == (found.node, found.node, "parity")
    assert read_of(store, found.engine) is None
    assert looks_of(store) == [found.node]


def test_a_parity_whose_engine_path_fails_has_still_read_the_window(
    ws: Workspace, store: StateStore
) -> None:
    """A strategy that raises stops the node path and raises on the engine path, after the node
    session was written: that session can be read, so its read stands."""
    carded(ws, store, strategy=RAISING, doc=document(id="demo_raise"))

    with pytest.raises(RuntimeError, match="the replay asked for the impossible"):
        replay.parity(ws, store, hyp="demo_raise")

    (node,) = replay.sessions(ws)
    assert node.mode == "node"
    assert the_read(store, node.session_id).where == "parity"
    assert looks_of(store, "demo_raise") == [node.session_id]


def test_a_refused_replay_reads_nothing(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window refused after three days were released wrote no session and records no read;
    the same range, not refused, records one."""
    from kanso.nautilus import backtest

    carded(ws, store)
    read_points = backtest._window_points
    calls: list[int] = []

    def refusing(*args: object) -> object:
        calls.append(1)
        if len(calls) > 3:
            raise PreconditionError("the fourth day is not wanted", remedy="run nothing")
        return read_points(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(backtest, "_window_points", refusing)
    for mode in replay.MODES:
        calls.clear()
        with pytest.raises(PreconditionError, match="the fourth day is not wanted"):
            replay.run(ws, store, hyp=HYP_ID, mode=mode)
    calls.clear()
    with pytest.raises(PreconditionError, match="the fourth day is not wanted"):
        replay.parity(ws, store, hyp=HYP_ID)

    assert replay.sessions(ws) == []
    assert store.events(kind=FORWARD_READ) == []

    monkeypatch.setattr(backtest, "_window_points", read_points)
    session = replay.run(ws, store, hyp=HYP_ID)

    assert [found.of for found in reads(store, HYP_ID)] == [session.session_id]


def test_a_session_on_disk_is_never_uncounted(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that fails as the session's row is written leaves the session's files on disk,
    where `kanso replay show` reads them, and its read already in the store — for a replay and
    for a parity's node path alike."""
    carded(ws, store)

    def failing(*_: object, **__: object) -> object:
        raise RuntimeError("the store went away")

    monkeypatch.setattr(record, "insert", failing)
    for attempt in (
        lambda: replay.run(ws, store, hyp=HYP_ID),
        lambda: replay.parity(ws, store, hyp=HYP_ID),
    ):
        with pytest.raises(RuntimeError, match="the store went away"):
            attempt()

    on_disk = [session.session_id for session in replay.sessions(ws)]
    assert len(on_disk) == 2
    assert [found.of for found in reads(store, HYP_ID)] == on_disk
    assert [found.where for found in reads(store, HYP_ID)] == ["node", "parity"]


# --- the one exemption --------------------------------------------------------


def test_parity_of_a_version_inside_its_stage_days_is_recorded_exempt_and_is_no_look(
    ws: Workspace, store: StateStore
) -> None:
    """Parity of a version over the days the paper stage showed it reads nothing the stage's
    own look had not: its read is recorded, exempt, and the count does not move."""
    deployable(ws, store)
    deploy(ws, store, "paper")
    before = looks_of(store)

    found = replay.parity(ws, store, strategy=HYP_ID, version=1)

    made = the_read(store, found.node)
    assert (made.exempt, made.target, made.first, made.last) == (EXEMPT_STAGE, VERSION, *FORWARD)
    assert looks_of(store) == before == [VERSION]


def test_parity_reaching_one_day_past_its_stage_days_is_a_look(
    ws: Workspace, store: StateStore
) -> None:
    """The stage showed the version through 31 March; a parity through 1 April reads a day it
    did not."""
    deployable(ws, store)
    deploy(ws, store, "paper")
    april(ws)

    found = replay.parity(ws, store, strategy=HYP_ID, end=APRIL[0])

    assert the_read(store, found.node).exempt is None
    assert looks_of(store) == [VERSION, found.node]


def test_parity_over_the_day_a_version_joined_a_running_stage_is_a_look(
    ws: Workspace, store: StateStore
) -> None:
    """A version that joined the stage after it resumed past its clock on 31 March was shown
    only the points after the clock that day: its parity over that day is a look, and from the
    day after it is exempt. The version already on the stage had that day in its first window,
    so its two windows join and its parity over both is exempt."""
    deployable(ws, store)
    deploy(ws, store, "paper")
    april(ws)
    deployable(ws, store, "late", doc=document(id="late"))
    deploy(ws, store, "paper")

    (joined,) = reads(store, "late")
    assert (joined.first, joined.shown) == (FORWARD[1], APRIL)

    over_the_day = replay.parity(ws, store, strategy="late", start=FORWARD[1])
    after_it = replay.parity(ws, store, strategy="late", start=APRIL[0])
    both = replay.parity(ws, store, strategy=HYP_ID)

    assert the_read(store, over_the_day.node).exempt is None
    assert the_read(store, after_it.node).exempt == EXEMPT_STAGE
    assert the_read(store, both.node).first == FORWARD_START
    assert the_read(store, both.node).exempt == EXEMPT_STAGE
    assert looks_of(store, "late") == ["late@1", over_the_day.node]
    assert looks_of(store) == [VERSION]


def test_parity_over_days_a_lagging_series_was_synced_into_after_the_stage_passed_is_a_look(
    ws: Workspace, store: StateStore
) -> None:
    """The window closes on 31 March, where the demo's data ends, but the version beside it was
    served only to the 20th: it was shown its decisions to the 20th and no further. Once a sync
    fills the 21st to the 31st, the next window releases nothing at or before the stage's clock,
    so the stage never shows them, and a parity over them is a look; one to the 20th is not."""
    deployable(ws, store)
    lagging(ws, store)
    deploy(ws, store, "paper")

    (behind,) = reads(store, "lagging")
    assert (behind.first, behind.last) == FORWARD, "the window read every day it was marked"
    assert behind.shown == (FORWARD_START, SYNCED_TO)
    assert [found.shown for found in reads(store, HYP_ID)] == [FORWARD]

    synced = (date(2024, 3, 21), FORWARD[1])
    catalog.write(ws, bars(synced, "LAGG"), ref=dataset(LAGGING, synced), source="synthetic")
    deploy(ws, store, "paper")

    assert [found.shown for found in reads(store, "lagging")] == [behind.shown, None]
    caught_up = replay.parity(ws, store, strategy="lagging", end=FORWARD[1])
    shown = replay.parity(ws, store, strategy="lagging", end=SYNCED_TO)

    assert the_read(store, caught_up.node).exempt is None
    assert the_read(store, shown.node).exempt == EXEMPT_STAGE
    assert looks_of(store, "lagging") == ["lagging@1", caught_up.node]


def test_parity_over_a_window_that_halted_is_a_look(ws: Workspace, store: StateStore) -> None:
    """A window that halted stopped releasing the version's decisions at the halt, so it showed
    none of its days, though the window is the version's look: a parity over them is another."""
    deployable(ws, store, "boomer", sleeve=RAISING, doc=document(id="boomer"))
    made = deploy(ws, store, "paper")
    assert made.halted is not None

    (halted,) = reads(store, "boomer")
    assert halted.shown is None
    assert looks_of(store, "boomer") == ["boomer@1"]

    with pytest.raises(RuntimeError, match="the replay asked for the impossible"):
        replay.parity(ws, store, strategy="boomer")

    (node,) = [session for session in replay.sessions(ws) if session.mode == replay.NODE]
    assert the_read(store, node.session_id).exempt is None
    assert looks_of(store, "boomer") == ["boomer@1", node.session_id]


def test_a_replay_run_of_a_version_over_its_stage_days_is_a_look(
    ws: Workspace, store: StateStore
) -> None:
    """The exemption is parity's alone: a `replay run` over the same days is a look."""
    deployable(ws, store)
    deploy(ws, store, "paper")

    session = replay.run(ws, store, strategy=HYP_ID)

    assert the_read(store, session.session_id).exempt is None
    assert looks_of(store) == [VERSION, session.session_id]


def test_parity_of_a_card_is_a_look_whatever_version_shares_its_bytes(
    ws: Workspace, store: StateStore
) -> None:
    """The deployed version's sleeve is the card's bytes, and a stage runs versions, not cards."""
    deployable(ws, store)
    deploy(ws, store, "paper")

    found = replay.parity(ws, store, hyp=HYP_ID)

    assert the_read(store, found.node).exempt is None
    assert looks_of(store) == [VERSION, found.node]


def test_a_replay_of_an_attached_construct_s_card_is_a_look_of_its_host_sleeve_s_lineage(
    ws: Workspace, store: StateStore
) -> None:
    """The construct's card runs on its host version, so it shows the host sleeve's forward
    days, which `forward_holdout` judges the sleeve on: two reads of one session, one look in
    each lineage."""
    carded(ws, store)
    composed(ws, store, HYP_ID)
    construct = carded(
        ws,
        store,
        doc=document(
            id="demo_filter",
            construct={"id": "filter", "host": HYP_ID, "params": {"scope": "time"}},
            objective={"id": "marginal_wf_sharpe", "params": {"min_delta": 0.0, "k_se": 0.5}},
        ),
        strategy=BLOCKING_FILTER,
        host_version=1,
    )

    session = replay.run(ws, store, hyp=construct)

    (own,) = reads(store, construct)
    (host,) = reads(store, HYP_ID)
    assert own.of == host.of == session.session_id
    assert (host.forward_start, host.first, host.last) == (FORWARD_START, *FORWARD)
    assert the_read(store, session.session_id) == own, "the target's own read comes first"
    assert looks_of(store, construct) == looks_of(store) == [session.session_id]


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


@pytest.mark.parametrize(
    ("windows", "start"),
    [
        ({"forward": {"start": "2024-03-01"}}, FORWARD_START),
        ({"forward": {"start": "2024-03-01T00:00:00Z"}}, FORWARD_START),
        ({"forward": {"start": "soon"}}, None),
        ({"forward": "2024-03-01"}, None),
        ({}, None),
    ],
    ids=["day", "midnight", "unparsed", "not-a-window", "none"],
)
def test_a_pinned_file_s_forward_start_is_read_as_the_schema_reads_it(
    store: StateStore, windows: dict[str, Any], start: date | None
) -> None:
    """A pin is read as a plain mapping, so one written under an older schema still reads, and
    one declaring no forward start that reads is `None` rather than an error."""
    import yaml

    pin = store.put_blob(yaml.safe_dump({"id": HYP_ID, "windows": windows}).encode("utf-8"))

    assert looks_module.forward_start(store, pin) == start
    assert looks_module.forward_start(store, None) is None


# --- what the counter can reach -----------------------------------------------


def test_the_look_counter_reaches_neither_the_stage_record_nor_replay() -> None:
    """The research loop reaches the counter through the planner, and the stage's record
    reaches replay, so the counter reads its own events and imports neither: a guard, which
    holds before the forward reads were counted as after."""
    reachable = _reachable(looks_module.__name__)

    assert "kanso.portfolio.records" not in reachable
    assert not any(name.startswith("kanso.replay") for name in reachable)
