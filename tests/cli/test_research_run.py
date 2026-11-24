"""`kanso research run`: the loop with no human in it, driven from the command line.

The driver itself is proved in its own slice; what is asserted here is the command around
it — that one invocation begins a run when there is none, that `--cards` counts what this
invocation proposed, that the object printed under `--json` carries the tally an operator
or an agent branches on, and that every model call it made is in the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kanso.certify import certificate
from kanso.certify.plan import read_plan
from kanso.certify.run import Precheck, precheck
from kanso.cli.research import spared_by
from kanso.criteria import gates
from kanso.criteria.context import MovedRunError, TrialFolds, skipped, verdict
from kanso.errors import Exit
from kanso.hyp import set_status, show
from kanso.models import spend
from kanso.research import records, scheduler
from kanso.schemas import Card, RunRecord, resolve_venue_model
from kanso.state import StateStore
from kanso.workspace import find
from tests.criteria.builders import build_run, context, make_hyp
from tests.criteria.test_param_plateau import Rerun, a_run

from . import mocked
from .conftest import DRAFT, HYP_ID, HYPOTHESIS, at, lane, payload, write_hypothesis


def ledger_calls(root: Path) -> int:
    """How many model calls this workspace has recorded, over every day and lane."""
    with StateStore(root / "state.db") as store:
        return spend(store).calls


WIDE_EDGES = (-400.0, 400.0, -200.0, 200.0, 0.0, 300.0, -300.0)
"""Per-trade edges in basis points far wider than anything the scripted cycle keeps, so the
search's expected maximum is out of its best's reach."""


def searched_wide(root: Path) -> None:
    """Cards of the hypothesis from a search before this one, under its registered file and
    another criteria version, one trial of each of `WIDE_EDGES`: the spread the deflation
    reads, and nothing the next proposal is shown as a number."""
    ws = find(root)
    with StateStore(ws.path("state.db")) as store:
        pin = str(show(ws, store, HYP_ID).hypothesis_sha)  # type: ignore[union-attr]
        for seq, edge in enumerate(WIDE_EDGES, start=1):
            started = datetime(2024, 1, 2, tzinfo=UTC)
            run = records.insert(
                store,
                RunRecord(
                    run_id=f"earlier{seq}",
                    hyp_id=HYP_ID,
                    tag=f"20240102-{seq}",
                    lane="op",
                    dir=f"runs/op/{HYP_ID}",
                    base_sha=pin,
                    hypothesis_sha=pin,
                    program_sha=pin,
                    snapshot_id="earlier",
                    criteria_version="0.0.1",
                    card_budget_s=60.0,
                    baseline_wall_s=1.0,
                    baseline_peak_mem_gb=0.5,
                    started_at=started,
                ),
            )
            records.record_card(
                store,
                run,
                Card(
                    run_id=run.run_id,
                    lane="op",
                    strategy_sha=store.put_blob(f"# earlier {seq}\n".encode()),
                    metric=edge,
                    metric_se=1.0,
                    n_trials=seq,
                    n_trades=10,
                    wall_s=1.0,
                    peak_mem_gb=0.5,
                    status="discard",
                    desc="an earlier search",
                    venue_model=resolve_venue_model("SIM", max_leverage=1.0),
                    created_at=started,
                ),
            )
            records.close(store, run)


def test_run_begins_a_run_and_proposes_the_cards_it_was_asked_for(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 3, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    outcome = payload(result)
    assert outcome["proposed"] == 3
    assert outcome["keeps"] + outcome["discards"] + outcome["crashes"] == 3
    assert (outcome["missed"], outcome["redundant"]) == (0, 0)
    assert outcome["reason"] == "cards"
    assert outcome["ended"] is False
    assert outcome["stall"] is None, "nothing stalled, so nothing was decided at a stall"
    # The run it began is still open, and its lane directory is still the interface.
    assert lane(mocked_ws).is_dir()


def test_the_three_scripted_answers_keep_discard_and_crash(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 3, "--json")

    outcome = payload(result)
    assert (outcome["keeps"], outcome["discards"], outcome["crashes"]) == (1, 1, 1)
    assert outcome["best_sha"] is not None
    assert outcome["best_metric"] is not None


def test_every_call_the_driver_made_is_in_the_ledger(runner: CliRunner, mocked_ws: Path) -> None:
    before = ledger_calls(mocked_ws)

    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 2).exit_code == Exit.OK

    # One `propose` call per card, and nothing is proposed without being recorded.
    assert ledger_calls(mocked_ws) - before >= 2


def test_a_second_invocation_resumes_the_same_run(runner: CliRunner, mocked_ws: Path) -> None:
    first = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1, "--json"))

    second = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1, "--json"))

    assert second["run_id"] == first["run_id"]
    assert second["proposed"] == 1


def test_run_reads_as_the_tally_and_the_next_command(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1)

    assert result.exit_code == Exit.OK
    assert "1 proposed" in result.stdout
    assert "0 repeated · 0 redundant" in result.stdout
    assert f"kanso research run {HYP_ID}" in result.stdout


def test_a_stall_ends_the_run_and_the_command_says_so(runner: CliRunner, mocked_ws: Path) -> None:
    # One non-keep is a stall, and the scripted cycle's second answer is a discard.
    mocked.tuned(mocked_ws, stall_k=1)

    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    outcome = payload(result)
    assert outcome["reason"] == "stalled"
    assert outcome["ended"] is True
    # The stall certified the keep it left behind, and the command says which and how.
    assert outcome["certificate"]["sha7"] == outcome["best_sha"][:7]
    assert outcome["certificate"]["verdict"] in ("pass", "fail")
    # Ending a run removes its lane directory and nothing else: the keep is still there.
    assert not lane(mocked_ws).exists()
    assert "Strategy.mode" in at(runner, mocked_ws, "research", "show", HYP_ID).stdout


def test_a_stall_says_what_its_search_correction_judged(runner: CliRunner, mocked_ws: Path) -> None:
    mocked.tuned(mocked_ws, stall_k=1)

    outcome = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))

    stall = outcome["stall"]
    assert stall["certifiable"] is True
    precheck = stall["precheck"]
    assert (precheck["gate"], precheck["judged"], precheck["pass"]) == ("deflated_edge", True, True)
    assert precheck["evidence"]["min_probability"] == 0.9, "the threshold the plan pinned"
    assert outcome["certificate"]["sha7"] == outcome["best_sha"][:7]
    readings = {reading["gate"]: reading for reading in precheck["gates"]}
    assert list(readings) == ["deflated_edge", "walk_forward_consistency", "research_plateau"]
    assert all(reading["judged"] and reading["pass"] for reading in readings.values())
    assert readings["deflated_edge"]["evidence"] == precheck["evidence"], "the deciding one"
    assert readings["walk_forward_consistency"]["evidence"]["min_positive_folds"] == 1
    assert readings["research_plateau"]["evidence"]["n_backtests"] > 0, "run by the child"


SHARPE = make_hyp(objective={"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 0.5}})
"""The demo with a Sharpe objective, scored off its period returns."""


def refusal(gate_id: str, params: dict[str, float], **slots: Any) -> dict[str, object]:
    """A stall's precheck as its `stalled` event records it, spared by this gate judging these
    slots — the gate's own evidence, not a hand-written copy of its keys."""
    reading = gates()[gate_id].evaluate(context(a_run(1.0), stage="cert", params=params, **slots))
    assert reading.skipped is None and not reading.passed, reading
    return Precheck(
        [skipped("deflated_edge", "plan carries none"), reading], measured=True
    ).payload()


def raising_at(moved: dict[str, float], otherwise: Rerun) -> Any:
    """A re-run whose strategy raises at one moved setting and scores as `otherwise` at the
    rest."""

    def rerun(overrides: dict[str, float]) -> Any:
        if dict(overrides) == moved:
            raise MovedRunError("moved", raised="ValueError: only one setting runs")
        return otherwise(overrides)

    return rerun


@pytest.mark.parametrize(
    ("precheck", "said"),
    [
        (
            Precheck(
                [
                    verdict(
                        "deflated_contribution",
                        False,
                        {"probability": 0.7921, "min_probability": 0.95},
                    )
                ],
                measured=False,
            ).payload(),
            "deflated_contribution 0.792 < 0.950",
        ),
        (
            {
                "gate": "deflated_sharpe",
                "judged": True,
                "pass": False,
                "measured": True,
                "evidence": {"dsr": 0.5, "min_dsr": 0.9},
            },
            "deflated_sharpe 0.500 < 0.900",
        ),
        (
            refusal(
                "walk_forward_consistency",
                params={"min_positive_folds": 4},
                hyp=SHARPE,
                research_run=build_run((3.0, -1.0, -4.0, -1.0, 5.0, -2.0, -6.0, -2.0)),
            ),
            "walk_forward_consistency 2 < 4 positive folds",
        ),
        (
            refusal(
                "overfit_probability",
                params={"half_blocks": 2, "max_pbo": 0.25},
                trial_folds=lambda blocks: TrialFolds(
                    values=((3.0, 1.0, 0.0, 2.0), (1.0, 2.0, 2.0, 1.0), (0.0, 0.0, 1.0, 1.0)),
                    subject=0,
                    excluded={},
                    periods=8,
                ),
            ),
            "overfit_probability 0.667 > 0.250",
        ),
        (
            refusal(
                "research_plateau",
                params={"perturb_pct": 10.0, "keep_fraction": 0.9},
                tunable={"fast": 10.0, "slow": 40.0},
                rerun=Rerun(0.5, **{"fast=11": 2.0}),
                research_run=a_run(1.0),
            ),
            "research_plateau 3 of 4 moves under 0.900",
        ),
        (
            refusal(
                "research_plateau",
                params={"perturb_pct": 10.0, "keep_fraction": 0.9},
                tunable={"fast": 10.0, "slow": 40.0},
                rerun=raising_at({"fast": 11.0}, Rerun(0.5)),
                research_run=a_run(1.0),
            ),
            "research_plateau 3 of 4 moves under 0.900, 1 raised",
        ),
        (
            refusal(
                "research_plateau",
                params={"perturb_pct": 10.0, "keep_fraction": 0.9},
                tunable={"fast": 10.0},
                rerun=Rerun(),
                research_run=a_run(-0.412),
            ),
            "research_plateau unperturbed -0.412 not positive",
        ),
        (
            Precheck([verdict("min_trades", False, {"total": 0})], measured=True).payload(),
            "min_trades refused",
        ),
        (
            Precheck(
                [verdict("deflated_edge", True, {"probability": 0.99})], measured=True
            ).payload(),
            None,
        ),
        (Precheck([skipped("deflated_edge", "too few trials")], measured=False).payload(), None),
        (Precheck(measured=False, reason="no card").payload(), None),
        (None, None),
    ],
    ids=[
        "contribution",
        "sharpe-recorded-before-gates",
        "folds",
        "selection",
        "plateau-moves",
        "plateau-raised",
        "plateau-loss",
        "another-gate",
        "passed",
        "judged-nothing",
        "read-nothing",
        "no-stall",
    ],
)
def test_a_spared_stall_is_said_in_the_terms_of_the_gate_that_spared_it(
    precheck: object, said: str | None
) -> None:
    """The `certification` line of `research run` and the `stall spared:` suffix of `status`
    name the research gate that spared a stall and the numbers it decided on; a stall recorded
    before a stall judged more than its correction reads as it always did."""
    assert spared_by(precheck) == said


def test_a_stall_its_search_correction_refuses_is_spared_and_says_so(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """The best clears no search this wide on research data, so certifying it would read the
    certification window for a verdict already known: the stall certifies nothing."""
    searched_wide(mocked_ws)
    mocked.tuned(mocked_ws, stall_k=1)

    result = at(runner, mocked_ws, "research", "run", HYP_ID)

    assert result.exit_code == Exit.OK, result.stdout
    assert "certification spared · deflated_edge " in result.stdout
    assert "on research data, so the window was not read" in result.stdout
    assert "certified" not in result.stdout
    assert payload(at(runner, mocked_ws, "cert", "show", HYP_ID, "--json"))["certificate"] is None


@pytest.mark.parametrize("as_json", [True, False])
def test_a_spared_stall_reports_no_certificate_an_earlier_plan_made(
    runner: CliRunner, mocked_ws: Path, as_json: bool
) -> None:
    """The best was certified under the first plan; after a replan its search is priced wider,
    and the next stall on the same bytes is spared. The first plan's certificate is still the
    hypothesis's newest, but it is not what this run came to: under the plan the stall judged
    by, the best stands uncertified."""
    mocked.tuned(mocked_ws, stall_k=1)
    first = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))
    assert first["certificate"] is not None and first["stall"]["certifiable"] is True
    replanned = at(runner, mocked_ws, "cert", "plan", HYP_ID, "--replan", "--json")
    assert replanned.exit_code == Exit.OK, replanned.stdout
    searched_wide(mocked_ws)

    second = at(runner, mocked_ws, "research", "run", HYP_ID, *(("--json",) if as_json else ()))

    assert second.exit_code == Exit.OK, second.stdout
    newest = payload(at(runner, mocked_ws, "cert", "show", HYP_ID, "--json"))
    assert newest["plan_version"] == 1, "the first plan's certificate is still the newest"
    if as_json:
        outcome = payload(second)
        assert outcome["best_sha"] == first["best_sha"], "the same bytes stalled again"
        assert outcome["stall"]["certifiable"] is False
        assert outcome["stall"]["precheck"]["pass"] is False
        assert outcome["certificate"] is None
    else:
        assert "certification spared · deflated_edge " in second.stdout
        assert "certified" not in second.stdout
        assert f"kanso cert show {HYP_ID}" not in second.stdout


@pytest.mark.parametrize("as_json", [True, False])
def test_a_spared_stall_reports_no_certificate_an_earlier_pin_made(
    runner: CliRunner, mocked_ws: Path, as_json: bool
) -> None:
    """The best was certified under the first `hypothesis.yaml`; the operator re-pins an edited
    one, the same bytes are carded under it, the search is priced wider, and the next stall on
    them is spared. The first pin's certificate is still the newest, of the same bytes under the
    same plan and engine, but the stall judged them uncertified under the pin it judged by
    (`certificate.judged`), so it is not what this run came to."""
    mocked.tuned(mocked_ws, stall_k=1)
    first = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))
    assert first["certificate"] is not None and first["stall"]["certifiable"] is True
    edited = write_hypothesis(mocked_ws, mocked.SEED, thesis=f"{HYPOTHESIS['thesis']} Edited.")
    added = at(runner, mocked_ws, "hyp", "add", str(edited), "--json")
    assert added.exit_code == Exit.OK, added.stdout
    searched_wide(mocked_ws)

    second = at(runner, mocked_ws, "research", "run", HYP_ID, *(("--json",) if as_json else ()))

    assert second.exit_code == Exit.OK, second.stdout
    newest = payload(at(runner, mocked_ws, "cert", "show", HYP_ID, "--json"))
    assert newest["plan_version"] == 1, "the first pin's certificate is still the newest"
    if as_json:
        outcome = payload(second)
        assert outcome["best_sha"] == first["best_sha"], "the same bytes stalled again"
        assert outcome["stall"]["certifiable"] is False
        assert outcome["stall"]["precheck"]["pass"] is False
        assert outcome["certificate"] is None
    else:
        assert "certification spared · deflated_edge " in second.stdout
        assert "certified" not in second.stdout
        assert f"kanso cert show {HYP_ID}" not in second.stdout


def test_a_stall_s_correction_is_the_certificate_s_own_to_the_last_bit(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """On a per-trade edge, corrected by `deflated_edge`: the lane's reading of the card the
    loop measured, the child's on the research window it ran again, and the certificate's
    correction are one number, bit for bit."""
    mocked.tuned(mocked_ws, stall_k=1)

    outcome = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))

    ws = find(mocked_ws)
    with StateStore(ws.path("state.db")) as opened:
        made = certificate.latest(opened, HYP_ID)
        pinned = read_plan(ws, HYP_ID)
        assert made is not None and pinned is not None
        lane = precheck(ws, opened, HYP_ID, outcome["best_sha"], pinned)
    (deflation,) = [gate for gate in made.gates if gate.id == "deflated_edge"]
    child = outcome["stall"]["precheck"]
    assert lane.judged and child["judged"] and deflation.skipped is None
    assert (lane.measured, child["measured"]) == (False, True)
    assert dict(lane.evidence) == child["evidence"] == dict(deflation.evidence)
    assert {"probability", "expected_maximum_bps", "trials", "scored_trials"} <= set(
        deflation.evidence
    )
    assert lane.passed is child["pass"] is deflation.passed is True


def test_a_stall_that_wrote_nothing_reports_no_earlier_stall_as_its_own(
    runner: CliRunner, mocked_ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retire that lands as the run ends leaves the stall nothing to decide and nothing to
    record; the hypothesis's newest `stalled` event is then an earlier run's, and what this
    invocation reports is its own stall, which certified nothing and judged nothing."""
    searched_wide(mocked_ws)
    mocked.tuned(mocked_ws, stall_k=1)
    first = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))
    assert first["stall"]["precheck"]["pass"] is False, "the first stall was spared"
    held = scheduler.hold

    def retired_as_it_ends(store: StateStore, hyp_id: str, lane: str) -> None:
        held(store, hyp_id, lane)
        set_status(store, hyp_id, "retired")

    monkeypatch.setattr(scheduler, "hold", retired_as_it_ends)

    second = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--json"))

    assert second["ended"] is True
    assert second["stall"] == {"certifiable": False, "precheck": None}
    assert second["certificate"] is None


def test_a_stall_points_at_the_certificate_it_produced(runner: CliRunner, mocked_ws: Path) -> None:
    mocked.tuned(mocked_ws, stall_k=1)

    result = at(runner, mocked_ws, "research", "run", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "certified" in result.stdout
    assert f"kanso cert show {HYP_ID}" in result.stdout


def test_a_drift_check_falls_on_the_cadence_the_workspace_sets(
    runner: CliRunner, mocked_ws: Path
) -> None:
    mocked.tuned(mocked_ws, align_every=1)
    mocked.scripted(mocked_ws, align_check=[mocked.ALIGNED])

    outcome = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 2, "--json"))

    assert outcome["checks"] == 2
    assert outcome["drifts"] == 0


def test_running_an_unclassified_hypothesis_is_refused(runner: CliRunner, loaded: Path) -> None:
    path = write_hypothesis(loaded, mocked.SEED, base=DRAFT)
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    mocked.scripted(loaded)

    result = at(runner, loaded, "research", "run", HYP_ID, "--cards", 1, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "classif" in payload(result)["error"]
