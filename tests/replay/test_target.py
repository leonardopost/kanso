"""Resolving what to replay, and refusing what cannot be one thing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kanso import strategy
from kanso.errors import PreconditionError, ValidationError
from kanso.replay.target import resolve
from kanso.research import records
from kanso.schemas import Card
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import (
    BLOCKING_FILTER,
    CERTIFICATION,
    FLAT,
    INSTRUMENT,
    REVERTING,
    carded,
    composed,
    document,
    venue_model,
    write_hypothesis,
)


def test_a_hypothesis_resolves_to_its_best_card(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The default subject is the card the research loop kept."""
    target = resolve(ws, store, hyp=carded_hyp)

    assert target.strategy_source == REVERTING
    assert target.label.startswith(f"{carded_hyp}@")
    assert target.universe == (INSTRUMENT,)
    assert target.modifiers == ()


def test_a_hypothesis_takes_its_data_pin_from_the_run(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """What a card was measured against comes from the run that produced it."""
    target = resolve(ws, store, hyp=carded_hyp)
    row = store.connection.execute(
        "SELECT snapshot_id FROM runs WHERE hyp_id = ?", (carded_hyp,)
    ).fetchone()

    assert target.snapshot_id == row["snapshot_id"]
    assert target.venue_model.costs.commission_bps == 0.5


def test_a_named_sha_selects_that_card(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A prefix names one of this hypothesis's cards."""
    sha = store.put_blob(REVERTING)

    target = resolve(ws, store, hyp=carded_hyp, sha=sha[:7])

    assert target.strategy_sha == sha


def test_an_unknown_sha_is_refused(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A prefix matching no card of this hypothesis is a validation failure."""
    with pytest.raises(ValidationError, match="no card of"):
        resolve(ws, store, hyp=carded_hyp, sha="deadbee")


def test_a_sha_that_is_not_hex_is_refused(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A sha is hex; anything else names nothing."""
    with pytest.raises(ValidationError, match="is not a strategy sha"):
        resolve(ws, store, hyp=carded_hyp, sha="not-a-sha")


def test_an_ambiguous_sha_is_refused(ws: Workspace, store: StateStore) -> None:
    """Two cards under one prefix is a question the operator has to answer."""
    hyp_id = carded(ws, store)
    run = records.runs_of(store, hyp_id)[0]
    seen: dict[str, str] = {}
    shared = ""
    for index in range(1000):
        sha = store.put_blob(f"# variant {index}\n".encode() + FLAT)
        records.record_card(store, run, _card(run.run_id, sha, index))
        if sha[0] in seen:
            shared = sha[0]
            break
        seen[sha[0]] = sha

    with pytest.raises(ValidationError, match="is ambiguous within"):
        resolve(ws, store, hyp=hyp_id, sha=shared)


def _card(run_id: str, sha: str, index: int) -> Card:
    """One more card of the same run, so a prefix can name two of them."""
    return Card(
        run_id=run_id,
        lane="op",
        strategy_sha=sha,
        metric=0.0,
        metric_se=0.0,
        n_trials=1,
        n_trades=0,
        wall_s=1.0,
        peak_mem_gb=1.0,
        status="discard",
        desc=f"variant {index}",
        venue_model=venue_model(),
        created_at=datetime(2024, 3, 2, tzinfo=UTC),
    )


def test_a_hypothesis_with_no_best_is_refused(ws: Workspace, store: StateStore) -> None:
    """There is nothing to replay before research has kept anything."""
    from kanso.hyp import add as register

    register(ws, store, write_hypothesis(ws, document(id="demo_empty")))

    with pytest.raises(PreconditionError, match="has no best card"):
        resolve(ws, store, hyp="demo_empty")


def test_an_unclassified_hypothesis_is_refused(ws: Workspace, store: StateStore) -> None:
    """Without a construct there is no arrangement to run the bytes in."""
    doc = document(id="demo_draft")
    for field in ("construct", "objective", "constraints"):
        doc.pop(field)
    hyp_id = carded(ws, store, doc=doc)

    with pytest.raises(PreconditionError, match="has no construct"):
        resolve(ws, store, hyp=hyp_id)


# --- a composed version -------------------------------------------------------


def test_a_strategy_resolves_to_its_latest_version(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A version is named by number, and no number means the newest."""
    composed(ws, store, carded_hyp)

    target = resolve(ws, store, strategy=carded_hyp)

    assert target.version == 1
    assert target.label == f"{carded_hyp}@1"
    assert target.strategy_source == REVERTING


def test_a_strategy_runs_the_generated_implementation(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A stage loads `impl/<version>/`, so that directory is what a replay runs.

    Not the blobs it was copied from: an implementation that has drifted from them is
    exactly what a replay of a version is meant to be running.
    """
    composed(ws, store, carded_hyp)
    edited = REVERTING.replace(b"notional: float = 5_000.0", b"notional: float = 4_000.0")
    manifest = strategy.read_manifest(ws, carded_hyp, 1)
    (strategy.impl_dir(ws, carded_hyp, 1) / manifest.sleeve.source).write_bytes(edited)

    target = resolve(ws, store, strategy=carded_hyp)

    assert target.strategy_source == edited


def test_a_version_carries_its_attached_constructs(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """What a version runs is the sleeve plus everything composed onto it."""
    composed(
        ws,
        store,
        carded_hyp,
        sleeve=FLAT,
        attached=(("demo_filter", "filter", BLOCKING_FILTER, {"allow": False}),),
    )

    target = resolve(ws, store, strategy=carded_hyp)

    assert [construct for construct, _, _ in target.modifiers] == ["filter"]
    assert target.modifiers[0][2] == {"allow": False}


def test_a_version_is_pinned_to_what_it_was_certified_under(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A version's pins are the whole of what it is replayed against."""
    file = composed(ws, store, carded_hyp)

    target = resolve(ws, store, strategy=carded_hyp)

    assert target.snapshot_id == file.latest().pins.snapshot_id
    assert target.venue_model == file.latest().pins.venue_model
    assert target.hyp.windows.certification.end == CERTIFICATION[1]


def test_an_unknown_strategy_is_refused(ws: Workspace, store: StateStore) -> None:
    """A strategy that was never composed cannot be replayed."""
    with pytest.raises(PreconditionError, match="is not a composed strategy"):
        resolve(ws, store, strategy="never_composed")


# --- naming exactly one thing -------------------------------------------------


def test_naming_neither_is_refused(ws: Workspace, store: StateStore) -> None:
    """A replay needs a subject."""
    with pytest.raises(ValidationError, match="name exactly one"):
        resolve(ws, store)


def test_naming_both_is_refused(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A strategy and a hypothesis are two subjects, not one."""
    with pytest.raises(ValidationError, match="name exactly one"):
        resolve(ws, store, strategy=carded_hyp, hyp=carded_hyp)


def test_a_sha_with_a_strategy_is_refused(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A version already names the bytes it runs."""
    with pytest.raises(ValidationError, match="belongs to --hyp"):
        resolve(ws, store, strategy=carded_hyp, sha="abc1234")


def test_a_version_with_a_hypothesis_is_refused(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A hypothesis has cards, not versions."""
    with pytest.raises(ValidationError, match="belongs to --strategy"):
        resolve(ws, store, hyp=carded_hyp, version=1)


# --- an attached construct on its pinned host ---------------------------------


def filter_document(host: str, hyp_id: str = "demo_filter") -> dict[str, object]:
    """A filter hypothesis attached to a composed host."""
    return document(
        id=hyp_id,
        construct={"id": "filter", "host": host, "params": {"scope": "time"}},
        objective={"id": "marginal_wf_sharpe", "params": {"min_delta": 0.0, "k_se": 0.5}},
    )


def test_an_attached_construct_is_replayed_on_its_pinned_host(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A filter is not a strategy: it runs on the host version its run pinned."""
    composed(ws, store, carded_hyp, sleeve=REVERTING)
    hyp_id = carded(
        ws, store, doc=filter_document(carded_hyp), strategy=BLOCKING_FILTER, host_version=1
    )

    target = resolve(ws, store, hyp=hyp_id)

    assert target.strategy_source == REVERTING
    assert [construct for construct, _, _ in target.modifiers] == ["filter"]
    assert target.modifiers[-1][1] == BLOCKING_FILTER
    assert target.modifiers[-1][2] == {"scope": "time"}


def test_a_construct_carries_the_host_versions_own_constructs(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """Everything already on the host runs beneath what is being replayed."""
    composed(
        ws,
        store,
        carded_hyp,
        sleeve=REVERTING,
        attached=(("demo_first", "overlay", BLOCKING_FILTER, {}),),
    )
    hyp_id = carded(
        ws, store, doc=filter_document(carded_hyp), strategy=BLOCKING_FILTER, host_version=1
    )

    target = resolve(ws, store, hyp=hyp_id)

    assert [construct for construct, _, _ in target.modifiers] == ["overlay", "filter"]


def test_a_host_that_is_gone_is_refused(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A construct whose host was never composed has nothing to attach to."""
    composed(ws, store, carded_hyp, sleeve=REVERTING)
    hyp_id = carded(
        ws, store, doc=filter_document(carded_hyp), strategy=BLOCKING_FILTER, host_version=1
    )
    ws.path("strategies", carded_hyp, "strategy.yaml").unlink()

    with pytest.raises(PreconditionError, match="is not a composed strategy"):
        resolve(ws, store, hyp=hyp_id)
