"""Exploration: a draft hypothesis written from what a parent's research learned.

The parent is researched for real — a baseline and one proposed keep through the mock
register — so the coverage, the keeps and the pins the explorer reads are the loop's own
records rather than rows a test inserted. The candidate the explorer answers with is
scripted, and every refusal is one answer's worth of complaints on the router's ladder.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from kanso.config import ResearchConfig
from kanso.criteria import CardRun
from kanso.errors import PreconditionError
from kanso.hyp import show
from kanso.inbox import unread
from kanso.models import Answer, Call, reset_mock
from kanso.models import router as router_module
from kanso.nautilus import backtest
from kanso.nautilus.sizing import Refusal
from kanso.research import driver, explore, loop, records, scheduler
from kanso.schemas import Hypothesis, ModelSpec, parse_yaml
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import (
    DOCUMENT,
    FLAT,
    HYP_ID,
    INSTRUMENT,
    RAISING,
    READING,
    RESEARCH,
    REVERTING,
    classify,
)
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
    """The candidate's `hypothesis.yaml`: the parent's idea, unclassified, under a new id,
    naming the parent it derives from."""
    return yaml.safe_dump(
        {
            **UNCLASSIFIED,
            "id": CANDIDATE_ID,
            "title": "Demo: daily breakout on a synthetic saw-tooth",
            "thesis": "A close above the last two continues for a session.",
            "mechanism": "mean_reversion",
            "derived_from": [HYP_ID],
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
            "seed": written.seed.payload(),
            "rationale": answer()["rationale"],
        }
    ]
    [entry] = unread(store)
    assert (entry.kind, entry.subject) == ("explored", CANDIDATE_ID)
    assert entry.summary.startswith(f"from {researched}: as its card the seed scored ")
    assert entry.summary.endswith(
        ": the parent only faded falls; this buys a break above the last two closes"
    )
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
        "seed": written.seed.payload(),
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
    assert "Leave `prior_trials` out, even where the parent's file states it" in call.system
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
        "mechanisms",
    }
    assert facts["mechanisms"] == ["mean_reversion"], "the parent's own, and nothing declared"
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


DATED_ROW = {"pre": 872.9, "open": 37.2, "rth": 9.0, "post": 90.0, "n_sessions": 5, "n_open": 25}
"""A year's widths, as a dated spread states them: each a median of the whole year."""


def test_the_explorer_is_shown_no_row_of_a_dated_spread_and_a_candidate_keeps_them(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    """Each row is a year's median, in force from the year's first day, and the explorer
    writes a `strategy.py`: it is shown the line standing for the rows, and a candidate that
    keeps that line is written with the parent's rows put back."""
    costs = {
        "commission_bps": 0.5,
        "slippage_bps": 1.0,
        "spread": "dated",
        "dated": [
            {"instrument": INSTRUMENT, "from": "2023-01-01", **DATED_ROW},
            {"instrument": INSTRUMENT, "from": "2024-01-01", **DATED_ROW},
        ],
    }
    pinned = yaml.safe_dump({**DOCUMENT, "costs": costs}, sort_keys=False)
    sha = store.put_blob(pinned.encode("utf-8"))
    store.connection.execute(
        "UPDATE runs SET hypothesis_sha = ? WHERE hyp_id = ?", (sha, researched)
    )
    line = driver.WITHHELD.format(rows=2)
    explorer(ws, [answer(hypothesis_yaml=candidate_yaml(costs={**costs, "dated": line}))])

    explore.explore(ws, store, researched)

    (call,) = [call for call in recorded.calls if call.task_class == "explore"]
    shown = call.system + call.user
    assert "872.9" not in shown and "37.2" not in shown
    assert "2 rows, withheld" in shown
    written = ws.path("hypotheses", CANDIDATE_ID, "hypothesis.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(written)["costs"] == yaml.safe_load(pinned)["costs"]


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
            {"hypothesis_yaml": candidate_yaml(derived_from=[])},
            f"derived_from does not name the parent {HYP_ID!r}",
        ),
        (
            {
                "hypothesis_yaml": candidate_yaml(
                    prior_trials={"count": 27, "source": "evidence/grid.md", "sha256": "3f" * 32}
                )
            },
            "hypothesis_yaml: carries prior_trials; a candidate's search is its parent's, "
            "counted through `derived_from`, grids and cards alike, so leave it out",
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
    elsewhere = candidate_yaml(mechanism="momentum")
    complaints = judge(ws, store, {**wrong, "hypothesis_yaml": elsewhere})
    assert [said.split(":")[0] for said in complaints] == [
        "id",
        "strategy_py",
        "hypothesis_yaml",
        "hypothesis_yaml",
    ]
    assert "declares id 'demo_breakout'" in complaints[2]
    assert "mechanism 'momentum' is not" in complaints[3]


def test_a_valid_candidate_earns_no_complaint(ws: Workspace, store: StateStore) -> None:
    assert judge(ws, store, answer()) == []


@pytest.mark.parametrize(
    ("changes", "field", "source"),
    [
        (
            {"universe": [INSTRUMENT, "OTHER.XNYS"]},
            "universe lists 'OTHER.XNYS', which demo_mr's does not hold (DEMO.XNAS)",
            BREAKOUT + "\nOTHER = 'OTHER.XNYS'\n",
        ),
        (
            {"data_requirements": ["bar", "trade"]},
            "data_requirements lists 'trade', which demo_mr's does not require (bar)",
            BREAKOUT,
        ),
        ({"resolution": "1h"}, "resolution is '1h', and demo_mr's is '1d'", BREAKOUT),
    ],
)
def test_a_candidate_that_widens_what_the_parent_s_file_loads_is_refused_by_the_field(
    ws: Workspace, store: StateStore, changes: dict[str, Any], field: str, source: str
) -> None:
    """A seed measured under the parent's file can read no name, bar or type that file does
    not load, so the candidate's file may not ask for one. Line checks of the seed against
    both files gave two complaints no text joins under a narrowed universe, and two that
    contradict each other under another resolution; one complaint per field gives neither."""
    complaints = judge(
        ws, store, answer(hypothesis_yaml=candidate_yaml(**changes), strategy_py=source)
    )

    assert complaints == [
        f"hypothesis_yaml: {field}; the seed is measured once as a card of its parent before it "
        "is written, so a draft keeps or narrows its universe and data_requirements and keeps "
        "its resolution and bar_resolution; one that needs more is the operator's to write "
        "with `kanso hyp new`"
    ]
    # A narrowed universe is no departure: every name the candidate holds, the parent's file
    # loads, so the candidate's own alignment check implies the parent's and a seed line
    # outside it is refused once, by that check.
    wider = parse_yaml(
        Hypothesis, yaml.safe_dump({**DOCUMENT, "universe": [INSTRUMENT, "OTHER.XNYS"]}), "p"
    )
    assert explore._judge(ws, store, wider, RESEARCH[1], answer()) == []
    outside = answer(strategy_py=BREAKOUT + "\nOTHER = 'OTHER.XNYS'\n")
    (complaint,) = explore._judge(ws, store, wider, RESEARCH[1], outside)
    assert complaint.startswith("strategy_py: line ")
    assert f"'OTHER.XNYS' is not in the universe ({INSTRUMENT})" in complaint


def test_a_seed_whose_file_does_not_parse_is_read_against_the_parent_s(
    ws: Workspace, store: StateStore
) -> None:
    """Every check that can be made is made in one answer: the candidate's own file cannot
    be read, and the seed is about to run under the parent's, so it is read against that."""
    source = BREAKOUT + "\nOTHER = 'OTHER.XNYS'\n"

    complaints = judge(ws, store, answer(hypothesis_yaml="id: [unclosed", strategy_py=source))

    assert complaints[0].startswith("hypothesis_yaml: not valid YAML")
    line = len(BREAKOUT.splitlines()) + 2
    assert complaints[1:] == [
        f"strategy_py: as a card of {HYP_ID}, line {line}: 'OTHER.XNYS' is not in the universe "
        f"({INSTRUMENT})"
    ]


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        (READING.decode(), "line 2: import of 'os' is not on the allow-list"),
        (
            BREAKOUT.replace("self.long = False\n", "self.long = self.clock.utc_now()\n", 1),
            ".utc_now",
        ),
    ],
)
def test_a_seed_strategy_integrity_refuses_under_the_parent_is_a_complaint(
    ws: Workspace, store: StateStore, source: str, problem: str
) -> None:
    """A candidate was never executed before; now its seed is, as the parent's card, so the
    card's order holds for it too: the scan every card passes before it runs, first."""
    complaints = judge(ws, store, answer(strategy_py=source))

    scanned = [
        said for said in complaints if said.startswith(f"strategy_py: as a card of {HYP_ID}, ")
    ]
    assert any(problem in said for said in scanned), complaints
    assert all(
        said.endswith(
            f"; the seed runs as a card of {HYP_ID} before it is written, so it passes the scan "
            "every card passes before it runs"
        )
        for said in scanned
    )


# --- the parent's mechanism ----------------------------------------------------


def test_a_candidate_outside_its_parent_s_mechanism_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    """smr_premarket_drift, smr_breadth_thrust and smr_close_flow_ride each declared momentum
    under a mean-reversion parent, as the instruction invited, and the operator rejected all
    three as outside the mandate."""
    complaints = judge(ws, store, answer(hypothesis_yaml=candidate_yaml(mechanism="momentum")))

    assert complaints == [
        f"hypothesis_yaml: mechanism 'momentum' is not {HYP_ID}'s ('mean_reversion'); an "
        "explored draft tests another idea inside the mechanism its parent's research "
        "measured, so state mechanism: mean_reversion"
    ]


def test_a_mechanism_the_workspace_declares_is_admitted_and_another_is_named(
    ws: Workspace, store: StateStore
) -> None:
    """The key widens what a draft may move to; it is the operator's list, not a free choice."""
    workspace = tuned(ws, explore_mechanisms=["momentum"])

    assert (
        judge(workspace, store, answer(hypothesis_yaml=candidate_yaml(mechanism="momentum"))) == []
    )
    (complaint,) = judge(
        workspace, store, answer(hypothesis_yaml=candidate_yaml(mechanism="carry"))
    )
    assert (
        f"mechanism 'carry' is not {HYP_ID}'s ('mean_reversion') nor one [research] "
        "explore_mechanisms admits (momentum);"
    ) in complaint
    assert complaint.endswith("so state mechanism: mean_reversion or one of momentum")


def test_the_parent_s_own_mechanism_is_admitted_whatever_the_workspace_declares(
    ws: Workspace, store: StateStore
) -> None:
    """The key adds mechanisms a draft may move to and never removes the parent's own."""
    workspace = tuned(ws, explore_mechanisms=["stat_arb"])

    assert judge(workspace, store, answer()) == []


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


# --- the seed, measured once as the parent's card ----------------------------


@dataclass
class Spawned:
    """Every card child a test's exploration asked for, and the directory it ran in."""

    requests: list[backtest.RunRequest]
    workdirs: list[Path]


def spy(monkeypatch: pytest.MonkeyPatch, after: Callable[[], None] = lambda: None) -> Spawned:
    """Watch `backtest.run_subprocess`, which a probe runs its seed through, as a card does."""
    seen = Spawned([], [])
    real = backtest.run_subprocess

    def spying(
        request: backtest.RunRequest,
        catalog: Path,
        workdir: Path,
        extensions: Sequence[tuple[str, str]] = (),
    ) -> backtest.RunResult:
        seen.requests.append(request)
        seen.workdirs.append(workdir)
        result = real(request, catalog, workdir, extensions)
        after()
        return result

    monkeypatch.setattr(backtest, "run_subprocess", spying)
    return seen


@pytest.fixture
def spawned(researched: str, monkeypatch: pytest.MonkeyPatch) -> Spawned:
    """The children an exploration of the researched parent spawns, the parent's own not
    among them."""
    return spy(monkeypatch)


def explored_calls(recorded: Recorder) -> list[Call]:
    return [call for call in recorded.calls if call.task_class == "explore"]


def respelt(store: StateStore, hyp_id: str, comment: str = "the parent's best") -> str:
    """The parent's best with a comment added: new bytes, the same book and the same number."""
    best, _ = records.best_of(store, hyp_id)
    assert best is not None
    return store.get_blob(best).decode("utf-8") + f"\n# {comment}, spelt otherwise\n"


def test_every_draft_reaches_the_inbox_with_its_seed_s_score(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    """Sixteen drafts reached the operator with a rationale and no number, and four held
    ones needed an offline workflow (wf_6bb22438) to get one."""
    explorer(ws, [answer()])

    written = explore.explore(ws, store, researched)

    [event] = [event for event in store.events(subject=researched) if event.kind == "explored"]
    seed = event.detail["seed"]
    blob = store.get_blob(seed["measured_sha"])
    measured = records.read_measurement(json.loads(zlib.decompress(blob)))
    assert (seed["metric"], seed["se"]) == measured.compute()
    assert seed["trades"] == len(measured.run.trades) > 0
    assert (seed["objective"], seed["window"]) == ("wf_sharpe_net", ["2024-01-01", "2024-01-31"])
    assert written.payload()["seed"] == seed
    assert event.detail["rationale"] == answer()["rationale"]
    [entry] = unread(store)
    assert entry.summary.startswith(
        f"from {researched}: as its card the seed scored {seed['metric']:.4g} ± "
        f"{seed['se']:.2g} on {seed['trades']} trades (best "
    )


def test_the_seed_is_measured_under_the_parent_s_newest_pins_over_its_research_window(
    ws: Workspace, store: StateStore, spawned: Spawned
) -> None:
    """The repeat check selects the parent's books by these pins and this reading, and under
    any other the books match nothing; the window is the embargo."""
    older = pin_earlier(
        store, HYP_ID, windows=windows((date(2023, 12, 1), date(2023, 12, 20)), date(2024, 2, 6))
    )
    store.connection.execute(
        "UPDATE runs SET snapshot_id = 'an-older-snapshot' WHERE run_id = ?", (older,)
    )
    explorer(ws, [answer()])

    explore.explore(ws, store, HYP_ID)

    *_, newest = records.runs_of(store, HYP_ID)
    assert newest.run_id != older
    [request] = spawned.requests
    assert request.window == RESEARCH
    assert request.snapshot_id == newest.snapshot_id != "an-older-snapshot"
    assert request.hyp == explore._pinned(store, newest.hypothesis_sha)
    assert request.hyp.windows.research.end == RESEARCH[1] != date(2023, 12, 20)
    assert request.budget_s == newest.card_budget_s
    assert request.mem_cap_gb == loop.mem_cap(ws, newest)
    assert request.strategy_source == BREAKOUT.encode("utf-8")  # a sleeve: the seed itself


def test_a_seed_that_repeats_a_parent_card_is_refused_as_that_card(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    """breadth_intraday's seed was smr_breadth's cards 24fe069 and d82d375 again, and
    breadth_turn_target's exit its parent's card 3928eb8."""
    best, _ = records.best_of(store, researched)
    assert best is not None
    explorer(ws, [answer(strategy_py=respelt(store, researched)), answer()])

    written = explore.explore(ws, store, researched)

    assert written.strategy_sha == sha256(BREAKOUT.encode()).hexdigest()
    retry = explored_calls(recorded)[1]
    assert (
        f"strategy_py: run as a card of {researched}, the seed held the same book as the "
        f'parent\'s card {best[:7]} ("run in revert mode") on '
    ) in retry.user
    assert (
        f"it repeats the parent's card {best[:7]}; write a seed whose book or result" in retry.user
    )


def test_a_book_matched_without_its_number_is_admitted_and_recorded(
    ws: Workspace, store: StateStore, researched: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kanso's redundancy rule refuses a repeat of the book and the number: a number that
    moved is a measurement (`records.matched_book`: 106 of 3,092 refusals were wrong on the
    book alone)."""
    best, _ = records.best_of(store, researched)
    assert best is not None
    moved = records.Match(best, 30, 31, 9.5, agreed=False)
    monkeypatch.setattr(records, "matched_book", lambda *args: moved)
    explorer(ws, [answer()])

    written = explore.explore(ws, store, researched)

    assert written.seed.same_book == moved
    assert (
        written.payload()["seed"]["same_book"]
        == {  # type: ignore[index]
            "like": best[:7],
            "pct": round(100 * 30 / 31, 2),
            "metric": 9.5,
        }
    )


def test_a_seed_measured_under_a_reading_no_parent_card_was_says_it_compared_nothing(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    """A check against an empty pool is no pass (`matched_book`'s own record: 3,043 of 3,228
    cards judged before the first book was stored read as matching nothing), and a best
    struck under four folds is not the number a three-fold seed is set beside."""
    workspace = tuned(ws, folds=3)
    explorer(workspace, [answer()])

    written = explore.explore(workspace, store, researched)

    assert (written.seed.books, written.seed.parent_best) == (0, None)
    [entry] = unread(store)
    assert "; no book of it stored to compare: the parent only faded falls" in entry.summary
    assert "(best" not in entry.summary


def test_the_parent_s_best_beside_the_seed_is_the_one_measured_under_the_seed_s_reading(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    explorer(ws, [answer()])

    written = explore.explore(ws, store, researched)

    best, metric = records.best_of(store, researched)
    assert best is not None
    *_, newest = records.runs_of(store, researched)
    reading = loop.setup_of(ws, store, newest).measured_under
    assert written.seed.parent_best == records.stored_metric(store, newest, best, reading)
    assert written.seed.parent_best == metric, "one reading, so the best's own number"
    assert written.seed.books == 2, "the flat baseline's book and the keep's"
    assert written.seed.inherited_trials == records.n_trials(store, researched) == 2
    assert f"(best {metric:.4g}), 2 trials inherited: " in unread(store)[0].summary


def test_a_seed_that_places_no_order_is_refused_as_the_parent_s_empty_book(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    """A flat run scores a flat run's zero with no error, so by both clauses it repeats every
    card of the parent that placed no order; the complaint says that, not "another book"."""
    explorer(ws, [answer(strategy_py=FLAT.decode()), answer()])

    written = explore.explore(ws, store, researched)

    baseline = records.cards_of(store, researched)[0]
    retry = explored_calls(recorded)[1]
    assert (
        f"strategy_py: run as a card of {researched} over 2024-01-01..2024-01-31, the seed "
        f'placed no order, and the parent\'s card {baseline.sha7} ("baseline") already measured '
        "an empty book; write a seed that trades the parent's universe at its resolution on "
        "data it requires"
    ) in retry.user
    assert "held the same book" not in retry.user
    assert written.strategy_sha == sha256(BREAKOUT.encode()).hexdigest()


def test_a_seed_that_places_no_order_is_written_where_no_empty_book_is_stored(
    ws: Workspace, store: StateStore
) -> None:
    scripted(ws)
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    loop.begin(ws, store, hyp_id)
    explorer(ws, [answer(strategy_py=FLAT.decode())])

    written = explore.explore(ws, store, hyp_id)

    assert (written.seed.trades, written.seed.books) == (0, 1)
    assert written.seed.same_book is None


RECORDED: Final = ("cards", "signatures", "runs")


def test_a_probe_writes_no_card_no_signature_and_no_trial(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    """A probe recorded as the parent's card would make the parent's later cards redundant
    against a book none of its cards held, and move its stall and trial counts."""

    def rows(table: str) -> list[tuple[object, ...]]:
        return [tuple(row) for row in store.connection.execute(f"SELECT * FROM {table}")]

    before = {table: rows(table) for table in RECORDED}
    trials = records.n_trials(store, researched)
    blobs = {str(row[0]) for row in store.connection.execute("SELECT sha FROM blobs")}
    (last,) = store.connection.execute("SELECT MAX(event_id) FROM events").fetchone()
    explorer(ws, [answer()])

    written = explore.explore(ws, store, researched)

    assert {table: rows(table) for table in RECORDED} == before
    assert records.n_trials(store, researched) == trials
    kinds = [
        str(row[0])
        for row in store.connection.execute("SELECT kind FROM events WHERE event_id > ?", (last,))
    ]
    assert not {"card", "same_book", "redundant"} & set(kinds), kinds
    added = {str(row[0]) for row in store.connection.execute("SELECT sha FROM blobs")} - blobs
    assert added == {written.strategy_sha, written.seed.measured_sha}


def test_each_answer_that_passes_the_static_checks_is_measured_once(
    ws: Workspace, store: StateStore, spawned: Spawned
) -> None:
    """The cost bound: an answer refused statically is never executed — the seed importing
    `os` is the integrity scan's, before anything runs, as a card's is — and every answer
    that passes is run once."""
    explorer(ws, [answer(strategy_py=READING.decode()), answer()])

    explore.explore(ws, store, HYP_ID)

    assert len(spawned.requests) == 1


def test_a_seed_refused_after_its_run_costs_one_run_and_the_next_answer_another(
    ws: Workspace, store: StateStore, spawned: Spawned
) -> None:
    explorer(ws, [answer(strategy_py=respelt(store, HYP_ID)), answer()])

    explore.explore(ws, store, HYP_ID)

    assert [request.strategy_source == BREAKOUT.encode() for request in spawned.requests] == [
        False,
        True,
    ]


HELD = {
    **DOCUMENT,
    "benchmark": {"hold": "first_leg"},
    "objective": {"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
}
"""The demo sleeve measured against a hold of its one instrument (`test_loop.HELD`)."""


def test_a_parent_s_hold_is_run_once_for_every_seed_of_an_exploration(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold every seed is differenced against is the run's request with the strategy
    replaced, as its `begin` ran it; an exploration that measures two seeds runs it once."""
    scripted(ws)
    hyp_id = classify(ws, store, HELD)
    loop.begin(ws, store, hyp_id)
    derived: list[object] = []
    original = backtest.benchmark

    def counted(request: backtest.RunRequest) -> backtest.RunRequest:
        derived.append(request.window)
        return original(request)

    monkeypatch.setattr(loop.backtest, "benchmark", counted)
    flat = FLAT.decode() + "\n# spelt otherwise\n"
    explorer(ws, [answer(strategy_py=flat), answer()])

    written = explore.explore(ws, store, hyp_id)

    assert derived == [RESEARCH]
    assert written.seed.objective == "wf_sharpe_vs_hold"


def crashed(request: backtest.RunRequest, **changes: Any) -> backtest.RunResult:
    """What the runner hands back for a card that did not run to a result."""
    run = CardRun(
        window=request.window,
        period="1d",
        period_ends_ns=(),
        returns=(),
        equity=(),
        trades=(),
        fills=(),
        capital=request.capital,
        currency="USD",
        venue_model=request.venue_model,
    )
    made: dict[str, Any] = {"wall_s": 1.0, "peak_mem_gb": 0.1, **changes}
    return backtest.RunResult(run=run, intents=(), **made)


def first_returns(
    monkeypatch: pytest.MonkeyPatch, made: Callable[[backtest.RunRequest], backtest.RunResult]
) -> None:
    """The first seed's run comes back as `made` says; every later one runs."""
    real = backtest.run_subprocess
    calls: list[object] = []

    def once(
        request: backtest.RunRequest,
        catalog: Path,
        workdir: Path,
        extensions: Sequence[tuple[str, str]] = (),
    ) -> backtest.RunResult:
        calls.append(request)
        if len(calls) == 1:
            return made(request)
        return real(request, catalog, workdir, extensions)

    monkeypatch.setattr(backtest, "run_subprocess", once)


REFUSAL = Refusal(
    rule="full_book",
    ts_event=0,
    instrument_id=INSTRUMENT,
    asked="BUY 500",
    held={},
    why="the order is over the budget",
)


@pytest.mark.parametrize(
    ("made", "complaint"),
    [
        (
            None,
            "strategy_py: run as a card of demo_mr, the seed raised:\n",
        ),
        (
            lambda request: crashed(request, crashed=True, reason=backtest.BUDGET, wall_s=184.0),
            "strategy_py: run as a card of demo_mr, the seed was killed after 184 s, over the "
            "{budget:.0f} s a card of demo_mr may take; write a seed no slower than the "
            "parent's cards",
        ),
        (
            lambda request: crashed(
                request, crashed=True, reason=backtest.MEMORY, peak_mem_gb=33.1
            ),
            "strategy_py: run as a card of demo_mr, the seed was killed at 33.10 GB, over the "
            "{cap:.2f} GB a card of demo_mr may hold; write a seed no heavier than the parent's "
            "cards",
        ),
        (
            lambda request: crashed(request, refused=REFUSAL),
            "strategy_py: run as a card of demo_mr over 2024-01-01..2024-01-31, the seed's order "
            f"was refused by sizing rule full_book at {INSTRUMENT}: the order is over the "
            "budget; write a seed whose orders the parent's sizing admits",
        ),
    ],
    ids=["raised", "clock", "memory", "sizing"],
)
def test_a_seed_that_does_not_run_is_a_complaint_naming_what_happened(
    ws: Workspace,
    store: StateStore,
    researched: str,
    recorded: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    made: Callable[[backtest.RunRequest], backtest.RunResult] | None,
    complaint: str,
) -> None:
    """A seed that cannot run as its parent's card would be a baseline that refuses its own
    run, and a hypothesis whose starting point cannot run has nothing to research."""
    first = RAISING.decode() if made is None else answer()["strategy_py"] + "\n# first\n"
    if made is not None:
        first_returns(monkeypatch, made)
    explorer(ws, [answer(strategy_py=first), answer()])

    written = explore.explore(ws, store, researched)

    assert written.strategy_sha == sha256(BREAKOUT.encode()).hexdigest()
    *_, newest = records.runs_of(store, researched)
    said = complaint.format(budget=newest.card_budget_s, cap=loop.mem_cap(ws, newest))
    retry = explored_calls(recorded)[1].user
    assert said in retry
    if made is None:
        assert (
            "RuntimeError: the card asked for the impossible\nfix the fault; a seed that does "
            "not run" in retry
        )


@pytest.mark.parametrize("how", ["raised", "crashed"])
def test_a_measurement_that_fails_for_the_workspace_fails_the_attempt_once(
    ws: Workspace,
    store: StateStore,
    researched: str,
    recorded: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """The model cannot mend a catalog, so asking it again would spend a frontier call."""
    remedy = "run `kanso data snapshot`, then explore again"

    def failing(request: backtest.RunRequest, *args: object) -> backtest.RunResult:
        if how == "raised":
            raise PreconditionError("the catalog no longer holds the window", remedy=remedy)
        return crashed(
            request, crashed=True, reason=backtest.EXCEPTION, traceback_tail="gone", remedy=remedy
        )

    monkeypatch.setattr(backtest, "run_subprocess", failing)
    explorer(ws, [answer(), answer()])

    with pytest.raises(PreconditionError) as caught:
        explore.explore(ws, store, researched)

    assert caught.value.remedy == remedy
    [failed] = [e for e in store.events(subject=researched) if e.kind == explore.EXPLORED_FAILED]
    assert failed.detail["because"] == remedy
    assert len(explored_calls(recorded)) == 1
    assert not ws.path("hypotheses", CANDIDATE_ID).exists()
    assert unread(store) == []


def test_a_parent_whose_pins_cannot_be_set_up_fails_before_the_model_is_called(
    ws: Workspace,
    store: StateStore,
    researched: str,
    recorded: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer that cannot be measured is not paid for."""

    def gone(*args: object) -> loop.Setup:
        raise PreconditionError("construct.host: 'gone' is not a composed strategy", remedy="R")

    monkeypatch.setattr(loop, "setup_of", gone)
    explorer(ws, [answer()])

    with pytest.raises(PreconditionError, match="not a composed strategy"):
        explore.explore(ws, store, researched)

    assert explored_calls(recorded) == []
    (spent,) = store.connection.execute(
        "SELECT COUNT(*) FROM spend WHERE task_class = 'explore'"
    ).fetchone()
    assert spent == 0
    [failed] = [e for e in store.events(subject=researched) if e.kind == explore.EXPLORED_FAILED]
    assert failed.detail["because"] == "R"


@pytest.fixture
def drained() -> Iterator[None]:
    """A process told to drain, as a daemon lane is on `research stop --drain`."""
    backtest.drain()
    try:
        yield
    finally:
        backtest.resume()


def nothing_left(ws: Workspace, store: StateStore, hyp_id: str) -> None:
    """No event of an attempt, no draft, no probe directory, and the spell not spent."""
    kinds = {event.kind for event in store.events(subject=hyp_id)}
    assert not {explore.EXPLORED, explore.EXPLORED_FAILED} & kinds
    assert not ws.path("hypotheses", CANDIDATE_ID).exists()
    assert not ws.path("runs", "op", f".explore-{hyp_id}").exists()
    assert explore.due(store, hyp_id, 1) is True


def test_a_drain_that_refuses_the_probe_leaves_no_event_and_no_draft(
    ws: Workspace, store: StateStore, researched: str, drained: None
) -> None:
    """A drain is the operator's word, as `loop.begin` treats one; counting it would restart
    the lane's spell."""
    stall(store, "a" * 64)
    explorer(ws, [answer()])

    with pytest.raises(backtest.DrainedError):
        explore.explore(ws, store, researched)

    nothing_left(ws, store, researched)


def test_a_stop_while_the_seed_runs_leaves_no_event_and_no_draft(
    ws: Workspace, store: StateStore, researched: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`research stop` kills a probe's child, and the interrupt carries a run's remedy;
    recorded, it would spend the spell on the operator's own word."""
    told: list[bool] = []

    def interrupted(*args: object) -> backtest.RunResult:
        told.append(True)
        raise backtest._interrupted()

    monkeypatch.setattr(backtest, "run_subprocess", interrupted)
    stall(store, "a" * 64)
    explorer(ws, [answer()])

    with pytest.raises(PreconditionError, match="the card was interrupted"):
        explore.explore(ws, store, researched, stop=lambda: bool(told))

    nothing_left(ws, store, researched)
    reset_mock()
    with pytest.raises(PreconditionError, match="the card was interrupted"):
        explore.explore(ws, store, researched)
    [failed] = [e for e in store.events(subject=researched) if e.kind == explore.EXPLORED_FAILED]
    assert failed.detail["because"] == "start the daemon again; the run resumes from its last card"


def test_a_lane_told_to_drain_while_the_seed_ran_asks_the_model_nothing_more(
    ws: Workspace,
    store: StateStore,
    researched: str,
    recorded: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain begins nothing more, and a frontier call made after one waits up to 420 s for
    an answer the next spawn would refuse."""
    told: list[bool] = []
    spy(monkeypatch, after=lambda: told.append(True))
    stall(store, "a" * 64)
    explorer(ws, [answer(strategy_py=respelt(store, researched)), answer()])

    with pytest.raises(PreconditionError, match="stopped before it asked again") as caught:
        explore.explore(ws, store, researched, stop=lambda: bool(told))

    assert caught.value.remedy == "start the daemon again; the parent's next stall explores"
    assert len(explored_calls(recorded)) == 1
    nothing_left(ws, store, researched)


def test_a_lane_told_to_stop_before_the_seed_runs_starts_none(
    ws: Workspace, store: StateStore, spawned: Spawned
) -> None:
    stall(store, "a" * 64)
    explorer(ws, [answer()])

    with pytest.raises(PreconditionError, match="stopped before it ran the seed"):
        explore.explore(ws, store, HYP_ID, stop=lambda: True)

    assert spawned.requests == []
    nothing_left(ws, store, HYP_ID)


def test_the_probe_runs_in_a_directory_of_its_own_and_leaves_none(
    ws: Workspace, store: StateStore, spawned: Spawned
) -> None:
    """`run_subprocess` empties `<workdir>/.card/` before a card, so a probe in the parent's
    lane directory would destroy the report of a card running there."""
    lane = ws.path("runs", "op", HYP_ID)
    held = {path.name: path.read_bytes() for path in lane.iterdir() if path.is_file()}
    explorer(ws, [answer()])

    explore.explore(ws, store, HYP_ID)

    assert spawned.workdirs == [ws.path("runs", "op", f".explore-{HYP_ID}")]
    assert not spawned.workdirs[0].exists()
    assert {path.name: path.read_bytes() for path in lane.iterdir() if path.is_file()} == held
    assert records.active(store, HYP_ID) is not None, "the parent's run is still the lane's"


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
    never = tuned(ws, explore_after_stalls=0)
    assert explore.after_stall(never, store, researched, "l1") is None  # zero is never

    workspace = tuned(ws, explore_after_stalls=1)
    written = explore.after_stall(workspace, store, researched, "l1")

    assert written is not None and written.hyp_id == CANDIDATE_ID
    [event] = [event for event in store.events(subject=researched) if event.kind == "explored"]
    assert event.detail["lane"] == "l1"
    assert explore.after_stall(workspace, store, researched, "l1") is None  # the spell is spent


def test_a_workspace_as_init_wrote_it_explores_at_the_fifth_stall_on_one_best(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    """The shipped default: a lane left to the template asks for another hill once five
    stalls have ended on the same best since the last exploration, and not before."""
    assert ws.config.research.explore_after_stalls == ResearchConfig().explore_after_stalls == 5
    explorer(ws, [answer()])
    for _ in range(4):
        stall(store, "a" * 64)
        assert explore.after_stall(ws, store, researched, "l1") is None

    stall(store, "a" * 64)
    written = explore.after_stall(ws, store, researched, "l1")

    assert written is not None and written.hyp_id == CANDIDATE_ID


def test_a_workspace_with_no_model_pays_an_event_per_spell_for_the_default(
    ws: Workspace, store: StateStore, researched: str, recorded: Recorder
) -> None:
    """With no register the default costs a modelless workspace no call: one
    `explored_failed` event at the fifth stall, then a fresh spell of five, so the second
    arrives at the tenth stall and not before."""
    ws.path("models.yaml").unlink()
    calls = len(recorded.calls)

    def failed_after_each_of(stalls: int) -> list[int]:
        counts = []
        for _ in range(stalls):
            stall(store, "a" * 64)
            assert explore.after_stall(ws, store, researched, "l1") is None
            kinds = [e.kind for e in store.events(subject=researched)]
            counts.append(kinds.count(explore.EXPLORED_FAILED))
        return counts

    assert failed_after_each_of(5) == [0, 0, 0, 0, 1]
    assert explore.due(store, researched, ws.config.research.explore_after_stalls) is False
    assert failed_after_each_of(5) == [1, 1, 1, 1, 2]

    assert len(recorded.calls) == calls
    assert explore.EXPLORED not in [e.kind for e in store.events(subject=researched)]
    assert not ws.path("hypotheses", CANDIDATE_ID).exists()


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


def test_a_ladder_of_repeats_fails_the_exploration_naming_the_card_it_repeats(
    ws: Workspace, store: StateStore, researched: str
) -> None:
    """Both answers re-spell the parent's best: the lane's spell is spent on an event whose
    remedy says why, and no draft is written."""
    workspace = tuned(ws, explore_after_stalls=1)
    best, _ = records.best_of(store, researched)
    assert best is not None
    explorer(
        workspace,
        [
            answer(strategy_py=respelt(store, researched, "once")),
            answer(strategy_py=respelt(store, researched, "twice")),
        ],
    )
    stall(store, "a" * 64)

    assert explore.after_stall(workspace, store, researched, "l1") is None

    [failed] = [e for e in store.events(subject=researched) if e.kind == explore.EXPLORED_FAILED]
    assert f"it repeats the parent's card {best[:7]}" in str(failed.detail["because"])
    assert not ws.path("hypotheses", CANDIDATE_ID).exists()
    assert not ws.path("runs", "l1", f".explore-{researched}").exists()
