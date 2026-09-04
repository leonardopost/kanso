"""Planning a certification: one scripted call, checked, pinned, and replanned.

The model is the shipped `mock` protocol reading a script from the workspace, so every
attempt, retry and refusal here is the router's real ladder over a real prompt. The prompt
itself is asserted, because the rule that keeps a planner honest — that it never sees the
research it is judging — is enforced nowhere else.

Nothing here resolves a credential or opens a socket: the workspace is the shipped demo,
whose register lists the mock on every tier.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from kanso.certify.plan import (
    PLAN_FILE,
    _ranges,
    availability,
    certificates_dir,
    plan,
    plan_file,
    plannable,
    read_plan,
)
from kanso.data.manifest import Manifest, dataset_id, write_manifest
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import add, hypothesis_dir, show
from kanso.models import reset_mock
from kanso.models.call import Call
from kanso.models.mock import MockClient
from kanso.research import records
from kanso.schemas import (
    Card,
    CriteriaItem,
    Hypothesis,
    ModelSpec,
    RunRecord,
    load_yaml,
    resolve_venue_model,
)
from kanso.state import StateStore
from kanso.workspace import TEMPLATES, Workspace, init

HYP_ID = "demo_mr"
INSTRUMENT = "DEMO.SIM"

SECRET = "SECRET_SIGNAL_ONLY_THE_STRATEGY_KNOWS"
"""A marker in the strategy source, so a prompt carrying source is caught by name."""

MEASURED = "7.7777"
"""A number only a measurement of this hypothesis holds; no prompt may contain it."""

DRAFT = f"""\
schema: 1
id: draft_one
title: "A draft nobody has classified"
thesis: "Prices of {INSTRUMENT} do something."
mechanism: mean_reversion
universe: [{INSTRUMENT}]
horizon: 30m
resolution: 1m
data_requirements: [bar]
costs: {{commission_bps: 0.5, slippage_bps: 1.0, spread: fixed_bps, fixed_bps: 2}}
risk_limits: {{max_position_pct: 20, max_drawdown_pct: 15, max_leverage: 1}}
windows:
  research:      {{start: 2024-01-02, end: 2024-12-31}}
  certification: {{start: 2025-01-06, end: 2025-05-30}}
  forward:       {{start: 2025-06-02}}
"""


@pytest.fixture(autouse=True)
def _fresh_cursors() -> Iterator[None]:
    """The mock's cursors live for the process, so each test starts its script over."""
    reset_mock()
    yield
    reset_mock()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """The demo workspace: a mock register, one classified hypothesis, no network."""
    return init(tmp_path / "ws", demo=True)


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store with the demo hypothesis registered."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        add(ws, opened, ws.path("hypotheses", HYP_ID, "hypothesis.yaml"))
        yield opened


def answer(**changes: Any) -> dict[str, Any]:
    """A usable plan, with these fields replaced."""
    return {
        "gates": [
            {
                "id": "embargoed_window",
                "stage": "cert",
                "params": {"min_fraction": 0.5},
                "rationale": "the only out-of-sample evidence",
            },
            {
                "id": "publication_lag",
                "stage": "cert",
                "params": {"tolerance_s": 0.0},
                "rationale": "availability is the load-bearing invariant",
            },
            {
                "id": "bootstrap",
                "stage": "cert",
                "params": {"n": 1000},
                "rationale": "path risk",
            },
            {
                "id": "paper_forward",
                "stage": "paper",
                "params": {"min_duration": "30d", "horizon_mult": 10.0},
                "rationale": "forward evidence",
            },
            {"id": "live_drift", "stage": "live", "params": {}, "rationale": "drift"},
        ],
        "excluded": [{"id": "deflated_sharpe", "reason": "a per-trade objective, not a Sharpe"}],
        **changes,
    }


def gates_of(*replacements: dict[str, Any], drop: str = "") -> list[dict[str, Any]]:
    """The usable plan's gates, with one dropped and others appended."""
    kept = [gate for gate in answer()["gates"] if gate["id"] != drop]
    return [*kept, *replacements]


def replacing(gate_id: str, **changes: Any) -> list[dict[str, Any]]:
    """The usable plan's gates with one of them altered."""
    return [dict(gate, **changes) if gate["id"] == gate_id else gate for gate in answer()["gates"]]


def script(ws: Workspace, **answers: list[Any]) -> None:
    """Replace the mock model's script of answers, keyed by task class."""
    path = ws.path("mock", "responses.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")


def prompts(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Record every call that reaches the client, prompt and all."""
    seen: list[Call] = []
    complete = MockClient.complete

    def spy(self: MockClient, spec: ModelSpec, call: Call) -> Any:
        seen.append(call)
        return complete(self, spec, call)

    monkeypatch.setattr(MockClient, "complete", spy)
    return seen


def a_card(store: StateStore, *, metric: float, seq: int = 1) -> None:
    """One recorded card, so the hypothesis has a trial count and a measurement."""
    run = records.insert(
        store,
        RunRecord.model_validate(
            {
                "run_id": f"run{seq}",
                "hyp_id": HYP_ID,
                "tag": f"20240102-{seq}",
                "lane": "op",
                "dir": f"runs/op/{HYP_ID}",
                "base_sha": "a" * 64,
                "hypothesis_sha": "a" * 64,
                "program_sha": "a" * 64,
                "snapshot_id": "snap1",
                "criteria_version": "0.1.0",
                "card_budget_s": 60.0,
                "baseline_wall_s": 1.0,
                "baseline_peak_mem_gb": 0.5,
                "started_at": datetime(2024, 1, 2, tzinfo=UTC),
            }
        ),
    )
    sha = store.put_blob(f"# {SECRET}\n{seq}\n".encode())
    records.record_card(
        store,
        run,
        Card.model_validate(
            {
                "run_id": run.run_id,
                "lane": "op",
                "strategy_sha": sha,
                "metric": metric,
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
    records.set_best(store, run, sha, metric)
    records.close(store, run)


def the_hypothesis(ws: Workspace) -> Hypothesis:
    """The demo hypothesis, parsed straight from the workspace file."""
    return load_yaml(Hypothesis, ws.path("hypotheses", HYP_ID, "hypothesis.yaml"))


def a_dataset(ws: Workspace, **changes: Any) -> Manifest:
    """One dataset the workspace holds, written as its manifest."""
    values: dict[str, Any] = {
        "source": "synthetic",
        "instrument": INSTRUMENT,
        "type": "bar",
        "resolution": "1m",
        "span": (date(2024, 1, 1), date(2024, 12, 31)),
        "adjusted": False,
        "row_count": 1000,
        "checksum": "c" * 64,
    }
    values.update(changes)
    values["dataset_id"] = dataset_id(
        values["instrument"],
        values["type"],
        values["resolution"],
        values["adjusted"],
        values["span"][1],
    )
    manifest = Manifest(**values)
    write_manifest(ws, manifest)
    return manifest


# --- the answer becomes the pinned plan --------------------------------------


def test_a_plan_is_written_pinned_and_recorded(ws: Workspace, store: StateStore) -> None:
    script(ws, certify_plan=[answer()])

    written = plan(ws, store, HYP_ID)

    assert written.hyp_id == HYP_ID
    assert written.plan_version == 1
    assert written.planned_by == "mock"
    assert [gate.id for gate in written.gates] == [
        "embargoed_window",
        "publication_lag",
        "bootstrap",
        "paper_forward",
        "live_drift",
    ]
    assert [left.id for left in written.excluded] == ["deflated_sharpe"]
    assert written.inputs.construct.id == "sleeve"
    assert written.inputs.n_trials == 0
    assert written.inputs.hypothesis_sha == show(ws, store, HYP_ID).hypothesis_sha

    path = plan_file(ws, HYP_ID)
    assert path == certificates_dir(ws, HYP_ID) / PLAN_FILE
    assert read_plan(ws, HYP_ID) == written

    row = store.connection.execute(
        "SELECT * FROM plans WHERE hyp_id = ? AND plan_version = 1", (HYP_ID,)
    ).fetchone()
    assert row["planned_by"] == "mock"
    assert "embargoed_window" in row["gates"]
    assert "deflated_sharpe" in row["excluded"]
    assert [event.kind for event in store.events(subject=HYP_ID)].count("planned") == 1


def test_read_plan_is_none_before_anything_is_planned(ws: Workspace) -> None:
    assert read_plan(ws, HYP_ID) is None


def test_a_pinned_plan_is_returned_without_a_second_call(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(ws, certify_plan=[answer()])
    first = plan(ws, store, HYP_ID)
    seen = prompts(monkeypatch)

    again = plan(ws, store, HYP_ID)

    assert again == first
    assert seen == []
    assert store.connection.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 1


def test_a_replan_re_runs_the_planner_and_mints_the_next_version(
    ws: Workspace, store: StateStore
) -> None:
    script(ws, certify_plan=[answer(), answer(excluded=[{"id": "cost_stress", "reason": "cheap"}])])
    first = plan(ws, store, HYP_ID)
    a_card(store, metric=1.5)

    second = plan(ws, store, HYP_ID, replan=True)

    assert first.plan_version == 1
    assert second.plan_version == 2
    assert [left.id for left in second.excluded] == ["cost_stress"]
    assert second.inputs.n_trials == 1
    assert read_plan(ws, HYP_ID) == second
    versions = store.connection.execute(
        "SELECT plan_version FROM plans WHERE hyp_id = ? ORDER BY plan_version", (HYP_ID,)
    ).fetchall()
    assert [row["plan_version"] for row in versions] == [1, 2]


def test_a_plan_pinned_without_a_recorded_version_still_moves_forward(
    ws: Workspace, store: StateStore
) -> None:
    """The file is the plan; a state store rebuilt beneath it never reuses a version."""
    script(ws, certify_plan=[answer(), answer()])
    plan(ws, store, HYP_ID)
    store.connection.execute("DELETE FROM plans WHERE hyp_id = ?", (HYP_ID,))

    again = plan(ws, store, HYP_ID, replan=True)

    assert again.plan_version == 2


# --- what the prompt may and may not carry -----------------------------------


def test_the_prompt_carries_the_idea_the_toolbox_and_the_invariants(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_dataset(ws)
    script(ws, certify_plan=[answer()])
    seen = prompts(monkeypatch)

    plan(ws, store, HYP_ID)

    assert len(seen) == 1
    call = seen[0]
    assert call.task_class == "certify_plan"
    assert call.tier == "frontier"
    assert call.effort == "high"
    assert call.max_output == 4096
    for stated in ("revert to a rolling mean", INSTRUMENT, "30m", "2025-01-06", "sleeve"):
        assert stated in call.system
    for catalogued in ("embargoed_window", "publication_lag", "paper_forward", "live_drift"):
        assert catalogued in call.system
    assert "meaningful_when" in call.system
    assert "required_gates" in call.system
    assert "at least one gate at each of the cert, paper and live stages" in call.system
    assert '"bar"' in call.user
    assert "2024-12-31" in call.user
    assert "n_trials" in call.user


def test_the_prompt_carries_no_card_metric_no_certificate_and_no_strategy_source(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_card(store, metric=float(MEASURED))
    (hypothesis_dir(ws, HYP_ID) / "strategy.py").write_text(
        f"# {SECRET}\nclass Strategy: ...\n", encoding="utf-8"
    )
    (hypothesis_dir(ws, HYP_ID) / "results.tsv").write_text(
        f"sha7\tmetric\n0123456\t{MEASURED}\n", encoding="utf-8"
    )
    certificates = certificates_dir(ws, HYP_ID)
    certificates.mkdir(parents=True)
    (certificates / "abc1234-9-p1-e1.231.0.yaml").write_text(
        f"verdict: pass\nobjective: {{id: net_edge_bps, value: {MEASURED}}}\n", encoding="utf-8"
    )
    script(ws, certify_plan=[answer()])
    seen = prompts(monkeypatch)

    plan(ws, store, HYP_ID)

    prompt = f"{seen[0].system}\n{seen[0].user}"
    assert SECRET not in prompt
    assert MEASURED not in prompt
    assert "verdict" not in prompt
    assert "snap1" not in prompt
    assert "class Strategy" not in prompt
    assert "best_metric" not in prompt


def test_the_planner_is_shown_the_pinned_bytes_not_a_since_edited_file(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hypothesis_sha` names what the planner saw, so it is what the planner is given."""
    path = ws.path("hypotheses", HYP_ID, "hypothesis.yaml")
    pinned_sha = show(ws, store, HYP_ID).hypothesis_sha
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "revert to a rolling mean", f"revert to a rolling mean, {SECRET}"
        ),
        encoding="utf-8",
    )
    script(ws, certify_plan=[answer()])
    seen = prompts(monkeypatch)

    written = plan(ws, store, HYP_ID)

    assert written.inputs.hypothesis_sha == pinned_sha
    assert SECRET not in seen[0].system


def test_the_trial_count_is_the_search_the_plan_will_be_read_against(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_card(store, metric=1.0, seq=1)
    a_card(store, metric=2.0, seq=2)
    script(ws, certify_plan=[answer()])
    seen = prompts(monkeypatch)

    written = plan(ws, store, HYP_ID)

    assert written.inputs.n_trials == 2
    assert '"n_trials": 2' in seen[0].user


def test_a_gate_with_no_implementation_is_not_offered_to_the_planner(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(ws, certify_plan=[answer()])
    seen = prompts(monkeypatch)

    plan(ws, store, HYP_ID)

    assert "parity_replay" not in plannable()
    assert "parity_replay" not in seen[0].system
    assert "paper_forward" in plannable()


# --- data availability -------------------------------------------------------


def test_availability_reports_the_universe_one_span_per_type(ws: Workspace) -> None:
    a_dataset(ws, span=(date(2024, 1, 1), date(2024, 6, 30)))
    a_dataset(ws, span=(date(2024, 7, 1), date(2024, 12, 31)))
    a_dataset(ws, type="quote", resolution=None, span=(date(2024, 3, 1), date(2024, 9, 30)))
    a_dataset(ws, instrument="OTHER.SIM", type="trade", resolution=None)
    hyp = the_hypothesis(ws)

    reported = availability(ws, hyp)

    assert reported.types == ["bar", "quote"]
    assert reported.spans["bar"].start == datetime(2024, 1, 1, tzinfo=UTC)
    assert reported.spans["bar"].end == datetime(2024, 12, 31, tzinfo=UTC)
    assert reported.spans["quote"].start == datetime(2024, 3, 1, tzinfo=UTC)


def test_availability_is_empty_when_the_workspace_holds_nothing(ws: Workspace) -> None:
    reported = availability(ws, the_hypothesis(ws))
    assert reported.types == []
    assert reported.spans == {}


def test_an_unbounded_range_reaches_the_prompt_as_null(ws: Workspace) -> None:
    """A prompt is JSON, and JSON has no infinity."""
    item = CriteriaItem.model_validate(
        {
            "id": "example",
            "kind": "gate",
            "stage": "cert",
            "meaningful_when": "an example with one open-ended range",
            "params": {"budget": "float"},
            "ranges": {"budget": [0, float("inf")]},
            "impl": "kanso.criteria.gates.example",
        }
    )

    assert _ranges(item, the_hypothesis(ws), 4) == {"budget": {"min": 0.0, "max": None}}


# --- the answer is checked before it is believed ------------------------------


def test_a_rejected_plan_is_retried_with_the_reasons(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(ws, certify_plan=[answer(gates=gates_of(drop="publication_lag")), answer()])
    seen = prompts(monkeypatch)

    written = plan(ws, store, HYP_ID)

    assert written.plan_version == 1
    assert len(seen) == 2
    assert seen[0].system == seen[1].system
    assert "publication_lag is a structural invariant" in seen[1].user
    assert store.connection.execute("SELECT COUNT(*) c FROM spend").fetchone()["c"] == 2


def test_a_second_invalid_plan_fails_the_step(ws: Workspace, store: StateStore) -> None:
    script(ws, certify_plan=[answer(gates=gates_of(drop="publication_lag"))])

    with pytest.raises(PreconditionError) as refused:
        plan(ws, store, HYP_ID)

    assert "in 2 attempts" in refused.value.message
    assert read_plan(ws, HYP_ID) is None
    assert store.connection.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_without_a_scripted_answer_the_step_refuses(ws: Workspace, store: StateStore) -> None:
    """There is no default plan: an unusable model is a refusal, not a fallback."""
    script(ws)

    with pytest.raises(PreconditionError):
        plan(ws, store, HYP_ID)


@pytest.mark.parametrize(
    ("given", "complaint"),
    [
        ({"gates": gates_of(drop="publication_lag")}, "is a structural invariant"),
        ({"gates": gates_of(drop="live_drift")}, "no live gate"),
        ({"gates": replacing("bootstrap", params={"n": 5})}, "outside the range"),
        (
            {
                "gates": gates_of(
                    {"id": "vibes", "stage": "cert", "params": {}, "rationale": "hunch"}
                )
            },
            "gates.vibes: is not a gate in the toolbox",
        ),
        (
            {
                "gates": gates_of(
                    {"id": "net_edge_bps", "stage": "cert", "params": {}, "rationale": "objective"}
                )
            },
            "is not a gate in the toolbox",
        ),
        ({"gates": replacing("bootstrap", stage="paper")}, "runs at the cert stage, not paper"),
        (
            {
                "gates": gates_of(
                    {
                        "id": "parity_replay",
                        "stage": "cert",
                        "params": {"ts_ns": 0},
                        "rationale": "one code path",
                    }
                )
            },
            "gates.parity_replay: this version of kanso has no implementation for it",
        ),
        (
            {
                "gates": gates_of(
                    {
                        "id": "bootstrap",
                        "stage": "cert",
                        "params": {"n": 200},
                        "rationale": "again",
                    }
                )
            },
            "gates.bootstrap: is named twice",
        ),
        (
            {"excluded": [{"id": "vibes", "reason": "not real"}]},
            "excluded.vibes: is not a gate in the toolbox",
        ),
        (
            {"excluded": [{"id": "parity_replay", "reason": "slow"}]},
            "excluded.parity_replay: this version of kanso has no implementation",
        ),
        (
            {"excluded": [{"id": "embargoed_window", "reason": "slow"}]},
            "is required and cannot be left out",
        ),
        (
            {"excluded": [{"id": "bootstrap", "reason": "already in the plan"}]},
            "is both included and excluded",
        ),
        (
            {"gates": replacing("bootstrap", params={"n": [1000]})},
            "the answer is not a plan",
        ),
    ],
)
def test_an_unusable_plan_is_refused_with_the_reason(
    ws: Workspace, store: StateStore, given: dict[str, Any], complaint: str
) -> None:
    script(ws, certify_plan=[answer(**given)])

    with pytest.raises(PreconditionError) as refused:
        plan(ws, store, HYP_ID)

    assert complaint in str(refused.value.remedy)


def test_every_problem_in_a_plan_is_reported_at_once(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = answer(
        gates=[
            {"id": "vibes", "stage": "cert", "params": {}, "rationale": "hunch"},
            {"id": "bootstrap", "stage": "paper", "params": {"n": 1}, "rationale": "wrong twice"},
        ],
        excluded=[{"id": "embargoed_window", "reason": "slow"}],
    )
    script(ws, certify_plan=[broken, answer()])
    seen = prompts(monkeypatch)

    plan(ws, store, HYP_ID)

    assert seen[1].user.count("\n- ") >= 6


# --- the pin ------------------------------------------------------------------


def test_a_pinned_plan_this_version_cannot_run_is_refused_with_the_way_out(
    ws: Workspace, store: StateStore
) -> None:
    script(ws, certify_plan=[answer()])
    written = plan(ws, store, HYP_ID)
    document = yaml.safe_load(plan_file(ws, HYP_ID).read_text(encoding="utf-8"))
    document["gates"].append(
        {"id": "parity_replay", "stage": "cert", "params": {}, "rationale": "hand-written"}
    )
    plan_file(ws, HYP_ID).write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        plan(ws, store, HYP_ID)

    assert "is not one this kanso can run" in refused.value.message
    assert "--replan" in str(refused.value.remedy)
    assert written.plan_version == 1


def test_a_replan_replaces_a_plan_this_version_cannot_run(ws: Workspace, store: StateStore) -> None:
    script(ws, certify_plan=[answer(), answer()])
    plan(ws, store, HYP_ID)
    document = yaml.safe_load(plan_file(ws, HYP_ID).read_text(encoding="utf-8"))
    document["gates"].append(
        {"id": "parity_replay", "stage": "cert", "params": {}, "rationale": "hand-written"}
    )
    plan_file(ws, HYP_ID).write_text(yaml.safe_dump(document), encoding="utf-8")

    replanned = plan(ws, store, HYP_ID, replan=True)

    assert "parity_replay" not in [gate.id for gate in replanned.gates]
    assert replanned.plan_version == 2


def test_a_plan_filed_under_another_hypothesis_is_refused(ws: Workspace, store: StateStore) -> None:
    script(ws, certify_plan=[answer()])
    plan(ws, store, HYP_ID)
    elsewhere = certificates_dir(ws, "other_hyp")
    elsewhere.mkdir(parents=True)
    (elsewhere / PLAN_FILE).write_text(
        plan_file(ws, HYP_ID).read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(ValidationError) as refused:
        read_plan(ws, "other_hyp")

    assert "is the plan of demo_mr" in refused.value.message


# --- preconditions ------------------------------------------------------------


def test_an_unclassified_hypothesis_is_refused(ws: Workspace, store: StateStore) -> None:
    directory = ws.path("hypotheses", "draft_one")
    directory.mkdir(parents=True)
    path = directory / "hypothesis.yaml"
    path.write_text(DRAFT, encoding="utf-8")
    add(ws, store, path)
    script(ws, certify_plan=[answer()])

    with pytest.raises(PreconditionError) as refused:
        plan(ws, store, "draft_one")

    assert "is not classified" in refused.value.message
    assert "kanso classify draft_one" in str(refused.value.remedy)


def test_a_hypothesis_whose_pin_is_gone_is_refused(ws: Workspace, store: StateStore) -> None:
    store.connection.execute(
        "UPDATE hypotheses SET hypothesis_sha = NULL WHERE hyp_id = ?", (HYP_ID,)
    )
    script(ws, certify_plan=[answer()])

    with pytest.raises(PreconditionError) as refused:
        plan(ws, store, HYP_ID)

    assert "no pinned hypothesis" in refused.value.message


def test_an_unregistered_hypothesis_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(PreconditionError) as refused:
        plan(ws, store, "never_added")

    assert "is not a registered hypothesis" in refused.value.message


# --- the shipped demo ---------------------------------------------------------


def test_the_shipped_demo_script_plans_the_demo_hypothesis(
    ws: Workspace, store: StateStore
) -> None:
    """`kanso init --demo` must plan with no provider key, no network and no cost."""
    scripted = yaml.safe_load((TEMPLATES / "demo" / "responses.yaml").read_text(encoding="utf-8"))
    assert ws.path("mock", "responses.yaml").read_text(encoding="utf-8") == (
        (TEMPLATES / "demo" / "responses.yaml").read_text(encoding="utf-8")
    )

    written = plan(ws, store, HYP_ID)

    assert [gate.id for gate in written.gates] == [
        gate["id"] for gate in scripted["certify_plan"][0]["gates"]
    ]
