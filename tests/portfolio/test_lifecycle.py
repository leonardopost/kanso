"""What a passing certificate turns into by itself, and what it does when it cannot.

Certification calls this, so its happy path is covered wherever a certificate passes. What
is asserted here is the other half: that a refusal on the way to the stage becomes an
escalation rather than a lost certificate, and that the object the command line renders says
which of the two acts got as far as happening.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kanso.certify import certificate
from kanso.criteria import criteria_version
from kanso.env.envelope import engine_version
from kanso.inbox import unread
from kanso.portfolio import deploy
from kanso.portfolio.lifecycle import Adoption, on_certified
from kanso.schemas import Certificate, StrategyFile
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.portfolio.conftest import HYP_ID, REVERTING, reconfigure
from tests.replay.conftest import snapshot, venue_model


def a_certificate(ws: Workspace, sha: str = "b" * 64) -> Certificate:
    """A passing certificate for the demo sleeve, made rather than run."""
    return Certificate.model_validate(
        {
            "hyp_id": HYP_ID,
            "strategy_sha": sha,
            "nautilus_version": engine_version(),
            "venue_model": venue_model(),
            "snapshot_id": snapshot.snapshots(ws)[-1].snapshot_id,
            "criteria_version": criteria_version(),
            "plan_version": 1,
            "construct": {"id": "sleeve"},
            "objective": {"id": "wf_sharpe_net", "value": 1.0, "se": 0.1},
            "gates": [
                {
                    "id": "embargoed_window",
                    "stage": "cert",
                    "params": {"min_fraction": 0.0},
                    "evidence": {},
                    "pass": True,
                }
            ],
            "n_trials": 1,
            "verdict": "pass",
            "created_at": datetime.now(tz=UTC),
        }
    )


def test_a_certificate_that_cannot_be_composed_escalates_and_stands(
    ws: Workspace, store: StateStore
) -> None:
    """The verdict is already true; a version that cannot be made does not unmake it.

    The certificate here was never written, so composition cannot find one to compose.
    """
    adoption = on_certified(ws, store, a_certificate(ws))

    assert adoption.strategy_id == HYP_ID
    assert adoption.version is None
    assert adoption.deployment is None
    assert adoption.blocked and "no passing certificate" in adoption.blocked
    assert adoption.label == HYP_ID, "there is no version to name it by"
    assert adoption.state is None
    assert adoption.capital is None
    entry = unread(store)[-1]
    assert entry.kind == "deploy_blocked"
    assert "could not reach the paper stage" in entry.summary


def test_a_stage_that_cannot_take_the_version_escalates_and_keeps_it(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """A halted stage is one of the things deployment refuses, and it refuses here too."""
    made = a_certificate(ws, sha=composed_strategy.versions[0].sleeve.strategy_sha)
    certificate.write(ws, store, made, REVERTING)
    reconfigure(ws, "paper", kill_switch=True)

    adoption = on_certified(ws, store, made)

    assert adoption.version is not None, "the version was composed before the stage refused it"
    assert adoption.deployment is None
    assert adoption.blocked and "kill_switch" in adoption.blocked
    assert adoption.label == f"{HYP_ID}@1"
    assert adoption.state == "composed", "composed, and waiting for a stage that will take it"
    assert adoption.capital is None
    assert unread(store)[-1].kind == "deploy_blocked"


def test_the_adoption_reports_the_money_the_stage_gave_it(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    adoption = Adoption(
        strategy_id=composed_strategy.id,
        version=composed_strategy.versions[0],
        deployment=deploy(ws, store, "paper"),
    )

    assert adoption.state == "paper"
    assert adoption.capital == 40_000.0
    assert adoption.payload() == {
        "strategy": composed_strategy.id,
        "version": 1,
        "state": "paper",
        "capital": 40_000.0,
        "blocked": None,
    }


def test_a_version_the_stage_did_not_admit_keeps_the_state_it_had(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """A deployment that admitted a different version says nothing about this one."""
    deployment = deploy(ws, store, "paper")
    other = composed_strategy.versions[0].model_copy(update={"version": 2})

    adoption = Adoption(strategy_id=composed_strategy.id, version=other, deployment=deployment)

    assert adoption.state == "composed"
    assert adoption.capital is None
