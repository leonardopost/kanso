"""What a certificate composes into: the two shapes, the record it leaves, the refusals."""

from __future__ import annotations

import pytest

from kanso.errors import PreconditionError
from kanso.schemas import Certificate, StrategyFile, load_yaml
from kanso.state import StateStore
from kanso.strategy import composition, files
from kanso.strategy.composition import compose
from kanso.workspace import Workspace
from tests.research.conftest import DOCUMENT, HYP_ID

from .conftest import (
    ALLOWING,
    BLOCKING,
    FILTER_DOCUMENT,
    FILTER_ID,
    VARYING,
    a_certificate,
    certified_filter,
)


def test_a_sleeve_composes_a_new_strategy_at_version_one(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)

    assert version.version == 1
    assert version.state == "composed"
    assert version.sleeve.hyp_id == HYP_ID
    assert version.sleeve.strategy_sha == sleeve.strategy_sha
    assert version.attached == []
    held = files.require(ws, HYP_ID)
    assert held.id == HYP_ID
    assert [v.version for v in held.versions] == [1]


def test_the_version_pins_what_the_certificate_was_made_under(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)

    assert version.pins.nautilus_version == sleeve.nautilus_version
    assert version.pins.criteria_version == sleeve.criteria_version
    assert version.pins.plan_version == sleeve.plan_version
    assert version.pins.snapshot_id == sleeve.snapshot_id
    assert version.pins.venue_model == sleeve.venue_model


def test_the_expectation_is_measured_over_the_sleeves_certification_window(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    expectation = compose(ws, store, HYP_ID).expectation

    assert expectation.objective_id == "wf_sharpe_net"
    assert expectation.value > 0, "the saw-tooth pays in the certification window"
    assert expectation.window.start.isoformat() == "2024-02-06"
    assert expectation.window.end.isoformat() == "2024-02-29"
    assert expectation.ci90[0] < expectation.ci90[1], "a resampling, not a point estimate"
    assert expectation.mdd_p95 >= 0


def test_an_attached_construct_composes_the_hosts_next_version(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    first = compose(ws, store, HYP_ID)
    attached = certified_filter(ws, store)

    second = compose(ws, store, FILTER_ID)

    assert second.version == 2
    assert second.sleeve == first.sleeve, "the sleeve is the host's, unchanged"
    assert [ref.hyp_id for ref in second.attached] == [FILTER_ID]
    assert second.attached[0].construct == "filter"
    assert second.attached[0].strategy_sha == attached.strategy_sha
    assert second.attached[0].params == {"scope": "time"}
    assert [v.version for v in files.require(ws, HYP_ID).versions] == [1, 2]
    assert files.read(ws, FILTER_ID) is None, "a filter is not a strategy of its own"


def test_a_neutral_modifier_leaves_the_hosts_expectation_where_it_was(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    first = compose(ws, store, HYP_ID)
    certified_filter(ws, store)

    second = compose(ws, store, FILTER_ID)

    assert second.expectation.value == pytest.approx(first.expectation.value)
    assert second.expectation.mdd_p95 == pytest.approx(first.expectation.mdd_p95)


def test_a_refusing_modifier_is_consulted_and_costs_the_host_everything(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    first = compose(ws, store, HYP_ID)
    certified_filter(ws, store, source=BLOCKING)

    second = compose(ws, store, FILTER_ID)

    assert first.expectation.value > 0
    assert second.expectation.value == 0.0, "a filter that refuses every entry never trades"
    assert second.expectation.mdd_p95 == 0.0
    assert second.expectation.ci90 == (0.0, 0.0), "nothing to resample, so no width invented"


def test_composing_the_same_sleeve_twice_returns_the_version_already_made(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    first = compose(ws, store, HYP_ID)

    again = compose(ws, store, HYP_ID)

    assert again == first
    assert len(files.require(ws, HYP_ID).versions) == 1


def test_composing_the_same_attached_construct_twice_returns_the_version_already_made(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    certified_filter(ws, store)
    first = compose(ws, store, FILTER_ID)

    again = compose(ws, store, FILTER_ID)

    assert again == first
    assert [v.version for v in files.require(ws, HYP_ID).versions] == [1, 2]


def test_a_second_sleeve_certificate_of_a_composed_strategy_is_refused(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    a_certificate(
        ws,
        store,
        HYP_ID,
        b"# a second, different sleeve\n" + VARYING,
        construct={"id": "sleeve"},
        objective_id="wf_sharpe_net",
    )

    with pytest.raises(PreconditionError, match="version 1"):
        compose(ws, store, HYP_ID)


def test_a_hypothesis_whose_certificates_all_failed_cannot_compose(
    ws: Workspace, store: StateStore
) -> None:
    a_certificate(
        ws,
        store,
        HYP_ID,
        VARYING,
        construct={"id": "sleeve"},
        objective_id="wf_sharpe_net",
        doc=DOCUMENT,
        verdict="fail",
    )

    with pytest.raises(PreconditionError, match="no passing certificate"):
        compose(ws, store, HYP_ID)


def test_an_attached_construct_naming_no_host_is_refused(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    a_certificate(
        ws,
        store,
        FILTER_ID,
        ALLOWING,
        construct={"id": "filter"},
        objective_id="marginal_wf_sharpe",
        doc=FILTER_DOCUMENT,
    )

    with pytest.raises(PreconditionError, match="names no host"):
        compose(ws, store, FILTER_ID)


def test_the_version_is_indexed_and_the_composition_is_an_event(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)

    rows = store.connection.execute(
        "SELECT strategy_id, version, state, stage, capital FROM strategy_versions"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(HYP_ID, 1, "composed", None, None)]
    assert store.connection.execute("SELECT strategy_id FROM strategies").fetchone()[0] == HYP_ID
    events = store.events(kind=composition.COMPOSED, subject=HYP_ID)
    assert len(events) == 1
    assert events[0].detail["version"] == 1
    assert events[0].detail["objective"] == version.expectation.objective_id


def test_the_written_file_reads_back_as_the_version_it_was_written_from(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)

    reread = load_yaml(StrategyFile, files.strategy_file(ws, HYP_ID))

    assert reread.latest() == version


def test_a_strategy_this_workspace_never_composed_is_refused_and_listed_as_nothing(
    ws: Workspace, store: StateStore
) -> None:
    assert files.strategies(ws) == []
    assert files.read(ws, HYP_ID) is None
    with pytest.raises(PreconditionError, match="not a composed strategy"):
        files.require(ws, HYP_ID)


def test_every_composed_strategy_is_listed_in_id_order(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)

    assert [held.id for held in files.strategies(ws)] == [HYP_ID]
