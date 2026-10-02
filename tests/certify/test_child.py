"""A stall's certification in a child of the lane: what the lane runs, holds and is told.

The workspace is the research suite's synthetic saw-tooth, so every certification here is a
real one — both windows, the perturbations and a node replay — made in a real child process.
What is under test is where it is made and what comes back across the boundary, not what
a certificate says; `test_run.py` has that.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from kanso.certify import certificate, child
from kanso.certify.run import certify, subject_run
from kanso.errors import (
    ApprovalError,
    Exit,
    KansoError,
    PreconditionError,
    ValidationError,
)
from kanso.hyp import set_status
from kanso.nautilus import backtest, session
from kanso.research import loop as research_loop
from kanso.research import scheduler
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import a_card, write_plan
from tests.research.conftest import DOCUMENT, REVERTING, classify


def certifiable(ws: Workspace, store: StateStore) -> tuple[str, str]:
    """A hypothesis with a best that scored above zero, a pinned plan and no certificate."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    write_plan(ws)
    return hyp_id, sha


def test_a_stall_certifies_in_a_child_and_runs_none_of_it_in_the_lane(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every window, perturbation and replay is the child's; the lane only reads its report.

    Measured before this on an operator's workspace: lanes that had certified in their own
    process held 2.9 GB and 4.1 GB between cards capped at 2.5 GB, and gave none of it back.
    """
    hyp_id, sha = certifiable(ws, store)

    def refused(*_: object, **__: object) -> Any:
        raise AssertionError("the lane ran what its certification child is for")

    for name in ("run", "execute", "execute_chunked", "window_data"):
        monkeypatch.setattr(backtest, name, refused)
    monkeypatch.setattr(session, "run_node", refused)
    watched = backtest.watched
    caps: list[float | None] = []

    def capped(argv: Any, **kwargs: Any) -> tuple[str | None, float, float]:
        caps.append(kwargs["mem_cap_gb"])
        return watched(argv, **kwargs)

    monkeypatch.setattr(backtest, "watched", capped)

    stall = scheduler.on_stall(ws, store, hyp_id, "l1")

    assert stall.verdict == "pass"
    made = certificate.latest(store, hyp_id)
    assert made is not None and made.verdict == "pass" and made.strategy_sha == sha
    assert caps == [research_loop.mem_cap(ws, subject_run(store, hyp_id, sha))], (
        "a certification is held to what a card of its run may hold"
    )
    (stalled,) = store.events(kind=scheduler.STALLED, subject=hyp_id)
    assert float(str(stalled.detail["cert_peak_mem_gb"])) > 0
    assert float(str(stalled.detail["cert_wall_s"])) > 0


def test_a_certification_over_its_lane_s_share_is_killed_and_refused(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id, sha = certifiable(ws, store)
    monkeypatch.setattr(child, "mem_cap", lambda *_: 0.001)

    with pytest.raises(PreconditionError, match="needed more than the 0.00 GB") as refused:
        child.certify_in_child(ws, store, hyp_id, sha=sha, lane="l1")

    assert refused.value.remedy is not None
    assert "`[env] mem_per_lane_gb`" in refused.value.remedy
    assert f"`kanso cert run {hyp_id}`" in refused.value.remedy
    assert certificate.latest(store, hyp_id) is None


def test_a_certification_whose_lane_is_told_to_stop_is_killed(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop reaches the child at the watcher's first poll, as it reaches a card."""
    hyp_id, sha = certifiable(ws, store)
    watch = backtest._watch

    def stopped_once_running(process: Any, *args: Any) -> tuple[str | None, float]:
        backtest.interrupt()
        return watch(process, *args)

    monkeypatch.setattr(backtest, "_watch", stopped_once_running)
    try:
        with pytest.raises(PreconditionError, match="was interrupted") as refused:
            child.certify_in_child(ws, store, hyp_id, sha=sha, lane="l1")
    finally:
        backtest.resume()

    assert refused.value.remedy is not None and "start the daemon again" in refused.value.remedy
    assert certificate.latest(store, hyp_id) is None


def test_a_refusal_in_the_child_is_the_same_refusal_in_the_lane(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id, sha = certifiable(ws, store)
    set_status(store, hyp_id, "retired")
    with pytest.raises(PreconditionError) as here:
        certify(ws, store, hyp_id, sha=sha)

    with pytest.raises(PreconditionError) as there:
        child.certify_in_child(ws, store, hyp_id, sha=sha)

    assert (there.value.message, there.value.remedy) == (here.value.message, here.value.remedy)


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (Exit.PRECONDITION, PreconditionError),
        (Exit.VALIDATION, ValidationError),
        (Exit.APPROVAL, ApprovalError),
        (Exit.ERROR, KansoError),
    ],
)
def test_a_refusal_comes_back_as_the_kind_it_was_raised_as(
    code: Exit, kind: type[KansoError]
) -> None:
    made = child._refusal({"ok": False, "code": int(code), "message": "no", "remedy": "do"})

    assert type(made) is kind
    assert (made.code, made.message, made.remedy) == (code, "no", "do")


def test_a_fault_in_the_child_comes_back_as_an_error_naming_it(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fault that is not a refusal is recorded by the lane rather than killing it."""
    hyp_id, sha = certifiable(ws, store)
    watching: list[int] = []
    monkeypatch.setattr(backtest, "end_with", watching.append)

    def tripped(*_: object, **__: object) -> Any:
        raise RuntimeError("the certifier tripped")

    monkeypatch.setattr(child, "certify", tripped)
    report = tmp_path / child.REPORT

    assert child.main([str(ws.root), hyp_id, sha, "l1", str(report), "4242"]) == 1

    answer = json.loads(report.read_text(encoding="utf-8"))
    assert answer["ok"] is False and answer["code"] == int(Exit.ERROR)
    assert "raised RuntimeError: the certifier tripped" in answer["message"]
    raised = child._refusal(answer)
    assert type(raised) is KansoError and raised.code == Exit.ERROR
    deadline = time.monotonic() + 5.0
    while not watching and time.monotonic() < deadline:
        time.sleep(0.01)
    assert watching == [4242], "the child watches the lane that started it"


def test_a_child_that_ends_without_a_report_is_refused(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id, sha = certifiable(ws, store)
    monkeypatch.setattr(child, "CHILD", "raise SystemExit(3)")

    with pytest.raises(PreconditionError, match="ended without a report") as refused:
        child.certify_in_child(ws, store, hyp_id, sha=sha)

    assert refused.value.remedy == f"certify it by hand with `kanso cert run {hyp_id}` to see why"


def test_a_child_s_last_words_are_the_last_line_it_wrote(tmp_path: Path) -> None:
    said = tmp_path / child.ERRORS
    said.write_text("Traceback (most recent call last):\n  ...\nImportError: no\n\n")
    assert child._last_words(said) == "ImportError: no"

    said.write_text("")
    assert child._last_words(said) == "it said nothing"
