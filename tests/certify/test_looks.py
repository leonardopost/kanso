"""Looks at a certification window, counted over a lineage, recorded and judging nothing.

Every certificate reads its certification window, so every certificate is a look at it, and a
lineage that certifies again draws again from the one sample research never saw. What is
under test is who counts — the connected component of `derived_from`, siblings and
descendants included — which certificates count — any verdict, plan or engine, on a window
that intersects, that read a window at all, read off any spelling of a day the schema admits —
that the count a certificate records is the one standing when it was written, and that the
count moves no verdict: the certificate's correction, the certification child's precheck and
the lane's are held to the plan's own threshold at every look.

The workspace is the research suite's synthetic saw-tooth. A certificate a test needs only
as a prior look is written by `certificate.write` from a document built here, since what is
counted is the row it leaves; the certificates whose numbers are asserted are certified.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kanso.certify import certificate, run
from kanso.certify import looks as looks_module
from kanso.certify.looks import Look, certification_window, looks, read_no_window
from kanso.certify.run import certify, certify_at_stall, precheck
from kanso.cli import status
from kanso.data import snapshot as snapshots
from kanso.research import records
from kanso.schemas import Certificate, DateWindow, Holdout
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_precheck import TIGHT, best_with, plan_of, searched
from tests.certify.test_run import (
    CERT_GATES,
    SEARCHED,
    a_card,
    a_manifest,
    venue_model,
    write_plan,
)
from tests.research.conftest import CERTIFICATION, DOCUMENT, HYP_ID, REVERTING, classify

WINDOW = CERTIFICATION
"""The demo's certification window: 2024-02-06 to 2024-02-29."""

THEN = datetime(2024, 3, 1, tzinfo=UTC)

SPREAD = (14.0, -12.0)
"""Two trials thirteen either side of a best that scored 1.0: measured on the demo's
reverting best, a search whose deflated ratio comes to 0.924 — clear of the plan's 0.9."""

CHILD, SIBLING, GRANDCHILD, STRANGER = "demo_child", "demo_sibling", "demo_grandchild", "demo_x"


def derived(ws: Workspace, store: StateStore, hyp_id: str, *parents: str) -> str:
    """A hypothesis registered beside the demo, naming `parents` as what it derives from."""
    return classify(ws, store, {**DOCUMENT, "id": hyp_id, "derived_from": list(parents)})


def pinned(store: StateStore, window: tuple[date, date]) -> str:
    """A hypothesis file whose certification window is `window`, as a run would pin it."""
    document = {
        **DOCUMENT,
        "windows": {
            **DOCUMENT["windows"],
            "certification": {"start": window[0], "end": window[1]},
        },
    }
    return store.put_blob(yaml.safe_dump(document, sort_keys=False).encode("utf-8"))


def looked(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    seq: int,
    *,
    pin: str | None = "",
    verdict: str = "pass",
    plan_version: int = 1,
    engine: str = "1.231.0",
    inadmissible: bool = False,
) -> Certificate:
    """A certificate of `hyp_id`, written as certification writes one: under `pin` — the
    demo's own window where it is left empty, none at all where it is `None` — and, where
    `inadmissible`, struck on a snapshot nothing could be measured on."""
    source = f"# certified {hyp_id} {seq}\n".encode()
    evidence: dict[str, Any] = (
        {"snapshot_id": "s1", "inadmissible": ["nobody declared when its points became public"]}
        if inadmissible
        else {"certification": 1.0}
    )
    made = Certificate.model_validate(
        {
            "schema": 1,
            "hyp_id": hyp_id,
            "strategy_sha": sha256(source).hexdigest(),
            "hypothesis_sha": pinned(store, WINDOW) if pin == "" else pin,
            "nautilus_version": engine,
            "venue_model": venue_model(),
            "snapshot_id": "s1",
            "criteria_version": "0.1.0",
            "plan_version": plan_version,
            "construct": {"id": "sleeve"},
            "objective": {"id": "wf_sharpe_net", "value": 1.0, "se": 0.5},
            "gates": [
                {
                    "id": "embargoed_window",
                    "stage": "cert",
                    "evidence": evidence,
                    "pass": verdict == "pass" and not inadmissible,
                }
            ],
            "n_trials": 1,
            "verdict": "fail" if inadmissible else verdict,
            "created_at": THEN + timedelta(minutes=seq),
        }
    )
    certificate.write(ws, store, made, source)
    return made


# --- a lineage ----------------------------------------------------------------


def test_a_lineage_is_every_registered_hypothesis_derived_from_joins_either_way(
    ws: Workspace, store: StateStore
) -> None:
    """Parent, children, a grandchild and a sibling are one lineage whichever of them asks,
    nearest first and by id among equals; a hypothesis joined to none is a lineage of one."""
    classify(ws, store, DOCUMENT)
    derived(ws, store, CHILD, HYP_ID)
    derived(ws, store, SIBLING, HYP_ID)
    derived(ws, store, GRANDCHILD, CHILD)
    derived(ws, store, STRANGER)

    assert records.lineage(store, HYP_ID) == (HYP_ID, CHILD, SIBLING, GRANDCHILD)
    assert records.lineage(store, SIBLING) == (SIBLING, HYP_ID, CHILD, GRANDCHILD)
    assert records.lineage(store, GRANDCHILD) == (GRANDCHILD, CHILD, HYP_ID, SIBLING)
    assert records.lineage(store, STRANGER) == (STRANGER,)
    assert records.lineage(store, "nobody_here") == ("nobody_here",)
    assert records.family(store, SIBLING) == (SIBLING, HYP_ID), "ancestors alone price a search"


def test_a_lineage_that_names_itself_round_is_walked_once(ws: Workspace, store: StateStore) -> None:
    """The demo re-registered to derive from its own grandchild: a cycle, each member once."""
    classify(ws, store, DOCUMENT)
    derived(ws, store, CHILD, HYP_ID)
    derived(ws, store, GRANDCHILD, CHILD)
    classify(ws, store, {**DOCUMENT, "derived_from": [GRANDCHILD]})

    assert records.lineage(store, HYP_ID) == (HYP_ID, CHILD, GRANDCHILD)
    assert records.lineage(store, CHILD) == (CHILD, GRANDCHILD, HYP_ID)


# --- what a look is -----------------------------------------------------------


def test_every_certificate_of_the_lineage_at_the_window_is_a_look(
    ws: Workspace, store: StateStore
) -> None:
    """Pass or fail, under any plan or engine, of the hypothesis or of a sibling: each read
    the window. A hypothesis outside the lineage looked at its own data."""
    classify(ws, store, DOCUMENT)
    derived(ws, store, SIBLING, HYP_ID)
    derived(ws, store, STRANGER)
    looked(ws, store, HYP_ID, 1)
    looked(ws, store, HYP_ID, 2, verdict="fail", plan_version=2)
    looked(ws, store, SIBLING, 3, engine="9.9.9")
    looked(ws, store, STRANGER, 4)

    found = looks(store, HYP_ID, WINDOW)

    assert found.lineage == (HYP_ID, SIBLING)
    assert [(look.hyp_id, look.plan_version, look.verdict) for look in found.prior] == [
        (HYP_ID, 1, "pass"),
        (HYP_ID, 2, "fail"),
        (SIBLING, 1, "pass"),
    ]
    assert found.look == 4
    assert found.prior[0] == Look(
        hyp_id=HYP_ID,
        sha7=sha256(f"# certified {HYP_ID} 1\n".encode()).hexdigest()[:7],
        plan_version=1,
        created_at=(THEN + timedelta(minutes=1)).isoformat(),
        verdict="pass",
    )
    assert looks(store, SIBLING, WINDOW).look == 4, "a sibling's count is the lineage's"
    assert looks(store, STRANGER, WINDOW).look == 2


def test_a_window_that_shares_a_day_is_the_same_hold_out_and_one_wholly_after_is_not(
    ws: Workspace, store: StateStore
) -> None:
    """A re-pin that moves the window by a day keeps the count; one onto a window that
    starts after this one ends starts a new one."""
    classify(ws, store, DOCUMENT)
    shifted = (WINDOW[0] + timedelta(days=1), WINDOW[1] + timedelta(days=1))
    later = (WINDOW[1] + timedelta(days=1), WINDOW[1] + timedelta(days=30))
    looked(ws, store, HYP_ID, 1)
    looked(ws, store, HYP_ID, 2, pin=pinned(store, shifted))
    looked(ws, store, HYP_ID, 3, pin=pinned(store, later))

    assert looks(store, HYP_ID, WINDOW).look == 3
    assert looks(store, HYP_ID, shifted).look == 4, "shifted shares days with all three"
    assert looks(store, HYP_ID, later).look == 3, "later shares a day with shifted alone"
    assert (
        looks(store, HYP_ID, (later[1] + timedelta(days=1), later[1] + timedelta(days=9))).look == 1
    )


def test_a_certificate_that_read_no_window_is_no_look(ws: Workspace, store: StateStore) -> None:
    """On a snapshot that cannot support a claim every gate fails with the snapshot's
    reasons and no window is run."""
    classify(ws, store, DOCUMENT)
    looked(ws, store, HYP_ID, 1, inadmissible=True)

    assert looks(store, HYP_ID, WINDOW).look == 1
    assert read_no_window([{"inadmissible": ["why"], "snapshot_id": "s1"}])
    assert not read_no_window([{"inadmissible": ["why"]}, {"certification": 1.0}])


def test_a_certificate_that_named_no_pin_is_read_under_the_pin_its_hypothesis_has_now(
    ws: Workspace, store: StateStore
) -> None:
    """A row written before certificates recorded their pin is read under its hypothesis's
    file as it stands: here a file pinned to a later window, so it is not a look at this
    one, and a look at that."""
    classify(ws, store, DOCUMENT)
    later = (WINDOW[1] + timedelta(days=1), WINDOW[1] + timedelta(days=30))
    looked(ws, store, HYP_ID, 1, pin=None)
    store.connection.execute(
        "UPDATE hypotheses SET hypothesis_sha = ? WHERE hyp_id = ?", (pinned(store, later), HYP_ID)
    )

    assert looks(store, HYP_ID, WINDOW).look == 1
    assert looks(store, HYP_ID, later).look == 2


def test_a_certificate_whose_window_cannot_be_read_is_counted(
    ws: Workspace, store: StateStore
) -> None:
    """A pin the store does not hold, or a file that declares no certification window, cannot
    show that the certificate looked elsewhere."""
    classify(ws, store, DOCUMENT)
    looked(ws, store, HYP_ID, 1, pin="f" * 64)
    looked(ws, store, HYP_ID, 2, pin=store.put_blob(b"id: demo_mr\nwindows: {}\n"))

    assert looks(store, HYP_ID, WINDOW).look == 3
    assert certification_window(store, None) is None
    assert certification_window(store, "f" * 64) is None
    assert certification_window(store, store.put_blob(b"- a list\n")) is None
    assert certification_window(store, pinned(store, WINDOW)) == WINDOW


def test_the_looks_are_what_a_certificate_records_as_its_hold_out(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT)
    derived(ws, store, CHILD, HYP_ID)
    looked(ws, store, CHILD, 1)

    assert looks(store, HYP_ID, WINDOW).holdout() == Holdout(
        window_start=WINDOW[0], window_end=WINDOW[1], look=2, lineage=[HYP_ID, CHILD]
    )


# --- a window read as the schema reads it ----------------------------------


SPELLINGS = (
    lambda day: day.isoformat(),
    lambda day: repr(day.isoformat()),
    lambda day: f"{day.isoformat()} 00:00:00",
    lambda day: f"{day.isoformat()}T00:00:00",
    lambda day: f"{day.isoformat()}T00:00:00Z",
    lambda day: f"'{day.isoformat()}T00:00:00Z'",
    lambda day: f"'{day.isoformat()}T00:00:00'",
    lambda day: f"{day.isoformat()}T00:00:00+10:00",
    lambda day: str(int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())),
)
"""Every way of writing a day in YAML that `DateWindow` admits as that day: a date, quoted or
not; a midnight timestamp, with a `T` or a space, a zone or none, quoted or not; and the unix
seconds of its midnight."""


@settings(
    max_examples=60,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    start=st.dates(min_value=date(1971, 1, 1), max_value=date(2090, 1, 1)),
    days=st.integers(min_value=0, max_value=4000),
    first=st.sampled_from(SPELLINGS),
    last=st.sampled_from(SPELLINGS),
)
def test_a_pinned_window_reads_as_the_schema_admitted_it_however_it_is_spelled(
    store: StateStore, start: date, days: int, first: Any, last: Any
) -> None:
    """The pin holds the operator's bytes, so the window is read from them as the schema that
    admitted the file read it, never more strictly: whatever `DateWindow` makes of a span is
    what the count reads, and a span it refuses is a window that cannot be read."""
    end = start + timedelta(days=days)
    text = f"id: demo_mr\nwindows:\n  certification:\n    start: {first(start)}\n"
    text += f"    end: {last(end)}\n"
    span = yaml.safe_load(text)["windows"]["certification"]

    admitted = DateWindow.model_validate(span)

    assert (admitted.start, admitted.end) == (start, end)
    assert certification_window(store, store.put_blob(text.encode())) == (start, end)


def test_a_window_the_schema_refuses_or_a_file_that_does_not_read_is_none(
    store: StateStore,
) -> None:
    """None of them shows where its certificate looked, so each counts wherever asked."""
    for text in (
        b"windows: {certification: {start: 2024-02-29, end: 2024-02-06}}\n",
        b"windows: {certification: {start: 2024-02-06T10:00:00+10:00, end: 2024-02-29}}\n",
        b"windows: {certification: {start: not a day, end: 2024-02-29}}\n",
        b"windows: {certification: {end: 2024-02-29}}\n",
        b"windows: {certification: [2024-02-06, 2024-02-29]}\n",
        b"windows: {certification: {start: [unclosed\n",
        b"\xff\xfe not text",
    ):
        assert certification_window(store, store.put_blob(text)) is None, text
    assert (
        certification_window(
            store,
            store.put_blob(
                b"windows: {certification: {start: 2024-02-06, end: 2024-02-29,"
                b" note: an older schema's}}\n"
            ),
        )
        == WINDOW
    ), "a key beside the two bounds is an older schema's, and the bounds still read"


MIDNIGHT = {
    **DOCUMENT,
    "windows": {
        **DOCUMENT["windows"],
        "certification": {
            "start": datetime(WINDOW[0].year, WINDOW[0].month, WINDOW[0].day),
            "end": datetime(WINDOW[1].year, WINDOW[1].month, WINDOW[1].day),
        },
    },
}
"""The demo with its certification window written as midnight timestamps, which
`yaml.safe_dump` writes as `2024-02-06 00:00:00` and the schema admits as the days."""


def test_a_lineage_pinned_to_a_midnight_window_counts_its_looks_and_shows_them(
    ws: Workspace, store: StateStore
) -> None:
    """The file is registered as the operator wrote it; its certificates are looks at the
    window it declares, and `kanso status` reads the count off it."""
    classify(ws, store, MIDNIGHT)
    derived(ws, store, SIBLING, HYP_ID)
    (pin,) = store.connection.execute(
        "SELECT hypothesis_sha FROM hypotheses WHERE hyp_id = ?", (HYP_ID,)
    ).fetchone()
    assert b"start: 2024-02-06 00:00:00" in store.get_blob(pin)
    looked(ws, store, HYP_ID, 1, pin=pin)
    looked(ws, store, SIBLING, 2)

    assert looks(store, HYP_ID, WINDOW).look == 3
    rows = {row["id"]: row["holdout_looks"] for row in status._hypotheses(store)}
    assert rows == {HYP_ID: 2, SIBLING: 2}


def test_status_reads_each_registered_file_once_however_many_hypotheses_it_lists(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lineage of six, each certified once: the lineage graph and every pinned window are
    read once for the whole screen, not once for each row that asks, and so is every
    certificate's gates — the six members share a window, so the lineage is counted once."""
    classify(ws, store, DOCUMENT)
    members = [HYP_ID]
    for n in range(5):
        members.append(derived(ws, store, f"demo_{n}", members[-1]))
    for seq, member in enumerate(members, start=1):
        looked(ws, store, member, seq)
    parsed: list[str] = []
    load = yaml.safe_load

    def counted(text: Any) -> Any:
        parsed.append(text)
        return load(text)

    monkeypatch.setattr(yaml, "safe_load", counted)
    judged: list[object] = []
    unread = looks_module.read_no_window

    def judging(evidence: Any) -> bool:
        judged.append(evidence)
        return unread(evidence)

    monkeypatch.setattr(looks_module, "read_no_window", judging)

    rows = status._hypotheses(store)

    assert {row["holdout_looks"] for row in rows} == {len(members)}
    assert len(judged) == len(members), "each certificate's gates once for the whole screen"
    pins = {
        store.get_blob(str(row[0]))
        for row in store.connection.execute("SELECT hypothesis_sha FROM hypotheses").fetchall()
    }
    certified = {
        store.get_blob(str(row[0]))
        for row in store.connection.execute("SELECT hypothesis_sha FROM certificates").fetchall()
    }
    assert len(parsed) == len(pins) + len(certified - pins) + len(pins), (
        "each registered file once for its derived_from and once for its window, each other "
        "certificate pin once"
    )


# --- the count a certificate records, and the verdicts it leaves alone --------


def deflation(made: Certificate) -> dict[str, Any]:
    """The evidence the certificate's search correction recorded."""
    (gate,) = [gate for gate in made.gates if gate.id == SEARCHED["id"]]
    assert gate.skipped is None, gate.skipped
    return dict(gate.evidence)


def test_a_certificate_records_its_look_and_its_correction_is_held_to_the_plan_at_every_look(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo's best clears a deflated ratio of 0.9 at 0.924. The same bytes re-certified
    after an engine upgrade are the second look at the same window, recorded as that, and
    judged on the same numbers against the same threshold: the count moves no verdict."""
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    searched(ws, store, *SPREAD)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    first = certify(ws, store, HYP_ID)
    monkeypatch.setattr(run, "engine_version", lambda: "9.9.9")
    second = certify(ws, store, HYP_ID)

    assert first.holdout == Holdout(
        window_start=WINDOW[0], window_end=WINDOW[1], look=1, lineage=[HYP_ID]
    )
    assert second.holdout == first.holdout.model_copy(update={"look": 2})
    assert deflation(first) == deflation(second)
    assert 0.9 < deflation(second)["dsr"] < 0.95 and deflation(second)["min_dsr"] == 0.9
    assert (first.verdict, second.verdict) == ("pass", "pass")
    assert certificate.latest(store, HYP_ID) == second, "the row reads back what was written"


def test_a_look_written_while_a_certificate_is_judged_is_counted_before_it(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling's lane, or a `cert run` by hand, can write a certificate of the lineage while
    this one runs its windows: it was written first, so this one is the look after it."""
    classify(ws, store, DOCUMENT, REVERTING)
    derived(ws, store, SIBLING, HYP_ID)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    judge = run._judge

    def meanwhile(*args: Any, **kwargs: Any) -> Any:
        judged = judge(*args, **kwargs)
        looked(ws, store, SIBLING, 1)
        return judged

    monkeypatch.setattr(run, "_judge", meanwhile)

    made = certify(ws, store, HYP_ID)

    assert made.holdout is not None and made.holdout.look == 2
    assert [look.hyp_id for look in looks(store, HYP_ID, WINDOW).prior] == [SIBLING, HYP_ID]


def test_the_look_is_counted_under_the_write_lock_its_row_is_written_under(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So no other certificate of the lineage lands between the count and the row: two
    certifications judged at once are two looks, in the order they were written."""
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    held: list[bool] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        held.append(store.connection.in_transaction)
        return looks(*args, **kwargs)

    monkeypatch.setattr(run, "looks_at", counting)

    made = certify(ws, store, HYP_ID)

    assert held == [True] and made.holdout is not None
    assert not store.connection.in_transaction


def test_a_certificate_on_a_snapshot_that_cannot_support_a_claim_records_no_look(
    ws: Workspace, store: StateStore
) -> None:
    a_manifest(ws, publication="unknown")
    snapshots.freeze(ws)
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "fail" and made.holdout is None
    assert looks(store, HYP_ID, WINDOW).prior == ()


def test_the_lane_s_precheck_is_held_to_the_plan_s_threshold_at_any_look(
    ws: Workspace, store: StateStore
) -> None:
    """The lane reads the same verdict, on the same evidence, before the lineage's first look
    at its window and after it."""
    sha = best_with(ws, store, *TIGHT)
    first = precheck(ws, store, HYP_ID, sha, plan_of(ws))
    looked(ws, store, HYP_ID, 1)

    second = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert first.passed and first.evidence["min_dsr"] == 0.9
    assert (second.passed, second.spared, second.evidence) == (True, False, first.evidence)


def test_the_child_certifies_a_best_its_correction_passes_whatever_look_it_is(
    ws: Workspace, store: StateStore
) -> None:
    """A best the plan's 0.9 lets through at 0.924 is certified at the lineage's second look
    as at its first, and the certificate is that look."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    searched(ws, store, *SPREAD)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    looked(ws, store, HYP_ID, 1)

    made, child = certify_at_stall(ws, store, HYP_ID, sha=sha)

    assert made is not None and child.passed and not child.spared and child.measured
    assert child.evidence == deflation(made) and child.evidence["min_dsr"] == 0.9
    assert made.holdout is not None and made.holdout.look == 2
