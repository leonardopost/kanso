"""The data a screen needs: held, fetchable through an adapter that declares it, or unserved.

A fake adapter here serves the demo's venue through the `synthetic` loader, so a fetch runs
the real backfill end to end with no vendor and no network: the plan names it, the fetch
writes the days the catalog lacked, a snapshot is taken, and the series reads as held.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.data import registry
from kanso.data.loader import Loader, get_loader
from kanso.data.snapshot import snapshots
from kanso.errors import Exit
from kanso.screen import fetch, plan, validate
from kanso.state import StateStore
from kanso.workspace import Workspace, find

from .conftest import INSTRUMENT, LAST, SPEC, at, payload
from .test_screen import FREE, write_screen

JULY = (date(2024, 7, 1), date(2024, 7, 31))


class Synthetic:
    """An adapter that serves the demo's venue through the generator, as a vendor would."""

    id = "aaa_synthetic"
    kind = "data"
    credentials: tuple[str, ...] = ()
    capabilities = registry.packaged()["massive"].capabilities
    timestamps = "exchange"

    def client(self, ws: Workspace) -> object:  # pragma: no cover - never opened here
        raise AssertionError("a screen asks an adapter for nothing but its declarations")

    def configured(self, ws: Workspace) -> bool:
        return True

    def credential_origins(self, ws: Workspace) -> dict[str, str | None]:
        return {}

    def quota(self, ws: Workspace) -> str:
        return "unlimited"

    def loaders(self, ws: Workspace) -> dict[str, Callable[[], Loader]]:
        return {"synthetic": lambda: get_loader("synthetic")}

    def provider(self, ws: Workspace) -> None:
        return None

    def survey(self, ws: Workspace) -> object:  # pragma: no cover - never surveyed here
        raise AssertionError("a screen surveys nothing")

    def serves(self, ws: Workspace, definition: Any, resolution: str | None) -> tuple[str, ...]:
        return ("bar",) if str(definition.id.venue) == "SIM" else ()

    def spec_for(
        self,
        ws: Workspace,
        definition: Any,
        kind: str,
        resolution: str | None,
        start: date,
        end: date,
    ) -> tuple[str, dict[str, object]]:
        symbol = str(definition.id.symbol)
        return "synthetic", {**SPEC, "instruments": [symbol], "start": str(start), "end": str(end)}


class Silent(Synthetic):
    """An adapter that declares nothing a screen asks: it serves nothing here."""

    id = "aaa_silent"
    serves = None  # type: ignore[assignment]


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    real = registry.adapters

    def with_synthetic(extensions: Any = ()) -> dict[str, Any]:
        return {**real(extensions), Silent.id: Silent(), Synthetic.id: Synthetic()}

    monkeypatch.setattr("kanso.screen.data.registry.adapters", with_synthetic)


def july(root: Path, **legs: dict[str, Any]) -> Path:
    window = {"start": str(JULY[0]), "end": str(JULY[1])}
    return write_screen(root, {**FREE, "window": window, "legs": legs or FREE["legs"]})


def test_a_series_the_catalog_serves_over_the_window_is_held(
    runner: CliRunner, loaded: Path
) -> None:
    path = write_screen(loaded)

    result = at(runner, loaded, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    (series,) = payload(result)["data"]
    assert series["legs"] == ["a", "b"]
    assert (series["instrument"], series["type"], series["resolution"]) == (INSTRUMENT, "bar", "1h")
    assert series["state"] == "held"
    assert series["timestamps"] == "unknown"
    assert series["missing"] == []


def test_a_us_equity_the_catalog_lacks_is_fetchable_through_the_tape_vendor(
    runner: CliRunner, loaded: Path
) -> None:
    path = july(loaded)

    result = at(runner, loaded, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    (series,) = payload(result)["data"]
    assert series["state"] == "fetchable"
    assert series["loader"] == "massive_bars" and series["configured"] is False
    assert series["timestamps"] == "consolidated_tape"
    assert series["missing"] == [[str(JULY[0]), str(JULY[1])]]
    human = at(runner, loaded, "screen", "validate", path)
    assert "fetchable · massive_bars via massive (not configured)" in human.stdout


def test_a_series_no_adapter_serves_is_unserved_and_one_never_defined_is_unresolved(
    runner: CliRunner, loaded: Path
) -> None:
    path = july(
        loaded,
        a={"instrument": INSTRUMENT, "type": "book"},
        b={"instrument": "NOPE.SIM", "type": "trade"},
    )

    result = at(runner, loaded, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    book, nope = payload(result)["data"]
    assert book["state"] == "unserved"
    assert "no registered adapter serves DEMO.SIM as book" in book["reason"]
    assert "workspace extension" in book["reason"]
    assert nope["state"] == "unresolved" and nope["defined"] is False
    assert "no definition of NOPE.SIM resolves" in nope["reason"]
    assert "workspace extension" not in nope["reason"]
    human = at(runner, loaded, "screen", "validate", path)
    assert "DEMO.SIM book · unserved" in human.stdout
    assert "NOPE.SIM trade · unresolved" in human.stdout


def test_a_fetch_backfills_what_was_missing_and_snapshots_it(loaded: Path, synthetic: None) -> None:
    ws = find(loaded)
    path = july(loaded)
    with StateStore(ws.path("state.db")) as store:
        valid = validate(ws, store, path)
        before = plan(ws, store, valid.screen, valid.window)
        assert [item.state for item in before] == ["fetchable"]
        assert before[0].adapter == Synthetic.id and before[0].timestamps == "exchange"
        taken = len(snapshots(ws))

        done = fetch(ws, store, valid.screen, valid.window, before)
        again = fetch(
            ws, store, valid.screen, valid.window, plan(ws, store, valid.screen, valid.window)
        )
        after = plan(ws, store, valid.screen, valid.window)

    (fetched,) = done
    assert fetched.rows == 23 * 6 and fetched.requests == 2  # 23 sessions, thirty-day chunks
    assert fetched.spec == ws.path("screens", "demo_lag", "specs", "DEMO.SIM-bar-1h.yaml")
    assert yaml.safe_load(fetched.spec.read_text(encoding="utf-8"))["end"] == str(JULY[1])
    assert len(snapshots(ws)) == taken + 1
    assert again == ()
    assert [item.state for item in after] == ["held"]
    assert after[0].timestamps == "exchange"
    assert fetched.payload()["loader"] == "synthetic"


def test_nothing_to_fetch_fetches_nothing(loaded: Path) -> None:
    ws = find(loaded)
    path = write_screen(loaded)
    with StateStore(ws.path("state.db")) as store:
        valid = validate(ws, store, path)
        held = plan(ws, store, valid.screen, valid.window)
        assert fetch(ws, store, valid.screen, valid.window, held) == ()
    assert not ws.path("screens", "demo_lag", "specs").exists()
    assert JULY[0] > LAST


def test_an_instrument_the_store_lacks_is_resolved_then_fetched(
    loaded: Path, synthetic: None
) -> None:
    entries = yaml.safe_load((loaded / "instruments.yaml").read_text(encoding="utf-8"))
    entries["OTHR.SIM"] = {**entries[INSTRUMENT], "nautilus_id": "OTHR.SIM"}
    (loaded / "instruments.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    other = {"instrument": "OTHR.SIM", "type": "bar", "resolution": "1h"}
    path = write_screen(loaded, {**FREE, "legs": {"a": FREE["legs"]["a"], "b": other}})
    ws = find(loaded)
    with StateStore(ws.path("state.db")) as store:
        valid = validate(ws, store, path)
        before = plan(ws, store, valid.screen, valid.window)
        assert [(item.state, item.defined) for item in before] == [
            ("held", True),
            ("fetchable", False),
        ]
        fetch(ws, store, valid.screen, valid.window, before)
        after = plan(ws, store, valid.screen, valid.window)

    assert [(item.state, item.defined) for item in after] == [("held", True), ("held", True)]


def test_a_run_fetches_what_it_lacks_and_says_what_it_fetched(
    runner: CliRunner, loaded: Path, synthetic: None
) -> None:
    lag = {**FREE["measures"][0], "from": "a", "to": "a", "lags": ["1h"]}
    path = july(loaded, a=FREE["legs"]["a"])
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**document, "measures": [lag]}), encoding="utf-8")

    result = at(runner, loaded, "screen", "run", path)

    assert result.exit_code == Exit.OK, result.stdout
    assert "fetched    DEMO.SIM bar 1h · 2 chunk(s) · 138 rows" in result.stdout


TAPED: dict[str, Any] = {
    **FREE,
    "id": "taped_lag",
    "legs": {"a": FREE["legs"]["a"]},
    "signals": {
        "regime": {"type": "kanso_state_tape", "field": "level", "instrument": INSTRUMENT},
        "event_day": {"type": "kanso_state_tape", "field": "flag", "instrument": INSTRUMENT},
    },
    "measures": [{**FREE["measures"][0], "from": "a", "to": "a", "lags": ["1h"]}],
}
"""A screen reading the state tape beside one leg: two signals of one series."""


def test_a_signal_is_read_as_its_custom_series_held_when_the_catalog_serves_it(
    runner: CliRunner, taped: Path
) -> None:
    result = at(runner, taped, "screen", "validate", write_screen(taped, TAPED), "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    bars, tape = document["data"]
    assert bars["legs"] == ["a"] and bars["state"] == "held"
    assert tape["legs"] == ["regime", "event_day"]
    assert (tape["instrument"], tape["type"], tape["resolution"]) == (
        INSTRUMENT,
        "kanso_state_tape",
        None,
    )
    assert tape["state"] == "held" and tape["missing"] == []
    assert document["signals"]["regime"] == TAPED["signals"]["regime"]
    human = at(runner, taped, "screen", "validate", write_screen(taped, TAPED)).stdout
    assert "regime kanso_state_tape.level on DEMO.SIM" in human
    assert "regime,event_day  DEMO.SIM kanso_state_tape · held" in human


def test_a_signal_no_file_or_adapter_supplies_is_unserved_naming_the_file_load(
    runner: CliRunner, taped: Path
) -> None:
    path = july(taped, a=FREE["legs"]["a"])
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    late = {**TAPED, "window": document["window"]}

    result = at(runner, taped, "screen", "validate", write_screen(taped, late), "--json")

    assert result.exit_code == Exit.OK, result.stdout
    tape = payload(result)["data"][1]
    assert tape["state"] == "unserved"
    assert "kanso data load --loader csv_parquet" in tape["reason"]
    assert "whose `serves` names kanso_state_tape" in tape["reason"]
    refused = at(runner, taped, "screen", "run", write_screen(taped, late), "--json")
    assert refused.exit_code == Exit.PRECONDITION
    assert "kanso data load --loader csv_parquet" in payload(refused)["remedy"]


@pytest.mark.parametrize(
    ("signal", "message", "remedy"),
    [
        ({"type": "kanso_nobody", "field": "level"}, "is not a known data type", "kanso ext show"),
        ({"type": "kanso_state_tape", "field": "label"}, "annotated str", "float, int or bool"),
        ({"type": "kanso_state_tape", "field": "levels"}, "is not a field", "name one of them"),
    ],
)
def test_a_signal_that_is_no_registered_number_is_refused(
    runner: CliRunner, taped: Path, signal: dict[str, str], message: str, remedy: str
) -> None:
    document = {**TAPED, "signals": {"regime": {**signal, "instrument": INSTRUMENT}}}

    result = at(runner, taped, "screen", "validate", write_screen(taped, document), "--json")

    assert result.exit_code == Exit.VALIDATION, result.stdout
    assert message in payload(result)["error"] and remedy in payload(result)["remedy"]


def test_a_bound_screen_reads_only_signals_its_hypothesis_would(
    runner: CliRunner, taped: Path
) -> None:
    from .conftest import HYP_ID, OTHER, write_hypothesis

    assert at(runner, taped, "hyp", "add", write_hypothesis(taped)).exit_code == Exit.OK
    requiring = write_hypothesis(
        taped, id="demo_tape", data_requirements=["bar", "kanso_state_tape"]
    )
    added = at(runner, taped, "hyp", "add", requiring)
    assert added.exit_code == Exit.OK, added.stdout
    bound = {key: value for key, value in TAPED.items() if key != "window"}
    outside = {"regime": {**TAPED["signals"]["regime"], "instrument": OTHER}}

    def checked(hyp: str, **changes: Any) -> Any:
        document = {**bound, "hyp": hyp, **changes}
        return at(runner, taped, "screen", "validate", write_screen(taped, document), "--json")

    admitted = checked("demo_tape")
    universe = checked("demo_tape", signals=outside)
    unrequired = checked(HYP_ID)

    assert admitted.exit_code == Exit.OK, admitted.stdout
    assert universe.exit_code == Exit.VALIDATION
    assert "OTHR.SIM is not in demo_tape's universe" in payload(universe)["error"]
    assert unrequired.exit_code == Exit.VALIDATION
    assert "demo_mr does not require kanso_state_tape data" in payload(unrequired)["error"]
