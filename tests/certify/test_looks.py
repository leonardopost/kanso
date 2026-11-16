"""Looks at a certification window, counted over a lineage, and the level they split.

Every certificate reads its certification window, so every certificate is a look at it, and a
lineage that certifies again draws again from the one sample research never saw. What is
under test is who counts — the connected component of `derived_from`, siblings and
descendants included — which certificates count — any verdict, plan or engine, on a window
that intersects, that read a window at all — and that the count reaches the deflation where
it is judged: in the certificate, in the certification child's precheck and in the lane's.

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

from kanso.certify import certificate, run
from kanso.certify.looks import Look, certification_window, looks, read_no_window
from kanso.certify.run import certify, certify_at_stall, precheck
from kanso.criteria.gates import look_level
from kanso.data import snapshot as snapshots
from kanso.research import records
from kanso.schemas import Certificate, Holdout
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
reverting best, a search whose deflated ratio comes to 0.924 — clear of 0.9, short of 0.95."""

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


# --- the count where the deflation is judged ----------------------------------


def deflation(made: Certificate) -> dict[str, Any]:
    """The evidence the certificate's search correction recorded."""
    (gate,) = [gate for gate in made.gates if gate.id == SEARCHED["id"]]
    assert gate.skipped is None, gate.skipped
    return dict(gate.evidence)


def test_a_certificate_records_its_look_and_holds_its_correction_to_the_split_level(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo's best clears a deflated ratio of 0.9 at 0.924: it passes at the lineage's
    first look, and the same bytes re-certified after an engine upgrade — the second look at
    the same window — are held to 0.95 and fail on that gate alone."""
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
    assert second.holdout is not None and second.holdout.look == 2
    one, two = deflation(first), deflation(second)
    assert (one["looks"], one["required_probability"]) == (1, 0.9)
    assert (two["looks"], two["required_probability"]) == (2, pytest.approx(0.95))
    assert one["dsr"] == two["dsr"] and 0.9 < one["dsr"] < 0.95
    failing = [gate.id for gate in second.gates if gate.skipped is None and not gate.passed]
    assert (first.verdict, second.verdict, failing) == ("pass", "fail", [SEARCHED["id"]])
    assert certificate.latest(store, HYP_ID) == second, "the row reads back what was written"


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


def test_the_lane_s_precheck_is_held_to_the_level_of_the_look_it_would_be(
    ws: Workspace, store: StateStore
) -> None:
    """A best the lane lets through, from the measurement it kept, at the lineage's first look
    at its window — 0.927 against 0.9 — it spares at the second, against 0.95."""
    sha = best_with(ws, store, *TIGHT)
    first = precheck(ws, store, HYP_ID, sha, plan_of(ws))
    looked(ws, store, HYP_ID, 1)

    second = precheck(ws, store, HYP_ID, sha, plan_of(ws))

    assert (first.passed, first.evidence["looks"], first.evidence["required_probability"]) == (
        True,
        1,
        0.9,
    )
    assert (second.spared, second.evidence["looks"]) == (True, 2)
    assert second.evidence["required_probability"] == look_level(0.9, 2)
    assert first.evidence["dsr"] == second.evidence["dsr"]


def test_the_child_s_precheck_is_held_to_the_level_of_the_look_it_would_be(
    ws: Workspace, store: StateStore
) -> None:
    """The certification child counts the looks before it runs anything, so a stall whose
    best the correction would pass at a first look is spared at a second without reading the
    window, and writes no look of its own."""
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    searched(ws, store, *SPREAD)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    looked(ws, store, HYP_ID, 1)

    made, child = certify_at_stall(ws, store, HYP_ID, sha=sha)

    assert made is None and child.spared and child.measured
    assert (child.evidence["looks"], child.evidence["required_probability"]) == (
        2,
        look_level(0.9, 2),
    )
    assert looks(store, HYP_ID, WINDOW).look == 2, "a spared stall is no look"
