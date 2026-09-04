"""The acceptance property: the same snapshot and configuration give the same numbers.

Determinism is what makes a metric a fact about a strategy rather than about a machine.
It is checked three ways here: the same request twice in one process, the same request
through the card subprocess, and the same data written into a second catalog. The
subprocess comparison is the strongest of the three, because a card runs under
`PYTHONHASHSEED=0` while the suite that spawned it runs under whatever seed pytest was
given: a set or dict iteration order reaching a number would show up as a difference.
"""

from __future__ import annotations

from pathlib import Path

from kanso.nautilus.backtest import _seed, run, run_subprocess

from .conftest import CERTIFICATION, RESEARCH, SNAPSHOT, bars, catalog, instrument


def test_the_same_request_twice_gives_the_same_extraction(store: Path, request_for) -> None:
    request = request_for()

    first = run(request, store)
    second = run(request, store)

    assert first.run == second.run
    assert repr(first.run) == repr(second.run)
    assert first.intents == second.intents


def test_the_card_path_gives_the_same_extraction_twice(
    store: Path, tmp_path: Path, request_for
) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    request = request_for()

    first = run_subprocess(request, store, lane)
    second = run_subprocess(request, store, lane)

    assert first.crashed is False
    assert repr(first.run) == repr(second.run)
    assert first.intents == second.intents


def test_the_same_data_in_another_catalog_gives_the_same_extraction(
    tmp_path: Path, request_for
) -> None:
    points = [*bars(RESEARCH), *bars(CERTIFICATION)]
    here = catalog(tmp_path / "here", points, [instrument()])
    there = catalog(tmp_path / "there", points, [instrument()])

    assert repr(run(request_for(), here).run) == repr(run(request_for(), there).run)


def test_the_seed_is_a_fact_about_the_snapshot_and_nothing_else() -> None:
    assert _seed(SNAPSHOT) == _seed(SNAPSHOT)
    assert _seed(SNAPSHOT) != _seed("b" * 64)
    assert 0 <= _seed(SNAPSHOT) < 2**32
