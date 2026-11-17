"""How many times a hypothesis's lineage has already looked at a hold-out window: at a
certification window, and at its forward window.

A certification window is the one sample research never saw, and every certificate reads it:
pass or fail, the verdict is a look, and a lineage that looks again — a new best, a replan,
an engine upgrade's re-certification, a sibling written after the first failed — is drawing
again from the same sample. Nothing about a certificate's own gates counts the draws, so this
module does. The count is shown — to the planner when it plans (`PlanInputs.looks`), on the
certificate (`Certificate.holdout`), on `kanso status` — and judges nothing: every gate that
judges does so by the threshold the plan pins at any look, and a search correction that skips
judges a look by none and passes, so how high the bar sits at a later look stays the
planner's, at a replan. A certificate whose correction skipped is a look like any other.

**A look is a certificate of the lineage that read a window intersecting this one.** The
lineage is the connected component of `derived_from` among registered hypotheses
(`kanso.research.records.lineage`): a sibling certified on the same window looked at the same
data, so it is counted, where `family` — ancestors only — would let each new sibling start
again at one. Every certificate of every member counts, under any plan version, engine,
pin or verdict, whose certification window — the `windows.certification` of the hypothesis
file its row names, its two bounds read by the schema's own `DateWindow` from the file as a
plain mapping, so a pin written under an older schema is still read and every spelling of a
day the schema admits reads as the day it admitted — intersects the window asked about.
Intersection rather than equality, so a re-pin that moves the window by a day does not reset
the count, and a re-pin onto a window wholly after it starts a new one once its subject is
read there (below). A row written before certificates recorded their pin names none and is
read under its hypothesis's pin as it stands; a row whose file the store does not hold, or
which declares no certification window that reads as one, cannot be shown to have looked
elsewhere and counts.

**The window asked about is the one the subject is read on.** Certification runs a card again
under the hypothesis file the research run that carded it pinned (`kanso.certify.run`), so
its certificate reads that file's certification window, which need not be the one its
hypothesis is registered under now: the window is no part of a hypothesis's scope, so a
re-pin that moves only the window keeps the best, and the best is certified on the window
of the run that carded it until a research run under the new pin cards those bytes again.
The planner and `kanso status` therefore count at the window of the file `subject_pin`
names — the newest card's run's, of the best or of the card a certification names — so the
next certificate of that subject is look `looks + 1`; a hypothesis with no best counts at
its own pinned window, which its first card's run will pin.

**A certificate that read no window is not a look.** On a snapshot that cannot support a
claim certification runs nothing and records every gate as failed with the snapshot's
reasons under `inadmissible` (`kanso.certify.run`); it read nothing of the window, so it is
left out. A stall spared by its search correction writes no certificate and is no look
either.

Nothing here is stored. The count is struck from the certificates table each time it is
asked for, so certificates written before kanso counted looks count at once, and the one a
certificate records (`Certificate.holdout`) is what stood when it was written: certification
counts under the write lock its row is written under (`kanso.certify.run`).

**A look at the forward window.** The forward window — the days from a hypothesis's
`windows.forward.start` on — is the one no research or certification run reads, and every act
that shows someone a day of it is a read of it, recorded as a `forward_read` event whose subject
is the hypothesis whose window it read (`record_read`, the one writer): how the read was made —
by a paper or live stage (`STAGE`) or a replay (`REPLAY`) — what it was of — a composed version
`S@V`, or a replay's session — the target, the stage or replay mode, the session, the forward
start it was judged against, and the forward days it read, from the later of its range's start
and that forward start through its range's end. A stage read also records `shown`, the days on
which the stage released the version's own decisions, which is what a read is exempted by; a
read recorded `exempt` counts nothing. A paper or live stage records one read per version per
window it closes (`kanso.portfolio.deploy`), of `S@V`, judged against the forward start of its
sleeve's registered pin: every stage window reaches it, and its read is on record before the
window's realised result is. A replay whose range reaches its target's forward window records
one read of its node session (`kanso.replay.run`) — a parity one for both its paths — charged
to the target's hypothesis, and a second, of the same session, to its host sleeve's where the
target is an attached construct's card run on its host version; a parity of a version `S@V`
whose forward days lie wholly inside the days the stages showed that version (`ran_on_stage`)
is recorded `exempt`, since the stage's own look read and showed them. The looks are struck
from the reads each time they are asked for (`forward_looks`) and nothing else about a look is
stored:

* the lineage is `kanso.research.records.lineage` as it stands when counted, the one the
  certification looks are counted over, so a re-pin that drops a `derived_from` link splits
  the lineage and its looks alike;
* every read of a lineage member that is not exempt and whose last day is on or after the
  forward start asked about counts — it shares a day with the window — and a read whose days
  do not parse cannot be shown to have read elsewhere, so it counts at every start, as a
  certificate whose window does not read counts at every window;
* reads are grouped by what they were of: every read of one version `S@V` is that version's
  one look, however many windows its stages close and however many passes judge it, as one
  certificate is one look however many gates read its window; every read of one replay is
  that replay's one look, however many members of the lineage it read;
* looks are ordered by when their first read was recorded, so a version's place never moves
  when later looks arrive; a re-pin that joins or splits the lineage can move it, as it moves
  a certificate's.

The count judges nothing. It is shown on `forward_holdout`'s evidence, to the planner
(`PlanInputs.forward_looks`), on `kanso status` — at the forward start of each hypothesis's
registered pin (`forward_start`) — and by `kanso replay`. A read is read back totally
(`ForwardRead.of_event`): a detail kanso did not write — a key missing, a value of another
type, a day that does not parse — reads as an empty text, no day and no `shown`, never as an
exception, so one corrupt event never ends the pass that counts it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, MutableMapping, Sequence, Set
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

import yaml

from kanso.errors import ValidationError
from kanso.research import records
from kanso.schemas import DateWindow, Holdout, OpenWindow

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import Event, StateStore

__all__ = [
    "EXEMPT_STAGE",
    "FORWARD_READ",
    "INADMISSIBLE",
    "REPLAY",
    "STAGE",
    "Declared",
    "ForwardLook",
    "ForwardLooks",
    "ForwardRead",
    "Look",
    "Looks",
    "Window",
    "certification_window",
    "forward_looks",
    "forward_start",
    "looks",
    "ran_on_stage",
    "read_no_window",
    "read_of",
    "reads",
    "record_read",
    "stage_days",
    "subject_pin",
    "window_of",
]

INADMISSIBLE: Final = "inadmissible"
"""The evidence key a gate failed on a snapshot that cannot support a claim records its
reasons under: a certificate whose every gate carries it read no window."""

Window = tuple[date, date]

Declared = MutableMapping[object, Mapping[str, object] | None]
"""The `windows` each pinned file declares, by pin, as the files were read: a caller reading
several windows of one file — `kanso status`, its certification window and its forward start —
passes one, so the file is parsed once."""

FORWARD_READ: Final = "forward_read"
"""The event a read of a forward window is recorded as, its subject the hypothesis whose
window it read."""

STAGE: Final = "stage"
REPLAY: Final = "replay"
"""How a forward read was made: a paper or live stage realising a window, or a replay."""

EXEMPT_STAGE: Final = "stage"
"""Why a read counts nothing: a stage's own look had already read, and shown, its days."""


@dataclass(frozen=True)
class Look:
    """One certificate that looked at the window: whose, of which bytes, under which plan and
    when — never what it found. The count is shown to the planner (`kanso.certify.plan`),
    which is never shown a result, so nothing a look carries is one."""

    hyp_id: str
    sha7: str
    plan_version: int
    created_at: str


@dataclass(frozen=True)
class Looks:
    """The looks a lineage has already taken at one certification window."""

    window: Window
    lineage: tuple[str, ...]
    prior: tuple[Look, ...]

    @property
    def look(self) -> int:
        """Which look the next certificate at this window is: one more than those taken."""
        return len(self.prior) + 1

    def holdout(self) -> Holdout:
        """What the next certificate at this window records about the looks before it."""
        start, end = self.window
        return Holdout(
            window_start=start, window_end=end, look=self.look, lineage=list(self.lineage)
        )


def looks(
    store: StateStore,
    hyp_id: str,
    window: Window,
    *,
    linked: Mapping[str, Set[str]] | None = None,
    pinned: MutableMapping[object, Window | None] | None = None,
    declared: Declared | None = None,
) -> Looks:
    """The certificates `hyp_id`'s lineage has made at a certification window intersecting
    `window` that read one, oldest first.

    A caller asking about many hypotheses at once — `kanso status`, one row each — passes the
    graph `records.links` read once as `linked`, and one `pinned` for every call, which holds
    the windows already read by pin and gains those read here, so each registered file is
    parsed once rather than once per hypothesis asked about — and one `declared`, where it reads
    other windows of the same files, so a file is parsed once for all of them."""
    lineage = records.lineage(store, hyp_id, linked)
    marks = ", ".join("?" for _ in lineage)
    pins = {
        str(row["hyp_id"]): row["hypothesis_sha"]
        for row in store.connection.execute(
            f"SELECT hyp_id, hypothesis_sha FROM hypotheses WHERE hyp_id IN ({marks})", lineage
        ).fetchall()
    }
    rows = store.connection.execute(
        "SELECT hyp_id, hypothesis_sha, strategy_sha, plan_version, created_at, gates"
        f" FROM certificates WHERE hyp_id IN ({marks}) ORDER BY created_at, rowid",
        lineage,
    ).fetchall()
    windows: MutableMapping[object, Window | None] = {} if pinned is None else pinned
    prior: list[Look] = []
    for row in rows:
        if read_no_window(gate.get("evidence") or {} for gate in json.loads(str(row["gates"]))):
            continue
        seen = window_of(
            store, row["hypothesis_sha"] or pins.get(str(row["hyp_id"])), windows, declared
        )
        if seen is not None and not _intersect(seen, window):
            continue
        prior.append(
            Look(
                hyp_id=str(row["hyp_id"]),
                sha7=str(row["strategy_sha"])[:7],
                plan_version=int(row["plan_version"]),
                created_at=str(row["created_at"]),
            )
        )
    return Looks(window=window, lineage=lineage, prior=tuple(prior))


def subject_pin(store: StateStore, hyp_id: str, sha: object, pin: object) -> object:
    """The hypothesis file a certificate of the bytes `sha` reads its certification window
    from: the one the run that made the newest card of them pinned, as certification pins its
    subject (`kanso.certify.run.subject_run`). `pin`, the hypothesis's own, where `sha` is
    `None` — a hypothesis with no best has no card to certify, and its next is carded under
    its own pin — or names no card of it."""
    if sha is None:
        return pin
    row = store.connection.execute(
        "SELECT r.hypothesis_sha FROM cards c JOIN runs r ON r.run_id = c.run_id"
        " WHERE c.hyp_id = ? AND c.strategy_sha = ? ORDER BY c.card_id DESC LIMIT 1",
        (hyp_id, str(sha)),
    ).fetchone()
    return pin if row is None else row["hypothesis_sha"]


def window_of(
    store: StateStore,
    pin: object,
    pinned: MutableMapping[object, Window | None],
    declared: Declared | None = None,
) -> Window | None:
    """`certification_window` of `pin`, read once into `pinned` and from it after."""
    if pin not in pinned:
        pinned[pin] = certification_window(store, pin, declared)
    return pinned[pin]


def certification_window(
    store: StateStore, pin: object, declared: Declared | None = None
) -> Window | None:
    """The certification window a pinned hypothesis file declares.

    The file is read as a plain mapping, so a pin written under an older schema is still read,
    and its two bounds by the schema's own `DateWindow`, so every spelling of a day the schema
    admitted the file with — a date, a midnight timestamp, quoted or not, with a zone or none —
    reads as the day it admitted. `None` for no pin, a file the store does not hold or that
    does not read, and one that declares no certification window or one `DateWindow` refuses:
    none of them shows where its certificate looked.
    """
    span = _window(store, pin, "certification", declared)
    if span is None:
        return None
    try:
        read = DateWindow.model_validate({"start": span.get("start"), "end": span.get("end")})
    except ValidationError:
        return None
    return read.start, read.end


def forward_start(store: StateStore, pin: object, declared: Declared | None = None) -> date | None:
    """The day the forward window a pinned hypothesis file declares opens on, read as
    `certification_window` reads its window — the file as a plain mapping, the day by the
    schema's own `OpenWindow` — and `None` wherever that reads none."""
    span = _window(store, pin, "forward", declared)
    if span is None:
        return None
    try:
        return OpenWindow.model_validate({"start": span.get("start")}).start
    except ValidationError:
        return None


def _window(
    store: StateStore, pin: object, name: str, declared: Declared | None
) -> Mapping[str, object] | None:
    """The window `name` a pinned hypothesis file declares, as a plain mapping, its file read
    once into `declared` where one is passed; `None` for no pin, a file the store does not hold
    or that does not read, and one declaring none."""
    if declared is None:
        windows = _windows(store, pin)
    else:
        if pin not in declared:
            declared[pin] = _windows(store, pin)
        windows = declared[pin]
    span = None if windows is None else windows.get(name)
    return span if isinstance(span, dict) else None


def _windows(store: StateStore, pin: object) -> Mapping[str, object] | None:
    """The `windows` a pinned hypothesis file declares, read as a plain mapping."""
    if not pin or not store.has_blob(str(pin)):
        return None
    try:
        found = yaml.safe_load(store.get_blob(str(pin)).decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None
    windows = found.get("windows") if isinstance(found, dict) else None
    return windows if isinstance(windows, dict) else None


def _intersect(one: Window, other: Window) -> bool:
    """Whether two closed spans of days share a day."""
    return one[0] <= other[1] and other[0] <= one[1]


def read_no_window(evidence: Iterable[Mapping[str, object]]) -> bool:
    """Whether a certificate whose gates recorded this evidence failed every one on a
    snapshot that cannot support a claim, so it ran no window and is no look. A certificate
    names at least one gate."""
    return all(INADMISSIBLE in recorded for recorded in evidence)


# --- the forward window -------------------------------------------------------


@dataclass(frozen=True)
class ForwardRead:
    """One act that showed someone days of a hypothesis's forward window, as its event holds it.

    `of` is what the read was of — `S@V` for a stage's, the node session's id for a replay's —
    and every read sharing it is one look. `first` and `last` are the forward days it read,
    `forward_start` the day they were judged against, and each is `None` where the recorded
    day does not parse. `shown` is, for a stage read, the days the stage released the
    version's own decisions on, which is what an exemption reads, and `None` for a replay or
    a stage window that showed none. `exempt` names why a read counts nothing, and is `None`
    for every read that counts. `event_id` and `at` are the event's, and zero and empty on a
    read not yet recorded."""

    hyp_id: str
    via: str
    of: str
    target: str
    where: str
    session: str
    forward_start: date | None
    first: date | None
    last: date | None
    shown: Window | None = None
    exempt: str | None = None
    event_id: int = 0
    at: str = ""

    def payload(self) -> dict[str, object]:
        """The read as its event's detail: every day as an ISO string, `shown` as two."""
        return {
            "via": self.via,
            "of": self.of,
            "target": self.target,
            "where": self.where,
            "session": self.session,
            "forward_start": _iso(self.forward_start),
            "from": _iso(self.first),
            "to": _iso(self.last),
            "shown": None if self.shown is None else [day.isoformat() for day in self.shown],
            "exempt": self.exempt,
        }

    @classmethod
    def of_event(cls, event: Event) -> ForwardRead:
        """The read one `forward_read` event records, read totally: a text missing or of
        another type is empty, a day that does not parse is `None`, a `shown` that is not two
        days in order is `None` and so exempts nothing, an exemption kanso does not grant is
        none, and a `via` kanso does not write is a replay's, which is a look of its own."""
        detail = event.detail
        via = _text(detail, "via")
        return cls(
            hyp_id=event.subject,
            via=via if via in (STAGE, REPLAY) else REPLAY,
            of=_text(detail, "of"),
            target=_text(detail, "target"),
            where=_text(detail, "where"),
            session=_text(detail, "session"),
            forward_start=_day(detail.get("forward_start")),
            first=_day(detail.get("from")),
            last=_day(detail.get("to")),
            shown=_span(detail.get("shown")),
            exempt=EXEMPT_STAGE if detail.get("exempt") == EXEMPT_STAGE else None,
            event_id=event.event_id,
            at=event.ts,
        )


@dataclass(frozen=True)
class ForwardLook:
    """One look at a forward window: a version's, every stage read of it together, or a
    replay's, every read it made together — whose, how, of what, over which forward days
    and when it was first taken; never what it showed."""

    hyp_id: str
    via: str
    of: str
    target: str
    first: date | None
    last: date | None
    at: str


@dataclass(frozen=True)
class ForwardLooks:
    """The looks a lineage has taken at the forward window that opens on `start`, oldest
    first."""

    start: date
    lineage: tuple[str, ...]
    taken: tuple[ForwardLook, ...]

    def place(self, of: str) -> int | None:
        """Which look, counting from one, the look of `of` is; `None` where none is counted."""
        return next((index for index, look in enumerate(self.taken, 1) if look.of == of), None)


def record_read(store: StateStore, read: ForwardRead) -> int:
    """Record one read of a forward window, and return its event's id."""
    return store.event(FORWARD_READ, read.hyp_id, read.payload())


def reads(store: StateStore, hyp_id: str) -> tuple[ForwardRead, ...]:
    """Every read of `hyp_id`'s forward window recorded, oldest first."""
    return tuple(
        ForwardRead.of_event(event) for event in store.events(kind=FORWARD_READ, subject=hyp_id)
    )


def read_of(store: StateStore, session_id: str) -> ForwardRead | None:
    """The read of a forward window a replay's session made, or `None` where it read none.
    Where it made two — an attached construct's card, charged to its host's sleeve too — the
    first recorded, which is its target's own where it read its own window."""
    for event in store.events(kind=FORWARD_READ):
        found = ForwardRead.of_event(event)
        if found.via == REPLAY and found.of == session_id:
            return found
    return None


def stage_days(store: StateStore, hyp_id: str, version: str) -> tuple[Window, ...]:
    """The days the paper and live stages showed `version` (`S@V`) its own decisions on, as
    its stage reads of `hyp_id`'s forward window record them in `shown`, merged and with
    adjacent days joined. A window that showed none — one that halted — adds nothing."""
    shown = sorted(
        read.shown
        for read in reads(store, hyp_id)
        if read.via == STAGE and read.of == version and read.shown is not None
    )
    merged: list[Window] = []
    for first, last in shown:
        if merged and first <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return tuple(merged)


def ran_on_stage(store: StateStore, hyp_id: str, version: str, span: Window) -> bool:
    """Whether every day of `span` is a day a stage showed `version` its own decisions on."""
    return any(
        first <= span[0] and span[1] <= last for first, last in stage_days(store, hyp_id, version)
    )


def forward_looks(
    store: StateStore,
    hyp_id: str,
    start: date,
    *,
    linked: Mapping[str, Set[str]] | None = None,
) -> ForwardLooks:
    """The looks `hyp_id`'s lineage has taken at the forward window that opens on `start`.

    Every read of a member that is not exempt and whose last day is on or after `start`, or
    does not parse, counts; the reads are grouped by what they were of, each group one look
    placed at its first read. A caller asking about many hypotheses passes the graph
    `records.links` read once as `linked`."""
    lineage = records.lineage(store, hyp_id, linked)
    counted = sorted(
        (
            read
            for member in lineage
            for read in reads(store, member)
            if read.exempt is None and (read.last is None or read.last >= start)
        ),
        key=lambda read: read.event_id,
    )
    grouped: dict[tuple[str, int], list[ForwardRead]] = {}
    for read in counted:
        grouped.setdefault((read.of, 0 if read.of else read.event_id), []).append(read)
    return ForwardLooks(
        start=start, lineage=lineage, taken=tuple(_look(group) for group in grouped.values())
    )


def _look(group: Sequence[ForwardRead]) -> ForwardLook:
    """One look from the reads that make it: placed at the first, over all their days."""
    first = group[0]
    return ForwardLook(
        hyp_id=first.hyp_id,
        via=first.via,
        of=first.of,
        target=first.target,
        first=min((read.first for read in group if read.first is not None), default=None),
        last=max((read.last for read in group if read.last is not None), default=None),
        at=first.at,
    )


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _text(detail: Mapping[str, object], key: str) -> str:
    """A text a detail records, or empty where it records none."""
    value = detail.get(key)
    return value if isinstance(value, str) else ""


def _day(value: object) -> date | None:
    """A day a detail records as an ISO string, or `None` where it does not parse."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _span(value: object) -> Window | None:
    """Two days in order a detail records, or `None` where it records anything else."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    first, last = _day(value[0]), _day(value[1])
    if first is None or last is None or last < first:
        return None
    return first, last
