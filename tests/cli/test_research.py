"""`kanso research`: the loop an agent drives, through the commands it actually types.

Every property M1 is accepted on is asserted here from the outside: the same bytes over the
same snapshot give the same metric, a card that reaches outside its lane never runs, the
keep rule moves `best` and a discard restores the lane, and the history survives both.
"""

from __future__ import annotations

from pathlib import Path

from nautilus_trader.model.instruments import Equity
from typer.testing import CliRunner

from kanso.data.catalog import day_start_ns, open_catalog, resolved_instruments_checksum
from kanso.errors import Exit
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
    edit,
    lane,
    payload,
    write_hypothesis,
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


def test_the_same_bytes_over_the_same_snapshot_give_the_same_metric(
    runner: CliRunner, registered: Path
) -> None:
    """Determinism is the property every other card comparison rests on."""
    begin(runner, registered)
    edit(registered, REVERTING)
    first = card(runner, registered, "fade the rolling mean")

    second = card(runner, registered, "the same code again")

    assert second["strategy_sha"] == first["strategy_sha"]
    assert second["metric"] == first["metric"]
    assert second["metric_se"] == first["metric_se"]
    assert second["n_trades"] == first["n_trades"]
    assert second["status"] == "discard"


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
    ]
    assert [row.split("\t")[-1] for row in rows[1:]] == [
        "baseline",
        "fade the rolling mean",
        "a shorter memory",
    ]
    assert [row.split("\t")[-2] for row in rows[1:]] == ["discard", "keep", "discard"]


def test_a_constraint_that_judged_nothing_says_why(runner: CliRunner, loaded: Path) -> None:
    """A gate with no parameter to judge by is skipped, and a skipped gate passes."""
    from .conftest import classify

    path = write_hypothesis(
        loaded, constraints=[{"id": "strategy_integrity"}, {"id": "min_trades"}]
    )
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    classify(loaded)
    begin(runner, loaded)

    result = at(runner, loaded, "research", "card", HYP_ID, "--desc", "the flat baseline again")

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
    card(runner, registered, "unchanged")

    assert resolved_instruments_checksum(ws) == pinned
    assert (registered / "instruments.yaml").read_bytes() == cache
