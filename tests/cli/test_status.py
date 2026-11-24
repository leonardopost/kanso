"""`kanso status`: the one screen that says how the workbench is doing.

Six facts, and each is asserted here for the reason it is on the screen at all: the lanes
say whether research is happening, the card rate says how fast, the best metric says
whether the speed bought anything, the spend says what it cost, the inbox says what is
waiting for a person, and a failed baseline is the one failure that is silent everywhere
else. Nothing here writes, so the last test runs it against a workspace with a run in it
and asserts the state is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.hyp import set_status
from kanso.inbox import escalate
from kanso.research import records
from kanso.state import StateStore
from kanso.workspace import find

from . import mocked
from .conftest import HYP_ID, at, edit, payload
from .test_research_run import searched_wide


def test_a_fresh_workspace_reports_every_field_with_nothing_in_it(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "status", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    found = payload(result)
    assert found["daemon"] == {"running": False, "pid": None, "children": [], "ending": []}
    assert found["cards_per_hour"] == 0
    assert found["hypotheses"] == []
    assert found["spend_today"]["calls"] == 0
    assert found["escalations"] == {"unread": 0, "entries": []}
    assert found["baseline_failed"] == []
    # `init` writes an envelope, so the lanes a daemon would use are already known.
    assert [row["lane"] for row in found["lanes"]]


def test_a_run_shows_up_on_its_lane_with_its_cards_and_its_best(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    found = payload(at(runner, mocked_ws, "status", "--json"))

    working = [row for row in found["lanes"] if row["id"] == HYP_ID]
    assert len(working) == 1
    lane_row = working[0]
    # The interactive lane is not in the envelope's plan, and an operator's run is real.
    assert lane_row["lane"] == "op"
    assert lane_row["planned"] is False
    assert lane_row["cards"] >= 2
    assert lane_row["best_metric"] is not None


def test_the_card_rate_counts_what_the_last_hour_produced(
    runner: CliRunner, mocked_ws: Path
) -> None:
    before = payload(at(runner, mocked_ws, "status", "--json"))["cards_per_hour"]

    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    after = payload(at(runner, mocked_ws, "status", "--json"))["cards_per_hour"]
    # The baseline and the proposed card, both recorded just now.
    assert after - before == 2


def test_the_best_metric_is_reported_per_hypothesis(runner: CliRunner, mocked_ws: Path) -> None:
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    found = payload(at(runner, mocked_ws, "status", "--json"))

    assert [row["id"] for row in found["hypotheses"]] == [HYP_ID]
    entry = found["hypotheses"][0]
    assert entry["status"] == "researching"
    assert entry["best_sha"] is not None
    assert entry["best_metric"] is not None
    assert entry["cards"] >= 2
    assert entry["last_stall"] is None, "the run has not stalled"


def test_a_hypothesis_s_last_stall_says_what_its_search_correction_decided(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """A stall the correction spared certified nothing and says why, by the gate's own
    probability against the threshold the plan pinned."""
    searched_wide(mocked_ws)
    mocked.tuned(mocked_ws, stall_k=1)
    outcome = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))

    found = payload(at(runner, mocked_ws, "status", "--json"))

    (entry,) = found["hypotheses"]
    stall = entry["last_stall"]
    assert stall["best_sha"] == outcome["best_sha"]
    assert (stall["certifiable"], stall["verdict"]) == (False, None)
    assert stall["precheck"] == outcome["stall"]["precheck"]
    precheck = stall["precheck"]
    assert (precheck["gate"], precheck["judged"], precheck["pass"]) == (
        "deflated_edge",
        True,
        False,
    )
    assert [reading["gate"] for reading in precheck["gates"]] == ["deflated_edge"], (
        "every reading the stall took, to the refusal that spared it"
    )
    assert precheck["gates"][0]["evidence"] == precheck["evidence"]
    evidence = precheck["evidence"]
    spared = (
        f"stall spared: deflated_edge {evidence['probability']:.3f} < "
        f"{evidence['min_probability']:.3f}"
    )
    assert spared in at(runner, mocked_ws, "status").stdout


def test_a_hypothesis_s_last_stall_says_whether_it_parked_it_and_after_how_many(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """`last_stall` carries what the stall recorded and `parked` whether the park holds now,
    by the predicate the queue applies — on the best it still holds, which a keep beating it
    ends at the next return to the queue — and the human line says what `parked` says, on a
    hypothesis that is not retired."""
    mocked.tuned(mocked_ws, stall_k=1, park_after_stalls=1)
    outcome = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))

    (entry,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]

    stall = entry["last_stall"]
    assert stall["best_sha"] == outcome["best_sha"] == entry["best_sha"]
    assert (stall["parked"], stall["stalls_on_best"], entry["parked"]) == (True, 1, True)
    assert "· parked after 1 stalls on its best" in at(runner, mocked_ws, "status").stdout

    with StateStore(mocked_ws / "state.db") as store:
        set_status(store, HYP_ID, "retired")
    (retired,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]
    assert retired["parked"] is True, "where its returns go once it is resumed"
    assert "parked after" not in at(runner, mocked_ws, "status").stdout, "not in the queue"

    with StateStore(mocked_ws / "state.db") as store:
        set_status(store, HYP_ID, "researching")
        run = records.runs_of(store, HYP_ID)[-1]
        records.set_best(store, run, store.put_blob(b"# better\n"), 1e9)

    (moved,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]
    assert moved["last_stall"]["parked"] is True, "what the stall recorded"
    assert moved["parked"] is False, "a keep moved the best"
    assert "parked after" not in at(runner, mocked_ws, "status").stdout


def test_a_stall_recorded_before_stalls_counted_reads_as_unparked(
    runner: CliRunner, mocked_ws: Path
) -> None:
    with StateStore(mocked_ws / "state.db") as store:
        store.event("stalled", HYP_ID, {"best_sha": None, "certifiable": False})

    (entry,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]

    assert (entry["last_stall"]["parked"], entry["last_stall"]["stalls_on_best"]) == (False, None)
    assert entry["parked"] is False
    assert "parked after" not in at(runner, mocked_ws, "status").stdout


def test_a_hypothesis_says_how_many_looks_its_lineage_has_taken_at_its_window(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """Every certificate of the lineage at the window the next certificate of its best reads,
    whatever its verdict — the window of the file the run that carded the best pinned, which
    the hypothesis's own pin does not move; none for a hypothesis with no best whose pinned
    file the store does not hold."""
    from .test_cert import begin, keep, recertified

    begin(runner, mocked_ws)
    keep(runner, mocked_ws)
    before = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"][0]
    assert at(runner, mocked_ws, "cert", "run", HYP_ID).exit_code == Exit.OK
    recertified(mocked_ws)

    (entry,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]

    assert (before["holdout_looks"], entry["holdout_looks"]) == (0, 2)
    assert "2 hold-out look(s)" in at(runner, mocked_ws, "status").stdout
    with StateStore(mocked_ws / "state.db") as store:
        store.connection.execute(
            "UPDATE hypotheses SET hypothesis_sha = NULL WHERE hyp_id = ?", (HYP_ID,)
        )
    (carded,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]
    assert carded["holdout_looks"] == 2, "counted where the best's run pinned its window"
    with StateStore(mocked_ws / "state.db") as store:
        store.connection.execute(
            "UPDATE hypotheses SET best_sha = NULL WHERE hyp_id = ?", (HYP_ID,)
        )
    (unpinned,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]
    assert unpinned["holdout_looks"] is None


def test_a_hypothesis_says_how_many_forward_looks_its_lineage_has_taken(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """A version's stage windows are one look at the hypothesis's own forward window, however
    many, and a replay of it another; a read wholly before the window opens is none; none at all
    for a hypothesis whose pinned file the store does not hold, and no line where there are
    none."""
    from datetime import date

    from kanso.certify.looks import ForwardRead, record_read

    before = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"][0]
    assert "forward look" not in at(runner, mocked_ws, "status").stdout
    with StateStore(mocked_ws / "state.db") as store:
        for of, via, last in (
            (f"{HYP_ID}@1", "stage", date(2024, 6, 14)),
            (f"{HYP_ID}@1", "stage", date(2024, 6, 28)),
            ("session-1", "replay", date(2024, 6, 28)),
            ("session-2", "replay", date(2024, 5, 31)),
        ):
            record_read(
                store,
                ForwardRead(
                    hyp_id=HYP_ID,
                    via=via,
                    of=of,
                    target=of,
                    where="paper" if via == "stage" else "node",
                    session=of,
                    forward_start=date(2024, 6, 3),
                    first=date(2024, 6, 3),
                    last=last,
                ),
            )

    (entry,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]

    assert (before["forward_looks"], entry["forward_looks"]) == (0, 2)
    assert "card(s) · 2 forward look(s)" in at(runner, mocked_ws, "status").stdout
    with StateStore(mocked_ws / "state.db") as store:
        store.connection.execute(
            "UPDATE hypotheses SET hypothesis_sha = NULL WHERE hyp_id = ?", (HYP_ID,)
        )
    (unpinned,) = payload(at(runner, mocked_ws, "status", "--json"))["hypotheses"]
    assert unpinned["forward_looks"] is None


def test_a_hypothesis_whose_window_is_written_as_midnight_timestamps_is_listed(
    runner: CliRunner, loaded: Path
) -> None:
    """The schema admits a day written as a timestamp at midnight, and `hyp add` pins the file
    as written; the screen reads its windows as the schema did rather than refuse to draw."""
    from datetime import datetime

    from .conftest import CERTIFICATION, HYPOTHESIS, write_hypothesis

    stamped = {
        "start": f"{CERTIFICATION[0].isoformat()}T00:00:00Z",
        "end": datetime(CERTIFICATION[1].year, CERTIFICATION[1].month, CERTIFICATION[1].day),
    }
    forward = {"start": "2024-06-03T00:00:00Z"}
    windows = {**HYPOTHESIS["windows"], "certification": stamped, "forward": forward}
    path = write_hypothesis(loaded, windows=windows)
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    assert "00:00:00" in path.read_text(encoding="utf-8")

    result = at(runner, loaded, "status", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    (entry,) = payload(result)["hypotheses"]
    assert entry["holdout_looks"] == entry["forward_looks"] == 0
    assert at(runner, loaded, "status").exit_code == Exit.OK


def test_today_s_spend_is_what_the_ledger_holds_broken_out_by_lane(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    spend = payload(at(runner, mocked_ws, "status", "--json"))["spend_today"]

    assert spend["calls"] >= 1
    assert spend["tokens_in"] > 0
    assert "op" in spend["by_lane"]


def test_unread_escalations_are_listed_and_counted(runner: CliRunner, mocked_ws: Path) -> None:
    ws = find(mocked_ws)
    with StateStore(ws.path("state.db")) as store:
        escalate(ws, store, "misaligned", HYP_ID, "it wandered off the thesis")

    found = payload(at(runner, mocked_ws, "status", "--json"))

    assert found["escalations"]["unread"] == 1
    assert found["escalations"]["entries"][0]["kind"] == "misaligned"
    assert "1 unread" in at(runner, mocked_ws, "status").stdout


def test_a_hypothesis_whose_baseline_will_not_run_is_named(
    runner: CliRunner, mocked_ws: Path
) -> None:
    # The workspace copy is what a run is opened from, and this one raises on its first bar.
    (mocked_ws / "hypotheses" / HYP_ID / "strategy.py").write_text(
        mocked.SEED.replace('mode = "flat"', 'mode = "boom"'), encoding="utf-8"
    )
    assert (
        at(
            runner, mocked_ws, "hyp", "add", mocked_ws / "hypotheses" / HYP_ID / "hypothesis.yaml"
        ).exit_code
        == Exit.OK
    )
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code != Exit.OK

    found = payload(at(runner, mocked_ws, "status", "--json"))

    assert [row["id"] for row in found["baseline_failed"]] == [HYP_ID]
    assert HYP_ID in at(runner, mocked_ws, "status").stdout


def test_a_baseline_failure_a_later_run_cleared_is_not_reported(
    runner: CliRunner, mocked_ws: Path
) -> None:
    document = mocked_ws / "hypotheses" / HYP_ID / "hypothesis.yaml"
    (mocked_ws / "hypotheses" / HYP_ID / "strategy.py").write_text(
        mocked.SEED.replace('mode = "flat"', 'mode = "boom"'), encoding="utf-8"
    )
    assert at(runner, mocked_ws, "hyp", "add", document).exit_code == Exit.OK
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code != Exit.OK
    (mocked_ws / "hypotheses" / HYP_ID / "strategy.py").write_text(mocked.SEED, encoding="utf-8")
    assert at(runner, mocked_ws, "hyp", "add", document).exit_code == Exit.OK
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    assert payload(at(runner, mocked_ws, "status", "--json"))["baseline_failed"] == []


def test_status_reads_as_one_screen_and_writes_nothing(runner: CliRunner, mocked_ws: Path) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    edited = edit(mocked_ws, mocked.SEED)
    before = edited.read_bytes()

    result = at(runner, mocked_ws, "status")

    assert result.exit_code == Exit.OK
    for label in ("workspace", "daemon", "lanes", "cards/h", "best", "spend", "inbox"):
        assert label in result.stdout
    assert edited.read_bytes() == before


def test_a_workspace_with_no_envelope_says_where_lanes_come_from(
    runner: CliRunner, mocked_ws: Path
) -> None:
    (mocked_ws / "envelope.yaml").unlink()

    result = at(runner, mocked_ws, "status")

    assert result.exit_code == Exit.OK
    assert "kanso env detect" in result.stdout
    assert payload(at(runner, mocked_ws, "status", "--json"))["lanes"] == []
