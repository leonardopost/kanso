"""Classification end to end: one scripted call, checked, written, pinned.

The model is the shipped `mock` protocol reading a script from the workspace, so every
attempt, retry and refusal here is the router's real ladder over a real prompt — and the
prompt itself is asserted, because the rule that classification never sees a result is
enforced nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from kanso.classify.classify import classify
from kanso.classify.features import features
from kanso.env import detect
from kanso.env import write as write_envelope
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import STRATEGY_FILE, hypothesis_file, set_status, show, stub
from kanso.models import reset_mock
from kanso.models.call import Call
from kanso.models.mock import MockClient
from kanso.research import begin, records
from kanso.schemas import Card, ModelSpec, RunRecord, resolve_venue_model
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.classify.test_classify_features import (
    COMMENT,
    DRAFT,
    HOST_ID,
    HYP_ID,
    INSTRUMENT,
    MEASURED,
    SECRET,
    certify,
    register,
    script,
    workspace,
    write_hypothesis,
)
from tests.params import pairs


@pytest.fixture(autouse=True)
def _fresh_cursors() -> Iterator[None]:
    """The mock's cursors live for the process, so each test starts its script over."""
    reset_mock()
    yield
    reset_mock()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """The demo workspace with one draft hypothesis in it."""
    return workspace(tmp_path)


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


STATED = DRAFT.split("construct:")[0] + (
    'construct: {id: sleeve, rationale: "stated by the operator"}\n'
    "objective: {id: net_edge_bps, params: {min_delta: 0.0, k_se: 1.0}}\n"
    "constraints: [{id: strategy_integrity, params: {}}]\n"
)
"""The same hypothesis with a classification the operator wrote themselves."""

SHA = "a" * 64


def answer(**changes: Any) -> dict[str, Any]:
    """A usable classification, with these fields replaced."""
    return {
        "construct": {"id": "sleeve"},
        "objective_params": {"min_delta": 0.0, "k_se": 1.0},
        "constraints": [
            {"id": "strategy_integrity", "params": pairs()},
            {"id": "min_trades", "params": pairs({"min": 30})},
        ],
        "rationale": "a complete signal-to-trade thesis with nothing to attach to",
        **changes,
    }


def prompts(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Record every call that reaches the client, prompt and all."""
    seen: list[Call] = []
    complete = MockClient.complete

    def spy(self: MockClient, spec: ModelSpec, call: Call) -> Any:
        seen.append(call)
        return complete(self, spec, call)

    monkeypatch.setattr(MockClient, "complete", spy)
    return seen


def run_record(hyp_id: str = HYP_ID, **changes: Any) -> RunRecord:
    """A run of one hypothesis, so `active` and `best` can be set up without a backtest."""
    return RunRecord.model_validate(
        {
            "run_id": "run1",
            "hyp_id": hyp_id,
            "tag": "20240102-1",
            "lane": "op",
            "dir": f"runs/op/{hyp_id}",
            "base_sha": SHA,
            "hypothesis_sha": SHA,
            "program_sha": SHA,
            "snapshot_id": "snap1",
            "criteria_version": "0.1.0",
            "card_budget_s": 60.0,
            "baseline_wall_s": 1.0,
            "baseline_peak_mem_gb": 0.5,
            "started_at": datetime(2024, 1, 2, tzinfo=UTC),
            **changes,
        }
    )


# --- the answer becomes the file ---------------------------------------------


def test_a_sleeve_is_written_pinned_and_left_readable(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer()])

    classified = classify(ws, store, HYP_ID)

    assert classified.construct is not None
    assert classified.construct.id == "sleeve"
    assert classified.construct.host is None
    assert classified.construct.rationale is not None
    assert classified.objective is not None
    assert classified.objective.id == "net_edge_bps"
    assert classified.objective.params.k_se == 1.0
    assert [one.id for one in classified.constraints or []] == ["strategy_integrity", "min_trades"]

    text = hypothesis_file(ws, HYP_ID).read_text(encoding="utf-8")
    assert COMMENT in text
    assert "one synthetic instrument, resolved manually" in text
    assert text.count("construct:") == 1

    registered = show(ws, store, HYP_ID)
    assert registered.status == "classified"
    assert registered.construct == "sleeve"
    assert registered.objective == "net_edge_bps"
    assert registered.pinned
    assert registered.hypothesis_sha == sha256(text.encode("utf-8")).hexdigest()


def test_the_parameters_the_model_chose_survive_the_wire_whole(
    ws: Workspace, store: StateStore
) -> None:
    """The one failure the pairs shape exists to prevent, pinned.

    An object closed to additional properties is accepted by a provider and answered
    `{}`, so a classification would be written with every parameter the model chose
    silently gone — a filter with no scope, a gate on its default. This asserts they
    arrive, of both types, in the file an operator reads.
    """
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    script(
        ws,
        classify=[
            answer(construct={"id": "filter", "host": HOST_ID, "params": pairs({"scope": "time"})})
        ],
    )

    classified = classify(ws, store, HYP_ID)

    assert classified.construct is not None
    assert classified.construct.params == {"scope": "time"}
    assert {one.id: one.params for one in classified.constraints or ()} == {
        "strategy_integrity": {},
        "min_trades": {"min": 30},
    }
    written = hypothesis_file(ws, HYP_ID).read_text(encoding="utf-8")
    assert "scope: time" in written
    assert "min: 30" in written


def test_the_objective_follows_from_the_horizon_not_from_the_model(
    ws: Workspace, store: StateStore
) -> None:
    daily = DRAFT.replace("horizon: 30m", "horizon: 2d").replace(
        "certification: {start: 2025-01-06", "certification: {start: 2025-01-20"
    )
    register(ws, store, HYP_ID, daily)
    script(ws, classify=[answer()])

    classified = classify(ws, store, HYP_ID)

    assert classified.objective is not None
    assert classified.objective.id == "wf_sharpe_net"


def test_a_second_classification_replaces_the_first(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(), answer(constraints=[{"id": "strategy_integrity"}])])

    classify(ws, store, HYP_ID)
    again = classify(ws, store, HYP_ID)

    assert [one.id for one in again.constraints or []] == ["strategy_integrity"]
    assert hypothesis_file(ws, HYP_ID).read_text(encoding="utf-8").count("min_trades") == 0


def test_a_classification_whose_host_has_gone_can_be_redone(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    path = hypothesis_file(ws, HYP_ID)
    stale = DRAFT.split("construct:")[0] + (
        "construct: {id: filter, host: retired_host}\n"
        "objective: {id: marginal_net_edge_bps, params: {min_delta: 0.0, k_se: 1.0}}\n"
        "constraints: [{id: strategy_integrity, params: {}}]\n"
    )
    path.write_text(stale, encoding="utf-8")
    script(ws, classify=[answer(construct={"id": "filter", "host": HOST_ID})])

    classified = classify(ws, store, HYP_ID)

    assert classified.construct is not None
    assert classified.construct.host == HOST_ID
    assert classified.objective is not None
    assert classified.objective.id == "marginal_net_edge_bps"


def test_a_changed_construct_clears_the_best(ws: Workspace, store: StateStore) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, STATED)
    run = records.insert(store, run_record())
    records.set_best(store, run, SHA, 1.5)
    records.close(store, run)
    script(ws, classify=[answer(construct={"id": "exit", "host": HOST_ID})])

    classify(ws, store, HYP_ID)

    assert records.best_of(store, HYP_ID) == (None, None)
    assert [event.kind for event in store.events(subject=HYP_ID)].count("best_cleared") == 1


def test_a_hypothesis_stripped_to_a_draft_does_not_carry_its_best_into_another_construct(
    ws: Workspace, store: StateStore
) -> None:
    """The route a researched hypothesis takes back to classifiable: strip, add, classify.

    The strip re-pins with no construct, so a classification that then chooses another
    one has nothing on the registry row to compare against; the best has to have gone at
    the strip, or a filter inherits a sleeve's champion and `research begin` takes a
    sleeve into a modifier harness.
    """
    certify(ws, store)
    register(ws, store, HYP_ID, STATED)
    run = records.insert(store, run_record())
    records.set_best(store, run, SHA, 1.5)
    records.close(store, run)
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(construct={"id": "filter", "host": HOST_ID})])

    classify(ws, store, HYP_ID)

    assert records.best_of(store, HYP_ID) == (None, None)
    assert [event.kind for event in store.events(subject=HYP_ID)].count("best_cleared") == 1


def test_an_unchanged_construct_keeps_the_best(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, STATED)
    run = records.insert(store, run_record())
    records.set_best(store, run, SHA, 1.5)
    records.close(store, run)
    script(ws, classify=[answer()])

    classify(ws, store, HYP_ID)

    assert records.best_of(store, HYP_ID) == (SHA, 1.5)


# --- the stub ----------------------------------------------------------------


def test_an_attached_construct_gets_its_modifier_stub(ws: Workspace, store: StateStore) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    script(
        ws,
        classify=[
            answer(construct={"id": "filter", "host": HOST_ID, "params": pairs({"scope": "time"})})
        ],
    )

    classify(ws, store, HYP_ID)

    source = ws.path("hypotheses", HYP_ID, STRATEGY_FILE).read_text(encoding="utf-8")
    assert source == stub(HYP_ID, "filter", HOST_ID)
    assert 'construct = "filter"' in source
    assert HOST_ID in source


def test_an_edited_strategy_is_never_overwritten(ws: Workspace, store: StateStore) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    edited = f"# {SECRET}\n" + stub(HYP_ID)
    ws.path("hypotheses", HYP_ID, STRATEGY_FILE).write_text(edited, encoding="utf-8")
    script(ws, classify=[answer(construct={"id": "filter", "host": HOST_ID})])

    classify(ws, store, HYP_ID)

    assert ws.path("hypotheses", HYP_ID, STRATEGY_FILE).read_text(encoding="utf-8") == edited


def test_a_missing_strategy_file_is_rendered(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, DRAFT)
    ws.path("hypotheses", HYP_ID, STRATEGY_FILE).unlink()
    script(ws, classify=[answer()])

    classify(ws, store, HYP_ID)

    assert ws.path("hypotheses", HYP_ID, STRATEGY_FILE).read_text(encoding="utf-8") == stub(HYP_ID)


def test_a_construct_this_version_cannot_run_is_classified_and_left_alone(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(construct={"id": "alpha", "host": HOST_ID})])

    classified = classify(ws, store, HYP_ID)

    assert classified.construct is not None
    assert classified.construct.id == "alpha"
    assert ws.path("hypotheses", HYP_ID, STRATEGY_FILE).read_text(encoding="utf-8") == stub(HYP_ID)


def test_a_construct_this_version_cannot_run_is_refused_when_a_run_begins(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    write_envelope(ws, detect(ws))
    script(ws, classify=[answer(construct={"id": "alpha", "host": HOST_ID})])
    classify(ws, store, HYP_ID)

    with pytest.raises(PreconditionError) as refused:
        begin(ws, store, HYP_ID)

    assert "not runnable in this version" in refused.value.message
    assert "canonical wrapper" in refused.value.message


# --- what the prompt may and may not carry -----------------------------------


def test_the_prompt_carries_the_idea_and_the_catalogues(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer()])
    seen = prompts(monkeypatch)

    classify(ws, store, HYP_ID)

    assert len(seen) == 1
    call = seen[0]
    assert call.task_class == "classify"
    assert call.tier == "frontier"
    assert call.effort == "high"
    for stated in ("revert to a rolling mean", INSTRUMENT, "30m", "mean_reversion"):
        assert stated in call.system
    for catalogued in ("sleeve", "filter", "allocation", "strategy_integrity", "min_trades"):
        assert catalogued in call.system
    assert "net_edge_bps" in call.system
    assert HOST_ID in call.user


def test_the_prompt_carries_no_result_no_certificate_and_no_source(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    ws.path("hypotheses", HYP_ID, STRATEGY_FILE).write_text(
        f"# {SECRET}\n" + stub(HYP_ID), encoding="utf-8"
    )
    ws.path("hypotheses", HYP_ID, "results.tsv").write_text(
        f"sha7\tmetric\n0123456\t{MEASURED}\n", encoding="utf-8"
    )
    certificates = ws.path("certificates", HOST_ID)
    certificates.mkdir(parents=True)
    (certificates / "abc1234-9-p1-e1.231.0.yaml").write_text(
        f"verdict: pass\nobjective: {{id: net_edge_bps, value: {MEASURED}}}\n", encoding="utf-8"
    )
    kept = store.put_blob(f"# {SECRET}\n".encode())
    run = records.insert(store, run_record())
    records.record_card(
        store,
        run,
        Card.model_validate(
            {
                "run_id": run.run_id,
                "lane": "op",
                "strategy_sha": kept,
                "metric": float(MEASURED),
                "metric_se": 0.25,
                "n_trials": 1,
                "n_trades": 40,
                "wall_s": 1.0,
                "peak_mem_gb": 0.5,
                "status": "keep",
                "desc": SECRET,
                "venue_model": resolve_venue_model("SIM", max_leverage=1.0),
                "created_at": datetime(2024, 1, 2, tzinfo=UTC),
            }
        ),
    )
    records.close(store, run)
    script(ws, classify=[answer()])
    seen = prompts(monkeypatch)

    classify(ws, store, HYP_ID)

    prompt = f"{seen[0].system}\n{seen[0].user}"
    assert SECRET not in prompt
    assert MEASURED not in prompt
    assert "verdict" not in prompt
    assert "6.6666" not in prompt
    assert "mdd_p95" not in prompt
    assert "snap1" not in prompt
    assert "KansoStrategy" not in prompt


# --- the answer is checked before it is believed ------------------------------


def test_a_rejected_answer_is_retried_with_the_reason(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(construct={"id": "nonsense"}), answer()])
    seen = prompts(monkeypatch)

    classified = classify(ws, store, HYP_ID)

    assert classified.construct is not None
    assert classified.construct.id == "sleeve"
    assert len(seen) == 2
    assert seen[0].system == seen[1].system
    assert "is not in the catalogue" in seen[1].user
    assert store.connection.execute("SELECT COUNT(*) c FROM spend").fetchone()["c"] == 2


def test_a_keep_rule_the_wire_no_longer_bounds_is_named_in_the_retry(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`k_se: 0` satisfies the schema kanso sent and is refused by `ObjectiveParams`.

    The bound is stated once, and no longer on the wire: `minimum` is a keyword the
    provider was measured to refuse on a number, so nothing in the document the model
    answers against says `k_se` is positive and this local refusal is the whole of it.
    That makes what the refusal says load-bearing — a complaint naming the field and the
    range is one the next attempt can act on, where `the answer is not a classification`
    would have spent the ladder's one retry saying nothing.
    """
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(objective_params={"min_delta": 0.0, "k_se": 0.0}), answer()])
    seen = prompts(monkeypatch)

    classified = classify(ws, store, HYP_ID)

    assert classified.objective is not None
    assert classified.objective.params.k_se == 1.0
    assert len(seen) == 2
    assert "objective.params.k_se: Input should be greater than 0" in seen[1].user


def test_a_frontier_class_makes_two_attempts_and_then_refuses(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(construct={"id": "nonsense"})])

    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert "in 2 attempts" in refused.value.message
    assert store.connection.execute("SELECT COUNT(*) c FROM spend").fetchone()["c"] == 2


@pytest.mark.parametrize(
    ("given", "complaint"),
    [
        ({"construct": {"id": "nonsense"}}, "is not in the catalogue"),
        (
            {"construct": {"id": "Sleeve"}},
            "the answer is not a classification: id: String should match pattern",
        ),
        ({"construct": {"id": "sleeve", "host": HOST_ID}}, "attaches to nothing"),
        ({"construct": {"id": "filter"}}, "so it names one of demo_sleeve"),
        ({"construct": {"id": "filter", "host": "other"}}, "is not one of demo_sleeve"),
        ({"construct": {"id": "allocation", "host": HOST_ID}}, "is not one of portfolio"),
        (
            {"construct": {"id": "filter", "host": HOST_ID, "params": pairs({"speed": "fast"})}},
            "has no parameter 'speed'",
        ),
        (
            {"construct": {"id": "filter", "host": HOST_ID, "params": pairs({"scope": "price"})}},
            "is not one of time, instrument",
        ),
        (
            {"construct": {"id": "filter", "host": HOST_ID, "params": pairs({"scope": ["time"]})}},
            "expected number or string or boolean, got array",
        ),
        (
            {"objective_params": {"min_delta": 0.0, "k_se": 0.0}},
            "objective.params.k_se: Input should be greater than 0",
        ),
        (
            {"objective_params": {"min_delta": -1.0, "k_se": 0.0}},
            "objective.params.min_delta: Input should be greater than or equal to 0",
        ),
        ({"objective_params": {"min_delta": 0.0, "k_se": 9.0}}, "objective.params.k_se"),
        (
            {"constraints": [{"id": "strategy_integrity"}, {"id": "embargoed_window"}]},
            "runs at the cert stage",
        ),
        (
            {"constraints": [{"id": "strategy_integrity"}, {"id": "no_such_gate"}]},
            "is not a gate in the toolbox",
        ),
        (
            {
                "constraints": [
                    {"id": "strategy_integrity"},
                    {"id": "min_trades", "params": pairs({"min": 30})},
                    {"id": "min_trades", "params": pairs({"min": 40})},
                ]
            },
            "is named twice",
        ),
        (
            {
                "constraints": [
                    {"id": "strategy_integrity"},
                    {"id": "min_trades", "params": pairs({"min": 0})},
                ]
            },
            "outside the range [1, 10000]",
        ),
        (
            {"constraints": [{"id": "min_trades", "params": pairs({"min": 30})}]},
            "strategy_integrity",
        ),
    ],
)
def test_an_unusable_answer_is_refused_with_the_reason(
    ws: Workspace, store: StateStore, given: dict[str, Any], complaint: str
) -> None:
    certify(ws, store)
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(**given)])

    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert complaint in str(refused.value.remedy)


def test_a_construct_with_nothing_to_attach_to_is_refused(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer(construct={"id": "filter", "host": HOST_ID})])

    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert "no certified strategy for it to attach to" in str(refused.value.remedy)


def test_a_construct_no_objective_can_measure_is_refused(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[answer()])

    def blind(ws_: Workspace, store_: StateStore, hyp_: Any) -> Any:
        computed = features(ws_, store_, hyp_)
        computed["objectives"]["absolute"]["selected"] = None
        return computed

    monkeypatch.setattr("kanso.classify.classify.features", blind)

    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert "no absolute objective applies to it" in str(refused.value.remedy)


def test_a_malformed_answer_is_refused(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, DRAFT)
    script(ws, classify=[{"construct": {"id": "sleeve"}}])

    with pytest.raises(PreconditionError):
        classify(ws, store, HYP_ID)


# --- when a hypothesis may be classified at all -------------------------------


def test_an_unregistered_hypothesis_is_not_classified(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert "is not a registered hypothesis" in refused.value.message


def test_a_hypothesis_with_an_active_run_is_not_classified(
    ws: Workspace, store: StateStore
) -> None:
    register(ws, store, HYP_ID, DRAFT)
    records.insert(store, run_record())

    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert "has an active run" in refused.value.message


def test_a_researched_hypothesis_is_not_reclassified(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, STATED)
    set_status(store, HYP_ID, "researching")

    with pytest.raises(PreconditionError) as refused:
        classify(ws, store, HYP_ID)

    assert "is researching" in refused.value.message
    assert refused.value.remedy is not None
    assert "kanso hyp add" in refused.value.remedy


def test_a_hypothesis_that_is_not_text_is_refused(ws: Workspace, store: StateStore) -> None:
    register(ws, store, HYP_ID, DRAFT)
    hypothesis_file(ws, HYP_ID).write_bytes(b"schema: 1\nid: \xff\xfe\n")

    with pytest.raises(ValidationError) as refused:
        classify(ws, store, HYP_ID)

    assert "is not UTF-8 text" in refused.value.message


def test_a_hypothesis_the_workspace_refuses_is_never_sent_to_a_model(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(ws, store, HYP_ID, DRAFT)
    write_hypothesis(ws, HYP_ID, DRAFT.replace(f"universe: [{INSTRUMENT}]", "universe: [GONE.SIM]"))
    seen = prompts(monkeypatch)

    with pytest.raises(ValidationError):
        classify(ws, store, HYP_ID)

    assert seen == []
