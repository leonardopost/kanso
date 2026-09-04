"""Running a certification: the subject, the two windows, the verdict and what it does.

The workspace is the research suite's synthetic saw-tooth, so every backtest here is a
real engine run over real catalog data and no network is touched. The subject is a card
inserted directly rather than researched, because what is under test is what certification
does with a card, not how the card was produced.

The plan is written to `certificates/<hyp>/plan.yaml` by hand: the planner has its own
tests, and pinning a plan is what a first certification does anyway. Every plan here is
one the planner would accept, since the runner reads it through the planner.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Any

import pytest
import yaml
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.certify import certificate, run
from kanso.certify.run import certify, show
from kanso.config import CertifyConfig
from kanso.criteria import library
from kanso.criteria.gates.parity_replay import NO_PARITY
from kanso.data import snapshot as snapshots
from kanso.data.manifest import Manifest, dataset_id, write_manifest
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import set_status
from kanso.inbox import inbox_file, unread
from kanso.replay.record import list_sessions as sessions
from kanso.research import driver, records
from kanso.schemas import (
    Card,
    Certificate,
    CertificationPlan,
    Costs,
    RunRecord,
    VenueModel,
    load_yaml,
    resolve_venue_model,
    write_yaml,
)
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.research.conftest import (
    DOCUMENT,
    FLAT,
    HYP_ID,
    INSTRUMENT,
    RAISING,
    REVERTING,
    classify,
)
from tests.research.test_attached import ALLOWING, FILTER, HOST, compose_host

NOW = datetime(2024, 2, 1, tzinfo=UTC)

CERT_GATES: list[dict[str, Any]] = [
    {
        "id": "embargoed_window",
        "stage": "cert",
        "params": {"min_fraction": 0.0},
        "rationale": "the one out-of-sample test",
    },
    {
        "id": "publication_lag",
        "stage": "cert",
        "params": {"tolerance_s": 0.0},
        "rationale": "availability is the load-bearing invariant",
    },
    {
        "id": "parity_replay",
        "stage": "cert",
        "params": {"ts_ns": 0},
        "rationale": "required; the deployed code path must be the researched one",
    },
]
"""The three cert gates every plan must carry, since all three are structural invariants."""

TAIL_GATES: list[dict[str, Any]] = [
    {
        "id": "paper_forward",
        "stage": "paper",
        "params": {"min_duration": "30d", "horizon_mult": 10.0},
        "rationale": "forward evidence",
    },
    {"id": "live_drift", "stage": "live", "params": {}, "rationale": "the expectation holds"},
]
"""A plan reaches every stage, so every plan here ends with these two."""


# --- building a workspace that has something to certify -----------------------


def write_plan(ws: Workspace, hyp_id: str = HYP_ID, *, gates: Any = None) -> CertificationPlan:
    """Pin a plan for a hypothesis, as a first `cert plan` would."""
    plan = CertificationPlan.model_validate(
        {
            "schema": 1,
            "hyp_id": hyp_id,
            "plan_version": 1,
            "planned_at": NOW,
            "planned_by": "mock",
            "inputs": {
                "hypothesis_sha": "0" * 64,
                "construct": {"id": "sleeve"},
                "data_availability": {"types": ["bar"], "spans": {}},
                "n_trials": 1,
            },
            "gates": [*(CERT_GATES if gates is None else gates), *TAIL_GATES],
            "excluded": [],
        }
    )
    path = certificate.certificates_dir(ws, hyp_id) / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(plan, path)
    return plan


def snapshot_id(ws: Workspace) -> str:
    """The newest snapshot this workspace holds."""
    return max(snapshots.snapshots(ws), key=lambda s: s.created_at).snapshot_id


def a_card(
    ws: Workspace,
    store: StateStore,
    source: bytes,
    *,
    hyp_id: str = HYP_ID,
    document: dict[str, Any] | None = None,
    seq: int = 1,
    metric: float = 1.0,
    best: bool = True,
    status: str = "keep",
) -> str:
    """One recorded card of a closed run, and the run record that pinned its data."""
    pinned = yaml.safe_dump(document or DOCUMENT, sort_keys=False).encode("utf-8")
    run_record = records.insert(
        store,
        RunRecord.model_validate(
            {
                "run_id": f"{hyp_id}-run{seq}",
                "hyp_id": hyp_id,
                "tag": f"20240201-{seq}",
                "lane": "op",
                "dir": f"runs/op/{hyp_id}",
                "base_sha": store.put_blob(source),
                "hypothesis_sha": store.put_blob(pinned),
                "program_sha": store.put_blob(b"# program\n"),
                "snapshot_id": snapshot_id(ws),
                "criteria_version": "0.1.0",
                "card_budget_s": 60.0,
                "baseline_wall_s": 1.0,
                "baseline_peak_mem_gb": 0.5,
                "started_at": NOW,
            }
        ),
    )
    sha = store.put_blob(source)
    records.record_card(
        store,
        run_record,
        Card.model_validate(
            {
                "run_id": run_record.run_id,
                "lane": "op",
                "strategy_sha": sha,
                "metric": metric,
                "metric_se": 0.25,
                "n_trials": seq,
                "n_trades": 4,
                "wall_s": 1.0,
                "peak_mem_gb": 0.5,
                "status": status,
                "desc": "a card",
                "venue_model": venue_model(),
                "created_at": NOW,
            }
        ),
    )
    if best:
        records.set_best(store, run_record, sha, metric)
    records.close(store, run_record)
    return sha


def venue_model() -> VenueModel:
    """The model the demo hypothesis's costs resolve to on its one venue."""
    return resolve_venue_model(
        "XNAS",
        hypothesis_costs=Costs.model_validate(DOCUMENT["costs"]),
        max_leverage=1.0,
        quotes_available=False,
    )


def a_manifest(ws: Workspace, **changes: Any) -> Manifest:
    """One more dataset in the workspace, so a snapshot can be frozen holding it."""
    values: dict[str, Any] = {
        "source": "synthetic",
        "instrument": "OTHER.XNAS",
        "type": "bar",
        "resolution": "1d",
        "span": (date(2024, 1, 1), date(2024, 2, 29)),
        "adjusted": False,
        "row_count": 10,
        "checksum": "c" * 64,
        "publication": "realtime",
    }
    values.update(changes)
    values["dataset_id"] = dataset_id(
        values["instrument"],
        values["type"],
        values["resolution"],
        values["adjusted"],
        values["span"][1],
    )
    manifest = Manifest.model_validate(values)
    write_manifest(ws, manifest)
    return manifest


def with_n_fail(ws: Workspace, limit: int) -> Workspace:
    """The same workspace whose certification policy allows `limit` failures."""
    return replace(ws, config=ws.config.model_copy(update={"certify": CertifyConfig(n_fail=limit)}))


def blob_from(prefix: str) -> bytes:
    """Bytes whose sha256 starts with `prefix`, found by counting rather than by luck."""
    for index in range(1_000_000):  # pragma: no branch - a one-character prefix arrives at once
        candidate = f"# {index}\n".encode()
        if sha256(candidate).hexdigest().startswith(prefix):
            return candidate
    raise AssertionError(f"no blob found for {prefix!r}")  # pragma: no cover - unreachable


# --- a passing certification --------------------------------------------------


def test_a_passing_plan_certifies_the_hypothesis_and_writes_its_bytes_beside_it(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "pass"
    assert made.strategy_sha == sha
    assert made.objective.id == "wf_sharpe_net"
    assert made.objective.value > 0, "the saw-tooth pays in the certification window too"
    path = certificate.certificate_file(ws, made)
    assert path.is_file()
    assert path.name == f"{sha[:7]}-1-p1-e{made.nautilus_version}.yaml"
    beside = certificate.source_file(ws, HYP_ID, sha)
    assert beside.read_bytes() == REVERTING
    assert beside.name == f"{sha[:7]}.py"


def test_a_pass_makes_the_hypothesis_certified_by_way_of_candidate(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    certify(ws, store, HYP_ID)

    moves = [event.kind for event in store.events(subject=HYP_ID)]
    assert moves.index("candidate") < moves.index("certificate") < moves.index("certified")


def test_the_certificate_records_everything_it_was_produced_under(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    plan = write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert made.snapshot_id == snapshot_id(ws)
    assert made.plan_version == plan.plan_version
    assert made.construct.id == "sleeve"
    assert made.venue_model.venue == "XNAS"
    assert made.criteria_version and made.nautilus_version
    assert made.n_trials == records.n_trials(store, HYP_ID) == 1


def test_a_certificate_is_immutable_and_reads_back_as_it_was_written(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert load_yaml(Certificate, certificate.certificate_file(ws, made)) == made
    assert show(ws, store, HYP_ID) == made


def test_the_name_the_refusal_checks_is_the_certificate_own(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert (
        certificate.filename(
            made.strategy_sha, made.n_trials, made.plan_version, made.nautilus_version
        )
        == made.filename()
    )


def test_show_says_nothing_before_the_first_certification(ws: Workspace, store: StateStore) -> None:
    classify(ws, store, DOCUMENT, REVERTING)

    assert show(ws, store, HYP_ID) is None


# --- a failing certification --------------------------------------------------


def test_a_failing_gate_returns_the_hypothesis_to_research(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, FLAT)
    a_card(ws, store, FLAT)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "fail"
    failed = [gate.id for gate in made.gates if not gate.passed]
    assert failed == ["embargoed_window"], "a strategy that never trades earns nothing"
    assert _status(store, HYP_ID) == "researching"
    assert _failures(store, HYP_ID) == 1
    assert not unread(store), "one failure is not an escalation"


def test_the_configured_number_of_failures_ends_the_hypothesis_and_escalates(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, FLAT)
    a_card(ws, store, FLAT)
    write_plan(ws)

    certify(with_n_fail(ws, 1), store, HYP_ID)

    assert _status(store, HYP_ID) == "failed"
    (entry,) = unread(store)
    assert entry.kind == "cert_failed"
    assert entry.subject == HYP_ID
    assert "embargoed_window" in entry.summary
    assert entry.escalation_id in inbox_file(ws).read_text(encoding="utf-8")


def test_a_failing_certificate_hands_its_gates_to_the_next_proposal(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, FLAT)
    a_card(ws, store, FLAT)
    write_plan(ws)

    certify(ws, store, HYP_ID)

    (line,) = driver._failing_gates(store, HYP_ID)
    assert line.startswith("embargoed_window: ")


def test_a_pass_clears_the_failures_a_hypothesis_had_run_up(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    store.connection.execute(
        "UPDATE hypotheses SET consecutive_cert_failures = 2 WHERE hyp_id = ?", (HYP_ID,)
    )

    certify(ws, store, HYP_ID)

    assert _failures(store, HYP_ID) == 0


# --- immutability -------------------------------------------------------------


def test_recertifying_the_same_subject_under_the_same_plan_and_engine_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    certify(ws, store, HYP_ID)

    with pytest.raises(PreconditionError, match="immutable"):
        certify(ws, store, HYP_ID)


def test_a_new_engine_version_makes_it_a_plain_recertification(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    first = certify(ws, store, HYP_ID)

    monkeypatch.setattr(run, "engine_version", lambda: "9.9.9")
    second = certify(ws, store, HYP_ID)

    assert second.nautilus_version == "9.9.9" != first.nautilus_version
    assert second.strategy_sha == first.strategy_sha
    assert certificate.certificate_file(ws, first).is_file()
    assert certificate.certificate_file(ws, second).is_file()
    assert [held.nautilus_version for held in certificate.of(store, HYP_ID)] == [
        "9.9.9",
        first.nautilus_version,
    ]


def test_a_target_file_that_already_exists_is_never_overwritten(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    write_plan(ws)
    target = certificate.certificates_dir(ws, HYP_ID) / certificate.filename(
        sha, 1, 1, run.engine_version()
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not a certificate\n", encoding="utf-8")

    with pytest.raises(PreconditionError, match="already exists"):
        certify(ws, store, HYP_ID)


def test_certified_bytes_are_never_written_over_another_subject_bytes(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING)
    write_plan(ws)
    beside = certificate.source_file(ws, HYP_ID, sha)
    beside.parent.mkdir(parents=True, exist_ok=True)
    beside.write_bytes(b"# somebody else's strategy\n")

    with pytest.raises(PreconditionError, match="share the prefix"):
        certify(ws, store, HYP_ID)


# --- what the pinned data has to support --------------------------------------


def test_a_snapshot_of_unknown_publication_records_a_failing_verdict(
    ws: Workspace, store: StateStore
) -> None:
    a_manifest(ws, publication="unknown")
    snapshots.freeze(ws)
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "fail"
    assert all(not gate.passed for gate in made.gates)
    assert "nobody declared when its points became public" in str(made.gates[0].evidence)
    assert _status(store, HYP_ID) == "researching", "a recorded fail, not an exit code"


def test_a_vendor_adjusted_snapshot_records_a_failing_verdict(
    ws: Workspace, store: StateStore
) -> None:
    a_manifest(ws, adjusted=True, adjustment_basis="split_and_dividend", as_of=date(2024, 3, 1))
    snapshots.freeze(ws)
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "fail"
    assert "vendor-adjusted" in str(made.gates[0].evidence)


def test_a_snapshot_naming_a_dataset_the_workspace_forgot_records_a_failing_verdict(
    ws: Workspace, store: StateStore
) -> None:
    manifest = a_manifest(ws)
    snapshots.freeze(ws)
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    ws.path("catalog", "manifests", f"{manifest.dataset_id}.yaml").unlink()

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "fail"
    assert "no longer describes it" in str(made.gates[0].evidence)


def test_a_snapshot_that_calls_itself_irreproducible_records_a_failing_verdict(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    held = snapshots.read(ws, snapshot_id(ws))
    snapshots.write(ws, held.model_copy(update={"reproducible": False}))

    made = certify(ws, store, HYP_ID)

    assert made.verdict == "fail"
    assert "irreproducible" in str(made.gates[0].evidence)


def test_a_delayed_dataset_is_measured_against_the_delay_its_class_documents(
    ws: Workspace, store: StateStore
) -> None:
    a_manifest(ws, publication="delayed", publication_rule="delayed_quote", type="quote")
    a_manifest(
        ws,
        publication="delayed",
        publication_rule="fundamental",
        type="fundamental",
        resolution=None,
        instrument="OTHER2.XNAS",
    )
    snapshots.freeze(ws)
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    made = certify(ws, store, HYP_ID)

    (lag,) = [gate for gate in made.gates if gate.id == "publication_lag"]
    assert lag.passed, "no point of either delayed set was seen, so none was published early"
    assert lag.evidence["n_datasets"] == 3
    assert lag.evidence["published_too_early"] == []


def test_the_certification_window_supplies_the_lags_and_the_volume_the_gates_read(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(
        ws,
        gates=[
            *CERT_GATES,
            {
                "id": "capacity_vs_adv",
                "stage": "cert",
                "params": {"participation": 0.2, "adv_days": 5},
                "rationale": "a day's trading is a share of a day's volume",
            },
        ],
    )

    made = certify(ws, store, HYP_ID)

    (capacity,) = [gate for gate in made.gates if gate.id == "capacity_vs_adv"]
    assert capacity.skipped is None, "the window's bars carry the volume series"
    assert INSTRUMENT in capacity.evidence["instruments"]
    (lag,) = [gate for gate in made.gates if gate.id == "publication_lag"]
    assert lag.evidence["n_datasets"] == 1


# --- choosing the subject -----------------------------------------------------


def test_the_named_sha_is_certified_rather_than_the_best(ws: Workspace, store: StateStore) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, FLAT, seq=1)
    named = a_card(ws, store, REVERTING, seq=2, best=False)
    write_plan(ws)

    made = certify(ws, store, HYP_ID, sha=named[:10])

    assert made.strategy_sha == named
    assert made.verdict == "pass"


@pytest.mark.parametrize(
    ("sha", "message"),
    [
        ("zzz", "not a strategy sha"),
        ("", "not a strategy sha"),
        ("deadbeef", "no card of"),
    ],
)
def test_a_sha_that_names_no_card_of_this_hypothesis_is_refused(
    ws: Workspace, store: StateStore, sha: str, message: str
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    with pytest.raises(ValidationError, match=message):
        certify(ws, store, HYP_ID, sha=sha)


def test_an_ambiguous_prefix_is_refused_rather_than_resolved(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, blob_from("a"), seq=1)
    a_card(ws, store, blob_from("ab"), seq=2)
    write_plan(ws)

    with pytest.raises(ValidationError, match="is ambiguous within"):
        certify(ws, store, HYP_ID, sha="a")


def test_a_hypothesis_with_no_best_card_has_nothing_to_certify(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    write_plan(ws)

    with pytest.raises(PreconditionError, match="no best card"):
        certify(ws, store, HYP_ID)


@pytest.mark.parametrize("status", ["draft", "failed", "retired"])
def test_a_hypothesis_that_is_over_or_unclassified_is_not_certified(
    ws: Workspace, store: StateStore, status: str
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    set_status(store, HYP_ID, status)  # type: ignore[arg-type]

    with pytest.raises(PreconditionError, match="certification runs on"):
        certify(ws, store, HYP_ID)


def test_a_card_pinned_to_an_unclassified_hypothesis_has_nothing_to_certify_as(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    unclassified = {name: value for name, value in DOCUMENT.items() if name != "construct"}
    a_card(ws, store, REVERTING, document=unclassified)
    write_plan(ws)

    with pytest.raises(PreconditionError, match="without a construct or an objective"):
        certify(ws, store, HYP_ID)


def test_a_subject_that_raises_on_the_certification_window_is_a_refusal_not_a_verdict(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, RAISING)
    write_plan(ws)

    with pytest.raises(PreconditionError, match="raised while certification ran it"):
        certify(ws, store, HYP_ID)

    assert show(ws, store, HYP_ID) is None, "a card that did not run took no test"
    assert _failures(store, HYP_ID) == 0


def test_a_window_the_catalog_cannot_serve_is_reported_as_the_data_problem_it_is(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    unserved = {
        **DOCUMENT,
        "windows": {
            **DOCUMENT["windows"],
            "research": {"start": date(2023, 11, 1), "end": date(2023, 11, 30)},
        },
    }
    a_card(ws, store, REVERTING, document=unserved)
    write_plan(ws)

    with pytest.raises(PreconditionError, match="the catalog holds nothing"):
        certify(ws, store, HYP_ID)


# --- gates the runner cannot run ----------------------------------------------


def test_a_gate_with_no_implementation_is_skipped_rather_than_failed(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)
    without_lag = {name: gate for name, gate in run.gates().items() if name != "publication_lag"}
    monkeypatch.setattr(run, "gates", lambda: without_lag)

    made = certify(ws, store, HYP_ID)

    (lag,) = [gate for gate in made.gates if gate.id == "publication_lag"]
    assert lag.skipped == run.UNIMPLEMENTED
    assert lag.passed, "a gate that judged nothing does not fail a certificate"
    assert made.verdict == "pass"


def test_a_plan_that_does_not_name_the_parity_gate_replays_nothing(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replays are what the gate costs, and a plan that did not ask does not pay it.

    The gate is a structural invariant, so the only plan that omits it is one written while
    this version could not run it. That is what is withheld here.
    """
    offered = {name: item for name, item in library.plannable().items() if name != "parity_replay"}
    monkeypatch.setattr(library, "plannable", lambda: offered)
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws, gates=[gate for gate in CERT_GATES if gate["id"] != "parity_replay"])

    made = certify(ws, store, HYP_ID)

    assert [gate.id for gate in made.gates] == ["embargoed_window", "publication_lag"]
    assert sessions(ws) == [], "nothing replayed, so no session was written"


def test_a_replay_that_cannot_be_set_up_leaves_the_gate_without_its_evidence(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate says nothing compared the paths rather than that they agreed."""
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    def refusing(*_: Any, **__: Any) -> Any:
        raise PreconditionError("the catalog serves nothing for this universe")

    monkeypatch.setattr(run, "replay_parity", refusing)

    made = certify(ws, store, HYP_ID)

    (parity,) = [gate for gate in made.gates if gate.id == "parity_replay"]
    assert parity.skipped == NO_PARITY
    assert parity.passed, "a gate that judged nothing does not fail a certificate"
    assert made.verdict == "pass"


def test_a_planned_deflated_sharpe_consumes_the_trial_count(
    ws: Workspace, store: StateStore
) -> None:
    classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, FLAT, seq=1, metric=0.5, best=False)
    a_card(ws, store, REVERTING, seq=2, metric=1.5)
    a_card(ws, store, b"# a card that crashed\n", seq=3, metric=0.0, best=False, status="crash")
    write_plan(
        ws,
        gates=[
            *CERT_GATES,
            {
                "id": "deflated_sharpe",
                "stage": "cert",
                "params": {"min_dsr": 0.5},
                "rationale": "how wide the search was",
            },
        ],
    )

    made = certify(ws, store, HYP_ID)

    (deflated,) = [gate for gate in made.gates if gate.id == "deflated_sharpe"]
    assert deflated.evidence["n_trials"] == records.n_trials(store, HYP_ID) == 3
    assert deflated.evidence["n_cards"] == 2, "a crash is a trial, but it measured nothing"
    assert made.n_trials == 3


# --- a construct attached to a host -------------------------------------------


def test_an_attached_construct_is_certified_against_its_host(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store)
    classify(ws, store, FILTER, ALLOWING)
    a_card(ws, store, ALLOWING, hyp_id=str(FILTER["id"]), document=FILTER)
    write_plan(ws, str(FILTER["id"]))

    made = certify(ws, store, str(FILTER["id"]))

    assert made.construct.host == HOST
    assert made.objective.id == "marginal_wf_sharpe"
    assert made.objective.value == 0.0, "a filter that allows everything is the host"


def test_certifying_against_a_host_the_workspace_no_longer_holds_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store)
    classify(ws, store, FILTER, ALLOWING)
    a_card(ws, store, ALLOWING, hyp_id=str(FILTER["id"]), document=FILTER)
    write_plan(ws, str(FILTER["id"]))
    shutil.rmtree(ws.path("strategies", HOST))

    with pytest.raises(PreconditionError, match="not a composed strategy"):
        certify(ws, store, str(FILTER["id"]))


# --- the small readings the gates depend on -----------------------------------


def a_quote() -> QuoteTick:
    """One quote of the demo instrument, for the readings that are not about bars."""
    return QuoteTick(
        InstrumentId.from_str(INSTRUMENT),
        Price.from_str("10.00"),
        Price.from_str("10.02"),
        Quantity.from_int(1),
        Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def test_the_instrument_a_point_belongs_to_is_read_off_the_point() -> None:
    assert run._instrument_of(a_quote()) == INSTRUMENT
    assert run._instrument_of(object()) == "", "a market-wide series belongs to no instrument"


def test_only_bars_carry_the_volume_a_capacity_gate_reads() -> None:
    assert run._daily_volume([(a_quote(),)]) == {}, "a quote is not a day's traded notional"


def test_a_data_class_whose_instant_comes_from_the_source_documents_no_delay(
    ws: Workspace,
) -> None:
    derived = a_manifest(ws, publication="delayed", publication_rule="official_close")
    supplied = a_manifest(
        ws, publication="delayed", publication_rule="fundamental", instrument="OTHER3.XNAS"
    )
    realtime = a_manifest(ws, instrument="OTHER4.XNAS")

    assert run._required_lag(derived) == 900.0
    assert run._required_lag(supplied) == 0.0, "a filing's instant is a fact about the filing"
    assert run._required_lag(realtime) == 0.0


def _status(store: StateStore, hyp_id: str) -> str:
    row = store.connection.execute(
        "SELECT status FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return str(row["status"])


def _failures(store: StateStore, hyp_id: str) -> int:
    row = store.connection.execute(
        "SELECT consecutive_cert_failures FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return int(row[0])
