"""Exploration: a draft hypothesis written from what a parent's research learned.

The parent is researched for real — a baseline and one proposed keep through the mock
register — so the coverage, the keeps and the pins the explorer reads are the loop's own
records rather than rows a test inserted. The candidate the explorer answers with is
scripted, and every refusal is one answer's worth of complaints on the router's ladder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Any

import pytest
import yaml

from kanso.errors import PreconditionError
from kanso.hyp import show
from kanso.inbox import unread
from kanso.models import Answer, Call
from kanso.models import router as router_module
from kanso.research import driver, explore, scheduler
from kanso.schemas import Hypothesis, ModelSpec, parse_yaml
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, HYP_ID, RESEARCH, classify
from .mocked import (  # noqa: F401
    PLAN,
    SEED,
    fresh_cursors,
    proposal,
    scripted,
    tuned,
    write_script,
)

CANDIDATE_ID = "demo_breakout"

BREAKOUT = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys a close above the last two, and sells a close below them."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes = []
        self.long = False

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        if third > max(first, second) and not self.long:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.long = True
        elif third < min(first, second) and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''

UNCLASSIFIED = {
    key: value
    for key, value in DOCUMENT.items()
    if key not in ("construct", "objective", "constraints")
}


def candidate_yaml(**changes: Any) -> str:
    """The candidate's `hypothesis.yaml`: the parent's idea, unclassified, under a new id."""
    return yaml.safe_dump(
        {
            **UNCLASSIFIED,
            "id": CANDIDATE_ID,
            "title": "Demo: daily breakout on a synthetic saw-tooth",
            "thesis": "A close above the last two continues for a session.",
            "mechanism": "momentum",
            **changes,
        },
        sort_keys=False,
    )


def answer(**changes: Any) -> dict[str, Any]:
    """One `explore` answer, valid unless a field is replaced."""
    return {
        "id": CANDIDATE_ID,
        "hypothesis_yaml": candidate_yaml(),
        "program_md": "# program.md\n\nEdit strategy.py; keep the breakout.\n",
        "strategy_py": BREAKOUT,
        "rationale": "the parent only faded falls; this buys a break above the last two closes",
        "tags": ["signal_breakout", "horizon_shorter"],
        **changes,
    }


def explorer(ws: Workspace, answers: list[Any]) -> None:
    """Script the frontier tier: the certification plan a stall needs, and the explorer."""
    write_script(ws, "frontier", {"certify_plan": [PLAN], "explore": answers})


@dataclass
class Recorder:
    calls: list[Call]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Every call that reached a client, so a test can read the prompt that went out."""
    seen = Recorder(calls=[])
    build = router_module.client_for

    def watching(root: Any, spec: ModelSpec) -> Any:
        client = build(root, spec)

        class Watched:
            protocol = client.protocol

            def complete(self, spec: ModelSpec, call: Call) -> Answer:
                seen.calls.append(call)
                return client.complete(spec, call)

        return Watched()

    monkeypatch.setattr(router_module, "client_for", watching)
    return seen


@pytest.fixture
def researched(ws: Workspace, store: StateStore) -> str:
    """The demo hypothesis after a baseline and one proposed keep, with the mock register."""
    scripted(ws, propose=[proposal("revert")])
    hyp_id = classify(ws, store, DOCUMENT, SEED)
    driver.run(ws, store, hyp_id, cards=1)
    return hyp_id


def parent_of(store: StateStore) -> Hypothesis:
    return parse_yaml(Hypothesis, yaml.safe_dump(DOCUMENT), "hypothesis.yaml")


def judge(ws: Workspace, store: StateStore, data: dict[str, Any]) -> list[str]:
    """Judge a candidate against the demo parent, whose only pin researched to its end."""
    return explore._judge(ws, store, parent_of(store), RESEARCH[1], data)


def windows(research: tuple[date, date], certification: date) -> dict[str, Any]:
    return {
        "research": {"start": research[0], "end": research[1]},
        "certification": {"start": certification, "end": date(2024, 2, 29)},
        "forward": {"start": date(2024, 3, 1)},
    }


EARLIER_PIN = "2020-01-01T00:00:00+00:00"
"""When the older pin's run started: before any run a test drives, so it is never newest."""


def pin_earlier(store: StateStore, hyp_id: str, **changes: Any) -> str:
    """An older run of `hyp_id` under another `hypothesis.yaml`, as a re-pin leaves behind.

    Copied from the newest run with its hypothesis blob replaced and its start moved before
    it, so it is the same research under a pin nothing reads as the newest.
    """
    [newest] = [
        dict(row)
        for row in store.connection.execute("SELECT * FROM runs WHERE hyp_id = ?", (hyp_id,))
    ]
    sha = store.put_blob(yaml.safe_dump({**DOCUMENT, **changes}).encode("utf-8"))
    row = {
        **newest,
        "run_id": "older-pin",
        "tag": "20200101-1",
        "hypothesis_sha": sha,
        "started_at": EARLIER_PIN,
        "ended_at": EARLIER_PIN,
    }
    store.connection.execute(
        f"INSERT INTO runs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )
    return "older-pin"


# --- a candidate written -----------------------------------------------------


def test_a_candidate_is_written_as_a_draft_nothing_registers(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    explorer(ws, [answer()])

    written = explore.explore(ws, store, researched)

    directory = ws.path("hypotheses", CANDIDATE_ID)
    assert written.directory == directory
    assert sorted(path.name for path in directory.iterdir()) == [
        "hypothesis.yaml",
        "program.md",
        "strategy.py",
    ]
    assert (directory / "strategy.py").read_text(encoding="utf-8") == BREAKOUT
    assert (directory / "hypothesis.yaml").read_text(encoding="utf-8") == candidate_yaml()
    assert written.strategy_sha == sha256(BREAKOUT.encode()).hexdigest()
    assert store.has_blob(written.strategy_sha)
    # Registration is the operator's: nothing holds the new id but the directory.
    with pytest.raises(PreconditionError, match="not a registered hypothesis"):
        show(ws, store, CANDIDATE_ID)
    assert scheduler.queued(store) == []

    events = [event for event in store.events(subject=researched) if event.kind == "explored"]
    assert [event.detail for event in events] == [
        {
            "parent": researched,
            "id": CANDIDATE_ID,
            "strategy_sha": written.strategy_sha,
            "tags": ["signal_breakout", "horizon_shorter"],
            "lane": "op",
        }
    ]
    [entry] = unread(store)
    assert (entry.kind, entry.subject) == ("explored", CANDIDATE_ID)
    assert entry.summary.startswith(f"from {researched}: the parent only faded falls")
    assert entry.actions == (
        f"kanso hyp validate hypotheses/{CANDIDATE_ID}/hypothesis.yaml · "
        f"kanso hyp add hypotheses/{CANDIDATE_ID}/hypothesis.yaml"
    )
    assert written.payload() == {
        "parent": researched,
        "id": CANDIDATE_ID,
        "dir": str(directory),
        "files": ["hypothesis.yaml", "program.md", "strategy.py"],
        "strategy_sha": written.strategy_sha,
        "tags": ["signal_breakout", "horizon_shorter"],
        "rationale": answer()["rationale"],
        "escalation": entry.escalation_id,
    }


def test_the_explorer_is_shown_the_research_and_only_ids_of_the_certification(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    store.connection.execute(
        "INSERT INTO certificates (hyp_id, strategy_sha, plan_version, nautilus_version,"
        " snapshot_id, criteria_version, gates, n_trials, verdict, path, created_at)"
        " VALUES (?, ?, 1, '1.231.0', 'snap', '0.1.0', ?, 1, 'fail', 'p.yaml',"
        " '2024-01-01T00:00:00+00:00')",
        (
            researched,
            "b" * 64,
            '[{"id": "embargoed_window", "pass": true, "evidence": {"window": "certification"}},'
            ' {"id": "deflated_sharpe", "pass": false, "evidence": {"dsr": 0.123456}}]',
        ),
    )
    store.event("stalled", researched, {"best_sha": "c" * 64, "verdict": "fail"})
    store.event("stalled", "demo_other", {"best_sha": "d" * 64, "verdict": "pass"})  # not ours
    explorer(ws, [answer()])

    explore.explore(ws, store, researched)

    [call] = [call for call in recorded.calls if call.task_class == "explore"]
    assert (call.tier, call.effort, call.max_output) == ("frontier", "high", 16384)
    stable = call.system.split("do not change while you work on demo_mr:\n", 1)[1]
    assert set(json.loads(stable)) == {"hypothesis.yaml", "program.md"}
    facts = json.loads(call.user)
    assert set(facts) == {
        "strategy.py",
        "coverage",
        "keeps",
        "stalls",
        "certificates",
        "taken_ids",
    }
    assert "Strategy.mode" in facts["strategy.py"]  # the best, which the keep wrote
    assert facts["coverage"]["signal_mean_reversion"]["count"] == 1
    assert [keep["desc"] for keep in facts["keeps"]] == ["run in revert mode", "baseline"]
    assert facts["stalls"] == {"count": 1, "newest": [{"best_sha7": "ccccccc", "verdict": "fail"}]}
    assert facts["certificates"] == [
        {"sha7": "bbbbbbb", "verdict": "fail", "failing_gates": ["deflated_sharpe"]}
    ]
    assert HYP_ID in facts["taken_ids"] and "portfolio" in facts["taken_ids"]
    # Nothing measured on the certification window travels: no evidence, no passing gate.
    assert "0.123456" not in call.user and "embargoed_window" not in call.user


@pytest.mark.parametrize("pin", ["hypothesis_sha", "snapshot_id", "criteria_version"])
def test_the_explorer_is_shown_only_the_keeps_measured_under_the_newest_pins(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder, pin: str
) -> None:
    """A keep under another pin was scored on another window, snapshot or criteria."""
    changed = {
        "hypothesis_sha": store.put_blob(
            yaml.safe_dump({**DOCUMENT, "title": "an older pin"}).encode("utf-8")
        ),
        "snapshot_id": "an-older-snapshot",
        "criteria_version": "0.0.1",
    }[pin]
    [run] = [
        dict(row)
        for row in store.connection.execute("SELECT * FROM runs WHERE hyp_id = ?", (researched,))
    ]
    older = {**run, "run_id": "older-pin", "tag": "20200101-1", pin: changed}
    older.update(started_at=EARLIER_PIN, ended_at=EARLIER_PIN)
    store.connection.execute(
        f"INSERT INTO runs ({', '.join(older)}) VALUES ({', '.join('?' for _ in older)})",
        tuple(older.values()),
    )
    [card] = [
        dict(row)
        for row in store.connection.execute(
            "SELECT * FROM cards WHERE run_id = ? AND status = 'keep' ORDER BY seq LIMIT 1",
            (run["run_id"],),
        )
    ]
    del card["card_id"]
    card.update(run_id="older-pin", metric=1e9, description="kept under an older pin")
    store.connection.execute(
        f"INSERT INTO cards ({', '.join(card)}) VALUES ({', '.join('?' for _ in card)})",
        tuple(card.values()),
    )
    explorer(ws, [answer()])

    explore.explore(ws, store, researched)

    [call] = [call for call in recorded.calls if call.task_class == "explore"]
    keeps = [keep["desc"] for keep in json.loads(call.user)["keeps"]]
    assert keeps == ["run in revert mode", "baseline"]


def test_a_rejected_candidate_is_asked_again_with_its_complaints(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    explorer(ws, [answer(id=HYP_ID), answer()])

    written = explore.explore(ws, store, researched)

    assert written.hyp_id == CANDIDATE_ID
    retry = [call for call in recorded.calls if call.task_class == "explore"][1]
    assert f"id: {HYP_ID!r} is taken" in retry.user
    # The parent's own directory is untouched.
    assert ws.path("hypotheses", HYP_ID, "strategy.py").read_bytes() != BREAKOUT.encode()


def test_a_ladder_that_runs_out_writes_nothing(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    explorer(ws, [answer(strategy_py=SEED.decode())])

    with pytest.raises(PreconditionError, match="explore: no model answered") as caught:
        explore.explore(ws, store, researched)

    assert "already stored" in str(caught.value.remedy)
    assert not ws.path("hypotheses", CANDIDATE_ID).exists()
    assert unread(store) == []


def test_a_directory_that_appears_while_the_answer_is_judged_is_not_written_into(
    ws: Workspace, store: StateStore, researched: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    explorer(ws, [answer()])
    judge = explore._judge

    def racing(*args: Any) -> list[str]:
        complaints = judge(*args)
        ws.path("hypotheses", CANDIDATE_ID).mkdir()
        return complaints

    monkeypatch.setattr(explore, "_judge", racing)

    with pytest.raises(PreconditionError, match="appeared while the candidate was being judged"):
        explore.explore(ws, store, researched)

    assert list(ws.path("hypotheses", CANDIDATE_ID).iterdir()) == []


def test_a_hypothesis_never_researched_has_nothing_to_explore_from(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, SEED)

    with pytest.raises(PreconditionError, match="never been researched") as caught:
        explore.explore(ws, store, hyp_id)
    assert caught.value.remedy == f"run `kanso research run {hyp_id}`"

    with pytest.raises(PreconditionError, match="not a registered hypothesis"):
        explore.explore(ws, store, "nobody")


# --- what a candidate must be ------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "complaint"),
    [
        ({"id": "Not-An-Id"}, "is not a hypothesis id; use 3 to 40 characters"),
        ({"id": HYP_ID}, f"id: {HYP_ID!r} is taken"),
        ({"id": "portfolio"}, "id: 'portfolio' is taken"),
        ({"hypothesis_yaml": "id: [unclosed"}, "hypothesis_yaml: not valid YAML"),
        ({"hypothesis_yaml": "id: demo_breakout\n"}, "hypothesis_yaml: "),
        ({"hypothesis_yaml": candidate_yaml(id="demo_other")}, "declares id 'demo_other'"),
        (
            {"hypothesis_yaml": candidate_yaml(construct={"id": "sleeve"})},
            "carries construct; a candidate is written as a draft",
        ),
        (
            {"hypothesis_yaml": candidate_yaml(objective=DOCUMENT["objective"])},
            "carries objective; a candidate is written as a draft",
        ),
        (
            {"hypothesis_yaml": candidate_yaml(constraints=DOCUMENT["constraints"])},
            "carries constraints; a candidate is written as a draft",
        ),
        (
            {
                "hypothesis_yaml": candidate_yaml(
                    windows={
                        "research": {"start": date(2024, 1, 1), "end": date(2024, 2, 5)},
                        "certification": {"start": date(2024, 2, 20), "end": date(2024, 2, 29)},
                        "forward": {"start": date(2024, 3, 1)},
                    }
                )
            },
            "windows.research.end: 2024-02-05 is after the parent's research window ends",
        ),
        (
            {
                "hypothesis_yaml": candidate_yaml(
                    windows={
                        "research": {"start": date(2024, 1, 1), "end": date(2024, 1, 20)},
                        "certification": {"start": date(2024, 2, 1), "end": date(2024, 2, 29)},
                        "forward": {"start": date(2024, 3, 1)},
                    }
                )
            },
            "windows.certification.start: 2024-02-01 is before the parent's certification",
        ),
        (
            # Its own research ends early enough for its own embargo; the parent's does not.
            {
                "hypothesis_yaml": candidate_yaml(
                    horizon="3d",
                    windows=windows((date(2023, 10, 1), date(2024, 1, 5)), date(2024, 2, 6)),
                )
            },
            "windows.certification.start: 2024-02-06 is inside the embargo counted from the "
            "end of the parent's research (2024-01-31) for a 3d horizon; certification may not "
            "open before 2024-02-15",
        ),
        (
            # A 20d horizon embargoes 100 days: its own research end passes its own check,
            # and certification still opens six days after the parent last researched.
            {
                "hypothesis_yaml": candidate_yaml(
                    horizon="20d",
                    windows=windows((date(2023, 1, 1), date(2023, 9, 1)), date(2024, 2, 6)),
                )
            },
            "windows.certification.start: 2024-02-06 is inside the embargo counted from the "
            "end of the parent's research (2024-01-31) for a 20d horizon; certification may "
            "not open before 2024-05-10",
        ),
        ({"strategy_py": "def broken(:\n"}, "strategy_py: strategy.py does not parse"),
    ],
)
def test_every_refusal_is_a_complaint_naming_what_to_change(
    ws: Workspace, store: StateStore, changes: dict[str, Any], complaint: str
) -> None:
    classify(ws, store, DOCUMENT, SEED)

    complaints = judge(ws, store, answer(**changes))

    assert any(complaint in said for said in complaints), complaints


def test_an_instrument_outside_the_candidate_s_universe_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    source = BREAKOUT + "\nOTHER = 'OTHER.XNYS'\n"

    complaints = judge(ws, store, answer(strategy_py=source))

    assert [said for said in complaints if "OTHER.XNYS" in said], complaints


def test_an_id_with_a_directory_is_taken_though_nothing_registered_it(
    ws: Workspace, store: StateStore
) -> None:
    ws.path("hypotheses", CANDIDATE_ID).mkdir(parents=True)

    complaints = judge(ws, store, answer())

    assert complaints == [
        f"id: {CANDIDATE_ID!r} is taken — registered, reserved, or already a directory under "
        "hypotheses/ — so choose one that is not in `taken_ids`"
    ]


def test_every_check_that_can_be_made_is_made_in_one_answer(
    ws: Workspace, store: StateStore
) -> None:
    """One retry is all the frontier tier gets, so a wrong id hides nothing else."""
    classify(ws, store, DOCUMENT, SEED)
    store.put_blob(SEED)
    wrong = answer(id=HYP_ID, strategy_py=SEED.decode(), hypothesis_yaml="id: [unclosed")

    complaints = judge(ws, store, wrong)

    assert [said.split(":")[0] for said in complaints] == ["id", "strategy_py", "hypothesis_yaml"]


def test_a_valid_candidate_earns_no_complaint(ws: Workspace, store: StateStore) -> None:
    assert judge(ws, store, answer()) == []


def test_a_candidate_may_certify_on_the_first_day_after_its_embargo_from_the_parent(
    ws: Workspace, store: StateStore
) -> None:
    """A 3d horizon embargoes 15 days, counted from the parent's research end, 2024-01-31."""
    boundary = candidate_yaml(
        horizon="3d", windows=windows((date(2023, 10, 1), date(2024, 1, 5)), date(2024, 2, 15))
    )

    assert judge(ws, store, answer(hypothesis_yaml=boundary)) == []


def test_the_embargo_is_counted_from_the_latest_research_any_pin_of_the_parent_held(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    """A re-pin that moved the windows earlier leaves a best researched on the later ones."""
    pin_earlier(
        store,
        researched,
        windows=windows((date(2024, 1, 1), date(2024, 2, 10)), date(2024, 2, 16)),
    )
    # Outside the embargo from the newest pin's research end, inside it from the older one's.
    inside = candidate_yaml(
        windows=windows((date(2024, 1, 1), date(2024, 1, 20)), date(2024, 2, 6))
    )
    after = candidate_yaml(
        windows=windows((date(2024, 1, 1), date(2024, 1, 20)), date(2024, 2, 15))
    )
    explorer(ws, [answer(hypothesis_yaml=inside), answer(hypothesis_yaml=after)])

    written = explore.explore(ws, store, researched)

    assert written.hyp_id == CANDIDATE_ID
    retry = [call for call in recorded.calls if call.task_class == "explore"][1]
    assert (
        "windows.certification.start: 2024-02-06 is inside the embargo counted from the end of "
        "the parent's research (2024-02-10) for a 1d horizon; certification may not open before "
        "2024-02-15"
    ) in retry.user


# --- when a lane explores ----------------------------------------------------


def stall(store: StateStore, best: str | None) -> None:
    store.event("stalled", HYP_ID, {"best_sha": best, "certifiable": False, "verdict": None})


def test_a_spell_of_stalls_on_one_best_calls_for_an_exploration(store: StateStore) -> None:
    assert explore.due(store, HYP_ID, 2) is False
    stall(store, "a" * 64)
    assert explore.due(store, HYP_ID, 2) is False
    stall(store, "a" * 64)
    assert explore.due(store, HYP_ID, 2) is True
    assert explore.due(store, HYP_ID, 0) is False


def test_a_stall_on_another_best_starts_the_spell_over(store: StateStore) -> None:
    stall(store, "a" * 64)
    stall(store, "b" * 64)
    assert explore.due(store, HYP_ID, 2) is False
    stall(store, "b" * 64)
    assert explore.due(store, HYP_ID, 2) is True


def test_a_hypothesis_that_never_kept_anything_stalls_on_the_same_nothing(
    store: StateStore,
) -> None:
    stall(store, None)
    stall(store, None)
    assert explore.due(store, HYP_ID, 2) is True


@pytest.mark.parametrize("attempt", [explore.EXPLORED, explore.EXPLORED_FAILED])
def test_an_attempt_failed_or_not_starts_the_spell_over(store: StateStore, attempt: str) -> None:
    stall(store, "a" * 64)
    stall(store, "a" * 64)
    store.event(attempt, HYP_ID, {})
    stall(store, "a" * 64)
    assert explore.due(store, HYP_ID, 2) is False
    stall(store, "a" * 64)
    assert explore.due(store, HYP_ID, 2) is True


def test_a_lane_explores_only_when_the_knob_and_the_spell_say_so(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    explorer(ws, [answer()])
    stall(store, "a" * 64)
    assert explore.after_stall(ws, store, researched, "l1") is None  # the template says never

    workspace = tuned(ws, explore_after_stalls=1)
    written = explore.after_stall(workspace, store, researched, "l1")

    assert written is not None and written.hyp_id == CANDIDATE_ID
    [event] = [event for event in store.events(subject=researched) if event.kind == "explored"]
    assert event.detail["lane"] == "l1"
    assert explore.after_stall(workspace, store, researched, "l1") is None  # the spell is spent


def test_a_lane_s_exploration_that_fails_is_an_event_and_not_a_failure(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    workspace = tuned(ws, explore_after_stalls=1)
    explorer(workspace, [{}])
    stall(store, "a" * 64)

    assert explore.after_stall(workspace, store, researched, "l1") is None

    [failed] = [e for e in store.events(subject=researched) if e.kind == explore.EXPLORED_FAILED]
    assert failed.detail["lane"] == "l1"
    assert "explore: no model answered" in str(failed.detail["error"])
    assert failed.detail["because"]
    assert not ws.path("hypotheses", CANDIDATE_ID).exists()
