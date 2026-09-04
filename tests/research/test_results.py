"""`results.tsv`: rendered from state, so nothing that happens to the file loses history."""

from __future__ import annotations

from kanso.research import loop
from kanso.research.results import RESULTS_FILE, results_file, results_tsv, write_results
from kanso.schemas import RESULTS_HEADER
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import REVERTING, WEAK


def test_a_hypothesis_with_no_cards_renders_its_header_alone(store: StateStore) -> None:
    assert results_tsv(store, "nobody_here") == RESULTS_HEADER + "\n"


def test_the_file_lives_beside_the_hypothesis(ws: Workspace, registered: str) -> None:
    assert results_file(ws, registered) == ws.path("hypotheses", registered, RESULTS_FILE)


def test_every_card_is_a_row_with_the_columns_the_header_names(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    (ws.root / run.dir / "strategy.py").write_bytes(REVERTING)
    kept = loop.card(ws, store, registered, "the trough rule")

    rows = results_tsv(store, registered).splitlines()
    assert rows[0] == RESULTS_HEADER
    columns = rows[2].split("\t")
    assert len(columns) == len(RESULTS_HEADER.split("\t"))
    assert columns[0] == kept.sha7
    assert columns[1] == f"{kept.metric:.6f}"
    assert columns[7] == "keep"


def test_a_deleted_file_comes_back_on_the_next_card(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    results_file(ws, registered).unlink()

    (ws.root / run.dir / "strategy.py").write_bytes(WEAK)
    loop.card(ws, store, registered, "one step of memory")

    rows = results_file(ws, registered).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3, "the baseline is rendered again beside the new card"


def test_rendering_can_be_asked_for_on_its_own(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    loop.begin(ws, store, registered)
    path = write_results(ws, store, registered)
    assert path.read_text(encoding="utf-8") == results_tsv(store, registered)
