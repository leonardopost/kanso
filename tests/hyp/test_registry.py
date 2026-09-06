"""Registration: the pin, the status a re-pin keeps, and what clears the best.

A registration is refused outright while a run is active, because the run is pinned to
the bytes registered when it began.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import pytest

from kanso import hyp
from kanso.errors import Exit, KansoError
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.hyp.conftest import (
    DOCUMENT,
    FILTER_CLASSIFICATION,
    HOST_ID,
    HYP_ID,
    SLEEVE_CLASSIFICATION,
    begin_run,
    document,
    write_hypothesis,
    write_strategy,
)


def register(ws: Workspace, store: StateStore, doc: dict[str, Any] = DOCUMENT) -> Any:
    """Write and register a document, as `kanso hyp add` does."""
    return hyp.add(ws, store, write_hypothesis(ws, doc))


def record(ws: Workspace, store: StateStore, hyp_id: str = HYP_ID) -> hyp.Registration:
    found = hyp.show(ws, store, hyp_id)
    assert isinstance(found, hyp.Registration)
    return found


def set_best(store: StateStore, sha: str = "c" * 64, metric: float = 1.5) -> None:
    """Record a best card, as a keep would."""
    store.connection.execute(
        "UPDATE hypotheses SET best_sha = ?, best_metric = ?, best_run_id = 'r1' WHERE hyp_id = ?",
        (sha, metric, HYP_ID),
    )


# -- the first registration ----------------------------------------------------


def test_registering_pins_the_file_by_the_sha256_of_its_bytes(
    ws: Workspace, store: StateStore
) -> None:
    path = write_hypothesis(ws, DOCUMENT)

    hyp.add(ws, store, path)

    assert record(ws, store).hypothesis_sha == sha256(path.read_bytes()).hexdigest()


def test_registering_stores_the_bytes_so_the_pin_can_be_read_back(
    ws: Workspace, store: StateStore
) -> None:
    path = write_hypothesis(ws, DOCUMENT)

    hyp.add(ws, store, path)

    assert store.get_blob(record(ws, store).hypothesis_sha or "") == path.read_bytes()


def test_a_first_registration_is_a_draft(ws: Workspace, store: StateStore) -> None:
    register(ws, store)

    assert record(ws, store).status == "draft"


def test_registering_records_the_classification_ids(ws: Workspace, store: StateStore) -> None:
    register(ws, store, document(**SLEEVE_CLASSIFICATION))

    found = record(ws, store)
    assert found.construct == "sleeve"
    assert found.objective == "net_edge_bps"


def test_registering_pins_the_scope_a_best_is_comparable_under(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store)

    assert record(ws, store).pins == {
        "hypothesis_sha": record(ws, store).hypothesis_sha,
        "universe": ["DEMO"],
        "resolution": "1m",
        "data_requirements": ["bar"],
        "construct": None,
    }


def test_registering_appends_an_event(ws: Workspace, store: StateStore) -> None:
    register(ws, store)

    assert [event.kind for event in store.events(subject=HYP_ID)] == ["registered"]


def test_registering_an_inadmissible_file_registers_nothing(
    ws: Workspace, store: StateStore
) -> None:
    with pytest.raises(KansoError) as failure:
        register(ws, store, document(universe=["NOPE"]))

    assert failure.value.code is Exit.VALIDATION
    assert hyp.show(ws, store) == []


# -- re-registering ------------------------------------------------------------


def test_re_registering_moves_the_pin(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    first = record(ws, store).hypothesis_sha

    register(ws, store, document(title="A better title"))

    assert record(ws, store).hypothesis_sha != first


def test_re_registering_appends_a_repin_event(ws: Workspace, store: StateStore) -> None:
    register(ws, store)

    register(ws, store, document(title="A better title"))

    assert [event.kind for event in store.events(subject=HYP_ID)] == ["registered", "repinned"]


def test_a_re_registration_keeps_the_status_of_a_still_classified_hypothesis(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store, document(**SLEEVE_CLASSIFICATION))
    hyp.set_status(store, HYP_ID, "researching")

    register(ws, store, document(title="A better title", **SLEEVE_CLASSIFICATION))

    assert record(ws, store).status == "researching"


def test_a_re_registration_that_lost_its_classification_returns_to_draft(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store, document(**SLEEVE_CLASSIFICATION))
    hyp.set_status(store, HYP_ID, "researching")

    register(ws, store, DOCUMENT)

    found = record(ws, store)
    assert found.status == "draft"
    assert found.construct is None


def test_a_re_registration_keeps_the_creation_time(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    created = record(ws, store).created_at

    register(ws, store, document(title="A better title"))

    found = record(ws, store)
    assert found.created_at == created
    assert found.updated_at >= created


# -- what clears the best ------------------------------------------------------


def test_a_re_registration_within_the_same_scope_keeps_the_best(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store)
    set_best(store)

    register(ws, store, document(title="A better title"))

    found = record(ws, store)
    assert found.best_sha == "c" * 64
    assert found.best_metric == 1.5


def test_reordering_the_universe_is_not_a_change_of_scope(ws: Workspace, store: StateStore) -> None:
    register(ws, store, document(universe=["DEMO", "EURO"]))
    set_best(store)

    register(ws, store, document(universe=["EURO", "DEMO"]))

    assert record(ws, store).best_sha == "c" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("universe", ["EURO"]),
        ("resolution", "5m"),
        ("data_requirements", ["bar", "quote"]),
    ],
)
def test_a_change_of_scope_clears_the_best(
    ws: Workspace, store: StateStore, field: str, value: Any
) -> None:
    register(ws, store)
    set_best(store)

    register(ws, store, document(**{field: value}))

    found = record(ws, store)
    assert found.best_sha is None
    assert found.best_metric is None
    assert found.best_run_id is None


def test_a_change_of_construct_clears_the_best(ws: Workspace, store: StateStore) -> None:
    """The override path: the operator states another construct and re-pins."""
    write_strategy(ws, HOST_ID)
    register(ws, store, document(**SLEEVE_CLASSIFICATION))
    set_best(store)

    register(ws, store, document(**FILTER_CLASSIFICATION))

    found = record(ws, store)
    assert found.construct == "filter"
    assert found.best_sha is None
    cleared = [event for event in store.events(subject=HYP_ID) if event.kind == "best_cleared"]
    assert [event.detail["reason"] for event in cleared] == [
        "construct changed from 'sleeve' to 'filter'"
    ]


def test_clearing_the_classification_clears_the_best(ws: Workspace, store: StateStore) -> None:
    """A draft has no construct, and the best was earned as one."""
    register(ws, store, document(**SLEEVE_CLASSIFICATION))
    set_best(store)

    register(ws, store, DOCUMENT)

    found = record(ws, store)
    assert found.status == "draft"
    assert found.best_sha is None


def test_a_re_registration_under_the_same_construct_keeps_the_best(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store, document(**SLEEVE_CLASSIFICATION))
    set_best(store)

    register(ws, store, document(title="A better title", **SLEEVE_CLASSIFICATION))

    assert record(ws, store).best_sha == "c" * 64


def test_a_row_pinned_before_the_construct_joined_the_scope_keeps_the_best(
    ws: Workspace, store: StateStore
) -> None:
    """A row from the first release pinned the construct in its column only."""
    register(ws, store, document(**SLEEVE_CLASSIFICATION))
    set_best(store)
    held = store.connection.execute(
        "SELECT pins FROM hypotheses WHERE hyp_id = ?", (HYP_ID,)
    ).fetchone()
    pins = json.loads(held["pins"])
    del pins["construct"]
    store.connection.execute(
        "UPDATE hypotheses SET pins = ? WHERE hyp_id = ?", (json.dumps(pins), HYP_ID)
    )

    register(ws, store, document(title="A better title", **SLEEVE_CLASSIFICATION))

    assert record(ws, store).best_sha == "c" * 64


def test_clearing_the_best_says_so_in_the_event_log(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    set_best(store)

    register(ws, store, document(resolution="5m"))

    events = store.events(subject=HYP_ID)
    assert [event.kind for event in events] == ["registered", "best_cleared", "repinned"]
    assert events[1].detail["reason"] == "resolution changed from '1m' to '5m'"


def test_a_registration_whose_pins_record_no_scope_clears_the_best(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store)
    set_best(store)
    store.connection.execute("UPDATE hypotheses SET pins = '{}' WHERE hyp_id = ?", (HYP_ID,))

    register(ws, store, DOCUMENT)

    assert record(ws, store).best_sha is None


# -- the refusal while a run is active -----------------------------------------


def test_re_registering_is_refused_while_a_run_is_active(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    begin_run(store, HYP_ID)

    with pytest.raises(KansoError) as failure:
        register(ws, store, document(title="A better title"))

    assert failure.value.code is Exit.PRECONDITION
    assert "active run (r1)" in failure.value.message


def test_the_pin_does_not_move_while_a_run_is_active(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    pinned = record(ws, store).hypothesis_sha
    begin_run(store, HYP_ID)

    with pytest.raises(KansoError):
        register(ws, store, document(title="A better title"))

    assert record(ws, store).hypothesis_sha == pinned


def test_an_ended_run_no_longer_refuses_a_registration(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    begin_run(store, HYP_ID)
    store.connection.execute("UPDATE runs SET ended_at = '2026-01-02' WHERE run_id = 'r1'")

    register(ws, store, document(title="A better title"))

    assert record(ws, store).active_run is None


# -- showing -------------------------------------------------------------------


def test_show_without_an_id_lists_every_registration(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    register(ws, store, document(id="another_one"))

    listed = hyp.show(ws, store)

    assert isinstance(listed, list)
    assert [found.hyp_id for found in listed] == ["another_one", HYP_ID]


def test_show_reports_the_active_run(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    begin_run(store, HYP_ID)

    assert record(ws, store).active_run == "r1"


def test_show_notices_a_file_that_has_moved_off_its_pin(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    assert record(ws, store).pinned

    write_hypothesis(ws, document(title="Edited since"))

    found = record(ws, store)
    assert not found.pinned
    assert found.file_sha != found.hypothesis_sha


def test_show_reports_a_missing_file(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    ws.path("hypotheses", HYP_ID, "hypothesis.yaml").unlink()

    found = record(ws, store)
    assert found.file_sha is None
    assert not found.pinned


def test_show_refuses_an_unregistered_id(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(KansoError) as failure:
        hyp.show(ws, store, "never_seen")

    assert failure.value.code is Exit.PRECONDITION
    assert "not a registered hypothesis" in failure.value.message


def test_the_payload_is_one_json_object(ws: Workspace, store: StateStore) -> None:
    register(ws, store, document(**SLEEVE_CLASSIFICATION))

    payload = record(ws, store).payload()

    assert payload["id"] == HYP_ID
    # A file that arrives already classified registers as classified: the operator has
    # stated what the thesis is, and no model is needed to repeat it back.
    assert payload["status"] == "classified"
    assert payload["construct"] == "sleeve"
    assert payload["pinned"] is True
    assert payload["path"] == f"hypotheses/{HYP_ID}/hypothesis.yaml"


# -- retiring ------------------------------------------------------------------


def test_retiring_ends_a_hypothesis(ws: Workspace, store: StateStore) -> None:
    register(ws, store)

    hyp.retire(ws, store, HYP_ID)

    assert record(ws, store).status == "retired"
    assert store.events(subject=HYP_ID)[-1].kind == "retired"


def test_retiring_is_refused_while_a_run_is_active(ws: Workspace, store: StateStore) -> None:
    register(ws, store)
    begin_run(store, HYP_ID)

    with pytest.raises(KansoError) as failure:
        hyp.retire(ws, store, HYP_ID)

    assert failure.value.code is Exit.PRECONDITION
    assert record(ws, store).status == "draft"


def test_retiring_an_unregistered_id_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(KansoError) as failure:
        hyp.retire(ws, store, "never_seen")

    assert failure.value.code is Exit.PRECONDITION


def test_active_run_is_none_without_one(ws: Workspace, store: StateStore) -> None:
    register(ws, store)

    assert hyp.active_run(store, HYP_ID) is None
    hyp.refuse_active_run(store, HYP_ID, "re-pin")
