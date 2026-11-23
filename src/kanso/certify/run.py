"""Running a certification plan: what is judged, on what data, and what the verdict does.

Certification is the one place a strategy meets the window that was kept away from it.
The subject is a card of the hypothesis — its `best`, or a named one — and it is judged
exactly as it was researched: the same snapshot the run that produced it was pinned to,
the same venue model that card was costed with, the same hypothesis bytes the run pinned.
Nothing about the subject is re-derived from the workspace as it stands now, because a
number that moved because a file moved is not evidence.

**Both windows are run, always, the research window first.** The certification window is
what the plan's gates judge; the research window is run beside it because the gates that
matter most compare the two, and an out-of-sample number means nothing beside an in-sample
one measured under different conditions. A relative construct adds its host's run over each
window, since its objective is a difference, and a hypothesis measured against a benchmark
adds the benchmark's run over each — produced by the runner from the subject's own request
for that window, and held fixed while a perturbation gate moves the subject. Every research
run is made before the certification window is read, so a stall's certification can stop
between the two.

**A stall certifies only a best whose research gates pass on research data.** A cert gate
whose library file says `reads: research` — the search correction the plan pins for the
subject's objective, the gate whose file `deflates` it, beside `walk_forward_consistency` and
`research_plateau`, both of which every plan carries — judges the research window's runs, the
trials and nothing of the certification window, and a certificate is the conjunction of the
gates that judged, so one of them that judged the subject and refused it fails the
certificate whatever the certification window shows. Reading that window then buys a verdict
already known and spends a look at the one window research never saw. So the research gates
are judged first (`_research_readings`): the correction, then the others in the plan's order,
`research_plateau` last, since it alone costs backtests — two of the research window per
parameter it moves, made only once every cheaper research gate has passed. Before a stall
certifies they are judged twice. In the lane that stalled (`precheck`), from the measurement
the subject card kept, when that measurement was struck the way certification would strike it
now, on a snapshot that can support a claim, running nothing — so the plateau is left to the
child. In the certification child itself, on the research window it has just run, before the
certification window is read (`certify_at_stall`). Either refusal spares the stall: no
certification window is read and no certificate is written. A gate that judged nothing — a
plan that carries no correction before the search has two draws, cards and declared settings
together, or a gate that skips on the research run — spares nothing, since it would judge
nothing in the certificate either. The certificate records each research gate as the child
read it, never evaluated again, so a card another lane records while the window runs does not
move it and every backtest is paid once. `kanso cert run` is the operator's own act and is
never spared: every research gate is judged, the plateau's backtests are not made once another
research gate has refused, and the certification window is read regardless.

**Both windows are measured the way a card is.** The objective the certificate records,
and every gate's reading of it, is taken over the workspace's `[research] folds` equal
calendar folds of the window it is read on, the certification window included however
short it is. It is therefore a mean of folds and not of sessions: five sessions in four
folds put two of them in one fold at half a session's weight each, and a fold that holds
no period scores zero, so the certificate's number is not the mean per session of the run
that produced it.

**An inadmissible snapshot fails the plan rather than raising.** A snapshot holding a
dataset whose publication nobody declared, or one whose prices a vendor adjusted as of the
day they were requested, cannot support a point-in-time claim: the first cannot be dated,
the second cannot be fetched again. Certification does not run on it and does not exit
with a code either — every gate of the plan is recorded as failed with the reason, so the
verdict is an ordinary failing verdict that counts toward the failure run and reaches the
operator through the escalation everything else uses. That is the difference between a
gate that cannot reach its context, which passes and says so, and one whose context is
present but inadmissible, which fails.

**A gate the toolbox declares but nothing implements is skipped, not failed.** The
certificate says which gate judged nothing and why, so a plan naming a gate whose
machinery arrives with a later milestone still produces an honest verdict over the gates
that did run.

**A gate that perturbs a parameter runs its window again through this runner** — the
certification window for `param_plateau` and `latency_stress`, the research window for
`research_plateau`. The window is read from the catalog again, a chunk at a time, as the
first run read it, and the perturbed run is extracted by exactly the arithmetic the
unperturbed one was. No window is
held between runs: a certification holds one read and one chunk of the window it is running,
whichever run that is, so what it costs does not grow with the window's length or with how
many runs its plan makes, and a perturbation costs one engine run and one read of the window.
Only the subject's own parameters can move — a sleeve's own configuration fields, or the
construct parameters an attached modifier was composed with — never the capital, the risk
limits or anything else the hypothesis injects. A strategy that raises at a setting
`research_plateau` moved it to has not refused a test: the card ran where the search left
it, so the move is one that kept nothing and the gate refuses the subject (`MovedRunError`):
a stall is spared on that evidence and a certificate by hand fails on it. Every plan carries
that gate, and a refusal there would refuse every certification of such a best, at every
stall. A run of the certification window that raises, a move of it included, still refuses
the certification (`docs/backlog.md` row 342).

**A gate that moves the trades in time reads the window once more.** `entry_timing` prices
the subject's trades at other sessions (`kanso.criteria.timing`), at prices the run does not
carry, so it is handed a reader that reads the certification window again, a chunk at a time
as every run reads it, and folds from each chunk only the readings at the instruments and New
York times of day the trades were struck at — a number per key per session, with the split
schedules the window's definitions carry — and nothing else of the window. No engine runs: the
read is the whole cost, and a plan that does not name the gate never pays it.

**A failed window spares the perturbation backtests, and that read.** The gates are evidence
for a pass, and a certificate is the conjunction of the gates that judged, so once
`embargoed_window` — required of every plan — has judged the subject and refused it, nothing
`param_plateau` could score changes the verdict, nor anything `entry_timing` could price. Those
gates are therefore judged after every other cert gate and, when the window gate failed,
recorded as skipped with the reason instead of running a backtest per parameter or reading the
window again; the certificate lists its gates in the plan's order regardless. A window gate
that judged nothing spares nothing: a skip is an absence of evidence, not a refusal, and the
plateau then refuses a loss itself before moving anything. The research gates were judged
before the window was read, so nothing the window shows spares or moves them.

**A search is priced in full, or no certificate is written.** The deflation a plan must carry
from the search's second draw takes its expected maximum over every card of the hypothesis's family
that ran and traded, under any objective, and its spread from the cards scored under the
subject's own (`kanso.criteria.gates._searched`): a hypothesis re-pinned to another
objective carries across the strategy the earlier search selected, and it is that search
the certificate prices. The settings the family declares tried outside kanso in the grids
that chose its seeds (`kanso.research.records.prior_trials`) are draws of the same search,
so they join the cards in every count here — the draws, the count of two from which a plan
carries the correction, and the two refusals below — and in no spread, and the
certificate's `n_trials` stays kanso's cards. Two cases are refused before anything runs,
because each would otherwise be a certificate whose correction judged nothing and so
passed. One is a subject behind two or more such draws with fewer than two cards scored in
its objective's units — the carried strategy certified as the first card under its new
objective, or the first card behind a declared grid — whose spread cannot be read
(`unpriced`). The other is a subject scored under an objective whose correction the pinned
plan does not carry, with its threshold, once the search has two draws: `cert run --sha` of
a card from before the re-pin, whose plan was made for the objective the hypothesis has now.
That route is refused too for a subject scored under an objective a cert gate guards —
`entry_timing` on a contribution or a Sharpe — when the plan does not carry the gate with
every parameter set, at any number of cards: its certificate would credit a drift held as
timing.

**Every certificate is a look at its window, and records which.** The certificates the
subject's lineage has already made at a certification window intersecting the subject's
(`kanso.certify.looks`) are counted when the certificate is written, under the write lock its
row is written under, so two certifications of one lineage judged at once are two looks in
the order they were written; the certificate records the window, its look — one more than
those — and the lineage (`holdout`). The window is the subject's, read off the file its run
pinned, and a plan certification makes where none is pinned is shown the looks taken there,
not at the window the hypothesis is registered under now (`plan`'s `sha`), so a card carded
before a re-pin that moved the window is the look after those its planner was told of. The
count is shown and judges nothing: every gate that
judges, the search correction included, does so by the threshold the plan pins at any look, a
stall's precheck as well, and a correction that skips passes at any look as at the first. One
struck on a snapshot that cannot support a claim read no window and records none.

The verdict moves the hypothesis and never ends it: a pass certifies it, a fail returns it
to research with the ids of its failing gates recorded where the proposer reads them, and
every `n_fail`-th consecutive failure escalates. The ids alone, because a certification
gate measures the certification window and its evidence carries those numbers; the embargo
does not have an exception for feedback. Ending a line of research is the operator's
decision and `kanso hyp retire` is the whole of it. A pass also
composes the version the certificate implies and offers it to the paper stage, because both
acts follow from the certificate with no decision left in them; a stage that cannot take it
escalates and the verdict still stands.

Engine facts this module relies on (nautilus_trader 1.231.0): a backtest is built and run
in the process certifying by `kanso.nautilus.backtest`, whose window refusal is what keeps a
certification on the window it asked for; every data point carries `ts_event` and
`ts_init` as nanosecond integers, and the difference between them is the publication delay
the availability gate measures.
"""

from __future__ import annotations

import gc
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import inf
from typing import TYPE_CHECKING, Any, Final, cast

from kanso import ext
from kanso.certify import certificate
from kanso.certify.looks import INADMISSIBLE, read_no_window
from kanso.certify.looks import looks as looks_at
from kanso.certify.plan import plan as plan_for
from kanso.certify.power import power, window_params
from kanso.classify.construct import Harness, HostRef
from kanso.classify.construct import get as construct_for
from kanso.criteria import (
    CardRun,
    DatasetFacts,
    GateContext,
    criteria_version,
    gates,
    objectives,
    plannable,
)
from kanso.criteria import catalogue as toolbox
from kanso.criteria.context import MovedRunError, skipped
from kanso.criteria.digest import session_edge
from kanso.criteria.library import (
    SEARCH_TRIALS,
    deflation_gate,
    required_cert_gates,
    required_deflation,
    research_gates,
)
from kanso.criteria.objectives import measures_benchmark
from kanso.criteria.run import day_of, midnight_ns
from kanso.criteria.timing import BAR, PRINT, QUOTE, Fold, PriceKey, SessionPrices
from kanso.data.manifest import Manifest, catalog_path, manifests
from kanso.data.publication import NANOS_PER_SECOND, PUBLICATION_RULES
from kanso.data.snapshot import Snapshot
from kanso.data.snapshot import read as read_snapshot
from kanso.data.types import type_id_of
from kanso.env.envelope import engine_version
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.hyp import HYPOTHESIS_FILE, Registration, host_resolution, host_sizing, set_status
from kanso.hyp import show as registration_of
from kanso.inbox import escalate
from kanso.nautilus import backtest
from kanso.replay.parity import Parity
from kanso.replay.parity import parity as replay_parity
from kanso.research import records
from kanso.research.lanes import DEFAULT_LANE
from kanso.research.scheduler import CERTIFIABLE as CERTIFIABLE_EVENT
from kanso.schemas import (
    Certificate,
    CertificationPlan,
    ConstructRef,
    EvaluatedGate,
    GateResult,
    Hypothesis,
    NetPerTrade,
    ObjectiveRef,
    ObjectiveResult,
    PeriodStops,
    PlannedGate,
    Power,
    RunRecord,
    StrategyFile,
    VenueModel,
    Verdict,
    load_yaml,
    parse_yaml,
)
from kanso.workspace import find

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "CERTIFIABLE",
    "CERT_STAGE",
    "LEFT_TO_CHILD",
    "RESEARCH_FAILED",
    "TIMING_SPARED",
    "UNIMPLEMENTED",
    "WINDOW_FAILED",
    "Measured",
    "Precheck",
    "Subject",
    "certify",
    "certify_at_stall",
    "pinned_sha",
    "precheck",
    "precheck_at_stall",
    "read_precheck",
    "show",
    "subject_run",
    "unpriced",
]

CERT_STAGE: Final = "cert"
"""The plan stage certification runs; the other two belong to a deployed stage."""

CERTIFIABLE: Final = frozenset({"classified", "researching", "candidate", "certified"})
"""The statuses certification starts from: a draft has no construct, and the rest are over."""

CANDIDATE: Final = "candidate"
CERTIFIED: Final = "certified"
RESEARCHING: Final = "researching"

CERTIFICATE: Final = "certificate"
"""The event kind a finished certification appends, under the hypothesis id."""

CERT_FAILED: Final = "cert_failed"
"""The escalation raised on every `n_fail`-th consecutive certification failure."""

UNIMPLEMENTED: Final = (
    "the toolbox declares this gate but nothing implements it yet, so it judged nothing"
)

PARITY: Final = "parity_replay"
"""The one cert gate whose evidence is a replay rather than a run of the window."""

WINDOW: Final = "embargoed_window"
"""The required out-of-sample gate: once it has refused a subject, the verdict is a fail."""

PLATEAU: Final = "param_plateau"
"""The cert gate that costs a backtest per perturbation, judged after all the others."""

LATENCY: Final = "latency_stress"
"""The cert gate that costs a backtest per latency multiple, judged with the plateau."""

RESEARCH_PLATEAU: Final = "research_plateau"
"""The research gate that costs a backtest of the research window per perturbation, judged
after every other research gate."""

RERUNNING: Final = frozenset({PLATEAU, LATENCY, RESEARCH_PLATEAU})
"""The cert gates that re-run the subject, judged after every cheap verdict is in hand: on the
certification window, spared when the window gate has already refused the subject; on the
research window, `research_plateau`, spared when another research gate has refused it, and
never run in the lane, which runs nothing."""

RESEARCH_FAILED: Final = (
    "a research gate refused the subject on research data and a certificate a gate refused "
    "cannot pass, so the plateau's research-window backtests were not made"
)
"""Why a re-running research gate is skipped once another has refused the subject."""

LEFT_TO_CHILD: Final = (
    "the lane runs nothing: the certification child judges this gate on the research window, "
    "before it reads the certification window"
)
"""Why the lane's precheck records a re-running research gate without judging it."""

WINDOW_FAILED: Final = (
    "embargoed_window failed and a certificate whose window failed cannot pass, so the "
    "re-run backtests were not made: a plateau or a slower route around a result the embargo "
    "refused is not evidence"
)

TIMING: Final = "entry_timing"
"""The cert gate that reads the certification window again, to price the subject's trades at
other sessions; judged with the plateau, and spared with it."""

LAST: Final = RERUNNING | {TIMING}
"""The cert gates judged after every cheap verdict is in hand: the ones that re-run the subject
and the one that reads its window again, each spared once the window gate has refused it."""

TIMING_SPARED: Final = (
    "embargoed_window failed and a certificate whose window failed cannot pass, so the window "
    "was not read again: the timing of a result the embargo refused is not evidence"
)
"""Why `entry_timing` is skipped after a failed window, in words of its own: it re-runs no
backtest, so `WINDOW_FAILED` would say what it did not spare."""

_HEX: Final = frozenset("0123456789abcdef")


# --- what is being certified --------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """One card of one hypothesis, with everything needed to run it again.

    Every field comes from the record of the run that produced the card rather than from
    the workspace as it stands: the hypothesis bytes the run pinned, the snapshot it was
    pinned to and the venue model the card was costed with. Re-running under anything else
    would compare two different experiments, and the gate that compares the certification
    window against the research one would be comparing nothing.
    """

    hyp: Hypothesis
    construct: ConstructRef
    objective_id: str
    strategy_sha: str
    hypothesis_sha: str
    source: bytes
    snapshot_id: str
    harness: Harness
    venue_model: VenueModel
    capital: float
    folds: int
    period: str
    catalog: Path
    host_source: bytes | None = None
    host_modifiers: tuple[tuple[str, bytes, Mapping[str, Any]], ...] = ()
    grains: tuple[str, ...] = ()
    sleeve_budget: float = 0.0
    research_prefix: tuple[date, date] | None = None
    certification_prefix: tuple[date, date] | None = None
    """The sessions each window warms on, resolved once when the subject is built so every
    run of a window — the subject's, the host's, a perturbation's — is fed the same span;
    `None` for a hypothesis that declares no warmup."""

    @property
    def certification(self) -> tuple[date, date]:
        """The embargoed window the plan's gates judge."""
        window = self.hyp.windows.certification
        return window.start, window.end

    @property
    def research(self) -> tuple[date, date]:
        """The window the subject was selected on, run again for the gates that compare."""
        window = self.hyp.windows.research
        return window.start, window.end

    def prefix_of(self, window: tuple[date, date]) -> tuple[date, date] | None:
        """The prefix resolved for one of the two windows this subject is run over."""
        return {
            self.research: self.research_prefix,
            self.certification: self.certification_prefix,
        }[window]


@dataclass(frozen=True)
class Measured:
    """What one certification ran: both windows, the host or the benchmark beside them,
    and what the evidence gates read off the certification window's points.

    No point of either window is kept. The evidence the availability and capacity gates
    read is folded from the certification window as the subject's run is handed it
    (`_Evidence`), and a gate that perturbs a parameter reads the window again, a chunk at
    a time, rather than finding it held: a window kept in hand for the perturbations is a
    window's worth of memory for the whole certification, and the window of a hypothesis
    on prints or quotes is what made one too large for any lane.
    """

    certification: CardRun
    research: CardRun
    evidence: _Evidence
    host_certification: CardRun | None = None
    host_research: CardRun | None = None
    benchmark_certification: CardRun | None = None
    benchmark_research: CardRun | None = None


@dataclass(frozen=True)
class _Researched:
    """The research window's runs: the subject's, and the host's or the benchmark's beside it
    where its objective reads one — everything the search correction reads, run before the
    certification window is."""

    research: CardRun
    host: CardRun | None = None
    benchmark: CardRun | None = None


@dataclass(frozen=True, init=False)
class Precheck:
    """A stall's research gates, judged on research data before any certification window is
    read: what each judged, in the order it was judged (`readings`), or why none was read —
    `reason`, given where there is no reading at all.

    The reading that decides is the first that judged and refused, else the first, else none;
    `gate`, `judged`, `passed`, `evidence` and `reason` are its. The search correction is
    judged first, so they are the correction's unless another research gate spared the stall,
    and a stall is `spared` exactly when some reading judged the subject and refused it.
    `passed` is `None` where the deciding reading judged nothing.

    `measured` says whether the research window was run for it, and every precheck states
    it: `False` in the lane, which runs nothing and reads the measurement the subject card
    kept where it can; `True` in the certification child, on the research window it has just
    run, exactly as the certificate's own research gates read it. The child runs that window
    at every stall it is started for but one on a snapshot that cannot support a claim, where
    certification runs no window at all: its precheck there judges nothing, carries the
    snapshot's reasons as its evidence, and is `False`. A stall that certifies records the
    child's precheck, so it records `True` unless its certificate failed on such a snapshot.
    """

    readings: tuple[GateResult, ...]
    measured: bool
    cause: str | None

    def __init__(
        self,
        readings: Sequence[GateResult] = (),
        *,
        measured: bool,
        reason: str | None = None,
    ) -> None:
        object.__setattr__(self, "readings", tuple(readings))
        object.__setattr__(self, "measured", measured)
        object.__setattr__(self, "cause", reason)

    @property
    def deciding(self) -> GateResult | None:
        """The first reading that refused the subject, else the first, else `None`."""
        first = self.readings[0] if self.readings else None
        return next((found for found in self.readings if _refused(found)), first)

    @property
    def spared(self) -> bool:
        """Whether a research gate judged the subject and refused it, so no certificate is
        made."""
        return any(_refused(found) for found in self.readings)

    @property
    def gate(self) -> str | None:
        """The deciding reading's gate, `None` where nothing was read."""
        deciding = self.deciding
        return None if deciding is None else deciding.id

    @property
    def judged(self) -> bool:
        """Whether the deciding reading judged the subject."""
        deciding = self.deciding
        return deciding is not None and deciding.skipped is None

    @property
    def passed(self) -> bool | None:
        """The deciding reading's verdict, `None` where it judged nothing."""
        deciding = self.deciding
        return deciding.passed if deciding is not None and self.judged else None

    @property
    def evidence(self) -> dict[str, object]:
        """The deciding reading's evidence."""
        deciding = self.deciding
        return {} if deciding is None else dict(deciding.evidence)

    @property
    def reason(self) -> str | None:
        """Why the deciding reading judged nothing — or, with no reading, why none was read."""
        deciding = self.deciding
        return self.cause if deciding is None else deciding.skipped

    def payload(self) -> dict[str, object]:
        """The precheck as JSON-shaped data: what a `stalled` event records — the deciding
        reading's keys, and every reading under `gates`."""
        found: dict[str, object] = {
            "gate": self.gate,
            "judged": self.judged,
            "pass": self.passed,
            "measured": self.measured,
            "evidence": self.evidence,
        }
        if self.reason is not None:
            found["reason"] = self.reason
        found["gates"] = [_reading_payload(reading) for reading in self.readings]
        return found


def _refused(reading: GateResult) -> bool:
    """Whether a reading judged the subject and refused it."""
    return reading.skipped is None and not reading.passed


def _reading_payload(reading: GateResult) -> dict[str, object]:
    """One reading as a `stalled` event's `gates` lists it."""
    judged = reading.skipped is None
    found: dict[str, object] = {
        "gate": reading.id,
        "judged": judged,
        "pass": reading.passed if judged else None,
        "evidence": dict(reading.evidence),
    }
    if reading.skipped is not None:
        found["reason"] = reading.skipped
    return found


def read_precheck(payload: Mapping[str, Any]) -> Precheck:
    """A precheck back from its `payload`, as the certification child reports it.

    A payload without `gates` — recorded before a stall judged more than its correction —
    reads as the one reading its own keys state, or as none where they name no gate."""
    listed = payload.get("gates")
    if listed is None:
        listed = [] if payload["gate"] is None else [payload]
    readings = tuple(
        GateResult.model_validate(
            {
                "id": entry["gate"],
                "pass": True if entry["pass"] is None else bool(entry["pass"]),
                "evidence": dict(entry["evidence"]),
                "skipped": entry.get("reason"),
            }
        )
        for entry in listed
    )
    reason = payload.get("reason")
    return Precheck(
        readings,
        measured=bool(payload["measured"]),
        reason=None if readings or reason is None else str(reason),
    )


# --- the entry points ---------------------------------------------------------


def certify(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    sha: str | None = None,
    lane: str = DEFAULT_LANE,
) -> Certificate:
    """Run the plan's certification gates for one card of a hypothesis and record the verdict.

    Plans first when no plan is pinned, and runs the plan that is pinned otherwise, which
    is also where a plan this version can no longer run is refused. Refuses when this
    subject was already certified under this plan version and this engine version, and
    when the target file exists, because a certificate is immutable. The verdict passes
    when every gate that judged something passed.
    """
    made, _ = _certify(ws, store, hyp_id, sha, lane, at_stall=False)
    assert made is not None  # only a stall's certification is spared
    return made


def certify_at_stall(
    ws: Workspace, store: StateStore, hyp_id: str, *, sha: str, lane: str = DEFAULT_LANE
) -> tuple[Certificate | None, Precheck]:
    """Certify the bytes a stall left as the best, unless a research gate refuses them on
    research data.

    `certify`, refusing what it refuses, with the research window run first and the plan's
    research gates judged on that run before the certification window is read — the
    correction for the subject's objective first, `research_plateau` last, its backtests of
    the research window made only once every other has passed — stopping at the first that
    refuses. A refusal returns no certificate beside the precheck that refused: the
    certification window was not read, nothing was written but the plan `certify` pins where
    none is pinned, and the hypothesis's status is as it was. Otherwise the hypothesis becomes
    a candidate there, recorded by a `certifiable` event, and certification goes on exactly as
    `certify` makes it, its certificate recording each research gate as this read it.

    On a snapshot that cannot support a claim certification runs no window at all, at a stall
    as by hand: its failing certificate is written with no move to candidate and no
    `certifiable` event, beside a precheck that judged nothing, `measured` `False`, its
    evidence the snapshot's reasons — the same the certificate's gates fail on. A precheck
    comes back from every stall, so what a stall records is always what its certification
    decided.
    """
    made, checked = _certify(ws, store, hyp_id, sha, lane, at_stall=True)
    assert checked is not None  # a stall's judging always says what it judged, or why not
    return made, checked


_Judged = tuple[
    list[EvaluatedGate], ObjectiveResult, Power | None, NetPerTrade | None, PeriodStops | None
]
"""What judging a plan produces: the gates, the objective, the power statement, the window's
net per trade and what its period target did."""


def _certify(
    ws: Workspace, store: StateStore, hyp_id: str, sha: str | None, lane: str, *, at_stall: bool
) -> tuple[Certificate | None, Precheck | None]:
    """The one certification `certify` and `certify_at_stall` make, spared at a stall alone."""
    registration = _registration(ws, store, hyp_id)
    _certifiable(registration)
    subject = _subject(ws, store, hyp_id, sha)
    plan = plan_for(ws, store, hyp_id, lane=lane, sha=subject.strategy_sha)
    engine = engine_version()
    n_trials = records.n_trials(store, hyp_id)
    _refuse_uncorrected(plan, subject, n_trials + records.prior_trials(store, hyp_id), registration)
    _refuse_unguarded(plan, subject, registration)
    reason = unpriced(store, hyp_id, subject.objective_id, n_trials)
    if reason is not None:
        raise PreconditionError(
            reason,
            remedy=f"research it further with `kanso research begin {hyp_id}` until two cards "
            f"are scored under {subject.objective_id}",
        )
    certificate.refuse_repeat(
        ws,
        store,
        hyp_id,
        strategy_sha=subject.strategy_sha,
        hypothesis_sha=subject.hypothesis_sha,
        n_trials=n_trials,
        plan_version=plan.plan_version,
        nautilus_version=engine,
    )

    def spare(readings: Sequence[GateResult]) -> Precheck:
        """The stall's research gates on the research run just made; a candidate if they
        hold."""
        checked = Precheck(readings, measured=True)
        if not checked.spared:
            _candidate(store, registration)
            store.event(CERTIFIABLE_EVENT, hyp_id, {"strategy_sha": subject.strategy_sha})
        return checked

    if not at_stall:
        _candidate(store, registration)
    judged, checked = _judge(ws, store, plan, subject, spare if at_stall else None)
    if judged is None:
        return None, checked
    evaluated, objective, stated, struck, stops = judged
    read = not read_no_window(gate.evidence for gate in evaluated)
    # The look is counted under the write lock the row is written under, so a certificate of
    # the lineage written while this one was judged — a sibling's lane, a `cert run` by hand —
    # is counted before it, and the next one to write counts this one.
    with store.transaction():
        made = Certificate(
            hyp_id=hyp_id,
            strategy_sha=subject.strategy_sha,
            hypothesis_sha=subject.hypothesis_sha,
            nautilus_version=engine,
            venue_model=subject.venue_model,
            snapshot_id=subject.snapshot_id,
            criteria_version=criteria_version(),
            plan_version=plan.plan_version,
            construct=subject.construct,
            objective=objective,
            gates=evaluated,
            n_trials=n_trials,
            verdict=_verdict(evaluated),
            created_at=datetime.now(tz=UTC),
            power=stated,
            net_per_trade=struck,
            period_stops=stops,
            holdout=looks_at(store, hyp_id, subject.certification).holdout() if read else None,
        )
        certificate.write(ws, store, made, subject.source)
    _apply(ws, store, made)
    return made, checked


def _candidate(store: StateStore, registration: Registration) -> None:
    """Move the hypothesis to `candidate`: certification is under way."""
    if registration.status != CANDIDATE:
        set_status(store, registration.hyp_id, CANDIDATE)


def show(ws: Workspace, store: StateStore, hyp_id: str) -> Certificate | None:
    """This hypothesis's newest certificate, or `None` when it has none."""
    _registration(ws, store, hyp_id)
    return certificate.latest(store, hyp_id)


def unpriced(store: StateStore, hyp_id: str, objective_id: str, n_trials: int) -> str | None:
    """Why the search behind a card scored under `objective_id` cannot be priced, or `None`.

    The search is priced from `SEARCH_TRIALS` trials on, by the deflation gate the toolbox
    says `deflates` the objective: over every card of the family that ran and traded and the
    settings the family declares tried outside kanso (`records.prior_trials`), with the
    spread of the cards scored in the objective's units. A search of two such draws with
    fewer than two cards in those units has no spread to price it by, and a deflation that
    cannot read its spread skips, which passes — the route a strategy carried across a re-pin
    to another objective took to an uncorrected certificate as the first card scored under
    its new one (`docs/backlog.md` row 173), and the route a declared grid would take on the
    first card of its hypothesis. `n_trials` is kanso's count of the family's cards; the
    declared settings are added to it here.
    """
    gate = deflation_gate(objective_id)
    prior = records.prior_trials(store, hyp_id)
    if n_trials + prior < SEARCH_TRIALS or gate is None:
        return None
    searched = records.candidates(store, hyp_id)
    scored = len(records.trial_metrics(store, hyp_id, objective_id))
    if searched + prior < SEARCH_TRIALS or scored >= SEARCH_TRIALS:
        return None
    beside = f", beside {prior} settings its family declares tried outside kanso" if prior else ""
    return (
        f"{hyp_id} has {searched} cards that ran and traded behind it and {scored} of them "
        f"scored under {objective_id}{beside}, so {gate} cannot read the spread of the search "
        "that selected this card: it prices the search from two cards in its units"
    )


def precheck_at_stall(
    root: Path, store: StateStore, hyp_id: str, sha: str, *, lane: str = DEFAULT_LANE
) -> Precheck:
    """A stall's plan and its research gates on research data, made in the lane in the place
    of the certification child it would start, on the workspace that child opens.

    Takes the workspace's root and never a workspace. A lane reads `kanso.toml` when it
    starts; the child reads it again (`kanso.certify.child.main`), as `kanso cert run` does,
    so under a daemon an operator has edited `[research] folds`, `return_period` or `capital`
    beneath, the child certifies by the file and the lane remembers the start. Judged on the
    lane's reading, a card measured under the old folds was judged and could be refused —
    the stall spared, a certificate the child would have written withheld — and a plan made
    on it was pinned against folds the child no longer runs. So the workspace is found again
    at `root`, as the child finds it, and both the plan and the precheck are made on that.

    The child turns a fault that is not a refusal into one its lane records and goes on from;
    in the lane nothing does, and the lane dies of it. So the two faults the lane now meets
    in the child's place end as they did in the child. One raised making the plan — a plan
    pinned on a full disk, say — is raised as the refusal the child's report made of it, exit
    1 naming what raised, with `kanso cert run ID` as the way to see it. One raised reading
    the card — a measurement this build cannot decode — reads no gate and says what raised,
    and the stall goes to the child, which never reads that measurement: it judges the
    research gates on the research run it makes.
    """
    try:
        ws = find(root)
        planned = plan_for(ws, store, hyp_id, lane=lane, sha=sha)
    except KansoError:
        raise
    except Exception as fault:
        raise KansoError(
            f"planning the certification of {sha[:7]} of {hyp_id} raised "
            f"{type(fault).__name__}: {fault}",
            Exit.ERROR,
            remedy=f"certify it by hand with `kanso cert run {hyp_id}` to see the traceback",
        ) from fault
    try:
        return precheck(ws, store, hyp_id, sha, planned)
    except KansoError:
        raise
    except Exception as fault:
        return Precheck(
            measured=False,
            reason=f"reading the newest card of {sha[:7]} raised {type(fault).__name__}: "
            f"{fault}, so the certification child judges it on the research run it makes",
        )


def precheck(
    ws: Workspace, store: StateStore, hyp_id: str, sha: str, plan: CertificationPlan
) -> Precheck:
    """The research gates `plan` carries, judged on the research run the subject card kept.

    Reads, and runs nothing. The subject is the card certification would take: the newest
    card of these bytes, under the hypothesis file its run pinned. Its stored measurement is
    read only when it was struck the way certification in `ws` would strike the research
    window now — the pin's objective, `[research] folds` and `return_period`, the subject's
    capital and the installed `criteria_version` — and otherwise every research gate is
    recorded as judging nothing, saying which differed, as it is for a card that kept no
    measurement; a stall reads `ws` as its certification child opens it
    (`precheck_at_stall`). Nor does it judge on a snapshot that cannot support a claim:
    certification runs no window there and judges no research gate, and the failing
    certificate it writes is how that snapshot reaches the operator, so the lane leaves the
    stall to it rather than spare it on a reading certification would not make. Where it does
    judge, each gate is evaluated by its own `evaluate` on the context certification builds
    for it (`_research_context`), the measurement's run in every run slot and the host and
    benchmark it was differenced against beside it, with the trials certification counts: one
    arithmetic, so what this judges is what the certificate's research gates would judge from
    the same run. The correction comes first — or, where the plan carries none, a reading
    saying so — and the others follow in the plan's order, stopping at the first that refuses;
    `research_plateau` is recorded as left to the child (`LEFT_TO_CHILD`), since it costs
    backtests of the research window and the lane runs nothing.

    The same run is what certification makes only where nothing moved since the card was
    measured that the guards cannot see. A run upgraded mid-way to a build that changed a
    return is carded by both builds under one criteria version, which names no engine either,
    so an engine patch moves a return under it too; certification resolves inputs afresh
    rather than reading them off the card — the warmup prefix, from the catalog as it stands,
    which a `kanso data load` adding a session inside the lookback moves, and an attached
    construct's budget and grains, from its host's registration as it stands; and the trials
    are counted when each reading is made. In those cases the certification child's own
    precheck, on the run it makes, is the one the certificate agrees with
    (`certify_at_stall`).

    A card whose run pinned a file without a construct or an objective has no gate to judge
    it by, and reads none: certification refuses that subject itself, with its remedy.
    """
    cards = [card for card in records.cards_of(store, hyp_id) if card.strategy_sha == sha]
    card = cards[-1]
    run = _run_of(store, hyp_id, card.run_id)
    hyp = parse_yaml(
        Hypothesis, store.get_blob(run.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    if hyp.construct is None or hyp.objective is None:
        # Certification refuses this subject outright (`_subject`), with its remedy; there
        # is no objective to judge it by, so the lane reads nothing and leaves the refusal
        # to it.
        return Precheck(
            measured=False,
            reason=f"the newest card of {sha[:7]} was researched without a construct or an "
            "objective, so there is no research gate to judge it by",
        )
    measured = records.measurement_of(store, card)
    if measured is None:
        return _unjudged(
            plan, hyp, f"the newest card of {sha[:7]} kept no measurement to judge it from"
        )
    research = ws.config.research
    expected = (
        ("objective", measured.objective.id, hyp.objective.id),
        ("folds", measured.folds, research.folds),
        ("return_period", measured.run.period, research.return_period),
        ("capital", measured.run.capital, hyp.capital or research.capital),
        ("criteria_version", measured.criteria_version, criteria_version()),
    )
    for name, was, now in expected:
        if was != now:
            return _unjudged(
                plan,
                hyp,
                f"the newest card of {sha[:7]} was measured under {name} {was}, and "
                f"certification would measure it under {now}",
            )
    snapshot = read_snapshot(ws, run.snapshot_id)
    blocked = _inadmissible(ws, snapshot)
    if blocked:
        # Certification fails every gate on this snapshot without judging a research gate,
        # and that failing certificate is how such a snapshot reaches the operator.
        return _unsupported(plan, hyp, snapshot, blocked)
    readings = _research_readings(
        store,
        plan,
        hyp,
        run.snapshot_id,
        sha,
        _Researched(measured.run, measured.host, measured.benchmark),
        measured.folds,
        stop_at_refusal=True,
    )
    return Precheck(readings, measured=False)


def _research_order(plan: CertificationPlan, hyp: Hypothesis) -> list[PlannedGate]:
    """The plan's research gates in the order they are judged: the search correction for the
    objective `hyp` pins first, the rest in the plan's order, and the one that re-runs the
    subject last, so every cheap refusal is in hand before a backtest is paid for."""
    correction = deflation_gate(cast("ObjectiveRef", hyp.objective).id)
    reading = research_gates()
    planned = [gate for gate in plan.stage_gates(CERT_STAGE) if gate.id in reading]
    return sorted(planned, key=lambda gate: (gate.id != correction, gate.id in RERUNNING))


def _uncorrected(plan: CertificationPlan, hyp: Hypothesis) -> GateResult | None:
    """The reading that says `plan` carries no search correction for the objective `hyp`
    pins, or `None` when it carries one.

    A plan carries it from the search's second draw — card, or setting of a declared grid —
    so one without it has no search to price, and the correction judges nothing; one made for
    another objective is refused by certification itself.
    """
    objective = cast("ObjectiveRef", hyp.objective).id
    gate = deflation_gate(objective)
    if gate is None or any(found.id == gate for found in plan.stage_gates(CERT_STAGE)):
        return None
    return skipped(
        gate,
        f"plan version {plan.plan_version} carries no {gate}, the search correction for "
        f"{objective}: a plan carries it from {SEARCH_TRIALS} draws, its cards and declared "
        "prior_trials together",
    )


def _unjudged(plan: CertificationPlan, hyp: Hypothesis, reason: str) -> Precheck:
    """A lane precheck that could read no research run: every research gate the plan carries,
    recorded as judging nothing, with the reason — after the plan's word that it carries no
    correction, where it carries none, as `_research_readings` reads it first, so the deciding
    reading is the correction's or that word whatever the lane could read."""
    missing = _uncorrected(plan, hyp)
    return Precheck(
        [
            *([] if missing is None else [missing]),
            *(skipped(gate.id, reason) for gate in _research_order(plan, hyp)),
        ],
        measured=False,
    )


def _research_context(
    store: StateStore,
    hyp: Hypothesis,
    planned: PlannedGate,
    snapshot_id: str,
    strategy_sha: str,
    researched: _Researched,
    folds: int,
    *,
    tunable: Mapping[str, float] | None = None,
    rerun: Callable[[Mapping[str, float]], CardRun] | None = None,
) -> GateContext:
    """What a research gate is handed, built from the research window's runs alone.

    Every run slot holds a research run — `run` and the `research_run` slots alike, the host's
    and the benchmark's beside them — and `window` is the research window, so no certification
    run can reach a research gate whatever it reads; the trials are counted as certification
    counts them. Nothing the certification window yields is set: no dataset facts, no volume,
    no cost re-run, no session prices and no replay. `rerun`, where given, runs the subject
    over the research window again with parameters moved.
    """
    objective = cast("ObjectiveRef", hyp.objective).id
    research = hyp.windows.research
    prior = records.prior_trials(store, hyp.id)
    return GateContext(
        hyp=hyp,
        construct=cast("ConstructRef", hyp.construct).id,
        stage=CERT_STAGE,
        params=dict(planned.params),
        window=(research.start, research.end),
        run=researched.research,
        host_run=researched.host,
        research_folds=folds,
        snapshot_id=snapshot_id,
        strategy_sha=strategy_sha,
        research_run=researched.research,
        host_research_run=researched.host,
        benchmark_run=researched.benchmark,
        benchmark_research_run=researched.benchmark,
        trial_metrics=records.trial_metrics(store, hyp.id, objective),
        trials=max(records.candidates(store, hyp.id) + prior, 1),
        prior_trials=prior,
        tunable=dict(tunable or {}),
        rerun=rerun,
    )


def _research_readings(
    store: StateStore,
    plan: CertificationPlan,
    hyp: Hypothesis,
    snapshot_id: str,
    strategy_sha: str,
    researched: _Researched,
    folds: int,
    *,
    tunable: Mapping[str, float] | None = None,
    rerun: Callable[[Mapping[str, float]], CardRun] | None = None,
    stop_at_refusal: bool,
) -> tuple[GateResult, ...]:
    """The plan's research gates judged on one research run, in the order `_research_order`
    fixes, each by its own `evaluate` on `_research_context`.

    A plan that carries no search correction is read as saying so first (`_uncorrected`). A
    gate nothing implements is recorded as skipped (`UNIMPLEMENTED`). One that re-runs the
    subject is recorded as left to the child where no `rerun` is given (`LEFT_TO_CHILD`), and
    as spared once a reading before it has refused the subject (`RESEARCH_FAILED`), so no
    backtest is made for a certificate that cannot pass. With `stop_at_refusal` — at a stall —
    the readings end at the first that judged and refused.
    """
    registry = gates()
    readings: list[GateResult] = []
    missing = _uncorrected(plan, hyp)
    if missing is not None:
        readings.append(missing)
    for gate in _research_order(plan, hyp):
        found = registry.get(gate.id)
        if found is None:
            readings.append(skipped(gate.id, UNIMPLEMENTED))
            continue
        if gate.id in RERUNNING and rerun is None:
            readings.append(skipped(gate.id, LEFT_TO_CHILD))
            continue
        if gate.id in RERUNNING and any(_refused(reading) for reading in readings):
            readings.append(skipped(gate.id, RESEARCH_FAILED))
            continue
        context = _research_context(
            store,
            hyp,
            gate,
            snapshot_id,
            strategy_sha,
            researched,
            folds,
            tunable=tunable,
            rerun=rerun,
        )
        reading = found.evaluate(context)
        readings.append(reading)
        if stop_at_refusal and _refused(reading):
            break
    return tuple(readings)


def _refuse_uncorrected(
    plan: CertificationPlan, subject: Subject, n_trials: int, registration: Registration
) -> None:
    """Refuse a subject whose objective's search correction the pinned plan does not carry.

    A plan is made for the objective the hypothesis is registered under, and from the
    search's second draw — `n_trials`, the cards and the declared `prior_trials` together —
    it carries that objective's deflation gate with its threshold. A card scored under
    another — one researched before a re-pin moved the objective, named by `--sha` — would
    be certified with that gate skipping on an objective it does not deflate and its own
    correction absent, so the search that selected it would go unpriced.
    """
    correction = required_deflation(subject.hyp, n_trials)
    if correction is None:
        return
    planned = next((gate for gate in plan.gates if gate.id == correction), None)
    declared = toolbox()[correction].params
    if planned is not None and all(name in planned.params for name in declared):
        return
    raise PreconditionError(
        f"{subject.strategy_sha[:7]} was scored under {subject.objective_id}, and the plan "
        f"pinned for {subject.hyp.id} carries no {correction} with its threshold, the search "
        "correction for it, so the search that selected it would go unpriced",
        remedy=f"certify a card scored under {registration.objective}, as the best is: "
        f"`kanso cert run {subject.hyp.id}`",
    )


def _refuse_unguarded(
    plan: CertificationPlan, subject: Subject, registration: Registration
) -> None:
    """Refuse a subject whose objective's guarding cert gates the pinned plan does not carry.

    A plan is made for the objective the hypothesis is registered under, and carries every cert
    gate the toolbox declares `required_for` that one — `entry_timing` on a contribution or a
    Sharpe — with every parameter set. A card scored under another objective — one researched
    before a re-pin moved it, named by `--sha` — is judged on its own, so a plan made for one
    the gate does not guard would certify a contribution or a Sharpe whose timing was never
    told from its exposure.
    """
    offered = plannable()
    for guard in required_cert_gates(subject.objective_id):
        planned = next((gate for gate in plan.gates if gate.id == guard), None)
        if guard not in offered or (
            planned is not None and all(name in planned.params for name in offered[guard].params)
        ):
            continue
        raise PreconditionError(
            f"{subject.strategy_sha[:7]} was scored under {subject.objective_id}, and the plan "
            f"pinned for {subject.hyp.id} carries no {guard} with every parameter set, which "
            f"every certificate of {subject.objective_id} is judged by, so a drift it held "
            "would be credited as timing",
            remedy=f"certify a card scored under {registration.objective}, as the best is: "
            f"`kanso cert run {subject.hyp.id}`",
        )


# --- resolving the subject ----------------------------------------------------


def _registration(ws: Workspace, store: StateStore, hyp_id: str) -> Registration:
    return cast("Registration", registration_of(ws, store, hyp_id))


def _certifiable(registration: Registration) -> None:
    """Refuse a hypothesis whose life has ended, or which was never classified."""
    if registration.status not in CERTIFIABLE:
        raise PreconditionError(
            f"{registration.hyp_id} is {registration.status}, and certification runs on "
            f"{', '.join(sorted(CERTIFIABLE))}",
            remedy=f"a draft is classified with `kanso classify {registration.hyp_id}`; a "
            "hypothesis that is over is replaced by a new one",
        )


def _subject(ws: Workspace, store: StateStore, hyp_id: str, sha: str | None) -> Subject:
    """The card to certify, and the run record that says what it was measured against.

    The workspace's extensions are imported first: every run certification makes is made
    in this process, over data that may be of a custom type an extension registers.
    """
    ext.imported(ws)
    chosen = _chosen_sha(store, hyp_id, sha)
    cards = [card for card in records.cards_of(store, hyp_id) if card.strategy_sha == chosen]
    card = cards[-1]
    run = _run_of(store, hyp_id, card.run_id)
    hyp = parse_yaml(
        Hypothesis, store.get_blob(run.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    if hyp.construct is None or hyp.objective is None:
        raise PreconditionError(
            f"{hyp_id} was researched without a construct or an objective, so there is nothing "
            "to certify it as",
            remedy=f"run `kanso classify {hyp_id}` and research it again",
        )
    harness = construct_for(hyp.construct.id, ws).harness(
        hyp, _host(ws, hyp.construct), version=run.host_version
    )
    host_source, modifiers = _host_sources(store, harness.host)
    catalog = catalog_path(ws)
    grains = backtest.grains_of(hyp, host_resolution(ws, store, harness.host))
    windows = hyp.windows
    return Subject(
        hyp=hyp,
        construct=hyp.construct,
        objective_id=hyp.objective.id,
        strategy_sha=chosen,
        hypothesis_sha=run.hypothesis_sha,
        source=store.get_blob(chosen),
        snapshot_id=run.snapshot_id,
        harness=harness,
        venue_model=card.venue_model,
        capital=hyp.capital or ws.config.research.capital,
        folds=ws.config.research.folds,
        period=ws.config.research.return_period,
        catalog=catalog,
        host_source=host_source,
        host_modifiers=modifiers,
        grains=grains,
        sleeve_budget=host_sizing(ws, store, harness.host)
        if harness.host is not None
        else (0.0 if hyp.sizing is None else hyp.sizing.budget),
        research_prefix=backtest.warmup_prefix(
            hyp, (windows.research.start, windows.research.end), catalog, grains
        ),
        certification_prefix=backtest.warmup_prefix(
            hyp, (windows.certification.start, windows.certification.end), catalog, grains
        ),
    )


def _chosen_sha(store: StateStore, hyp_id: str, sha: str | None) -> str:
    """The hypothesis's `best`, or the one card sha the given prefix names.

    A prefix is resolved among this hypothesis's own cards, so a prefix that is unique
    here is accepted however many other blobs the store holds, and one naming a card of
    another hypothesis is refused rather than silently certified.
    """
    if sha is None:
        best, _ = records.best_of(store, hyp_id)
        if best is None:
            raise PreconditionError(
                f"{hyp_id} has no best card, so there is nothing to certify",
                remedy=f"research it with `kanso research begin {hyp_id}` until a card keeps",
            )
        return best
    prefix = sha.strip().lower()
    if not prefix or not set(prefix) <= _HEX:
        raise ValidationError(
            f"sha: {sha!r} is not a strategy sha or a prefix of one",
            remedy=f"pass the hex prefix of a card's sha, as `kanso research show {hyp_id}` "
            "prints it",
        )
    rows = store.connection.execute(
        "SELECT DISTINCT strategy_sha FROM cards WHERE hyp_id = ? AND strategy_sha >= ?"
        " AND strategy_sha < ? ORDER BY strategy_sha LIMIT 2",
        (hyp_id, prefix, prefix + "g"),
    ).fetchall()
    if not rows:
        raise ValidationError(
            f"sha: no card of {hyp_id} starts with {prefix!r}",
            remedy=f"run `kanso research show {hyp_id}` to list its cards",
        )
    if len(rows) > 1:
        raise ValidationError(
            f"sha: {prefix!r} is ambiguous within {hyp_id}: "
            f"{', '.join(str(row[0])[:12] for row in rows)}",
            remedy="pass more characters of the sha",
        )
    return str(rows[0][0])


def pinned_sha(store: StateStore, hyp_id: str, sha: str) -> str:
    """The hypothesis file the newest card of these bytes was measured under, as its run
    pinned it — the pin a certification of the card would record."""
    return subject_run(store, hyp_id, sha).hypothesis_sha


def subject_run(store: StateStore, hyp_id: str, sha: str) -> RunRecord:
    """The run that produced the newest card of these bytes: what a certification of them
    is pinned to, and what its lane sized that run's cards by."""
    cards = [card for card in records.cards_of(store, hyp_id) if card.strategy_sha == sha]
    if not cards:
        raise PreconditionError(
            f"{hyp_id} has no card of {sha[:7]}",
            remedy="name a strategy this hypothesis has carded",
        )
    return _run_of(store, hyp_id, cards[-1].run_id)


def _run_of(store: StateStore, hyp_id: str, run_id: str) -> RunRecord:
    """The run that produced the subject card, which is what pinned its data."""
    found = next((run for run in records.runs_of(store, hyp_id) if run.run_id == run_id), None)
    if found is None:  # pragma: no cover - a card's run_id is a foreign key into runs
        raise PreconditionError(f"{hyp_id} has no run {run_id}")
    return found


def _host(ws: Workspace, ref: ConstructRef) -> StrategyFile | None:
    """The certified host strategy a relative construct attaches to, when it names one."""
    if ref.host is None:
        return None
    path = ws.path("strategies", ref.host, "strategy.yaml")
    if not path.is_file():
        raise PreconditionError(
            f"construct.host: {ref.host!r} is not a composed strategy of this workspace",
            remedy="certify and compose the host sleeve before certifying against it",
        )
    return load_yaml(StrategyFile, path)


def _host_sources(
    store: StateStore, host: HostRef | None
) -> tuple[bytes | None, tuple[tuple[str, bytes, Mapping[str, Any]], ...]]:
    """The host version's own sleeve bytes and the constructs already attached to it."""
    if host is None:
        return None, ()
    attached = tuple(
        (ref.construct, store.get_blob(ref.strategy_sha), dict(ref.params or {}))
        for ref in host.attached
    )
    return store.get_blob(host.sleeve.strategy_sha), attached


# --- running both windows -----------------------------------------------------


def _request(
    subject: Subject,
    window: tuple[date, date],
    *,
    host_only: bool = False,
    overrides: Mapping[str, float] | None = None,
    costs: Mapping[str, float] | None = None,
) -> backtest.RunRequest:
    """The backtest one window asks for: the sleeve, or the host with this modifier on it.

    `overrides` moves the subject's own parameters and nothing else. For an absolute
    construct the subject is the sleeve, so they replace fields of its configuration; for
    a relative one the subject is the attached modifier, so they replace the construct
    parameters it was attached with, and the host underneath it is left exactly as it was
    certified. `costs` replaces keys of the venue model's cost block — `latency_ms`, for a
    latency stress — and the venue, the harness and the extraction all read the replaced
    model, as they read the certified one.
    """
    moved = dict(overrides or {})
    venue_model = subject.venue_model.model_dump()
    if costs:
        venue_model["costs"] = {**venue_model["costs"], **costs}
    attached: Sequence[tuple[str, bytes, Mapping[str, Any]]] = ()
    version_source = version_hyp = None
    if subject.host_source is None:
        strategy, own = subject.source, moved
    else:
        strategy, own = subject.host_source, {}
        host = subject.harness.host
        assert host is not None
        version_source, version_hyp = host.sleeve_file(strategy), host.sleeve.hyp_id
        attached = (
            subject.host_modifiers
            if host_only
            else (
                *subject.host_modifiers,
                (
                    subject.harness.construct,
                    subject.source,
                    {**dict(subject.construct.params or {}), **_own_budget(subject), **moved},
                ),
            )
        )
    return backtest.RunRequest(
        hyp=subject.hyp,
        strategy_source=strategy,
        window=window,
        snapshot_id=subject.snapshot_id,
        venue_model=venue_model,
        capital=subject.capital,
        modifiers=attached,
        period=subject.period,
        overrides=own,
        grains=subject.grains,
        sleeve_budget=subject.sleeve_budget,
        prefix=subject.prefix_of(window),
        version_source=version_source,
        version_hyp=version_hyp,
    )


def _own_budget(subject: Subject) -> dict[str, float]:
    """An attached construct's own budget, as the parameter its config takes."""
    if subject.hyp.sizing is None:
        return {}
    return {"sizing_budget": subject.hyp.sizing.budget}


def _tunable(subject: Subject) -> dict[str, float]:
    """The subject's own numeric parameters, at the values it was certified at.

    A sleeve's are the numeric fields its configuration class declares beyond the ones the
    hypothesis injects; an attached construct's are the numeric construct parameters it was
    composed with, which is the whole of what a modifier is configured by.
    """
    if subject.host_source is None:
        return backtest.tunable(_request(subject, subject.certification))
    return {
        name: value
        for name, value in (subject.construct.params or {}).items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _research(subject: Subject) -> _Researched:
    """Run the subject over the research window, with the benchmark's run beside it where
    its objective is measured against one and the host's for a relative construct: every
    research run certification makes, made before the certification window is read."""
    request = _request(subject, subject.research)
    research = _ran(subject, lambda: backtest.run(request, subject.catalog))
    benchmark = (
        _ran(subject, lambda: backtest.run(backtest.benchmark(request), subject.catalog))
        if measures_benchmark(subject.hyp)
        else None
    )
    host = (
        _ran(
            subject,
            lambda: backtest.run(
                _request(subject, subject.research, host_only=True), subject.catalog
            ),
        )
        if subject.harness.relative
        else None
    )
    return _Researched(research, host, benchmark)


def _measure(subject: Subject, researched: _Researched | None = None) -> Measured:
    """Run the subject over both windows, with the host beside it for a relative construct.

    The research window's runs come first (`_research`), or are the ones given, and the
    certification window's after them. Every window is read from the catalog a chunk at a
    time and each chunk runs before the next is read, as a card's is (`backtest.run`), so
    the process certifying holds one read
    and one chunk of a window whichever run it is making, and nothing of a window once its
    run has ended. The subject's own run over the certification window is read here rather
    than inside the runner's entry point, because its chunks also pass the evidence fold on
    their way to the engine (`_Evidence`): the delays the availability gate measures and the
    volume the capacity gate compares a day's trading against are taken from the very
    chunks the subject is judged on.

    Every run is made in the process certifying rather than in a child per run, as a card
    is: the subject is a card that already passed the integrity gate and already ran, and
    the window it is run over is fixed here rather than chosen by it. A strategy that
    nevertheless raises stops the certification and says so, so the failure reaches the
    operator as a refusal rather than as a verdict — a card that will not run has not failed
    a test, it has not taken one; only a move of the research plateau that raises is a
    verdict, since the card it moves ran (`_ran`). The process certifying is the operator's
    own under `kanso cert run`, and for a stall a child of the lane that stalled, watched by
    that lane under its memory share (`kanso.certify.child`), so the daemon lane that asked
    for it survives to research something else and is left holding none of what the windows
    cost.
    """
    from kanso.nautilus.venue import PRINT_SIZE

    research = _research(subject) if researched is None else researched
    request = _request(subject, subject.certification)
    backtest.stage_of(subject.hyp, subject.certification)
    instruments, chunks = backtest.window_chunks(request, subject.catalog)
    evidence = _Evidence(
        midnight_ns(subject.certification[0]),
        instruments,
        prints=subject.venue_model.costs.limit_fill in PRINT_SIZE,
    )
    certification = _ran(
        subject,
        lambda: backtest.execute_chunked(request, instruments, map(evidence.take, chunks)),
    )
    benchmark_certification: CardRun | None = None
    if measures_benchmark(subject.hyp):
        benchmark_certification = _ran(
            subject, lambda: backtest.run(backtest.benchmark(request), subject.catalog)
        )
    host_certification: CardRun | None = None
    if subject.harness.relative:
        host_certification = _ran(
            subject,
            lambda: backtest.run(
                _request(subject, subject.certification, host_only=True), subject.catalog
            ),
        )
    return Measured(
        certification=certification,
        research=research.research,
        evidence=evidence,
        host_certification=host_certification,
        host_research=research.host,
        benchmark_certification=benchmark_certification,
        benchmark_research=research.benchmark,
    )


def _ran(
    subject: Subject,
    backtest_run: Callable[[], backtest.RunResult],
    *,
    moved: Mapping[str, float] | None = None,
) -> CardRun:
    """One backtest, with an exception turned into a refusal that names what raised — or,
    for a run of the research window with parameters `moved`, into `MovedRunError`, which a
    perturbation gate reads as a move that kept nothing.

    What the run's engine leaves is collected before the next run begins. A disposed engine
    leaves its orders, positions and components in reference cycles, which only the cyclic
    collector frees and which it reaches when it gets round to them, so a certification
    running one engine after another carried each one's until then: measured on 2026-10-14 on
    eight sessions of one name's quotes, about 150 KB a run, the peak of each run above the
    last's, and nothing of it once collected.
    """
    try:
        return backtest_run().run
    except KansoError:
        raise
    except Exception as exc:
        raised = f"{type(exc).__name__}: {exc}"
        remedy = f"research {subject.hyp.id} further, or certify a card that runs"
        if moved is not None:
            raise MovedRunError(
                f"{subject.strategy_sha[:7]} raised when certification moved "
                f"{', '.join(f'{name} to {value}' for name, value in moved.items())}: {raised}",
                raised=raised,
                remedy=remedy,
            ) from exc
        raise PreconditionError(
            f"{subject.strategy_sha[:7]} raised while certification ran it: {raised}",
            remedy=remedy,
        ) from exc
    finally:
        gc.collect()


# --- what the pinned data supports --------------------------------------------


def _inadmissible(ws: Workspace, snapshot: Snapshot) -> list[str]:
    """Why this snapshot cannot support a point-in-time claim, or nothing when it can."""
    held = manifests(ws)
    reasons: list[str] = []
    for name in snapshot.datasets:
        manifest = held.get(name)
        if manifest is None:
            reasons.append(f"{name}: this workspace no longer describes it")
        elif manifest.publication == "unknown":
            reasons.append(f"{name}: nobody declared when its points became public")
        elif manifest.adjusted:
            reasons.append(
                f"{name}: its prices are vendor-adjusted as of "
                f"{manifest.as_of or 'the day they were requested'}, so the same request "
                "returns different numbers after the next corporate action"
            )
    if not reasons and not snapshot.reproducible:
        reasons.append(f"{snapshot.snapshot_id[:12]}: the snapshot records itself irreproducible")
    return reasons


class _Evidence:
    """What the evidence gates read off the certification window, folded a chunk at a time.

    The subject's run over the certification window is handed the window through `take`, a
    chunk at a time, and each chunk is folded here on its way to the engine and handed on
    unchanged: the smallest publication delay seen per instrument and data type, and each
    instrument's traded notional per day. Nothing of a chunk is kept, so the evidence costs
    a number per instrument and type and a number per instrument and day however long the
    window is.

    Only the points from the window's open on are folded. A warmed subject is fed its prefix
    before the window, and the lags and the volume the gates read are the window's own: a
    publication delay observed before the window is not evidence about it.

    A bar's volume counts the instrument's own unit — shares for a share, contracts for a
    perpetual or a future — and the runner strikes a fill's notional as `qty x px x
    multiplier`, so a day's volume is `volume x close x multiplier` of the resolved
    definition the window was run with, read from the same table the extraction reads
    (`backtest.multipliers_of`). A share's multiplier is one; a contract of a thousand DOGE
    is a thousand times the capacity its contract count says, and one of a hundredth of a
    coin a hundredth of it.

    Under the print rules the window carries no bars — they are refused beside quotes — and
    the trade series is the tape the venue fills from, so a name's day's volume is summed from
    its prints instead, each at its own price: `size x price x multiplier` over the day's
    trades, and `capacity_vs_adv` judges a tick hypothesis rather than skipping it. Under
    `touch` and `through` a trade series may be a few prints a session loaded as a signal —
    an operator's session prints, four a day beside a grid of quotes — and summing those
    would read a day's volume a thousandfold too small and refuse every fill against it, so
    prints are read as volume only where the rule says they are the tape (`prints`). Where a
    name carries bars as well, the bars are read and the prints are not, since a day's bars
    already hold its prints' volume and summing both would count it twice.
    """

    def __init__(
        self, opens_ns: int, instruments: Sequence[object], *, prints: bool = False
    ) -> None:
        self.opens_ns = opens_ns
        self.prints = prints
        self.multipliers = backtest.multipliers_of(instruments)
        self.lags: dict[tuple[str, str], float] = {}
        """The smallest delay observed, per instrument and data type, in seconds."""
        self._volume: dict[str, dict[date, float]] = {}
        self._printed: dict[str, dict[date, float]] = {}

    def take(self, groups: Sequence[Sequence[object]]) -> Sequence[Sequence[object]]:
        """Fold one chunk's points from the window's open on, and hand the chunk back."""
        from nautilus_trader.model.data import Bar, TradeTick

        for group in groups:
            for point in group:
                stamped = cast("Any", point)
                if int(stamped.ts_init) < self.opens_ns:
                    continue
                key = (_instrument_of(point), type_id_of(point))
                delay = (int(stamped.ts_init) - int(stamped.ts_event)) / NANOS_PER_SECOND
                self.lags[key] = min(self.lags.get(key, inf), delay)
                if isinstance(point, Bar):
                    name = str(point.bar_type.instrument_id)
                    days = self._volume.setdefault(name, {})
                    day = day_of(int(point.ts_event))
                    days[day] = days.get(day, 0.0) + (
                        float(point.volume) * float(point.close) * self.multipliers.get(name, 1.0)
                    )
                elif self.prints and isinstance(point, TradeTick):
                    name = str(point.instrument_id)
                    days = self._printed.setdefault(name, {})
                    day = day_of(int(point.ts_event))
                    days[day] = days.get(day, 0.0) + (
                        float(point.size) * float(point.price) * self.multipliers.get(name, 1.0)
                    )
        return groups

    @property
    def daily_volume(self) -> dict[str, list[float]]:
        """Each instrument's daily traded notional over the window, oldest first: from its
        bars where the window held any, else from its prints."""
        volumes = {name: [days[day] for day in sorted(days)] for name, days in self._volume.items()}
        for name, days in self._printed.items():
            if name not in volumes:
                volumes[name] = [days[day] for day in sorted(days)]
        return volumes


def _session_prices(subject: Subject, keys: Sequence[PriceKey]) -> SessionPrices:
    """The subject's certification window read once more and folded to `keys`.

    Read as the subject's own run read it (`backtest.window_chunks`), a chunk at a time, each
    chunk folded and let go before the next is read, so this holds a number per key per session
    and one chunk of the window. Every point is folded at the instant it was published, its
    `ts_init`, the clock the run was handed it on and its fills were struck on, with its
    `ts_event` kept for the dated row a fill struck at it pays. A point of the prefix a warmed
    subject is fed is folded into no session; one published on the evening before the window's
    first New York date, which the window delivers, is that evening's own date's session, the
    date before the first (`Fold`). A bar is read at its close and a print at its price; a quote
    keeps its bid and ask. The split schedules are those of the definitions the window was
    resolved with, which the moved trades are restated by and which nothing a gate is handed
    carries otherwise.

    Engine facts this relies on (nautilus_trader 1.231.0): a `Bar` carries its `close` and its
    `bar_type.instrument_id`, a `TradeTick` its `price` and a `QuoteTick` its `bid_price` and
    `ask_price`, each a `Price` that `float` reads, and both ticks their `instrument_id`; every
    point carries `ts_init` and `ts_event` as nanoseconds since the epoch.
    """
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick

    request = _request(subject, subject.certification)
    instruments, chunks = backtest.window_chunks(request, subject.catalog)
    fold = Fold(keys, subject.certification, backtest.schedules_of(instruments))
    for chunk in chunks:
        for group in chunk:
            for point in group:
                if isinstance(point, Bar):
                    close = float(point.close)
                    name = str(point.bar_type.instrument_id)
                    fold.take(BAR, name, point.ts_init, close, close, point.ts_event)
                elif isinstance(point, TradeTick):
                    price = float(point.price)
                    fold.take(
                        PRINT, str(point.instrument_id), point.ts_init, price, price, point.ts_event
                    )
                elif isinstance(point, QuoteTick):
                    fold.take(
                        QUOTE,
                        str(point.instrument_id),
                        point.ts_init,
                        float(point.bid_price),
                        float(point.ask_price),
                        point.ts_event,
                    )
    return fold.prices()


def _dataset_facts(
    ws: Workspace, snapshot: Snapshot, lags: Mapping[tuple[str, str], float]
) -> tuple[DatasetFacts, ...]:
    """Each pinned dataset with the delay it was published under and the one its class needs.

    The observed delay is the smallest `ts_init - ts_event` seen in the window that was
    just run, per instrument and type (`_Evidence`); a dataset the window held no point of
    is recorded as infinitely late, since nothing in it was published too early.
    """
    held = manifests(ws)
    return tuple(
        DatasetFacts(
            dataset_id=name,
            type=held[name].type,
            publication=held[name].publication,
            min_lag_s=lags.get((held[name].instrument, held[name].type), inf),
            required_lag_s=_required_lag(held[name]),
        )
        for name in snapshot.datasets
    )


def _required_lag(manifest: Manifest) -> float:
    """The delay this dataset's class is documented to have, in seconds; zero when none is."""
    rule = PUBLICATION_RULES.get(manifest.publication_rule or "")
    if rule is None or rule.lag is None:
        return 0.0
    return rule.lag.total_seconds()


def _instrument_of(point: object) -> str:
    """The instrument a point belongs to, or the empty name for a market-wide one."""
    inner = getattr(point, "data", point)
    bar_type = getattr(inner, "bar_type", None)
    if bar_type is not None:
        return str(bar_type.instrument_id)
    instrument_id = getattr(inner, "instrument_id", None)
    return "" if instrument_id is None else str(instrument_id)


# --- the two code paths -------------------------------------------------------


def _parity(
    ws: Workspace, store: StateStore, plan: CertificationPlan, subject: Subject
) -> Parity | None:
    """Replay the subject on both code paths over the certification window, if asked to.

    The parity gate compares what the live path and the research path decided, and nothing
    but a replay can produce that: it is run here, once, only when the plan names the gate,
    and over the window the plan judges rather than the forward one a bare replay defaults
    to.

    A replay that cannot be set up at all — a host that was never composed, a window the
    catalog cannot serve — leaves the gate without its evidence rather than ending the
    certification. The gate then skips and says so, which is the same honesty every other
    gate without its context observes, and the certificate records that nothing compared
    the paths instead of claiming that they agreed.
    """
    if not any(gate.id == PARITY for gate in plan.stage_gates(CERT_STAGE)):
        return None
    opens, closes = subject.certification
    try:
        return replay_parity(
            ws,
            store,
            hyp=subject.hyp.id,
            sha=subject.strategy_sha,
            start=opens,
            end=closes,
        )
    except KansoError:
        return None


# --- judging ------------------------------------------------------------------


def _judge(
    ws: Workspace,
    store: StateStore,
    plan: CertificationPlan,
    subject: Subject,
    spare: Callable[[Sequence[GateResult]], Precheck] | None = None,
) -> tuple[_Judged | None, Precheck | None]:
    """Evaluate the plan's certification gates, measure the objective they judged, state
    what the certification window could resolve from the research window's own dispersion —
    the same statement `cert plan` makes from the card, here from the research window run
    again — and strike the certification window's net per trade from the run the gates
    judged (`net_per_trade`), beside what its period target did over that run.

    The research window is run first and the plan's research gates judged on it
    (`_research_readings`), `research_plateau` re-running the subject over that window, before
    the certification window is read; the certificate records those readings as they were
    taken, in the plan's order beside the rest, and the certification window's gates are
    judged after. `spare`, at a stall, is handed the research readings — taken to the first
    refusal — and a precheck it returns that refused the subject ends the judging there, with
    nothing else judged. By hand every research gate is judged, and once one refuses, the
    plateau's backtests are not made. The precheck `spare` returned comes back beside what was
    judged — at a stall on a snapshot that cannot support a claim, which runs neither window,
    one that judged nothing and says why (`_unsupported`)."""
    planned = plan.stage_gates(CERT_STAGE)
    if not planned:  # pragma: no cover - the planner refuses a plan that reaches no stage
        raise PreconditionError(
            f"plan version {plan.plan_version} of {plan.hyp_id} names no certification gate",
            remedy=f"replan with `kanso cert plan {plan.hyp_id} --replan`",
        )
    objective = objectives()[subject.objective_id]
    snapshot = read_snapshot(ws, subject.snapshot_id)
    blocked = _inadmissible(ws, snapshot)
    if blocked:
        return (
            [_blocked(gate, snapshot, blocked) for gate in planned],
            ObjectiveResult(id=objective.id, value=0.0, se=0.0),
            None,
            None,
            None,
        ), None if spare is None else _unsupported(plan, subject.hyp, snapshot, blocked)
    first = _research(subject)
    parameters = _tunable(subject)

    def rerun_research(overrides: Mapping[str, float]) -> CardRun:
        """The subject over the research window again, with parameters moved, read from the
        catalog a chunk at a time as its first run was. A strategy that raises at the moved
        setting raises `MovedRunError`, which the plateau records as a move that kept nothing:
        `research_plateau` is in every plan, so a refusal here would refuse every
        certification of a best that runs only where the search left it, at every stall."""
        request = _request(subject, subject.research, overrides=overrides)
        return _ran(subject, lambda: backtest.run(request, subject.catalog), moved=overrides)

    readings = _research_readings(
        store,
        plan,
        subject.hyp,
        subject.snapshot_id,
        subject.strategy_sha,
        first,
        subject.folds,
        tunable=parameters,
        rerun=rerun_research,
        stop_at_refusal=spare is not None,
    )
    checked = None if spare is None else spare(readings)
    if checked is not None and checked.spared:
        return None, checked
    measured = _measure(subject, first)
    compared = _parity(ws, store, plan, subject)
    value, se = objective.compute(
        measured.certification,
        subject.folds,
        measured.host_certification,
        measured.benchmark_certification,
    )
    researched = objective.compute(
        measured.research, subject.folds, measured.host_research, measured.benchmark_research
    )
    stated = power(*researched, subject.research, subject.certification, window_params(plan))
    facts = _dataset_facts(ws, snapshot, measured.evidence.lags)
    volume = measured.evidence.daily_volume

    def rerun(overrides: Mapping[str, float]) -> CardRun:
        """The subject over the certification window again, with parameters moved, read
        from the catalog a chunk at a time as the first run was."""
        request = _request(subject, subject.certification, overrides=overrides)
        return _ran(subject, lambda: backtest.run(request, subject.catalog))

    def rerun_costs(costs: Mapping[str, float]) -> CardRun:
        """The subject over the certification window again under a venue model whose cost
        keys are replaced, the strategy as it was."""
        request = _request(subject, subject.certification, costs=costs)
        return _ran(subject, lambda: backtest.run(request, subject.catalog))

    def session_prices(keys: Sequence[PriceKey]) -> SessionPrices:
        """The certification window read again, folded to the readings at these keys."""
        return _session_prices(subject, keys)

    registry = gates()
    research = research_gates()
    taken = {found.id: found for found in readings}
    # A research gate is recorded as it was read, before the window was: never judged again.
    verdicts: dict[str, EvaluatedGate] = {
        gate.id: _evaluated(gate, taken[gate.id]) for gate in planned if gate.id in research
    }
    for gate in _perturbation_last([gate for gate in planned if gate.id not in research]):
        implemented = registry.get(gate.id)
        if implemented is None:
            verdicts[gate.id] = _skipped(gate, UNIMPLEMENTED)
            continue
        if gate.id in LAST and _window_failed(verdicts):
            verdicts[gate.id] = _skipped(
                gate, WINDOW_FAILED if gate.id in RERUNNING else TIMING_SPARED
            )
            continue
        context = GateContext(
            hyp=subject.hyp,
            construct=subject.harness.construct,
            stage=CERT_STAGE,
            params=dict(gate.params),
            window=subject.certification,
            run=measured.certification,
            host_run=measured.host_certification,
            research_folds=subject.folds,
            snapshot_id=subject.snapshot_id,
            strategy_sha=subject.strategy_sha,
            research_run=measured.research,
            host_research_run=measured.host_research,
            benchmark_run=measured.benchmark_certification,
            benchmark_research_run=measured.benchmark_research,
            datasets=facts,
            daily_volume=volume,
            tunable=parameters,
            rerun=rerun,
            rerun_costs=rerun_costs,
            session_prices=session_prices,
            session=compared,
        )
        verdicts[gate.id] = _evaluated(gate, implemented.evaluate(context))
    return (
        [verdicts[gate.id] for gate in planned],
        ObjectiveResult(id=objective.id, value=value, se=se),
        stated,
        net_per_trade(measured.certification),
        measured.certification.period_stops,
    ), checked


def net_per_trade(run: CardRun) -> NetPerTrade | None:
    """The certification window's mean net per trade, from the run certification made of it.

    Struck here, once, from the run every gate judged — never by running the window again —
    so a book that ranks its members by it (`kanso.nautilus.book`) reads the number the
    certificate was judged on (`kanso.criteria.digest.session_edge`). `None` for a window
    that closed no trade.
    """
    edge = session_edge(run)
    if edge is None:
        return None
    value, se, sessions, trades = edge
    return NetPerTrade(
        net_per_trade_bp=value,
        net_se_session=se,
        net_t=None if not se else value / se,
        sessions_with_trades=sessions,
        trades=trades,
    )


def _perturbation_last(planned: Sequence[PlannedGate]) -> list[PlannedGate]:
    """The plan's cert gates with the ones that cost a backtest per move, or a read of the
    window, last.

    Every cheap verdict is then in hand before the expensive ones are paid for, the window
    gate's among them, which is the one that decides whether they are paid for at all. Only
    the judging is reordered: the certificate lists the gates in the plan's order.
    """
    return sorted(planned, key=lambda gate: gate.id in LAST)


def _window_failed(verdicts: Mapping[str, EvaluatedGate]) -> bool:
    """Whether the out-of-sample gate judged this subject and refused it.

    A window gate that skipped is an absence of evidence rather than a refusal, and spares
    nothing: the plateau then runs, and refuses a loss itself before moving anything.
    """
    window = verdicts.get(WINDOW)
    return window is not None and window.skipped is None and not window.passed


def _evaluated(planned: PlannedGate, result: GateResult) -> EvaluatedGate:
    """One gate's verdict, with the values it was given and the evidence it produced."""
    return EvaluatedGate.model_validate(
        {
            "id": result.id,
            "stage": planned.stage,
            "params": dict(planned.params),
            "evidence": dict(result.evidence),
            "pass": result.passed,
            "skipped": result.skipped,
        }
    )


def _skipped(planned: PlannedGate, reason: str) -> EvaluatedGate:
    """A gate that could not judge, recorded as passing and saying what it lacked."""
    return EvaluatedGate.model_validate(
        {
            "id": planned.id,
            "stage": planned.stage,
            "params": dict(planned.params),
            "pass": True,
            "skipped": reason,
        }
    )


def _blocked(planned: PlannedGate, snapshot: Snapshot, reasons: Sequence[str]) -> EvaluatedGate:
    """A gate that would have judged data nothing can vouch for, recorded as failed."""
    return EvaluatedGate.model_validate(
        {
            "id": planned.id,
            "stage": planned.stage,
            "params": dict(planned.params),
            "evidence": {"snapshot_id": snapshot.snapshot_id, INADMISSIBLE: list(reasons)},
            "pass": False,
        }
    )


def _unsupported(
    plan: CertificationPlan, hyp: Hypothesis, snapshot: Snapshot, reasons: Sequence[str]
) -> Precheck:
    """A stall's precheck on a snapshot that cannot support a claim: certification runs no
    window there and judges no research gate, so neither the lane nor the child judges one,
    and each records every research gate the plan carries as judging nothing, with the
    evidence the certificate's own gates fail on (`_blocked`). The correction for the
    objective `hyp` pins is read first whether or not the plan carries it, as a plan that
    carries none is read first everywhere else (`_uncorrected`), so the deciding reading is
    always the correction's."""
    evidence = {"snapshot_id": snapshot.snapshot_id, INADMISSIBLE: list(reasons)}
    reason = (
        f"snapshot {snapshot.snapshot_id[:12]} cannot support a claim, so certification runs "
        "no window on it and judges no research gate: its evidence says why"
    )
    missing = _uncorrected(plan, hyp)
    ids = [
        *([] if missing is None else [missing.id]),
        *(gate.id for gate in _research_order(plan, hyp)),
    ]
    return Precheck(
        [
            GateResult.model_validate(
                {"id": gate, "pass": True, "evidence": evidence, "skipped": reason}
            )
            for gate in ids
        ],
        measured=False,
    )


def _verdict(evaluated: Sequence[EvaluatedGate]) -> Verdict:
    """A pass exactly when every gate that judged something passed."""
    judged = [gate for gate in evaluated if gate.skipped is None]
    return "pass" if all(gate.passed for gate in judged) else "fail"


# --- what the verdict does ----------------------------------------------------


def _apply(ws: Workspace, store: StateStore, made: Certificate) -> None:
    """Move the hypothesis, and tell the operator on the failure cadence.

    A failing verdict never ends anything. It returns the hypothesis to research and
    escalates on every `n_fail`-th consecutive failure, because how many times an idea
    has failed to certify is worth saying out loud and is not the framework's decision to
    act on. Only an operator ends a line of research, with `kanso hyp retire`.
    """
    hyp_id = made.hyp_id
    store.event(
        CERTIFICATE,
        hyp_id,
        {
            "sha": made.sha7,
            "verdict": made.verdict,
            "plan_version": made.plan_version,
            "nautilus_version": made.nautilus_version,
        },
    )
    if made.verdict == "pass":
        _set_failures(store, hyp_id, 0)
        set_status(store, hyp_id, CERTIFIED)
        # Composition reads certificates; this is the one call back the other way, so the
        # import is deferred and the cycle exists only while a verdict is being applied.
        from kanso.portfolio.lifecycle import on_certified

        on_certified(ws, store, made)
        return
    failures = _set_failures(store, hyp_id, _failures(store, hyp_id) + 1)
    set_status(store, hyp_id, RESEARCHING)
    if failures % ws.config.certify.n_fail:
        return
    failing = ", ".join(gate.id for gate in made.gates if gate.skipped is None and not gate.passed)
    escalate(
        ws,
        store,
        CERT_FAILED,
        hyp_id,
        f"{failures} consecutive certification failures over {made.n_trials} trials;"
        f" {made.sha7} failed on {failing}",
    )


def _failures(store: StateStore, hyp_id: str) -> int:
    """How many certifications of this hypothesis have failed in a row."""
    row = store.connection.execute(
        "SELECT consecutive_cert_failures FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return int(row[0])


def _set_failures(store: StateStore, hyp_id: str, count: int) -> int:
    store.connection.execute(
        "UPDATE hypotheses SET consecutive_cert_failures = ? WHERE hyp_id = ?", (count, hyp_id)
    )
    return count
