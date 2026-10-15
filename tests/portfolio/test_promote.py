"""Promotion, demotion, and the kill switch that must not deadlock either of them."""

from __future__ import annotations

import json

import pytest

from kanso import strategy as strategies
from kanso.errors import Exit, KansoError, PreconditionError
from kanso.portfolio import approvals, demote, deploy, files, promote
from kanso.schemas import Expectation, StrategyFile, StrategyVersion
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable, reconfigure, second_version


def make_promotable(ws: Workspace, store: StateStore, strategy_id: str) -> int:
    """Put the deployed paper version into `promotable`, as passing paper gates would."""
    file = strategy_files.require(ws, strategy_id)
    latest = file.latest()
    strategy_files.write(
        ws,
        file.model_copy(
            update={
                "versions": [
                    *file.versions[:-1],
                    latest.model_copy(update={"state": "promotable"}),
                ]
            }
        ),
    )
    store.connection.execute(
        "UPDATE strategy_versions SET state = 'promotable' WHERE strategy_id = ? AND version = ?",
        (strategy_id, latest.version),
    )
    return latest.version


def _breached(*_: object) -> Expectation:
    raise PreconditionError("the composed version's book falls to 40% of its gross")


@pytest.fixture
def promotable(ws: Workspace, store: StateStore, composed_strategy: StrategyFile) -> str:
    """A version on the paper stage whose paper gates have passed."""
    deploy(ws, store, "paper")
    reconfigure(ws, "live", capital=50_000.0)
    make_promotable(ws, store, composed_strategy.id)
    return composed_strategy.id


# --- promotion ----------------------------------------------------------------


def test_a_promotion_without_a_name_is_refused(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable)

    assert raised.value.code == Exit.APPROVAL
    assert "--as NAME" in (raised.value.remedy or "")
    assert approvals(store, promotable, 1) == []


def test_a_blank_name_is_no_name(ws: Workspace, store: StateStore, promotable: str) -> None:
    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="   ")

    assert raised.value.code == Exit.APPROVAL


def test_a_promotion_records_the_approval_and_moves_the_version(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    made = promote(ws, store, promotable, operator="Leonardo")

    assert made.approval.operator == "Leonardo"
    assert [one.operator for one in approvals(store, promotable, 1)] == ["Leonardo"]
    assert strategies.require(ws, promotable).latest().state == "live"
    live = files.stage_of(files.read(ws), "live")
    assert files.held(live, promotable) is not None
    assert files.held(files.stage_of(files.read(ws), "paper"), promotable) is None


def test_a_named_version_in_the_right_state_is_promoted(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    made = promote(ws, store, promotable, version=1, operator="Leonardo")

    assert made.version == 1
    assert made.label == f"{promotable}@1"
    assert made.retired is None


def test_a_second_promotion_retires_the_version_it_replaces(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")

    assert strategies.require(ws, promotable).latest().state == "live"


def test_a_demotion_names_itself(ws: Workspace, store: StateStore, promotable: str) -> None:
    promote(ws, store, promotable, operator="Leonardo")

    made = demote(ws, store, promotable)

    assert made.label == f"{promotable}@1"


def test_a_promotion_redeploys_both_stages(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    made = promote(ws, store, promotable, operator="Leonardo")

    assert [one.stage for one in made.deployments] == ["live", "paper"]
    assert [one.label for one in made.deployments[0].admitted] == [f"{promotable}@1"]
    assert made.deployments[1].admitted == ()


def test_a_version_that_is_not_promotable_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    with pytest.raises(KansoError) as raised:
        promote(ws, store, composed_strategy.id, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION


def test_a_named_version_in_the_wrong_state_is_refused(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, version=99, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION


def test_a_live_stage_with_no_capital_blocks_the_promotion(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    reconfigure(ws, "live", capital=0.0)

    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION
    assert approvals(store, promotable, 1) == [], "nothing is approved that cannot be funded"
    kinds = store.connection.execute("SELECT kind FROM escalations").fetchall()
    assert [row[0] for row in kinds] == ["deploy_blocked"]


def test_a_version_the_live_venue_cannot_hold_is_refused_before_it_is_approved(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    """A version certified under another round trip than the one live already trades on
    its venue is refused (exit 2) before its approval is recorded or anything moves: the
    live stage's deployment would refuse it, after the move, with the approval on record."""
    from tests.replay.conftest import DOCUMENT, document

    promote(ws, store, promotable, operator="Leonardo")
    deployable(
        ws, store, "other", doc=document(id="other", costs={**DOCUMENT["costs"], "latency_ms": 50})
    )
    deploy(ws, store, "paper")
    make_promotable(ws, store, "other")
    portfolio = files.read(ws)

    with pytest.raises(KansoError) as raised:
        promote(ws, store, "other", operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION
    assert f"{promotable}@1" in raised.value.message and "other@1" in raised.value.message
    assert "one venue is one round trip" in raised.value.message
    assert approvals(store, "other", 1) == []
    assert strategies.require(ws, "other").latest().state == "promotable"
    assert files.read(ws) == portfolio


def test_a_promotion_strikes_the_live_share_s_expectation_once_and_moves_it_with_the_version(
    ws: Workspace, store: StateStore, promotable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`live_drift` judges the version in the live share's units, so the expectation is
    measured at that share before the approval — and written with the move, so the live
    deploy that follows does not measure it a second time."""
    real = strategies.restruck
    measured: list[float] = []

    def counted(
        ws_: Workspace, store_: StateStore, sid: str, version: StrategyVersion, capital: float
    ) -> Expectation:
        measured.append(capital)
        return real(ws_, store_, sid, version, capital)

    monkeypatch.setattr(strategies, "restruck", counted)

    promote(ws, store, promotable, operator="Leonardo")

    held = files.held(files.stage_of(files.read(ws), "live"), promotable)
    assert held is not None and measured == [held.capital]
    assert strategies.require(ws, promotable).latest().expectation.capital == held.capital
    (event,) = store.events(kind="expected")
    assert (event.detail["stage"], event.detail["capital"]) == ("live", held.capital)
    row = store.connection.execute(
        "SELECT expectation FROM strategy_versions WHERE strategy_id = ?", (promotable,)
    ).fetchone()
    assert json.loads(row[0])["capital"] == held.capital


def test_a_version_whose_book_breaches_its_floor_at_the_live_share_is_refused_before_it_is_approved(
    ws: Workspace, store: StateStore, promotable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused (exit 2) with the approval unrecorded and nothing moved — not inside the live
    deploy, after the approval, with the version on neither stage."""
    monkeypatch.setattr(strategies, "restruck", _breached)
    portfolio = files.read(ws)

    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION
    assert "measured again at the 20000 the live stage would fund it with" in raised.value.message
    assert "falls to 40% of its gross" in raised.value.message
    assert f"kanso strat retire {promotable}@1" in (raised.value.remedy or "")
    assert approvals(store, promotable, 1) == []
    assert strategies.require(ws, promotable).latest().state == "promotable"
    assert files.read(ws) == portfolio
    assert [e.detail["stage"] for e in store.events(kind="deployed")] == ["paper"]


def test_a_live_share_below_the_version_s_budgets_is_not_measured(
    ws: Workspace, store: StateStore, promotable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live deploy blocks a sized version at a share under its budgets before it would
    measure anything, so the promotion measures nothing there either (backlog row 185)."""
    import yaml

    from kanso.strategy import impl

    path = impl.manifest_file(ws, promotable, 1)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["sleeve"]["config"]["sizing_budget"] = 30_000.0
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(strategies, "restruck", _breached)

    made = promote(ws, store, promotable, operator="Leonardo")

    assert made.deployments[0].blocked == (f"{promotable}@1",)
    assert store.events(kind="expected") == []


def test_a_halted_live_stage_refuses_a_promotion_into_it(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    reconfigure(ws, "live", kill_switch=True)

    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION
    assert approvals(store, promotable, 1) == []


# --- demotion -----------------------------------------------------------------


def test_a_demotion_returns_the_version_to_the_paper_stage(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")

    made = demote(ws, store, promotable)

    assert made.state == "paper"
    assert strategies.require(ws, promotable).latest().state == "paper"
    assert files.held(files.stage_of(files.read(ws), "live"), promotable) is None
    assert files.held(files.stage_of(files.read(ws), "paper"), promotable) is not None
    assert made.halted == ()


def test_a_demotion_redeploys_the_live_stage_before_the_paper_one(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    """The stage holding the money is not made to wait for the paper share's measurement."""
    promote(ws, store, promotable, operator="Leonardo")

    made = demote(ws, store, promotable)

    assert [one.stage for one in made.deployments] == ["live", "paper"]
    assert [one.label for one in made.deployments[1].admitted] == [f"{promotable}@1"]


def test_a_demoted_version_paper_cannot_hold_at_its_share_is_blocked_and_the_rest_deploys(
    ws: Workspace, store: StateStore, promotable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured at the paper share and refused, the demoted version is blocked on paper and
    escalated; the live stage was redeployed first, and the other version on paper deploys."""
    deployable(ws, store, "other")
    promote(ws, store, promotable, operator="Leonardo")
    real = strategies.restruck

    def breached(
        ws_: Workspace, store_: StateStore, sid: str, version: StrategyVersion, capital: float
    ) -> Expectation:
        if sid == promotable:
            _breached()
        return real(ws_, store_, sid, version, capital)

    monkeypatch.setattr(strategies, "restruck", breached)

    made = demote(ws, store, promotable)

    live, paper = made.deployments
    assert (live.stage, paper.stage) == ("live", "paper")
    assert paper.blocked == (f"{promotable}@1",)
    assert [one.label for one in paper.admitted] == ["other@1"]
    assert strategies.require(ws, promotable).latest().state == "paper"
    assert files.held(files.stage_of(files.read(ws), "live"), promotable) is None
    summary = store.connection.execute(
        "SELECT summary FROM escalations WHERE kind = 'deploy_blocked' AND subject = ?",
        (f"{promotable}@1",),
    ).fetchone()
    assert "falls to 40% of its gross" in summary[0]


def test_a_demotion_of_a_version_that_is_not_live_is_refused(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        demote(ws, store, promotable)

    assert raised.value.code == Exit.PRECONDITION


def test_an_unknown_strategy_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(KansoError) as raised:
        demote(ws, store, "nothing_here")

    assert raised.value.code == Exit.PRECONDITION


# --- the kill switch and the demotion it triggers -----------------------------


def test_a_halted_stage_stays_halted_through_a_demotion(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    reconfigure(ws, "live", kill_switch=True)

    made = demote(ws, store, promotable)

    assert made.halted == ("live",)
    assert [one.stage for one in made.deployments] == ["paper"]
    assert files.stage_of(files.read(ws), "live").kill_switch is True
    assert strategies.require(ws, promotable).latest().state == "paper"


def test_the_demotion_names_the_halted_stage_and_how_to_resume_it(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    reconfigure(ws, "live", kill_switch=True)

    demote(ws, store, promotable)

    row = store.connection.execute(
        "SELECT kind, subject, summary, actions FROM escalations WHERE kind = 'demoted'"
    ).fetchone()
    assert row is not None
    assert row[1] == f"{promotable}@1"
    assert "live" in row[2]
    assert "kanso portfolio deploy --stage live" in row[3]


def test_both_stages_halted_still_moves_the_version(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    reconfigure(ws, "live", kill_switch=True)
    reconfigure(ws, "paper", kill_switch=True)

    made = demote(ws, store, promotable)

    assert made.halted == ("paper", "live")
    assert made.deployments == ()
    assert strategies.require(ws, promotable).latest().state == "paper"


def test_a_named_version_that_is_not_live_cannot_be_demoted(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        demote(ws, store, promotable, version=1)

    assert raised.value.code == Exit.PRECONDITION
    assert "promotable, not live" in raised.value.message


def test_promoting_a_second_version_retires_the_one_it_replaces(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    second_version(ws, store, promotable)
    deploy(ws, store, "paper")
    make_promotable(ws, store, promotable)

    made = promote(ws, store, promotable, operator="Leonardo")

    assert made.version == 2
    assert made.retired == 1
    assert [v.state for v in strategies.require(ws, promotable).versions] == ["retired", "live"]
    live = files.stage_of(files.read(ws), "live")
    held = files.held(live, promotable)
    assert held is not None
    assert held.version == 2
