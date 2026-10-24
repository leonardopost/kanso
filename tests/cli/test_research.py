"""`kanso research`: the loop an agent drives, through the commands it actually types.

Every property M1 is accepted on is asserted here from the outside: the same bytes over the
same snapshot give the same metric, a card that reaches outside its lane never runs, the
keep rule moves `best` and a discard restores the lane, and the history survives both.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from nautilus_trader.model.instruments import Equity
from typer.testing import CliRunner

from kanso.cli import research as research_cli
from kanso.criteria import criteria_version
from kanso.criteria.gates import periods_margin
from kanso.criteria.run import CardRun, midnight_ns
from kanso.data.catalog import day_start_ns, open_catalog, resolved_instruments_checksum
from kanso.errors import Exit
from kanso.research import records
from kanso.schemas import Card, ObjectiveParams, ObjectiveRef, resolve_venue_model
from kanso.state import StateStore
from kanso.workspace import find

from .conftest import (
    BETTER,
    FIRST,
    FLAT,
    HYP_ID,
    INSTRUMENT,
    RAISING,
    READING,
    REVERTING,
    WEAK,
    at,
    classify,
    edit,
    lane,
    payload,
    schedule_split,
    write_hypothesis,
    write_split,
)


def begin(runner: CliRunner, root: Path, *args: object) -> dict[str, object]:
    result = at(runner, root, "research", "begin", HYP_ID, *args, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def card(runner: CliRunner, root: Path, desc: str) -> dict[str, object]:
    result = at(runner, root, "research", "card", HYP_ID, "--desc", desc, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


# -- begin --------------------------------------------------------------------------


def test_begin_prints_the_lane_directory_because_an_agent_works_there(
    runner: CliRunner, registered: Path
) -> None:
    document = begin(runner, registered)

    assert Path(document["lane_dir"]) == lane(registered)
    assert sorted(path.name for path in lane(registered).iterdir()) == [
        "hypothesis.yaml",
        "program.md",
        "strategy.py",
    ]
    assert document["lane"] == "op"
    assert document["snapshot_id"]
    assert document["baseline"]["desc"] == "baseline"


def test_begin_reads_as_the_path_and_the_next_command(runner: CliRunner, registered: Path) -> None:
    result = at(runner, registered, "research", "begin", HYP_ID)

    assert result.exit_code == Exit.OK
    assert str(lane(registered)) in result.stdout
    assert "kanso research card demo_mr" in result.stdout


def test_a_second_run_of_the_same_hypothesis_is_refused(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)

    result = at(runner, registered, "research", "begin", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "already has an active run" in payload(result)["error"]


def test_a_draft_hypothesis_cannot_begin_a_run(runner: CliRunner, loaded: Path) -> None:
    # Genuinely unclassified: no construct, so nothing says what to research it as.
    path = write_hypothesis(loaded, construct=None, objective=None, constraints=None)
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK

    result = at(runner, loaded, "research", "begin", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "kanso classify" in payload(result)["remedy"]


def test_a_run_needs_a_snapshot_covering_its_windows(runner: CliRunner, loaded: Path) -> None:
    from .conftest import classify

    path = write_hypothesis(
        loaded,
        windows={
            "research": {"start": "2023-01-02", "end": "2023-03-29"},
            "certification": {"start": "2023-04-03", "end": "2023-05-31"},
            "forward": {"start": "2023-06-01"},
        },
    )
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded)

    result = at(runner, loaded, "research", "begin", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "kanso data snapshot" in payload(result)["remedy"]


def test_a_run_begins_across_the_weekends_at_its_chunk_edges(
    runner: CliRunner, chunked: Path
) -> None:
    """A chunk edge on a weekend splits what the chunks served, and no session.

    The store defines the instrument as an equity, so it is read on the US equity calendar:
    the snapshot taken straight after the backfill covers, with nothing asked twice.
    """
    taken = payload(at(runner, chunked, "data", "snapshot", "--json"))
    path = write_hypothesis(chunked)
    assert at(runner, chunked, "hyp", "add", path).exit_code == Exit.OK
    classify(chunked)

    assert begin(runner, chunked)["snapshot_id"] == taken["snapshot_id"]


def test_a_tag_names_the_run(runner: CliRunner, registered: Path) -> None:
    document = begin(runner, registered, "--tag", "20240102-9")

    assert document["tag"] == "20240102-9"


# -- cards --------------------------------------------------------------------------


def test_a_card_that_trades_and_passes_its_constraints_keeps(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)

    document = card(runner, registered, "fade the rolling mean")

    assert document["status"] == "keep"
    assert document["n_trades"] > 0
    assert document["metric"] > 0
    assert document["best_sha"] == document["strategy_sha"]
    assert [gate["id"] for gate in document["gate_results"]] == ["strategy_integrity", "min_trades"]


def test_a_keep_rewrites_the_workspace_strategy(runner: CliRunner, registered: Path) -> None:
    """The workspace copy is the best so far, and kanso owns it once a best exists."""
    begin(runner, registered)
    edit(registered, REVERTING)

    card(runner, registered, "fade the rolling mean")

    workspace_copy = (registered / "hypotheses" / HYP_ID / "strategy.py").read_text()
    assert workspace_copy == REVERTING


def test_the_same_bytes_over_the_same_snapshot_are_a_known_result_and_a_redundant_card(
    runner: CliRunner, registered: Path
) -> None:
    """Determinism is the property every other card comparison rests on, and it is why
    the second run of the same book is refused: its number is already on a card. The
    refusal is of the turn, not of the record — the result was measured, so it is a card
    of its own status, a trial, and a row in `results.tsv` like any other."""
    begin(runner, registered)
    edit(registered, REVERTING)
    first = card(runner, registered, "fade the rolling mean")

    result = at(runner, registered, "research", "card", HYP_ID, "--desc", "again", "--json")

    assert result.exit_code == Exit.PRECONDITION, result.stdout
    refused = payload(result)
    assert "already known" in refused["error"]
    assert f"--sha {first['strategy_sha'][:7]}" in refused["remedy"]
    with StateStore(registered / "state.db") as store:
        (event,) = store.events(kind="redundant", subject=HYP_ID)
        assert event.detail["metric"] == first["metric"]
        assert event.detail["like"] == first["strategy_sha"][:7]
    rows = [
        row.split("\t")
        for row in (registered / "hypotheses" / HYP_ID / "results.tsv").read_text().splitlines()[1:]
    ]
    assert [(row[7], row[8]) for row in rows] == [
        ("discard", "baseline"),
        ("keep", "fade the rolling mean"),
        ("redundant", "again"),
    ]
    assert rows[-1][1] == rows[-2][1], "the same book measured the same number"
    assert rows[-1][3] == "3", "and it is the third trial"
    assert (lane(registered) / "strategy.py").read_text() == REVERTING


def test_a_card_that_earns_less_than_the_best_discards_and_the_lane_is_restored(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")
    edit(registered, WEAK)

    document = card(runner, registered, "a shorter memory")

    assert document["status"] == "discard"
    assert document["metric"] < kept["metric"]
    assert (lane(registered) / "strategy.py").read_text() == REVERTING


def test_a_better_card_moves_the_best(runner: CliRunner, registered: Path) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    first = card(runner, registered, "fade the rolling mean")
    edit(registered, BETTER)

    document = card(runner, registered, "a longer memory")

    assert document["status"] == "keep"
    assert document["metric"] > first["metric"]
    assert document["best_sha"] == document["strategy_sha"]
    assert (registered / "hypotheses" / HYP_ID / "strategy.py").read_text() == BETTER


def test_code_that_reaches_outside_the_lane_never_runs(runner: CliRunner, registered: Path) -> None:
    """The static half of the integrity gate is read before anything is executed."""
    begin(runner, registered)
    edit(registered, READING)

    document = card(runner, registered, "reach for the catalog")

    assert document["status"] == "discard"
    assert document["metric"] == 0
    assert document["wall_s"] == 0
    assert document["n_trades"] == 0
    [gate] = document["gate_results"]
    assert gate["id"] == "strategy_integrity"
    assert gate["pass"] is False
    assert "persistence" in str(gate["evidence"])
    assert (lane(registered) / "strategy.py").read_text() == FLAT


def test_a_strategy_that_raises_is_a_crash_with_the_tail_recorded(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, RAISING)

    document = card(runner, registered, "raise on the first bar")

    assert document["status"] == "crash"
    assert document["metric"] == 0
    assert "the card asked for the impossible" in document["crash_tail"]


def test_a_card_needs_an_active_run(runner: CliRunner, registered: Path) -> None:
    result = at(runner, registered, "research", "card", HYP_ID, "--desc", "nothing", "--json")

    assert result.exit_code == Exit.PRECONDITION


def test_every_card_is_a_trial_of_the_hypothesis(runner: CliRunner, registered: Path) -> None:
    """`n_trials` counts every card of every run, baselines and crashes included."""
    begin(runner, registered)
    edit(registered, REVERTING)
    first = card(runner, registered, "fade the rolling mean")
    edit(registered, RAISING)
    second = card(runner, registered, "raise on the first bar")

    assert first["n_trials"] == 2
    assert second["n_trials"] == 3


def test_results_tsv_is_rendered_from_state_and_survives_a_restore(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    card(runner, registered, "fade the rolling mean")
    edit(registered, WEAK)
    card(runner, registered, "a shorter memory")

    rows = (registered / "hypotheses" / HYP_ID / "results.tsv").read_text().splitlines()

    assert rows[0].split("\t") == [
        "sha7",
        "metric",
        "metric_se",
        "n_trials",
        "n_trades",
        "wall_s",
        "peak_mem_gb",
        "status",
        "desc",
        "trades_per_session",
        "hit_rate",
        "median_hold_s",
        "maker_share",
        "fill_ratio",
        "gross_bps_per_trade",
        "cost_bps_per_trade",
        "net_bps_per_trade",
        "net_bps_per_trade_se",
        "gross_bps_per_session",
        "cost_bps_per_session",
        "net_bps_per_session",
    ]
    assert [row.split("\t")[8] for row in rows[1:]] == [
        "baseline",
        "fade the rolling mean",
        "a shorter memory",
    ]
    assert [row.split("\t")[7] for row in rows[1:]] == ["discard", "keep", "discard"]


def test_a_constraint_that_judged_nothing_says_why(runner: CliRunner, loaded: Path) -> None:
    """A gate with no parameter to judge by is skipped, and a skipped gate passes."""
    from .conftest import classify

    path = write_hypothesis(
        loaded, constraints=[{"id": "strategy_integrity"}, {"id": "min_trades"}]
    )
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded)
    begin(runner, loaded)
    edit(loaded, REVERTING)  # the baseline's own bytes would be a known result, not a card

    result = at(runner, loaded, "research", "card", HYP_ID, "--desc", "the trough rule")

    assert result.exit_code == Exit.OK
    assert "min_trades: pass — skipped:" in result.stdout


def test_a_card_reads_as_a_few_lines_for_a_human(runner: CliRunner, registered: Path) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)

    result = at(runner, registered, "research", "card", HYP_ID, "--desc", "fade")

    assert result.exit_code == Exit.OK
    assert "keep" in result.stdout
    assert "strategy_integrity: pass" in result.stdout
    assert "min_trades: pass" in result.stdout
    assert " a session over " in result.stdout, "how often it traded, beside the metric"
    assert " bp a trade net (gross " in result.stdout, "and what a trade earned"


def test_a_card_reports_what_its_trades_did_and_the_log_carries_the_same_numbers(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)

    kept = card(runner, registered, "fade the rolling mean")

    digest = kept["digest"]
    assert isinstance(digest, dict)
    assert digest["trades_per_session"] * digest["sessions"] == pytest.approx(kept["n_trades"])
    assert digest["gross_bps_per_trade"] - digest["cost_bps_per_trade"] == pytest.approx(
        digest["net_bps_per_trade"]
    )
    assert digest["fill_ratio"] is None, "the strategy sends market orders only"
    rows = (registered / "hypotheses" / HYP_ID / "results.tsv").read_text().splitlines()
    cells = dict(zip(rows[0].split("\t"), rows[-1].split("\t"), strict=True))
    assert cells["net_bps_per_trade"] == f"{digest['net_bps_per_trade']:.4f}"
    assert cells["fill_ratio"] == ""


def test_a_digest_reads_as_two_lines_and_a_number_it_lacks_reads_as_none() -> None:
    """A hold is read in the unit it is easiest to read in, and an absent number is said to
    be absent rather than printed as a zero."""
    from kanso.cli.research import _digest_lines
    from kanso.schemas import CardDigest

    def hold(seconds: float | None) -> str:
        traded = {
            "hit_rate": 0.5,
            "median_hold_s": seconds,
            "gross_bps_per_trade": 3.0,
            "cost_bps_per_trade": 1.0,
            "net_bps_per_trade": 2.0,
            "net_bps_per_trade_se": 0.5,
        }
        if seconds is None:
            traded = dict.fromkeys(traded)
        made = CardDigest(sessions=0, fills=0, **traded)
        lines = _digest_lines(made)
        assert len(lines) == 2 and "filled n/a" in lines[0] and "maker n/a" in lines[0]
        return lines[0].split(" · ")[2]

    assert [hold(s) for s in (None, 45.0, 960.0, 9000.0)] == [
        "hold n/a",
        "hold 45s",
        "hold 16m",
        "hold 2.5h",
    ]
    assert _digest_lines(None) == []


CHASE = REVERTING.replace(
    "deviation < -0.3 and not self.long", "deviation > 0.3 and not self.long"
).replace("deviation > 0.1 and self.long", "deviation < -0.1 and self.long")
"""The fading strategy turned round: it buys a close above its rolling mean and sells one
below, which on a reverting path loses on every round trip it makes."""


def test_a_card_whose_trades_lose_on_average_is_refused_by_edge_per_trade(
    runner: CliRunner, loaded: Path
) -> None:
    """Required by the operator, the gate refuses the card however its book scored, and
    its evidence is the number the card's digest records."""
    from .conftest import classify

    edge = {"id": "edge_per_trade", "params": {"min_bps": 0.0, "k_se": 1.0}}
    path = write_hypothesis(loaded, required_constraints=[edge])
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded)
    begin(runner, loaded)
    edit(loaded, REVERTING)
    assert card(runner, loaded, "fade the deviation")["n_trades"] > 0
    edit(loaded, CHASE)

    refused = card(runner, loaded, "chase the deviation")

    (judged,) = [gate for gate in refused["gate_results"] if gate["id"] == "edge_per_trade"]
    assert refused["status"] == "discard" and judged["pass"] is False
    assert judged["evidence"]["net_bps_per_trade"] < 0
    assert judged["evidence"]["net_bps_per_trade"] == refused["digest"]["net_bps_per_trade"]
    assert judged["evidence"]["se"] == refused["digest"]["net_bps_per_trade_se"]
    assert refused["n_trades"] > 0
    # One draw of a search of two: the fading card and this one. The flat baseline traded
    # nothing, so it is no draw.
    assert judged["evidence"]["trials"] == 2
    evidence = judged["evidence"]
    assert evidence["normal_margin"] == 1.0, "k_se stands above two trials' 0.52"
    assert evidence["margin"] == periods_margin(1.0, evidence["periods_with_trades"])


# -- end and show -------------------------------------------------------------------


def test_end_removes_the_lane_directory_and_nothing_else(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")

    result = at(runner, registered, "research", "end", HYP_ID, "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["cards"] == 2
    assert document["keeps"] == 1
    assert document["best_sha"] == kept["strategy_sha"]
    assert not lane(registered).exists()
    assert (registered / "hypotheses" / HYP_ID / "results.tsv").is_file()
    assert (registered / "hypotheses" / HYP_ID / "strategy.py").read_text() == REVERTING


def test_end_without_a_run_is_a_precondition_failure(runner: CliRunner, registered: Path) -> None:
    result = at(runner, registered, "research", "end", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION


def test_a_second_run_starts_from_the_best_of_the_first(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    card(runner, registered, "fade the rolling mean")
    assert at(runner, registered, "research", "end", HYP_ID).exit_code == Exit.OK

    begin(runner, registered)

    assert (lane(registered) / "strategy.py").read_text() == REVERTING


def test_from_workspace_starts_from_the_workspace_file(runner: CliRunner, registered: Path) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    card(runner, registered, "fade the rolling mean")
    assert at(runner, registered, "research", "end", HYP_ID).exit_code == Exit.OK
    (registered / "hypotheses" / HYP_ID / "strategy.py").write_text(FLAT, encoding="utf-8")

    begin(runner, registered, "--from-workspace")

    assert (lane(registered) / "strategy.py").read_text() == FLAT
    assert payload(at(runner, registered, "hyp", "show", HYP_ID, "--json"))["best_sha"] is None


def test_show_prints_the_best_card_s_strategy(runner: CliRunner, registered: Path) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")

    result = at(runner, registered, "research", "show", HYP_ID, "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["sha"] == kept["strategy_sha"]
    assert document["source"] == REVERTING
    assert document["card"]["digest"] == kept["digest"], "the card that ran those bytes"
    assert at(runner, registered, "research", "show", HYP_ID).stdout.strip() == REVERTING.strip()


def test_show_takes_any_unique_prefix_of_a_card_of_this_hypothesis(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")

    document = payload(
        at(
            runner,
            registered,
            "research",
            "show",
            HYP_ID,
            "--sha",
            kept["strategy_sha"][:7],
            "--json",
        )
    )

    assert document["sha"] == kept["strategy_sha"]


def test_show_refuses_a_prefix_that_names_no_card_of_this_hypothesis(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)

    result = at(runner, registered, "research", "show", HYP_ID, "--sha", "deadbee", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "names no card" in payload(result)["error"]


def test_show_refuses_an_ambiguous_prefix(runner: CliRunner, registered: Path) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    card(runner, registered, "fade the rolling mean")

    result = at(runner, registered, "research", "show", HYP_ID, "--sha", "", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "ambiguous" in payload(result)["error"]


def test_show_without_a_keep_has_no_best_to_print(runner: CliRunner, registered: Path) -> None:
    begin(runner, registered)

    result = at(runner, registered, "research", "show", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no keep yet" in payload(result)["error"]


def test_show_diffs_two_cards_of_the_hypothesis(runner: CliRunner, registered: Path) -> None:
    document = begin(runner, registered)
    baseline = str(document["baseline"]["strategy_sha"])
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")

    result = at(
        runner,
        registered,
        "research",
        "show",
        HYP_ID,
        "--sha",
        baseline[:7],
        "--diff",
        str(kept["strategy_sha"])[:7],
        "--json",
    )

    assert result.exit_code == Exit.OK
    diff = payload(result)["diff"]
    assert diff.startswith(f"--- {baseline[:7]}/strategy.py")
    assert "+    lookback: int = 12" in diff


def test_show_measured_prints_what_the_card_was_scored_from(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")

    result = at(runner, registered, "research", "show", HYP_ID, "--measured")

    assert result.exit_code == Exit.OK, result.stdout
    lines = result.stdout.splitlines()
    sha7 = str(kept["strategy_sha"])[:7]
    assert lines[0].startswith("card ") and lines[0].endswith(
        f"{sha7} · keep · fade the rolling mean"
    )
    assert lines[1].startswith("measured ") and lines[1].endswith(
        f"net_edge_bps over 4 fold(s) · criteria {criteria_version()}"
    )
    assert f"{kept['metric']:.6f} ± {kept['metric_se']:.6f}" in lines[2]
    assert f"{kept['n_trades']} trade(s)" in lines[2]
    periods = lines[4:]
    assert lines[3].split()[:4] == ["periods", str(len(periods)), "of", "1d"]
    # One line a period end: its UTC date, what the period returned, and the equity after.
    day, change, equity = periods[0].split()
    assert len(day) == len("2024-01-02") and day.startswith("2024-01")
    assert change[0] in "+-" and float(equity) > 0

    document = payload(at(runner, registered, "research", "show", HYP_ID, "--measured", "--json"))
    assert document["sha"] == kept["strategy_sha"]
    assert document["card"]["measured_sha"] == kept["measured_sha"]
    assert set(document["measurement"]) == {
        "objective",
        "folds",
        "criteria_version",
        "run",
        "host",
        "benchmark",
    }
    assert document["measurement"]["criteria_version"] == criteria_version()
    assert len(document["measurement"]["run"]["returns"]) == len(periods)
    # The payload is the codec's form, so kanso's own code scores it to the card's number.
    measured = records.read_measurement(document["measurement"])
    assert measured.compute() == (kept["metric"], kept["metric_se"])


def test_an_intraday_measurement_prints_each_end_s_time_and_what_it_was_differenced_against(
    tmp_path: Path,
) -> None:
    """Two ends on one UTC date are told apart by their time, and a host is named."""
    opened = midnight_ns(date(2024, 1, 2))
    half_hour = 1_800 * 1_000_000_000
    run = CardRun(
        window=(date(2024, 1, 2), date(2024, 1, 2)),
        period="30m",
        period_ends_ns=(opened + 29 * half_hour, opened + 30 * half_hour),
        returns=(12.5, -2.25),
        equity=(100_012.5, 100_010.25),
        trades=(),
        fills=(),
        capital=100_000.0,
        currency="USD",
        venue_model={"venue": "XNAS"},
    )
    measured = records.Measurement(
        ObjectiveRef(id="marginal_wf_sharpe", params=ObjectiveParams(min_delta=0.0, k_se=0.5)),
        2,
        run,
        host=run,
        criteria_version="0.1.0+000000000000",
    )
    with StateStore(tmp_path / "state.db") as store:
        store.migrate()
        made = Card(
            run_id="r1",
            lane="op",
            strategy_sha=store.put_blob(b"class Strategy: ..."),
            metric=0.0,
            metric_se=0.0,
            n_trials=1,
            n_trades=0,
            wall_s=1.0,
            peak_mem_gb=0.1,
            status="keep",
            desc="baseline",
            venue_model=resolve_venue_model("XNAS", max_leverage=1.0),
            created_at=datetime(2024, 1, 3, tzinfo=UTC),
            measured_sha=records.record_measurement(store, measured),
        )
        # A newer card of the same bytes that kept none is passed over and counted.
        unmeasured = made.model_copy(update={"status": "redundant", "measured_sha": None})
        shown = research_cli._measurement(
            store, HYP_ID, made.strategy_sha, [made, unmeasured, unmeasured]
        )

    assert shown.lines[0].endswith("· keep · baseline · 2 newer card(s) of these bytes kept none")
    # Scored under another kanso, which the line says: this one's objective may compute
    # another number from the same run.
    assert shown.lines[1].endswith(
        "marginal_wf_sharpe over 2 fold(s) · against the host run · criteria "
        f"0.1.0+000000000000, not this kanso's {criteria_version()}"
    )
    assert shown.data["card"]["measured_sha"] == made.measured_sha
    assert shown.lines[3].split()[:4] == ["periods", "2", "of", "30m"]
    assert [line.split()[:2] for line in shown.lines[4:]] == [
        ["2024-01-02", "14:30:00"],
        ["2024-01-02", "15:00:00"],
    ]
    assert shown.data["measurement"]["host"] == shown.data["measurement"]["run"]


def test_show_measured_refuses_a_card_that_kept_no_measurement(
    runner: CliRunner, registered: Path
) -> None:
    begin(runner, registered)
    edit(registered, RAISING)
    crashed = card(runner, registered, "raise on the first bar")
    sha7 = str(crashed["strategy_sha"])[:7]

    result = at(
        runner, registered, "research", "show", HYP_ID, "--sha", sha7, "--measured", "--json"
    )

    assert result.exit_code == Exit.PRECONDITION
    refused = payload(result)
    assert f"no card of {HYP_ID} that evaluated {sha7} kept a measurement" in refused["error"]
    assert refused["remedy"] == f"kanso research show {HYP_ID} --sha {sha7} --json"
    assert at(runner, registered, *refused["remedy"].split()[1:]).exit_code == Exit.OK


def test_show_measured_reads_past_a_newer_card_of_the_bytes_that_kept_none(
    runner: CliRunner, registered: Path
) -> None:
    """A card of the same bytes recorded by a kanso that kept no measurement — a lane not yet
    restarted onto this one, say — does not hide what the keep before it kept: the newest
    card that kept one is shown, and the card line says how many were passed over."""
    begin(runner, registered)
    edit(registered, REVERTING)
    kept = card(runner, registered, "fade the rolling mean")
    with StateStore(registered / "state.db") as store:
        run = records.require_active(store, HYP_ID)
        newest = records.cards_of(store, HYP_ID)[-1]
        records.record_card(
            store,
            run,
            newest.model_copy(
                update={"status": "redundant", "desc": "an old lane's card", "measured_sha": None}
            ),
        )
    sha7 = str(kept["strategy_sha"])[:7]

    result = at(runner, registered, "research", "show", HYP_ID, "--measured")

    assert result.exit_code == Exit.OK, result.stdout
    assert result.stdout.splitlines()[0].endswith(
        f"{sha7} · keep · fade the rolling mean · 1 newer card(s) of these bytes kept none"
    )
    document = payload(at(runner, registered, "research", "show", HYP_ID, "--measured", "--json"))
    assert (document["card"]["status"], document["card"]["measured_sha"]) == (
        "keep",
        kept["measured_sha"],
    )
    assert records.read_measurement(document["measurement"]).compute() == (
        kept["metric"],
        kept["metric_se"],
    )
    # The source is still shown with the newest card of the bytes, which kept none.
    shown = payload(at(runner, registered, "research", "show", HYP_ID, "--json"))
    assert (shown["card"]["desc"], shown["card"]["measured_sha"]) == ("an old lane's card", None)


def test_show_measured_with_a_diff_is_refused_before_anything_is_read(
    runner: CliRunner, registered: Path
) -> None:
    result = at(
        runner, registered, "research", "show", HYP_ID, "--measured", "--diff", "abc", "--json"
    )

    assert result.exit_code == Exit.VALIDATION
    assert "--measured" in payload(result)["error"] and "--diff" in payload(result)["error"]


def test_begin_needs_no_model_register(runner: CliRunner, registered: Path) -> None:
    """A run begun by hand is measurement only: the register is read where a call is made."""
    (registered / "models.yaml").rename(registered / "models.yaml.away")

    result = at(runner, registered, "research", "begin", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["baseline"]["status"] in ("keep", "discard")


def test_a_run_writes_nothing_to_the_instrument_store(runner: CliRunner, registered: Path) -> None:
    """A run is priced under the store its snapshot pins, and its resolution records nothing.

    The store is left holding a definition dated otherwise than the research start, so
    the resolution `begin` makes for the venue model has nothing held to answer from. Were
    it recorded, a definition dated the research start would join the store and move its
    checksum out from under the snapshot the run was pinned to a moment earlier.
    """
    later = ("data", "instruments", "resolve", "--as-of", "2024-02-01")
    assert at(runner, registered, *later).exit_code == Exit.OK
    ws = find(registered)
    start = day_start_ns(FIRST)
    open_catalog(ws).delete_data_range(Equity, INSTRUMENT, start, start)
    assert at(runner, registered, "data", "snapshot").exit_code == Exit.OK
    pinned = resolved_instruments_checksum(ws)
    cache = (registered / "instruments.yaml").read_bytes()

    begin(runner, registered)
    edit(registered, REVERTING)
    card(runner, registered, "the trough rule")

    assert resolved_instruments_checksum(ws) == pinned
    assert (registered / "instruments.yaml").read_bytes() == cache


def test_a_window_holding_a_split_the_definition_does_not_schedule_is_refused(
    runner: CliRunner, loaded: Path
) -> None:
    """The half that protects an operator who has not adopted the schedule.

    The refusal is made in the parent process, before the card's child is spawned, so it
    reaches the operator as a refusal with a remedy rather than as a crash the run records.
    The split point is loadable here because it carries the instant its action was
    announced, which is what an honest `corporate_action` dataset carries; the shipped
    `massive_corporate_actions` loader cannot supply one for a split, so it marks any spec
    including splits `unknown` and no snapshot relies on it in the first place.
    """
    path = write_hypothesis(loaded, REVERTING, data_requirements=["bar", "corporate_action"])
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded)
    write_split(loaded)
    assert at(runner, loaded, "data", "snapshot").exit_code == Exit.OK

    result = at(runner, loaded, "research", "begin", HYP_ID, "--tag", "20240101-1")

    assert result.exit_code == Exit.PRECONDITION, result.output
    assert result.output == (
        f"error: {INSTRUMENT}: the window holds a split effective 2024-02-01 at a ratio of "
        "0.1, and its definition schedules none\n"
        "remedy: add the split to `info.splits` in this instrument's `override` in "
        "instruments.yaml, then re-resolve and re-snapshot\n"
    )


def test_a_scheduled_split_the_window_also_carries_runs(runner: CliRunner, loaded: Path) -> None:
    """Agreement is silence: the definition and the data say the same thing, so the card runs."""
    path = write_hypothesis(loaded, REVERTING, data_requirements=["bar", "corporate_action"])
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded)
    write_split(loaded)
    schedule_split(runner, loaded)

    begun = at(runner, loaded, "research", "begin", HYP_ID, "--tag", "20240101-1")
    edit(loaded, WEAK)  # a book of its own: the baseline's bytes would be a known result
    result = at(runner, loaded, "research", "card", HYP_ID, "--desc", "fade a declared split")

    assert begun.exit_code == Exit.OK, begun.output
    assert result.exit_code == Exit.OK, result.output
