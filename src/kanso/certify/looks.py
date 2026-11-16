"""How many times a hypothesis's lineage has already looked at a certification window.

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
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, MutableMapping, Set
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import yaml

from kanso.errors import ValidationError
from kanso.research import records
from kanso.schemas import DateWindow, Holdout

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore

__all__ = [
    "INADMISSIBLE",
    "Look",
    "Looks",
    "Window",
    "certification_window",
    "looks",
    "read_no_window",
    "subject_pin",
    "window_of",
]

INADMISSIBLE: Final = "inadmissible"
"""The evidence key a gate failed on a snapshot that cannot support a claim records its
reasons under: a certificate whose every gate carries it read no window."""

Window = tuple[date, date]


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
) -> Looks:
    """The certificates `hyp_id`'s lineage has made at a certification window intersecting
    `window` that read one, oldest first.

    A caller asking about many hypotheses at once — `kanso status`, one row each — passes the
    graph `records.links` read once as `linked`, and one `pinned` for every call, which holds
    the windows already read by pin and gains those read here, so each registered file is
    parsed once rather than once per hypothesis asked about."""
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
        seen = window_of(store, row["hypothesis_sha"] or pins.get(str(row["hyp_id"])), windows)
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
    store: StateStore, pin: object, pinned: MutableMapping[object, Window | None]
) -> Window | None:
    """`certification_window` of `pin`, read once into `pinned` and from it after."""
    if pin not in pinned:
        pinned[pin] = certification_window(store, pin)
    return pinned[pin]


def certification_window(store: StateStore, pin: object) -> Window | None:
    """The certification window a pinned hypothesis file declares.

    The file is read as a plain mapping, so a pin written under an older schema is still read,
    and its two bounds by the schema's own `DateWindow`, so every spelling of a day the schema
    admitted the file with — a date, a midnight timestamp, quoted or not, with a zone or none —
    reads as the day it admitted. `None` for no pin, a file the store does not hold or that
    does not read, and one that declares no certification window or one `DateWindow` refuses:
    none of them shows where its certificate looked.
    """
    if not pin or not store.has_blob(str(pin)):
        return None
    try:
        found = yaml.safe_load(store.get_blob(str(pin)).decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None
    windows = found.get("windows") if isinstance(found, dict) else None
    span = windows.get("certification") if isinstance(windows, dict) else None
    if not isinstance(span, dict):
        return None
    try:
        read = DateWindow.model_validate({"start": span.get("start"), "end": span.get("end")})
    except ValidationError:
        return None
    return read.start, read.end


def _intersect(one: Window, other: Window) -> bool:
    """Whether two closed spans of days share a day."""
    return one[0] <= other[1] and other[0] <= one[1]


def read_no_window(evidence: Iterable[Mapping[str, object]]) -> bool:
    """Whether a certificate whose gates recorded this evidence failed every one on a
    snapshot that cannot support a claim, so it ran no window and is no look. A certificate
    names at least one gate."""
    return all(INADMISSIBLE in recorded for recorded in evidence)
