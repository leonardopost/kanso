"""`kanso screen run` and `show`: measured once under its pins, refused where it must be.

The demo series is an Ornstein-Uhlenbeck path pulled back by half its gap each hour, so its
hourly returns are negatively autocorrelated: a screen of the series against itself at one
hour reads that, at the smallest p its draws can give, in every fold.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.schemas import parse_yaml
from kanso.schemas.screen import Screen, ScreenResult
from kanso.screen import embargo, validate
from kanso.state import StateStore
from kanso.workspace import find

from .conftest import INSTRUMENT, at, payload
from .test_screen import write_screen

OU: dict[str, Any] = {
    "schema": 1,
    "id": "ou_rev",
    "title": "The demo path reverts hour to hour",
    "thesis": "Hourly returns of the demo series are negatively autocorrelated.",
    "window": {"start": "2024-01-02", "end": "2024-03-29"},
    "legs": {"a": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"}},
    "clock": {"grid": "1h", "hours": "overlap"},
    "measures": [
        {"id": "lead_lag", "from": "a", "to": "a", "estimator": "grid", "lags": ["1h", "2h"]}
    ],
    "verdict": {"alpha": 0.05, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 20},
}


def run_screen(runner: CliRunner, root: Path, document: dict[str, Any] = OU) -> dict[str, Any]:
    path = write_screen(root, document)
    result = at(runner, root, "screen", "run", path, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def test_a_run_measures_judges_records_and_renders(runner: CliRunner, loaded: Path) -> None:
    document = run_screen(runner, loaded)

    assert document["stored"] is False and document["fetched"] == []
    assert document["cells"] == 2
    assert document["verdict"]["declared"] is True
    assert document["verdict"]["worth_a_lane"] is False
    best = document["best"][0]
    assert best["key"] == "lead_lag/a>a/1h" and best["judged"] == "pass"
    assert best["mean"] < -0.1
    assert best["p"] < 0.01
    assert best["folds_same_sign"] == 4 and best["staleness"] == {"a": 0.0}
    rendered = parse_yaml(ScreenResult, Path(document["result"]).read_text(encoding="utf-8"))
    assert rendered.assumption == "sessions are roughly independent of each other"
    assert rendered.series[0].timestamps == "unknown"
    with StateStore(find(loaded).path("state.db")) as store:
        events = store.events(kind="screened", subject="ou_rev")
    assert len(events) == 1


def test_the_same_pins_return_the_stored_result_and_read_nothing(
    runner: CliRunner, loaded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = run_screen(runner, loaded)

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("a stored result reads no point")

    monkeypatch.setattr("kanso.screen.run.sessions.read", forbidden)
    again = run_screen(runner, loaded)

    assert again["stored"] is True
    assert again["sha"] == first["sha"] and again["best"] == first["best"]
    human = at(runner, loaded, "screen", "run", write_screen(loaded, OU))
    assert "stored" in human.stdout


def test_two_workspaces_holding_the_same_bytes_measure_the_same_numbers(
    runner: CliRunner, loaded: Path, tmp_path: Path
) -> None:
    twin = tmp_path / "twin"
    shutil.copytree(loaded, twin, symlinks=True)

    one = parse_yaml(ScreenResult, Path(run_screen(runner, loaded)["result"]).read_text("utf-8"))
    two = parse_yaml(ScreenResult, Path(run_screen(runner, twin)["result"]).read_text("utf-8"))

    assert [cell.model_dump() for cell in one.cells] == [cell.model_dump() for cell in two.cells]


def test_a_file_edited_while_its_run_fetches_is_recorded_as_the_bytes_validated(
    runner: CliRunner, loaded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: a screen edited during an hour's fetch lost its result to a foreign-key
    failure, because the run stored the file as it stood after the fetch under the sha it had
    validated before it."""
    from hashlib import sha256

    from kanso.screen import data

    path = write_screen(loaded, OU)
    validated = path.read_bytes()
    fetch = data.fetch

    def edit_then_fetch(*args: Any, **kwargs: Any) -> Any:
        path.write_text(path.read_text(encoding="utf-8") + "# edited mid-run\n", encoding="utf-8")
        return fetch(*args, **kwargs)

    monkeypatch.setattr(data, "fetch", edit_then_fetch)

    result = at(runner, loaded, "screen", "run", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["sha"] == sha256(validated).hexdigest()
    with StateStore(find(loaded).path("state.db")) as store:
        assert store.get_blob(sha256(validated).hexdigest()) == validated


def test_show_reads_every_result_back_newest_first(runner: CliRunner, loaded: Path) -> None:
    run_screen(runner, loaded)
    run_screen(runner, loaded, {**OU, "title": "the same question, other bytes"})

    listed = payload(at(runner, loaded, "screen", "show", "ou_rev", "--json"))
    assert len(listed["results"]) == 2
    assert listed["results"][0]["created_at"] >= listed["results"][1]["created_at"]
    every = payload(at(runner, loaded, "screen", "show", "--json"))
    assert [item["screen"] for item in every["screens"]] == ["ou_rev"]
    cell = payload(
        at(runner, loaded, "screen", "show", "ou_rev", "--cell", "lead_lag/a>a/1h", "--json")
    )
    assert cell["key"] == "lead_lag/a>a/1h" and cell["judged"] == "pass"
    assert {item["key"] for item in listed["cells"]} == {"lead_lag/a>a/1h", "lead_lag/a>a/2h"}
    human = at(runner, loaded, "screen", "show", "ou_rev")
    assert "worth a lane: no · 1 pass" in human.stdout
    assert "cells" in human.stdout and "pass · lead_lag/a>a/1h" in human.stdout
    assert "ou_rev" in at(runner, loaded, "screen", "show").stdout
    detail = at(runner, loaded, "screen", "show", "ou_rev", "--cell", "lead_lag/a>a/1h").stdout
    assert "pass · lead_lag/a>a/1h" in detail and "share the sign" in detail


def test_a_response_cell_in_full_says_what_its_p_and_its_folds_measure(
    runner: CliRunner, loaded: Path
) -> None:
    run_screen(runner, loaded, FADE)

    detail = at(runner, loaded, "screen", "show", "ou_fade", "--cell", FADE_CELL).stdout

    assert "drift-adjusted" in detail and "(what p tests)" in detail
    assert "(net bp a day)" in detail


FADE_CELL = "response/a/40bp/1h/a/1h"


def test_show_refuses_what_it_does_not_hold(runner: CliRunner, loaded: Path) -> None:
    assert "none measured" in at(runner, loaded, "screen", "show").stdout
    unknown = at(runner, loaded, "screen", "show", "nothing_here", "--json")
    assert unknown.exit_code == Exit.VALIDATION
    assert "has no recorded result" in payload(unknown)["error"]
    run_screen(runner, loaded)
    cell = at(runner, loaded, "screen", "show", "ou_rev", "--cell", "nope", "--json")
    assert cell.exit_code == Exit.VALIDATION
    assert "is not a cell" in payload(cell)["error"]
    assert payload(cell)["remedy"] == "list them with `kanso screen show ou_rev`"


def test_a_failing_cell_names_every_clause_it_missed() -> None:
    from kanso.schemas.screen import ScreenVerdict
    from kanso.screen.run import _judged

    from .test_screen_draft import result_of

    cell = result_of(Screen.model_validate(FADE), FADE_CELL).cells[0]
    verdict = ScreenVerdict(alpha=0.005, min_margin_bp=10, min_events_per_day=5, min_sessions=12)

    judged = _judged(verdict, cell)

    assert judged.judged == "fail"
    assert judged.reason is not None
    assert [clause.split()[0] for clause in judged.reason.split("; ")] == ["p", "margin", "1"]


def test_a_screen_with_no_verdict_judges_nothing(runner: CliRunner, loaded: Path) -> None:
    document = run_screen(runner, loaded, {k: v for k, v in OU.items() if k != "verdict"})

    assert document["verdict"] == {
        "declared": False,
        "worth_a_lane": None,
        "passed": 0,
        "failed": 0,
        "thin": 0,
        "best": [],
    }
    assert "judged" not in document["best"][0]
    human = at(runner, loaded, "screen", "run", write_screen(loaded, {**OU, "verdict": None}))
    assert human.exit_code == Exit.OK and "none declared" in human.stdout


def test_a_cell_short_of_sessions_is_thin_and_a_weak_one_fails(
    runner: CliRunner, loaded: Path
) -> None:
    verdict = {**OU["verdict"], "alpha": 0.00005, "min_sessions": 64}
    document = run_screen(runner, loaded, {**OU, "verdict": verdict})

    with StateStore(find(loaded).path("state.db")) as store:
        (result,) = [
            ScreenResult.model_validate(json.loads(row[0]))
            for row in store.connection.execute("SELECT result FROM screen_results")
        ]
    judged = {cell.key: (cell.judged, cell.reason) for cell in result.cells}
    verdict_of_1h, reason_of_1h = judged["lead_lag/a>a/1h"]
    assert verdict_of_1h == "fail" and str(reason_of_1h).endswith("above alpha 5e-05")
    assert judged["lead_lag/a>a/2h"][0] == "thin"
    assert "under min_sessions 64" in str(judged["lead_lag/a>a/2h"][1])
    assert document["verdict"]["thin"] == 1


def test_a_free_window_on_data_a_hypothesis_certifies_on_is_refused(
    runner: CliRunner, registered: Path
) -> None:
    path = write_screen(registered, {**OU, "window": {"start": "2024-04-15", "end": "2024-04-30"}})

    result = at(runner, registered, "screen", "run", path, "--json")

    assert result.exit_code == Exit.PRECONDITION
    error = payload(result)
    assert "demo_mr certifies on" in error["error"] and INSTRUMENT in error["error"]
    assert "end the window on or before 2024-03-31" in error["remedy"]


def test_a_free_window_clear_of_every_certification_span_runs(
    runner: CliRunner, registered: Path
) -> None:
    assert run_screen(runner, registered)["cells"] == 2


def test_a_bound_screen_is_held_to_its_own_windows_and_not_refused_here(registered: Path) -> None:
    ws = find(registered)
    late = {**OU, "id": "late_one", "window": {"start": "2024-04-15", "end": "2024-04-30"}}
    with StateStore(ws.path("state.db")) as store:
        valid = validate(ws, store, write_screen(registered, late))
        bound = valid.screen.model_copy(update={"hyp": "demo_mr", "window": None})
        embargo.refuse_certification_data(ws, store, bound, valid.window)


def test_a_series_no_adapter_serves_is_refused_before_anything_is_read(
    runner: CliRunner, loaded: Path
) -> None:
    book = {**OU, "legs": {"a": {"instrument": INSTRUMENT, "type": "book"}}}
    book["clock"] = {"grid": "1h", "hours": "overlap"}

    result = at(runner, loaded, "screen", "run", write_screen(loaded, book), "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no registered adapter serves DEMO.SIM as book" in payload(result)["error"]
    assert "workspace extension" in payload(result)["remedy"]


def test_an_instrument_no_definition_resolves_is_refused_with_the_resolve_command(
    runner: CliRunner, loaded: Path
) -> None:
    other = {**OU, "legs": {"a": {"instrument": "NOPE.SIM", "type": "bar", "resolution": "1h"}}}

    result = at(runner, loaded, "screen", "run", write_screen(loaded, other), "--json")

    # Not "build an adapter": the instrument may be served already, and resolving it under
    # the key its vendor files is what the operator has to do.
    assert result.exit_code == Exit.PRECONDITION
    assert "no definition of NOPE.SIM resolves" in payload(result)["error"]
    assert "`kanso data instruments resolve NOPE`" in payload(result)["remedy"]
    assert "workspace extension" not in payload(result)["remedy"]


def test_a_snapshot_that_does_not_cover_is_not_pinned(
    runner: CliRunner, loaded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = find(loaded)
    for manifest in ws.path("catalog", "manifests").glob("*.yaml"):
        text = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        text["publication"] = "unknown"
        manifest.write_text(yaml.safe_dump(text, sort_keys=False), encoding="utf-8")

    result = at(runner, loaded, "screen", "run", write_screen(loaded, OU), "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no snapshot covers DEMO.SIM bar 1h" in payload(result)["error"]


def test_a_snapshot_whose_definitions_moved_is_refused_by_name(
    runner: CliRunner, loaded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kanso.data.snapshot import InstrumentDrift

    monkeypatch.setattr(
        "kanso.screen.pin.instrument_drift",
        lambda ws, snapshot: InstrumentDrift(snapshot.snapshot_id, "a" * 64, "b" * 64),
    )

    result = at(runner, loaded, "screen", "run", write_screen(loaded, OU), "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "the store now holds" in payload(result)["error"]


def test_a_bound_screen_reads_its_hypothesis_s_research_window_and_records_its_pin(
    runner: CliRunner, registered: Path
) -> None:
    bound = {key: value for key, value in OU.items() if key != "window"}
    document = run_screen(runner, registered, {**bound, "id": "mr_bound", "hyp": "demo_mr"})

    assert document["hyp"] == "demo_mr"
    assert document["window"] == ["2024-01-02", "2024-03-29"]
    rendered = parse_yaml(ScreenResult, Path(document["result"]).read_text(encoding="utf-8"))
    with StateStore(find(registered).path("state.db")) as store:
        (pin,) = store.connection.execute(
            "SELECT hypothesis_sha FROM hypotheses WHERE hyp_id = 'demo_mr'"
        ).fetchone()
    assert rendered.hypothesis_sha == pin


def test_a_hypothesis_on_other_instruments_does_not_hold_a_screen_back(registered: Path) -> None:
    ws = find(registered)
    other = {
        **OU,
        "id": "other_names",
        "window": {"start": "2024-04-15", "end": "2024-04-30"},
        "legs": {"a": {"instrument": "OTHR.SIM", "type": "bar", "resolution": "1h"}},
    }
    with StateStore(ws.path("state.db")) as store:
        valid = validate(ws, store, write_screen(registered, other))
        embargo.refuse_certification_data(ws, store, valid.screen, valid.window)


def test_a_refusal_names_each_series_as_the_store_files_it() -> None:
    from kanso.screen.pin import _spelled

    assert _spelled(("BTC-USDT-SWAP.OKX", "trade", None)) == "BTC-USDT-SWAP.OKX trade"
    assert _spelled((INSTRUMENT, "bar", "1h")) == "DEMO.SIM bar 1h"


def test_hy_without_a_grid_reads_the_same_reversion(runner: CliRunner, loaded: Path) -> None:
    hy = {**OU["measures"][0], "estimator": "hy", "lags": ["1h"]}
    document = run_screen(runner, loaded, {**OU, "clock": {"hours": "overlap"}, "measures": [hy]})

    best = document["best"][0]
    assert best["key"] == "lead_lag/a>a/1h" and best["judged"] == "pass"
    assert best["mean"] < -0.1 and best["staleness"] == {}
    assert best["clock_bound"] is False


def test_a_sub_second_lead_between_clocks_that_differ_is_clock_bound() -> None:
    from kanso.screen.lead_lag import CellKey
    from kanso.screen.run import _clock_bound

    spec = Screen.model_validate(
        {
            **OU,
            "legs": {
                "a": {"instrument": "A.OKX", "type": "trade"},
                "b": {"instrument": "B.XNAS", "type": "trade"},
            },
            "derived": {"g": {"gap": {"a": "a", "b": "b"}}},
            "clock": {"hours": "overlap"},
            "measures": [
                {"id": "lead_lag", "from": "a", "to": "b", "estimator": "hy", "lags": ["5ms"]}
            ],
        }
    )
    fast = CellKey("k", "a", "b", "5ms")
    slow = CellKey("k", "a", "b", "1s")
    same = {"a": "exchange", "b": "exchange"}
    assert _clock_bound(spec, fast, {"a": "exchange", "b": "consolidated_tape"}) is True
    assert _clock_bound(spec, fast, same) is False
    assert _clock_bound(spec, fast, {"a": "exchange"}) is True
    assert _clock_bound(spec, slow, {"a": "exchange", "b": "consolidated_tape"}) is False
    assert _clock_bound(spec, CellKey("k", "g", "g", "-5ms"), {**same, "b": "mixed"}) is True


FADE: dict[str, Any] = {
    **OU,
    "id": "ou_fade",
    "clock": {"hours": "overlap"},
    "costs": {
        "SIM": {"commission_bps": 0.5, "slippage_bps": 1, "spread": "fixed_bps", "fixed_bps": 2}
    },
    "measures": [
        {
            "id": "response",
            "trigger": {"leg": "a", "move_bp": [40], "within": "1h"},
            "followers": "a",
            "side": "against",
            "horizons": ["1h"],
            "latency_ms": 0,
        }
    ],
    "verdict": {"alpha": 0.05, "min_margin_bp": 1, "min_events_per_day": 0.5, "min_sessions": 20},
}


def test_fading_a_move_of_the_reverting_path_clears_the_round_trip(
    runner: CliRunner, loaded: Path
) -> None:
    document = run_screen(runner, loaded, FADE)

    assert document["verdict"]["worth_a_lane"] is True
    best = document["best"][0]
    stats = best["response"]
    assert best["key"] == "response/a/40bp/1h/a/1h" and best["judged"] == "pass"
    assert stats["hurdle_bp"] == pytest.approx(5.0)
    assert stats["margin_bp"] == pytest.approx(stats["gross_bp"] - 5.0)
    assert stats["gross_bp"] > 5.0 and stats["ceiling_bp_day"] > 0
    assert stats["events_per_day"] > 0.5
    human = at(runner, loaded, "screen", "show", "ou_fade", "--cell", best["key"])
    assert "ceiling" in human.stdout and "hurdle 5" in human.stdout


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        ({"min_margin_bp": 500}, "under min_margin_bp 500"),
        ({"min_events_per_day": 50}, "events a day, under min_events_per_day 50"),
    ],
)
def test_a_response_short_of_its_margin_or_its_events_fails_naming_which(
    runner: CliRunner, loaded: Path, verdict: dict[str, Any], reason: str
) -> None:
    document = run_screen(runner, loaded, {**FADE, "verdict": {**FADE["verdict"], **verdict}})

    assert document["verdict"]["worth_a_lane"] is False
    cell = document["best"][0]
    assert cell["judged"] == "fail" and reason in cell["reason"]


def test_validate_prints_the_model_the_hurdle_is_struck_under(
    runner: CliRunner, loaded: Path
) -> None:
    result = at(runner, loaded, "screen", "validate", write_screen(loaded, FADE), "--json")

    assert result.exit_code == Exit.OK, result.stdout
    hurdle = payload(result)["hurdles"]["SIM"]
    assert hurdle["costs"]["fixed_bps"] == 2 and hurdle["origin"] == "screen"
    human = at(runner, loaded, "screen", "validate", write_screen(loaded, FADE)).stdout
    assert "spread fixed 2 bp" in human and "sale fees 0 bp · from screen" in human


def test_a_follower_with_no_spread_to_charge_is_refused(runner: CliRunner, loaded: Path) -> None:
    result = at(
        runner,
        loaded,
        "screen",
        "validate",
        write_screen(loaded, {**FADE, "costs": None}),
        "--json",
    )

    assert result.exit_code == Exit.VALIDATION
    assert "fixed_bps" in payload(result)["remedy"] and "in the screen" in payload(result)["remedy"]


def test_a_bound_screen_is_charged_its_hypothesis_s_costs(
    runner: CliRunner, registered: Path
) -> None:
    bound = {key: value for key, value in FADE.items() if key not in ("window", "costs")}
    document = run_screen(runner, registered, {**bound, "id": "mr_fade", "hyp": "demo_mr"})

    assert document["best"][0]["response"]["hurdle_bp"] == pytest.approx(5.0)


def test_a_model_that_takes_its_spread_from_quotes_cannot_price_a_bar_follower(
    runner: CliRunner, loaded: Path
) -> None:
    mixed = {
        **FADE,
        "costs": None,
        "legs": {**FADE["legs"], "b": {"instrument": INSTRUMENT, "type": "quote"}},
        "groups": {"both": ["a", "b"]},
        "measures": [{**FADE["measures"][0], "followers": "both"}],
    }

    result = at(runner, loaded, "screen", "validate", write_screen(loaded, mixed), "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "takes the spread from quotes" in payload(result)["error"]
